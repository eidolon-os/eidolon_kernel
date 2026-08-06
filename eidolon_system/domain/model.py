"""System service catalog, desired state and observed runtime model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from eidolon_system.domain.errors import InvalidManifest, NotFound

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


def _identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise InvalidManifest(f"{name} must be a stable lowercase identifier")
    return value


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    endpoint_id: str
    protocol: str
    address: str
    contract: str
    health_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_id", _identifier("endpoint_id", self.endpoint_id))
        object.__setattr__(self, "protocol", _identifier("protocol", self.protocol))
        if not self.address.strip():
            raise InvalidManifest("endpoint address must not be empty")
        if not self.contract.strip():
            raise InvalidManifest("endpoint contract must not be empty")
        if self.health_url is not None and not self.health_url.startswith(("http://", "https://")):
            raise InvalidManifest("health_url must be HTTP(S)")


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    service_id: str
    description: str
    required: bool
    enabled_by_default: bool
    dependencies: tuple[str, ...]
    host_targets: dict[str, str]
    endpoints: tuple[ServiceEndpoint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_id", _identifier("service_id", self.service_id))
        dependencies = tuple(_identifier("dependency", item) for item in self.dependencies)
        if len(dependencies) != len(set(dependencies)):
            raise InvalidManifest(f"{self.service_id} has duplicate dependencies")
        if self.service_id in dependencies:
            raise InvalidManifest(f"{self.service_id} cannot depend on itself")
        object.__setattr__(self, "dependencies", dependencies)
        if not self.host_targets:
            raise InvalidManifest(f"{self.service_id} requires at least one host target")
        targets: dict[str, str] = {}
        for driver, target in self.host_targets.items():
            targets[_identifier("host driver", driver)] = target.strip()
            if not targets[driver]:
                raise InvalidManifest(f"{self.service_id} host target must not be empty")
        object.__setattr__(self, "host_targets", targets)
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise InvalidManifest(f"{self.service_id} has duplicate endpoint_id")

    def target_for(self, driver_name: str) -> str:
        try:
            return self.host_targets[driver_name]
        except KeyError as exc:
            raise InvalidManifest(
                f"{self.service_id} has no host target for {driver_name}"
            ) from exc


class ServiceCatalog:
    """Validated immutable service definitions and dependency ordering."""

    def __init__(self, definitions: tuple[ServiceDefinition, ...]) -> None:
        if not definitions:
            raise InvalidManifest("service manifest must contain at least one service")
        self._by_id: dict[str, ServiceDefinition] = {}
        for definition in definitions:
            if definition.service_id in self._by_id:
                raise InvalidManifest(f"duplicate service_id: {definition.service_id}")
            self._by_id[definition.service_id] = definition
        for definition in definitions:
            unknown = set(definition.dependencies) - set(self._by_id)
            if unknown:
                raise InvalidManifest(
                    f"{definition.service_id} has unknown dependency: {sorted(unknown)[0]}"
                )
        self._start_order = self._topological_order()

    def _topological_order(self) -> tuple[ServiceDefinition, ...]:
        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[ServiceDefinition] = []

        def visit(service_id: str) -> None:
            if service_id in visiting:
                raise InvalidManifest(f"service dependency cycle contains {service_id}")
            if service_id in visited:
                return
            visiting.add(service_id)
            definition = self._by_id[service_id]
            for dependency in definition.dependencies:
                visit(dependency)
            visiting.remove(service_id)
            visited.add(service_id)
            ordered.append(definition)

        for service_id in sorted(self._by_id):
            visit(service_id)
        return tuple(ordered)

    @property
    def definitions(self) -> tuple[ServiceDefinition, ...]:
        return tuple(self._by_id[service_id] for service_id in sorted(self._by_id))

    @property
    def start_order(self) -> tuple[ServiceDefinition, ...]:
        return self._start_order

    @property
    def stop_order(self) -> tuple[ServiceDefinition, ...]:
        return tuple(reversed(self._start_order))

    def get(self, service_id: str) -> ServiceDefinition:
        try:
            return self._by_id[service_id]
        except KeyError as exc:
            raise NotFound(f"system service not found: {service_id}") from exc

    def dependents_of(self, service_id: str) -> tuple[str, ...]:
        return tuple(
            definition.service_id
            for definition in self.definitions
            if service_id in definition.dependencies
        )


@dataclass(frozen=True, slots=True)
class DesiredServiceState:
    service_id: str
    enabled: bool
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HostServiceState:
    active: bool
    state: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    service_id: str
    required: bool
    desired: DesiredServiceState
    runtime_state: str
    detail: str | None
    observed_at: datetime
    endpoints: tuple[ServiceEndpoint, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemAuditEvent:
    position: int
    service_id: str
    operation: str
    desired_revision: int
    enabled: bool
    request_id: str
    fingerprint: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMutation:
    operation: str
    fingerprint: str
    state: DesiredServiceState
    audit_position: int
    replayed: bool = False
