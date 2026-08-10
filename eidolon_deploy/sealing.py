"""Seal a native, already-prepared target release into a fixed descriptor."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from eidolon_deploy.contract import (
    ACTIVATOR_RELATIVE_PATH,
    ACTIVATOR_SOURCE_RELATIVE_PATH,
    INTERPRETER_RELATIVE_PATH,
    INTERPRETER_SOURCE_RELATIVE_PATH,
)
from eidolon_deploy.fingerprints import (
    INSTALLED_DISTRIBUTIONS_SCRIPT,
    environment_sha256,
    source_tree_sha256,
)
from eidolon_deploy.manifest import (
    V2_AFFECTED_UNITS,
    V2_COMPONENT_ENTRYPOINTS,
    V2_READINESS,
    V2_REQUIRED_SECRETS,
    V2_SYSTEM_ASSETS,
    release_descriptor_from_document,
)

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RELEASES = Path("/opt/eidolon/releases")


class PreparationError(RuntimeError):
    """The target release is incomplete or cannot be sealed safely."""


@dataclass(frozen=True, slots=True)
class ReleaseRevisions:
    kernel: str
    data: str
    hub: str
    admin: str
    agent: str
    channel: str
    memory: str
    sdk: str

    def __post_init__(self) -> None:
        if any(
            _REVISION.fullmatch(value) is None
            for value in (
                self.kernel,
                self.data,
                self.hub,
                self.admin,
                self.agent,
                self.channel,
                self.memory,
                self.sdk,
            )
        ):
            raise PreparationError("each release revision must be a full lowercase Git object id")


@dataclass(frozen=True, slots=True)
class EnvironmentFacts:
    python_version: str
    freeze_output: str


class EnvironmentInspector(Protocol):
    def inspect(self, python: Path) -> EnvironmentFacts: ...


class SubprocessEnvironmentInspector:
    """Inspect a prepared venv with fixed, shell-free Python commands."""

    def inspect(self, python: Path) -> EnvironmentFacts:
        version = self._run(
            python,
            "-c",
            "import platform; print('.'.join(platform.python_version_tuple()[:2]))",
        ).strip()
        freeze = self._run(python, "-c", INSTALLED_DISTRIBUTIONS_SCRIPT)
        return EnvironmentFacts(python_version=version, freeze_output=freeze)

    @staticmethod
    def _run(*command: Path | str) -> str:
        try:
            result = subprocess.run(
                tuple(str(item) for item in command),
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreparationError(f"environment inspection could not run: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise PreparationError(f"environment inspection failed: {detail}")
        return result.stdout


def seal_prepared_release(
    *,
    release_id: str,
    revisions: ReleaseRevisions,
    host_root: Path = Path("/"),
    inspector: EnvironmentInspector | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> Path:
    """Validate and seal the fixed Eidolon OS V2 release layout on its target."""

    if _RELEASE_ID.fullmatch(release_id) is None:
        raise PreparationError("release id is invalid")
    actual_system = (system or platform.system()).lower()
    actual_machine = (machine or platform.machine()).lower()
    if actual_system != "linux" or actual_machine != "aarch64":
        raise PreparationError(
            f"release must be sealed on the linux/aarch64 target host, got "
            f"{actual_system}/{actual_machine}"
        )

    root = host_root.resolve()
    canonical_root = _RELEASES / release_id
    release_root = _host_path(root, canonical_root)
    kernel = release_root / "eidolon_kernel"
    data = release_root / "eidolon_data"
    hub = release_root / "eidolon_hub"
    admin = release_root / "eidolon_admin"
    agent = release_root / "eidolon_agent"
    channel = release_root / "eidolon_channel"
    memory = release_root / "eidolon_memory"
    sdk = release_root / "eidolon_sdk"
    for source in (kernel, data, hub, admin, agent, channel, memory, sdk):
        if not source.is_dir():
            raise PreparationError(f"release source directory is missing: {source.name}")

    environment_inspector = inspector or SubprocessEnvironmentInspector()
    component_inputs = (
        (
            "eidolon_kernel",
            revisions.kernel,
            kernel,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_kernel"]),
        ),
        (
            "eidolon_data",
            revisions.data,
            data,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_data"]),
        ),
        (
            "eidolon_hub",
            revisions.hub,
            hub,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_hub"]),
        ),
        (
            "eidolon_admin",
            revisions.admin,
            admin,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_admin"]),
        ),
        (
            "eidolon_agent",
            revisions.agent,
            agent,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_agent"]),
        ),
        (
            "eidolon_channel",
            revisions.channel,
            channel,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_channel"]),
        ),
        (
            "eidolon_memory",
            revisions.memory,
            memory,
            tuple(str(item) for item in V2_COMPONENT_ENTRYPOINTS["eidolon_memory"]),
        ),
    )
    components: list[dict[str, object]] = []
    python_version: str | None = None
    for component_id, revision, source, entrypoints in component_inputs:
        lock = source / "uv.lock"
        if not lock.is_file():
            raise PreparationError(f"component lock is missing: {component_id}")
        python = source / ".venv/bin/python"
        if not python.is_file() or not os.access(python, os.X_OK):
            raise PreparationError(f"component venv is incomplete: {component_id}")
        for entrypoint in entrypoints:
            executable = source / entrypoint
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise PreparationError(
                    f"component entrypoint is missing or not executable: "
                    f"{component_id}/{entrypoint}"
                )
        environment = environment_inspector.inspect(python)
        if python_version is None:
            python_version = environment.python_version
        elif environment.python_version != python_version:
            raise PreparationError("component Python versions do not match")
        components.append(
            {
                "component_id": component_id,
                "revision": revision,
                "release_path": str(canonical_root / component_id),
                "current_link": f"/opt/eidolon/current/{component_id}",
                "source_tree_sha256": source_tree_sha256(source),
                "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "environment_sha256": environment_sha256(environment.freeze_output),
                "required_entrypoints": list(entrypoints),
            }
        )
    assert python_version is not None

    assets: list[dict[str, object]] = []
    component_roots = {"eidolon_kernel": kernel, "eidolon_admin": admin}
    for destination, (source_component_id, source_value) in V2_SYSTEM_ASSETS.items():
        source = component_roots[source_component_id] / source_value
        if not source.is_file() or source.is_symlink():
            raise PreparationError(f"system asset source is missing: {source_value}")
        assets.append(
            {
                "source_component_id": source_component_id,
                "source": str(source_value),
                "destination": str(destination),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "mode": "0644",
            }
        )

    document = {
        "schema_version": 2,
        "release_id": release_id,
        "target": {
            "system": "linux",
            "machine": "aarch64",
            "python": python_version,
        },
        "components": components,
        "support_sources": [
            {
                "source_id": "eidolon_sdk",
                "revision": revisions.sdk,
                "release_path": str(canonical_root / "eidolon_sdk"),
                "source_tree_sha256": source_tree_sha256(sdk),
            }
        ],
        "system_assets": assets,
        "required_secrets": [{"path": str(path), "mode": "0600"} for path in V2_REQUIRED_SECRETS],
        "affected_units": list(V2_AFFECTED_UNITS),
        "readiness_checks": [
            {
                "check_id": check_id,
                "kind": values[0],
                "url": values[1],
                **({"socket": str(values[2])} if values[2] is not None else {}),
                "expected_status": values[3],
            }
            for check_id, values in V2_READINESS.items()
        ],
        "database_migrations": [],
    }
    release_descriptor_from_document(document)
    descriptor_path = release_root / "release.json"
    checksum_path = descriptor_path.with_suffix(".json.sha256")
    if descriptor_path.exists() or checksum_path.exists():
        raise PreparationError("release descriptor is already sealed")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _publish_activator(release_root)
    _atomic_write(descriptor_path, payload)
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {descriptor_path.name}\n".encode()
    _atomic_write(checksum_path, checksum)
    return descriptor_path


def _publish_activator(release_root: Path) -> None:
    """Expose the operator entries at component-neutral paths in the release.

    Operator tooling resolves the activator and its interpreter here, so which
    component ships them stays an internal detail of this repository.
    """

    published = (
        (ACTIVATOR_SOURCE_RELATIVE_PATH, ACTIVATOR_RELATIVE_PATH),
        (INTERPRETER_SOURCE_RELATIVE_PATH, INTERPRETER_RELATIVE_PATH),
    )
    for source_relative, link_relative in published:
        source = release_root / source_relative
        if not source.is_file() or not os.access(source, os.X_OK):
            raise PreparationError(f"release operator entry is missing: {source}")
        link = release_root / link_relative
        link.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.symlink_to(source)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)


def _host_path(root: Path, value: Path) -> Path:
    if root == Path("/"):
        return value
    return root / value.relative_to("/")


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
