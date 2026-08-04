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
    CompanionIdentity,
    DeviceAdmission,
    DeviceMount,
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

    async def get_device(self, *, owner_id: str, device_id: str) -> DeviceAdmission:
        self.calls += 1
        return DeviceAdmission(
            device_id=device_id,
            owner_id=self.actual_owner or owner_id,
            status=self.status,
            manifest_revision="sha256:hub-manifest",
        )


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


def sample_mount(
    revision: int = 1, *, request_id: str = "request-1", active: bool = True
) -> DeviceMount:
    now = datetime(2026, 8, 4, 8, 0, tzinfo=UTC) + timedelta(seconds=revision)
    return DeviceMount(
        device_id="device-1",
        owner_id="owner-1",
        companion_id="companion-1",
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
        "companion_id": "companion-1",
        "expected_revision": 0,
        "replace_existing": False,
    }
    value.update(overrides)
    return value


def headers(owner_id: str = "owner-1") -> dict[str, str]:
    return {"X-Eidolon-Owner": owner_id}
