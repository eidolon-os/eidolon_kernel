"""Exclusive SQLite authority for system desired state, requests and audit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path

from eidolon_system.domain.errors import (
    IdempotencyConflict,
    NotFound,
    RevisionConflict,
    StateStoreFailed,
)
from eidolon_system.domain.model import (
    DesiredServiceState,
    RuntimeIntent,
    ServiceDefinition,
    StoredMutation,
    SystemAuditEvent,
)

SCHEMA_VERSION = 1

_EXPECTED_COLUMNS = {
    "system_schema_meta": {"schema_version"},
    "system_desired_services": {"service_id", "enabled", "revision", "updated_at"},
    "system_requests": {
        "request_id",
        "operation",
        "fingerprint",
        "outcome_json",
        "audit_position",
        "created_at",
    },
    "system_audit_events": {
        "position",
        "service_id",
        "operation",
        "desired_revision",
        "enabled",
        "request_id",
        "fingerprint",
        "occurred_at",
    },
}


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _state_from_row(row: sqlite3.Row) -> DesiredServiceState:
    return DesiredServiceState(
        service_id=row["service_id"],
        enabled=bool(row["enabled"]),
        revision=row["revision"],
        updated_at=_datetime(row["updated_at"]),
    )


def _state_document(state: DesiredServiceState) -> dict[str, object]:
    return {
        "service_id": state.service_id,
        "enabled": state.enabled,
        "revision": state.revision,
        "updated_at": _timestamp(state.updated_at),
    }


def _state_from_document(document: dict[str, object]) -> DesiredServiceState:
    return DesiredServiceState(
        service_id=str(document["service_id"]),
        enabled=bool(document["enabled"]),
        revision=int(document["revision"]),
        updated_at=_datetime(str(document["updated_at"])),
    )


def _storage_errors(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.Error as exc:
            raise StateStoreFailed(f"system state storage failed: {exc}") from exc

    return guarded


class SqliteSystemStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.path.with_suffix(self.path.suffix + ".lock").open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(f"system state database is already owned: {self.path}") from exc
        self._mutex = threading.RLock()
        self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._initialize_or_reject()
        except Exception:
            self.close()
            raise

    def _initialize_or_reject(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE system_schema_meta (
                    schema_version INTEGER NOT NULL
                );
                INSERT INTO system_schema_meta(schema_version) VALUES (1);
                CREATE TABLE system_desired_services (
                    service_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE system_audit_events (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    desired_revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    request_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE system_requests (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    audit_position INTEGER NOT NULL REFERENCES system_audit_events(position),
                    created_at TEXT NOT NULL
                );
                COMMIT;
                """
            )
        self._validate_schema()
        # Execution metadata extends the existing durable request receipt. The
        # public outcome (desired state + audit position) remains immutable.
        # A partial unique index enforces one unfinished operation per service;
        # completed receipts do not participate or slow down reconciliation.
        self._connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS system_pending_runtime
            ON system_requests(json_extract(outcome_json, '$.service_id'))
            WHERE json_type(outcome_json, '$.runtime_intent') = 'object'
        """)

    def _validate_schema(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != set(_EXPECTED_COLUMNS):
            raise RuntimeError(
                "system SQLite schema is partial or unknown; migrations are unsupported"
            )
        for table, expected in _EXPECTED_COLUMNS.items():
            actual = {row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise RuntimeError(f"system SQLite table {table} does not match schema v1")
        versions = self._connection.execute(
            "SELECT schema_version FROM system_schema_meta"
        ).fetchall()
        if len(versions) != 1 or versions[0][0] != SCHEMA_VERSION:
            raise RuntimeError("system SQLite schema version is unsupported")

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None
        lock_file = getattr(self, "_lock_file", None)
        if lock_file is not None and not lock_file.closed:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    @_storage_errors
    def ensure_services(self, definitions: tuple[ServiceDefinition, ...], *, now: datetime) -> None:
        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for definition in definitions:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO system_desired_services(
                            service_id, enabled, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            definition.service_id,
                            int(definition.enabled_by_default or definition.required),
                            _timestamp(now),
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @_storage_errors
    def get(self, service_id: str) -> DesiredServiceState:
        with self._mutex:
            row = self._connection.execute(
                "SELECT * FROM system_desired_services WHERE service_id = ?", (service_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"system desired state not found: {service_id}")
        return _state_from_row(row)

    @_storage_errors
    def list_states(self) -> tuple[DesiredServiceState, ...]:
        with self._mutex:
            rows = self._connection.execute(
                "SELECT * FROM system_desired_services ORDER BY service_id"
            ).fetchall()
        return tuple(_state_from_row(row) for row in rows)

    @_storage_errors
    def get_request(
        self, *, request_id: str, operation: str, fingerprint: str
    ) -> StoredMutation | None:
        with self._mutex:
            row = self._connection.execute(
                "SELECT * FROM system_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["fingerprint"] != fingerprint:
            raise IdempotencyConflict("request_id was already used for different input")
        return StoredMutation(
            operation=row["operation"],
            fingerprint=row["fingerprint"],
            state=_state_from_document(json.loads(row["outcome_json"])),
            audit_position=row["audit_position"],
            replayed=True,
        )

    def _current_in_transaction(
        self, service_id: str, expected_revision: int
    ) -> DesiredServiceState:
        row = self._connection.execute(
            "SELECT * FROM system_desired_services WHERE service_id = ?", (service_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"system desired state not found: {service_id}")
        current = _state_from_row(row)
        if current.revision != expected_revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, current revision {current.revision}"
            )
        return current

    def _write_request_and_audit(
        self,
        *,
        state: DesiredServiceState,
        operation: str,
        request_id: str,
        fingerprint: str,
        now: datetime,
        intent: RuntimeIntent | None = None,
    ) -> StoredMutation:
        document = _state_document(state)
        if intent is not None:
            document["runtime_intent"] = asdict(intent)
        cursor = self._connection.execute(
            """
            INSERT INTO system_audit_events(
                service_id, operation, desired_revision, enabled, request_id,
                fingerprint, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.service_id,
                operation,
                state.revision,
                int(state.enabled),
                request_id,
                fingerprint,
                _timestamp(now),
            ),
        )
        position = int(cursor.lastrowid)
        self._connection.execute(
            """
            INSERT INTO system_requests(
                request_id, operation, fingerprint, outcome_json, audit_position, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                operation,
                fingerprint,
                json.dumps(document, separators=(",", ":"), sort_keys=True),
                position,
                _timestamp(now),
            ),
        )
        return StoredMutation(
            operation=operation,
            fingerprint=fingerprint,
            state=state,
            audit_position=position,
        )

    @_storage_errors
    def set_enabled(
        self,
        *,
        service_id: str,
        enabled: bool,
        expected_revision: int,
        request_id: str,
        fingerprint: str,
        now: datetime,
    ) -> StoredMutation:
        operation = "system.service.enable" if enabled else "system.service.disable"
        replay = self.get_request(
            request_id=request_id, operation=operation, fingerprint=fingerprint
        )
        if replay is not None:
            return replay
        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._current_in_transaction(service_id, expected_revision)
                state = DesiredServiceState(
                    service_id=service_id,
                    enabled=enabled,
                    revision=current.revision + 1,
                    updated_at=now,
                )
                self._connection.execute(
                    """
                    UPDATE system_desired_services
                    SET enabled = ?, revision = ?, updated_at = ?
                    WHERE service_id = ? AND revision = ?
                    """,
                    (
                        int(enabled),
                        state.revision,
                        _timestamp(now),
                        service_id,
                        expected_revision,
                    ),
                )
                result = self._write_request_and_audit(
                    state=state,
                    operation=operation,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    now=now,
                )
                self._connection.execute("COMMIT")
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @_storage_errors
    def record_operation(
        self,
        *,
        service_id: str,
        operation: str,
        expected_revision: int,
        request_id: str,
        fingerprint: str,
        now: datetime,
        intent: RuntimeIntent | None = None,
    ) -> StoredMutation:
        replay = self.get_request(
            request_id=request_id, operation=operation, fingerprint=fingerprint
        )
        if replay is not None:
            return replay
        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                state = self._current_in_transaction(service_id, expected_revision)
                if intent is not None and (
                    intent.service_id != service_id or intent.request_id != request_id
                ):
                    raise ValueError("runtime intent must belong to this request")
                result = self._write_request_and_audit(
                    state=state,
                    operation=operation,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    now=now,
                    intent=intent,
                )
                self._connection.execute("COMMIT")
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @_storage_errors
    def pending_intent(self, service_id: str) -> RuntimeIntent | None:
        with self._mutex:
            row = self._connection.execute(
                "SELECT outcome_json FROM system_requests "
                "WHERE json_type(outcome_json, '$.runtime_intent') = 'object' "
                "AND json_extract(outcome_json, '$.service_id') = ?",
                (service_id,),
            ).fetchone()
        return RuntimeIntent(**json.loads(row[0])["runtime_intent"]) if row else None

    @_storage_errors
    def put_intent(self, intent: RuntimeIntent, *, now: datetime) -> None:
        state = self.get(intent.service_id)
        payload = json.dumps(asdict(intent), sort_keys=True).encode()
        self.record_operation(
            service_id=intent.service_id,
            operation=f"system.runtime.{intent.action}",
            expected_revision=state.revision,
            request_id=intent.request_id,
            fingerprint="sha256:" + hashlib.sha256(payload).hexdigest(),
            now=now,
            intent=intent,
        )

    @_storage_errors
    def update_intent(self, intent: RuntimeIntent) -> None:
        with self._mutex:
            cursor = self._connection.execute(
                "UPDATE system_requests SET outcome_json = "
                "json_set(outcome_json, '$.runtime_intent', json(?)) "
                "WHERE request_id = ? AND json_type(outcome_json, '$.runtime_intent') = 'object'",
                (json.dumps(asdict(intent)), intent.request_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("cannot update a missing runtime intent")

    @_storage_errors
    def finish_intent(self, intent: RuntimeIntent) -> None:
        with self._mutex:
            self._connection.execute(
                "UPDATE system_requests SET outcome_json = json_remove(outcome_json, '$.runtime_intent') "
                "WHERE request_id = ?",
                (intent.request_id,),
            )

    @_storage_errors
    def list_audit(self, *, after_position: int, limit: int) -> tuple[SystemAuditEvent, ...]:
        with self._mutex:
            rows = self._connection.execute(
                """
                SELECT * FROM system_audit_events
                WHERE position > ? ORDER BY position LIMIT ?
                """,
                (after_position, limit),
            ).fetchall()
        return tuple(
            SystemAuditEvent(
                position=row["position"],
                service_id=row["service_id"],
                operation=row["operation"],
                desired_revision=row["desired_revision"],
                enabled=bool(row["enabled"]),
                request_id=row["request_id"],
                fingerprint=row["fingerprint"],
                occurred_at=_datetime(row["occurred_at"]),
            )
            for row in rows
        )
