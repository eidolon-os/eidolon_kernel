"""What the machine can observe about itself, read from the kernel it runs on.

Every reading here comes from a file Linux already keeps — ``/proc/meminfo``,
``/proc/loadavg``, ``/proc/uptime``, the thermal zones, and ``statvfs`` on the
paths this product actually stores things in. No sampling loop, no history, no
extra dependency: one read per request, and what cannot be read is reported as
unreadable rather than guessed at.

That last part is the whole design. A Host that cannot find its thermal zone
has not measured zero degrees, and a filesystem that could not be stat'ed is
not empty. Every failure mode here ends in a named absence, because a health
screen that silently substitutes a plausible number for a missing one is worse
than no health screen: it is a health screen that lies exactly when something
is wrong.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from eidolon_system.domain.model import HostVitals, Measurement

__all__ = ["LinuxHostVitals", "read_host_vitals"]

#: Where this product keeps the things that would hurt to lose, and the root
#: filesystem they sit on. Named rather than discovered: "which disk matters"
#: is a deployment fact, and a Host that reports every mount reports mostly
#: noise.
_WATCHED_PATHS = (
    ("disk.state", Path("/var/lib/eidolon")),
    ("disk.root", Path("/")),
)

_MEMINFO = Path("/proc/meminfo")
_LOADAVG = Path("/proc/loadavg")
_UPTIME = Path("/proc/uptime")
_THERMAL = Path("/sys/class/thermal")


class LinuxHostVitals:
    """Reads vitals from procfs and sysfs. Absent files are absent readings."""

    def __init__(self, *, now: object | None = None) -> None:
        self._now = now

    def read(self) -> HostVitals:
        return read_host_vitals()


def read_host_vitals() -> HostVitals:
    measurements: list[Measurement] = []
    measurements.extend(_disks())
    measurements.extend(_memory())
    measurements.extend(_load())
    measurements.append(_uptime())
    measurements.append(_temperature())
    return HostVitals(
        observed_at=datetime.now(UTC),
        measurements=tuple(measurements),
    )


def _disks() -> list[Measurement]:
    readings: list[Measurement] = []
    for name, path in _WATCHED_PATHS:
        try:
            stat = os.statvfs(path)
        except OSError as error:
            readings.append(
                Measurement(
                    name=name,
                    unit="bytes",
                    unavailable_reason=f"{path}: {error.strerror or error}",
                )
            )
            continue
        # Available to an unprivileged writer, not merely unallocated: the
        # reserved blocks a filesystem keeps for root are not space this
        # product can use, and counting them is how a disk looks fine right
        # up until a write fails.
        readings.append(
            Measurement(
                name=name,
                value=float(stat.f_bavail * stat.f_frsize),
                unit="bytes",
                capacity=float(stat.f_blocks * stat.f_frsize),
            )
        )
    return readings


def _memory() -> list[Measurement]:
    try:
        fields = _meminfo_fields()
    except OSError as error:
        return [
            Measurement(
                name="memory.available",
                unit="bytes",
                unavailable_reason=f"{_MEMINFO}: {error.strerror or error}",
            )
        ]
    total = fields.get("MemTotal")
    # MemAvailable rather than MemFree: the kernel's own estimate of what a
    # new allocation could actually get, which counts reclaimable cache. MemFree
    # on a healthy Linux box looks alarmingly small and means nothing.
    available = fields.get("MemAvailable")
    if total is None or available is None:
        return [
            Measurement(
                name="memory.available",
                unit="bytes",
                unavailable_reason=f"{_MEMINFO} did not report MemTotal/MemAvailable",
            )
        ]
    return [
        Measurement(
            name="memory.available",
            value=float(available),
            unit="bytes",
            capacity=float(total),
        )
    ]


def _meminfo_fields() -> dict[str, int]:
    fields: dict[str, int] = {}
    for line in _MEMINFO.read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            amount = int(parts[0])
        except ValueError:
            continue
        # kB in this file means KiB, and everything else here is bytes.
        fields[key.strip()] = amount * 1024 if parts[-1].lower() == "kb" else amount
    return fields


def _load() -> list[Measurement]:
    cores = os.cpu_count()
    try:
        raw = _LOADAVG.read_text(encoding="utf-8").split()
        one, five, fifteen = (float(value) for value in raw[:3])
    except (OSError, ValueError) as error:
        return [
            Measurement(
                name="cpu.load1",
                unavailable_reason=f"{_LOADAVG}: {error}",
            )
        ]
    # Capacity is the core count, so a reader can say "load 4 on 4 cores" —
    # a percentage would throw away the part that makes load legible.
    return [
        Measurement(name="cpu.load1", value=one, capacity=cores),
        Measurement(name="cpu.load5", value=five, capacity=cores),
        Measurement(name="cpu.load15", value=fifteen, capacity=cores),
    ]


def _uptime() -> Measurement:
    try:
        seconds = float(_UPTIME.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError) as error:
        return Measurement(
            name="uptime",
            unit="seconds",
            unavailable_reason=f"{_UPTIME}: {error}",
        )
    return Measurement(name="uptime", value=seconds, unit="seconds")


def _temperature() -> Measurement:
    """The warmest zone this board exposes.

    Warmest rather than the first one: a board with several sensors is as hot
    as its hottest part, and picking zone 0 would report whichever one the
    kernel happened to enumerate first.
    """

    hottest: float | None = None
    try:
        zones = sorted(_THERMAL.glob("thermal_zone*/temp"))
    except OSError as error:
        return Measurement(
            name="temperature",
            unit="celsius",
            unavailable_reason=f"{_THERMAL}: {error}",
        )
    for zone in zones:
        try:
            millidegrees = int(zone.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        celsius = millidegrees / 1000
        hottest = celsius if hottest is None else max(hottest, celsius)
    if hottest is None:
        return Measurement(
            name="temperature",
            unit="celsius",
            unavailable_reason="this board exposes no readable thermal zone",
        )
    return Measurement(name="temperature", value=hottest, unit="celsius")
