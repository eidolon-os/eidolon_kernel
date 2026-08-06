"""Shell-free asynchronous command execution for host adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from eidolon_system.domain.errors import HostOperationFailed


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessCommandRunner:
    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def run(self, *command: str) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise HostOperationFailed(f"host command failed: {command[0]}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise HostOperationFailed(
                f"host command timed out: {command[0]} after {self.timeout_seconds}s"
            ) from exc
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
