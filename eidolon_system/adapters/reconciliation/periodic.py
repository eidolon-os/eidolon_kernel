"""Single-process periodic reconciler for eidolond."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

_log = logging.getLogger(__name__)


class ReconciliationJob(Protocol):
    async def reconcile(self) -> None: ...


class PeriodicServiceReconciler:
    def __init__(self, manager: ReconciliationJob, *, interval_seconds: float) -> None:
        self.manager = manager
        self.interval_seconds = interval_seconds
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="eidolond-reconciler")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                try:
                    await self.manager.reconcile()
                except Exception:
                    _log.exception("system service reconciliation failed")

    async def close(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
