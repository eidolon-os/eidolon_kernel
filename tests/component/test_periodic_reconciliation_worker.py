from __future__ import annotations

import asyncio

import pytest

from eidolon_kernel.adapters.reconciliation.periodic import (
    PeriodicReconciliationWorker,
)


@pytest.mark.asyncio
async def test_periodic_worker_runs_immediately_and_closes_idempotently() -> None:
    completed = asyncio.Event()

    class Job:
        async def execute(self):
            completed.set()

    worker = PeriodicReconciliationWorker(Job(), interval_seconds=60)
    worker.start()
    await asyncio.wait_for(completed.wait(), timeout=1)
    with pytest.raises(RuntimeError, match="already started"):
        worker.start()
    await worker.close()
    await worker.close()


@pytest.mark.asyncio
async def test_periodic_worker_survives_one_failed_scan(caplog) -> None:
    completed = asyncio.Event()

    class FlakyJob:
        calls = 0

        async def execute(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("scan failure")
            completed.set()

    job = FlakyJob()
    worker = PeriodicReconciliationWorker(job, interval_seconds=0.001)
    worker.start()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await worker.close()

    assert job.calls >= 2
    assert "device mount reconciliation scan failed" in caplog.text


def test_periodic_worker_rejects_non_positive_interval() -> None:
    class Job:
        async def execute(self):
            return None

    with pytest.raises(ValueError, match="positive"):
        PeriodicReconciliationWorker(Job(), interval_seconds=0)
