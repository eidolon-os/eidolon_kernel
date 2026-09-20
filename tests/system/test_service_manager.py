from __future__ import annotations

import pytest

from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.domain.errors import Conflict, HostOperationFailed, NotReady
from eidolon_system.domain.model import (
    EXTERNAL_HOST_TARGET,
    HostServiceState,
    ServiceCatalog,
)
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


def build_manager_with_external_dependency(tmp_path, *, ready: bool = True):
    """A Host where the thing everything waits on is not ours to start."""

    nats = service("nats", required=True, host_targets={"fake": EXTERNAL_HOST_TARGET})
    catalog = ServiceCatalog((nats, service("agent", dependencies=("nats",))))
    host = FakeHostSupervisor()
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(
        catalog=catalog,
        store=store,
        directory=InMemoryServiceDirectory(),
        host=host,
        readiness=FakeReadinessProbe(ready=ready),
        clock=FixedClock(),
    )
    return manager, store, host


@pytest.mark.asyncio
async def test_external_service_is_observed_never_started_and_still_gates(tmp_path) -> None:
    manager, store, host = build_manager_with_external_dependency(tmp_path)
    await manager.initialize()
    await manager.reconcile()

    assert host.calls == [("start", "agent")]
    assert manager.get_service("nats").runtime_state == "ready"
    assert manager.get_service("agent").runtime_state == "ready"
    store.close()


@pytest.mark.asyncio
async def test_unhealthy_external_service_blocks_its_dependents(tmp_path) -> None:
    manager, store, host = build_manager_with_external_dependency(tmp_path, ready=False)
    await manager.initialize()
    await manager.reconcile()

    # Nothing was started: an external dependency that is not answering is the
    # one case where eidolond has nothing to do but say so.
    assert host.calls == []
    assert manager.get_service("nats").runtime_state == "degraded"
    assert manager.get_service("agent").runtime_state == "blocked"
    store.close()


@pytest.mark.asyncio
async def test_external_service_cannot_be_restarted_here(tmp_path) -> None:
    manager, store, _host = build_manager_with_external_dependency(tmp_path)
    await manager.initialize()
    await manager.reconcile()

    with pytest.raises(Conflict, match="external system service"):
        await manager.restart(service_id="nats", expected_revision=1, request_id="req-restart-nats")
    store.close()


@pytest.mark.asyncio
async def test_disabling_an_external_service_reports_the_gap_rather_than_a_stop(tmp_path) -> None:
    nats = service("nats", host_targets={"fake": EXTERNAL_HOST_TARGET})
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    host = FakeHostSupervisor()
    manager = ServiceManager(
        catalog=ServiceCatalog((nats,)),
        store=store,
        directory=InMemoryServiceDirectory(),
        host=host,
        readiness=FakeReadinessProbe(),
        clock=FixedClock(),
    )
    await manager.initialize()
    await manager.set_enabled(
        service_id="nats", enabled=False, expected_revision=1, request_id="req-disable-nats"
    )

    status = manager.get_service("nats")
    assert host.calls == []
    assert status.runtime_state == "degraded"
    assert "disable it where it actually runs" in status.detail
    store.close()


async def test_product_device_authority_remains_readable_during_media_outage(tmp_path):
    from pathlib import Path

    from eidolon_system.adapters.manifest.yaml_file import YamlServiceManifest

    catalog = YamlServiceManifest(Path("config/system-services.yaml")).load()
    host = FakeHostSupervisor()
    host.driver_name = "systemd"
    store = SqliteSystemStateStore(tmp_path / "system.sqlite3")
    manager = ServiceManager(catalog=catalog, store=store,
        directory=InMemoryServiceDirectory(), host=host,
        readiness=FakeReadinessProbe(), clock=FixedClock())
    try:
        await manager.initialize()
        await manager.reconcile()
        # No network observation: LiveKit must fail closed, while Hub reads work.
        assert manager.get_service("livekit").runtime_state == "degraded"
        assert manager.get_service("channel-provider").runtime_state == "blocked"
        assert manager.get_service("hub").runtime_state == "ready"
        assert manager.resolve("hub", "device-authority.http").endpoint_id == "device-authority.http"
    finally:
        store.close()
