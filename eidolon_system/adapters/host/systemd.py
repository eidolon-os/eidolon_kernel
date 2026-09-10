"""Linux systemd execution adapter; systemd owns PIDs and restart mechanics.

Reading and mutating are split by privilege, not by taste. `systemctl show` is
readable by anyone, so `inspect` runs it directly. Starting a unit is not: the
manager runs as `eidolon` under `NoNewPrivileges`, so the three mutating verbs
go through an injected mutator, which in the product is the root applier on a
socket. The `HostServiceSupervisor` port is unchanged by any of it — this is a
host mechanism, and it stops here.
"""

from __future__ import annotations

from typing import Protocol

from eidolon_system.adapters.host.runner import SubprocessCommandRunner
from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.domain.model import HostServiceState


class UnitMutator(Protocol):
    """How this Host lets a unit verb actually run."""

    @property
    def mechanism(self) -> str: ...

    async def apply(self, verb: str, unit: str) -> None: ...


class SystemctlUnitMutator:
    """Run `systemctl <verb> <unit>` in this process's own privilege.

    Correct only where eidolond is root — a source run, a container, an operator
    reproducing a failure by hand. The product image injects the applier instead.
    """

    mechanism = "systemctl"

    def __init__(self, *, runner=None, systemctl: str = "systemctl") -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.systemctl = systemctl

    async def apply(self, verb: str, unit: str) -> None:
        result = await self.runner.run(self.systemctl, verb, unit)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HostOperationFailed(f"systemd {verb} failed for {unit}: {detail}")


class SystemdHostSupervisor:
    driver_name = "systemd"

    def __init__(
        self,
        *,
        runner=None,
        systemctl: str = "systemctl",
        mutator: UnitMutator | None = None,
    ) -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.systemctl = systemctl
        self.mutator = mutator or SystemctlUnitMutator(
            runner=self.runner, systemctl=self.systemctl
        )

    async def inspect(self, target: str) -> HostServiceState:
        result = await self.runner.run(
            self.systemctl,
            "show",
            target,
            "--property=ActiveState",
            "--property=SubState",
            "--property=InvocationID",
            "--no-pager",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HostOperationFailed(f"systemd inspect failed for {target}: {detail}")
        values = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
        active = values.get("ActiveState", "unknown")
        sub = values.get("SubState", "unknown")
        return HostServiceState(
            active=active == "active",
            state=f"{active}/{sub}",
            instance_id=values.get("InvocationID") or None,
        )

    async def start(self, target: str) -> None:
        await self.mutator.apply("start", target)

    async def stop(self, target: str) -> None:
        await self.mutator.apply("stop", target)

    async def restart(self, target: str) -> None:
        await self.mutator.apply("restart", target)
