"""Linux poweroff with fixed argv, no shell, and no automatic retry.

The command is awaited: only a successful exit means accepted. The network may
close first, in which case callers must report an unknown outcome. A command
acceptance does not establish that hardware has finished powering down.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from eidolon_sdk.system.v1 import HostPowerOffAccepted, HostPowerStatusWire

from eidolon_system.domain.errors import Conflict, HostOperationFailed, PowerOffRejected

from .runner import SubprocessCommandRunner


class LinuxHostPower:
    def __init__(self, *, runner=None, platform=None, euid=None, executable=None, applier=None):
        self._applier = applier
        self._runner = runner or SubprocessCommandRunner(timeout_seconds=8)
        self._platform = sys.platform if platform is None else platform
        self._euid = os.geteuid() if euid is None else euid
        self._executable = executable or (lambda p: Path(p).is_file() and os.access(p, os.X_OK))
        self._lock = asyncio.Lock()
        self._accepted: HostPowerOffAccepted | None = None
        # A timeout can occur after the command has taken effect. Never run a
        # second command in this daemon after an indeterminate execution.
        self._uncertain = False

    def _command(self) -> tuple[str, ...] | None:
        if self._platform != "linux":
            return None
        poweroff = next(
            (p for p in ("/usr/sbin/poweroff", "/sbin/poweroff") if self._executable(p)), None
        )
        if poweroff is None:
            return None
        if self._euid == 0:
            return (poweroff,)
        if not self._executable("/usr/bin/sudo"):
            return None
        return ("/usr/bin/sudo", "-n", "--", poweroff)

    async def read(self) -> HostPowerStatusWire:
        if self._accepted is not None:
            return HostPowerStatusWire(can_power_off=False, unavailable_reason="关机指令已接受")
        if self._uncertain:
            return HostPowerStatusWire(
                can_power_off=False, unavailable_reason="关机结果未确认，请检查主机状态"
            )
        if self._platform != "linux":
            return HostPowerStatusWire(
                can_power_off=False, unavailable_reason="这台主机的操作系统暂不支持远程关机"
            )
        if self._applier is not None:
            try:
                await self._applier.apply("power-status", "@host")
            except HostOperationFailed:
                return HostPowerStatusWire(
                    can_power_off=False,
                    unavailable_reason="主机特权服务未提供关机能力，请检查服务或更新主机软件",
                )
            return HostPowerStatusWire(can_power_off=True)
        if self._command() is None:
            return HostPowerStatusWire(
                can_power_off=False, unavailable_reason="主机未安装关机命令或 sudo"
            )
        return HostPowerStatusWire(can_power_off=True)

    async def power_off(self, *, request_id: str) -> HostPowerOffAccepted:
        async with self._lock:
            if self._accepted is not None:
                if self._accepted.request_id == request_id:
                    return self._accepted
                raise Conflict("关机指令已接受，请勿重复关机")
            capability = await self.read()
            if not capability.can_power_off:
                raise HostOperationFailed(capability.unavailable_reason)
            self._uncertain = True
            # Do not clear uncertainty on timeout/cancellation: the OS might
            # have accepted the command even though its result was lost.
            if self._applier is not None:
                await self._applier.apply("poweroff", "@host")
            else:
                command = self._command()
                assert command is not None
                result = await self._runner.run(*command)
                if result.returncode != 0:
                    self._uncertain = False
                    raise PowerOffRejected("主机拒绝关机，请检查关机命令和免密 sudo 权限")
            self._accepted = HostPowerOffAccepted(request_id=request_id)
            return self._accepted
