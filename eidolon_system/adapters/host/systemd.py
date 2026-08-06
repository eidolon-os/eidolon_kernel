"""Linux systemd execution adapter; systemd owns PIDs and restart mechanics."""

from __future__ import annotations

from eidolon_system.adapters.host.runner import SubprocessCommandRunner
from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.domain.model import HostServiceState


class SystemdHostSupervisor:
    driver_name = "systemd"

    def __init__(self, *, runner=None, systemctl: str = "systemctl") -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.systemctl = systemctl

    async def inspect(self, target: str) -> HostServiceState:
        result = await self.runner.run(
            self.systemctl,
            "show",
            target,
            "--property=ActiveState",
            "--property=SubState",
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
        )

    async def _mutate(self, operation: str, target: str) -> None:
        result = await self.runner.run(self.systemctl, operation, target)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HostOperationFailed(f"systemd {operation} failed for {target}: {detail}")

    async def start(self, target: str) -> None:
        await self._mutate("start", target)

    async def stop(self, target: str) -> None:
        await self._mutate("stop", target)

    async def restart(self, target: str) -> None:
        await self._mutate("restart", target)
