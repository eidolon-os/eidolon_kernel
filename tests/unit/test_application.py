import asyncio
import dataclasses

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.application.body_assignments import ReconcileAssignments
from eidolon_kernel.application.device_mounts import MountDevice, UnmountDevice
from eidolon_kernel.domain.commands import MountDeviceCommand, UnmountDeviceCommand
from eidolon_kernel.domain.errors import (
    AuthorityRejected,
    Conflict,
    IdempotencyConflict,
    NotFound,
    RevisionConflict,
)
from tests.support import (
    FakeDeviceAuthority,
    MemoryStore,
    MutableClock,
)

_DEVICE = named_device_instance_id("device")


# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_2 = named_device_instance_id("device-2")

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")


def handler(*, devices=None):
    store = MemoryStore()
    projection = InMemoryMountProjection()
    return (
        MountDevice(
            store,
            projection,
            devices or FakeDeviceAuthority(),
            MutableClock(),
        ),
        UnmountDevice(store, projection, MutableClock()),
        store,
        projection,
    )


@pytest.mark.asyncio
async def test_mount_is_idempotent_before_external_revalidation() -> None:
    devices = FakeDeviceAuthority()
    mount, _, store, projection = handler(devices=devices)
    command = MountDeviceCommand("request-1", _DEVICE_1, "owner-1", 0)
    first = await mount.execute(command)
    devices.status = "revoked"
    replay = await mount.execute(command)
    assert first.audit_position == replay.audit_position == 1
    assert replay.replayed is True
    assert devices.calls == 1
    assert projection.get(_DEVICE_1) == store.get(_DEVICE_1)


@pytest.mark.asyncio
async def test_request_id_reuse_and_authority_mismatches_are_rejected() -> None:
    mount, _, _, _ = handler()
    command = MountDeviceCommand("same", _DEVICE_1, "owner-1", 0)
    await mount.execute(command)
    with pytest.raises(IdempotencyConflict):
        await mount.execute(
            MountDeviceCommand("same", _DEVICE_2, "owner-1", 0),
        )

    rejected, _, _, _ = handler(devices=FakeDeviceAuthority(status="revoked"))
    with pytest.raises(AuthorityRejected):
        await rejected.execute(command)

@pytest.mark.asyncio
async def test_active_mount_requires_explicit_remount_and_cas() -> None:
    mount, _, _, _ = handler()
    first = await mount.execute(
        MountDeviceCommand("r1", _DEVICE_1, "owner-1", 0)
    )
    with pytest.raises(Conflict):
        await mount.execute(
            MountDeviceCommand("r2", _DEVICE_1, "owner-1", 1),
        )
    with pytest.raises(RevisionConflict):
        await mount.execute(
            MountDeviceCommand("r3", _DEVICE_1, "owner-1", 0, True),
        )
    remount = await mount.execute(
        MountDeviceCommand("r4", _DEVICE_1, "owner-1", 1, True),
    )
    assert first.mount.revision == 1
    assert remount.mount.revision == 2


@pytest.mark.asyncio
async def test_remount_can_never_transfer_a_device_between_owner_namespaces() -> None:
    devices = FakeDeviceAuthority()
    mount, _, _, _ = handler(devices=devices)
    await mount.execute(
        MountDeviceCommand("r1", _DEVICE_1, "owner-1", 0)
    )

    devices.actual_owner = "owner-2"
    with pytest.raises(NotFound, match="device mount not found"):
        await mount.execute(
            MountDeviceCommand("r1", _DEVICE_1, "owner-2", 0),
        )
    with pytest.raises(NotFound, match="device mount not found"):
        await mount.execute(
            MountDeviceCommand("r2", _DEVICE_1, "owner-2", 1, True),
        )


@pytest.mark.asyncio
async def test_unmount_is_cas_guarded_idempotent_and_reactivatable() -> None:
    mount, unmount, _, projection = handler()
    await mount.execute(
        MountDeviceCommand("r1", _DEVICE_1, "owner-1", 0)
    )
    with pytest.raises(RevisionConflict):
        unmount.execute(UnmountDeviceCommand("u0", _DEVICE_1, "owner-1", 2))
    result = unmount.execute(
        UnmountDeviceCommand("u1", _DEVICE_1, "owner-1", 1)
    )
    replay = unmount.execute(
        UnmountDeviceCommand("u1", _DEVICE_1, "owner-1", 1)
    )
    with pytest.raises(NotFound, match="device mount not found"):
        unmount.execute(UnmountDeviceCommand("u1", _DEVICE_1, "owner-2", 1))
    assert not result.mount.active and replay.replayed
    assert projection.get(_DEVICE_1).revision == 2
    reactivated = await mount.execute(
        MountDeviceCommand("r2", _DEVICE_1, "owner-1", 2)
    )
    assert reactivated.mount.active and reactivated.mount.revision == 3


@pytest.mark.asyncio
async def test_missing_mount_is_rejected() -> None:
    _, unmount, _, _ = handler()
    with pytest.raises(NotFound):
        unmount.execute(UnmountDeviceCommand("u", "missing", "owner-1", 1))


@pytest.mark.asyncio
async def test_failed_authoritative_commit_never_updates_projection() -> None:
    class FailingStore(MemoryStore):
        def commit(self, **kwargs):
            raise RuntimeError("disk full")

    store = FailingStore()
    projection = InMemoryMountProjection()
    use_case = MountDevice(
        store,
        projection,
        FakeDeviceAuthority(),
        MutableClock(),
    )
    with pytest.raises(RuntimeError, match="disk full"):
        await use_case.execute(
            MountDeviceCommand("r", _DEVICE, "owner-1", 0)
        )
    assert projection.get(_DEVICE) is None


@pytest.mark.asyncio
async def test_projection_increment_failure_rebuilds_from_authority() -> None:
    class FlakyProjection(InMemoryMountProjection):
        fail = True

        def put(self, mount):
            if self.fail:
                self.fail = False
                raise RuntimeError("projection update failed")
            super().put(mount)

    store = MemoryStore()
    projection = FlakyProjection()
    use_case = MountDevice(
        store,
        projection,
        FakeDeviceAuthority(),
        MutableClock(),
    )
    result = await use_case.execute(
        MountDeviceCommand("r", _DEVICE, "owner-1", 0)
    )
    assert result.mount == projection.get(_DEVICE) == store.get(_DEVICE)


@pytest.mark.asyncio
async def test_concurrent_identical_mounts_share_one_stable_outcome() -> None:
    class YieldingDevice(FakeDeviceAuthority):
        async def get_device(self, **kwargs):
            await asyncio.sleep(0)
            return await super().get_device(**kwargs)

    store = MemoryStore()
    projection = InMemoryMountProjection()
    use_case = MountDevice(
        store,
        projection,
        YieldingDevice(),
        MutableClock(),
    )
    command = MountDeviceCommand("same", _DEVICE, "owner-1", 0)
    results = await asyncio.gather(
        use_case.execute(command),
        use_case.execute(command),
    )
    assert {result.audit_position for result in results} == {1}
    assert sorted(result.replayed for result in results) == [False, True]
    assert len(store.events) == 1


@pytest.mark.asyncio
async def test_reconciliation_cannot_adjudicate_a_claim_because_it_cannot_ask() -> None:
    """Whether a Claim stands is the Claim stream's answer, not this scan's.

    This scan used to ask a second Hub surface and unmount on its answer. That
    surface knows nothing about a canonically claimed device, so every device
    added through Admission was mounted from its ClaimActivated event and
    unmounted seconds later — and could then never be mounted again, because
    the same reference read as terminal.

    It is now unable to make that mistake rather than merely instructed not to:
    the scan holds no Device authority at all, and it converges assignments
    rather than mounts. Asserted structurally, because a behavioural test would
    only be checking that a call nobody can make was not made.
    """

    ports = {field.name for field in dataclasses.fields(ReconcileAssignments)}
    assert "devices" not in ports
    assert ports == {"store", "projection", "companions", "clock"}
