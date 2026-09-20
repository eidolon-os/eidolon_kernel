"""System service catalog, desired state and observed runtime model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from eidolon_system.domain.errors import InvalidManifest, NotFound

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

#: The one reserved host target. It says the driver named alongside it does not
#: manage this service on this kind of Host — NATS is a systemd unit on the
#: product image and an already-listening server a macOS source run merely
#: shares. Declaring that is not the same as leaving the driver out: an omitted
#: driver is a manifest that forgot one, and reconciliation would fail on it at
#: the moment it mattered. A service pinned here is still catalogued, still
#: probed, and still gates its dependents; only start, stop and restart are
#: refused, because there is nothing here to start them with.
EXTERNAL_HOST_TARGET = "external"


def _identifier(name: str, value: str, *, max_length: int = 128) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or not _IDENTIFIER.fullmatch(value)
    ):
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
        object.__setattr__(
            self,
            "protocol",
            _identifier("protocol", self.protocol, max_length=32),
        )
        if not self.address.strip() or len(self.address) > 2048:
            raise InvalidManifest("endpoint address must contain 1 to 2048 characters")
        if not self.contract.strip() or len(self.contract) > 256:
            raise InvalidManifest("endpoint contract must contain 1 to 256 characters")
        if self.health_url is not None and (
            len(self.health_url) > 2048
            or not self.health_url.startswith(("http://", "https://"))
        ):
            raise InvalidManifest("health_url must be HTTP(S) and at most 2048 characters")


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    service_id: str
    description: str
    required: bool
    enabled_by_default: bool
    dependencies: tuple[str, ...]
    host_targets: dict[str, str]
    endpoints: tuple[ServiceEndpoint, ...]
    #: A Host capability this service needs present, or None when every Host
    #: runs it — which is what every service was before any Host could differ,
    #: and why this is the one field with a default. `required` and
    #: `enabled_by_default` are read within the set this Host actually has: a
    #: service that is not here is not a disabled service, it is not a service
    #: of this Host at all.
    requires_capability: str | None = None
    # Services whose transport captures local interfaces at process creation.
    restart_on_network_change: bool = False

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
        if self.requires_capability is not None:
            object.__setattr__(
                self,
                "requires_capability",
                _identifier("requires_capability", self.requires_capability),
            )

    def manages(self, driver_name: str) -> bool:
        """Whether this driver is the thing that starts and stops the service."""

        return self.host_targets.get(driver_name) not in (None, EXTERNAL_HOST_TARGET)

    def target_for(self, driver_name: str) -> str:
        try:
            target = self.host_targets[driver_name]
        except KeyError as exc:
            raise InvalidManifest(
                f"{self.service_id} has no host target for {driver_name}"
            ) from exc
        if target == EXTERNAL_HOST_TARGET:
            raise InvalidManifest(
                f"{self.service_id} is external to {driver_name} and has no target to act on"
            )
        return target


def require_satisfiable_conditions(definitions: tuple[ServiceDefinition, ...]) -> None:
    """Refuse a manifest whose graph cannot survive being filtered.

    A service every Host runs must not depend on one only some Hosts have:
    filtering the conditional one away would leave a dependency naming nothing,
    and the Host that got there would be told it has an unknown dependency
    rather than that the manifest asks for something impossible.

    Checked over the whole manifest rather than after filtering, because it is a
    property of the manifest and is equally wrong on every Host — including the
    ones where the missing piece happens to be present.
    """

    conditional = {
        definition.service_id
        for definition in definitions
        if definition.requires_capability is not None
    }
    for definition in definitions:
        if definition.requires_capability is not None:
            continue
        depends_on = sorted(set(definition.dependencies) & conditional)
        if depends_on:
            raise InvalidManifest(
                f"{definition.service_id} runs on every Host and depends on "
                f"{depends_on[0]}, which only some Hosts have"
            )


def select_for_capabilities(
    definitions: tuple[ServiceDefinition, ...],
    capabilities: frozenset[str],
) -> tuple[tuple[ServiceDefinition, ...], tuple[tuple[str, str], ...]]:
    """The services this Host has, and what was left out and why.

    The second half is returned rather than discarded so the caller can say it.
    A service silently absent from a catalogue is the failure mode this whole
    mechanism exists to make impossible to reach by accident: nothing starts it,
    nothing reports it missing, and the readiness check that waits for it times
    out with no explanation anywhere.
    """

    require_satisfiable_conditions(definitions)
    kept: list[ServiceDefinition] = []
    dropped: list[tuple[str, str]] = []
    for definition in definitions:
        needed = definition.requires_capability
        if needed is None or needed in capabilities:
            kept.append(definition)
        else:
            dropped.append((definition.service_id, needed))
    return tuple(kept), tuple(dropped)


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
class Measurement:
    """One reading, or the honest absence of one.

    Every field here is optional on purpose. A Host reports what it can
    measure on the hardware and kernel it happens to be running on, and says
    nothing about the rest — a missing thermal zone is not zero degrees, and a
    filesystem that could not be stat'ed is not a full disk. Absence travels
    all the way to the screen, where it is drawn as absence.
    """

    name: str
    #: What was read. ``None`` means this Host cannot say.
    value: float | None = None
    unit: str = ""
    #: The ceiling this reading is measured against, when there is one — total
    #: bytes for a filesystem, core count for load. Ratios are computed where
    #: someone reads them, not stored here as a second version of the truth.
    capacity: float | None = None
    #: Why the value is missing, for someone diagnosing it. Never shown as a
    #: measurement.
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("measurement name is required")
        if self.value is None and not self.unavailable_reason:
            raise ValueError(
                f"measurement {self.name!r} has no value and no reason for it"
            )


@dataclass(frozen=True, slots=True)
class HostVitals:
    """How the machine itself is doing, as it can observe from inside.

    Not a verdict. Nothing here decides "healthy" or "degraded": what counts
    as too little disk on a Host that holds one person's memories is a product
    judgement, and it belongs where the product is, not in the daemon that can
    only read /proc.
    """

    observed_at: datetime
    measurements: tuple[Measurement, ...] = ()

    def named(self, name: str) -> Measurement | None:
        for measurement in self.measurements:
            if measurement.name == name:
                return measurement
        return None


@dataclass(frozen=True, slots=True)
class HostServiceState:
    active: bool
    state: str
    detail: str | None = None
    instance_id: str | None = None
    transitioning: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeIntent:
    """One durable, unfinished host operation; independent of readiness."""

    service_id: str
    request_id: str
    action: str
    before_instance_id: str | None
    network_input: str | None

    def __post_init__(self) -> None:
        if self.action not in {"start", "stop", "restart"}:
            raise ValueError(f"unsupported runtime action: {self.action}")

    def completed_by(self, observed: HostServiceState) -> bool:
        if observed.transitioning:
            return False
        if self.action == "stop":
            return not observed.active
        if not observed.active:
            return False
        if self.action == "start":
            return True
        return (observed.instance_id is not None
                and observed.instance_id != self.before_instance_id)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    service_id: str
    required: bool
    desired: DesiredServiceState
    runtime_state: str
    detail: str | None
    observed_at: datetime
    endpoints: tuple[ServiceEndpoint, ...] = ()
    # None means this process has not established the network-input fact.
    network_current: bool | None = None


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
