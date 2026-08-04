"""Small asyncio scheduler for the typed reconciliation use case."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

LOGGER = logging.getLogger(__name__)


class ReconciliationJob(Protocol):
    async def execute(self) -> object: ...


class PeriodicReconciliationWorker:
    def __init__(
        self,
        reconciliation: ReconciliationJob,
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self._reconciliation = reconciliation
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("reconciliation worker is already started")
        self._task = asyncio.create_task(
            self._run(),
            name="eidolon-kernel-mount-reconciliation",
        )

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await self._reconciliation.execute()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("device mount reconciliation scan failed")
            await asyncio.sleep(self._interval_seconds)
