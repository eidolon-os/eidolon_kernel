from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from eidolon_system.adapters.host.monitor import LinuxHostMonitor, safe_command


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def proc_stat(ticks=10, start=100):
    fields = ["S", "1"] + ["0"] * 22
    fields[11] = str(ticks)
    fields[12] = "0"
    fields[19] = str(start)
    fields[21] = "32"
    return "12 (worker ) name) " + " ".join(fields)


def collector(tmp_path):
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    put(
        proc / "stat",
        "cpu 100 0 0 900 0 0 0 0\ncpu0 50 0 0 450 0 0 0 0\ncpu1 50 0 0 450 0 0 0 0\nbtime 1700000000",
    )
    put(proc / "uptime", "1000 0")
    put(proc / "meminfo", "MemTotal: 1000 kB\nMemAvailable: 600 kB")
    put(proc / "cpuinfo", "processor: 0\nCPU part: 0xd05\n\nprocessor: 1\nCPU part: 0xd0b")
    put(sys / "firmware/devicetree/base/compatible", "vendor,board\x00rockchip,rk3588\x00")
    put(sys / "kernel/debug/rknpu/load", "NPU load: Core0: 12%, Core1: 75%, Core2: 0%")
    put(proc / "12/stat", proc_stat())
    put(proc / "12/cgroup", "0::/system.slice/eidolon-hub.service")
    put(proc / "12/cmdline", "python\x00-m\x00app.main\x00--api-key\x00SECRET\x00")
    (proc / "12/exe").symlink_to("/opt/python")
    (proc / "12/cwd").symlink_to("/opt/eidolon/app")
    put(sys / "fs/cgroup/system.slice/eidolon-hub.service/memory.current", "12000")
    put(sys / "fs/cgroup/system.slice/eidolon-hub.service/cpu.stat", "usage_usec 100000")
    monitor = LinuxHostMonitor(
        SimpleNamespace(), proc=proc, sys=sys, clock=lambda: datetime(2026, 9, 9, tzinfo=UTC)
    )
    units = {
        "hub": {
            "Id": "eidolon-hub.service",
            "ControlGroup": "/system.slice/eidolon-hub.service",
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "12",
            "FragmentPath": "/etc/systemd/system/eidolon-hub.service",
        }
    }
    return monitor, units, proc, sys


def test_real_counter_deltas_per_core_and_process_pid_reuse(tmp_path):
    monitor, units, proc, sys = collector(tmp_path)
    first = monitor._snapshot(units, None)
    assert first.cpu.usage_percent is None
    assert [c.model for c in first.cpu.cores] == ["Cortex-A55", "Cortex-A76"]
    assert [c.usage_percent for c in first.npus[0].cores] == [12, 75, 0]
    assert first.npus[0].usage_percent is None  # never invent a total
    put(
        proc / "stat",
        "cpu 130 0 0 970 0 0 0 0\ncpu0 70 0 0 480 0 0 0 0\ncpu1 60 0 0 490 0 0 0 0\nbtime 1700000000",
    )
    put(proc / "12/stat", proc_stat(ticks=20))
    second = monitor._snapshot(units, None)
    assert second.cpu.usage_percent == pytest.approx(30)
    assert [c.usage_percent for c in second.cpu.cores] == pytest.approx([40, 20])
    process = second.services[0].processes[0]
    assert process.cpu_percent == 10
    assert process.entry_module == "app.main"
    assert process.source_path is None
    assert process.executable == "/opt/python"
    assert process.working_directory == "/opt/eidolon/app"
    assert "SECRET" not in second.model_dump_json()
    assert second.services[0].memory_bytes == 12000
    put(proc / "12/stat", proc_stat(ticks=1, start=200))
    third = monitor._snapshot(units, None)
    assert third.services[0].processes[0].cpu_percent is None
    assert (12, 100) not in monitor._process_previous


def test_missing_metrics_are_unavailable_not_zero(tmp_path):
    monitor, units, proc, sys = collector(tmp_path)
    (sys / "kernel/debug/rknpu/load").unlink()
    (proc / "meminfo").unlink()
    data = monitor._snapshot(units, None)
    assert data.npus == () and data.npu_unavailable_reason
    assert data.memory.total_bytes is None and data.memory.unavailable_reason
    assert data.cpu.cores  # partial failure does not blank the page


def test_unrelated_processes_are_not_attached_by_name(tmp_path):
    monitor, units, proc, sys = collector(tmp_path)
    put(proc / "12/cgroup", "0::/system.slice/unrelated-eidolon.service")
    assert monitor._snapshot(units, None).services[0].processes == ()
    assert monitor._process_previous == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "app.py", "--password=SECRET"],
        ["python", "app.py", "--new-secret-kind", "SECRET"],
        ["bash", "-c", "echo SECRET"],
        ["python", "-c", 'print("SECRET")'],
        ["app", "--config", "https://user:SECRET@host/file"],
    ],
)
def test_command_redaction_defaults_to_unknown_value_removal(argv):
    command, _, _ = safe_command(argv, "/opt/app")
    assert "SECRET" not in command


def test_script_path_is_distinct_from_interpreter():
    command, source, module = safe_command(["python", "server.py", "--port", "9000"], "/opt/app")
    assert source == "/opt/app/server.py"
    assert module is None
    assert "9000" in command


@pytest.mark.asyncio
async def test_systemd_is_one_read_only_bounded_call(tmp_path):
    calls = []

    class Runner:
        async def run(self, *args):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout="Id=eidolon-hub.service\nActiveState=active\n\nId=eidolond.service\nActiveState=active",
            )

    definition = SimpleNamespace(
        host_targets={"systemd": "eidolon-hub.service"}, service_id="hub", manages=lambda x: True
    )
    manager = SimpleNamespace(
        host=SimpleNamespace(driver_name="systemd", runner=Runner(), systemctl="systemctl"),
        catalog=SimpleNamespace(definitions=[definition]),
    )
    monitor = LinuxHostMonitor(
        manager, proc=tmp_path / "missing", sys=tmp_path / "missing", runner=manager.host.runner
    )
    result = await monitor.read()
    assert len(calls) == 1 and calls[0][1] == "show"
    assert len(result.services) == 2
    assert result.cpu.usage_percent is None


def test_large_process_detail_fits_mobile_transport_without_losing_totals(tmp_path):
    from eidolon_system.adapters.host.monitor import fit_transport

    monitor, units, _, _ = collector(tmp_path)
    snapshot = monitor._snapshot(units, None)
    service = snapshot.services[0]
    process = service.processes[0].model_copy(update={"command": "x" * 20000})
    large = snapshot.model_copy(
        update={"services": (service.model_copy(update={"processes": (process,) * 100}),)}
    )
    bounded = fit_transport(large)
    assert len(bounded.model_dump_json().encode()) <= 900 * 1024
    assert bounded.services[0].memory_bytes == service.memory_bytes
    assert bounded.services_unavailable_reason


def test_console_script_is_a_real_entry_path():
    _, source, module = safe_command(
        ["/opt/venv/bin/python", "/opt/venv/bin/eidolon-agent"], "/opt/app"
    )
    assert source == "/opt/venv/bin/eidolon-agent"
    assert module is None
