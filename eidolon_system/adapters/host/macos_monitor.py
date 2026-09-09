"""macOS current telemetry using OS counters through psutil.

No powermetrics privilege escalation, process environment, or history. Service
membership follows supervisord PIDs and the live process tree, not name matches.
"""

from __future__ import annotations

import asyncio
import json
import platform
import socket
import time
from datetime import UTC, datetime

from eidolon_sdk.system.v1.host_monitor import (
    HostMonitorWire,
    MonitorCore,
    MonitorDisk,
    MonitorMemory,
    MonitorProcess,
    MonitorProcessor,
    MonitorService,
)

from eidolon_system.adapters.host.monitor import LinuxHostMonitor, fit_transport, safe_command
from eidolon_system.adapters.host.runner import SubprocessCommandRunner


class MacHostMonitor:
    def __init__(self, manager, *, system=None, runner=None, monotonic=time.monotonic):
        if system is None:
            import psutil

            system = psutil
        self.system = system
        self.manager = manager
        self.runner = runner or SubprocessCommandRunner(timeout_seconds=3)
        self.monotonic = monotonic
        self._lock = asyncio.Lock()
        self._cpu_previous = []
        self._process_previous = {}
        self._observed = None
        self._machine_name = None

    async def read(self):
        async with self._lock:
            status, model, machine = await asyncio.gather(
                self._status(), self._model(), self._machine_model()
            )
            return await asyncio.to_thread(self._snapshot, status, model, machine)

    async def _model(self, key="machdep.cpu.brand_string"):
        try:
            result = await self.runner.run("/usr/sbin/sysctl", "-n", key)
            return result.stdout.strip()[:256] if result.returncode == 0 else None
        except Exception:
            return None

    async def _machine_model(self):
        if self._machine_name:
            return self._machine_name
        try:
            result = await self.runner.run(
                "/usr/sbin/system_profiler", "SPHardwareDataType", "-json"
            )
            hardware = json.loads(result.stdout)["SPHardwareDataType"][0]
            # Only model fields leave this boundary; serials and UUIDs do not.
            name = hardware.get("machine_name")
            code = hardware.get("machine_model")
            self._machine_name = " · ".join(str(v)[:128] for v in (name, code) if v)
        except Exception:
            pass
        if not self._machine_name:
            self._machine_name = await self._model("hw.model")
        return self._machine_name

    async def _status(self):
        host = self.manager.host
        try:
            result = await self.runner.run(
                host.supervisorctl, "-c", str(host.config_path), "status"
            )
            if result.returncode not in (0, 3):
                return None
            services = {}
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) < 2 or fields[1].lower() not in host._STATUS_VALUES:
                    continue
                pid = None
                if "pid" in fields:
                    index = fields.index("pid") + 1
                    pid = int(fields[index].rstrip(","))
                services[fields[0]] = (fields[1].lower(), pid)
            return services
        except Exception:
            return None

    def _snapshot(self, status, model, machine=None):
        ps = self.system
        now = self.monotonic()
        elapsed = now - self._observed if self._observed is not None else None
        self._observed = now
        cores = []
        current = []
        try:
            for index, counters in enumerate(ps.cpu_times(percpu=True)):
                pair = (sum(counters), counters.idle)
                current.append(pair)
                before = self._cpu_previous[index] if index < len(self._cpu_previous) else None
                value = LinuxHostMonitor._cpu_percent(pair, before)
                cores.append(
                    MonitorCore(
                        core_id=str(index),
                        model=model,
                        usage_percent=value,
                        unavailable_reason="CPU 占用采样中" if value is None else None,
                    )
                )
        except (OSError, ps.Error):
            pass
        overall = None
        if len(current) == len(self._cpu_previous) and current:
            overall = LinuxHostMonitor._cpu_percent(
                tuple(map(sum, zip(*current))), tuple(map(sum, zip(*self._cpu_previous)))
            )
        self._cpu_previous = current
        cpu = MonitorProcessor(
            device_id="cpu",
            model=model,
            cores=tuple(cores),
            usage_percent=overall,
            unavailable_reason="无法读取 CPU 计数"
            if not cores
            else "CPU 占用采样中"
            if overall is None
            else None,
        )
        try:
            mem = ps.virtual_memory()
            memory = MonitorMemory(total_bytes=mem.total, available_bytes=mem.available)
        except (OSError, ps.Error):
            memory = MonitorMemory(unavailable_reason="无法读取内存")
        try:
            boot = ps.boot_time()
            uptime = max(0.0, time.time() - boot)
        except (OSError, ps.Error):
            uptime = None
        disks = []
        for path in dict.fromkeys(
            (
                "/",
                str(self.manager.store.path.parent)
                if hasattr(self.manager.store, "path")
                else "/System/Volumes/Data",
            )
        ):
            try:
                usage = ps.disk_usage(path)
                disks.append(
                    MonitorDisk(path=path, total_bytes=usage.total, available_bytes=usage.free)
                )
            except (OSError, ps.Error):
                disks.append(MonitorDisk(path=path, unavailable_reason="无法读取文件系统"))
        services, limited = self._services(status or {}, elapsed, max(len(current), 1))
        return fit_transport(
            HostMonitorWire(
                observed_at=datetime.now(UTC),
                hostname=socket.gethostname(),
                machine_model=machine,
                operating_system="macOS " + platform.mac_ver()[0],
                uptime_seconds=uptime,
                cpu=cpu,
                memory=memory,
                disks=tuple(disks),
                services=services,
                npu_unavailable_reason="macOS 未提供可读取的 NPU 型号与逐核占用接口",
                services_unavailable_reason="无法读取 supervisord 服务信息"
                if status is None
                else "进程较多或权限不足，部分详情未提供"
                if limited
                else None,
            )
        )

    def _services(self, status, elapsed, cpu_count):
        ps = self.system
        roots = {pid: name for name, (_, pid) in status.items() if pid}
        processes = {}
        started = self.monotonic()
        limited = False
        try:
            for process in ps.process_iter(["pid", "ppid", "create_time"], ad_value=None):
                if len(processes) >= 4096 or self.monotonic() - started > 2:
                    limited = True
                    break
                processes[process.pid] = process.info
        except (OSError, ps.Error):
            limited = True
        groups = {name: [] for name in status}
        counters = {}
        for pid, info in processes.items():
            ancestor, seen = pid, set()
            while ancestor not in roots and ancestor in processes and ancestor not in seen:
                seen.add(ancestor)
                ancestor = processes[ancestor]["ppid"]
            if ancestor not in roots:
                continue
            if len(counters) >= 128 or self.monotonic() - started > 3:
                limited = True
                break
            try:
                process = ps.Process(pid)
                created = process.create_time()
                if created != info["create_time"]:
                    continue
                unavailable = []

                def read(method):
                    try:
                        return getattr(process, method)()
                    except (OSError, ps.Error):
                        unavailable.append(method)
                        return None

                with process.oneshot():
                    times = read("cpu_times")
                    memory = read("memory_info")
                    cwd, exe = read("cwd"), read("exe")
                    argv = read("cmdline") or []
                    command, source, module = safe_command(argv, cwd)
                    name = read("name") or str(pid)
                    user, state = read("username"), read("status")
                key = (pid, created)
                value = None
                if times is not None:
                    ticks = times.user + times.system
                    counters[key] = ticks
                    before = self._process_previous.get(key)
                    if before is not None and elapsed and elapsed > 0 and ticks >= before:
                        value = min(100.0, 100 * (ticks - before) / (elapsed * cpu_count))
                if ps.Process(pid).create_time() != created:
                    counters.pop(key, None)
                    continue
                groups[roots[ancestor]].append(
                    MonitorProcess(
                        pid=pid,
                        parent_pid=info["ppid"] or 0,
                        name=name,
                        state=state or "unknown",
                        cpu_percent=value,
                        rss_bytes=memory.rss if memory else None,
                        executable=exe,
                        working_directory=cwd,
                        command=command or None,
                        source_path=source,
                        entry_module=module,
                        user=user,
                        started_at=datetime.fromtimestamp(created, UTC),
                        uptime_seconds=max(0.0, time.time() - created),
                        unavailable_reason="部分信息不可读取：" + ", ".join(unavailable)
                        if unavailable
                        else None,
                    )
                )
            except (OSError, ps.Error):
                limited = True
                continue
        self._process_previous = counters
        services = []
        for name, (state, pid) in status.items():
            members = groups[name]
            cpu = (
                sum(p.cpu_percent for p in members)
                if members and all(p.cpu_percent is not None for p in members)
                else None
            )
            memory = (
                sum(p.rss_bytes for p in members)
                if members and all(p.rss_bytes is not None for p in members)
                else None
            )
            main = next((p for p in members if p.pid == pid), None)
            services.append(
                MonitorService(
                    service_id=name,
                    unit=name,
                    state=state,
                    main_pid=pid,
                    cpu_percent=min(100.0, cpu) if cpu is not None else None,
                    memory_bytes=memory,
                    memory_kind="RSS 合计（共享页可能重复）",
                    configuration_path=str(self.manager.host.config_path),
                    working_directory=main.working_directory if main else None,
                    user=main.user if main else None,
                    processes=tuple(members),
                    unavailable_reason="部分进程不可读取，总量可能不完整" if limited else None,
                )
            )
        return tuple(services), limited
