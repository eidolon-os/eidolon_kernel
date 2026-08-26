import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.application.device_mounts import (
    AttachCompanion,
    DetachCompanion,
    MountDevice,
    ReconcileMountPrerequisites,
    UnmountDevice,
)
from eidolon_kernel.domain.commands import (
    AttachCompanionCommand,
    DetachCompanionCommand,
    MountDeviceCommand,
    UnmountDeviceCommand,
)
from eidolon_kernel.domain.errors import AuthorityUnavailable
from tests.support import (
    FakeCompanionAuthority,
    FakeDeviceAuthority,
    MemoryStore,
    MutableClock,
)

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")


def services(*, devices=None, companions=None):
    store = MemoryStore()
    projection = InMemoryMountProjection()
    clock = MutableClock()
    devices = devices or FakeDeviceAuthority()
    companions = companions or FakeCompanionAuthority()
    return (
        MountDevice(store, projection, devices, clock),
        AttachCompanion(store, projection, companions, clock),
        DetachCompanion(store, projection, clock),
        ReconcileMountPrerequisites(
            store, projection, devices, companions, clock
        ),
        store,
        projection,
    )


@pytest.mark.asyncio
async def test_device_mount_does_not_require_or_validate_a_companion() -> None:
    companions = FakeCompanionAuthority(status="inactive")
    mount, _, _, _, store, _ = services(companions=companions)

    result = await mount.execute(
        MountDeviceCommand(
            request_id="mount-1",
            device_id=_DEVICE_1,
            owner_id="owner-1",
            expected_revision=0,
        )
    )

    assert result.mount.active is True
    assert result.mount.attached_companion_id is None
    assert store.get(_DEVICE_1) == result.mount
    assert companions.calls == 0


@pytest.mark.asyncio
async def test_attach_and_detach_are_explicit_cas_guarded_transitions() -> None:
    mount, attach, detach, _, _, _ = services()
    mounted = await mount.execute(
        MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0)
    )
    attached = await attach.execute(
        AttachCompanionCommand(
            "attach-1", _DEVICE_1, "owner-1", "companion-1", mounted.mount.revision
        )
    )
    detached = detach.execute(
        DetachCompanionCommand(
            "detach-1", _DEVICE_1, "owner-1", attached.mount.revision
        )
    )

    assert attached.mount.attached_companion_id == "companion-1"
    assert detached.mount.attached_companion_id is None
    assert detached.mount.active is True
    assert (mounted.mount.revision, attached.mount.revision, detached.mount.revision) == (
        1,
        2,
        3,
    )


@pytest.mark.asyncio
async def test_unmount_atomically_ends_optional_attachment() -> None:
    mount, attach, _, _, store, projection = services()
    mounted = await mount.execute(
        MountDeviceCommand("mount-1", _DEVICE_1, "owner-1", 0)
    )
    attached = await attach.execute(
        AttachCompanionCommand(
            "attach-1", _DEVICE_1, "owner-1", "companion-1", mounted.mount.revision
        )
    )

    result = UnmountDevice(
        store, projection, MutableClock(value=attached.mount.updated_at)
    ).execute(
        UnmountDeviceCommand(
            "unmount-1", _DEVICE_1, "owner-1", attached.mount.revision
        )
    )

    assert result.mount.active is False
    assert result.mount.attached_companion_id is None
    assert result.mount.revision == 3
    assert store.events[-1].data["previous_attached_companion_id"] == "companion-1"


@pytest.mark.asyncio
async def test_reconciliation_detaches_inactive_companion_but_keeps_device_mounted() -> None:
    companions = FakeCompanionAuthority()
    mount, attach, _, reconciliation, store, projection = services(
        companions=companions
    )
    await mount.execute(MountDeviceCommand("mount", _DEVICE_1, "owner-1", 0))
    await attach.execute(
        AttachCompanionCommand("attach", _DEVICE_1, "owner-1", "companion-1", 1)
    )
    companions.status = "inactive"

    result = await reconciliation.execute()

    current = store.get(_DEVICE_1)
    assert result.checked == result.detached == 1
    assert result.unmounted == result.deferred == 0
    assert current.active is True
    assert current.attached_companion_id is None
    assert projection.get(_DEVICE_1) == current
    assert store.events[-1].event_type == (
        "eidolon.kernel.companion-detached-by-authority.v1"
    )


@pytest.mark.asyncio
async def test_reconciliation_asks_nothing_about_a_mount_with_no_companion() -> None:
    """A mount nothing is attached to has nothing this scan can check.

    Its Claim is the stream's to adjudicate, so an authority reached here would
    only be re-deriving a fact this Kernel was already told.
    """

    devices = FakeDeviceAuthority()
    companions = FakeCompanionAuthority()
    mount, _, _, reconciliation, store, _ = services(
        devices=devices, companions=companions
    )
    await mount.execute(MountDeviceCommand("mount", _DEVICE_1, "owner-1", 0))
    devices.status = "revoked"

    result = await reconciliation.execute()

    assert result.checked == 1
    assert result.unmounted == result.detached == result.deferred == 0
    assert store.get(_DEVICE_1).active is True
    assert companions.calls == 0


@pytest.mark.asyncio
async def test_reconciliation_defers_outages_without_detaching() -> None:
    """An authority that did not answer has not revoked anything."""

    class OfflineCompanionAuthority(FakeCompanionAuthority):
        async def get_companion(self, **kwargs):
            raise AuthorityUnavailable("Companion authority offline")

    mount, attach, _, _, store, projection = services()
    await mount.execute(MountDeviceCommand("mount", _DEVICE_1, "owner-1", 0))
    await attach.execute(
        AttachCompanionCommand("attach", _DEVICE_1, "owner-1", "companion-1", 1)
    )

    result = await ReconcileMountPrerequisites(
        store,
        projection,
        FakeDeviceAuthority(),
        OfflineCompanionAuthority(),
        MutableClock(),
    ).execute()

    assert result.checked == result.deferred == 1
    assert result.unmounted == result.detached == 0
    assert store.get(_DEVICE_1).active is True
    assert store.get(_DEVICE_1).attached_companion_id == "companion-1"
