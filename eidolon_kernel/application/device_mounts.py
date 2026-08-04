"""Device Mount command handlers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from eidolon_kernel.domain.commands import MountDeviceCommand, UnmountDeviceCommand
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    AuthorityUnavailable,
    Conflict,
    IdempotencyConflict,
    NotFound,
    RevisionConflict,
)
from eidolon_kernel.domain.model import DeviceMount, request_fingerprint
from eidolon_kernel.ports.authorities import CompanionAuthority, DeviceAuthority
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
    companions: CompanionAuthority
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
        companion = await self.companions.get_companion(companion_id=command.companion_id)
        if (
            companion.companion_id != command.companion_id
            or companion.owner_id != command.owner_id
            or companion.status != "active"
        ):
            raise AuthorityRejected("Companion must exist, be active, and match owner")

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
            raise Conflict("device already has an active mount; explicit replace_existing is required")

        now = self.clock.now()
        if current is None:
            mount = DeviceMount.first(
                device_id=command.device_id,
                owner_id=command.owner_id,
                companion_id=command.companion_id,
                at=now,
                request_id=command.request_id,
                fingerprint=command.fingerprint,
            )
            event_type = "eidolon.kernel.device-mounted.v1"
        else:
            mount = current.mounted_as(
                owner_id=command.owner_id,
                companion_id=command.companion_id,
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
class ReconciliationResult:
    checked: int
    unmounted: int
    deferred: int


@dataclass(slots=True)
class ReconcileMountPrerequisites:
    """Tombstone mounts rejected by their current Device or Companion authority."""

    store: MountStore
    projection: MountProjection
    devices: DeviceAuthority
    companions: CompanionAuthority
    clock: Clock

    async def execute(self) -> ReconciliationResult:
        checked = 0
        unmounted = 0
        deferred = 0
        for snapshot in self.store.list_all():
            if not snapshot.active:
                continue
            checked += 1
            try:
                reason = await self._rejection_reason(snapshot)
            except AuthorityUnavailable:
                # Transport/auth/provider outages are not authoritative revocations.
                deferred += 1
                continue
            if reason is None:
                continue

            # Re-read after awaiting external authorities. A concurrent user mutation
            # wins; the next scan evaluates its newer revision.
            current = self.store.get(snapshot.device_id)
            if (
                current is None
                or not current.active
                or current.owner_id != snapshot.owner_id
                or current.revision != snapshot.revision
            ):
                continue
            request_id = self._request_id(current)
            fingerprint = request_fingerprint(
                "device.reconcile-unmount",
                {
                    "device_id": current.device_id,
                    "owner_id": current.owner_id,
                    "expected_revision": current.revision,
                },
            )
            tombstone = current.unmounted(
                at=self.clock.now(),
                request_id=request_id,
                fingerprint=fingerprint,
            )
            try:
                result = self.store.commit(
                    mount=tombstone,
                    expected_revision=current.revision,
                    operation="device.reconcile-unmount",
                    event_type="eidolon.kernel.device-unmounted-by-authority.v1",
                    event_data={
                        "previous_revision": current.revision,
                        "reason": reason,
                    },
                )
            except (IdempotencyConflict, RevisionConflict):
                continue
            _project_committed(self.store, self.projection, result.mount)
            unmounted += 1
        return ReconciliationResult(checked, unmounted, deferred)

    async def _rejection_reason(self, mount: DeviceMount) -> str | None:
        try:
            device = await self.devices.get_device(
                owner_id=mount.owner_id,
                device_id=mount.device_id,
            )
        except AuthorityRejected:
            return "device-missing-from-owner-scope"
        if (
            device.device_id != mount.device_id
            or device.owner_id != mount.owner_id
            or device.status != "approved"
        ):
            return "device-not-approved"

        try:
            companion = await self.companions.get_companion(
                companion_id=mount.companion_id
            )
        except AuthorityRejected:
            return "companion-missing"
        if (
            companion.companion_id != mount.companion_id
            or companion.owner_id != mount.owner_id
            or companion.status != "active"
        ):
            return "companion-not-active"
        return None

    @staticmethod
    def _request_id(mount: DeviceMount) -> str:
        source = f"{mount.device_id}\0{mount.owner_id}\0{mount.revision}".encode()
        return "reconcile-device-" + hashlib.sha256(source).hexdigest()
