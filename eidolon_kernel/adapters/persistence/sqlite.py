"""Single-process SQLite authority for mounts, requests and audit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from eidolon_sdk.device_foundation.v1 import (
    ClaimActivatedEvent,
    ClaimEventCursor,
    ClaimEventStreamItem,
    DeviceRef,
)

from eidolon_kernel.domain.body import BodyAssignment
from eidolon_kernel.domain.errors import IdempotencyConflict, RevisionConflict
from eidolon_kernel.domain.model import (
    AUDIT_SUBJECT_BODY_ASSIGNMENT,
    AUDIT_SUBJECT_DEVICE_MOUNT,
    AuditEvent,
    DeviceMount,
)
from eidolon_kernel.ports.runtime import (
    AssignmentCommitResult,
    ClaimEventCommitResult,
    CommitResult,
    StoredClaimEvent,
    StoredRequest,
)

SCHEMA_VERSION = 8

_EXPECTED_COLUMNS = {
    "kernel_schema_meta": {"schema_version"},
    "kernel_device_mounts": {
        "device_id",
        "owner_id",
        "owner_domain_id",
        "owner_domain_generation",
        "claim_generation",
        "trust_epoch",
        "revision",
        "created_at",
        "updated_at",
        "request_id",
        "fingerprint",
        "active",
    },
    "kernel_body_assignments": {
        "body_endpoint_id",
        "device_id",
        "endpoint_id",
        "owner_id",
        "companion_id",
        "selection_provenance",
        "change_reason",
        "mode",
        "policy_refs_json",
        "revision",
        "generation",
        "created_at",
        "updated_at",
        "request_id",
        "fingerprint",
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
        "subject",
        "subject_id",
        "subject_revision",
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


#: Where this Kernel keeps the one fact in this database that nothing else can
#: give back, and the column in it that names the chosen Companion.
#:
#: Both are spelled here rather than inline so the refusal below and the census
#: an operator asks for before repairing a Host read the same two names. The day
#: the selection moves to another table, this pair moves with it — and if it is
#: ever forgotten, the census says it cannot count rather than answering zero.
SELECTION_TABLE = "kernel_body_assignments"
SELECTION_COLUMN = "companion_id"

#: The operation that owns setting a refused database aside. Named inside the
#: refusal because the refusal is the moment somebody chooses between a command
#: and ``rm``, and until this existed only ``rm`` was on offer.
REMEDIATION_COMMAND = "eidolon <host> kernel-schema-reset"


@dataclass(frozen=True, slots=True)
class SelectionCensus:
    """What setting one refused Kernel database aside would actually destroy.

    This Kernel supports no migrations, so a schema it does not recognise is a
    Host that will not start, and the only way to start it is to set the file
    aside. That is the right refusal — opening a database of unknown shape as
    if it were this one is worse. What was missing is the price on it.

    The price is not the file. Mounts are rebuilt from the Hub's Claim stream,
    and the audit log and request ledger are evidence rather than authority. One
    fact in here is authoritative nowhere else: which Companion the Owner chose
    to answer through each Body. Only an explicit Owner command can write it
    (``domain/body.py``), the Claim stream cannot replay it because the Hub was
    never told, and a backup of this file is a backup at the schema it was taken
    at — which is precisely the shape the new Kernel refuses. Setting the file
    aside destroys it, and nothing gets it back except the Owner choosing again.

    So the count travels with the refusal. It is a count and not a guess: when
    this database does not have the table this Kernel stores selections in,
    ``selections`` is ``None`` and the refusal says it cannot count, rather than
    saying zero. A false zero here is the one answer that would make things
    worse than the silence it replaces.
    """

    schema_version: int | None
    mounts: int | None
    assignments: int | None
    selections: int | None
    unreadable: str | None = None

    def as_document(self) -> dict[str, Any]:
        """The same census as JSON, for whoever is repairing the Host.

        Ops reads this through the Kernel's own interpreter rather than
        restating the two table names on its side; there is one definition of
        where the selection lives and both readers use it.
        """

        return {
            "schema_version": self.schema_version,
            "mounts": self.mounts,
            "assignments": self.assignments,
            "selections": self.selections,
            "selection_table": SELECTION_TABLE,
            "selection_column": SELECTION_COLUMN,
            "countable": self.selections is not None,
            "unreadable": self.unreadable,
            "remediation": self.remediation(),
        }

    def cost(self) -> str:
        """One sentence naming what setting this database aside would take."""

        if self.unreadable is not None:
            return (
                "This Kernel could not read the database to say what setting it aside "
                f"would destroy ({self.unreadable}), so treat it as holding Owner "
                "Companion selections until something proves otherwise."
            )
        if self.selections is None:
            held = (
                "an unknown number of device mounts"
                if self.mounts is None
                else _plural(self.mounts, "device mount")
            )
            return (
                f"Setting it aside destroys every Owner Companion selection it holds, and "
                f"this Kernel cannot say how many that is: the database has no "
                f"{SELECTION_TABLE}.{SELECTION_COLUMN} to count. It holds {held}."
            )
        if self.selections == 0:
            return (
                "Setting it aside destroys no Owner Companion selection: of the "
                f"{_plural(self.assignments or 0, 'Body assignment')} here, none names a "
                "Companion."
            )
        return (
            f"Setting it aside destroys {self.selections} of "
            f"{_plural(self.assignments or 0, 'Body assignment')} — the Owner's choice of "
            "which Companion answers through each of those devices."
        )

    def remediation(self) -> str | None:
        """The exact command that will set *this* database aside, flags and all.

        Flags and all, because a bare ``--apply`` is not one command — it is
        three, and which one it is depends on the census. The first version of
        this named ``--apply`` in every state, including the state where the
        repair demands an acknowledgement ``--apply`` alone does not carry. On
        the most likely database in the workspace — one schema version behind,
        so with no ``kernel_body_assignments`` at all — that produced a refusal
        telling the operator to run a command that always refused back.

        Which is the same defect this whole census exists to remove, one layer
        up: a path that reads as available and is not. So the sentence names the
        command that works, and ops holds a test that every command named here
        is one its gate accepts.
        """

        if self.unreadable is not None:
            # Nothing is promised here on purpose. A database this Kernel could
            # not open is not one it can prescribe an ending for.
            return None
        if self.selections is None:
            return f"{REMEDIATION_COMMAND} --apply --forget-uncounted-selections"
        if self.selections == 0:
            return f"{REMEDIATION_COMMAND} --apply"
        return f"{REMEDIATION_COMMAND} --apply --forget-selections {self.selections}"

    def notice(self) -> str:
        """The cost, plus why no other copy of this Host can supply it back."""

        command = self.remediation()
        ending = (
            "Read the file yourself before going further; this Kernel cannot tell you "
            "what is in it."
            if command is None
            else f"Set it aside — it is renamed, not deleted — with `{command}`."
        )
        return (
            f"{self.cost()} Device mounts come back from the Hub Claim stream; an Owner's "
            "Companion selection does not, and a backup of this file is a backup at the "
            f"schema this Kernel just refused. {ending}"
        )


def schema_refusal(connection: sqlite3.Connection) -> str | None:
    """Why this Kernel will not use the database behind this connection, if it will not.

    Lifted out of the store so that "does this release's Kernel accept this
    file" has one answer and two askers: the Kernel deciding whether to start,
    and whoever is repairing a Host that did not. The repair used to have to
    infer it — from a version number, or from the fact that the service was
    crash-looping — and an inference is exactly what must not be standing behind
    a decision to set an authority aside.
    """

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(_EXPECTED_COLUMNS):
        return "kernel SQLite schema is partial or unknown; migrations are unsupported"
    for table, expected in _EXPECTED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if actual != expected:
            return f"kernel SQLite table {table} does not match schema v{SCHEMA_VERSION}"
    version_row = connection.execute("SELECT schema_version FROM kernel_schema_meta").fetchall()
    if len(version_row) != 1 or version_row[0][0] != SCHEMA_VERSION:
        return "kernel SQLite schema version is unsupported"
    return None


def schema_report(path: Path) -> dict[str, Any]:
    """Everything a repair needs to decide, read off the file without opening the store.

    One call rather than several, and answered by the Kernel rather than
    restated by the operator's tooling: whether this Kernel accepts the
    database, why not if it does not, what setting it aside would destroy, and
    the sentence to put in front of a person. Ops runs this through the Kernel's
    own interpreter, so the number in the refusal an operator saw and the number
    in the plan they are about to approve cannot disagree.

    Nothing here raises. A repair tool that crashed while asking what a repair
    would cost would send its operator back to ``rm``, which is the whole thing
    this exists to replace.
    """

    census = selection_census(path)
    accepted: bool | None = None
    refusal: str | None = None
    if census.unreadable is None:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                refusal = schema_refusal(connection)
            finally:
                connection.close()
            accepted = refusal is None
        except sqlite3.Error as exc:
            refusal = f"kernel SQLite schema could not be read: {exc}"
    return {
        "path": str(path),
        "code_schema_version": SCHEMA_VERSION,
        "accepted": accepted,
        "refusal": refusal,
        "notice": census.notice(),
        **census.as_document(),
    }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def selection_census(path: Path) -> SelectionCensus:
    """Count, without opening the store, what one Kernel database would cost.

    Read-only and side-effect free by construction: the caller is either a
    Kernel that is already refusing to start, or an operator deciding whether to
    set the file aside. Neither may be the reason the file changes.
    """

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return SelectionCensus(None, None, None, None, unreadable=str(exc))
    try:
        return _selection_census(connection)
    finally:
        connection.close()


def _selection_census(connection: sqlite3.Connection) -> SelectionCensus:
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.Error as exc:
        return SelectionCensus(None, None, None, None, unreadable=str(exc))
    version: int | None = None
    if "kernel_schema_meta" in tables:
        rows = _rows(connection, "SELECT schema_version FROM kernel_schema_meta")
        if rows is not None and len(rows) == 1:
            version = rows[0][0] if isinstance(rows[0][0], int) else None
    mounts = (
        _count(connection, "SELECT count(*) FROM kernel_device_mounts")
        if "kernel_device_mounts" in tables
        else None
    )
    if SELECTION_TABLE not in tables:
        return SelectionCensus(version, mounts, None, None)
    assignments = _count(connection, f"SELECT count(*) FROM {SELECTION_TABLE}")
    columns = _rows(connection, f"PRAGMA table_info({SELECTION_TABLE})")
    if columns is None or SELECTION_COLUMN not in {row[1] for row in columns}:
        return SelectionCensus(version, mounts, assignments, None)
    selections = _count(
        connection,
        f"SELECT count(*) FROM {SELECTION_TABLE} WHERE {SELECTION_COLUMN} IS NOT NULL",
    )
    return SelectionCensus(version, mounts, assignments, selections)


def _rows(connection: sqlite3.Connection, sql: str) -> list[Any] | None:
    """A query whose failure is an answer of "unknown" rather than an exception.

    Every caller here runs while something has already gone wrong with this
    file. A census that can raise would turn a refusal that names its cost into
    a stack trace that names nothing — strictly worse than the silence it is
    replacing — so nothing in the census is allowed to fail loudly.
    """

    try:
        return list(connection.execute(sql))
    except sqlite3.Error:
        return None


def _count(connection: sqlite3.Connection, sql: str) -> int | None:
    rows = _rows(connection, sql)
    if rows is None or len(rows) != 1 or not isinstance(rows[0][0], int):
        return None
    return rows[0][0]


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
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        request_id=row["request_id"],
        fingerprint=row["fingerprint"],
        active=bool(row["active"]),
    )

_AUDIT_INSERT = """INSERT INTO kernel_audit_events(
    event_id, event_type, device_id, owner_id, subject, subject_id,
    subject_revision, request_id, fingerprint, occurred_at, data_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _audit_data(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _audit_from_row(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        position=row["position"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        owner_id=row["owner_id"],
        device_id=row["device_id"],
        subject=row["subject"],
        subject_id=row["subject_id"],
        subject_revision=row["subject_revision"],
        request_id=row["request_id"],
        fingerprint=row["fingerprint"],
        occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")),
        data=json.loads(row["data_json"]),
    )


def _assignment_from_row(row: sqlite3.Row) -> BodyAssignment:
    return BodyAssignment(
        body_endpoint_id=row["body_endpoint_id"],
        device_id=row["device_id"],
        endpoint_id=row["endpoint_id"],
        owner_id=row["owner_id"],
        companion_id=row["companion_id"],
        selection_provenance=row["selection_provenance"],
        change_reason=row["change_reason"],
        mode=row["mode"],
        policy_refs=tuple(json.loads(row["policy_refs_json"])),
        revision=row["revision"],
        generation=row["generation"],
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        request_id=row["request_id"],
        fingerprint=row["fingerprint"],
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
                INSERT INTO kernel_schema_meta(schema_version) VALUES (8);
                CREATE TABLE kernel_device_mounts (
                    device_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    owner_domain_id TEXT NOT NULL,
                    owner_domain_generation INTEGER NOT NULL CHECK (owner_domain_generation >= 1),
                    claim_generation INTEGER NOT NULL CHECK (claim_generation >= 1),
                    trust_epoch INTEGER NOT NULL CHECK (trust_epoch >= 1),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                );
                CREATE INDEX ix_kernel_mounts_owner_active
                    ON kernel_device_mounts(owner_id, active, device_id);
                CREATE TABLE kernel_body_assignments (
                    body_endpoint_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    companion_id TEXT,
                    selection_provenance TEXT NOT NULL,
                    change_reason TEXT,
                    mode TEXT NOT NULL CHECK (mode = 'default'),
                    policy_refs_json TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    UNIQUE(device_id, endpoint_id)
                );
                CREATE INDEX ix_kernel_assignments_owner_companion
                    ON kernel_body_assignments(owner_id, companion_id, body_endpoint_id);
                CREATE TABLE kernel_audit_events (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    subject_revision INTEGER NOT NULL,
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
        reason = schema_refusal(self._connection)
        if reason is not None:
            raise self._refuse(reason)

    def _refuse(self, reason: str) -> RuntimeError:
        """Refuse this database, and say what starting without it would cost.

        Every one of the three refusals above ends the same way for whoever is
        holding the Host: this file has to be set aside for the Kernel to run.
        So all three carry the same second half. Which of the three fired says
        what is wrong with the file; the census says what is *in* it, and that
        is the half nobody had — the loss used to be discovered afterwards, by
        an Owner whose speaker had stopped answering as anyone.

        The census reads the connection this Kernel already refused to trust,
        which is exactly why nothing in it may raise: an exception thrown while
        composing an error message replaces a refusal that names its cost with a
        traceback that names nothing.
        """

        return RuntimeError(f"{reason}. {_selection_census(self._connection).notice()}")

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
                            revision, created_at, updated_at,
                            request_id, fingerprint, active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                else:
                    cursor = self._connection.execute(
                        """UPDATE kernel_device_mounts SET
                            owner_domain_id=?, owner_domain_generation=?,
                            claim_generation=?, trust_epoch=?,
                            revision=?, created_at=?, updated_at=?, request_id=?,
                            fingerprint=?, active=?
                        WHERE device_id=? AND owner_id=? AND revision=?""",
                        (
                            mount.owner_domain_id,
                            mount.owner_domain_generation,
                            mount.claim_generation,
                            mount.trust_epoch,
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
                    _AUDIT_INSERT,
                    (
                        event_id,
                        event_type,
                        mount.device_id,
                        mount.owner_id,
                        AUDIT_SUBJECT_DEVICE_MOUNT,
                        mount.device_id,
                        mount.revision,
                        mount.request_id,
                        mount.fingerprint,
                        _timestamp(mount.updated_at),
                        _audit_data({**event_data, "active": mount.active}),
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
        return tuple(_audit_from_row(row) for row in rows)

    def get_assignment(self, body_endpoint_id: str) -> BodyAssignment | None:
        with self._mutex:
            row = self._connection.execute(
                "SELECT * FROM kernel_body_assignments WHERE body_endpoint_id = ?",
                (body_endpoint_id,),
            ).fetchone()
        return _assignment_from_row(row) if row is not None else None

    def list_assignments(self) -> tuple[BodyAssignment, ...]:
        with self._mutex:
            rows = self._connection.execute(
                "SELECT * FROM kernel_body_assignments ORDER BY body_endpoint_id"
            ).fetchall()
        return tuple(_assignment_from_row(row) for row in rows)

    def commit_assignment(
        self,
        *,
        assignment: BodyAssignment,
        expected_revision: int,
        mount_revision: int,
        event_type: str,
        event_data: dict[str, Any],
    ) -> AssignmentCommitResult:
        """Replace one Body's assignment under compare-and-swap.

        The audit row's ``request_id`` is unique across the whole stream, which
        is what makes a replayed request detectable here rather than only in the
        caller. The caller checks first because it can answer without opening a
        transaction; this check is the one that holds under a race.
        """

        with self._mutex:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    "SELECT * FROM kernel_body_assignments WHERE body_endpoint_id = ?",
                    (assignment.body_endpoint_id,),
                ).fetchone()
                if current is not None and current["request_id"] == assignment.request_id:
                    if current["fingerprint"] != assignment.fingerprint:
                        raise IdempotencyConflict(
                            "request_id already belongs to a different mutation"
                        )
                    stored = _assignment_from_row(current)
                    position = self._connection.execute(
                        "SELECT position FROM kernel_audit_events WHERE request_id = ?",
                        (assignment.request_id,),
                    ).fetchone()
                    self._connection.execute("COMMIT")
                    return AssignmentCommitResult(
                        assignment=stored,
                        audit_position=int(position["position"]) if position else 0,
                        replayed=True,
                    )
                actual_revision = current["revision"] if current is not None else 0
                if actual_revision != expected_revision:
                    raise RevisionConflict(
                        f"expected assignment revision {expected_revision}, "
                        f"current revision is {actual_revision}"
                    )
                if current is not None and current["owner_id"] != assignment.owner_id:
                    raise RevisionConflict("body assignment owner namespace cannot change")
                values = (
                    assignment.body_endpoint_id,
                    assignment.device_id,
                    assignment.endpoint_id,
                    assignment.owner_id,
                    assignment.companion_id,
                    assignment.selection_provenance,
                    assignment.change_reason,
                    assignment.mode,
                    json.dumps(list(assignment.policy_refs), separators=(",", ":")),
                    assignment.revision,
                    assignment.generation,
                    _timestamp(assignment.created_at),
                    _timestamp(assignment.updated_at),
                    assignment.request_id,
                    assignment.fingerprint,
                )
                if current is None:
                    self._connection.execute(
                        """INSERT INTO kernel_body_assignments(
                            body_endpoint_id, device_id, endpoint_id, owner_id, companion_id,
                            selection_provenance, change_reason, mode, policy_refs_json,
                            revision, generation, created_at, updated_at, request_id, fingerprint
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                else:
                    cursor = self._connection.execute(
                        """UPDATE kernel_body_assignments SET
                            device_id=?, endpoint_id=?, owner_id=?, companion_id=?,
                            selection_provenance=?, change_reason=?, mode=?, policy_refs_json=?,
                            revision=?, generation=?, created_at=?, updated_at=?,
                            request_id=?, fingerprint=?
                        WHERE body_endpoint_id=? AND revision=?""",
                        (*values[1:], assignment.body_endpoint_id, expected_revision),
                    )
                    if cursor.rowcount != 1:
                        raise RevisionConflict("assignment revision changed during commit")
                cursor = self._connection.execute(
                    _AUDIT_INSERT,
                    (
                        f"kernel:{assignment.request_id}:{assignment.revision}",
                        event_type,
                        assignment.device_id,
                        assignment.owner_id,
                        AUDIT_SUBJECT_BODY_ASSIGNMENT,
                        assignment.body_endpoint_id,
                        assignment.revision,
                        assignment.request_id,
                        assignment.fingerprint,
                        _timestamp(assignment.updated_at),
                        _audit_data(
                            {
                                **event_data,
                                "generation": assignment.generation,
                                "mount_revision": mount_revision,
                            }
                        ),
                    ),
                )
                position = int(cursor.lastrowid)
                self._connection.execute("COMMIT")
                return AssignmentCommitResult(
                    assignment=assignment, audit_position=position, replayed=False
                )
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

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
                                trust_epoch, revision, created_at,
                                updated_at, request_id, fingerprint, active
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                                revision=?, created_at=?, updated_at=?,
                                request_id=?, fingerprint=?, active=?
                            WHERE device_id=? AND owner_id=? AND revision=?""",
                            (
                                mount.owner_domain_id,
                                mount.owner_domain_generation,
                                mount.claim_generation,
                                mount.trust_epoch,
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
                        _AUDIT_INSERT,
                        (
                            f"kernel:{mount.request_id}:{mount.revision}",
                            (
                                "eidolon.kernel.device-mounted-by-claim-event.v1"
                                if mount.active
                                else "eidolon.kernel.device-unmounted-by-claim-event.v1"
                            ),
                            mount.device_id,
                            mount.owner_id,
                            AUDIT_SUBJECT_DEVICE_MOUNT,
                            mount.device_id,
                            mount.revision,
                            mount.request_id,
                            mount.fingerprint,
                            _timestamp(mount.updated_at),
                            _audit_data(
                                {
                                    "claim_event_id": event.id,
                                    "claim_event_source": event.source,
                                    "claim_event_position": item.stream_position,
                                    "claim_aggregate_revision": event.aggregaterev,
                                    "previous_revision": expected_mount_revision,
                                    "active": mount.active,
                                }
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
