from __future__ import annotations

from datetime import datetime, timezone

from eidolon_system.domain.model import HostServiceState


class FakeHostSupervisor:
    driver_name = "fake"

    def __init__(self) -> None:
        self.active: dict[str, bool] = {}
        self.instances: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    async def inspect(self, target: str) -> HostServiceState:
        active = self.active.get(target, False)
        return HostServiceState(active=active, state="running" if active else "stopped",
                                instance_id=str(self.instances.get(target, 0)) if active else None)

    async def start(self, target: str) -> None:
        self.calls.append(("start", target))
        self.active[target] = True
        self.instances[target] = self.instances.get(target, 0) + 1

    async def stop(self, target: str) -> None:
        self.calls.append(("stop", target))
        self.active[target] = False

    async def restart(self, target: str) -> None:
        self.calls.append(("restart", target))
        self.active[target] = True
        self.instances[target] = self.instances.get(target, 0) + 1


class FakeReadinessProbe:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.urls: list[str] = []

    async def check(self, url: str) -> bool:
        self.urls.append(url)
        return self.ready


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)
