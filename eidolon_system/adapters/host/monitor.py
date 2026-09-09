"""Bounded Linux current snapshot. Only previous counters survive a request.

No shell, process environment, history buffer or disk writes. Unknown command
arguments are redacted rather than relying on a denylist of secret names.
"""

from __future__ import annotations

import asyncio
import os
import platform
import pwd
import re
import shlex
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

from eidolon_sdk.system.v1.host_monitor import (
    HostMonitorWire,
    MonitorCore,
    MonitorDisk,
    MonitorMemory,
    MonitorProcess,
    MonitorProcessor,
    MonitorService,
)

from eidolon_system.adapters.host.runner import SubprocessCommandRunner

_ARM_PARTS = {
    "0xd03": "Cortex-A53",
    "0xd05": "Cortex-A55",
    "0xd08": "Cortex-A72",
    "0xd09": "Cortex-A73",
    "0xd0a": "Cortex-A75",
    "0xd0b": "Cortex-A76",
    "0xd41": "Cortex-A78",
}
_SAFE_FLAGS = {
    "--host",
    "--port",
    "--workers",
    "--log-level",
    "--bind",
    "--config",
    "--config-file",
    "--settings",
    "--app-dir",
    "--uds",
}
_LIMIT = 1024 * 1024


def read_text(path: Path) -> str:
    with path.open("rb") as stream:
        return stream.read(_LIMIT).decode("utf-8", errors="replace").strip()


def optional_text(path: Path) -> str | None:
    try:
        return read_text(path)
    except OSError:
        return None


def integer(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def process_stat(text: str) -> tuple[str, list[str]]:
    # comm can contain spaces and ')' characters.
    closing = text.rindex(")")
    return text[text.index("(") + 1 : closing], text[closing + 2 :].split()


def safe_command(argv: list[str], cwd: str | None) -> tuple[str, str | None, str | None]:
    if not argv:
        return "", None, None
    result = [argv[0]]
    source = None
    module = None
    allowed_next = False
    module_next = False
    shell = Path(argv[0]).name in {"sh", "bash", "zsh", "dash"}
    interpreter = Path(argv[0]).name.startswith(("python", "node", "ruby", "perl"))
    for index, arg in enumerate(argv[1:64], 1):
        if shell:
            result.append("[redacted]")
            break
        if module_next:
            module = arg if re.fullmatch(r"[\w.]+", arg) else None
            result.append(module or "[redacted]")
            module_next = False
        elif allowed_next:
            # Safe configuration switches still cannot carry URL credentials.
            result.append(
                arg
                if re.fullmatch(r"[\w./:@-]{1,512}", arg) and "://" not in arg and "@" not in arg
                else "[redacted]"
            )
            allowed_next = False
        elif arg == "-m" and interpreter:
            result.append(arg)
            module_next = True
        elif arg.startswith("-"):
            flag, sep, value = arg.partition("=")
            # Never return arbitrary flag names containing embedded data.
            flag = flag if re.fullmatch(r"--?[A-Za-z][A-Za-z0-9_-]{0,64}", flag) else "[argument]"
            if sep:
                safe = (
                    flag in _SAFE_FLAGS
                    and re.fullmatch(r"[\w./:-]{1,512}", value)
                    and "://" not in value
                )
                result.append(flag + "=" + (value if safe else "[redacted]"))
            else:
                result.append(flag)
                allowed_next = flag in _SAFE_FLAGS
        elif (
            interpreter
            and index == 1
            and ("/" in arg or arg.endswith((".py", ".js", ".rb", ".pl")))
        ):
            source = str(Path(cwd or "/") / arg) if cwd or arg.startswith("/") else None
            result.append(arg)
        else:
            result.append("[redacted]")
    return shlex.join(result)[:2048], source, module


class LinuxHostMonitor:
    def __init__(
        self,
        manager,
        *,
        proc: Path = Path("/proc"),
        sys: Path = Path("/sys"),
        clock=None,
        runner=None,
    ):
        self.manager = manager
        self._runner = runner or SubprocessCommandRunner(timeout_seconds=3)
        self.proc, self.sys = proc, sys
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._cpu_previous: dict[str, tuple[int, int]] = {}
        self._process_previous: dict[tuple[int, int], int] = {}
        self._group_previous: dict[str, int] = {}
        self._hz = os.sysconf("SC_CLK_TCK")
        self._pages = os.sysconf("SC_PAGE_SIZE")

    async def read(self) -> HostMonitorWire:
        async with self._lock:
            # One batched subprocess, bounded by the existing host runner.
            units, unit_error = await self._units()
            return await asyncio.to_thread(self._snapshot, units, unit_error)

    async def _units(self):
        host = self.manager.host
        if host.driver_name != "systemd":
            return {}, "当前监控采集仅支持 Linux systemd 主机"
        targets = {
            d.host_targets.get("systemd"): d.service_id
            for d in self.manager.catalog.definitions
            if d.manages("systemd")
        }
        targets.setdefault("eidolond.service", "eidolond")
        properties = "Id,ActiveState,SubState,MainPID,ControlGroup,FragmentPath,WorkingDirectory,User,ExecMainStatus,ExecMainCode"
        try:
            result = await self._runner.run(
                host.systemctl, "show", *targets, "--no-pager", "--property=" + properties
            )
            if result.returncode != 0 and not result.stdout.strip():
                return {}, "无法读取 systemd 服务信息"
        except Exception:
            return {}, "无法读取 systemd 服务信息"
        blocks = {}
        for block in result.stdout.strip().split("\n\n"):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if fields.get("Id") in targets:
                blocks[targets[fields["Id"]]] = fields
        for unit, service in targets.items():
            blocks.setdefault(service, {"Id": unit})
        return blocks, None

    def _snapshot(self, units, unit_error):
        stat = optional_text(self.proc / "stat") or ""
        current = {}
        for line in stat.splitlines():
            fields = line.split()
            if fields and re.fullmatch(r"cpu\d*", fields[0]):
                try:
                    counters = list(map(int, fields[1:9]))  # guest is already in user/nice
                    current[fields[0]] = (sum(counters), counters[3] + counters[4])
                except (ValueError, IndexError):
                    continue
        previous = self._cpu_previous
        self._cpu_previous = current
        total_delta = (
            current.get("cpu", (0, 0))[0] - previous.get("cpu", current.get("cpu", (0, 0)))[0]
        )
        if total_delta <= 0:
            total_delta = None
        info = optional_text(self.proc / "cpuinfo") or ""
        per_core = {}
        for block in info.split("\n\n"):
            fields = dict(
                (k.strip(), v.strip())
                for line in block.splitlines()
                if ":" in line
                for k, v in [line.split(":", 1)]
            )
            if "processor" in fields:
                part = fields.get("CPU part", "").lower()
                per_core[fields["processor"]] = (
                    fields.get("model name")
                    or _ARM_PARTS.get(part)
                    or ("ARM part " + part if part else None)
                )
        model = optional_text(self.sys / "firmware/devicetree/base/compatible")
        chip = next(
            (
                x.split(",", 1)[1]
                for x in (model or "").split("\x00")
                if re.fullmatch(r"(?:rockchip,rk[0-9a-z]+|brcm,bcm[0-9]+)", x)
            ),
            None,
        )
        model = chip or next((v for v in per_core.values() if v), None)
        cores = []
        for key in sorted((k for k in current if k != "cpu"), key=lambda k: int(k[3:])):
            value = self._cpu_percent(current[key], previous.get(key))
            freq = integer(
                optional_text(self.sys / f"devices/system/cpu/{key}/cpufreq/scaling_cur_freq")
            )
            cores.append(
                MonitorCore(
                    core_id=key[3:],
                    model=per_core.get(key[3:]),
                    usage_percent=value,
                    frequency_mhz=freq / 1000 if freq is not None else None,
                    unavailable_reason="等待下一次采样" if value is None else None,
                )
            )
        temperatures = [
            integer(optional_text(path))
            for path in self.sys.glob("class/thermal/thermal_zone*/temp")
            if "cpu" in (optional_text(path.parent / "type") or "").lower()
        ]
        temperature = max((v / 1000 for v in temperatures if v is not None), default=None)
        cpu = MonitorProcessor(
            device_id="cpu",
            model=model,
            usage_percent=self._cpu_percent(current.get("cpu"), previous.get("cpu")),
            cores=tuple(cores),
            temperature_celsius=temperature,
            unavailable_reason=(
                "无法读取 CPU 信息"
                if not cores
                else "CPU 占用采样中"
                if total_delta is None
                else None
            ),
        )
        mem = self._memory()
        disks = []
        for path in ("/", "/var/lib/eidolon"):
            try:
                s = os.statvfs(path)
                disks.append(
                    MonitorDisk(
                        path=path,
                        total_bytes=s.f_blocks * s.f_frsize,
                        available_bytes=s.f_bavail * s.f_frsize,
                    )
                )
            except OSError:
                disks.append(MonitorDisk(path=path, unavailable_reason="无法读取文件系统"))
        raw_uptime = optional_text(self.proc / "uptime")
        try:
            uptime = float(raw_uptime.split()[0]) if raw_uptime else None
        except (ValueError, IndexError):
            uptime = None
        btime = next(
            (integer(line.split()[-1]) for line in stat.splitlines() if line.startswith("btime ")),
            None,
        )
        npus, npu_reason = self._npus(chip)
        processes, membership, limited = self._processes(
            total_delta,
            uptime,
            btime,
            tuple(f["ControlGroup"] for f in units.values() if f.get("ControlGroup")),
        )
        groups_now = {}
        services = []
        for service_id, fields in units.items():
            cg = fields.get("ControlGroup", "")
            members = tuple(
                p
                for p in processes
                if cg and any(g == cg or g.startswith(cg + "/") for g in membership.get(p.pid, ()))
            )
            # Read cgroup totals once per service, including descendants.
            group_path = self.sys / "fs/cgroup" / cg.lstrip("/")
            memory = integer(optional_text(group_path / "memory.current")) if cg else None
            cpu_raw = optional_text(group_path / "cpu.stat") if cg else None
            used = next(
                (
                    integer(line.split()[-1])
                    for line in (cpu_raw or "").splitlines()
                    if line.startswith("usage_usec ")
                ),
                None,
            )
            percent = None
            if used is not None:
                groups_now[cg] = used
                before = self._group_previous.get(cg)
                if before is not None and used >= before and total_delta:
                    percent = min(
                        100.0, 100 * (used - before) * self._hz / (1_000_000 * total_delta)
                    )
            main_pid = integer(fields.get("MainPID"))
            services.append(
                MonitorService(
                    service_id=service_id,
                    unit=fields.get("Id"),
                    state="/".join(
                        (fields.get("ActiveState", "unknown"), fields.get("SubState", "unknown"))
                    ),
                    cpu_percent=percent,
                    memory_bytes=memory,
                    main_pid=main_pid if main_pid else None,
                    configuration_path=fields.get("FragmentPath") or None,
                    working_directory=fields.get("WorkingDirectory") or None,
                    user=fields.get("User") or None,
                    exit_code=integer(fields.get("ExecMainStatus"))
                    if fields.get("ExecMainCode") not in (None, "0")
                    else None,
                    processes=members,
                    unavailable_reason=(
                        "无法读取服务资源（未运行或权限不足）"
                        if memory is None
                        else "进程详情不可读取"
                        if main_pid and not members
                        else None
                    ),
                )
            )
        self._group_previous = groups_now
        snapshot = HostMonitorWire(
            observed_at=self.clock(),
            hostname=socket.gethostname(),
            machine_model=(optional_text(self.sys / "firmware/devicetree/base/model")
                           or optional_text(self.sys / "class/dmi/id/product_name")
                           or "").strip("\x00\n ")[:256] or None,
            operating_system=linux_operating_system(),
            uptime_seconds=uptime,
            cpu=cpu,
            npus=npus,
            npu_unavailable_reason=npu_reason,
            memory=mem,
            disks=tuple(disks),
            services=tuple(services),
            services_unavailable_reason=unit_error
            or ("进程较多或读取较慢，本次仅显示部分进程详情" if limited else None),
        )

        return fit_transport(snapshot)

    @staticmethod
    def _cpu_percent(now, before):
        if now is None or before is None or now[0] <= before[0] or now[1] < before[1]:
            return None
        return max(0.0, min(100.0, 100 * (1 - (now[1] - before[1]) / (now[0] - before[0]))))

    def _memory(self):
        fields = {}
        for line in (optional_text(self.proc / "meminfo") or "").splitlines():
            key, _, rest = line.partition(":")
            value = integer(rest.split()[0]) if rest.split() else None
            if value is not None:
                fields[key] = value * 1024
        total, available = fields.get("MemTotal"), fields.get("MemAvailable")
        if total is None or available is None:
            return MonitorMemory(unavailable_reason="无法读取内存信息")
        return MonitorMemory(total_bytes=total, available_bytes=available)

    def _npus(self, chip):
        path = self.sys / "kernel/debug/rknpu/load"
        try:
            load = read_text(path)
        except PermissionError:
            return (), "NPU 读数权限不足"
        except FileNotFoundError:
            return (), "未发现可读取的 NPU 驱动接口"
        except OSError:
            return (), "NPU 驱动读数失败"
        cores = tuple(
            MonitorCore(
                core_id=m[0],
                model=(chip.upper() + " NPU") if chip else None,
                usage_percent=float(m[1]),
            )
            for m in re.findall(r"Core(\d+):\s*(\d+(?:\.\d+)?)%", load)
            if 0 <= float(m[1]) <= 100
        )
        if not cores:
            return (), "NPU 驱动未提供可识别的逐核占用"
        return (
            MonitorProcessor(
                device_id="rknpu",
                model=(chip.upper() + " NPU") if chip else None,
                cores=cores,
                unavailable_reason="驱动仅提供逐核占用，未提供总体利用率",
            ),
        ), None

    def _processes(self, total_delta, uptime, btime, allowed_groups):
        result = []
        membership = {}
        counters = {}
        limited = False
        started = time.monotonic()
        try:
            dirs = sorted(
                (p for p in self.proc.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)
            )
            limited = len(dirs) > 4096
            dirs = dirs[:4096]
        except OSError:
            dirs = []
        for directory in dirs:
            if len(result) >= 128 or time.monotonic() - started > 2:
                limited = True
                break
            try:
                name, fields = process_stat(read_text(directory / "stat"))
                start = int(fields[19])
                ticks = int(fields[11]) + int(fields[12])
                pid = int(directory.name)
                key = (pid, start)
                counters[key] = ticks
                groups = tuple(
                    line.split(":", 2)[-1]
                    for line in (optional_text(directory / "cgroup") or "").splitlines()
                )
                membership[pid] = groups
                # Only Eidolon unit processes leave the machine; other counters
                # are unnecessary and discarded before the next sample.
                if not any(
                    group == allowed or group.startswith(allowed + "/")
                    for group in groups
                    for allowed in allowed_groups
                ):
                    counters.pop(key, None)
                    continue
                before = self._process_previous.get(key)
                percent = (
                    min(100.0, 100 * (ticks - before) / total_delta)
                    if before is not None and total_delta and ticks >= before
                    else None
                )
                inaccessible = []

                def link(field):
                    try:
                        return os.readlink(directory / field)
                    except OSError:
                        inaccessible.append(field)
                        return None

                executable, cwd = link("exe"), link("cwd")
                cmdline = optional_text(directory / "cmdline")
                command, source, module = safe_command(
                    (cmdline or "").rstrip("\x00").split("\x00") if cmdline else [], cwd
                )
                if cmdline is None:
                    inaccessible.append("cmdline")
                try:
                    uid = directory.stat().st_uid
                    try:
                        user = pwd.getpwuid(uid).pw_name
                    except KeyError:
                        user = str(uid)
                except OSError:
                    user = None
                # PID reuse/exit during collection must not attach another
                # process's paths or counters to the original identity.
                _, after = process_stat(read_text(directory / "stat"))
                if int(after[19]) != start:
                    counters.pop(key, None)
                    continue
                result.append(
                    MonitorProcess(
                        pid=pid,
                        parent_pid=int(fields[1]),
                        name=name,
                        state=fields[0],
                        cpu_percent=percent,
                        rss_bytes=max(0, int(fields[21]) * self._pages),
                        executable=executable,
                        working_directory=cwd,
                        command=command or None,
                        source_path=source,
                        entry_module=module,
                        user=user,
                        started_at=datetime.fromtimestamp(btime + start / self._hz, UTC)
                        if btime is not None
                        else None,
                        uptime_seconds=max(0.0, uptime - start / self._hz)
                        if uptime is not None
                        else None,
                        unavailable_reason="部分启动信息不可读：" + ", ".join(inaccessible)
                        if inaccessible
                        else None,
                    )
                )
            except (OSError, ValueError, IndexError):
                continue
        self._process_previous = counters
        return result, membership, limited


def fit_transport(snapshot: HostMonitorWire) -> HostMonitorWire:
    """Leave framing headroom within Mobile's 1 MiB pinned HTTPS limit.

    Preserve service totals and remove only process detail if necessary.
    The reason remains visible; a truncated listing never claims completeness.
    """
    budget = 900 * 1024
    services = list(snapshot.services)
    while len(snapshot.model_dump_json().encode("utf-8")) > budget:
        candidates = [i for i, service in enumerate(services) if service.processes]
        if not candidates:
            raise ValueError("host monitor metadata exceeds transport limit")
        index = max(candidates, key=lambda i: len(services[i].model_dump_json()))
        service = services[index]
        services[index] = service.model_copy(
            update={"processes": service.processes[: len(service.processes) // 2]}
        )
        snapshot = snapshot.model_copy(
            update={
                "services": tuple(services),
                "services_unavailable_reason": "进程详情较多，本次已省略部分条目；服务资源总量仍完整",
            }
        )
    return snapshot


def linux_operating_system() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")[:256]
    except OSError:
        return "Linux"
