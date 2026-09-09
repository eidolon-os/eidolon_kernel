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

from eidolon_deploy.capabilities import require_known_capabilities

_COMPONENT_LINKS = {
    "eidolon_kernel": Path("/opt/eidolon/current/eidolon_kernel"),
    "eidolon_data": Path("/opt/eidolon/current/eidolon_data"),
    "eidolon_hub": Path("/opt/eidolon/current/eidolon_hub"),
    "eidolon_admin": Path("/opt/eidolon/current/eidolon_admin"),
    "eidolon_agent": Path("/opt/eidolon/current/eidolon_agent"),
    "eidolon_channel": Path("/opt/eidolon/current/eidolon_channel"),
    "eidolon_memory": Path("/opt/eidolon/current/eidolon_memory"),
    # Present only on a Host that declares a capability requiring it; see
    # BASE_COMPONENTS. Linked the same way as the rest when it is present,
    # which is why it is in this table unconditionally.
    "eidolon_models": Path("/opt/eidolon/current/eidolon_models"),
}

#: The components every Host runs, whatever it can do.
BASE_COMPONENTS: frozenset[str] = frozenset(set(_COMPONENT_LINKS) - {"eidolon_models"})

#: What a capability adds to that. Three capabilities name one component
#: because speech in, speech out and the conversation are three things one
#: repository holds; a Host that declares any of them carries it once.
CAPABILITY_COMPONENTS: dict[str, tuple[str, ...]] = {
    "local_asr": ("eidolon_models",),
    "local_tts": ("eidolon_models",),
    "local_llm": ("eidolon_models",),
}
V2_COMPONENT_ENTRYPOINTS = {
    "eidolon_kernel": (
        Path(".venv/bin/eidolond"),
        # eidolon-unit-applier.service ExecStarts this. It is listed for the
        # same reason eidolond is: a release that shipped the unit without
        # the binary would fail at first start, not at activation.
        Path(".venv/bin/eidolon-unit-applier"),
        Path(".venv/bin/uvicorn"),
    ),
    "eidolon_data": (Path(".venv/bin/uvicorn"),),
    "eidolon_hub": (Path(".venv/bin/uvicorn"),),
    "eidolon_admin": (
        Path(".venv/bin/eidolon-admin"),
        Path(".venv/bin/eidolon-bootstrapd"),
        Path(".venv/bin/eidolon-local-api"),
        Path(".venv/bin/eidolon-lifecycle-workflow"),
    ),
    "eidolon_agent": (Path(".venv/bin/eidolon-agent"),),
    "eidolon_channel": (
        Path(".venv/bin/python"),
        Path(".venv/bin/eidolon-channel-provider"),
    ),
    "eidolon_memory": (
        Path(".venv/bin/eidolon-memory-embedder"),
        Path(".venv/bin/eidolon-memory-supervisor"),
        Path(".venv/bin/eidolon-memory-discovery"),
    ),
    # A shell entry point rather than a console script: the ASR service is
    # started through `scripts/eidolon-asr`, which is what its unit ExecStarts.
    "eidolon_models": (Path("scripts/eidolon-asr"),),
}
#: Every file a release writes outside its own release root, and which component
#: it comes from.
#:
#: Hub's settings are deliberately not here. A Hub is started with the per-Host
#: rendering Ops writes to /etc/eidolon/generated/hub.yaml, so a release copy at
#: /etc/eidolon/hub.yaml was read by nothing while still looking authoritative:
#: it carried the template's placeholder hub_id, and an operator who opened it
#: on a working Host read it as evidence that the Host was misconfigured.
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
    # One manifest for every host driver. It used to be a systemd-only copy
    # beside a supervisord-only one that had drifted to a different service set;
    # `host_targets` names both mechanisms, so the copy is gone and the installed
    # file no longer calls itself an example.
    Path("/etc/eidolon/system-services.yaml"): (
        "eidolon_kernel",
        Path("config/system-services.yaml"),
    ),
    # The privilege eidolond does not have. A polkit rule used to grant it
    # `manage-units` directly; it was replaced because a refusal wrote nothing to
    # the journal, so "why did every start fail" had no answer on the Host.
    Path("/etc/systemd/system/eidolon-unit-applier.socket"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-unit-applier.socket"),
    ),
    Path("/etc/systemd/system/eidolon-unit-applier.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-unit-applier.service"),
    ),
    Path("/etc/systemd/system/eidolon-bootstrapd.service"): (
        "eidolon_admin",
        Path("deploy/systemd/eidolon-bootstrapd.service"),
    ),
    Path("/etc/systemd/system/eidolon-local-api.service"): (
        "eidolon_admin",
        Path("deploy/systemd/eidolon-local-api.service"),
    ),
    Path("/etc/systemd/system/eidolon-lifecycle-workflow.service"): (
        "eidolon_admin",
        Path("deploy/systemd/eidolon-lifecycle-workflow.service"),
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
    Path("/etc/systemd/system/eidolon-memory-embedder.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-memory-embedder.service"),
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
    Path("/etc/systemd/system/eidolon-channel-provider.service"): (
        "eidolon_kernel",
        Path("deploy/systemd/eidolon-channel-provider.service"),
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
    "eidolon-lifecycle-workflow.service",
    "eidolon-bootstrapd.service",
    # Ordered here, not with the pre-manager units, so quiesce's reverse sweep
    # reaches them last: the manager is stopped before them, and stopping the
    # socket earlier would propagate a stop into the manager mid-transaction.
    "eidolon-unit-applier.socket",
    "eidolon-unit-applier.service",
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-embedder.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel-provider.service",
    "eidolon-channel.service",
)
#: What a capability adds to the asset, unit and readiness sets. Stated as
#: additions rather than as alternative whole sets so that the baseline stays
#: one reviewed list: a Host that declares nothing gets exactly what every Host
#: got before any of this existed.
CAPABILITY_SYSTEM_ASSETS: dict[str, dict[Path, tuple[str, Path]]] = {
    "local_asr": {
        Path("/etc/systemd/system/eidolon-asr.service"): (
            "eidolon_models",
            Path("deploy/systemd/eidolon-asr.service"),
        ),
    },
    "local_llm": {
        Path("/etc/systemd/system/eidolon-llm.service"): (
            "eidolon_models",
            Path("deploy/systemd/eidolon-llm.service"),
        ),
    },
}

CAPABILITY_AFFECTED_UNITS: dict[str, tuple[str, ...]] = {
    "local_asr": ("eidolon-asr.service",),
    "local_llm": ("eidolon-llm.service",),
}

CAPABILITY_READINESS: dict[str, dict[str, tuple[str, str, Path | None, str]]] = {
    "local_asr": {
        # The service's own readiness route, not a bare socket check. A port
        # that is merely bound says a process started; `/readyz` answers only
        # once the backend has its models open, which is what a release needs
        # to know before it calls itself activated. `http_2xx` because the
        # document reports `ok` rather than a `status` field.
        "asr": ("http", "http://127.0.0.1:8768/readyz", None, "http_2xx"),
    },
    "local_llm": {
        # llama-server's own route, and the same reason: it binds the port
        # before the weights are mapped, and answers /health with 503 until
        # the model is loaded. On the A55 cores that gap is tens of seconds,
        # so a socket check here would call the release activated while the
        # first request would still fail.
        "llm": ("http", "http://127.0.0.1:8769/health", None, "http_2xx"),
    },
}


def expected_components(capabilities: frozenset[str]) -> frozenset[str]:
    """The components a Host with these capabilities runs."""

    return BASE_COMPONENTS.union(
        component
        for capability in capabilities
        for component in CAPABILITY_COMPONENTS.get(capability, ())
    )


def expected_system_assets(capabilities: frozenset[str]) -> dict[Path, tuple[str, Path]]:
    """Every file this release writes outside its own root, for this Host."""

    assets = dict(V2_SYSTEM_ASSETS)
    for capability in sorted(capabilities):
        assets.update(CAPABILITY_SYSTEM_ASSETS.get(capability, {}))
    return assets


def expected_affected_units(capabilities: frozenset[str]) -> tuple[str, ...]:
    """The unit topology, in the one order a cutover may use.

    Order is load-bearing — quiesce sweeps it in reverse — so additions go
    after the baseline and capabilities are applied in sorted order. Two Hosts
    declaring the same capabilities therefore produce the same tuple, and a
    descriptor cannot smuggle a different sequence past this.
    """

    units = list(V2_AFFECTED_UNITS)
    for capability in sorted(capabilities):
        units.extend(CAPABILITY_AFFECTED_UNITS.get(capability, ()))
    return tuple(units)


def expected_readiness(
    capabilities: frozenset[str],
) -> dict[str, tuple[str, str, Path | None, str]]:
    """What must answer before this release is considered started."""

    checks = dict(V2_READINESS)
    for capability in sorted(capabilities):
        checks.update(CAPABILITY_READINESS.get(capability, {}))
    return checks


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
    "lifecycle-workflow": (
        "systemd",
        "systemd://eidolon-lifecycle-workflow.service",
        None,
        "active",
    ),
    "nats": ("http", "http://127.0.0.1:8222/healthz", None, "ok"),
    "livekit": ("tcp", "tcp://127.0.0.1:7880", None, "open"),
    "memory-embedder": (
        "http",
        "http://127.0.0.1:8760/v1/health",
        None,
        "ok",
    ),
    "memory": (
        "http",
        "http://127.0.0.1:8020/api/discovery/agent-routing",
        None,
        "http_2xx",
    ),
    "agent": ("http", "http://127.0.0.1:8180/readyz", None, "ready"),
    "channel-provider": ("http", "http://127.0.0.1:8767/health", None, "ok"),
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
    #: What the Host this release was sealed for can do. Sealed with it, so it
    #: is evidence rather than a parameter: the expected component, asset, unit
    #: and readiness sets are computed from this and compared exactly, and a
    #: descriptor cannot claim a capability and then ship a set that does not
    #: match it.
    capabilities: frozenset[str]
    components: tuple[ComponentRelease, ...]
    support_sources: tuple[SupportSource, ...]
    system_assets: tuple[SystemAsset, ...]
    required_secrets: tuple[RequiredSecret, ...]
    affected_units: tuple[str, ...]
    readiness_checks: tuple[ReadinessCheck, ...]
    cutover_mode: str
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
    try:
        capabilities = require_known_capabilities(document.get("capabilities", []))
    except ValueError as exc:
        raise ReleaseDescriptorError(f"release descriptor capability is invalid: {exc}") from exc
    components = tuple(_component_from_wire(item) for item in document["components"])
    component_ids = [item.component_id for item in components]
    if set(component_ids) != expected_components(capabilities) or len(component_ids) != len(
        set(component_ids)
    ):
        raise ReleaseDescriptorError(
            "release descriptor component set must be the unique reviewed set for these "
            "capabilities"
        )
    for component in components:
        expected_path = Path("/opt/eidolon/releases") / release_id / component.component_id
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
    expected_support_path = Path("/opt/eidolon/releases") / release_id / "eidolon_sdk"
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
    declared_assets = expected_system_assets(capabilities)
    if set(destinations) != set(declared_assets):
        raise ReleaseDescriptorError(
            "system asset set must equal the reviewed set for these capabilities"
        )
    for asset in assets:
        if asset.source.is_absolute() or ".." in asset.source.parts:
            raise ReleaseDescriptorError("system asset source must stay inside its component")
        expected_component, expected_source = declared_assets[asset.destination]
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
    if affected_units != expected_affected_units(capabilities):
        raise ReleaseDescriptorError(
            "affected unit set must equal the reviewed topology for these capabilities"
        )

    readiness = tuple(_readiness_from_wire(item) for item in document["readiness_checks"])
    check_ids = [check.check_id for check in readiness]
    if len(check_ids) != len(set(check_ids)):
        raise ReleaseDescriptorError("readiness check id must be unique")
    declared_readiness = expected_readiness(capabilities)
    if set(check_ids) != set(declared_readiness) or any(
        (check.kind, check.url, check.socket, check.expected_status)
        != declared_readiness[check.check_id]
        for check in readiness
    ):
        raise ReleaseDescriptorError(
            "readiness set must equal the reviewed set for these capabilities"
        )

    migrations = tuple(str(item) for item in document["database_migrations"])
    cutover_mode = str(document["cutover_mode"])
    if migrations:
        raise ReleaseDescriptorError(
            "database migration execution is not declared by release descriptor V2"
        )
    return ReleaseDescriptor(
        schema_version=int(document["schema_version"]),
        capabilities=capabilities,
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
        cutover_mode=cutover_mode,
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
            or parsed.hostname
            not in {
                "eidolon-channel.service",
                "eidolon-lifecycle-workflow.service",
            }
        ):
            raise ReleaseDescriptorError(
                "systemd readiness must name a fixed non-HTTP unit"
            )
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
