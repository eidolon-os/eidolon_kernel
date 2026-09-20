from __future__ import annotations

import pytest

from eidolon_system.adapters.host.runner import CommandResult
from eidolon_system.adapters.host.supervisord import SupervisordHostSupervisor
from eidolon_system.adapters.host.systemd import SystemdHostSupervisor
from eidolon_system.domain.errors import HostOperationFailed


class FakeRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.commands: list[tuple[str, ...]] = []

    async def run(self, *command: str) -> CommandResult:
        self.commands.append(command)
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_systemd_adapter_parses_state_and_never_uses_a_shell() -> None:
    runner = FakeRunner(
        [
            CommandResult(0, "ActiveState=active\nSubState=running\nInvocationID=run-a\n", ""),
            CommandResult(0, "", ""),
        ]
    )
    host = SystemdHostSupervisor(runner=runner, systemctl="/bin/systemctl")
    state = await host.inspect("eidolon-kernel.service")
    await host.restart("eidolon-kernel.service")
    assert state.active is True
    assert state.state == "active/running"
    assert state.instance_id == "run-a"
    assert runner.commands == [
        (
            "/bin/systemctl",
            "show",
            "eidolon-kernel.service",
            "--property=ActiveState",
            "--property=SubState",
            "--property=InvocationID",
            "--property=Job",
            "--no-pager",
        ),
        ("/bin/systemctl", "--no-block", "restart", "--", "eidolon-kernel.service"),
    ]


@pytest.mark.asyncio
async def test_systemd_adapter_surfaces_inspect_and_mutation_failures() -> None:
    runner = FakeRunner(
        [
            CommandResult(1, "", "unit missing"),
            CommandResult(0, "", ""),
            CommandResult(0, "", ""),
            CommandResult(1, "", "permission denied"),
        ]
    )
    host = SystemdHostSupervisor(runner=runner)
    with pytest.raises(HostOperationFailed, match="unit missing"):
        await host.inspect("missing.service")
    await host.start("service.service")
    await host.stop("service.service")
    with pytest.raises(HostOperationFailed, match="permission denied"):
        await host.restart("service.service")


@pytest.mark.asyncio
async def test_supervisord_adapter_parses_status_and_surfaces_failure(tmp_path) -> None:
    config = tmp_path / "supervisord.conf"
    config.write_text("[supervisord]\n", encoding="utf-8")
    runner = FakeRunner(
        [
            CommandResult(0, "hub:hub-api RUNNING pid 42, uptime 0:00:04\n", ""),
            CommandResult(1, "", "refused"),
        ]
    )
    host = SupervisordHostSupervisor(
        runner=runner,
        supervisorctl="supervisorctl",
        config_path=config,
    )
    assert (await host.inspect("hub:hub-api")).active is True
    with pytest.raises(HostOperationFailed, match="refused"):
        await host.stop("hub:hub-api")


@pytest.mark.asyncio
async def test_supervisord_adapter_supports_inactive_start_and_restart(tmp_path) -> None:
    config = tmp_path / "supervisord.conf"
    config.write_text("[supervisord]\n", encoding="utf-8")
    # supervisord prints a single-program group under the bare program name when
    # the group is named after it, which is how the manifest's `agent:agent` and
    # `channel-provider:channel-provider` targets are declared. This fixture
    # used to invent `agent:agent STOPPED ...`, a line real supervisorctl never
    # emits, so an inspector that required the name to be echoed back passed
    # here and reported both services permanently failed on every Mac Host —
    # taking Hub and Channel down with them through the dependency graph.
    runner = FakeRunner(
        [
            CommandResult(3, "agent                            STOPPED   Not started\n", ""),
            CommandResult(0, "started\n", ""),
            CommandResult(0, "restarted\n", ""),
        ]
    )
    host = SupervisordHostSupervisor(runner=runner, config_path=config)
    assert (await host.inspect("agent:agent")).active is False
    await host.start("agent:agent")
    await host.restart("agent:agent")


@pytest.mark.asyncio
async def test_supervisord_adapter_still_rejects_a_line_about_another_program(
    tmp_path,
) -> None:
    """Tolerating the bare name must not mean tolerating any name.

    A group-qualified target is satisfied by its own bare program name only
    when the group is named after the program. `hub:hub-api` reported as
    `data-api` is a different service, and reading it as an observation of Hub
    is worse than admitting the output was not understood.
    """

    config = tmp_path / "supervisord.conf"
    config.write_text("[supervisord]\n", encoding="utf-8")
    runner = FakeRunner(
        [CommandResult(0, "data-api                         RUNNING   pid 5, uptime 0:00:10\n", "")]
    )
    host = SupervisordHostSupervisor(runner=runner, config_path=config)

    with pytest.raises(HostOperationFailed, match="inspect failed"):
        await host.inspect("hub:hub-api")


@pytest.mark.asyncio
async def test_supervisord_adapter_rejects_non_status_error_output(tmp_path) -> None:
    config = tmp_path / "supervisord.conf"
    config.write_text("[supervisord]\n", encoding="utf-8")
    runner = FakeRunner([CommandResult(3, "missing ERROR (no such process)\n", "")])
    host = SupervisordHostSupervisor(runner=runner, config_path=config)

    with pytest.raises(HostOperationFailed, match="no such process"):
        await host.inspect("missing")


async def test_supervisord_instance_uses_pid_and_creation_time(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from eidolon_system.adapters.host import supervisord
    created = [100.0]
    monkeypatch.setattr(supervisord.psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: created[0]))
    host = SupervisordHostSupervisor(config_path=tmp_path / "supervisor.conf", runner=FakeRunner([
        CommandResult(0, "media RUNNING pid 1234, uptime 0:00:10", ""),
        CommandResult(0, "media RUNNING pid 1234, uptime 0:00:10", ""),
    ]))
    first = await host.inspect("media")
    created[0] = 200.0
    assert first.instance_id != (await host.inspect("media")).instance_id


@pytest.mark.parametrize("active,sub,job,transitioning", [
    ("active", "running", "42", True),
    ("active", "running", "42 /org/freedesktop/systemd1/job/42", True),
    ("active", "running", "0", False),
    ("active", "running", "", False),
    ("activating", "start", "0", True),
    ("deactivating", "stop-sigterm", "0", True),
    ("reloading", "reload", "0", True),
    ("activating", "auto-restart", "0", True),
    ("inactive", "dead", "0", False),
    ("failed", "failed", "0", False),
])
async def test_systemd_reports_queued_and_running_jobs(active, sub, job, transitioning):
    host = SystemdHostSupervisor(runner=FakeRunner([
        CommandResult(0, f"ActiveState={active}\nSubState={sub}\nJob={job}\n", ""),
    ]))
    assert (await host.inspect("media.service")).transitioning == transitioning


@pytest.mark.parametrize("state", ["STARTING", "STOPPING", "BACKOFF"])
async def test_supervisor_owns_transient_states(tmp_path, state):
    host = SupervisordHostSupervisor(config_path=tmp_path / "supervisor.conf", runner=FakeRunner([
        CommandResult(3, f"media {state}", ""),
    ]))
    assert (await host.inspect("media")).transitioning


@pytest.mark.parametrize("output", ["", "ActiveState=unknown\nSubState=unknown\n"])
async def test_unrecognized_systemd_state_is_not_treated_as_stopped(output):
    host = SystemdHostSupervisor(runner=FakeRunner([CommandResult(0, output, "")]))
    with pytest.raises(HostOperationFailed, match="unrecognized state"):
        await host.inspect("media.service")
