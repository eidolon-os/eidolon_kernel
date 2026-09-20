"""Write intent, submit a host job, then reconcile its observed outcome.

The host owns execution and its deadlines. A transport reply is never evidence
that a process started/stopped. One persisted intent per service prevents a
reconciler restart from forgetting work already handed to the host.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from eidolon_system.domain.errors import HostOperationFailed
from eidolon_system.domain.model import HostServiceState, RuntimeIntent
from eidolon_system.ports.runtime import HostServiceSupervisor, NetworkEnvironment, SystemStateStore


class RuntimeCoordinator:
    def __init__(
        self, store: SystemStateStore, host: HostServiceSupervisor, *, monotonic=time.monotonic
    ) -> None:
        self.store = store
        self.host = host
        self.monotonic = monotonic
        self._retry_after: dict[str, float] = {}

    def prepare(
        self,
        service_id: str,
        action: str,
        observed: HostServiceState,
        network_input: str | None,
        request_id: str | None = None,
    ) -> RuntimeIntent:
        if action == "restart" and observed.active and observed.instance_id is None:
            raise HostOperationFailed("cannot observe process instance for restart")
        return RuntimeIntent(
            service_id,
            request_id or f"runtime-{uuid4()}",
            action,
            observed.instance_id,
            network_input,
        )

    def enqueue(self, intent: RuntimeIntent, *, now: datetime) -> None:
        self.store.put_intent(intent, now=now)
        self.accepted(intent)

    def accepted(self, intent: RuntimeIntent) -> None:
        self._retry_after[intent.service_id] = self.monotonic()

    async def _finish(
        self,
        intent: RuntimeIntent,
        observed: HostServiceState,
        network_input: str | None,
        network: NetworkEnvironment | None,
    ) -> None:
        # Write observation before removing intent. If either write fails,
        # observing the same process again is safe and must not restart it.
        if (
            intent.action != "stop"
            and intent.network_input is not None
            and intent.network_input == network_input
            and network is not None
            and await network.snapshot() == network_input
        ):
            if observed.instance_id is None:
                raise HostOperationFailed("cannot observe refreshed process instance")
            network.record(intent.service_id, json.dumps([network_input, observed.instance_id]))
        self.store.finish_intent(intent)
        self._retry_after.pop(intent.service_id, None)

    async def advance(
        self,
        *,
        service_id: str,
        target: str,
        enabled: bool,
        observed: HostServiceState,
        network_input: str | None,
        can_start: bool,
        network: NetworkEnvironment | None,
    ) -> tuple[HostServiceState, bool]:
        intent = self.store.pending_intent(service_id)
        # Job presence takes precedence even if the old process is still active.
        if observed.transitioning:
            return observed, True
        if intent is None:
            return observed, False
        if intent.completed_by(observed):
            await self._finish(intent, observed, network_input if enabled else None, network)
            return observed, False
        # Latest desired state wins after the host settles, never by racing an
        # opposite job against one already running.
        if (intent.action == "stop") == enabled:
            self.store.finish_intent(intent)
            self._retry_after.pop(service_id, None)
            return observed, False
        if intent.action != "stop" and not can_start:
            return observed, True
        # A recovered intent may have been submitted just before the daemon
        # crashed. Give an uncertain submission a grace period before retrying.
        deadline = self._retry_after.setdefault(service_id, self.monotonic() + 30)
        if self.monotonic() < deadline:
            return observed, True
        # A retry submits against the current stable input. Persist it before
        # dispatch, otherwise a network move while waiting would cause a second
        # restart merely to correct the old attempt's observation.
        if intent.action != "stop" and intent.network_input != network_input:
            intent = replace(intent, network_input=network_input)
            self.store.update_intent(intent)
        self._retry_after[service_id] = self.monotonic() + 30
        error = None
        try:
            await getattr(self.host, intent.action)(target)
        except (HostOperationFailed, OSError) as exc:
            error = exc
        observed = await self.host.inspect(target)
        if intent.completed_by(observed):
            await self._finish(intent, observed, network_input if enabled else None, network)
            return observed, False
        if error is not None:
            raise error
        return observed, True
