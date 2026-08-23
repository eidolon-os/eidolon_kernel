from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from eidolon_kernel.domain.errors import (
    AuthorityUnavailable,
    IdempotencyConflict,
    RevisionConflict,
)
from eidolon_kernel.domain.model import (
    AuditEvent,
    ClaimEvent,
    CompanionIdentity,
    DeviceAdmission,
    DeviceMount,
    DeviceRef,
)
from eidolon_kernel.ports.runtime import CommitResult, StoredRequest


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


@dataclass
class FakeDeviceAuthority:
    status: str = "approved"
    actual_owner: str | None = None
    calls: int = 0
    claim_events: tuple[ClaimEvent, ...] = ()

    async def get_device(self, *, owner_id: str, device_id: str) -> DeviceAdmission:
        self.calls += 1
        actual_owner = self.actual_owner or owner_id
        return DeviceAdmission(
            device_id=device_id,
            owner_id=actual_owner,
            status=self.status,
            manifest_revision="sha256:hub-manifest",
            device_ref=DeviceRef(
                device_instance_id=device_id,
                owner_domain_id=actual_owner,
                claim_generation=1,
                trust_epoch=1,
                accepted_manifest_digest="sha256:hub-manifest",
            ),
        )

    async def list_claim_events(self, *, after_stream_position: int, limit: int):
        return tuple(
            event
            for event in self.claim_events
            if event.stream_position > after_stream_position
        )[:limit]


@dataclass
class FakeCompanionAuthority:
    status: str = "active"
    actual_owner: str = "owner-1"
    calls: int = 0

    async def get_companion(self, *, companion_id: str) -> CompanionIdentity:
        self.calls += 1
        return CompanionIdentity(
            companion_id=companion_id,
            owner_id=self.actual_owner,
            status=self.status,
        )


class OfflineCompanionAuthority:
    async def get_companion(self, *, companion_id: str) -> CompanionIdentity:
        raise AuthorityUnavailable("Companion authority is offline")


@dataclass
class MemoryStore:
    mounts: dict[str, DeviceMount] = field(default_factory=dict)
    requests: dict[str, StoredRequest] = field(default_factory=dict)
    events: list[AuditEvent] = field(default_factory=list)
    claim_events: dict[str, tuple[ClaimEvent, str]] = field(default_factory=dict)

    def get(self, device_id: str) -> DeviceMount | None:
        return self.mounts.get(device_id)

    def get_request(self, request_id: str) -> StoredRequest | None:
        return self.requests.get(request_id)

    def commit(
        self,
        *,
        mount: DeviceMount,
        expected_revision: int,
        operation: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> CommitResult:
        request = self.requests.get(mount.request_id)
        if request is not None:
            if request.operation != operation or request.fingerprint != mount.fingerprint:
                raise IdempotencyConflict
            return CommitResult(request.mount, request.audit_position, True)
        current = self.mounts.get(mount.device_id)
        actual = current.revision if current else 0
        if actual != expected_revision:
            raise RevisionConflict
        position = len(self.events) + 1
        event = AuditEvent(
            position=position,
            event_id=f"event-{position}",
            event_type=event_type,
            mount=mount,
            occurred_at=mount.updated_at,
            data=event_data,
        )
        self.mounts[mount.device_id] = mount
        self.events.append(event)
        self.requests[mount.request_id] = StoredRequest(
            mount.request_id, operation, mount.fingerprint, mount, position
        )
        return CommitResult(mount, position, False)

    def list_all(self) -> tuple[DeviceMount, ...]:
        return tuple(sorted(self.mounts.values(), key=lambda item: item.device_id))

    def list_audit(
        self, *, after_position: int, limit: int, owner_id: str
    ) -> tuple[AuditEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.position > after_position
            and event.mount.owner_id == owner_id
        )[:limit]

    def claim_event_position(self) -> int:
        return max((value[0].stream_position for value in self.claim_events.values()), default=0)

    def claim_event_outcome(self, event_id: str) -> str | None:
        value = self.claim_events.get(event_id)
        return None if value is None else value[1]

    def record_claim_event(self, *, event: ClaimEvent, outcome: str, processed_at) -> None:
        existing = self.claim_events.get(event.event_id)
        if existing is not None and existing != (event, outcome):
            raise IdempotencyConflict
        if existing is None and event.stream_position != self.claim_event_position() + 1:
            raise RevisionConflict
        self.claim_events[event.event_id] = (event, outcome)


def sample_mount(
    revision: int = 1,
    *,
    request_id: str = "request-1",
    active: bool = True,
    attached_companion_id: str | None = None,
) -> DeviceMount:
    now = datetime(2026, 8, 4, 8, 0, tzinfo=UTC) + timedelta(seconds=revision)
    return DeviceMount(
        device_id="device-1",
        owner_id="owner-1",
        claim_generation=1,
        trust_epoch=1,
        accepted_manifest_digest="sha256:hub-manifest",
        attached_companion_id=attached_companion_id,
        revision=revision,
        created_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        updated_at=now,
        request_id=request_id,
        fingerprint="sha256:" + "a" * 64,
        active=active,
    )


def mount_body(**overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "operation": "device.mount",
        "request_id": "mount-1",
        "device_id": "device-1",
        "expected_revision": 0,
        "replace_existing": False,
    }
    value.update(overrides)
    return value


def headers(owner_id: str = "owner-1") -> dict[str, str]:
    return {"X-Eidolon-Owner": owner_id}
