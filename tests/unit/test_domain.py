from datetime import UTC, datetime, timedelta

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.domain.commands import (
    ASSIGNMENT_ORIGIN_OWNER,
    MountDeviceCommand,
    ReplaceAssignmentCommand,
    UnmountDeviceCommand,
)
from eidolon_kernel.domain.errors import InvalidRequest
from eidolon_kernel.domain.model import DeviceMount, DeviceRef

_DEVICE = named_device_instance_id("device")


# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEV = named_device_instance_id("dev")

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")

NOW = datetime(2026, 8, 4, tzinfo=UTC)
FP = "sha256:" + "a" * 64


def _ref(device_id=_DEVICE_1, owner_id="owner-1") -> DeviceRef:
    return DeviceRef(
        device_instance_id=device_id,
        owner_domain_id=owner_id,
        owner_domain_generation=1,
        claim_generation=1,
        trust_epoch=1,
    )


def test_mount_aggregate_transitions_preserve_monotonic_revision() -> None:
    first = DeviceMount.first(
        device_id=_DEVICE_1,
        owner_id="owner-1",
        device_ref=_ref(),
        at=NOW,
        request_id="request-1",
        fingerprint=FP,
    )
    assert first.device_ref == _ref()
    inactive = first.unmounted(at=NOW, request_id="request-2", fingerprint=FP)
    mounted = inactive.mounted_as(
        owner_id="owner-1",
        device_ref=_ref(),
        at=NOW,
        request_id="request-3",
        fingerprint=FP,
    )
    assert (first.revision, inactive.revision, mounted.revision) == (1, 2, 3)
    assert first.active and not inactive.active and mounted.active
    # A mount says whether a device belongs here and at which generation, and
    # nothing about which Eidolon answers through it. That fact moved to a Body
    # assignment precisely so remounting could stop erasing it.
    assert not hasattr(mounted, "attached_companion_id")


def test_command_fingerprint_is_canonical_and_sensitive() -> None:
    command = MountDeviceCommand("request", _DEVICE, "owner", 0)
    same = MountDeviceCommand("request", _DEVICE, "owner", 0)
    replacement = MountDeviceCommand(
        "request", _DEVICE, "owner", 0, replace_existing=True
    )
    assert command.fingerprint == same.fingerprint
    assert command.fingerprint != replacement.fingerprint
    assert len(command.fingerprint) == 71


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MountDeviceCommand("r", "d", "o", -1),
        lambda: ReplaceAssignmentCommand(
            request_id="r",
            owner_id="o",
            body_endpoint_id="d:body",
            expected_assignment_revision=-1,
            companion_id="c",
            origin=ASSIGNMENT_ORIGIN_OWNER,
        ),
        lambda: UnmountDeviceCommand("r", "d", "o", 0),
        lambda: DeviceMount.first(
            device_id=_DEV,
            owner_id="owner-1",
            device_ref=_ref(_DEV),
            at=datetime(2026, 8, 4),
            request_id="r",
            fingerprint=FP,
        ),
    ],
)
def test_invalid_domain_values_are_rejected(factory) -> None:
    with pytest.raises(InvalidRequest):
        factory()


def test_mount_rejects_non_hex_fingerprint_and_backwards_transition_time() -> None:
    with pytest.raises(InvalidRequest, match="sha256"):
        DeviceMount.first(
            device_id=_DEV,
            owner_id="owner-1",
            device_ref=_ref(_DEV),
            at=NOW,
            request_id="r",
            fingerprint="sha256:" + "z" * 64,
        )
    first = DeviceMount.first(
        device_id=_DEV,
        owner_id="owner-1",
        device_ref=_ref(_DEV),
        at=NOW,
        request_id="r",
        fingerprint=FP,
    )
    with pytest.raises(InvalidRequest, match="backwards"):
        first.unmounted(at=NOW - timedelta(seconds=1), request_id="u", fingerprint=FP)


def test_mount_aggregate_rejects_owner_namespace_transfer() -> None:
    first = DeviceMount.first(
        device_id=_DEV,
        owner_id="owner-1",
        device_ref=_ref(_DEV),
        at=NOW,
        request_id="r",
        fingerprint=FP,
    )
    with pytest.raises(InvalidRequest, match="owner namespace"):
        first.mounted_as(
            owner_id="owner-2",
            device_ref=_ref(_DEV, "owner-2"),
            at=NOW,
            request_id="r2",
            fingerprint=FP,
        )
