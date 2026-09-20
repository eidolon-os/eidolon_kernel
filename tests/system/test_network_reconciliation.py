import json
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eidolon_system.adapters.directory.memory import InMemoryServiceDirectory
from eidolon_system.adapters.host import network as network_module
from eidolon_system.adapters.host.network import LocalNetworkEnvironment
from eidolon_system.adapters.persistence.sqlite import SqliteSystemStateStore
from eidolon_system.application.service_manager import ServiceManager
from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.domain.model import ServiceCatalog
from tests.system.support import FakeHostSupervisor, FakeReadinessProbe, FixedClock
from tests.system.test_domain import service


class Host(FakeHostSupervisor):
    def __init__(self):
        super().__init__()
        self.generation = 0
        self.fail = False

    async def inspect(self, target):
        return replace(await super().inspect(target), instance_id=str(self.generation))

    async def start(self, target):
        self.generation += 1
        await super().start(target)

    async def restart(self, target):
        if self.fail:
            self.calls.append(("failed-restart", target))
            raise HostOperationFailed("restart refused")
        self.generation += 1
        await super().restart(target)


@pytest.fixture
def setup(tmp_path):
    inputs = SimpleNamespace(value="lan-a", now=0.0)
    network = LocalNetworkEnvironment(
        tmp_path / "network.json", read=lambda: inputs.value, monotonic=lambda: inputs.now
    )
    store = SqliteSystemStateStore(tmp_path / "system.db")
    host = Host()

    def manager():
        return ServiceManager(
            catalog=ServiceCatalog(
                (replace(service("media"), restart_on_network_change=True), service("nats"))
            ),
            store=store,
            host=host,
            directory=InMemoryServiceDirectory(),
            readiness=FakeReadinessProbe(),
            clock=FixedClock(),
            network=network,
            monotonic=lambda: inputs.now,
        )

    yield inputs, network, host, manager
    store.close()


async def ready(inputs, manager):
    await manager.initialize()
    await manager.reconcile()
    inputs.now += 10
    await manager.reconcile()
    assert manager.get_service("media").runtime_state == "ready"


async def test_change_refreshes_only_declared_service_once(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    assert manager.get_service("media").endpoints == ()
    assert manager.get_service("nats").runtime_state == "ready"
    inputs.now += 10
    await manager.reconcile()
    await manager.reconcile()
    assert host.calls == [("start", "nats"), ("start", "media"), ("restart", "media")]
    assert manager.store.get("media").revision == 1
    assert json.loads(network.applied("media"))[0] == "lan-b"


async def test_outage_and_flapping_do_not_restart(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    for value in (None, "lan-b", None, "lan-a"):
        inputs.value = value
        inputs.now += 2
        await manager.reconcile()
        assert manager.get_service("media").runtime_state == "degraded"
    inputs.now += 10
    await manager.reconcile()
    assert host.calls == [("start", "nats"), ("start", "media")]


async def test_restart_failure_is_not_acknowledged_and_has_backoff(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    previous = network.applied("media")
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    host.fail = True
    await manager.reconcile()
    assert manager.get_service("media").runtime_state == "failed"
    assert network.applied("media") == previous
    for _ in range(4):
        inputs.now += 5
        await manager.reconcile()
    assert host.calls.count(("failed-restart", "media")) == 1
    host.fail = False
    inputs.now += 10
    await manager.reconcile()
    assert manager.get_service("media").runtime_state == "ready"


async def test_daemon_restart_keeps_observation_but_external_process_change_invalidates(setup):
    inputs, network, host, factory = setup
    await ready(inputs, factory())
    replacement = factory()
    replacement.network = LocalNetworkEnvironment(
        network.path, read=lambda: inputs.value, monotonic=lambda: inputs.now
    )
    await ready(inputs, replacement)
    assert ("restart", "media") not in host.calls
    host.generation += 1  # Supervisor restarted it while eidolond was absent.
    await replacement.reconcile()
    assert host.calls.count(("restart", "media")) == 1
    await replacement.reconcile()
    assert host.calls.count(("restart", "media")) == 1


async def test_unobserved_existing_process_is_adopted_by_refresh(setup):
    inputs, network, host, factory = setup
    host.active["media"] = True
    await ready(inputs, factory())
    assert host.calls.count(("restart", "media")) == 1


async def test_move_during_restart_cannot_publish_ready(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    previous = network.applied("media")
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    original = host.restart

    async def restart(target):
        await original(target)
        inputs.value = "lan-c"

    host.restart = restart
    await manager.reconcile()
    assert network.applied("media") == previous
    assert manager.get_service("media").endpoints == ()
    host.restart = original
    inputs.now += 30
    await manager.reconcile()
    assert json.loads(network.applied("media"))[0] == "lan-c"


async def test_manual_restart_does_not_trigger_second_refresh(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    await manager.restart(service_id="media", expected_revision=1, request_id="manual")
    await manager.reconcile()
    assert host.calls.count(("restart", "media")) == 1


async def test_missing_network_adapter_fails_closed(setup):
    inputs, network, host, factory = setup
    manager = factory()
    manager.network = None
    await manager.initialize()
    await manager.reconcile()
    assert manager.get_service("media").runtime_state == "degraded"
    assert host.calls == [("start", "nats")]


async def test_corrupt_observation_is_disposable_and_offline_never_settles(tmp_path):
    path = tmp_path / "network.json"
    path.write_text("broken")
    env = LocalNetworkEnvironment(path, read=lambda: None, settle_seconds=0)
    assert env.applied("media") is None
    assert await env.snapshot() is None
    env.record("media", "stamp")
    assert LocalNetworkEnvironment(path).applied("media") == "stamp"
    assert path.stat().st_mode & 0o777 == 0o600


def test_snapshot_enumerates_nics_and_ignores_order_linklocal_and_loopback(monkeypatch):
    def addr(value):
        return SimpleNamespace(family=socket.AF_INET, address=value, netmask="255.255.255.0")

    values = {
        "wifi": [addr("192.168.1.32")],
        "eth": [addr("10.1.1.2")],
        "lo": [addr("127.0.0.1")],
        "link": [addr("169.254.1.1")],
    }
    monkeypatch.setattr(network_module.psutil, "net_if_addrs", lambda: values)
    monkeypatch.setattr(
        network_module.psutil,
        "net_if_stats",
        lambda: {name: SimpleNamespace(isup=True) for name in values},
    )

    class Route:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect(self, address):
            pass

        def getsockname(self):
            return ("192.168.1.32", 1234)

    monkeypatch.setattr(network_module.socket, "socket", lambda *args: Route())
    first = network_module.network_fingerprint()
    values = dict(reversed(list(values.items())))
    assert network_module.network_fingerprint() == first
    values["link"] = [addr("169.254.2.3")]
    assert network_module.network_fingerprint() == first
    values["eth"] = [addr("10.2.2.2")]
    assert network_module.network_fingerprint() != first
    values = {"lo": [addr("127.0.0.1")]}
    assert network_module.network_fingerprint() is None


async def test_external_service_is_observed_but_never_refreshed(setup):
    inputs, network, host, factory = setup
    manager = factory()
    manager.catalog = ServiceCatalog(
        (
            replace(
                service("media"), restart_on_network_change=True, host_targets={"fake": "external"}
            ),
        )
    )
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    await manager.reconcile()
    assert host.calls == []


async def test_cache_write_failure_keeps_service_unpublished(setup, monkeypatch):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10

    def fail(*args):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(network, "record", fail)
    await manager.reconcile()
    assert manager.get_service("media").runtime_state == "failed"
    assert manager.get_service("media").endpoints == ()
    await manager.reconcile()
    assert host.calls.count(("restart", "media")) == 1


async def test_network_fact_is_explicit_and_validates_in_service_contract(setup):
    from eidolon_system.contracts.mappers import status_to_wire
    from eidolon_system.contracts.registry import SystemContractRegistry

    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    document = status_to_wire(manager.get_service("media")).model_dump(mode="json")
    SystemContractRegistry().validate("system/service-status.schema.json", document)
    assert document["network_current"] is True
    assert manager.get_service("nats").network_current is None
    inputs.value = None
    await manager.reconcile()
    assert manager.get_service("media").network_current is None


async def test_timed_out_restart_observes_late_completion_without_restarting_again(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    previous = network.applied("media")

    async def pending_restart(target):
        host.calls.append(("restart", target))
        raise HostOperationFailed("applier response timed out; systemd job continues")

    host.restart = pending_restart
    await manager.reconcile()
    assert network.applied("media") == previous
    assert manager.get_service("media").runtime_state == "failed"
    # Completion happens after the caller's timeout, but before the retry budget.
    host.generation += 1
    inputs.now += 5
    await manager.reconcile()
    assert manager.get_service("media").runtime_state == "ready"
    assert json.loads(network.applied("media")) == ["lan-b", str(host.generation)]
    for _ in range(12):
        inputs.now += 5
        await manager.reconcile()
    assert host.calls.count(("restart", "media")) == 1


async def test_late_restart_completion_for_previous_network_is_not_adopted(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    previous = network.applied("media")
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    host.fail = True
    await manager.reconcile()
    host.generation += 1
    inputs.value = "lan-c"
    await manager.reconcile()
    inputs.now += 10
    await manager.reconcile()
    assert network.applied("media") == previous
    assert manager.get_service("media").endpoints == ()
    host.fail = False
    inputs.now += 30
    await manager.reconcile()
    assert json.loads(network.applied("media"))[0] == "lan-c"


async def test_network_job_owns_transition_across_daemon_restart_and_another_network_move(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    original_inspect, original_restart = host.inspect, host.restart
    busy = [False]

    async def inspect(target):
        return replace(await original_inspect(target), transitioning=busy[0] and target == "media")

    async def restart(target):
        assert not busy[0]
        host.calls.append(("restart", target))
        busy[0] = True
        raise HostOperationFailed("reply lost")

    host.inspect, host.restart = inspect, restart
    await manager.reconcile()
    replacement = factory()
    await replacement.initialize()
    inputs.value = "lan-c"
    for _ in range(8):
        inputs.now += 30
        await replacement.reconcile()
    assert host.calls.count(("restart", "media")) == 1
    assert json.loads(network.applied("media"))[0] == "lan-a"
    # The old-network job finishes; one refresh against the new network is
    # required. The previous completion must not acknowledge the newer input.
    busy[0] = False
    host.generation += 1
    host.restart = original_restart
    await replacement.reconcile()
    assert json.loads(network.applied("media"))[0] == "lan-c"
    assert host.calls.count(("restart", "media")) == 2
    await replacement.reconcile()
    assert host.calls.count(("restart", "media")) == 2


async def test_retry_captures_latest_stable_network_before_dispatch(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    host.fail = True
    await manager.reconcile()
    inputs.value = "lan-c"
    await manager.reconcile()
    inputs.now += 30
    host.fail = False
    await manager.reconcile()
    assert json.loads(network.applied("media"))[0] == "lan-c"
    # Only one successful restart, not one with stale metadata then another.
    assert host.calls.count(("restart", "media")) == 1
    await manager.reconcile()
    assert host.calls.count(("restart", "media")) == 1


async def test_network_move_during_health_probe_unpublishes_without_acknowledging_it(setup):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    previous = network.applied("media")
    async def check(url):
        inputs.value = "lan-b"
        return True
    manager.readiness.check = check
    await manager.reconcile()
    assert manager.get_service("media").endpoints == ()
    assert network.applied("media") == previous


async def test_disable_after_completed_restart_does_not_wait_for_network_cache_write(setup, monkeypatch):
    inputs, network, host, factory = setup
    manager = factory()
    await ready(inputs, manager)
    inputs.value = "lan-b"
    await manager.reconcile()
    inputs.now += 10
    async def lost_reply(target):
        host.calls.append(("restart", target))
        raise HostOperationFailed("reply lost")
    host.restart = lost_reply
    await manager.reconcile()
    host.generation += 1  # replacement completed after the lost reply
    def failed_cache(*args):
        raise OSError("cache is unwritable")
    monkeypatch.setattr(network, "record", failed_cache)
    await manager.set_enabled(service_id="media", enabled=False, expected_revision=1, request_id="off")
    assert host.calls[-1] == ("stop", "media")
    assert manager.get_service("media").runtime_state == "inactive"
