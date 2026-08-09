"""Strict loader and domain mapping for a sealed target release descriptor."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, ValidationError

_COMPONENT_LINKS = {
    "eidolon_kernel": Path("/srv/eidolon/current/eidolon_kernel"),
    "eidolon_data": Path("/srv/eidolon/current/eidolon_data"),
    "eidolon_hub": Path("/srv/eidolon/current/eidolon_hub"),
    "eidolon_admin": Path("/srv/eidolon/current/eidolon_admin"),
    "eidolon_agent": Path("/srv/eidolon/current/eidolon_agent"),
    "eidolon_channel": Path("/srv/eidolon/current/eidolon_channel"),
    "eidolon_memory": Path("/srv/eidolon/current/eidolon_memory"),
}
V2_COMPONENT_ENTRYPOINTS = {
    "eidolon_kernel": (Path(".venv/bin/eidolond"), Path(".venv/bin/uvicorn")),
    "eidolon_data": (Path(".venv/bin/uvicorn"),),
    "eidolon_hub": (Path(".venv/bin/uvicorn"),),
    "eidolon_admin": (
        Path(".venv/bin/eidolon-admin"),
        Path(".venv/bin/eidolon-bootstrapd"),
        Path(".venv/bin/eidolon-local-api"),
    ),
    "eidolon_agent": (Path(".venv/bin/eidolon-agent"),),
    "eidolon_channel": (Path(".venv/bin/python"),),
    "eidolon_memory": (
        Path(".venv/bin/eidolon-memory-supervisor"),
        Path(".venv/bin/eidolon-memory-discovery"),
    ),
}
V2_SYSTEM_ASSETS = {
    Path("/etc/systemd/system/eidolond.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolond.service"),
    ),
    Path("/etc/systemd/system/eidolon-data.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-data.service"),
    ),
    Path("/etc/systemd/system/eidolon-data-workspace.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-data-workspace.service"),
    ),
    Path("/etc/systemd/system/eidolon-hub.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-hub.service"),
    ),
    Path("/etc/systemd/system/eidolon-kernel.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-kernel.service"),
    ),
    Path("/etc/eidolon/eidolond.yaml"): (
        "eidolon_kernel",
        Path("config/eidolond.systemd.example.yaml"),
    ),
    Path("/etc/eidolon/kernel.yaml"): (
        "eidolon_kernel",
        Path("config/kernel.systemd.example.yaml"),
    ),
    Path("/etc/eidolon/hub.yaml"): (
        "eidolon_kernel",
        Path("config/hub.systemd.example.yaml"),
    ),
    Path("/etc/eidolon/system-services.systemd.example.yaml"): (
        "eidolon_kernel",
        Path("config/system-services.systemd.example.yaml"),
    ),
    Path("/etc/polkit-1/rules.d/60-eidolon-system-manager.rules"): (
        "eidolon_kernel",
        Path("deploy/polkit/60-eidolon-system-manager.rules"),
    ),
    Path("/etc/systemd/system/eidolon-bootstrapd.service"): (
        "eidolon_admin",
        Path("deploy/systemd/eidolon-bootstrapd.service"),
    ),
    Path("/etc/systemd/system/eidolon-local-api.service"): (
        "eidolon_admin",
        Path("deploy/systemd/eidolon-local-api.service"),
    ),
    Path("/etc/systemd/system/eidolon-admin.service"): (
        "eidolon_admin",
        Path("deploy/systemd/eidolon-admin.service"),
    ),
    Path("/etc/polkit-1/rules.d/60-eidolon-bootstrap-network.rules"): (
        "eidolon_admin",
        Path("deploy/polkit/60-eidolon-bootstrap-network.rules"),
    ),
    Path("/etc/avahi/services/eidolon-local-api.service"): (
        "eidolon_admin",
        Path("deploy/avahi/eidolon-local-api.service"),
    ),
    Path("/etc/systemd/system/eidolon-nats.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-nats.service"),
    ),
    Path("/etc/systemd/system/eidolon-livekit.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-livekit.service"),
    ),
    Path("/etc/systemd/system/eidolon-memory-supervisor.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-memory-supervisor.service"),
    ),
    Path("/etc/systemd/system/eidolon-memory-discovery.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-memory-discovery.service"),
    ),
    Path("/etc/systemd/system/eidolon-agent.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-agent.service"),
    ),
    Path("/etc/systemd/system/eidolon-channel.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-channel.service"),
    ),
    Path("/usr/local/libexec/eidolon-livekit-launch"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-livekit-launch"),
    ),
}
V2_REQUIRED_SECRETS = (
    Path("/etc/eidolon/data.env"),
    Path("/etc/eidolon/hub.env"),
    Path("/etc/eidolon/kernel.env"),
    Path("/etc/eidolon/admin.env"),
    Path("/etc/eidolon/local-api.env"),
    Path("/etc/eidolon/bootstrap.env"),
    Path("/var/lib/eidolon-bootstrap/host_identity.ed25519"),
    Path("/etc/eidolon/agent.env"),
    Path("/etc/eidolon/channel.env"),
    Path("/etc/eidolon/memory.env"),
    Path("/etc/eidolon/livekit.env"),
)
V2_AFFECTED_UNITS = (
    "eidolon-admin.service",
    "eidolon-local-api.service",
    "eidolon-bootstrapd.service",
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel.service",
)
V2_READINESS = {
    "eidolond": (
        "unix_http",
        "http://eidolond/health",
        Path("/run/eidolon/system.sock"),
        "ready",
    ),
    "data": ("http", "http://127.0.0.1:8084/health", None, "ready"),
    "data-workspace": ("http", "http://127.0.0.1:8085/health", None, "ready"),
    "hub": ("http", "http://127.0.0.1:8082/health", None, "ok"),
    "kernel": ("http", "http://127.0.0.1:8083/health", None, "ready"),
    "admin": ("http", "http://127.0.0.1:9000/healthz", None, "ready"),
    "local-api": ("https", "https://127.0.0.1:9002/healthz", None, "ok"),
    "nats": ("http", "http://127.0.0.1:8222/healthz", None, "ok"),
    "livekit": ("tcp", "tcp://127.0.0.1:7880", None, "open"),
    "memory": (
        "http",
        "http://127.0.0.1:8020/api/discovery/agent-routing",
        None,
        "http_2xx",
    ),
    "agent": ("http", "http://127.0.0.1:8180/readyz", None, "ready"),
    "channel": (
        "systemd",
        "systemd://eidolon-channel.service",
        None,
        "active",
    ),
}


class ReleaseDescriptorError(ValueError):
    """The release descriptor is unsealed, ambiguous, or unsafe."""


@dataclass(frozen=True, slots=True)
class TargetProfile:
    system: str
    machine: str
    python: str


@dataclass(frozen=True, slots=True)
class ComponentRelease:
    component_id: str
    revision: str
    release_path: Path
    current_link: Path
    source_tree_sha256: str
    lock_sha256: str
    environment_sha256: str
    required_entrypoints: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class SupportSource:
    source_id: str
    revision: str
    release_path: Path
    source_tree_sha256: str


@dataclass(frozen=True, slots=True)
class SystemAsset:
    source_component_id: str
    source: Path
    destination: Path
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class RequiredSecret:
    path: Path
    mode: int


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    check_id: str
    kind: str
    url: str
    socket: Path | None
    expected_status: str


@dataclass(frozen=True, slots=True)
class ReleaseDescriptor:
    schema_version: int
    release_id: str
    target: TargetProfile
    components: tuple[ComponentRelease, ...]
    support_sources: tuple[SupportSource, ...]
    system_assets: tuple[SystemAsset, ...]
    required_secrets: tuple[RequiredSecret, ...]
    affected_units: tuple[str, ...]
    readiness_checks: tuple[ReadinessCheck, ...]
    database_migrations: tuple[str, ...]

    @property
    def components_by_id(self) -> Mapping[str, ComponentRelease]:
        return MappingProxyType({item.component_id: item for item in self.components})


def load_release_descriptor(path: Path) -> ReleaseDescriptor:
    payload = path.read_bytes()
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    try:
        checksum_parts = checksum_path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise ReleaseDescriptorError("release descriptor checksum sidecar is missing") from exc
    if len(checksum_parts) != 2 or checksum_parts[1] != path.name:
        raise ReleaseDescriptorError("release descriptor checksum sidecar is invalid")
    if hashlib.sha256(payload).hexdigest() != checksum_parts[0]:
        raise ReleaseDescriptorError("release descriptor checksum mismatch")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseDescriptorError("release descriptor is not valid JSON") from exc
    return release_descriptor_from_document(document)


def release_descriptor_from_document(document: object) -> ReleaseDescriptor:
    schema = json.loads(
        files("eidolon_deploy.contracts")
        .joinpath("schemas/release-descriptor.schema.json")
        .read_text(encoding="utf-8")
    )
    try:
        Draft202012Validator(schema).validate(document)
    except ValidationError as exc:
        raise ReleaseDescriptorError(
            f"release descriptor contract violation: {exc.message}"
        ) from exc
    assert isinstance(document, dict)
    release_id = str(document["release_id"])
    target_wire = document["target"]
    components = tuple(_component_from_wire(item) for item in document["components"])
    component_ids = [item.component_id for item in components]
    if set(component_ids) != set(_COMPONENT_LINKS) or len(component_ids) != len(set(component_ids)):
        raise ReleaseDescriptorError(
            "release descriptor component set must be the unique reviewed full product set"
        )
    for component in components:
        expected_path = Path("/srv/eidolon/releases") / release_id / component.component_id
        if component.release_path != expected_path:
            raise ReleaseDescriptorError(
                f"component release path must be {expected_path}: {component.component_id}"
            )
        expected_link = _COMPONENT_LINKS[component.component_id]
        if component.current_link != expected_link:
            raise ReleaseDescriptorError(
                f"component current link must be {expected_link}: {component.component_id}"
            )
        for entrypoint in component.required_entrypoints:
            if entrypoint.is_absolute() or ".." in entrypoint.parts:
                raise ReleaseDescriptorError("component entrypoint must stay inside release path")
        if component.required_entrypoints != V2_COMPONENT_ENTRYPOINTS[component.component_id]:
            raise ReleaseDescriptorError(
                f"component entrypoint set is not the fixed V2 set: {component.component_id}"
            )

    support_sources = tuple(
        SupportSource(
            source_id=str(item["source_id"]),
            revision=str(item["revision"]),
            release_path=Path(item["release_path"]),
            source_tree_sha256=str(item["source_tree_sha256"]),
        )
        for item in document["support_sources"]
    )
    expected_support_path = Path("/srv/eidolon/releases") / release_id / "eidolon_sdk"
    if (
        len(support_sources) != 1
        or support_sources[0].source_id != "eidolon_sdk"
        or support_sources[0].release_path != expected_support_path
    ):
        raise ReleaseDescriptorError(
            f"support source must be eidolon_sdk at {expected_support_path}"
        )

    assets = tuple(_asset_from_wire(item) for item in document["system_assets"])
    destinations = [asset.destination for asset in assets]
    if len(destinations) != len(set(destinations)):
        raise ReleaseDescriptorError("system asset destination must be unique")
    if set(destinations) != set(V2_SYSTEM_ASSETS):
        raise ReleaseDescriptorError("system asset set must equal the fixed V2 set")
    for asset in assets:
        if asset.source.is_absolute() or ".." in asset.source.parts:
            raise ReleaseDescriptorError("system asset source must stay inside its component")
        expected_component, expected_source = V2_SYSTEM_ASSETS[asset.destination]
        if asset.source_component_id != expected_component or asset.source != expected_source:
            raise ReleaseDescriptorError(
                f"system asset source mapping is not fixed: {asset.destination}"
            )

    secrets = tuple(
        RequiredSecret(path=Path(item["path"]), mode=int(item["mode"], 8))
        for item in document["required_secrets"]
    )
    if tuple(secret.path for secret in secrets) != V2_REQUIRED_SECRETS:
        raise ReleaseDescriptorError("required secret set must equal the fixed V2 set")

    affected_units = tuple(str(item) for item in document["affected_units"])
    if affected_units != V2_AFFECTED_UNITS:
        raise ReleaseDescriptorError("affected unit set must equal the fixed V2 set")

    readiness = tuple(_readiness_from_wire(item) for item in document["readiness_checks"])
    check_ids = [check.check_id for check in readiness]
    if len(check_ids) != len(set(check_ids)):
        raise ReleaseDescriptorError("readiness check id must be unique")
    if set(check_ids) != set(V2_READINESS) or any(
        (check.kind, check.url, check.socket, check.expected_status) != V2_READINESS[check.check_id]
        for check in readiness
    ):
        raise ReleaseDescriptorError("readiness set must equal the fixed V2 set")

    migrations = tuple(str(item) for item in document["database_migrations"])
    if migrations:
        raise ReleaseDescriptorError(
            "database migration is outside release descriptor V2 rollback semantics"
        )
    return ReleaseDescriptor(
        schema_version=int(document["schema_version"]),
        release_id=release_id,
        target=TargetProfile(
            system=str(target_wire["system"]),
            machine=str(target_wire["machine"]),
            python=str(target_wire["python"]),
        ),
        components=components,
        support_sources=support_sources,
        system_assets=assets,
        required_secrets=secrets,
        affected_units=affected_units,
        readiness_checks=readiness,
        database_migrations=migrations,
    )


def _component_from_wire(value: dict) -> ComponentRelease:
    return ComponentRelease(
        component_id=str(value["component_id"]),
        revision=str(value["revision"]),
        release_path=Path(value["release_path"]),
        current_link=Path(value["current_link"]),
        source_tree_sha256=str(value["source_tree_sha256"]),
        lock_sha256=str(value["lock_sha256"]),
        environment_sha256=str(value["environment_sha256"]),
        required_entrypoints=tuple(Path(item) for item in value["required_entrypoints"]),
    )


def _asset_from_wire(value: dict) -> SystemAsset:
    return SystemAsset(
        source_component_id=str(value["source_component_id"]),
        source=Path(value["source"]),
        destination=Path(value["destination"]),
        sha256=str(value["sha256"]),
        mode=int(value["mode"], 8),
    )


def _readiness_from_wire(value: dict) -> ReadinessCheck:
    kind = str(value["kind"])
    url = str(value["url"])
    socket = Path(value["socket"]) if "socket" in value else None
    parsed = urlparse(url)
    if kind in {"http", "https"}:
        if socket is not None or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ReleaseDescriptorError("HTTP readiness must use loopback without a socket")
        if parsed.scheme != kind:
            raise ReleaseDescriptorError("HTTP readiness scheme must match its kind")
    elif kind == "tcp":
        if (
            socket is not None
            or parsed.scheme != "tcp"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.port is None
        ):
            raise ReleaseDescriptorError("TCP readiness must use one loopback port")
    elif kind == "systemd":
        if (
            socket is not None
            or parsed.scheme != "systemd"
            or parsed.hostname != "eidolon-channel.service"
        ):
            raise ReleaseDescriptorError("systemd readiness must name the fixed Channel unit")
    elif (
        kind != "unix_http"
        or socket != Path("/run/eidolon/system.sock")
        or parsed.hostname != "eidolond"
    ):
        raise ReleaseDescriptorError("Unix HTTP readiness must use the eidolond system socket")
    return ReadinessCheck(
        check_id=str(value["check_id"]),
        kind=kind,
        url=url,
        socket=socket,
        expected_status=str(value["expected_status"]),
    )
