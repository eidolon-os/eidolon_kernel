"""What a Host says about itself, and what it refuses to make up.

The rule under test is the one that makes a health screen worth having: a
reading that could not be taken is reported as absent, with a reason, and
never as a number. A disk that could not be stat'ed is not an empty disk, and
a board with no thermal zone is not a cold board — and it is exactly when
something is wrong that a substituted value does the damage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_system.adapters.host import vitals as vitals_module
from eidolon_system.adapters.host.vitals import read_host_vitals
from eidolon_system.contracts.mappers import vitals_to_wire
from eidolon_system.domain.model import Measurement


def test_a_reading_is_a_number_or_a_reason_never_neither() -> None:
    with pytest.raises(ValueError) as error:
        Measurement(name="disk.state", unit="bytes")

    assert "no value and no reason" in str(error.value)


def test_an_unreadable_disk_is_absent_rather_than_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vitals_module,
        "_WATCHED_PATHS",
        (("disk.state", Path("/definitely/not/here")),),
    )

    vitals = read_host_vitals()
    disk = vitals.named("disk.state")

    assert disk is not None
    assert disk.value is None
    # Zero free bytes and "we could not look" are the same picture on a screen
    # unless the difference survives to it.
    assert disk.capacity is None
    assert "definitely/not/here" in (disk.unavailable_reason or "")


def test_free_space_is_what_this_product_could_write(tmp_path: Path) -> None:
    monkey = pytest.MonkeyPatch()
    monkey.setattr(vitals_module, "_WATCHED_PATHS", (("disk.state", tmp_path),))
    try:
        disk = read_host_vitals().named("disk.state")
    finally:
        monkey.undo()

    assert disk is not None and disk.value is not None and disk.capacity is not None
    assert 0 < disk.value <= disk.capacity
    assert disk.unit == "bytes"


def test_memory_reports_what_an_allocation_could_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:        8000000 kB\n"
        "MemFree:          100000 kB\n"
        "MemAvailable:    6000000 kB\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(vitals_module, "_MEMINFO", meminfo)

    memory = read_host_vitals().named("memory.available")

    assert memory is not None
    # MemAvailable, not MemFree: free memory on a healthy Linux box looks
    # alarmingly small because the kernel spends it on cache it will give back.
    assert memory.value == 6000000 * 1024
    assert memory.capacity == 8000000 * 1024


def test_a_meminfo_without_the_fields_is_an_absence_not_a_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("Committed_AS:  123 kB\n", encoding="utf-8")
    monkeypatch.setattr(vitals_module, "_MEMINFO", meminfo)

    memory = read_host_vitals().named("memory.available")

    assert memory is not None and memory.value is None
    assert "MemTotal" in (memory.unavailable_reason or "")


def test_load_carries_the_core_count_it_should_be_read_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loadavg = tmp_path / "loadavg"
    loadavg.write_text("1.50 0.80 0.40 2/311 4242\n", encoding="utf-8")
    monkeypatch.setattr(vitals_module, "_LOADAVG", loadavg)

    vitals = read_host_vitals()

    assert vitals.named("cpu.load1").value == 1.5
    assert vitals.named("cpu.load5").value == 0.8
    assert vitals.named("cpu.load15").value == 0.4
    # "Load 1.5" means nothing without knowing how many cores it is spread
    # over, and a percentage would throw that away.
    assert vitals.named("cpu.load1").capacity == float(__import__("os").cpu_count())


def test_the_hottest_zone_is_the_temperature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index, millidegrees in enumerate((41200, 57800, 39000)):
        zone = tmp_path / f"thermal_zone{index}"
        zone.mkdir()
        (zone / "temp").write_text(str(millidegrees), encoding="utf-8")
    monkeypatch.setattr(vitals_module, "_THERMAL", tmp_path)

    temperature = read_host_vitals().named("temperature")

    # A board is as hot as its hottest part; zone 0 is just whichever one the
    # kernel enumerated first.
    assert temperature is not None and temperature.value == 57.8


def test_a_board_without_thermal_zones_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vitals_module, "_THERMAL", tmp_path / "absent")

    temperature = read_host_vitals().named("temperature")

    assert temperature is not None and temperature.value is None
    assert temperature.unavailable_reason


def test_absence_survives_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vitals_module, "_THERMAL", Path("/definitely/not/here"))

    wire = vitals_to_wire(read_host_vitals())
    document = wire.model_dump(mode="json")
    temperature = next(
        item for item in document["measurements"] if item["name"] == "temperature"
    )

    assert temperature["value"] is None
    assert temperature["unavailable_reason"]
    # Consumers key on this to know the shape they are reading.
    assert document["operation"] == "system.host-vitals"
