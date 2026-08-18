"""Desired-state reconciliation and service directory publication."""

from __future__ import annotations

import asyncio
import hashlib
import json

from eidolon_system.domain.errors import Conflict, HostOperationFailed
from eidolon_system.domain.model import (
    ServiceCatalog,
    ServiceDefinition,
    ServiceStatus,
    StoredMutation,
)
from eidolon_system.ports.runtime import (
    Clock,
    HostServiceSupervisor,
    ReadinessProbe,
    ServiceDirectory,
    SystemStateStore,
)


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
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.directory = directory
        self.host = host
        self.readiness = readiness
        self.clock = clock
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            now = self.clock.now()
            self.store.ensure_services(self.catalog.definitions, now=now)
            for definition in self.catalog.definitions:
                self.directory.put(
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
            if not dependencies_ready:
                self.directory.put(
                    ServiceStatus(
                        service_id=definition.service_id,
                        required=definition.required,
                        desired=state,
                        runtime_state="blocked",
                        detail="dependency is not ready",
                        observed_at=self.clock.now(),
                    )
                )
                continue
            await self._start_or_observe(definition, state)

    async def _stop_disabled(self, definition: ServiceDefinition, state) -> None:
        driver = self.host.driver_name
        if not definition.manages(driver):
            # Desired state says off and the thing that could turn it off is
            # not this Host's driver. Reporting "inactive" would claim a stop
            # that never happened, so the gap is published as what it is.
            self.directory.put(
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
            if observed.active:
                await self.host.stop(target)
            runtime_state = "inactive"
            detail = None
        except HostOperationFailed as exc:
            runtime_state = "failed"
            detail = str(exc)
        self.directory.put(
            ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state=runtime_state,
                detail=detail,
                observed_at=self.clock.now(),
            )
        )

    async def _start_or_observe(self, definition: ServiceDefinition, state) -> None:
        driver = self.host.driver_name
        if not definition.manages(driver):
            await self._observe_external(definition, state)
            return
        target = definition.target_for(driver)
        try:
            observed = await self.host.inspect(target)
            if not observed.active:
                await self.host.start(target)
                observed = await self.host.inspect(target)
            if not observed.active:
                status = ServiceStatus(
                    service_id=definition.service_id,
                    required=definition.required,
                    desired=state,
                    runtime_state="starting",
                    detail=observed.detail or observed.state,
                    observed_at=self.clock.now(),
                )
            else:
                health_urls = self._health_urls(definition)
                checks = await asyncio.gather(
                    *(self.readiness.check(url) for url in health_urls)
                )
                ready = all(checks)
                status = ServiceStatus(
                    service_id=definition.service_id,
                    required=definition.required,
                    desired=state,
                    runtime_state="ready" if ready else "degraded",
                    detail=None if ready else "readiness probe failed",
                    observed_at=self.clock.now(),
                    endpoints=definition.endpoints if ready else (),
                )
        except HostOperationFailed as exc:
            status = ServiceStatus(
                service_id=definition.service_id,
                required=definition.required,
                desired=state,
                runtime_state="failed",
                detail=str(exc),
                observed_at=self.clock.now(),
            )
        self.directory.put(status)

    async def _observe_external(self, definition: ServiceDefinition, state) -> None:
        """Publish an unmanaged service from its own health, not from a target.

        A dependency gate reads ``runtime_state == "ready"``, so a service this
        driver does not start still has to earn that word rather than be given
        it: without a health endpoint there is nothing to earn it with, and the
        honest answer is that we do not know.
        """

        health_urls = self._health_urls(definition)
        if not health_urls:
            self.directory.put(
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
        self.directory.put(
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
                raise Conflict(
                    f"external system service cannot be restarted here: {service_id}"
                )
            await self.host.restart(definition.target_for(self.host.driver_name))
            result = self.store.record_operation(
                service_id=service_id,
                operation=operation,
                expected_revision=expected_revision,
                request_id=request_id,
                fingerprint=fingerprint,
                now=self.clock.now(),
            )
            await self._reconcile_unlocked()
            return result

    def list_audit(self, *, after_position: int, limit: int):
        self._require_initialized()
        return self.store.list_audit(after_position=after_position, limit=limit)
