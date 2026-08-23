from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.application.device_mounts import ReconcileClaimEvents
from eidolon_kernel.domain.model import ClaimEvent, DeviceRef
from tests.support import FakeDeviceAuthority, MemoryStore, MutableClock, sample_mount


def _event(*, generation: int = 1) -> ClaimEvent:
    return ClaimEvent(
        stream_position=1,
        event_id=f"claim-event-{generation}",
        event_type="live.eidolon.device.claim-revoked.v1",
        device_ref=DeviceRef(
            "device-1",
            "owner-1",
            generation,
            1,
            "sha256:hub-manifest",
        ),
        aggregate_revision=3,
        correlation_id="removal-intent-1",
        causation_id="revoke-command-1",
        occurred_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        reason="owner-removed",
    )


@pytest.mark.asyncio
async def test_matching_claim_event_unmounts_once_and_advances_durable_inbox() -> None:
    mount = sample_mount()
    store = MemoryStore(mounts={mount.device_id: mount})
    projection = InMemoryMountProjection()
    projection.rebuild(store.list_all())
    authority = FakeDeviceAuthority(claim_events=(_event(),))
    reconciler = ReconcileClaimEvents(
        store=store,
        projection=projection,
        devices=authority,
        clock=MutableClock(value=mount.updated_at + timedelta(seconds=1)),
    )

    first = await reconciler.execute()
    replay = await reconciler.execute()

    assert first.unmounted == first.consumed == 1
    assert replay.consumed == 0
    assert store.claim_event_position() == 1
    assert store.claim_event_outcome("claim-event-1") == "unmounted"
    assert store.get("device-1").active is False
    assert store.events[-1].event_type == (
        "eidolon.kernel.device-unmounted-by-claim-event.v1"
    )


@pytest.mark.asyncio
async def test_old_claim_event_cannot_unmount_a_newer_mount_generation() -> None:
    newer = replace(sample_mount(), claim_generation=2)
    store = MemoryStore(mounts={newer.device_id: newer})
    projection = InMemoryMountProjection()
    projection.rebuild(store.list_all())
    reconciler = ReconcileClaimEvents(
        store=store,
        projection=projection,
        devices=FakeDeviceAuthority(claim_events=(_event(generation=1),)),
        clock=MutableClock(value=newer.updated_at + timedelta(seconds=1)),
    )

    result = await reconciler.execute()

    assert result.ignored == result.consumed == 1
    assert store.get("device-1").active is True
    assert store.claim_event_outcome("claim-event-1") == (
        "stale-generation-ignored"
    )
