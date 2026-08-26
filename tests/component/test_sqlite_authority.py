from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from eidolon_sdk.device_foundation.v1 import ClaimEventCursor, ClaimEventStreamItem
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.domain.errors import IdempotencyConflict, RevisionConflict
from eidolon_kernel.domain.model import DeviceMount, request_fingerprint
from tests.support import claim_event_item, sample_mount

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")


def test_sqlite_atomically_commits_mount_request_and_ordered_audit(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    try:
        first = store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={"source": "hub"},
        )
        replay = store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={"source": "ignored-on-replay"},
        )
        inactive = sample_mount(2, request_id="request-2", active=False)
        second = store.commit(
            mount=inactive,
            expected_revision=1,
            operation="device.unmount",
            event_type="unmounted",
            event_data={"previous_revision": 1},
        )
        events = store.list_audit(after_position=0, limit=100, owner_id="owner-1")
        assert first.audit_position == replay.audit_position == 1
        assert replay.replayed is True
        assert second.audit_position == 2
        assert [event.position for event in events] == [1, 2]
        assert events[0].mount.revision == 1 and events[0].mount.active
        assert events[1].mount.revision == 2 and not events[1].mount.active
        assert store.get_request("request-2").mount == inactive
        assert store.list_audit(after_position=2, limit=1, owner_id="owner-1") == ()
        assert store.list_audit(after_position=0, limit=1, owner_id="other") == ()
    finally:
        store.close()


def test_sqlite_enforces_cas_idempotency_and_single_process_ownership(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    store = SqliteMountStore(path)
    try:
        store.commit(
            mount=sample_mount(),
            expected_revision=0,
            operation="device.mount",
            event_type="mounted",
            event_data={},
        )
        with pytest.raises(RevisionConflict):
            store.commit(
                mount=sample_mount(2, request_id="other"),
                expected_revision=0,
                operation="device.mount",
                event_type="mounted",
                event_data={},
            )
        with pytest.raises(IdempotencyConflict):
            store.commit(
                mount=sample_mount(),
                expected_revision=1,
                operation="device.unmount",
                event_type="unmounted",
                event_data={},
            )
        with pytest.raises(RevisionConflict, match="owner namespace"):
            store.commit(
                mount=replace(
                    sample_mount(2, request_id="owner-transfer"), owner_id="owner-2"
                ),
                expected_revision=1,
                operation="device.mount",
                event_type="remounted",
                event_data={},
            )
        with pytest.raises(RuntimeError, match="already owned"):
            SqliteMountStore(path)
    finally:
        store.close()


def test_restart_rebuilds_projection_from_only_authoritative_table(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first = SqliteMountStore(path)
    first.commit(
        mount=sample_mount(attached_companion_id="companion-1"),
        expected_revision=0,
        operation="device.mount",
        event_type="mounted",
        event_data={},
    )
    first.close()

    restarted = SqliteMountStore(path)
    try:
        projection = InMemoryMountProjection()
        projection.rebuild(restarted.list_all())
        assert projection.get(_DEVICE_1) == restarted.get(_DEVICE_1)
        assert projection.list(
            owner_id="owner-1",
            companion_id="companion-1",
            active_only=True,
            after_device_id=None,
            limit=10,
        )[0].device_id == _DEVICE_1
    finally:
        restarted.close()


def test_partial_or_old_database_is_rejected_without_migration(tmp_path) -> None:
    path = tmp_path / "partial.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE kernel_device_mounts(device_id TEXT PRIMARY KEY)")
    connection.close()
    with pytest.raises(RuntimeError, match="migrations are unsupported"):
        SqliteMountStore(path)


def test_schema_v6_without_owner_domain_is_rejected_instead_of_inferred(tmp_path) -> None:
    path = tmp_path / "kernel-v6.sqlite3"
    current = SqliteMountStore(path)
    current.commit(
        mount=sample_mount(),
        expected_revision=0,
        operation="device.mount",
        event_type="mounted",
        event_data={"source": "v4-characterization"},
    )
    current.close()

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE kernel_device_mounts DROP COLUMN owner_domain_id")
    connection.execute("ALTER TABLE kernel_audit_events DROP COLUMN owner_domain_id")
    connection.execute("UPDATE kernel_schema_meta SET schema_version = 6")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="does not match schema v7"):
        SqliteMountStore(path)


def _mount_from_claim(item: ClaimEventStreamItem, *, current=None) -> DeviceMount:
    ref = item.event.data.device_ref
    request_id = f"claim-event-{item.stream_position}"
    fingerprint = request_fingerprint(
        "device.mount-by-claim-event",
        {"event_id": item.event.id, "device_ref": ref.model_dump(mode="json")},
    )
    if current is None:
        return DeviceMount.first(
            device_id=ref.device_instance_id,
            owner_id=str(item.event.data.business_owner_id),
            device_ref=ref,
            at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
            request_id=request_id,
            fingerprint=fingerprint,
        )
    return current.mounted_as(
        owner_id=str(item.event.data.business_owner_id),
        device_ref=ref,
        at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        request_id=request_id,
        fingerprint=fingerprint,
    )


def test_claim_inbox_mount_and_cursor_checkpoint_are_one_transaction(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    item = claim_event_item()
    mount = _mount_from_claim(item)
    try:
        result = store.commit_claim_event(
            item=item,
            requested_after=ClaimEventCursor(stream_position=0),
            next_cursor=ClaimEventCursor(stream_position=1),
            high_watermark=4,
            outcome="mounted",
            mount=mount,
            expected_mount_revision=0,
            processed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        )
        assert result.mount == mount
        assert store.get(_DEVICE_1) == mount
        assert store.claim_event_cursor().stream_position == 1
        assert store.claim_event_high_watermark() == 4
        stored = store.claim_events_for_device(_DEVICE_1)
        assert stored[0].device_ref == item.event.data.device_ref
        row = store._connection.execute(
            "SELECT manifest_id, manifest_revision, manifest_digest "
            "FROM kernel_claim_event_inbox"
        ).fetchone()
        assert tuple(row) == ("manifest-one", 1, "sha256:" + "a" * 64)
    finally:
        store.close()


def test_claim_checkpoint_failure_rolls_back_mount_inbox_and_audit(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    item = claim_event_item()
    mount = _mount_from_claim(item)
    store._connection.execute(
        "CREATE TRIGGER fail_claim_cursor BEFORE UPDATE ON kernel_claim_event_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected cursor failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected cursor failure"):
            store.commit_claim_event(
                item=item,
                requested_after=ClaimEventCursor(stream_position=0),
                next_cursor=ClaimEventCursor(stream_position=1),
                high_watermark=1,
                outcome="mounted",
                mount=mount,
                expected_mount_revision=0,
                processed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
            )
        assert store.get(_DEVICE_1) is None
        assert store.claim_events_for_device(_DEVICE_1) == ()
        assert store.claim_event_cursor().stream_position == 0
        assert store.list_audit(after_position=0, limit=10, owner_id="owner-1") == ()
    finally:
        store.close()


def test_claim_same_event_replays_and_same_source_id_different_bytes_conflict(tmp_path) -> None:
    store = SqliteMountStore(tmp_path / "kernel.sqlite3")
    item = claim_event_item()
    mount = _mount_from_claim(item)
    arguments = {
        "item": item,
        "requested_after": ClaimEventCursor(stream_position=0),
        "next_cursor": ClaimEventCursor(stream_position=1),
        "high_watermark": 1,
        "outcome": "mounted",
        "mount": mount,
        "expected_mount_revision": 0,
        "processed_at": datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    }
    try:
        first = store.commit_claim_event(**arguments)
        replay = store.commit_claim_event(**arguments)
        assert first.replayed is False
        assert replay.replayed is True
        conflicting = ClaimEventStreamItem(
            stream_position=1,
            event=item.event.model_copy(update={"correlationid": "intent-other"}),
        )
        with pytest.raises(IdempotencyConflict, match=r"source\+id"):
            store.commit_claim_event(**{**arguments, "item": conflicting})
        assert store.claim_event_cursor().stream_position == 1
    finally:
        store.close()


def test_claim_cursor_and_high_watermark_survive_restart(tmp_path) -> None:
    path = tmp_path / "kernel.sqlite3"
    first = SqliteMountStore(path)
    item = claim_event_item()
    first.commit_claim_event(
        item=item,
        requested_after=ClaimEventCursor(stream_position=0),
        next_cursor=ClaimEventCursor(stream_position=1),
        high_watermark=7,
        outcome="mounted",
        mount=_mount_from_claim(item),
        expected_mount_revision=0,
        processed_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    )
    first.close()

    restarted = SqliteMountStore(path)
    try:
        assert restarted.claim_event_cursor().stream_position == 1
        assert restarted.claim_event_high_watermark() == 7
        restarted.checkpoint_claim_cursor(
            requested_after=ClaimEventCursor(stream_position=1),
            next_cursor=ClaimEventCursor(stream_position=1),
            high_watermark=9,
            processed_at=datetime(2026, 8, 4, 9, 1, tzinfo=UTC),
        )
        assert restarted.claim_event_high_watermark() == 9
        with pytest.raises(RevisionConflict, match="inconsistent"):
            restarted.checkpoint_claim_cursor(
                requested_after=ClaimEventCursor(stream_position=1),
                next_cursor=ClaimEventCursor(stream_position=1),
                high_watermark=8,
                processed_at=datetime(2026, 8, 4, 9, 2, tzinfo=UTC),
            )
        assert restarted.claim_event_high_watermark() == 9
    finally:
        restarted.close()
