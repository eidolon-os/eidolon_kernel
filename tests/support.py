from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from eidolon_sdk.device_foundation.v1 import (
    ClaimActivatedData,
    ClaimActivatedEvent,
    ClaimEventCursor,
    ClaimEventPage,
    ClaimEventStreamItem,
    ClaimRevokedData,
    ClaimRevokedEvent,
    DeviceRef,
    ManifestRef,
)

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
from eidolon_kernel.ports.runtime import (
    ClaimEventCommitResult,
    CommitResult,
    StoredClaimEvent,
    StoredRequest,
)


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
    claim_events: tuple[ClaimEventStreamItem, ...] = ()
    high_watermark: int | None = None

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
                owner_domain_generation=1,
                claim_generation=1,
                trust_epoch=1,
            ),
        )

    async def list_claim_events(self, *, cursor: ClaimEventCursor, limit: int):
        events = tuple(
            item
            for item in self.claim_events
            if item.stream_position > cursor.stream_position
        )[:limit]
        next_position = events[-1].stream_position if events else cursor.stream_position
        high_watermark = self.high_watermark
        if high_watermark is None:
            high_watermark = max(
                (item.stream_position for item in self.claim_events), default=cursor.stream_position
            )
        return ClaimEventPage(
            requested_after=cursor,
            events=events,
            next_cursor=ClaimEventCursor(stream_position=next_position),
            high_watermark=high_watermark,
            observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
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
    claim_inbox: dict[tuple[str, str], tuple[ClaimEventStreamItem, str, str]] = field(
        default_factory=dict
    )
    claim_cursor_position: int = 0
    claim_high_watermark: int = 0

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

    def claim_event_cursor(self) -> ClaimEventCursor:
        return ClaimEventCursor(stream_position=self.claim_cursor_position)

    def claim_event_high_watermark(self) -> int:
        return self.claim_high_watermark

    def claim_events_for_device(self, device_id: str) -> tuple[StoredClaimEvent, ...]:
        values = []
        for item, outcome, fingerprint in self.claim_inbox.values():
            event = item.event
            if event.data.device_ref.device_instance_id != device_id:
                continue
            values.append(
                StoredClaimEvent(
                    stream_position=item.stream_position,
                    source=event.source,
                    event_id=event.id,
                    event_fingerprint=fingerprint,
                    event_type=event.type,
                    device_ref=event.data.device_ref,
                    aggregate_revision=event.aggregaterev,
                    outcome=outcome,
                )
            )
        return tuple(sorted(values, key=lambda value: value.stream_position))

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
        processed_at,
    ) -> ClaimEventCommitResult:
        import hashlib
        import json

        event = item.event
        encoded = json.dumps(
            event.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        key = (event.source, event.id)
        existing = self.claim_inbox.get(key)
        if existing is not None:
            if existing != (item, outcome, fingerprint):
                raise IdempotencyConflict
            return ClaimEventCommitResult(self.get(event.data.device_ref.device_instance_id), outcome, True)
        if requested_after.stream_position != self.claim_cursor_position:
            raise RevisionConflict
        if item.stream_position != self.claim_cursor_position + 1:
            raise RevisionConflict
        if (
            next_cursor.stream_position != item.stream_position
            or high_watermark < item.stream_position
            or high_watermark < self.claim_high_watermark
        ):
            raise RevisionConflict
        if mount is not None:
            current = self.mounts.get(mount.device_id)
            actual = current.revision if current else 0
            if actual != expected_mount_revision:
                raise RevisionConflict
            position = len(self.events) + 1
            self.mounts[mount.device_id] = mount
            self.events.append(
                AuditEvent(
                    position=position,
                    event_id=f"event-{position}",
                    event_type=(
                        "eidolon.kernel.device-mounted-by-claim-event.v1"
                        if mount.active
                        else "eidolon.kernel.device-unmounted-by-claim-event.v1"
                    ),
                    mount=mount,
                    occurred_at=mount.updated_at,
                    data={"claim_event_id": event.id},
                )
            )
        self.claim_inbox[key] = (item, outcome, fingerprint)
        self.claim_cursor_position = next_cursor.stream_position
        self.claim_high_watermark = high_watermark
        return ClaimEventCommitResult(mount, outcome, False)

    def checkpoint_claim_cursor(
        self,
        *,
        requested_after: ClaimEventCursor,
        next_cursor: ClaimEventCursor,
        high_watermark: int,
        processed_at,
    ) -> None:
        if (
            requested_after.stream_position != self.claim_cursor_position
            or next_cursor != requested_after
            or high_watermark < next_cursor.stream_position
            or high_watermark < self.claim_high_watermark
        ):
            raise RevisionConflict
        self.claim_high_watermark = high_watermark


def claim_event_item(
    *,
    position: int = 1,
    aggregate_revision: int = 5,
    event_id: str | None = None,
    event_type: str = "activated",
    claim_generation: int = 1,
    owner_domain_generation: int = 1,
    trust_epoch: int = 1,
    owner_domain_id: str = "owner-1",
    reason: str = "owner-removed",
) -> ClaimEventStreamItem:
    device_ref = DeviceRef(
        device_instance_id="device-1",
        owner_domain_id=owner_domain_id,
        owner_domain_generation=owner_domain_generation,
        claim_generation=claim_generation,
        trust_epoch=trust_epoch,
    )
    at = datetime(2026, 8, 4, 8, 0, tzinfo=UTC) + timedelta(seconds=position)
    if event_type == "activated":
        event = ClaimActivatedEvent(
            id=event_id or f"claim-activated-{position}",
            subject="device-instances/device-1",
            time=at,
            ownerdomainid=owner_domain_id,
            aggregaterev=aggregate_revision,
            correlationid="intent-one",
            causationid="grant-ack-one",
            data=ClaimActivatedData(
                device_ref=device_ref,
                manifest_ref=ManifestRef(
                    manifest_id="manifest-one",
                    revision=1,
                    digest="sha256:" + "a" * 64,
                ),
                approval_decision_id="decision-one",
                activated_at=at,
            ),
        )
    elif event_type == "revoked":
        event = ClaimRevokedEvent(
            id=event_id or f"claim-revoked-{position}",
            subject="device-instances/device-1",
            time=at,
            ownerdomainid=owner_domain_id,
            aggregaterev=aggregate_revision,
            correlationid="intent-one",
            causationid="revoke-command-one",
            data=ClaimRevokedData(
                device_ref=device_ref,
                reason=reason,
                revoked_at=at,
            ),
        )
    else:
        raise ValueError("unsupported test Claim event type")
    return ClaimEventStreamItem(stream_position=position, event=event)


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
