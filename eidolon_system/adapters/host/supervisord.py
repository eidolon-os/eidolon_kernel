"""supervisord adapter for the existing macOS/development stack."""

from __future__ import annotations

from pathlib import Path

from eidolon_system.adapters.host.runner import SubprocessCommandRunner
from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.domain.model import HostServiceState


class SupervisordHostSupervisor:
    driver_name = "supervisord"
    _STATUS_VALUES = {
        "backoff",
        "exited",
        "fatal",
        "running",
        "starting",
        "stopped",
        "stopping",
        "unknown",
    }

    def __init__(
        self,
        *,
        config_path: Path,
        runner=None,
        supervisorctl: str = "supervisorctl",
    ) -> None:
        self.config_path = config_path.resolve()
        self.runner = runner or SubprocessCommandRunner()
        self.supervisorctl = supervisorctl

    async def _run(self, *arguments: str):
        return await self.runner.run(self.supervisorctl, "-c", str(self.config_path), *arguments)

    async def inspect(self, target: str) -> HostServiceState:
        result = await self._run("status", target)
        output = result.stdout.strip()
        fields = output.split()
        state = fields[1].lower() if len(fields) > 1 else ""
        # supervisorctl exits 3 for a valid non-running process. Treat only a
        # target-matched, documented process state as an observation; errors
        # such as "no such process" remain failures even though they also use
        # a non-zero exit status.
        observed = len(fields) > 1 and fields[0] == target and state in self._STATUS_VALUES
        if not observed:
            detail = result.stderr.strip() or output
            raise HostOperationFailed(f"supervisord inspect failed for {target}: {detail}")
        return HostServiceState(
            active=state == "running",
            state=state,
            detail=output or None,
        )

    async def _mutate(self, operation: str, target: str) -> None:
        result = await self._run(operation, target)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HostOperationFailed(f"supervisord {operation} failed for {target}: {detail}")

    async def start(self, target: str) -> None:
        await self._mutate("start", target)

    async def stop(self, target: str) -> None:
        await self._mutate("stop", target)

    async def restart(self, target: str) -> None:
        await self._mutate("restart", target)
