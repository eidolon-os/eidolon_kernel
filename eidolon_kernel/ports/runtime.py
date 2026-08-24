"""Clock, persistence and hot-projection ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from eidolon_sdk.device_foundation.v1 import (
    ClaimEventCursor,
    ClaimEventStreamItem,
    DeviceRef,
)

from eidolon_kernel.domain.model import AuditEvent, DeviceMount


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class StoredRequest:
    request_id: str
    operation: str
    fingerprint: str
    mount: DeviceMount
    audit_position: int


@dataclass(frozen=True, slots=True)
class CommitResult:
    mount: DeviceMount
    audit_position: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class StoredClaimEvent:
    stream_position: int
    source: str
    event_id: str
    event_fingerprint: str
    event_type: str
    device_ref: DeviceRef
    aggregate_revision: int
    outcome: str


@dataclass(frozen=True, slots=True)
class ClaimEventCommitResult:
    mount: DeviceMount | None
    outcome: str
    replayed: bool


class MountStore(Protocol):
    def get(self, device_id: str) -> DeviceMount | None: ...

    def get_request(self, request_id: str) -> StoredRequest | None: ...

    def commit(
        self,
        *,
        mount: DeviceMount,
        expected_revision: int,
        operation: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> CommitResult: ...

    def list_all(self) -> tuple[DeviceMount, ...]: ...

    def list_audit(
        self, *, after_position: int, limit: int, owner_id: str
    ) -> tuple[AuditEvent, ...]: ...

    def claim_event_cursor(self) -> ClaimEventCursor: ...

    def claim_event_high_watermark(self) -> int: ...

    def claim_events_for_device(self, device_id: str) -> tuple[StoredClaimEvent, ...]: ...

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
    ) -> ClaimEventCommitResult: ...

    def checkpoint_claim_cursor(
        self,
        *,
        requested_after: ClaimEventCursor,
        next_cursor: ClaimEventCursor,
        high_watermark: int,
        processed_at: datetime,
    ) -> None: ...


class MountProjection(Protocol):
    def rebuild(self, mounts: tuple[DeviceMount, ...]) -> None: ...

    def put(self, mount: DeviceMount) -> None: ...

    def get(self, device_id: str) -> DeviceMount | None: ...

    def list(
        self,
        *,
        owner_id: str,
        companion_id: str | None,
        active_only: bool,
        after_device_id: str | None,
        limit: int,
    ) -> tuple[DeviceMount, ...]: ...
