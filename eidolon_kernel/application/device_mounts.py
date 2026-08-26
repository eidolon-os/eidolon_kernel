"""Device Mount command handlers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from eidolon_sdk.device_foundation.v1 import (
    ClaimActivatedEvent,
    ClaimEventCursor,
    ClaimRevokedEvent,
    DeviceRef,
)

from eidolon_kernel.domain.commands import MountDeviceCommand, UnmountDeviceCommand
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    Conflict,
    IdempotencyConflict,
    NotFound,
    RevisionConflict,
)
from eidolon_kernel.domain.model import DeviceMount, request_fingerprint
from eidolon_kernel.ports.authorities import DeviceAuthority
from eidolon_kernel.ports.runtime import Clock, CommitResult, MountProjection, MountStore


def _replay(
    store: MountStore,
    *,
    request_id: str,
    operation: str,
    fingerprint: str,
    owner_id: str,
) -> CommitResult | None:
    stored = store.get_request(request_id)
    if stored is None:
        return None
    if stored.mount.owner_id != owner_id:
        raise NotFound("device mount not found")
    if stored.operation != operation or stored.fingerprint != fingerprint:
        raise IdempotencyConflict("request_id already belongs to a different mutation")
    return CommitResult(
        mount=stored.mount,
        audit_position=stored.audit_position,
        replayed=True,
    )


def _project_committed(
    store: MountStore, projection: MountProjection, mount: DeviceMount
) -> None:
    try:
        projection.put(mount)
    except Exception:
        # The commit already succeeded. Recover the disposable projection from the
        # sole authority instead of pretending the write failed or leaving stale reads.
        projection.rebuild(store.list_all())


@dataclass(slots=True)
class MountDevice:
    store: MountStore
    projection: MountProjection
    devices: DeviceAuthority
    clock: Clock

    async def execute(self, command: MountDeviceCommand) -> CommitResult:
        replay = _replay(
            self.store,
            request_id=command.request_id,
            operation="device.mount",
            fingerprint=command.fingerprint,
            owner_id=command.owner_id,
        )
        if replay is not None:
            _project_committed(self.store, self.projection, replay.mount)
            return replay

        current = self.store.get(command.device_id)
        if current is not None and current.owner_id != command.owner_id:
            raise NotFound("device mount not found")

        device = await self.devices.get_device(
            owner_id=command.owner_id, device_id=command.device_id
        )
        if (
            device.device_id != command.device_id
            or device.owner_id != command.owner_id
            or device.status != "approved"
        ):
            raise AuthorityRejected("Hub device must exist, be approved, and match owner")
        # Another identical call may have committed while this call awaited its
        # external authorities. Preserve concurrent idempotency before evaluating CAS.
        replay = _replay(
            self.store,
            request_id=command.request_id,
            operation="device.mount",
            fingerprint=command.fingerprint,
            owner_id=command.owner_id,
        )
        if replay is not None:
            _project_committed(self.store, self.projection, replay.mount)
            return replay

        current = self.store.get(command.device_id)
        if current is not None and current.owner_id != command.owner_id:
            raise NotFound("device mount not found")
        actual_revision = current.revision if current is not None else 0
        if actual_revision != command.expected_revision:
            raise RevisionConflict(
                f"expected revision {command.expected_revision}, current revision is {actual_revision}"
            )
        if current is not None and current.active and not command.replace_existing:
            raise Conflict(
                "device already has an active mount; explicit replace_existing is required"
            )

        now = self.clock.now()
        if current is None:
            mount = DeviceMount.first(
                device_id=command.device_id,
                owner_id=command.owner_id,
                device_ref=device.device_ref,
                at=now,
                request_id=command.request_id,
                fingerprint=command.fingerprint,
            )
            event_type = "eidolon.kernel.device-mounted.v1"
        else:
            mount = current.mounted_as(
                owner_id=command.owner_id,
                device_ref=device.device_ref,
                at=now,
                request_id=command.request_id,
                fingerprint=command.fingerprint,
            )
            event_type = (
                "eidolon.kernel.device-remounted.v1"
                if current.active
                else "eidolon.kernel.device-mounted.v1"
            )
        result = self.store.commit(
            mount=mount,
            expected_revision=command.expected_revision,
            operation="device.mount",
            event_type=event_type,
            event_data={
                "previous_revision": actual_revision,
                "manifest_revision": device.manifest_revision,
                "replace_existing": command.replace_existing,
            },
        )
        _project_committed(self.store, self.projection, result.mount)
        return result


@dataclass(slots=True)
class UnmountDevice:
    store: MountStore
    projection: MountProjection
    clock: Clock

    def execute(self, command: UnmountDeviceCommand) -> CommitResult:
        replay = _replay(
            self.store,
            request_id=command.request_id,
            operation="device.unmount",
            fingerprint=command.fingerprint,
            owner_id=command.owner_id,
        )
        if replay is not None:
            _project_committed(self.store, self.projection, replay.mount)
            return replay

        current = self.store.get(command.device_id)
        if current is None or current.owner_id != command.owner_id:
            raise NotFound("active device mount not found")
        if not current.active:
            raise Conflict("device mount is already inactive")
        if current.revision != command.expected_revision:
            raise RevisionConflict(
                f"expected revision {command.expected_revision}, current revision is {current.revision}"
            )
        mount = current.unmounted(
            at=self.clock.now(),
            request_id=command.request_id,
            fingerprint=command.fingerprint,
        )
        result = self.store.commit(
            mount=mount,
            expected_revision=command.expected_revision,
            operation="device.unmount",
            event_type="eidolon.kernel.device-unmounted.v1",
            event_data={"previous_revision": current.revision},
        )
        _project_committed(self.store, self.projection, result.mount)
        return result


@dataclass(frozen=True, slots=True)
class ClaimEventReconciliationResult:
    consumed: int
    mounted: int
    unmounted: int
    ignored: int


@dataclass(slots=True)
class ReconcileClaimEvents:
    """Consume Hub Claim facts and converge only matching Mount generations."""

    store: MountStore
    projection: MountProjection
    devices: DeviceAuthority
    clock: Clock
    batch_size: int = 100

    async def execute(self) -> ClaimEventReconciliationResult:
        cursor = self.store.claim_event_cursor()
        page = await self.devices.list_claim_events(cursor=cursor, limit=self.batch_size)
        if page.requested_after != cursor:
            raise RevisionConflict("Hub Claim page does not continue the persisted cursor")
        if not page.events:
            if page.high_watermark > cursor.stream_position:
                raise RevisionConflict("Hub Claim page omitted events below its high watermark")
            self.store.checkpoint_claim_cursor(
                requested_after=cursor,
                next_cursor=page.next_cursor,
                high_watermark=page.high_watermark,
                processed_at=self.clock.now(),
            )
            return ClaimEventReconciliationResult(0, 0, 0, 0)

        consumed = mounted = unmounted = ignored = 0
        requested_after = cursor
        for item in page.events:
            event = item.event
            expected_ref = event.data.device_ref
            history = self.store.claim_events_for_device(expected_ref.device_instance_id)
            if history and event.aggregaterev <= max(
                stored.aggregate_revision for stored in history
            ):
                raise RevisionConflict("Claim aggregate revision is duplicate or out of order")

            current = self.store.get(expected_ref.device_instance_id)
            mount = None
            expected_mount_revision = None
            outcome: str
            prior_refs = [stored.device_ref for stored in history]
            if current is not None:
                prior_refs.append(self._mount_ref(current))
            if any(
                str(ref.owner_domain_id) != str(expected_ref.owner_domain_id) for ref in prior_refs
            ):
                raise RevisionConflict(
                    "Claim event Owner Domain conflicts with persisted device history"
                )

            newest = max(
                prior_refs,
                key=self._generation,
                default=None,
            )
            if newest is not None and self._generation(expected_ref) < self._generation(newest):
                outcome = "stale-generation-ignored"
                ignored += 1
            elif isinstance(event, ClaimActivatedEvent):
                was_revoked = any(
                    stored.device_ref == expected_ref
                    and stored.event_type == "live.eidolon.device.claim-revoked.v1"
                    for stored in history
                )
                if was_revoked or (
                    current is not None
                    and self._mount_ref(current) == expected_ref
                    and not current.active
                ):
                    outcome = "terminal-generation-ignored"
                    ignored += 1
                elif (
                    current is not None
                    and self._mount_ref(current) == expected_ref
                    and current.active
                ):
                    outcome = "already-mounted"
                    ignored += 1
                else:
                    request_id, fingerprint = self._event_mutation_identity(
                        event.id, "device.mount-by-claim-event", expected_ref
                    )
                    if current is None:
                        mount = DeviceMount.first(
                            device_id=expected_ref.device_instance_id,
                            owner_id=str(event.data.business_owner_id),
                            device_ref=expected_ref,
                            at=self.clock.now(),
                            request_id=request_id,
                            fingerprint=fingerprint,
                        )
                        expected_mount_revision = 0
                    else:
                        mount = current.mounted_as(
                            owner_id=str(event.data.business_owner_id),
                            device_ref=expected_ref,
                            at=self.clock.now(),
                            request_id=request_id,
                            fingerprint=fingerprint,
                        )
                        expected_mount_revision = current.revision
                    outcome = "mounted"
                    mounted += 1
            elif isinstance(event, ClaimRevokedEvent):
                if (
                    current is not None
                    and self._mount_ref(current) == expected_ref
                    and current.active
                ):
                    request_id, fingerprint = self._event_mutation_identity(
                        event.id, "device.unmount-by-claim-event", expected_ref
                    )
                    mount = current.unmounted(
                        at=self.clock.now(),
                        request_id=request_id,
                        fingerprint=fingerprint,
                    )
                    expected_mount_revision = current.revision
                    outcome = "unmounted"
                    unmounted += 1
                else:
                    outcome = "no-matching-active-mount"
                    ignored += 1
            else:  # pragma: no cover - SDK discriminator makes this unreachable.
                raise RevisionConflict("unsupported canonical Claim event type")

            next_cursor = ClaimEventCursor(stream_position=item.stream_position)
            result = self.store.commit_claim_event(
                item=item,
                requested_after=requested_after,
                next_cursor=next_cursor,
                high_watermark=page.high_watermark,
                outcome=outcome,
                mount=mount,
                expected_mount_revision=expected_mount_revision,
                processed_at=self.clock.now(),
            )
            if result.mount is not None:
                _project_committed(self.store, self.projection, result.mount)
            requested_after = next_cursor
            consumed += 1
        if requested_after != page.next_cursor:
            raise RevisionConflict("Hub Claim page next_cursor was not checkpointed")
        return ClaimEventReconciliationResult(consumed, mounted, unmounted, ignored)

    @staticmethod
    def _generation(device_ref: DeviceRef) -> tuple[int, int, int]:
        return (
            device_ref.owner_domain_generation,
            device_ref.claim_generation,
            device_ref.trust_epoch,
        )

    @staticmethod
    def _mount_ref(mount: DeviceMount) -> DeviceRef:
        return mount.device_ref

    @staticmethod
    def _event_mutation_identity(
        event_id: str, operation: str, device_ref: DeviceRef
    ) -> tuple[str, str]:
        request_id = "claim-event:" + hashlib.sha256(event_id.encode()).hexdigest()[:48]
        fingerprint = request_fingerprint(
            operation,
            {
                "event_id": event_id,
                "device_ref": device_ref.model_dump(mode="json"),
            },
        )
        return request_id, fingerprint
