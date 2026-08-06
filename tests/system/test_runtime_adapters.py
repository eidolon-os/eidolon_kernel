from __future__ import annotations

import asyncio
import sys

import httpx
import pytest

from eidolon_system.adapters.host.runner import SubprocessCommandRunner
from eidolon_system.adapters.readiness.http import HttpReadinessProbe
from eidolon_system.adapters.reconciliation.periodic import PeriodicServiceReconciler
from eidolon_system.adapters.runtime import SystemClock
from eidolon_system.domain.errors import HostOperationFailed


@pytest.mark.asyncio
async def test_subprocess_runner_executes_without_shell_and_reports_missing_binary() -> None:
    runner = SubprocessCommandRunner(timeout_seconds=2)
    result = await runner.run(sys.executable, "-c", "print('ready')")
    assert result.returncode == 0
    assert result.stdout.strip() == "ready"
    with pytest.raises(HostOperationFailed, match="host command failed"):
        await runner.run("/definitely/missing/eidolon-command")
    timeout_runner = SubprocessCommandRunner(timeout_seconds=0.01)
    with pytest.raises(HostOperationFailed, match="timed out"):
        await timeout_runner.run(sys.executable, "-c", "import time; time.sleep(1)")


class FakeHttpClient:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.closed = False

    async def get(self, url: str):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_http_readiness_treats_transport_and_5xx_as_not_ready() -> None:
    probe = HttpReadinessProbe()
    probe._client = FakeHttpClient(httpx.Response(204))
    assert await probe.check("http://127.0.0.1/health") is True
    probe._client = FakeHttpClient(httpx.Response(503))
    assert await probe.check("http://127.0.0.1/health") is False
    client = FakeHttpClient(httpx.ConnectError("offline"))
    probe._client = client
    assert await probe.check("http://127.0.0.1/health") is False
    await probe.close()
    assert client.closed is True


class CountingManager:
    def __init__(self) -> None:
        self.calls = 0
        self.called = asyncio.Event()

    async def reconcile(self) -> None:
        self.calls += 1
        self.called.set()


@pytest.mark.asyncio
async def test_periodic_reconciler_runs_and_closes_cleanly() -> None:
    manager = CountingManager()
    worker = PeriodicServiceReconciler(manager, interval_seconds=0.01)
    worker.start()
    await asyncio.wait_for(manager.called.wait(), timeout=1)
    await worker.close()
    await worker.close()
    assert manager.calls >= 1
    assert SystemClock().now().utcoffset() is not None
