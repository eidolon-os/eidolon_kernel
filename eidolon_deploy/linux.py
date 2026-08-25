"""Linux host adapter for fail-closed, offline release activation."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import platform
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from eidolon_deploy.activation import ActivationReceipt, receipt_to_document
from eidolon_deploy.fingerprints import (
    INSTALLED_DISTRIBUTIONS_SCRIPT,
    environment_sha256,
    source_tree_sha256,
)
from eidolon_deploy.manifest import ReadinessCheck, ReleaseDescriptor
from eidolon_deploy.ports import DeploymentSnapshot

_SYSTEMCTL = "/usr/bin/systemctl"
_SYSTEMD_ANALYZE = "/usr/bin/systemd-analyze"
_SYSTEMD_STOP_ATTEMPTS = 3
_MANAGER_UNIT = "eidolond.service"
_PRE_MANAGER_UNITS = (
    "eidolon-local-api.service",
    "eidolon-lifecycle-workflow.service",
    "eidolon-admin.service",
    "eidolon-bootstrapd.service",
)
_POST_MANAGER_UNITS = (
    "eidolon-admin.service",
    "eidolon-lifecycle-workflow.service",
    "eidolon-local-api.service",
)
_READINESS_UNITS = {
    "eidolond": _MANAGER_UNIT,
    "data": "eidolon-data.service",
    "data-workspace": "eidolon-data-workspace.service",
    "hub": "eidolon-hub.service",
    "kernel": "eidolon-kernel.service",
    "admin": "eidolon-admin.service",
    "local-api": "eidolon-local-api.service",
    "lifecycle-workflow": "eidolon-lifecycle-workflow.service",
    "nats": "eidolon-nats.service",
    "livekit": "eidolon-livekit.service",
    "memory": "eidolon-memory-discovery.service",
    "agent": "eidolon-agent.service",
    "channel-provider": "eidolon-channel-provider.service",
    "channel": "eidolon-channel.service",
}
_RELEASE_UNITS = (
    "eidolon-bootstrapd.service",
    _MANAGER_UNIT,
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-local-api.service",
    "eidolon-lifecycle-workflow.service",
    "eidolon-admin.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel-provider.service",
    "eidolon-channel.service",
)
_SNAPSHOT_ROOT = Path("/var/lib/eidolon/deployments")
_ACTIVATION_LOCK = Path("/run/lock/eidolon-release.lock")
_BOOTSTRAP_DATABASE = Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3")
_BOOTSTRAP_SCHEMA_VERSION_SCRIPT = (
    "from eidolon_admin_server.bootstrap.adapters.persistence import sqlite as store;"
    "print(store.BOOTSTRAP_SCHEMA_VERSION "
    "if hasattr(store, 'BOOTSTRAP_SCHEMA_VERSION') else store._SCHEMA_VERSION)"
)
_SQLITE_USER_VERSION_SCRIPT = (
    "import sqlite3,sys;"
    "connection=sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True);"
    "print(connection.execute('PRAGMA user_version').fetchone()[0]);"
    "connection.close()"
)
#: The development commissioning registry is an ops-installed input, not part
#: of a sealed release, so its format version and the Hub that reads it move
#: independently. Ask the release's own Hub which format it reads, the same way
#: the bootstrap gate asks Admin for its schema version.
_COMMISSIONING_REGISTRY = Path("/etc/eidolon/commissioning-secrets.json")
_COMMISSIONING_PROFILE_SCRIPT = (
    "from hub.composition import resources;"
    "print(resources.DEVELOPMENT_COMMISSIONING_REGISTRY_PROFILE)"
)
_COMMISSIONING_FILE_PROFILE_SCRIPT = (
    "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8')).get('profile') or '')"
)
#: Lowest bootstrap schema version any release can still migrate forward.
#: Admin walks one ordered ladder indexed from v1, so every stamped version at
#: or above this has a path to the release's own version and needs no gate
#: here. Zero is the value that does need one: it means the database file
#: exists but nothing ever stamped it, and admin would answer that by creating
#: tables which may already be there. That is a broken host, not a migration.
_MIGRATABLE_BOOTSTRAP_SCHEMA = 1
_SNAPSHOT_SCHEMA_VERSION = 2
_TOPOLOGY_EXPANSION_COMPONENTS = frozenset(
    {
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
    }
)


class LinuxDeploymentError(RuntimeError):
    """A host invariant or fixed host operation failed."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, *command: str) -> CommandResult: ...


class SubprocessRunner:
    """Shell-free runner used only for fixed deployment commands."""

    def run(self, *command: str) -> CommandResult:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path) -> None:
        super().__init__("localhost", timeout=2)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self._socket_path))
        self.sock = connection


class LinuxDeploymentHost:
    """Owns Linux filesystem and systemd mechanics behind the deployment port."""

    def __init__(
        self,
        *,
        root: Path = Path("/"),
        runner: CommandRunner | None = None,
        system: str | None = None,
        machine: str | None = None,
        readiness_probe: Callable[[ReadinessCheck], bool] | None = None,
        # Long enough for the slowest component this activator has to wait for,
        # on the slowest hardware it activates. The Channel worker alone spends
        # sixteen seconds loading models on a Pi 5 and about a hundred from stop
        # to serving; thirty made that unreachable, so a healthy release failed
        # its own gate and the rollback failed the same way. The loop exits as
        # soon as every check passes, so a generous ceiling costs nothing when
        # the host is fast.
        readiness_timeout_seconds: float = 300.0,
        readiness_interval_seconds: float = 0.25,
        require_root: bool = True,
    ) -> None:
        self._root = root.resolve()
        self._runner = runner or SubprocessRunner()
        self._system = (system or platform.system()).lower()
        self._machine = (machine or platform.machine()).lower()
        self._readiness_probe = readiness_probe or self._probe_readiness
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._readiness_interval_seconds = readiness_interval_seconds
        self._require_root = require_root
        self._transaction_paths: dict[str, Path] = {}

    @contextmanager
    def exclusive_activation(self):
        """Fail fast when another release transaction owns the host lock."""

        self._assert_privileged()
        path = self._host_path(_ACTIVATION_LOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LinuxDeploymentError(
                    "another release activation is already in progress"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def preflight(self, release: ReleaseDescriptor) -> Mapping[str, str]:
        if release.target.system != self._system or release.target.machine != self._machine:
            raise LinuxDeploymentError(
                "target profile mismatch: "
                f"expected {release.target.system}/{release.target.machine}, "
                f"host is {self._system}/{self._machine}"
            )

        previous_targets: dict[str, str] = {}
        for support_source in release.support_sources:
            support_path = self._host_path(support_source.release_path)
            if (
                not support_path.is_dir()
                or source_tree_sha256(support_path) != support_source.source_tree_sha256
            ):
                raise LinuxDeploymentError(
                    f"support source tree fingerprint mismatch: {support_source.source_id}"
                )
        for component in release.components:
            release_path = self._host_path(component.release_path)
            if not release_path.is_dir():
                raise LinuxDeploymentError(
                    f"component release directory is missing: {component.component_id}"
                )
            if source_tree_sha256(release_path) != component.source_tree_sha256:
                raise LinuxDeploymentError(
                    f"component source tree fingerprint mismatch: {component.component_id}"
                )
            self._verify_file_digest(
                release_path / "uv.lock",
                component.lock_sha256,
                f"component lock fingerprint mismatch: {component.component_id}",
            )
            python = release_path / ".venv/bin/python"
            if not python.is_file() or not os.access(python, os.X_OK):
                raise LinuxDeploymentError(
                    f"component virtual environment is incomplete: {component.component_id}"
                )
            version = self._checked_command(
                "Python version probe",
                str(python),
                "-c",
                "import platform; print('.'.join(platform.python_version_tuple()[:2]))",
            ).stdout.strip()
            if version != release.target.python:
                raise LinuxDeploymentError(
                    f"component Python version mismatch: {component.component_id}"
                )
            freeze = self._checked_command(
                "Python environment inspection",
                str(python),
                "-c",
                INSTALLED_DISTRIBUTIONS_SCRIPT,
            ).stdout
            if environment_sha256(freeze) != component.environment_sha256:
                raise LinuxDeploymentError(
                    f"component environment fingerprint mismatch: {component.component_id}"
                )
            for entrypoint in component.required_entrypoints:
                executable = release_path / entrypoint
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    raise LinuxDeploymentError(
                        f"component entrypoint is missing or not executable: "
                        f"{component.component_id}/{entrypoint}"
                    )

            current_link = self._host_path(component.current_link)
            if not current_link.is_symlink():
                if (
                    component.component_id in _TOPOLOGY_EXPANSION_COMPONENTS
                    and not current_link.exists()
                ):
                    continue
                raise LinuxDeploymentError(
                    f"component current link is missing or not a symlink: {component.component_id}"
                )
            previous_target = os.readlink(current_link)
            self._validate_component_target(
                component.component_id,
                current_link,
                Path(previous_target),
            )
            previous_targets[component.component_id] = previous_target

        if not self._valid_previous_targets(release, previous_targets):
            raise LinuxDeploymentError("component current links are a partial topology expansion")

        components = release.components_by_id
        current_admin = self._validate_component_target(
            "eidolon_admin",
            self._host_path(components["eidolon_admin"].current_link),
            Path(previous_targets["eidolon_admin"]),
        )
        self._verify_bootstrap_schema_compatibility(
            current_admin=current_admin,
            release_admin=self._host_path(components["eidolon_admin"].release_path),
            # The sealed descriptor is the only statement of intent that
            # reaches a host with no network: the operator picks the mode at
            # bundle time and `seal` writes it here, checksummed. Reading it
            # rather than assuming "reversible" is the whole difference between
            # a schema advance that can land and one that cannot.
            cutover_mode=release.cutover_mode,
        )
        self._verify_commissioning_registry_readable(
            release_hub=self._host_path(components["eidolon_hub"].release_path),
        )
        service_sources: list[str] = []
        for asset in release.system_assets:
            source = (
                self._host_path(components[asset.source_component_id].release_path) / asset.source
            )
            self._verify_file_digest(
                source,
                asset.sha256,
                f"system asset fingerprint mismatch: {asset.destination}",
            )
            if asset.destination.parent == Path("/etc/systemd/system"):
                service_sources.append(str(source))
        for secret in release.required_secrets:
            path = self._host_path(secret.path)
            if not path.is_file() or path.is_symlink():
                raise LinuxDeploymentError(f"required secret is missing: {secret.path}")
            actual_mode = stat.S_IMODE(path.stat().st_mode)
            if actual_mode != secret.mode:
                raise LinuxDeploymentError(
                    f"required secret mode mismatch: {secret.path} "
                    f"is {actual_mode:04o}, expected {secret.mode:04o}"
                )
        if service_sources:
            # Units intentionally execute through stable /opt/eidolon/current
            # links.  During a topology expansion the current release cannot
            # contain a newly introduced entrypoint yet, so systemd-analyze
            # would reject a valid candidate before activation.  Verify an
            # ephemeral copy whose current links point at this candidate; the
            # digests above still authenticate the original shipped assets.
            with tempfile.TemporaryDirectory(prefix="eidolon-systemd-verify-") as temporary:
                candidate_sources: list[str] = []
                replacements = tuple(
                    (str(component.current_link), str(component.release_path))
                    for component in release.components
                )
                for source_value in service_sources:
                    source = Path(source_value)
                    rendered = source.read_text(encoding="utf-8")
                    for current_link, release_path in replacements:
                        rendered = rendered.replace(current_link, release_path)
                    candidate = Path(temporary) / source.name
                    candidate.write_text(rendered, encoding="utf-8")
                    candidate_sources.append(str(candidate))
                self._checked_command(
                    "systemd unit verification",
                    _SYSTEMD_ANALYZE,
                    "verify",
                    *candidate_sources,
                )
        return previous_targets

    def create_snapshot(
        self,
        release: ReleaseDescriptor,
        previous_targets: Mapping[str, str],
    ) -> DeploymentSnapshot:
        self._assert_privileged()
        transaction_id = uuid.uuid4().hex
        backup_path = self._host_path(_SNAPSHOT_ROOT) / (f"{release.release_id}-{transaction_id}")
        backup_path.mkdir(parents=True, mode=0o700)
        os.chmod(backup_path, 0o700)

        asset_states: list[dict[str, object]] = []
        for asset in release.system_assets:
            destination = self._host_path(asset.destination)
            existed = destination.is_file() and not destination.is_symlink()
            if destination.exists() and not existed:
                raise LinuxDeploymentError(
                    f"system asset destination is not a regular file: {asset.destination}"
                )
            if existed:
                destination_stat = destination.stat()
                backup = backup_path / "assets" / asset.destination.relative_to("/")
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
                uid: int | None = destination_stat.st_uid
                gid: int | None = destination_stat.st_gid
            else:
                uid = None
                gid = None
            asset_states.append(
                {
                    "destination": str(asset.destination),
                    "existed": existed,
                    "uid": uid,
                    "gid": gid,
                }
            )

        metadata = {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION,
            "release_id": release.release_id,
            "transaction_id": transaction_id,
            "previous_targets": dict(previous_targets),
            "system_assets": asset_states,
        }
        self._atomic_write_json(backup_path / "snapshot.json", metadata, mode=0o600)
        self._transaction_paths[transaction_id] = backup_path
        return DeploymentSnapshot(
            transaction_id=transaction_id,
            previous_targets=dict(previous_targets),
            backup_path=str(backup_path),
        )

    def quiesce(self, release: ReleaseDescriptor) -> None:
        self._assert_privileged()
        affected = set(release.affected_units)
        for unit in _PRE_MANAGER_UNITS:
            if unit in affected:
                self._stop_unit_if_loaded(unit, operation="service stop")
        self._stop_unit_if_loaded(_MANAGER_UNIT, operation="manager stop")
        # Stop consumers before the services they keep alive.  In particular,
        # Channel Provider holds a LiveKit participant; stopping LiveKit first
        # can exhaust TimeoutStopSec and trigger Restart=on-failure, canceling
        # the release's stop transaction.  Descriptor order is startup order,
        # so quiesce it in reverse after the manager is gone.
        for unit in reversed(release.affected_units):
            if unit not in _PRE_MANAGER_UNITS:
                self._stop_unit_if_loaded(unit, operation="service stop")

    def install_assets(self, release: ReleaseDescriptor) -> None:
        self._assert_privileged()
        components = release.components_by_id
        for asset in release.system_assets:
            source = (
                self._host_path(components[asset.source_component_id].release_path) / asset.source
            )
            destination = self._host_path(asset.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.chmod(temporary, asset.mode)
                if self._root == Path("/"):
                    os.chown(temporary, 0, 0)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def switch_components(self, release: ReleaseDescriptor) -> None:
        self._assert_privileged()
        for component in release.components:
            link = self._host_path(component.current_link)
            target = self._host_path(component.release_path)
            link.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_symlink(target, link)

    def reload_systemd(self) -> None:
        self._assert_privileged()
        self._checked_command("systemd reload", _SYSTEMCTL, "daemon-reload")

    def start_release(self, release: ReleaseDescriptor) -> None:
        self._assert_privileged()
        self._checked_command("bootstrap start", _SYSTEMCTL, "start", "eidolon-bootstrapd.service")
        self._checked_command("manager start", _SYSTEMCTL, "start", _MANAGER_UNIT)
        for unit in _POST_MANAGER_UNITS:
            if unit in release.affected_units:
                self._checked_command("service start", _SYSTEMCTL, "start", unit)

    def wait_ready(self, release: ReleaseDescriptor) -> None:
        self._wait_for_readiness(release.readiness_checks)

    def _wait_for_readiness(self, checks: tuple[ReadinessCheck, ...]) -> None:
        deadline = time.monotonic() + self._readiness_timeout_seconds
        pending = {check.check_id: check for check in checks}
        while pending:
            for check_id, check in tuple(pending.items()):
                try:
                    ready = self._readiness_probe(check)
                except OSError:
                    ready = False
                if ready:
                    pending.pop(check_id)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise LinuxDeploymentError("readiness timeout: " + ", ".join(sorted(pending)))
            time.sleep(self._readiness_interval_seconds)

    def doctor(self, release: ReleaseDescriptor) -> Mapping[str, object]:
        """Validate the active release without mutating host state."""

        current_targets = dict(self.preflight(release))
        expected_components = {component.component_id for component in release.components}
        if set(current_targets) != expected_components:
            raise LinuxDeploymentError("active release is missing a component current link")
        for component in release.components:
            current = self._validate_component_target(
                component.component_id,
                self._host_path(component.current_link),
                Path(current_targets[component.component_id]),
            )
            expected = self._host_path(component.release_path).resolve()
            if current != expected:
                raise LinuxDeploymentError(
                    f"component is not active release: {component.component_id}"
                )
        for unit in _RELEASE_UNITS:
            self._checked_command(
                "systemd active-state verification",
                _SYSTEMCTL,
                "is-active",
                "--quiet",
                unit,
            )
        self.wait_ready(release)
        return {
            "release_id": release.release_id,
            "active_targets": current_targets,
            "units": _RELEASE_UNITS,
            "readiness_checks": tuple(check.check_id for check in release.readiness_checks),
        }

    def restore(self, release: ReleaseDescriptor, snapshot: DeploymentSnapshot) -> None:
        self._assert_privileged()
        backup_path = Path(snapshot.backup_path)
        metadata = self._read_snapshot_document(release, snapshot)
        self.quiesce(release)

        for state in metadata["system_assets"]:
            destination_value = Path(state["destination"])
            destination = self._host_path(destination_value)
            if state["existed"]:
                backup = backup_path / "assets" / destination_value.relative_to("/")
                if not backup.is_file():
                    raise LinuxDeploymentError(
                        f"system asset backup is missing: {destination_value}"
                    )
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copy2(backup, temporary)
                    self._restore_file_ownership(
                        temporary,
                        state["uid"],
                        state["gid"],
                    )
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                destination.unlink(missing_ok=True)

        for component in release.components:
            previous = snapshot.previous_targets.get(component.component_id)
            current_link = self._host_path(component.current_link)
            if previous is None:
                if component.component_id not in _TOPOLOGY_EXPANSION_COMPONENTS:
                    raise LinuxDeploymentError(
                        f"snapshot has no previous component target: {component.component_id}"
                    )
                self._validate_expansion_current(
                    component.component_id,
                    current_link,
                    self._host_path(component.release_path),
                )
                current_link.unlink()
                continue
            self._validate_component_target(
                component.component_id,
                current_link,
                Path(previous),
            )
            self._atomic_symlink(Path(previous), current_link)
        self.reload_systemd()
        restored_units = self._restored_units(metadata)
        self._start_restored_release(restored_units)
        self._wait_for_readiness(
            tuple(
                check
                for check in release.readiness_checks
                if _READINESS_UNITS[check.check_id] in restored_units
            )
        )

    def write_receipt(self, receipt: ActivationReceipt) -> None:
        if receipt.transaction_id is None:
            raise LinuxDeploymentError("activation receipt has no transaction id")
        backup_path = self._transaction_paths.get(receipt.transaction_id)
        if backup_path is None:
            raise LinuxDeploymentError("activation transaction is unknown to this host")
        self._atomic_write_json(
            backup_path / "receipt.json",
            receipt_to_document(receipt),
            mode=0o600,
        )

    def load_snapshot(
        self,
        release: ReleaseDescriptor,
        backup_path: Path,
    ) -> DeploymentSnapshot:
        """Load a snapshot for an explicit rollback in a later operator process."""

        resolved = backup_path.resolve()
        snapshot_root = self._host_path(_SNAPSHOT_ROOT).resolve()
        if resolved.parent != snapshot_root:
            raise LinuxDeploymentError("deployment snapshot is outside the fixed snapshot root")
        try:
            document = json.loads((resolved / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LinuxDeploymentError("deployment snapshot metadata is unreadable") from exc
        transaction_id = document.get("transaction_id")
        previous_targets = document.get("previous_targets")
        if (
            document.get("schema_version") != _SNAPSHOT_SCHEMA_VERSION
            or document.get("release_id") != release.release_id
            or not isinstance(transaction_id, str)
            or not isinstance(previous_targets, dict)
            or not self._valid_previous_targets(release, previous_targets)
        ):
            raise LinuxDeploymentError("deployment snapshot identity or shape is invalid")
        snapshot = DeploymentSnapshot(
            transaction_id=transaction_id,
            previous_targets=dict(previous_targets),
            backup_path=str(resolved),
        )
        self._read_snapshot_document(release, snapshot)
        self._transaction_paths[transaction_id] = resolved
        return snapshot

    def _host_path(self, value: Path) -> Path:
        if not value.is_absolute():
            raise LinuxDeploymentError(f"host path must be absolute: {value}")
        if self._root == Path("/"):
            return value
        return self._root / value.relative_to("/")

    def _assert_privileged(self) -> None:
        if self._require_root and os.geteuid() != 0:
            raise LinuxDeploymentError("release activation requires root privileges")

    def _verify_file_digest(self, path: Path, expected: str, message: str) -> None:
        if not path.is_file() or path.is_symlink():
            raise LinuxDeploymentError(message)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise LinuxDeploymentError(message)

    def _validate_component_target(
        self,
        component_id: str,
        link: Path,
        target: Path,
    ) -> Path:
        resolved = target if target.is_absolute() else link.parent / target
        resolved = resolved.resolve(strict=False)
        releases_root = (link.parent.parent / "releases").resolve()
        if resolved.name != component_id or resolved.parent.parent != releases_root:
            raise LinuxDeploymentError(
                f"component current target is outside release namespace: {component_id}"
            )
        if not resolved.is_dir():
            raise LinuxDeploymentError(
                f"component current target directory is missing: {component_id}"
            )
        return resolved

    def _validate_expansion_current(
        self,
        component_id: str,
        current_link: Path,
        release_path: Path,
    ) -> None:
        if not current_link.is_symlink():
            raise LinuxDeploymentError(
                f"new component current link is missing or unsafe: {component_id}"
            )
        current = self._validate_component_target(
            component_id,
            current_link,
            Path(os.readlink(current_link)),
        )
        if current != release_path.resolve():
            raise LinuxDeploymentError(
                f"new component current target does not match failed release: {component_id}"
            )

    def _read_snapshot_document(
        self,
        release: ReleaseDescriptor,
        snapshot: DeploymentSnapshot,
    ) -> dict:
        backup_path = Path(snapshot.backup_path).resolve()
        snapshot_root = self._host_path(_SNAPSHOT_ROOT).resolve()
        if backup_path.parent != snapshot_root:
            raise LinuxDeploymentError("deployment snapshot is outside the fixed snapshot root")
        try:
            document = json.loads((backup_path / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LinuxDeploymentError("deployment snapshot metadata is unreadable") from exc
        previous_targets = document.get("previous_targets")
        asset_states = document.get("system_assets")
        if (
            document.get("schema_version") != _SNAPSHOT_SCHEMA_VERSION
            or document.get("release_id") != release.release_id
            or document.get("transaction_id") != snapshot.transaction_id
            or previous_targets != dict(snapshot.previous_targets)
            or not isinstance(previous_targets, dict)
            or not self._valid_previous_targets(release, previous_targets)
            or not isinstance(asset_states, list)
        ):
            raise LinuxDeploymentError("deployment snapshot identity or shape is invalid")
        expected_destinations = {str(item.destination) for item in release.system_assets}
        actual_destinations: set[str] = set()
        for state in asset_states:
            existed = state.get("existed") if isinstance(state, dict) else None
            uid = state.get("uid") if isinstance(state, dict) else None
            gid = state.get("gid") if isinstance(state, dict) else None
            if (
                not isinstance(state, dict)
                or set(state) != {"destination", "existed", "uid", "gid"}
                or not isinstance(state.get("destination"), str)
                or not isinstance(existed, bool)
                or (
                    existed and (type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0)
                )
                or (not existed and (uid is not None or gid is not None))
            ):
                raise LinuxDeploymentError("deployment snapshot asset state is invalid")
            actual_destinations.add(state["destination"])
            if state["existed"]:
                destination = Path(state["destination"])
                backup = backup_path / "assets" / destination.relative_to("/")
                if not backup.is_file():
                    raise LinuxDeploymentError(f"system asset backup is missing: {destination}")
        if actual_destinations != expected_destinations or len(asset_states) != len(
            expected_destinations
        ):
            raise LinuxDeploymentError("deployment snapshot asset set is invalid")
        for component in release.components:
            previous = previous_targets.get(component.component_id)
            if previous is None:
                self._validate_expansion_current(
                    component.component_id,
                    self._host_path(component.current_link),
                    self._host_path(component.release_path),
                )
                continue
            self._validate_component_target(
                component.component_id,
                self._host_path(component.current_link),
                Path(previous),
            )
        return document

    @staticmethod
    def _valid_previous_targets(
        release: ReleaseDescriptor,
        previous_targets: object,
    ) -> bool:
        if not isinstance(previous_targets, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in previous_targets.items()
        ):
            return False
        expected = {component.component_id for component in release.components}
        required = expected - _TOPOLOGY_EXPANSION_COMPONENTS
        actual = set(previous_targets)
        return actual == required or actual == expected

    def _restore_file_ownership(self, path: Path, uid: int, gid: int) -> None:
        """Restore captured ownership on the real host before atomic replacement."""

        if self._root == Path("/"):
            os.chown(path, uid, gid)

    @staticmethod
    def _restored_units(metadata: Mapping[str, object]) -> frozenset[str]:
        assets = metadata["system_assets"]
        assert isinstance(assets, list)
        return frozenset(
            Path(state["destination"]).name
            for state in assets
            if state["existed"] and Path(state["destination"]).parent == Path("/etc/systemd/system")
        )

    def _start_restored_release(self, restored_units: frozenset[str]) -> None:
        if "eidolon-bootstrapd.service" in restored_units:
            self._checked_command(
                "bootstrap start",
                _SYSTEMCTL,
                "start",
                "eidolon-bootstrapd.service",
            )
        if _MANAGER_UNIT in restored_units:
            self._checked_command("manager start", _SYSTEMCTL, "start", _MANAGER_UNIT)
        for unit in _POST_MANAGER_UNITS:
            if unit in restored_units:
                self._checked_command("service start", _SYSTEMCTL, "start", unit)

    def _checked_command(self, operation: str, *command: str) -> CommandResult:
        try:
            result = self._runner.run(*command)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LinuxDeploymentError(f"{operation} could not run: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise LinuxDeploymentError(f"{operation} failed: {detail}")
        return result

    def _verify_bootstrap_schema_compatibility(
        self,
        *,
        current_admin: Path,
        release_admin: Path,
        cutover_mode: str,
    ) -> None:
        """Prove this release can read the bootstrap authority state on disk.

        The two directions are not the same event and must not share an answer.
        Advancing the schema is what a release carrying a bootstrap migration
        is *for*; refusing it is how a shipped v6->v7 Grant migration became
        unshippable, rejected on two consecutive installs — under
        ``forward-only`` as well — while the migration that would have fixed
        the "reinstalled phone can never reclaim its Host" dead end sat unused
        inside the candidate. Going backwards is the direction that actually
        strands persistent state: admin refuses to open a database stamped
        above its own version, so the old interpreter would crash-loop after
        the links had already been switched. That direction stays refused.
        """

        database = self._host_path(_BOOTSTRAP_DATABASE)
        if not database.is_file() or database.is_symlink():
            raise LinuxDeploymentError(
                f"bootstrap authority database is missing or unsafe: {_BOOTSTRAP_DATABASE}"
            )
        current_version = self._bootstrap_code_schema_version(current_admin)
        release_version = self._bootstrap_code_schema_version(release_admin)
        database_version = self._schema_version_from_command(
            "bootstrap database schema inspection",
            str(release_admin / ".venv/bin/python"),
            "-c",
            _SQLITE_USER_VERSION_SCRIPT,
            str(database),
        )
        if release_version < current_version:
            raise LinuxDeploymentError(
                "bootstrap schema rollback would strand persistent authority state: "
                f"current code expects {current_version}, release expects {release_version}, "
                f"database is stamped {database_version}. This release cannot migrate the "
                "authority database downwards; activate a release at schema "
                f"{current_version} or newer instead"
            )
        if release_version > current_version and cutover_mode != "forward-only":
            raise LinuxDeploymentError(
                "bootstrap schema advance requires a forward-only release: "
                f"current code expects {current_version}, release expects {release_version}. "
                "This release migrates the authority database on its first start and no "
                "snapshot can undo that, so seal and deploy it with "
                "--cutover-mode forward-only"
            )
        if not _MIGRATABLE_BOOTSTRAP_SCHEMA <= database_version <= release_version:
            raise LinuxDeploymentError(
                "bootstrap authority database is outside this release's migration ladder: "
                f"database is {database_version}, release migrates "
                f"{_MIGRATABLE_BOOTSTRAP_SCHEMA} through {release_version}"
            )

    def _verify_commissioning_registry_readable(self, *, release_hub: Path) -> None:
        """Prove this release's Hub can read the registry already on disk.

        Unlike the bootstrap database, this file is an ops-installed input: it
        is replaced by ``install``/``converge-inputs`` and is not part of any
        sealed release, so the file's format and the Hub that reads it move on
        separate schedules. Both directions of that gap are the same failure —
        a Hub that cannot read it exits during application startup, after the
        links have already been switched, and restarts forever. So this is
        asked once, before switching anything, and there is nothing to refuse
        in only one direction: either this release's Hub reads the file that is
        there, or activation stops here with both profiles named.

        Absence is not a failure. The registry is development-only; a
        production Host has no file and Hub installs the rejecting verifier.
        """

        registry = self._host_path(_COMMISSIONING_REGISTRY)
        if not registry.is_file() or registry.is_symlink():
            return
        python = release_hub / ".venv/bin/python"
        if not python.is_file() or not os.access(python, os.X_OK):
            raise LinuxDeploymentError(
                f"commissioning registry probe runtime is unavailable: {python}"
            )
        accepted = self._checked_command(
            "commissioning registry profile inspection",
            str(python),
            "-c",
            _COMMISSIONING_PROFILE_SCRIPT,
        ).stdout.strip()
        installed = self._checked_command(
            "installed commissioning registry inspection",
            str(python),
            "-c",
            _COMMISSIONING_FILE_PROFILE_SCRIPT,
            str(registry),
        ).stdout.strip()
        if not accepted:
            raise LinuxDeploymentError(
                "release Hub does not state which commissioning registry profile it reads"
            )
        if installed != accepted:
            raise LinuxDeploymentError(
                "installed commissioning registry is not one this release's Hub reads: "
                f"{_COMMISSIONING_REGISTRY} states {installed or 'no profile'}, this "
                f"release's Hub reads {accepted}. Converge the Host's inputs from a "
                "workstation registry in that format, or activate a release whose Hub "
                f"reads {installed or 'that format'}"
            )

    def _bootstrap_code_schema_version(self, admin_root: Path) -> int:
        python = admin_root / ".venv/bin/python"
        if not python.is_file() or not os.access(python, os.X_OK):
            raise LinuxDeploymentError(f"bootstrap schema probe runtime is unavailable: {python}")
        return self._schema_version_from_command(
            "bootstrap code schema inspection",
            str(python),
            "-c",
            _BOOTSTRAP_SCHEMA_VERSION_SCRIPT,
        )

    def _schema_version_from_command(self, operation: str, *command: str) -> int:
        value = self._checked_command(operation, *command).stdout.strip()
        if not value.isascii() or not value.isdecimal():
            raise LinuxDeploymentError(f"{operation} returned an invalid version")
        return int(value)

    def _stop_unit_if_loaded(self, unit: str, *, operation: str) -> None:
        state = self._checked_command(
            "systemd unit load-state inspection",
            _SYSTEMCTL,
            "show",
            "--property=LoadState",
            "--value",
            unit,
        ).stdout.strip()
        if state == "not-found":
            return
        if not state:
            raise LinuxDeploymentError(f"systemd returned an empty load state for {unit}")
        for attempt in range(_SYSTEMD_STOP_ATTEMPTS):
            result = self._runner.run(_SYSTEMCTL, "stop", unit)
            if result.returncode == 0:
                return
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            # A manager/reconciler may have queued a competing systemd job just
            # before it became inactive.  systemd reports that race as a
            # canceled stop transaction.  Retry the same declarative stop;
            # never infer success and never broaden the unit set.
            if "Job for " not in detail or " canceled" not in detail:
                raise LinuxDeploymentError(f"{operation} failed: {detail}")
            if attempt + 1 < _SYSTEMD_STOP_ATTEMPTS:
                time.sleep(0.2)
        raise LinuxDeploymentError(
            f"{operation} failed after {_SYSTEMD_STOP_ATTEMPTS} attempts: {detail}"
        )

    @staticmethod
    def _atomic_symlink(target: Path, link: Path) -> None:
        temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_write_json(path: Path, document: object, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _probe_readiness(check: ReadinessCheck) -> bool:
        parsed = urlsplit(check.url)
        if check.kind == "tcp":
            try:
                with socket.create_connection((str(parsed.hostname), int(parsed.port)), timeout=2):
                    return True
            except OSError:
                return False
        if check.kind == "systemd":
            try:
                result = subprocess.run(
                    (_SYSTEMCTL, "is-active", "--quiet", str(parsed.hostname)),
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return result.returncode == 0
        if check.kind == "unix_http":
            assert check.socket is not None
            connection: http.client.HTTPConnection = _UnixHTTPConnection(check.socket)
        elif check.kind == "https":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
                context=context,
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=2,
            )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            connection.request("GET", path)
            response = connection.getresponse()
            payload = response.read()
            if response.status != 200:
                return False
            if check.expected_status == "http_2xx":
                return True
            document = json.loads(payload)
            return isinstance(document, dict) and document.get("status") == check.expected_status
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            return False
        finally:
            connection.close()
