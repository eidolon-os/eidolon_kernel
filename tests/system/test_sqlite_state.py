from __future__ import annotations

import pytest

from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.domain.errors import IdempotencyConflict, RevisionConflict, StateStoreFailed
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


def test_pending_intent_and_request_commit_atomically_and_complete_without_changing_replay(tmp_path):
    from dataclasses import replace

    from eidolon_system.domain.model import RuntimeIntent

    store = SqliteSystemStateStore(tmp_path / "state.db")
    try:
        now = FixedClock().now()
        store.ensure_services((service("agent"),), now=now)
        intent = RuntimeIntent("agent", "first", "restart", "old-pid", "lan-a")
        result = store.record_operation(service_id="agent", operation="system.service.restart",
            expected_revision=1, request_id="first", fingerprint="same", now=now, intent=intent)
        assert store.pending_intent("agent") == intent
        with pytest.raises(StateStoreFailed):
            store.record_operation(service_id="agent", operation="system.service.restart",
                expected_revision=1, request_id="second", fingerprint="second", now=now,
                intent=replace(intent, request_id="second"))
        assert store.get_request(request_id="second", operation="system.service.restart", fingerprint="second") is None
        assert len(store.list_audit(after_position=0, limit=10)) == 1
        updated = replace(intent, network_input="lan-b")
        store.update_intent(updated)
        assert store.pending_intent("agent") == updated
        store.finish_intent(updated)
        assert store.pending_intent("agent") is None
        replay = store.get_request(request_id="first", operation="system.service.restart", fingerprint="same")
        assert replay.state == result.state
        assert replay.audit_position == result.audit_position
        assert replay.replayed
    finally:
        store.close()
