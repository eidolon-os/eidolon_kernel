"""Desired-state reconciliation and service directory publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from eidolon_system.application.runtime_coordinator import RuntimeCoordinator
from eidolon_system.domain.errors import Conflict, HostOperationFailed, NotFound, StateStoreFailed
from eidolon_system.domain.model import (
    ServiceCatalog,
    ServiceDefinition,
    ServiceStatus,
    StoredMutation,
)
from eidolon_system.ports.runtime import (
    Clock,
    HostServiceSupervisor,
    NetworkEnvironment,
    ReadinessProbe,
    ServiceDirectory,
    SystemStateStore,
)

_log = logging.getLogger(__name__)


def _fingerprint(operation: str, service_id: str, expected_revision: int) -> str:
    payload = json.dumps(
        {
            "expected_revision": expected_revision,
            "operation": operation,
            "service_id": service_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ServiceManager:
    def __init__(
        self,
        *,
        catalog: ServiceCatalog,
        store: SystemStateStore,
        directory: ServiceDirectory,
        host: HostServiceSupervisor,
        readiness: ReadinessProbe,
        clock: Clock,
        network: NetworkEnvironment | None = None,
        monotonic=time.monotonic,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.directory = directory
        self.host = host
        self.readiness = readiness
        self.clock = clock
        self.network = network
        self._network_snapshot: str | None = None
        self.runtime = RuntimeCoordinator(store, host, monotonic=monotonic)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            now = self.clock.now()
            self.store.ensure_services(self.catalog.definitions, now=now)
            for definition in self.catalog.definitions:
                self._publish(
                    ServiceStatus(
                        service_id=definition.service_id,
                        required=definition.required,
                        desired=self.store.get(definition.service_id),
                        runtime_state="unknown",
                        detail="not yet reconciled",
                        observed_at=now,
                    )
                )
            self._initialized = True

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("ServiceManager.initialize() must run before use")

    async def reconcile(self) -> None:
        async with self._lock:
            self._require_initialized()
            await self._reconcile_unlocked()

    async def _reconcile_unlocked(self) -> None:
        self._network_snapshot = await self.network.snapshot() if self.network else None
        desired = {state.service_id: state for state in self.store.list_states()}
        for definition in self.catalog.stop_order:
            state = desired[definition.service_id]
            if state.enabled:
                continue
            await self._stop_disabled(definition, state)

        for definition in self.catalog.start_order:
            state = desired[definition.service_id]
            if not state.enabled:
                continue
            dependencies_ready = all(
                self.directory.get(dependency).runtime_state == "ready"
                for dependency in definition.dependencies
            )
            await self._start_or_observe(definition, state, can_start=dependencies_ready)

    async def _stop_disabled(self, definition: ServiceDefinition, state) -> None:
        driver = self.host.driver_name
        if not definition.manages(driver):
            # Desired state says off and the thing that could turn it off is
            # not this Host's driver. Reporting "inactive" would claim a stop
            # that never happened, so the gap is published as what it is.
            self._publish(
                ServiceStatus(
                    service_id=definition.service_id,
                    required=definition.required,
                    desired=state,
                    runtime_state="degraded",
                    detail=f"external to {driver}; disable it where it actually runs",
                    observed_at=self.clock.now(),
                )
            )
            return
        target = definition.target_for(driver)
        try:
            observed = await self.host.inspect(target)
            observed, pending = await self.runtime.advance(
                service_id=definition.service_id,
                target=target,
                enabled=False,
                observed=observed,
                network_input=self._network_snapshot,
                can_start=False,
                network=self.network,
            )
            if not pending and observed.active:
                intent = self.runtime.prepare(definition.service_id, "stop", observed, None)
                self.runtime.enqueue(intent, now=self.clock.now())
                self._transition(definition, state, "stopping")
                observed, pending = await self.runtime.advance(
                    service_id=definition.service_id,
                    target=target,
                    enabled=False,
                    observed=observed,
                    network_input=self._network_snapshot,
                    can_start=False,
                    network=self.network,
                )
            runtime_state = "degraded" if pending or observed.active else "inactive"
            detail = "waiting for host stop" if runtime_state != "inactive" else None
        except (HostOperationFailed, OSError, StateStoreFailed) as exc:
            runtime_state = "failed"
            detail = str(exc)
        self._publish(
            ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state=runtime_state,
                detail=detail,
                observed_at=self.clock.now(),
            )
        )

    def _publish(self, status: ServiceStatus) -> None:
        """Record a status, and say so when it changed.

        Every status the reconciler produces goes through here, which is also
        the only place that can tell a new observation from a repeated one.
        That distinction is the whole point: reconciliation runs every few
        seconds, so logging each observation would bury the one that matters,
        and logging none — which is what happened before — leaves an operator
        with "readiness timeout: nats, kernel, ..." and no way to learn that
        every start was refused for want of privilege. The reason was already
        here, in detail, published to a directory nothing durable read.
        """

        try:
            previous = self.directory.get(status.service_id)
        except NotFound:
            previous = None
        self.directory.put(status)
        unchanged = previous is not None and (
            previous.runtime_state,
            previous.detail,
            previous.network_current,
        ) == (status.runtime_state, status.detail, status.network_current)
        if unchanged:
            return
        _log.log(
            logging.ERROR if status.runtime_state == "failed" else logging.INFO,
            "service %s: %s%s",
            status.service_id,
            status.runtime_state,
            f" ({status.detail})" if status.detail else "",
        )

    def _transition(self, definition, state, detail, runtime_state="starting") -> None:
        self._publish(
            ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state=runtime_state,
                detail=detail,
                observed_at=self.clock.now(),
            )
        )

    async def _start_or_observe(
        self, definition: ServiceDefinition, state, *, can_start: bool = True
    ) -> None:
        driver = self.host.driver_name
        if not definition.manages(driver):
            if can_start:
                await self._observe_external(definition, state)
            else:
                self._transition(definition, state, "dependency is not ready", "blocked")
            return
        target = definition.target_for(driver)
        try:
            observed = await self.host.inspect(target)
            fingerprint = self._network_snapshot
            network_bound = definition.restart_on_network_change
            network_available = not network_bound or (
                self.network is not None and fingerprint is not None
            )
            # Observe completion even when dependencies/network became unavailable.
            observed, pending = await self.runtime.advance(
                service_id=definition.service_id,
                target=target,
                enabled=True,
                observed=observed,
                network_input=fingerprint if network_bound else None,
                can_start=can_start and network_available,
                network=self.network,
            )
            if not can_start:
                self._transition(definition, state, "dependency is not ready", "blocked")
                return
            if not network_available:
                self._transition(
                    definition, state, "local network unavailable or settling", "degraded"
                )
                return
            if pending:
                self._transition(definition, state, "waiting for host operation")
                return
            if network_bound and await self.network.snapshot() != fingerprint:
                self._transition(
                    definition, state, "local network changed while observing", "degraded"
                )
                return
            applied_input = json.dumps([fingerprint, observed.instance_id])
            if network_bound and observed.active and observed.instance_id is None:
                raise HostOperationFailed("cannot observe process instance for network refresh")
            stale = network_bound and self.network.applied(definition.service_id) != applied_input
            if not observed.active or stale:
                intent = self.runtime.prepare(
                    definition.service_id,
                    "restart" if observed.active else "start",
                    observed,
                    fingerprint if network_bound else None,
                )
                self.runtime.enqueue(intent, now=self.clock.now())
                self._transition(definition, state, "waiting for host operation")
                observed, pending = await self.runtime.advance(
                    service_id=definition.service_id,
                    target=target,
                    enabled=True,
                    observed=observed,
                    network_input=fingerprint if network_bound else None,
                    can_start=True,
                    network=self.network,
                )
            if pending or observed.transitioning or not observed.active:
                self._transition(definition, state, "waiting for host operation")
                return
            if network_bound and (
                await self.network.snapshot() != fingerprint
                or self.network.applied(definition.service_id)
                != json.dumps([fingerprint, observed.instance_id])
            ):
                self._transition(
                    definition, state, "local network changed while starting", "degraded"
                )
                return
            checks = await asyncio.gather(
                *(self.readiness.check(url) for url in self._health_urls(definition))
            )
            ready = all(checks)
            # Health belongs to the instance we probed, not a replacement or a
            # queued job that appeared while the probe was in flight.
            after_probe = await self.host.inspect(target)
            if (
                after_probe.transitioning
                or not after_probe.active
                or after_probe.instance_id != observed.instance_id
            ):
                self._transition(definition, state, "process changed during readiness probe")
                return
            if network_bound and await self.network.snapshot() != fingerprint:
                self._transition(
                    definition, state, "local network changed during readiness probe", "degraded"
                )
                return
            status = ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state="ready" if ready else "degraded",
                detail=None if ready else "readiness probe failed",
                observed_at=self.clock.now(),
                endpoints=definition.endpoints if ready else (),
                network_current=True if network_bound else None,
            )
        except (HostOperationFailed, OSError, StateStoreFailed) as exc:
            status = ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state="failed",
                detail=str(exc),
                observed_at=self.clock.now(),
            )
        self._publish(status)

    async def _observe_external(self, definition: ServiceDefinition, state) -> None:
        """Publish an unmanaged service from its own health, not from a target.

        A dependency gate reads ``runtime_state == "ready"``, so a service this
        driver does not start still has to earn that word rather than be given
        it: without a health endpoint there is nothing to earn it with, and the
        honest answer is that we do not know.
        """

        health_urls = self._health_urls(definition)
        if not health_urls:
            self._publish(
                ServiceStatus(
                    service_id=definition.service_id,
                    required=definition.required,
                    desired=state,
                    runtime_state="unknown",
                    detail=f"external to {self.host.driver_name}; no health endpoint to observe it by",
                    observed_at=self.clock.now(),
                )
            )
            return
        checks = await asyncio.gather(*(self.readiness.check(url) for url in health_urls))
        ready = all(checks)
        self._publish(
            ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state="ready" if ready else "degraded",
                detail=None if ready else "external readiness probe failed",
                observed_at=self.clock.now(),
                endpoints=definition.endpoints if ready else (),
            )
        )

    @staticmethod
    def _health_urls(definition: ServiceDefinition) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                endpoint.health_url
                for endpoint in definition.endpoints
                if endpoint.health_url is not None
            )
        )

    def list_services(self) -> tuple[ServiceStatus, ...]:
        self._require_initialized()
        return self.directory.list()

    def get_service(self, service_id: str) -> ServiceStatus:
        self._require_initialized()
        self.catalog.get(service_id)
        return self.directory.get(service_id)

    def resolve(self, service_id: str, endpoint_id: str):
        self._require_initialized()
        self.catalog.get(service_id)
        return self.directory.resolve(service_id, endpoint_id)

    async def set_enabled(
        self,
        *,
        service_id: str,
        enabled: bool,
        expected_revision: int,
        request_id: str,
    ) -> StoredMutation:
        async with self._lock:
            self._require_initialized()
            definition = self.catalog.get(service_id)
            operation = "system.service.enable" if enabled else "system.service.disable"
            fingerprint = _fingerprint(operation, service_id, expected_revision)
            existing = self.store.get_request(
                request_id=request_id, operation=operation, fingerprint=fingerprint
            )
            if existing is not None:
                return existing
            current = self.store.get(service_id)
            if current.enabled == enabled:
                label = "enabled" if enabled else "disabled"
                raise Conflict(f"system service is already {label}; replay the original request_id")
            if not enabled and definition.required:
                raise Conflict(f"required system service cannot be disabled: {service_id}")
            if not enabled:
                enabled_dependents = [
                    dependent
                    for dependent in self.catalog.dependents_of(service_id)
                    if self.store.get(dependent).enabled
                ]
                if enabled_dependents:
                    raise Conflict(
                        f"enabled dependents must be disabled first: {', '.join(enabled_dependents)}"
                    )
            result = self.store.set_enabled(
                service_id=service_id,
                enabled=enabled,
                expected_revision=expected_revision,
                request_id=request_id,
                fingerprint=fingerprint,
                now=self.clock.now(),
            )
            self._transition(definition, result.state, "desired state changed")
            await self._reconcile_unlocked()
            return result

    async def restart(
        self, *, service_id: str, expected_revision: int, request_id: str
    ) -> StoredMutation:
        async with self._lock:
            self._require_initialized()
            definition = self.catalog.get(service_id)
            operation = "system.service.restart"
            fingerprint = _fingerprint(operation, service_id, expected_revision)
            existing = self.store.get_request(
                request_id=request_id, operation=operation, fingerprint=fingerprint
            )
            if existing is not None:
                return existing
            state = self.store.get(service_id)
            if not state.enabled:
                raise Conflict(f"disabled system service cannot be restarted: {service_id}")
            if not definition.manages(self.host.driver_name):
                raise Conflict(f"external system service cannot be restarted here: {service_id}")
            if self.store.pending_intent(service_id) is not None:
                raise Conflict(f"system service already has a pending operation: {service_id}")
            observed = await self.host.inspect(definition.target_for(self.host.driver_name))
            if observed.transitioning:
                raise Conflict(f"system service is transitioning: {service_id}")
            before = (
                await self.network.snapshot()
                if self.network and definition.restart_on_network_change
                else None
            )
            intent = self.runtime.prepare(service_id, "restart", observed, before, request_id)
            result = self.store.record_operation(
                service_id=service_id,
                operation=operation,
                expected_revision=expected_revision,
                request_id=request_id,
                fingerprint=fingerprint,
                now=self.clock.now(),
                intent=intent,
            )
            self.runtime.accepted(intent)
            self._transition(definition, state, "restart requested")
            await self._reconcile_unlocked()
            return result

    def list_audit(self, *, after_position: int, limit: int):
        self._require_initialized()
        return self.store.list_audit(after_position=after_position, limit=limit)
