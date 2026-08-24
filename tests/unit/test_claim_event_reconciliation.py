from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from eidolon_sdk.device_foundation.v1 import (
    ClaimActivatedData,
    ClaimActivatedEvent,
    ClaimEventStreamItem,
    ClaimRevokedData,
    ClaimRevokedEvent,
    DeviceRef,
    ManifestRef,
)

from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.application.device_mounts import ReconcileClaimEvents
from eidolon_kernel.domain.errors import RevisionConflict
from tests.support import FakeDeviceAuthority, MemoryStore, MutableClock, sample_mount

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
MANIFEST = ManifestRef(
    manifest_id="manifest-1",
    revision=1,
    digest="sha256:" + "a" * 64,
)


def _ref(
    *, generation: int = 1, owner_generation: int = 1, owner: str = "owner-1"
) -> DeviceRef:
    return DeviceRef(
        device_instance_id="device-1",
        owner_domain_id=owner,
        owner_domain_generation=owner_generation,
        claim_generation=generation,
        trust_epoch=1,
    )


def _activated(
    *,
    position: int,
    aggregate_revision: int,
    generation: int = 1,
    owner_generation: int = 1,
    owner: str = "owner-1",
) -> ClaimEventStreamItem:
    ref = _ref(
        generation=generation, owner_generation=owner_generation, owner=owner
    )
    at = NOW + timedelta(seconds=position)
    return ClaimEventStreamItem(
        stream_position=position,
        event=ClaimActivatedEvent(
            id=f"claim-activated-{position}",
            subject="device-instances/device-1",
            time=at,
            ownerdomainid=owner,
            aggregaterev=aggregate_revision,
            correlationid="removal-intent-1",
            causationid="grant-ack-1",
            data=ClaimActivatedData(
                device_ref=ref,
                manifest_ref=MANIFEST,
                approval_decision_id="decision-1",
                activated_at=at,
            ),
        ),
    )


def _revoked(
    *,
    position: int,
    aggregate_revision: int,
    generation: int = 1,
    owner_generation: int = 1,
    owner: str = "owner-1",
) -> ClaimEventStreamItem:
    ref = _ref(
        generation=generation, owner_generation=owner_generation, owner=owner
    )
    at = NOW + timedelta(seconds=position)
    return ClaimEventStreamItem(
        stream_position=position,
        event=ClaimRevokedEvent(
            id=f"claim-revoked-{position}",
            subject="device-instances/device-1",
            time=at,
            ownerdomainid=owner,
            aggregaterev=aggregate_revision,
            correlationid="removal-intent-1",
            causationid="revoke-command-1",
            data=ClaimRevokedData(
                device_ref=ref,
                reason="owner-removed",
                revoked_at=at,
            ),
        ),
    )


def _reconciler(store: MemoryStore, *items: ClaimEventStreamItem) -> ReconcileClaimEvents:
    projection = InMemoryMountProjection()
    projection.rebuild(store.list_all())
    return ReconcileClaimEvents(
        store=store,
        projection=projection,
        devices=FakeDeviceAuthority(claim_events=items),
        clock=MutableClock(value=NOW + timedelta(minutes=1)),
    )


@pytest.mark.asyncio
async def test_claim_activated_mounts_once_and_advances_canonical_cursor() -> None:
    store = MemoryStore()
    reconciler = _reconciler(store, _activated(position=1, aggregate_revision=5))

    first = await reconciler.execute()
    replay = await reconciler.execute()

    assert first.mounted == first.consumed == 1
    assert replay.consumed == 0
    assert store.claim_event_cursor().stream_position == 1
    assert store.get("device-1").active is True
    assert store.get("device-1").claim_generation == 1


@pytest.mark.asyncio
async def test_matching_claim_revoked_unmounts_once() -> None:
    mount = sample_mount()
    store = MemoryStore(mounts={mount.device_id: mount})
    reconciler = _reconciler(store, _revoked(position=1, aggregate_revision=6))

    result = await reconciler.execute()

    assert result.unmounted == result.consumed == 1
    assert store.get("device-1").active is False
    assert store.events[-1].event_type == (
        "eidolon.kernel.device-unmounted-by-claim-event.v1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mount",
    [
        replace(sample_mount(), claim_generation=2),
        replace(sample_mount(), owner_domain_generation=2),
        replace(sample_mount(), trust_epoch=2),
    ],
)
async def test_old_generation_event_cannot_unmount_new_mount(mount) -> None:
    store = MemoryStore(mounts={mount.device_id: mount})
    result = await _reconciler(
        store, _revoked(position=1, aggregate_revision=6)
    ).execute()

    assert result.ignored == result.consumed == 1
    assert store.get("device-1").active is True


@pytest.mark.asyncio
async def test_revoked_generation_is_terminal_and_cannot_be_reactivated() -> None:
    mount = sample_mount()
    store = MemoryStore(mounts={mount.device_id: mount})
    reconciler = _reconciler(
        store,
        _revoked(position=1, aggregate_revision=6),
        _activated(position=2, aggregate_revision=7),
    )

    result = await reconciler.execute()

    assert result.unmounted == 1
    assert result.ignored == 1
    assert store.get("device-1").active is False


@pytest.mark.asyncio
async def test_new_claim_generation_can_mount_after_old_generation_revocation() -> None:
    mount = sample_mount()
    store = MemoryStore(mounts={mount.device_id: mount})
    reconciler = _reconciler(
        store,
        _revoked(position=1, aggregate_revision=6),
        _activated(position=2, aggregate_revision=7, generation=2),
    )

    result = await reconciler.execute()

    assert result.unmounted == result.mounted == 1
    assert store.get("device-1").active is True
    assert store.get("device-1").claim_generation == 2


@pytest.mark.asyncio
async def test_aggregate_out_of_order_fails_closed_without_advancing_cursor() -> None:
    store = MemoryStore()
    await _reconciler(store, _activated(position=1, aggregate_revision=7)).execute()
    second = _reconciler(
        store,
        _activated(position=1, aggregate_revision=7),
        _revoked(position=2, aggregate_revision=6),
    )

    with pytest.raises(RevisionConflict, match="aggregate revision"):
        await second.execute()

    assert store.claim_event_cursor().stream_position == 1
    assert store.get("device-1").active is True


@pytest.mark.asyncio
async def test_owner_domain_change_for_same_device_fails_closed() -> None:
    store = MemoryStore()
    await _reconciler(store, _activated(position=1, aggregate_revision=5)).execute()

    with pytest.raises(RevisionConflict, match="Owner Domain"):
        await _reconciler(
            store,
            _activated(position=1, aggregate_revision=5),
            _revoked(
                position=2,
                aggregate_revision=6,
                owner="owner-other",
            ),
        ).execute()

    assert store.claim_event_cursor().stream_position == 1
    assert store.get("device-1").active is True


@pytest.mark.asyncio
async def test_empty_page_cannot_skip_a_claim_high_watermark_gap() -> None:
    store = MemoryStore()
    reconciler = _reconciler(store)
    reconciler.devices.high_watermark = 3

    with pytest.raises(RevisionConflict, match="high watermark"):
        await reconciler.execute()

    assert store.claim_event_cursor().stream_position == 0
