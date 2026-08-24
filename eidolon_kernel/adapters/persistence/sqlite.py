"""Single-process SQLite authority for mounts, requests and audit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from eidolon_sdk.device_foundation.v1 import (
    ClaimActivatedEvent,
    ClaimEventCursor,
    ClaimEventStreamItem,
    DeviceRef,
)

from eidolon_kernel.domain.errors import IdempotencyConflict, RevisionConflict
from eidolon_kernel.domain.model import AuditEvent, DeviceMount
from eidolon_kernel.ports.runtime import (
    ClaimEventCommitResult,
    CommitResult,
    StoredClaimEvent,
    StoredRequest,
)

SCHEMA_VERSION = 7

_EXPECTED_COLUMNS = {
    "kernel_schema_meta": {"schema_version"},
    "kernel_device_mounts": {
        "device_id",
        "owner_id",
        "owner_domain_id",
        "owner_domain_generation",
        "claim_generation",
        "trust_epoch",
        "attached_companion_id",
        "revision",
        "created_at",
        "updated_at",
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
        "owner_domain_id",
        "owner_domain_generation",
        "claim_generation",
        "trust_epoch",
        "attached_companion_id",
        "mount_revision",
        "mount_created_at",
        "active",
        "request_id",
        "fingerprint",
        "occurred_at",
        "data_json",
    },
    "kernel_claim_event_inbox": {
        "stream_position",
        "source",
        "event_id",
        "event_fingerprint",
        "event_json",
        "event_type",
        "device_id",
        "owner_domain_id",
        "owner_domain_generation",
        "claim_generation",
        "trust_epoch",
        "manifest_id",
        "manifest_revision",
        "manifest_digest",
        "aggregate_revision",
        "outcome",
        "processed_at",
    },
    "kernel_claim_event_cursor": {
        "singleton",
        "stream_id",
        "stream_position",
        "high_watermark",
        "updated_at",
    },
}


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _canonical_event(item: ClaimEventStreamItem) -> tuple[str, str]:
    document = item.event.model_dump(mode="json")
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return encoded, "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mount_document(mount: DeviceMount) -> dict[str, Any]:
    return {
        "device_id": mount.device_id,
        "owner_id": mount.owner_id,
        "owner_domain_id": mount.owner_domain_id,
        "owner_domain_generation": mount.owner_domain_generation,
        "claim_generation": mount.claim_generation,
        "trust_epoch": mount.trust_epoch,
        "attached_companion_id": mount.attached_companion_id,
        "revision": mount.revision,
        "created_at": _timestamp(mount.created_at),
        "updated_at": _timestamp(mount.updated_at),
        "request_id": mount.request_id,
        "fingerprint": mount.fingerprint,
        "active": mount.active,
    }


def _mount_from_document(document: dict[str, Any]) -> DeviceMount:
    return DeviceMount(
        device_id=document["device_id"],
        owner_id=document["owner_id"],
        owner_domain_id=document["owner_domain_id"],
        owner_domain_generation=document["owner_domain_generation"],
        claim_generation=document["claim_generation"],
        trust_epoch=document["trust_epoch"],
        attached_companion_id=document["attached_companion_id"],
        revision=document["revision"],
        created_at=datetime.fromisoformat(document["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00")),
        request_id=document["request_id"],
        fingerprint=document["fingerprint"],
        active=document["active"],
    )


def _mount_from_row(row: sqlite3.Row) -> DeviceMount:
    return DeviceMount(
        device_id=row["device_id"],
        owner_id=row["owner_id"],
        owner_domain_id=row["owner_domain_id"],
        owner_domain_generation=row["owner_domain_generation"],
        claim_generation=row["claim_generation"],
        trust_epoch=row["trust_epoch"],
        attached_companion_id=row["attached_companion_id"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
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
                INSERT INTO kernel_schema_meta(schema_version) VALUES (7);
                CREATE TABLE kernel_device_mounts (
                    device_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    owner_domain_id TEXT NOT NULL,
                    owner_domain_generation INTEGER NOT NULL CHECK (owner_domain_generation >= 1),
                    claim_generation INTEGER NOT NULL CHECK (claim_generation >= 1),
                    trust_epoch INTEGER NOT NULL CHECK (trust_epoch >= 1),
                    attached_companion_id TEXT,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                );
                CREATE INDEX ix_kernel_mounts_owner_active
                    ON kernel_device_mounts(owner_id, active, device_id);
                CREATE INDEX ix_kernel_mounts_companion_active
                    ON kernel_device_mounts(attached_companion_id, active, device_id);
                CREATE TABLE kernel_audit_events (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    owner_domain_id TEXT NOT NULL,
                    owner_domain_generation INTEGER NOT NULL,
                    claim_generation INTEGER NOT NULL,
                    trust_epoch INTEGER NOT NULL,
                    attached_companion_id TEXT,
                    mount_revision INTEGER NOT NULL,
                    mount_created_at TEXT NOT NULL,
                    active INTEGER NOT NULL,
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
                CREATE TABLE kernel_claim_event_inbox (
                    stream_position INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_fingerprint TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    owner_domain_id TEXT NOT NULL,
                    owner_domain_generation INTEGER NOT NULL,
                    claim_generation INTEGER NOT NULL,
                    trust_epoch INTEGER NOT NULL,
                    manifest_id TEXT,
                    manifest_revision INTEGER,
                    manifest_digest TEXT,
                    aggregate_revision INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    UNIQUE(source, event_id)
                );
                CREATE INDEX ix_kernel_claim_inbox_device_position
                    ON kernel_claim_event_inbox(device_id, stream_position);
                CREATE TABLE kernel_claim_event_cursor (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    stream_id TEXT NOT NULL CHECK (stream_id = 'admission-claims-v1'),
                    stream_position INTEGER NOT NULL CHECK (stream_position >= 0),
                    high_watermark INTEGER NOT NULL CHECK (high_watermark >= stream_position),
                    updated_at TEXT NOT NULL
                );
                INSERT INTO kernel_claim_event_cursor(
                    singleton, stream_id, stream_position, high_watermark, updated_at
                ) VALUES (1, 'admission-claims-v1', 0, 0, '1970-01-01T00:00:00Z');
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
            raise RuntimeError(
                "kernel SQLite schema is partial or unknown; migrations are unsupported"
            )
        for table, expected in _EXPECTED_COLUMNS.items():
            actual = {row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise RuntimeError(
                    f"kernel SQLite table {table} does not match schema v{SCHEMA_VERSION}"
                )
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
                    raise RevisionConflict("next mount revision must equal expected revision + 1")

                current = self._connection.execute(
                    "SELECT owner_id, owner_domain_id, revision "
                    "FROM kernel_device_mounts WHERE device_id = ?",
                    (mount.device_id,),
                ).fetchone()
                actual_revision = current["revision"] if current is not None else 0
                if actual_revision != expected_revision:
                    raise RevisionConflict(
                        f"expected revision {expected_revision}, current revision is {actual_revision}"
                    )
                if current is not None and current["owner_id"] != mount.owner_id:
                    raise RevisionConflict("device mount owner namespace cannot change")
                if current is not None and current["owner_domain_id"] != mount.owner_domain_id:
                    raise RevisionConflict("device mount Owner Domain cannot change")
                values = (
                    mount.device_id,
                    mount.owner_id,
                    mount.owner_domain_id,
                    mount.owner_domain_generation,
                    mount.claim_generation,
                    mount.trust_epoch,
                    mount.attached_companion_id,
                    mount.revision,
                    _timestamp(mount.created_at),
                    _timestamp(mount.updated_at),
                    mount.request_id,
                    mount.fingerprint,
                    int(mount.active),
                )
                if current is None:
                    self._connection.execute(
                        """INSERT INTO kernel_device_mounts(
                            device_id, owner_id, owner_domain_id, owner_domain_generation,
                            claim_generation, trust_epoch,
                            attached_companion_id, revision, created_at, updated_at,
                            request_id, fingerprint, active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                else:
                    cursor = self._connection.execute(
                        """UPDATE kernel_device_mounts SET
                            owner_domain_id=?, owner_domain_generation=?,
                            claim_generation=?, trust_epoch=?,
                            attached_companion_id=?, revision=?, created_at=?, updated_at=?, request_id=?,
                            fingerprint=?, active=?
                        WHERE device_id=? AND owner_id=? AND revision=?""",
                        (
                            mount.owner_domain_id,
                            mount.owner_domain_generation,
                            mount.claim_generation,
                            mount.trust_epoch,
                            mount.attached_companion_id,
                            mount.revision,
                            _timestamp(mount.created_at),
                            _timestamp(mount.updated_at),
                            mount.request_id,
                            mount.fingerprint,
                            int(mount.active),
                            mount.device_id,
                            mount.owner_id,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RevisionConflict("mount revision changed during commit")
                event_id = f"kernel:{mount.request_id}:{mount.revision}"
                cursor = self._connection.execute(
                    """INSERT INTO kernel_audit_events(
                        event_id, event_type, device_id, owner_id, owner_domain_id,
                        owner_domain_generation,
                        claim_generation,
                        trust_epoch, attached_companion_id, mount_revision,
                        mount_created_at, active, request_id, fingerprint, occurred_at, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        event_type,
                        mount.device_id,
                        mount.owner_id,
                        mount.owner_domain_id,
                        mount.owner_domain_generation,
                        mount.claim_generation,
                        mount.trust_epoch,
                        mount.attached_companion_id,
                        mount.revision,
                        _timestamp(mount.created_at),
                        int(mount.active),
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
        self, *, after_position: int, limit: int, owner_id: str
    ) -> tuple[AuditEvent, ...]:
        sql = (
            "SELECT * FROM kernel_audit_events "
            "WHERE position > ? AND owner_id = ? ORDER BY position LIMIT ?"
        )
        values: list[Any] = [after_position, owner_id, limit]
        with self._mutex:
            rows = self._connection.execute(sql, values).fetchall()
        events = []
        for row in rows:
            document = {
                "device_id": row["device_id"],
                "owner_id": row["owner_id"],
                "owner_domain_id": row["owner_domain_id"],
                "owner_domain_generation": row["owner_domain_generation"],
                "claim_generation": row["claim_generation"],
                "trust_epoch": row["trust_epoch"],
                "attached_companion_id": row["attached_companion_id"],
                "revision": row["mount_revision"],
                "created_at": row["mount_created_at"],
                "updated_at": row["occurred_at"],
                "active": bool(row["active"]),
                "request_id": row["request_id"],
                "fingerprint": row["fingerprint"],
            }
            events.append(
                AuditEvent(
                    position=row["position"],
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    mount=_mount_from_document(document),
                    occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")),
                    data=json.loads(row["data_json"]),
                )
            )
        return tuple(events)

    def claim_event_cursor(self) -> ClaimEventCursor:
        with self._mutex:
            row = self._connection.execute(
                "SELECT stream_id, stream_position FROM kernel_claim_event_cursor WHERE singleton = 1"
            ).fetchone()
        return ClaimEventCursor(stream_id=row["stream_id"], stream_position=row["stream_position"])

    def claim_event_high_watermark(self) -> int:
        with self._mutex:
            row = self._connection.execute(
                "SELECT high_watermark FROM kernel_claim_event_cursor WHERE singleton = 1"
            ).fetchone()
        return int(row[0])

    def claim_events_for_device(self, device_id: str) -> tuple[StoredClaimEvent, ...]:
        with self._mutex:
            rows = self._connection.execute(
                "SELECT * FROM kernel_claim_event_inbox WHERE device_id = ? ORDER BY stream_position",
                (device_id,),
            ).fetchall()
        return tuple(
            StoredClaimEvent(
                stream_position=row["stream_position"],
                source=row["source"],
                event_id=row["event_id"],
                event_fingerprint=row["event_fingerprint"],
                event_type=row["event_type"],
                device_ref=DeviceRef(
                    device_instance_id=row["device_id"],
                    owner_domain_id=row["owner_domain_id"],
                    owner_domain_generation=row["owner_domain_generation"],
                    claim_generation=row["claim_generation"],
                    trust_epoch=row["trust_epoch"],
                ),
                aggregate_revision=row["aggregate_revision"],
                outcome=row["outcome"],
            )
            for row in rows
        )

    def commit_claim_event(
        self,
        *,
        item: ClaimEventStreamItem,
        requested_after: ClaimEventCursor,
        next_cursor: ClaimEventCursor,
        high_watermark: int,
        outcome: str,
        mount: DeviceMount | None,
        expected_mount_revision: int | None,
        processed_at: datetime,
    ) -> ClaimEventCommitResult:
        event = item.event
        device_ref = event.data.device_ref
        event_json, event_fingerprint = _canonical_event(item)
        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM kernel_claim_event_inbox WHERE source = ? AND event_id = ?",
                    (event.source, event.id),
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["stream_position"] == item.stream_position
                        and existing["event_fingerprint"] == event_fingerprint
                        and existing["event_json"] == event_json
                        and existing["outcome"] == outcome
                    )
                    if not same:
                        raise IdempotencyConflict(
                            "Claim source+id was reused with different bytes or outcome"
                        )
                    self._connection.execute("COMMIT")
                    current_mount = self._connection.execute(
                        "SELECT * FROM kernel_device_mounts WHERE device_id = ?",
                        (device_ref.device_instance_id,),
                    ).fetchone()
                    return ClaimEventCommitResult(
                        mount=(
                            _mount_from_row(current_mount) if current_mount is not None else None
                        ),
                        outcome=outcome,
                        replayed=True,
                    )

                cursor_row = self._connection.execute(
                    "SELECT * FROM kernel_claim_event_cursor WHERE singleton = 1"
                ).fetchone()
                if (
                    cursor_row["stream_id"] != requested_after.stream_id
                    or cursor_row["stream_position"] != requested_after.stream_position
                ):
                    raise RevisionConflict("Claim event cursor changed before checkpoint")
                if item.stream_position != requested_after.stream_position + 1:
                    raise RevisionConflict(
                        "Claim event stream item is not contiguous with persisted cursor"
                    )
                if (
                    next_cursor.stream_id != requested_after.stream_id
                    or next_cursor.stream_position != item.stream_position
                    or high_watermark < next_cursor.stream_position
                    or high_watermark < cursor_row["high_watermark"]
                ):
                    raise RevisionConflict("Claim event cursor checkpoint is inconsistent")

                committed_mount: DeviceMount | None = None
                if mount is not None:
                    if (
                        expected_mount_revision is None
                        or mount.revision != expected_mount_revision + 1
                    ):
                        raise RevisionConflict("Claim event mount revision is invalid")
                    current_mount = self._connection.execute(
                        "SELECT * FROM kernel_device_mounts WHERE device_id = ?",
                        (mount.device_id,),
                    ).fetchone()
                    actual_revision = current_mount["revision"] if current_mount is not None else 0
                    if actual_revision != expected_mount_revision:
                        raise RevisionConflict(
                            "Claim event mount revision changed before checkpoint"
                        )
                    values = (
                        mount.device_id,
                        mount.owner_id,
                        mount.owner_domain_id,
                        mount.owner_domain_generation,
                        mount.claim_generation,
                        mount.trust_epoch,
                        mount.attached_companion_id,
                        mount.revision,
                        _timestamp(mount.created_at),
                        _timestamp(mount.updated_at),
                        mount.request_id,
                        mount.fingerprint,
                        int(mount.active),
                    )
                    if current_mount is None:
                        self._connection.execute(
                            """INSERT INTO kernel_device_mounts(
                                device_id, owner_id, owner_domain_id,
                                owner_domain_generation, claim_generation,
                                trust_epoch, attached_companion_id, revision, created_at,
                                updated_at, request_id, fingerprint, active
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            values,
                        )
                    else:
                        if current_mount["owner_id"] != mount.owner_id:
                            raise RevisionConflict(
                                "Claim event cannot change Mount owner namespace"
                            )
                        if current_mount["owner_domain_id"] != mount.owner_domain_id:
                            raise RevisionConflict("Claim event cannot change Mount Owner Domain")
                        cursor = self._connection.execute(
                            """UPDATE kernel_device_mounts SET
                                owner_domain_id=?, owner_domain_generation=?,
                                claim_generation=?, trust_epoch=?,
                                attached_companion_id=?, revision=?, created_at=?, updated_at=?,
                                request_id=?, fingerprint=?, active=?
                            WHERE device_id=? AND owner_id=? AND revision=?""",
                            (
                                mount.owner_domain_id,
                                mount.owner_domain_generation,
                                mount.claim_generation,
                                mount.trust_epoch,
                                mount.attached_companion_id,
                                mount.revision,
                                _timestamp(mount.created_at),
                                _timestamp(mount.updated_at),
                                mount.request_id,
                                mount.fingerprint,
                                int(mount.active),
                                mount.device_id,
                                mount.owner_id,
                                expected_mount_revision,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RevisionConflict("Claim event Mount CAS failed")
                    self._connection.execute(
                        """INSERT INTO kernel_audit_events(
                            event_id, event_type, device_id, owner_id, owner_domain_id,
                            owner_domain_generation,
                            claim_generation, trust_epoch, attached_companion_id, mount_revision,
                            mount_created_at, active, request_id, fingerprint, occurred_at, data_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"kernel:{mount.request_id}:{mount.revision}",
                            (
                                "eidolon.kernel.device-mounted-by-claim-event.v1"
                                if mount.active
                                else "eidolon.kernel.device-unmounted-by-claim-event.v1"
                            ),
                            mount.device_id,
                            mount.owner_id,
                            mount.owner_domain_id,
                            mount.owner_domain_generation,
                            mount.claim_generation,
                            mount.trust_epoch,
                            mount.attached_companion_id,
                            mount.revision,
                            _timestamp(mount.created_at),
                            int(mount.active),
                            mount.request_id,
                            mount.fingerprint,
                            _timestamp(mount.updated_at),
                            json.dumps(
                                {
                                    "claim_event_id": event.id,
                                    "claim_event_source": event.source,
                                    "claim_event_position": item.stream_position,
                                    "claim_aggregate_revision": event.aggregaterev,
                                    "previous_revision": expected_mount_revision,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                    committed_mount = mount

                manifest_ref = (
                    event.data.manifest_ref if isinstance(event, ClaimActivatedEvent) else None
                )
                self._connection.execute(
                    """INSERT INTO kernel_claim_event_inbox(
                        stream_position, source, event_id, event_fingerprint, event_json,
                        event_type, device_id,
                        owner_domain_id, owner_domain_generation, claim_generation, trust_epoch,
                        manifest_id, manifest_revision, manifest_digest, aggregate_revision,
                        outcome, processed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.stream_position,
                        event.source,
                        event.id,
                        event_fingerprint,
                        event_json,
                        event.type,
                        device_ref.device_instance_id,
                        str(device_ref.owner_domain_id),
                        device_ref.owner_domain_generation,
                        device_ref.claim_generation,
                        device_ref.trust_epoch,
                        manifest_ref.manifest_id if manifest_ref is not None else None,
                        manifest_ref.revision if manifest_ref is not None else None,
                        manifest_ref.digest if manifest_ref is not None else None,
                        event.aggregaterev,
                        outcome,
                        _timestamp(processed_at),
                    ),
                )
                self._connection.execute(
                    """UPDATE kernel_claim_event_cursor SET
                        stream_id=?, stream_position=?, high_watermark=?, updated_at=?
                    WHERE singleton = 1""",
                    (
                        next_cursor.stream_id,
                        next_cursor.stream_position,
                        high_watermark,
                        _timestamp(processed_at),
                    ),
                )
                self._connection.execute("COMMIT")
                return ClaimEventCommitResult(
                    mount=committed_mount,
                    outcome=outcome,
                    replayed=False,
                )
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def checkpoint_claim_cursor(
        self,
        *,
        requested_after: ClaimEventCursor,
        next_cursor: ClaimEventCursor,
        high_watermark: int,
        processed_at: datetime,
    ) -> None:
        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM kernel_claim_event_cursor WHERE singleton = 1"
                ).fetchone()
                if (
                    row["stream_id"] != requested_after.stream_id
                    or row["stream_position"] != requested_after.stream_position
                    or next_cursor != requested_after
                    or high_watermark < next_cursor.stream_position
                    or high_watermark < row["high_watermark"]
                ):
                    raise RevisionConflict("empty Claim event page cursor is inconsistent")
                self._connection.execute(
                    """UPDATE kernel_claim_event_cursor SET
                        high_watermark=?, updated_at=? WHERE singleton = 1""",
                    (high_watermark, _timestamp(processed_at)),
                )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
