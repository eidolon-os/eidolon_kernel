"""Infrastructure ports owned by the System Manager application."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from eidolon_system.domain.model import (
    DesiredServiceState,
    HostServiceState,
    HostVitals,
    RuntimeIntent,
    ServiceDefinition,
    ServiceEndpoint,
    ServiceStatus,
    StoredMutation,
    SystemAuditEvent,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class HostServiceSupervisor(Protocol):
    """Mutations submit work; only inspect establishes execution outcomes.

    Adapters must report queued/running transitions even if the old instance is
    still active. A timeout leaves the outcome unknown, not undone.
    """
    @property
    def driver_name(self) -> str: ...

    async def inspect(self, target: str) -> HostServiceState: ...

    async def start(self, target: str) -> None: ...

    async def stop(self, target: str) -> None: ...

    async def restart(self, target: str) -> None: ...


class NetworkEnvironment(Protocol):
    """Observed network input and the last input applied to each process.

    A missing snapshot means unavailable or still settling, never an empty LAN.
    Applied inputs are disposable observations, separate from desired state.
    """

    async def snapshot(self) -> str | None: ...

    def applied(self, service_id: str) -> str | None: ...

    def record(self, service_id: str, fingerprint: str) -> None: ...


class ReadinessProbe(Protocol):
    async def check(self, url: str) -> bool: ...


class ServiceDirectory(Protocol):
    def put(self, status: ServiceStatus) -> None: ...

    def get(self, service_id: str) -> ServiceStatus: ...

    def list(self) -> tuple[ServiceStatus, ...]: ...

    def resolve(self, service_id: str, endpoint_id: str) -> ServiceEndpoint: ...


class SystemStateStore(Protocol):
    def ensure_services(
        self, definitions: tuple[ServiceDefinition, ...], *, now: datetime
    ) -> None: ...

    def get(self, service_id: str) -> DesiredServiceState: ...

    def list_states(self) -> tuple[DesiredServiceState, ...]: ...

    def get_request(
        self, *, request_id: str, operation: str, fingerprint: str
    ) -> StoredMutation | None: ...

    def set_enabled(
        self,
        *,
        service_id: str,
        enabled: bool,
        expected_revision: int,
        request_id: str,
        fingerprint: str,
        now: datetime,
    ) -> StoredMutation: ...

    def record_operation(
        self,
        *,
        service_id: str,
        operation: str,
        expected_revision: int,
        request_id: str,
        fingerprint: str,
        now: datetime,
        intent: RuntimeIntent | None = None,
    ) -> StoredMutation: ...

    def pending_intent(self, service_id: str) -> RuntimeIntent | None: ...

    def put_intent(self, intent: RuntimeIntent, *, now: datetime) -> None: ...

    def update_intent(self, intent: RuntimeIntent) -> None: ...

    def finish_intent(self, intent: RuntimeIntent) -> None: ...

    def list_audit(
        self, *, after_position: int, limit: int
    ) -> tuple[SystemAuditEvent, ...]: ...

    def close(self) -> None: ...


class HostVitalsReader(Protocol):
    """Reads how the machine itself is doing.

    A port because the readings come from the platform — procfs and sysfs on
    Linux, and nothing like them elsewhere — and because a router that reached
    for the platform directly would make every test of that router need a
    particular machine to run on.
    """

    def read(self) -> HostVitals: ...
