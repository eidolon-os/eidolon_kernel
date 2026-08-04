from datetime import UTC, datetime, timedelta

import pytest

from eidolon_kernel.domain.commands import MountDeviceCommand, UnmountDeviceCommand
from eidolon_kernel.domain.errors import InvalidRequest
from eidolon_kernel.domain.model import Actor, DeviceMount
from tests.support import ACTOR

NOW = datetime(2026, 8, 4, tzinfo=UTC)
FP = "sha256:" + "a" * 64


def test_mount_aggregate_transitions_preserve_monotonic_revision() -> None:
    first = DeviceMount.first(
        device_id="device-1",
        owner_id="owner-1",
        companion_id="companion-1",
        at=NOW,
        actor=ACTOR,
        request_id="request-1",
        fingerprint=FP,
    )
    inactive = first.unmounted(
        at=NOW, actor=ACTOR, request_id="request-2", fingerprint=FP
    )
    mounted = inactive.mounted_as(
        owner_id="owner-1",
        companion_id="companion-2",
        at=NOW,
        actor=ACTOR,
        request_id="request-3",
        fingerprint=FP,
    )
    assert (first.revision, inactive.revision, mounted.revision) == (1, 2, 3)
    assert first.active and not inactive.active and mounted.active
    assert mounted.companion_id == "companion-2"


def test_command_fingerprint_is_canonical_and_sensitive() -> None:
    command = MountDeviceCommand("request", "device", "owner", "companion", 0)
    same = MountDeviceCommand("request", "device", "owner", "companion", 0)
    replacement = MountDeviceCommand(
        "request", "device", "owner", "companion", 0, replace_existing=True
    )
    assert command.fingerprint == same.fingerprint
    assert command.fingerprint != replacement.fingerprint
    assert len(command.fingerprint) == 71


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Actor("", "owner", "test"),
        lambda: MountDeviceCommand("r", "d", "o", "c", -1),
        lambda: UnmountDeviceCommand("r", "d", "o", 0),
        lambda: DeviceMount.first(
            device_id="d",
            owner_id="owner-1",
            companion_id="c",
            at=datetime(2026, 8, 4),
            actor=ACTOR,
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
            device_id="d",
            owner_id="owner-1",
            companion_id="c",
            at=NOW,
            actor=ACTOR,
            request_id="r",
            fingerprint="sha256:" + "z" * 64,
        )
    first = DeviceMount.first(
        device_id="d",
        owner_id="owner-1",
        companion_id="c",
        at=NOW,
        actor=ACTOR,
        request_id="r",
        fingerprint=FP,
    )
    with pytest.raises(InvalidRequest, match="backwards"):
        first.unmounted(
            at=NOW - timedelta(seconds=1),
            actor=ACTOR,
            request_id="u",
            fingerprint=FP,
        )
