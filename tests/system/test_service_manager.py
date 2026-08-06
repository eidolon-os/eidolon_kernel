from __future__ import annotations

import pytest

from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.domain.errors import Conflict, HostOperationFailed, NotReady
from eidolon_system.domain.model import HostServiceState, ServiceCatalog
from tests.system.support import FakeHostSupervisor, FakeReadinessProbe, FixedClock
from tests.system.test_domain import service


def build_manager(tmp_path):
    catalog = ServiceCatalog(
        (
            service("agent", dependencies=("kernel",)),
            service("kernel", required=True),
        )
    )
    host = FakeHostSupervisor()
    directory = InMemoryServiceDirectory()
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(
        catalog=catalog,
        store=store,
        directory=directory,
        host=host,
        readiness=FakeReadinessProbe(),
        clock=FixedClock(),
    )
    return manager, store, host, directory


@pytest.mark.asyncio
async def test_reconcile_starts_in_dependency_order_and_publishes_only_ready(tmp_path) -> None:
    manager, store, host, directory = build_manager(tmp_path)
    await manager.initialize()
    await manager.reconcile()
    assert host.calls == [("start", "kernel"), ("start", "agent")]
    assert [item.service_id for item in manager.list_services()] == ["agent", "kernel"]
    assert manager.get_service("agent").runtime_state == "ready"
    assert directory.resolve("agent", "control.http").address.endswith("/agent")
    store.close()


@pytest.mark.asyncio
async def test_disable_uses_cas_stops_leaf_and_protects_required_dependency(tmp_path) -> None:
    manager, store, host, directory = build_manager(tmp_path)
    await manager.initialize()
    await manager.reconcile()
    result = await manager.set_enabled(
        service_id="agent",
        enabled=False,
        expected_revision=1,
        request_id="req-disable-agent",
    )
    assert result.state.enabled is False
    assert host.calls[-1] == ("stop", "agent")
    with pytest.raises(Conflict, match="required"):
        await manager.set_enabled(
            service_id="kernel",
            enabled=False,
            expected_revision=1,
            request_id="req-disable-kernel",
        )
    with pytest.raises(Conflict, match="already enabled"):
        await manager.set_enabled(
            service_id="kernel",
            enabled=True,
            expected_revision=1,
            request_id="req-enable-kernel",
        )
    store.close()


@pytest.mark.asyncio
async def test_restart_is_idempotent_and_does_not_change_desired_revision(tmp_path) -> None:
    manager, store, host, _ = build_manager(tmp_path)
    await manager.initialize()
    await manager.reconcile()
    result = await manager.restart(
        service_id="agent", expected_revision=1, request_id="req-restart-agent"
    )
    replay = await manager.restart(
        service_id="agent", expected_revision=1, request_id="req-restart-agent"
    )
    assert result.state.revision == 1
    assert replay.replayed is True
    assert host.calls.count(("restart", "agent")) == 1
    store.close()


@pytest.mark.asyncio
async def test_degraded_dependency_blocks_consumer_and_unpublishes_endpoints(tmp_path) -> None:
    catalog = ServiceCatalog(
        (service("agent", dependencies=("kernel",)), service("kernel", required=True))
    )
    host = FakeHostSupervisor()
    directory = InMemoryServiceDirectory()
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(
        catalog=catalog,
        store=store,
        directory=directory,
        host=host,
        readiness=FakeReadinessProbe(ready=False),
        clock=FixedClock(),
    )
    await manager.initialize()
    await manager.initialize()
    await manager.reconcile()
    assert manager.get_service("kernel").runtime_state == "degraded"
    assert manager.get_service("agent").runtime_state == "blocked"
    with pytest.raises(NotReady):
        manager.resolve("kernel", "control.http")
    store.close()


class FailingHost(FakeHostSupervisor):
    async def inspect(self, target: str) -> HostServiceState:
        raise HostOperationFailed(f"cannot inspect {target}")


@pytest.mark.asyncio
async def test_host_failure_is_observed_without_crashing_reconciliation(tmp_path) -> None:
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(
        catalog=ServiceCatalog((service("kernel", required=True),)),
        store=store,
        directory=InMemoryServiceDirectory(),
        host=FailingHost(),
        readiness=FakeReadinessProbe(),
        clock=FixedClock(),
    )
    await manager.initialize()
    await manager.reconcile()
    assert manager.get_service("kernel").runtime_state == "failed"
    store.close()


@pytest.mark.asyncio
async def test_enabled_dependents_prevent_disabling_dependency(tmp_path) -> None:
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(
        catalog=ServiceCatalog(
            (service("agent", dependencies=("kernel",)), service("kernel"))
        ),
        store=store,
        directory=InMemoryServiceDirectory(),
        host=FakeHostSupervisor(),
        readiness=FakeReadinessProbe(),
        clock=FixedClock(),
    )
    await manager.initialize()
    with pytest.raises(Conflict, match="dependents"):
        await manager.set_enabled(
            service_id="kernel",
            enabled=False,
            expected_revision=1,
            request_id="req-disable-kernel",
        )
    store.close()
