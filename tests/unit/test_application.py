import asyncio

import pytest

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
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
    FakeCompanionAuthority,
    FakeDeviceAuthority,
    MemoryStore,
    MutableClock,
)


def handler(*, devices=None, companions=None):
    store = MemoryStore()
    projection = InMemoryMountProjection()
    return (
        MountDevice(
            store,
            projection,
            devices or FakeDeviceAuthority(),
            companions or FakeCompanionAuthority(),
            MutableClock(),
        ),
        UnmountDevice(store, projection, MutableClock()),
        store,
        projection,
    )


@pytest.mark.asyncio
async def test_mount_is_idempotent_before_external_revalidation() -> None:
    devices = FakeDeviceAuthority()
    companions = FakeCompanionAuthority()
    mount, _, store, projection = handler(devices=devices, companions=companions)
    command = MountDeviceCommand("request-1", "device-1", "owner-1", "companion-1", 0)
    first = await mount.execute(command)
    devices.status = "revoked"
    companions.status = "inactive"
    replay = await mount.execute(command)
    assert first.audit_position == replay.audit_position == 1
    assert replay.replayed is True
    assert devices.calls == companions.calls == 1
    assert projection.get("device-1") == store.get("device-1")


@pytest.mark.asyncio
async def test_request_id_reuse_and_authority_mismatches_are_rejected() -> None:
    mount, _, _, _ = handler()
    command = MountDeviceCommand("same", "device-1", "owner-1", "companion-1", 0)
    await mount.execute(command)
    with pytest.raises(IdempotencyConflict):
        await mount.execute(
            MountDeviceCommand("same", "device-2", "owner-1", "companion-1", 0),
        )

    rejected, _, _, _ = handler(devices=FakeDeviceAuthority(status="revoked"))
    with pytest.raises(AuthorityRejected):
        await rejected.execute(command)

    wrong_companion, _, _, _ = handler(
        companions=FakeCompanionAuthority(actual_owner="owner-2")
    )
    with pytest.raises(AuthorityRejected):
        await wrong_companion.execute(command)


@pytest.mark.asyncio
async def test_active_mount_requires_explicit_remount_and_cas() -> None:
    mount, _, _, _ = handler()
    first = await mount.execute(
        MountDeviceCommand("r1", "device-1", "owner-1", "companion-1", 0)
    )
    with pytest.raises(Conflict):
        await mount.execute(
            MountDeviceCommand("r2", "device-1", "owner-1", "companion-2", 1),
        )
    with pytest.raises(RevisionConflict):
        await mount.execute(
            MountDeviceCommand("r3", "device-1", "owner-1", "companion-2", 0, True),
        )
    remount = await mount.execute(
        MountDeviceCommand("r4", "device-1", "owner-1", "companion-2", 1, True),
    )
    assert first.mount.revision == 1
    assert remount.mount.revision == 2
    assert remount.mount.companion_id == "companion-2"


@pytest.mark.asyncio
async def test_remount_can_never_transfer_a_device_between_owner_namespaces() -> None:
    devices = FakeDeviceAuthority()
    companions = FakeCompanionAuthority()
    mount, _, _, _ = handler(devices=devices, companions=companions)
    await mount.execute(
        MountDeviceCommand("r1", "device-1", "owner-1", "companion-1", 0)
    )

    devices.actual_owner = "owner-2"
    companions.actual_owner = "owner-2"
    with pytest.raises(NotFound, match="device mount not found"):
        await mount.execute(
            MountDeviceCommand("r1", "device-1", "owner-2", "companion-2", 0),
        )
    with pytest.raises(NotFound, match="device mount not found"):
        await mount.execute(
            MountDeviceCommand("r2", "device-1", "owner-2", "companion-2", 1, True),
        )


@pytest.mark.asyncio
async def test_unmount_is_cas_guarded_idempotent_and_reactivatable() -> None:
    mount, unmount, _, projection = handler()
    await mount.execute(
        MountDeviceCommand("r1", "device-1", "owner-1", "companion-1", 0)
    )
    with pytest.raises(RevisionConflict):
        unmount.execute(UnmountDeviceCommand("u0", "device-1", "owner-1", 2))
    result = unmount.execute(
        UnmountDeviceCommand("u1", "device-1", "owner-1", 1)
    )
    replay = unmount.execute(
        UnmountDeviceCommand("u1", "device-1", "owner-1", 1)
    )
    with pytest.raises(NotFound, match="device mount not found"):
        unmount.execute(UnmountDeviceCommand("u1", "device-1", "owner-2", 1))
    assert not result.mount.active and replay.replayed
    assert projection.get("device-1").revision == 2
    reactivated = await mount.execute(
        MountDeviceCommand("r2", "device-1", "owner-1", "companion-1", 2)
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
        FakeCompanionAuthority(),
        MutableClock(),
    )
    with pytest.raises(RuntimeError, match="disk full"):
        await use_case.execute(
            MountDeviceCommand("r", "device", "owner-1", "companion", 0)
        )
    assert projection.get("device") is None


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
        FakeCompanionAuthority(),
        MutableClock(),
    )
    result = await use_case.execute(
        MountDeviceCommand("r", "device", "owner-1", "companion", 0)
    )
    assert result.mount == projection.get("device") == store.get("device")


@pytest.mark.asyncio
async def test_concurrent_identical_mounts_share_one_stable_outcome() -> None:
    class YieldingDevice(FakeDeviceAuthority):
        async def get_device(self, **kwargs):
            await asyncio.sleep(0)
            return await super().get_device(**kwargs)

    class YieldingCompanion(FakeCompanionAuthority):
        async def get_companion(self, **kwargs):
            await asyncio.sleep(0)
            return await super().get_companion(**kwargs)

    store = MemoryStore()
    projection = InMemoryMountProjection()
    use_case = MountDevice(
        store,
        projection,
        YieldingDevice(),
        YieldingCompanion(),
        MutableClock(),
    )
    command = MountDeviceCommand("same", "device", "owner-1", "companion", 0)
    results = await asyncio.gather(
        use_case.execute(command),
        use_case.execute(command),
    )
    assert {result.audit_position for result in results} == {1}
    assert sorted(result.replayed for result in results) == [False, True]
    assert len(store.events) == 1
