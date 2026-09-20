"""Fault schedules across intent persistence, host submission and observation."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.domain.errors import (
    Conflict,
    HostOperationFailed,
    NotReady,
    RevisionConflict,
    StateStoreFailed,
)
from eidolon_system.domain.model import ServiceCatalog
from tests.system.support import FakeHostSupervisor, FakeReadinessProbe, FixedClock
from tests.system.test_domain import service


class ScheduledHost(FakeHostSupervisor):
    """A submitted job is independent of the caller and test clock."""

    def __init__(self):
        super().__init__()
        self.job = None
        self.lose_reply = False

    async def inspect(self, target):
        return replace(await super().inspect(target), transitioning=self.job is not None)

    async def start(self, target):
        self.submit("start", target)

    async def stop(self, target):
        self.submit("stop", target)

    async def restart(self, target):
        self.submit("restart", target)

    def submit(self, action, target):
        assert self.job is None, "must not race or replace an outstanding host job"
        self.calls.append((action, target))
        self.job = (action, target)
        if self.lose_reply:
            raise HostOperationFailed("response lost after host accepted job")

    def complete(self):
        action, target = self.job
        self.job = None
        self.active[target] = action != "stop"
        if action != "stop":
            self.instances[target] = self.instances.get(target, 0) + 1


@pytest.fixture
def rig(tmp_path):
    state = SimpleNamespace(
        now=0.0, host=ScheduledHost(), store=SqliteSystemStateStore(tmp_path / "state.db")
    )

    async def new_manager(*, reopen=False):
        if reopen:
            state.store.close()
            state.store = SqliteSystemStateStore(tmp_path / "state.db")
        manager = ServiceManager(
            catalog=ServiceCatalog((service("agent"),)),
            store=state.store,
            host=state.host,
            directory=InMemoryServiceDirectory(),
            readiness=FakeReadinessProbe(),
            clock=FixedClock(),
            monotonic=lambda: state.now,
        )
        await manager.initialize()
        return manager

    state.manager = new_manager
    yield state
    state.store.close()


async def boot(rig):
    manager = await rig.manager()
    await manager.reconcile()
    rig.host.complete()
    await manager.reconcile()
    assert manager.get_service("agent").runtime_state == "ready"
    return manager


@pytest.mark.parametrize("lose_reply", [False, True])
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_slow_host_jobs_survive_daemon_restart_without_duplicate_submission(
    rig, action, lose_reply
):
    manager = await rig.manager() if action == "start" else await boot(rig)
    rig.host.lose_reply = lose_reply
    if action == "start":
        await manager.reconcile()
    elif action == "stop":
        await manager.set_enabled(
            service_id="agent", enabled=False, expected_revision=1, request_id="off"
        )
    else:
        await manager.restart(service_id="agent", expected_revision=1, request_id="restart")
    count = len(rig.host.calls)
    assert rig.store.pending_intent("agent") is not None
    # A queued restart still reports the old active PID. This must not pass health.
    with pytest.raises(NotReady):
        manager.resolve("agent", "control.http")
    replacement = await rig.manager(reopen=True)
    for _ in range(6):
        rig.now += 60  # much longer than retry timeout or normal systemd drain
        await replacement.reconcile()
    assert len(rig.host.calls) == count
    rig.host.complete()
    await replacement.reconcile()
    assert replacement.get_service("agent").runtime_state == (
        "inactive" if action == "stop" else "ready"
    )
    assert rig.store.pending_intent("agent") is None
    await replacement.reconcile()
    assert len(rig.host.calls) == count
    if action == "restart":
        replay = await replacement.restart(
            service_id="agent", expected_revision=1, request_id="restart"
        )
        assert replay.replayed
        assert len(rig.host.calls) == count


async def test_crash_after_intent_before_dispatch_eventually_submits_once(rig, monkeypatch):
    manager = await boot(rig)

    async def crash():
        raise RuntimeError("daemon crash before dispatch")

    monkeypatch.setattr(manager, "_reconcile_unlocked", crash)
    with pytest.raises(RuntimeError, match="daemon crash"):
        await manager.restart(service_id="agent", expected_revision=1, request_id="restart")
    assert rig.store.pending_intent("agent").request_id == "restart"
    assert rig.host.calls == [("start", "agent")]
    replacement = await rig.manager(reopen=True)
    replay = await replacement.restart(
        service_id="agent", expected_revision=1, request_id="restart"
    )
    assert replay.replayed
    await replacement.reconcile()
    assert rig.host.job is None  # recovered uncertain submission gets one grace period
    rig.now += 30
    await replacement.reconcile()
    assert rig.host.calls == [("start", "agent"), ("restart", "agent")]
    rig.host.complete()
    await replacement.reconcile()
    assert replacement.get_service("agent").runtime_state == "ready"


async def test_crash_after_host_completed_before_receipt_finalization_does_not_repeat(
    rig, monkeypatch
):
    manager = await boot(rig)
    await manager.restart(service_id="agent", expected_revision=1, request_id="restart")
    rig.host.complete()

    def fail(*args):
        raise StateStoreFailed("disk full")

    monkeypatch.setattr(rig.store, "finish_intent", fail)
    await manager.reconcile()
    assert manager.get_service("agent").runtime_state == "failed"
    assert rig.store.pending_intent("agent") is not None
    replacement = await rig.manager(reopen=True)
    await replacement.reconcile()
    assert rig.host.calls.count(("restart", "agent")) == 1
    assert replacement.get_service("agent").runtime_state == "ready"


@pytest.mark.parametrize("initial_action", ["start", "stop"])
async def test_latest_desired_state_waits_for_previous_job_then_converges(rig, initial_action):
    manager = await rig.manager() if initial_action == "start" else await boot(rig)
    if initial_action == "start":
        await manager.reconcile()
        await manager.set_enabled(
            service_id="agent", enabled=False, expected_revision=1, request_id="off"
        )
    else:
        await manager.set_enabled(
            service_id="agent", enabled=False, expected_revision=1, request_id="off"
        )
        await manager.set_enabled(
            service_id="agent", enabled=True, expected_revision=2, request_id="on"
        )
    assert rig.host.job[0] == initial_action
    rig.host.complete()
    await manager.reconcile()
    assert rig.host.job[0] == ("stop" if initial_action == "start" else "start")
    rig.host.complete()
    await manager.reconcile()
    assert manager.get_service("agent").runtime_state == (
        "inactive" if initial_action == "start" else "ready"
    )


async def test_invalid_revision_and_uncommitted_intent_cannot_restart_host(rig, monkeypatch):
    manager = await boot(rig)
    with pytest.raises(RevisionConflict):
        await manager.restart(service_id="agent", expected_revision=99, request_id="stale")
    assert rig.store.pending_intent("agent") is None

    def fail(**kwargs):
        raise StateStoreFailed("read only")

    monkeypatch.setattr(rig.store, "record_operation", fail)
    with pytest.raises(StateStoreFailed):
        await manager.restart(service_id="agent", expected_revision=1, request_id="new")
    assert rig.host.calls == [("start", "agent")]


async def test_different_manual_request_cannot_overtake_pending_job(rig):
    manager = await boot(rig)
    await manager.restart(service_id="agent", expected_revision=1, request_id="first")
    with pytest.raises(Conflict, match="pending operation"):
        await manager.restart(service_id="agent", expected_revision=1, request_id="second")
    assert rig.host.calls.count(("restart", "agent")) == 1


async def test_successful_reply_without_new_instance_is_not_restart_completion(rig):
    manager = await boot(rig)
    await manager.restart(service_id="agent", expected_revision=1, request_id="restart")
    rig.host.job = None  # job vanished without replacing old process
    await manager.reconcile()
    assert manager.get_service("agent").runtime_state == "starting"
    assert rig.store.pending_intent("agent") is not None
    assert rig.host.calls.count(("restart", "agent")) == 1
    rig.now += 30
    await manager.reconcile()
    assert rig.host.calls.count(("restart", "agent")) == 2


async def test_readiness_cannot_publish_a_process_replaced_during_probe(rig):
    manager = await boot(rig)

    async def check(url):
        rig.host.instances["agent"] += 1
        return True

    manager.readiness.check = check
    await manager.reconcile()
    assert manager.get_service("agent").runtime_state == "starting"
    assert manager.get_service("agent").endpoints == ()
