from collections import namedtuple
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_system.adapters.host.macos_monitor import MacHostMonitor

Cpu = namedtuple("Cpu", "user system idle nice")


class FakeProcess:
    def __init__(self, pid, parent, system):
        self.pid = pid
        self.info = {"pid": pid, "ppid": parent, "create_time": 1000.0}
        self.system = system

    def create_time(self):
        return 1000.0

    def oneshot(self):
        return nullcontext()

    def cpu_times(self):
        return SimpleNamespace(user=self.system.ticks, system=0.0)

    def memory_info(self):
        return SimpleNamespace(rss=1024)

    def cwd(self):
        return "/opt/app"

    def exe(self):
        return "/opt/python"

    def cmdline(self):
        return ["python", "server.py", "--token", "hidden"]

    def name(self):
        return "python"

    def username(self):
        return "user"

    def status(self):
        return "sleeping"


class FakeSystem:
    class Error(Exception):
        pass

    def __init__(self):
        self.ticks = 1.0
        self.processes = {
            pid: FakeProcess(pid, parent, self) for pid, parent in [(10, 1), (11, 10), (20, 1)]
        }

    def cpu_times(self, percpu):
        return [Cpu(10 + self.ticks, 0, 90, 0), Cpu(20 + self.ticks, 0, 80, 0)]

    def virtual_memory(self):
        return SimpleNamespace(total=10000, available=3000)

    def boot_time(self):
        return 1000.0

    def disk_usage(self, path):
        return SimpleNamespace(total=10000, free=2000)

    def process_iter(self, *args, **kwargs):
        return iter(self.processes.values())

    def Process(self, pid):
        return self.processes[pid]


def test_macos_process_tree_and_delta_are_machine_normalized():
    system = FakeSystem()
    manager = SimpleNamespace(
        host=SimpleNamespace(config_path=Path("/etc/supervisor.conf")),
        store=SimpleNamespace(path=Path("/tmp/state.db")),
    )
    current = [10.0]
    monitor = MacHostMonitor(manager, system=system, monotonic=lambda: current[0])
    first = monitor._snapshot({"hub": ("running", 10)}, "Apple test chip")
    assert first.cpu.usage_percent is None
    assert len(first.cpu.cores) == 2
    assert [p.pid for p in first.services[0].processes] == [10, 11]
    assert first.services[0].memory_kind.startswith("RSS")
    assert "hidden" not in first.model_dump_json()
    current[0] = 20.0
    system.ticks = 3.0
    second = monitor._snapshot({"hub": ("running", 10)}, "Apple test chip")
    assert second.services[0].processes[0].cpu_percent == pytest.approx(10)
    assert second.services[0].cpu_percent == pytest.approx(20)
    assert second.services[0].processes[0].source_path == "/opt/app/server.py"
    assert second.npus == () and second.npu_unavailable_reason


def test_missing_supervisor_does_not_hide_hardware():
    system = FakeSystem()
    manager = SimpleNamespace(
        host=SimpleNamespace(config_path=Path("/etc/supervisor.conf")),
        store=SimpleNamespace(path=Path("/tmp/state.db")),
    )
    monitor = MacHostMonitor(manager, system=system)
    value = monitor._snapshot(None, "Apple test chip")
    assert value.cpu.cores
    assert value.services_unavailable_reason


@pytest.mark.asyncio
async def test_machine_name_uses_only_model_fields_and_is_cached():
    class Runner:
        calls = 0
        async def run(self, *args):
            self.calls += 1
            return SimpleNamespace(stdout='{"SPHardwareDataType":[{"machine_name":"MacBook Pro","machine_model":"Mac15,7","serial_number":"private-serial"}]}')
    runner = Runner()
    monitor = MacHostMonitor(SimpleNamespace(), system=FakeSystem(), runner=runner)
    assert await monitor._machine_model() == "MacBook Pro · Mac15,7"
    assert await monitor._machine_model() == "MacBook Pro · Mac15,7"
    assert runner.calls == 1
