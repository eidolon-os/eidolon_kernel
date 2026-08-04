"""Single-process SQLite authority for mounts, requests and audit."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from eidolon_kernel.domain.errors import IdempotencyConflict, RevisionConflict
from eidolon_kernel.domain.model import Actor, AuditEvent, DeviceMount
from eidolon_kernel.ports.runtime import CommitResult, StoredRequest

SCHEMA_VERSION = 1

_EXPECTED_COLUMNS = {
    "kernel_schema_meta": {"schema_version"},
    "kernel_device_mounts": {
        "device_id",
        "owner_id",
        "companion_id",
        "revision",
        "created_at",
        "updated_at",
        "actor_id",
        "actor_owner_id",
        "actor_source",
        "request_id",
        "fingerprint",
        "active",
    },
    "kernel_requests": {
        "request_id",
        "operation",
        "fingerprint",
        "outcome_json",
        "audit_position",
        "created_at",
    },
    "kernel_audit_events": {
        "position",
        "event_id",
        "event_type",
        "device_id",
        "owner_id",
        "companion_id",
        "mount_revision",
        "mount_created_at",
        "active",
        "actor_id",
        "actor_owner_id",
        "actor_source",
        "request_id",
        "fingerprint",
        "occurred_at",
        "data_json",
    },
}


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _mount_document(mount: DeviceMount) -> dict[str, Any]:
    return {
        "device_id": mount.device_id,
        "owner_id": mount.owner_id,
        "companion_id": mount.companion_id,
        "revision": mount.revision,
        "created_at": _timestamp(mount.created_at),
        "updated_at": _timestamp(mount.updated_at),
        "actor": {
            "actor_id": mount.actor.actor_id,
            "owner_id": mount.actor.owner_id,
            "source": mount.actor.source,
        },
        "request_id": mount.request_id,
        "fingerprint": mount.fingerprint,
        "active": mount.active,
    }


def _mount_from_document(document: dict[str, Any]) -> DeviceMount:
    actor = document["actor"]
    return DeviceMount(
        device_id=document["device_id"],
        owner_id=document["owner_id"],
        companion_id=document["companion_id"],
        revision=document["revision"],
        created_at=datetime.fromisoformat(document["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00")),
        actor=Actor(
            actor_id=actor["actor_id"], owner_id=actor["owner_id"], source=actor["source"]
        ),
        request_id=document["request_id"],
        fingerprint=document["fingerprint"],
        active=document["active"],
    )


def _mount_from_row(row: sqlite3.Row) -> DeviceMount:
    return DeviceMount(
        device_id=row["device_id"],
        owner_id=row["owner_id"],
        companion_id=row["companion_id"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        actor=Actor(
            actor_id=row["actor_id"],
            owner_id=row["actor_owner_id"],
            source=row["actor_source"],
        ),
        request_id=row["request_id"],
        fingerprint=row["fingerprint"],
        active=bool(row["active"]),
    )


class SqliteMountStore:
    """Owns one SQLite file and its process lock for the full app lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.path.with_suffix(self.path.suffix + ".lock").open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(f"kernel database is already owned: {self.path}") from exc
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
                CREATE TABLE kernel_schema_meta (
                    schema_version INTEGER NOT NULL
                );
                INSERT INTO kernel_schema_meta(schema_version) VALUES (1);
                CREATE TABLE kernel_device_mounts (
                    device_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    companion_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_owner_id TEXT NOT NULL,
                    actor_source TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                );
                CREATE INDEX ix_kernel_mounts_owner_active
                    ON kernel_device_mounts(owner_id, active, device_id);
                CREATE INDEX ix_kernel_mounts_companion_active
                    ON kernel_device_mounts(companion_id, active, device_id);
                CREATE TABLE kernel_audit_events (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    companion_id TEXT NOT NULL,
                    mount_revision INTEGER NOT NULL,
                    mount_created_at TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_owner_id TEXT NOT NULL,
                    actor_source TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX ix_kernel_audit_owner_position
                    ON kernel_audit_events(owner_id, position);
                CREATE TABLE kernel_requests (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    audit_position INTEGER NOT NULL REFERENCES kernel_audit_events(position),
                    created_at TEXT NOT NULL
                );
                COMMIT;
                """
            )
        self._validate_schema()

    def _validate_schema(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != set(_EXPECTED_COLUMNS):
            raise RuntimeError("kernel SQLite schema is partial or unknown; migrations are unsupported")
        for table, expected in _EXPECTED_COLUMNS.items():
            actual = {row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise RuntimeError(f"kernel SQLite table {table} does not match schema v1")
        version_row = self._connection.execute(
            "SELECT schema_version FROM kernel_schema_meta"
        ).fetchall()
        if len(version_row) != 1 or version_row[0][0] != SCHEMA_VERSION:
            raise RuntimeError("kernel SQLite schema version is unsupported")

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None
        lock_file = getattr(self, "_lock_file", None)
        if lock_file is not None and not lock_file.closed:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def get(self, device_id: str) -> DeviceMount | None:
        with self._mutex:
            row = self._connection.execute(
                "SELECT * FROM kernel_device_mounts WHERE device_id = ?", (device_id,)
            ).fetchone()
        return _mount_from_row(row) if row is not None else None

    def get_request(self, request_id: str) -> StoredRequest | None:
        with self._mutex:
            row = self._connection.execute(
                "SELECT * FROM kernel_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        return StoredRequest(
            request_id=row["request_id"],
            operation=row["operation"],
            fingerprint=row["fingerprint"],
            mount=_mount_from_document(json.loads(row["outcome_json"])),
            audit_position=row["audit_position"],
        )

    def commit(
        self,
        *,
        mount: DeviceMount,
        expected_revision: int,
        operation: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> CommitResult:
        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing_request = self._connection.execute(
                    "SELECT * FROM kernel_requests WHERE request_id = ?", (mount.request_id,)
                ).fetchone()
                if existing_request is not None:
                    if (
                        existing_request["operation"] != operation
                        or existing_request["fingerprint"] != mount.fingerprint
                    ):
                        raise IdempotencyConflict(
                            "request_id already belongs to a different mutation"
                        )
                    self._connection.execute("COMMIT")
                    return CommitResult(
                        mount=_mount_from_document(json.loads(existing_request["outcome_json"])),
                        audit_position=existing_request["audit_position"],
                        replayed=True,
                    )

                if mount.revision != expected_revision + 1:
                    raise RevisionConflict(
                        "next mount revision must equal expected revision + 1"
                    )

                current = self._connection.execute(
                    "SELECT revision FROM kernel_device_mounts WHERE device_id = ?",
                    (mount.device_id,),
                ).fetchone()
                actual_revision = current["revision"] if current is not None else 0
                if actual_revision != expected_revision:
                    raise RevisionConflict(
                        f"expected revision {expected_revision}, current revision is {actual_revision}"
                    )
                values = (
                    mount.device_id,
                    mount.owner_id,
                    mount.companion_id,
                    mount.revision,
                    _timestamp(mount.created_at),
                    _timestamp(mount.updated_at),
                    mount.actor.actor_id,
                    mount.actor.owner_id,
                    mount.actor.source,
                    mount.request_id,
                    mount.fingerprint,
                    int(mount.active),
                )
                if current is None:
                    self._connection.execute(
                        """INSERT INTO kernel_device_mounts(
                            device_id, owner_id, companion_id, revision, created_at, updated_at,
                            actor_id, actor_owner_id, actor_source, request_id, fingerprint, active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                else:
                    cursor = self._connection.execute(
                        """UPDATE kernel_device_mounts SET
                            owner_id=?, companion_id=?, revision=?, created_at=?, updated_at=?,
                            actor_id=?, actor_owner_id=?, actor_source=?, request_id=?,
                            fingerprint=?, active=?
                        WHERE device_id=? AND revision=?""",
                        (*values[1:], mount.device_id, expected_revision),
                    )
                    if cursor.rowcount != 1:
                        raise RevisionConflict("mount revision changed during commit")
                event_id = f"kernel:{mount.request_id}:{mount.revision}"
                cursor = self._connection.execute(
                    """INSERT INTO kernel_audit_events(
                        event_id, event_type, device_id, owner_id, companion_id, mount_revision,
                        mount_created_at, active, actor_id, actor_owner_id, actor_source, request_id,
                        fingerprint, occurred_at, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        event_type,
                        mount.device_id,
                        mount.owner_id,
                        mount.companion_id,
                        mount.revision,
                        _timestamp(mount.created_at),
                        int(mount.active),
                        mount.actor.actor_id,
                        mount.actor.owner_id,
                        mount.actor.source,
                        mount.request_id,
                        mount.fingerprint,
                        _timestamp(mount.updated_at),
                        json.dumps(event_data, sort_keys=True, separators=(",", ":")),
                    ),
                )
                position = int(cursor.lastrowid)
                self._connection.execute(
                    """INSERT INTO kernel_requests(
                        request_id, operation, fingerprint, outcome_json, audit_position, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        mount.request_id,
                        operation,
                        mount.fingerprint,
                        json.dumps(_mount_document(mount), sort_keys=True, separators=(",", ":")),
                        position,
                        _timestamp(mount.updated_at),
                    ),
                )
                self._connection.execute("COMMIT")
                return CommitResult(mount=mount, audit_position=position, replayed=False)
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def list_all(self) -> tuple[DeviceMount, ...]:
        with self._mutex:
            rows = self._connection.execute(
                "SELECT * FROM kernel_device_mounts ORDER BY device_id"
            ).fetchall()
        return tuple(_mount_from_row(row) for row in rows)

    def list_audit(
        self, *, after_position: int, limit: int, owner_id: str | None = None
    ) -> tuple[AuditEvent, ...]:
        sql = "SELECT * FROM kernel_audit_events WHERE position > ?"
        values: list[Any] = [after_position]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            values.append(owner_id)
        sql += " ORDER BY position LIMIT ?"
        values.append(limit)
        with self._mutex:
            rows = self._connection.execute(sql, values).fetchall()
        events = []
        for row in rows:
            document = {
                "device_id": row["device_id"],
                "owner_id": row["owner_id"],
                "companion_id": row["companion_id"],
                "revision": row["mount_revision"],
                "created_at": row["mount_created_at"],
                "updated_at": row["occurred_at"],
                "active": bool(row["active"]),
                "actor": {
                    "actor_id": row["actor_id"],
                    "owner_id": row["actor_owner_id"],
                    "source": row["actor_source"],
                },
                "request_id": row["request_id"],
                "fingerprint": row["fingerprint"],
            }
            events.append(
                AuditEvent(
                    position=row["position"],
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    mount=_mount_from_document(document),
                    occurred_at=datetime.fromisoformat(
                        row["occurred_at"].replace("Z", "+00:00")
                    ),
                    data=json.loads(row["data_json"]),
                )
            )
        return tuple(events)
