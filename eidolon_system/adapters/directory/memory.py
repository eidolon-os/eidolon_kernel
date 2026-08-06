"""Typed in-process projection of currently observed system services."""

from __future__ import annotations

import threading

from eidolon_system.domain.errors import NotFound, NotReady
from eidolon_system.domain.model import ServiceEndpoint, ServiceStatus


class InMemoryServiceDirectory:
    def __init__(self) -> None:
        self._statuses: dict[str, ServiceStatus] = {}
        self._lock = threading.RLock()

    def put(self, status: ServiceStatus) -> None:
        with self._lock:
            self._statuses[status.service_id] = status

    def get(self, service_id: str) -> ServiceStatus:
        with self._lock:
            status = self._statuses.get(service_id)
        if status is None:
            raise NotFound(f"system service not found: {service_id}")
        return status

    def list(self) -> tuple[ServiceStatus, ...]:
        with self._lock:
            return tuple(self._statuses[key] for key in sorted(self._statuses))

    def resolve(self, service_id: str, endpoint_id: str) -> ServiceEndpoint:
        status = self.get(service_id)
        if status.runtime_state != "ready":
            raise NotReady(f"system service is not ready: {service_id}")
        for endpoint in status.endpoints:
            if endpoint.endpoint_id == endpoint_id:
                return endpoint
        raise NotFound(f"system service endpoint not found: {service_id}/{endpoint_id}")
