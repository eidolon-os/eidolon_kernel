from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.adapters.projection.memory import InMemoryMountProjection
from eidolon_kernel.domain.errors import IdempotencyConflict, RevisionConflict
from tests.support import sample_mount


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
        assert projection.get("device-1") == restarted.get("device-1")
        assert projection.list(
            owner_id="owner-1",
            companion_id="companion-1",
            active_only=True,
            after_device_id=None,
            limit=10,
        )[0].device_id == "device-1"
    finally:
        restarted.close()


def test_partial_or_old_database_is_rejected_without_migration(tmp_path) -> None:
    path = tmp_path / "partial.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE kernel_device_mounts(device_id TEXT PRIMARY KEY)")
    connection.close()
    with pytest.raises(RuntimeError, match="migrations are unsupported"):
        SqliteMountStore(path)
