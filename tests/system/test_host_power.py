import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from eidolon_system.adapters.host.power import LinuxHostPower
from eidolon_system.adapters.host.runner import CommandResult
from eidolon_system.domain.errors import Conflict, HostOperationFailed, PowerOffRejected
from eidolon_system.interfaces.http.router import create_system_router
from eidolon_system.unitapplier.authorize import Denied, Peer, authorize
from eidolon_system.unitapplier.protocol import Request
from eidolon_system.unitapplier.server import UnitApplier


class Runner:
    def __init__(self, code=0, error=None):
        self.calls = []
        self.code = code
        self.error = error

    async def run(self, *args):
        self.calls.append(args)
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return CommandResult(self.code, "", "")


def power(runner, **kwargs):
    return LinuxHostPower(runner=runner, platform="linux", executable=lambda _: True, **kwargs)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_unsupported_os_never_runs_any_command(platform):
    runner = Runner()
    control = LinuxHostPower(runner=runner, platform=platform)
    assert not (await control.read()).can_power_off
    with pytest.raises(HostOperationFailed):
        await control.power_off(request_id="one")
    assert runner.calls == []


async def test_missing_command_is_unavailable():
    control = LinuxHostPower(runner=Runner(), platform="linux", executable=lambda _: False)
    assert not (await control.read()).can_power_off


@pytest.mark.parametrize(
    "uid,argv",
    [(1000, ("/usr/bin/sudo", "-n", "--", "/usr/sbin/poweroff")), (0, ("/usr/sbin/poweroff",))],
)
async def test_fixed_command_and_concurrent_duplicate_are_executed_once(uid, argv):
    runner = Runner()
    control = power(runner, euid=uid)
    assert (await control.read()).can_power_off
    assert not runner.calls
    first, second = await asyncio.gather(
        control.power_off(request_id="one"), control.power_off(request_id="one")
    )
    assert first == second
    assert first.status == "accepted"
    assert runner.calls == [argv]
    assert not (await control.read()).can_power_off
    with pytest.raises(Conflict):
        await control.power_off(request_id="two")
    assert len(runner.calls) == 1


async def test_permission_denied_is_not_accepted():
    runner = Runner(code=1)
    control = power(runner, euid=1000)
    with pytest.raises(PowerOffRejected):
        await control.power_off(request_id="one")
    assert (await control.read()).can_power_off


async def test_timeout_does_not_allow_another_execution():
    runner = Runner(error=HostOperationFailed("timed out"))
    control = power(runner, euid=1000)
    for request_id in ("one", "one", "two"):
        with pytest.raises(HostOperationFailed):
            await control.power_off(request_id=request_id)
    assert not (await control.read()).can_power_off
    assert len(runner.calls) == 1


async def test_production_uses_the_existing_privileged_socket():
    calls = []

    class Applier:
        async def apply(self, verb, unit):
            calls.append((verb, unit))

    runner = Runner()
    control = power(runner, euid=1000, applier=Applier())
    assert (await control.read()).can_power_off
    await control.power_off(request_id="one")
    assert calls == [("power-status", "@host"), ("power-status", "@host"), ("poweroff", "@host")]
    assert runner.calls == []


async def test_old_privileged_service_disables_power():
    class OldApplier:
        async def apply(self, verb, unit):
            raise HostOperationFailed("unsupported verb")

    control = power(Runner(), euid=1000, applier=OldApplier())
    assert not (await control.read()).can_power_off


def test_power_requires_manager_cgroup_and_exact_target():
    manager = Peer(pid=20, uid=1000, unit="eidolond.service", pinned=True)
    for verb in ("power-status", "poweroff"):
        authorize(Request(verb, "@host"), manager, allowed_units=frozenset())
        with pytest.raises(Denied):
            authorize(
                Request(verb, "other.service"), manager, allowed_units=frozenset({"other.service"})
            )
        with pytest.raises(Denied):
            authorize(
                Request(verb, "@host"),
                Peer(21, 1000, "eidolon-agent.service", True),
                allowed_units=frozenset(),
            )


def test_privileged_power_probe_is_read_only_and_execution_is_fixed(monkeypatch):
    calls = []
    monkeypatch.setattr("eidolon_system.unitapplier.server.Path.is_file", lambda p: True)
    monkeypatch.setattr("eidolon_system.unitapplier.server.os.access", lambda *a: True)

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("eidolon_system.unitapplier.server.subprocess.run", run)
    applier = UnitApplier(allowed_units=frozenset())
    assert applier._power(Request("power-status", "@host"), 20).ok
    assert calls == []
    assert applier._power(Request("poweroff", "@host"), 20).ok
    assert not applier._power(Request("poweroff", "@host"), 20).ok
    assert calls == [["/usr/sbin/poweroff"]]


async def test_system_endpoint_forbids_arbitrary_command_and_returns_accepted():
    runner = Runner()
    app = FastAPI()
    app.include_router(
        create_system_router(
            manager=None, contracts=None, vitals=None, monitor=None, power=power(runner, euid=1000)
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://system"
    ) as client:
        for body in ({}, {"request_id": "a", "command": "rm"}, {"request_id": "a;whoami"}):
            assert (await client.post("/api/system/v1/poweroff", json=body)).status_code == 422
        assert not runner.calls
        result = await client.post("/api/system/v1/poweroff", json={"request_id": "a"})
        assert result.status_code == 202
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["request_id"] == "a"
