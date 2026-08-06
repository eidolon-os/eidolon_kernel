from __future__ import annotations

import pytest

from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.domain.errors import IdempotencyConflict, RevisionConflict
from tests.system.support import FixedClock
from tests.system.test_domain import service


def test_sqlite_seeds_desired_state_commits_cas_audit_and_replay(tmp_path) -> None:
    path = tmp_path / "system.sqlite3"
    store = SqliteSystemStateStore(path)
    now = FixedClock().now()
    store.ensure_services((service("kernel", required=True), service("agent")), now=now)
    assert [(item.service_id, item.enabled, item.revision) for item in store.list_states()] == [
        ("agent", True, 1),
        ("kernel", True, 1),
    ]

    result = store.set_enabled(
        service_id="agent",
        enabled=False,
        expected_revision=1,
        request_id="req-disable-agent",
        fingerprint="sha256:" + "a" * 64,
        now=now,
    )
    assert result.state.revision == 2
    assert result.state.enabled is False
    assert result.audit_position == 1
    replay = store.set_enabled(
        service_id="agent",
        enabled=False,
        expected_revision=1,
        request_id="req-disable-agent",
        fingerprint="sha256:" + "a" * 64,
        now=now,
    )
    assert replay.replayed is True
    assert len(store.list_audit(after_position=0, limit=10)) == 1

    with pytest.raises(IdempotencyConflict):
        store.set_enabled(
            service_id="agent",
            enabled=True,
            expected_revision=2,
            request_id="req-disable-agent",
            fingerprint="sha256:" + "b" * 64,
            now=now,
        )
    with pytest.raises(RevisionConflict):
        store.set_enabled(
            service_id="agent",
            enabled=True,
            expected_revision=1,
            request_id="req-enable-agent",
            fingerprint="sha256:" + "c" * 64,
            now=now,
        )
    store.close()

    reopened = SqliteSystemStateStore(path)
    assert reopened.get("agent").revision == 2
    reopened.close()


def test_sqlite_records_idempotent_operational_command_and_rejects_second_owner(tmp_path) -> None:
    path = tmp_path / "system.sqlite3"
    store = SqliteSystemStateStore(path)
    now = FixedClock().now()
    store.ensure_services((service("agent"),), now=now)
    result = store.record_operation(
        service_id="agent",
        operation="system.service.restart",
        expected_revision=1,
        request_id="req-restart-agent",
        fingerprint="sha256:" + "d" * 64,
        now=now,
    )
    replay = store.record_operation(
        service_id="agent",
        operation="system.service.restart",
        expected_revision=1,
        request_id="req-restart-agent",
        fingerprint="sha256:" + "d" * 64,
        now=now,
    )
    assert result.state.revision == 1
    assert replay.replayed is True
    with pytest.raises(RuntimeError, match="already owned"):
        SqliteSystemStateStore(path)
    store.close()
