"""Build and validate a commit-pinned source bundle for target preparation."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from eidolon_deploy.capabilities import require_known_capabilities
from eidolon_deploy.manifest import expected_system_assets
from eidolon_deploy.sealing import ReleaseRevisions

_BASE_SOURCE_IDS = (
    "eidolon_kernel",
    "eidolon_data",
    "eidolon_hub",
    "eidolon_admin",
    "eidolon_agent",
    "eidolon_channel",
    "eidolon_memory",
    "eidolon_sdk",
)
#: What a capability adds to the sources a bundle carries. Kept out of the
#: baseline rather than always included: eidolon_models holds about 720 MB of
#: committed ASR weights, so a Host that reaches a provider for speech would
#: otherwise ship them in every bundle and never open them.
_CAPABILITY_SOURCE_IDS = {
    "local_asr": ("eidolon_models",),
    "local_tts": ("eidolon_models",),
    "local_llm": ("eidolon_models",),
    "local_laya": ("eidolon_models",),
    "local_laya_participation": ("eidolon_models",),
}
_REVISION_BY_SOURCE = {
    "eidolon_kernel": "kernel",
    "eidolon_data": "data",
    "eidolon_hub": "hub",
    "eidolon_admin": "admin",
    "eidolon_agent": "agent",
    "eidolon_channel": "channel",
    "eidolon_memory": "memory",
    "eidolon_sdk": "sdk",
    "eidolon_models": "models",
}

# These directories contain the Git/LFS-backed model payloads. The service
# code remains in the Python project, but a Host never receives the weights
# for a model it did not select. LLM and Laya weights are separate artifacts
# already selected by their own capabilities.
_MODEL_SOURCE_DIRECTORIES = {
    "asr": "local_asr",
    "tts": "local_tts",
}


#: Carried so components can import it, and not a project of its own: it has
#: no lock and gets no environment built for it.
_SUPPORT_SOURCE_ID = "eidolon_sdk"


def bundle_source_ids(capabilities: frozenset[str]) -> tuple[str, ...]:
    """The sources a bundle for a Host with these capabilities must carry."""

    extra: list[str] = []
    for capability in sorted(capabilities):
        for source_id in _CAPABILITY_SOURCE_IDS.get(capability, ()):
            if source_id not in extra:
                extra.append(source_id)
    return (*_BASE_SOURCE_IDS, *extra)


def bundle_project_ids(capabilities: frozenset[str]) -> tuple[str, ...]:
    """Those of them that are projects: locked, and given an environment.

    Named rather than sliced. This was `_SOURCE_IDS[:-1]`, which meant "all but
    the SDK" only because the SDK happened to be written last — and the first
    conditional source appended after it would have quietly excluded that
    instead, building no environment for a component whose service then falls
    back to resolving its dependencies at start.
    """

    return tuple(
        source_id
        for source_id in bundle_source_ids(capabilities)
        if source_id != _SUPPORT_SOURCE_ID
    )
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREPARER_NAME = "prepare_target.py"
_MANIFEST_NAME = "bundle.json"
_DEPENDENCY_CACHE_NAME = "python-dependencies.tar.gz"
_ARTIFACT_DIRECTORY = "artifacts"
_ARTIFACT_STORE_ENV = "EIDOLON_RELEASE_ARTIFACT_STORE"
_DEPENDENCY_KEY_DIRECTORY = "dependency-keys"
_UV_VERSION = "0.11.15"
_PYTHON_VERSION = "3.13"
_PYTHON_PLATFORM = "aarch64-manylinux_2_40"
_BUILD_REQUIREMENTS = (
    "setuptools==80.9.0",
    "wheel==0.45.1",
    "hatchling==1.27.0",
)
_LFS_POINTER = re.compile(
    rb"\Aversion https://git-lfs.github.com/spec/v1\n"
    rb"oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?\Z"
)
_EXTERNAL_ARTIFACT_POINTER = re.compile(
    rb"\Aeidolon-external-artifact-v1\nsha256 ([0-9a-f]{64})\nsize ([0-9]+)\n\Z"
)
_CHANNEL_MODEL_PATHS = {
    "eidolon/livekit/plugins/eot/data/model/firered_chat_turn_detector/chinese_best_model_q8.onnx",
    "eidolon/livekit/plugins/eot/data/model/firered_chat_turn_detector/multilingual_best_model_q8.onnx",
    "eidolon/livekit/plugins/vad/firered/resources/pvad.onnx",
    "eidolon/livekit/plugins/vad/firered/resources/spkrec-ecapa-voxceleb/classifier.ckpt",
    "eidolon/livekit/plugins/vad/firered/resources/spkrec-ecapa-voxceleb/embedding_model.ckpt",
    "eidolon/livekit/plugins/vad/firered/resources/spkrec-ecapa-voxceleb/label_encoder.ckpt",
    "eidolon/livekit/plugins/vad/firered/resources/spkrec-ecapa-voxceleb/mean_var_norm_emb.ckpt",
    "eidolon/livekit/plugins/speaker_verification/resources/3dspeaker/"
    "campplus_zh_16k_common/campplus_cn_common.bin",
}


class BundleError(RuntimeError):
    """A source revision or bundle invariant failed closed."""


@dataclass(frozen=True, slots=True)
class BundleSource:
    source_id: str
    revision: str
    archive: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SourceBundle:
    release_id: str
    cutover_mode: str
    sources: tuple[BundleSource, ...]
    preparer_sha256: str
    dependency_cache_sha256: str
    artifacts: tuple["BundleArtifact", ...]


@dataclass(frozen=True, slots=True)
class BundleArtifact:
    artifact_id: str
    kind: str
    sha256: str
    size: int
    bundle_path: str
    install_path: str


@dataclass(frozen=True, slots=True)
class BuiltBundle:
    """What was built, and what this workstation had to do to build it.

    ``notes`` describes the build, never the release. Nothing in it can change
    which bytes a target installs — that is fixed by the manifest — so a note
    is reported and the release continues.
    """

    manifest: Path
    notes: tuple[str, ...]


def build_source_bundle(
    *,
    release_id: str,
    repositories: Mapping[str, Path],
    revisions: ReleaseRevisions,
    output: Path,
    git: str = "git",
    uv: str = "uv",
    cutover_mode: str = "reversible",
    capabilities: frozenset[str] | None = None,
) -> BuiltBundle:
    """Archive exact Git commits without reading working-tree content.

    Which sources that is depends on what the Host can do. One component is not
    on every Host, and it holds about 720 MB of committed ASR weights: a Host
    that reaches a provider for speech would otherwise carry them in every
    bundle and never open them.
    """

    if _RELEASE_ID.fullmatch(release_id) is None:
        raise BundleError("release id is invalid")
    if cutover_mode not in {"reversible", "forward-only"}:
        raise BundleError("release cutover mode is invalid")
    declared = frozenset() if capabilities is None else frozenset(capabilities)
    try:
        require_known_capabilities(sorted(declared))
    except ValueError as exc:
        raise BundleError(str(exc)) from exc
    source_ids = bundle_source_ids(declared)
    if set(repositories) != set(source_ids):
        raise BundleError(
            "repository set must be exactly the sources this Host's capabilities pin: "
            + ", ".join(source_ids)
        )
    output = output.resolve()
    if output.exists():
        raise BundleError("bundle output already exists")
    output_parent = output.parent.resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    temporary = output_parent / f".{output.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        source_dir = temporary / "sources"
        source_dir.mkdir()
        bundle_artifact_root = temporary / _ARTIFACT_DIRECTORY / "sha256"
        bundle_artifact_root.mkdir(parents=True)
        persistent_artifact_root = _persistent_artifact_root()
        records: list[dict[str, str]] = []
        artifact_records: list[dict[str, object]] = []
        channel_artifacts: dict[str, dict[str, object]] = {}
        for source_id in source_ids:
            repository = repositories[source_id].resolve()
            if not repository.is_dir():
                raise BundleError(f"repository is missing: {source_id}")
            revision = getattr(revisions, _REVISION_BY_SOURCE[source_id])
            resolved = _git_output(
                git,
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                f"{revision}^{{commit}}",
            ).strip()
            if resolved != revision:
                raise BundleError(f"revision is not the exact commit object: {source_id}")
            archive_relative = f"sources/{source_id}.tar"
            archive = temporary / archive_relative
            model_paths = (
                ("--", ".", *(
                    f":(exclude){directory}"
                    for directory, capability in _MODEL_SOURCE_DIRECTORIES.items()
                    if capability not in declared
                ))
                if source_id == "eidolon_models"
                else ()
            )
            _git_run(
                git,
                "-C",
                str(repository),
                "archive",
                "--format=tar",
                f"--output={archive}",
                revision,
                *model_paths,
            )
            if source_id == "eidolon_channel":
                for record in _externalize_channel_archive(
                    git,
                    repository,
                    archive,
                    bundle_artifact_root=bundle_artifact_root,
                    persistent_artifact_root=persistent_artifact_root,
                ):
                    artifact_records.append(record)
                    channel_artifacts[str(record["install_path"])] = record
            _validate_source_archive(
                source_id,
                archive,
                channel_artifacts=channel_artifacts,
                capabilities=declared,
            )
            records.append(
                {
                    "source_id": source_id,
                    "revision": revision,
                    "archive": archive_relative,
                    "sha256": _file_sha256(archive),
                }
            )

        dependency_cache = temporary / f".{_DEPENDENCY_CACHE_NAME}.tmp"
        notes, dependency_object = _dependency_artifact(
            uv=uv,
            source_dir=source_dir,
            destination=dependency_cache,
            workspace=temporary,
            bundle_artifact_root=bundle_artifact_root,
            persistent_artifact_root=persistent_artifact_root,
            project_ids=bundle_project_ids(declared),
            capabilities=declared,
        )
        artifact_records.append(dependency_object)
        dependency_record = {
            "artifact_id": "python-dependencies",
            "uv_version": _UV_VERSION,
            "python_version": _PYTHON_VERSION,
            "platform": _PYTHON_PLATFORM,
            "build_requirements": list(_BUILD_REQUIREMENTS),
            "index_url": os.environ.get("UV_DEFAULT_INDEX", "https://pypi.org/simple"),
        }

        preparer_source = Path(__file__).with_name(_PREPARER_NAME)
        preparer_destination = temporary / _PREPARER_NAME
        shutil.copyfile(preparer_source, preparer_destination)
        os.chmod(preparer_destination, 0o755)
        document = {
            "schema_version": 3,
            "release_id": release_id,
            "cutover_mode": cutover_mode,
            # Recorded so the bundle says which sources it should hold rather
            # than the reader assuming a fixed list. Absent on bundles built
            # before this field, which is a Host that declares nothing.
            "capabilities": sorted(declared),
            "target": {"system": "linux", "machine": "aarch64"},
            "sources": records,
            "preparer": {
                "path": _PREPARER_NAME,
                "sha256": _file_sha256(preparer_destination),
            },
            "artifacts": sorted(artifact_records, key=lambda item: str(item["artifact_id"])),
            "python_dependencies": dependency_record,
        }
        _atomic_write_json(temporary / _MANIFEST_NAME, document)
        validate_source_bundle(temporary)
        os.replace(temporary, output)
        return BuiltBundle(manifest=output / _MANIFEST_NAME, notes=notes)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def validate_source_bundle(path: Path) -> SourceBundle:
    """Validate the fixed bundle shape and every transferred byte digest."""

    root = path.resolve()
    expected_root = {
        _MANIFEST_NAME,
        _PREPARER_NAME,
        _ARTIFACT_DIRECTORY,
        "sources",
    }
    try:
        if {item.name for item in root.iterdir()} != expected_root:
            raise BundleError("bundle root contains unexpected entries")
    except OSError as exc:
        raise BundleError("bundle root is unreadable") from exc
    try:
        document = json.loads((root / _MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError("bundle manifest is unreadable") from exc
    # `capabilities` is optional so a bundle built before it existed still
    # reads: absent means a Host that declares nothing, which is what those
    # bundles were.
    if not isinstance(document, dict) or set(document) - {"capabilities"} != {
        "schema_version",
        "release_id",
        "cutover_mode",
        "target",
        "sources",
        "preparer",
        "artifacts",
        "python_dependencies",
    }:
        raise BundleError("bundle manifest shape is invalid")
    release_id = document.get("release_id")
    if (
        document.get("schema_version") != 3
        or not isinstance(release_id, str)
        or _RELEASE_ID.fullmatch(release_id) is None
        or document.get("target") != {"system": "linux", "machine": "aarch64"}
        or document.get("cutover_mode") not in {"reversible", "forward-only"}
    ):
        raise BundleError("bundle identity or target is invalid")
    wire_artifacts = document.get("artifacts")
    if not isinstance(wire_artifacts, list) or len(wire_artifacts) != len(
        _CHANNEL_MODEL_PATHS
    ) + 1:
        raise BundleError("bundle artifact set is invalid")
    artifacts: list[BundleArtifact] = []
    channel_artifacts: dict[str, dict[str, object]] = {}
    dependency_artifact: dict[str, object] | None = None
    artifact_ids: set[str] = set()
    expected_object_names: set[str] = set()
    for value in wire_artifacts:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"artifact_id", "kind", "sha256", "size", "bundle_path", "install_path"}
            or not isinstance(value.get("artifact_id"), str)
            or not isinstance(value.get("kind"), str)
            or not isinstance(value.get("sha256"), str)
            or _SHA256.fullmatch(value["sha256"]) is None
            or not isinstance(value.get("size"), int)
            or isinstance(value.get("size"), bool)
            or value["size"] < 0
            or value.get("bundle_path") != f"artifacts/sha256/{value['sha256']}"
            or not isinstance(value.get("install_path"), str)
            or value["artifact_id"] in artifact_ids
        ):
            raise BundleError("bundle artifact record is invalid")
        artifact_ids.add(value["artifact_id"])
        expected_object_names.add(value["sha256"])
        object_path = root / value["bundle_path"]
        if (
            not object_path.is_file()
            or object_path.is_symlink()
            or object_path.stat().st_size != value["size"]
            or _file_sha256(object_path) != value["sha256"]
        ):
            raise BundleError(f"bundle artifact checksum mismatch: {value['artifact_id']}")
        if value["kind"] == "dependency-cache":
            if (
                value["artifact_id"] != "python-dependencies"
                or value["install_path"] != ""
                or dependency_artifact is not None
            ):
                raise BundleError("bundle dependency artifact record is invalid")
            dependency_artifact = value
        elif value["kind"] == "channel-model":
            install_path = value["install_path"]
            if (
                install_path not in _CHANNEL_MODEL_PATHS
                or value["artifact_id"] != f"channel-model:{install_path}"
                or install_path in channel_artifacts
            ):
                raise BundleError("bundle Channel model artifact record is invalid")
            channel_artifacts[install_path] = value
        else:
            raise BundleError("bundle artifact kind is invalid")
        artifacts.append(BundleArtifact(**value))
    artifact_root = root / _ARTIFACT_DIRECTORY / "sha256"
    if (
        dependency_artifact is None
        or set(channel_artifacts) != _CHANNEL_MODEL_PATHS
        or not artifact_root.is_dir()
        or artifact_root.is_symlink()
        or {item.name for item in artifact_root.iterdir()} != expected_object_names
        or any(item.is_symlink() or not item.is_file() for item in artifact_root.iterdir())
    ):
        raise BundleError("bundle artifact directory contains unexpected entries")
    try:
        declared = require_known_capabilities(document.get("capabilities", []))
    except ValueError as exc:
        raise BundleError(str(exc)) from exc
    source_ids = bundle_source_ids(declared)
    wire_sources = document.get("sources")
    if not isinstance(wire_sources, list) or len(wire_sources) != len(source_ids):
        raise BundleError("bundle source set is invalid")
    source_root = root / "sources"
    if (
        not source_root.is_dir()
        or source_root.is_symlink()
        or {item.name for item in source_root.iterdir()}
        != {f"{source_id}.tar" for source_id in source_ids}
    ):
        raise BundleError("bundle sources directory contains unexpected entries")
    sources: list[BundleSource] = []
    for index, source_id in enumerate(source_ids):
        value = wire_sources[index]
        expected_archive = f"sources/{source_id}.tar"
        if (
            not isinstance(value, dict)
            or set(value) != {"source_id", "revision", "archive", "sha256"}
            or value.get("source_id") != source_id
            or value.get("archive") != expected_archive
            or not isinstance(value.get("revision"), str)
            or re.fullmatch(r"[0-9a-f]{40}", value["revision"]) is None
            or not isinstance(value.get("sha256"), str)
            or _SHA256.fullmatch(value["sha256"]) is None
        ):
            raise BundleError(f"bundle source record is invalid: {source_id}")
        archive = root / expected_archive
        if (
            not archive.is_file()
            or archive.is_symlink()
            or _file_sha256(archive) != value["sha256"]
        ):
            raise BundleError(f"bundle source archive checksum mismatch: {source_id}")
        _validate_source_archive(
            source_id, archive, channel_artifacts=channel_artifacts, capabilities=declared
        )
        sources.append(BundleSource(**value))
    preparer = document.get("preparer")
    if (
        not isinstance(preparer, dict)
        or set(preparer) != {"path", "sha256"}
        or preparer.get("path") != _PREPARER_NAME
        or not isinstance(preparer.get("sha256"), str)
        or _SHA256.fullmatch(preparer["sha256"]) is None
    ):
        raise BundleError("bundle target preparer record is invalid")
    preparer_path = root / _PREPARER_NAME
    if (
        not preparer_path.is_file()
        or preparer_path.is_symlink()
        or _file_sha256(preparer_path) != preparer["sha256"]
    ):
        raise BundleError("bundle target preparer checksum mismatch")
    dependency_cache = document.get("python_dependencies")
    if (
        not isinstance(dependency_cache, dict)
        or set(dependency_cache)
        != {
            "artifact_id",
            "uv_version",
            "python_version",
            "platform",
            "build_requirements",
            "index_url",
        }
        or dependency_cache.get("artifact_id") != "python-dependencies"
        or dependency_cache.get("uv_version") != _UV_VERSION
        or dependency_cache.get("python_version") != _PYTHON_VERSION
        or dependency_cache.get("platform") != _PYTHON_PLATFORM
        or dependency_cache.get("build_requirements") != list(_BUILD_REQUIREMENTS)
        or not isinstance(dependency_cache.get("index_url"), str)
        or not dependency_cache["index_url"].startswith("https://")
    ):
        raise BundleError("bundle Python dependency cache record is invalid")
    return SourceBundle(
        release_id=release_id,
        cutover_mode=document["cutover_mode"],
        sources=tuple(sources),
        preparer_sha256=preparer["sha256"],
        dependency_cache_sha256=str(dependency_artifact["sha256"]),
        artifacts=tuple(artifacts),
    )


def _persistent_artifact_root() -> Path | None:
    value = os.environ.get(_ARTIFACT_STORE_ENV, "").strip()
    if not value:
        return None
    root = Path(value)
    if not root.is_absolute():
        raise BundleError(f"{_ARTIFACT_STORE_ENV} must be an absolute path")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise BundleError("release artifact store is unsafe")
    return root


def _artifact_record(
    *,
    artifact_id: str,
    kind: str,
    install_path: str,
    source: Path,
    bundle_artifact_root: Path,
    persistent_artifact_root: Path | None,
) -> dict[str, object]:
    digest = _file_sha256(source)
    size = source.stat().st_size
    stored = source
    if persistent_artifact_root is not None:
        object_root = persistent_artifact_root / "sha256"
        object_root.mkdir(parents=True, exist_ok=True)
        stored = object_root / digest
        _materialize_object(source, stored, digest=digest, size=size)
    bundled = bundle_artifact_root / digest
    _materialize_object(stored, bundled, digest=digest, size=size)
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "sha256": digest,
        "size": size,
        "bundle_path": f"artifacts/sha256/{digest}",
        "install_path": install_path,
    }


def _materialize_object(source: Path, destination: Path, *, digest: str, size: int) -> None:
    if destination.exists() and not destination.is_symlink():
        if (
            destination.is_file()
            and destination.stat().st_size == size
            and _file_sha256(destination) == digest
        ):
            return
        destination.unlink()
    elif destination.is_symlink():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        if temporary.stat().st_size != size or _file_sha256(temporary) != digest:
            raise BundleError("release artifact changed while it was being stored")
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _dependency_artifact(
    *,
    uv: str,
    source_dir: Path,
    destination: Path,
    workspace: Path,
    bundle_artifact_root: Path,
    persistent_artifact_root: Path | None,
    project_ids: tuple[str, ...],
    capabilities: frozenset[str] = frozenset(),
) -> tuple[tuple[str, ...], dict[str, object]]:
    key = _dependency_input_key(source_dir, project_ids, capabilities)
    if persistent_artifact_root is not None:
        record_path = persistent_artifact_root / _DEPENDENCY_KEY_DIRECTORY / f"{key}.json"
        cached = _read_dependency_key(record_path, persistent_artifact_root, key)
        if cached is not None:
            source = persistent_artifact_root / "sha256" / cached["sha256"]
            record = _artifact_record(
                artifact_id="python-dependencies",
                kind="dependency-cache",
                install_path="",
                source=source,
                bundle_artifact_root=bundle_artifact_root,
                persistent_artifact_root=persistent_artifact_root,
            )
            return ("reused dependency artifact for unchanged locked inputs",), record

    notes = _build_dependency_cache(
        uv=uv,
        source_dir=source_dir,
        destination=destination,
        workspace=workspace,
        project_ids=project_ids,
        capabilities=capabilities,
    )
    record = _artifact_record(
        artifact_id="python-dependencies",
        kind="dependency-cache",
        install_path="",
        source=destination,
        bundle_artifact_root=bundle_artifact_root,
        persistent_artifact_root=persistent_artifact_root,
    )
    destination.unlink()
    if persistent_artifact_root is not None:
        record_path = persistent_artifact_root / _DEPENDENCY_KEY_DIRECTORY / f"{key}.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            record_path,
            {
                "schema_version": 1,
                "input_sha256": key,
                "sha256": record["sha256"],
                "size": record["size"],
            },
        )
    return notes, record


def _dependency_input_key(
    source_dir: Path, project_ids: tuple[str, ...], capabilities: frozenset[str] = frozenset()
) -> str:
    digest = hashlib.sha256()
    inputs = {
        "schema_version": 1,
        "uv_version": _UV_VERSION,
        "python_version": _PYTHON_VERSION,
        "platform": _PYTHON_PLATFORM,
        "build_requirements": list(_BUILD_REQUIREMENTS),
        "index_url": os.environ.get("UV_DEFAULT_INDEX", "https://pypi.org/simple"),
        # A pre-partition artifact may contain wheels from a different model
        # selection even with the same locks. Never reuse it for this release.
        "cache_scope": "model-capabilities-v1",
        "capabilities": sorted(capabilities),
    }
    digest.update(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode())
    for source_id in project_ids:
        archive_path = source_dir / f"{source_id}.tar"
        digest.update(source_id.encode())
        with tarfile.open(archive_path, "r:") as archive:
            members = {member.name.removeprefix("./"): member for member in archive.getmembers()}
            for name in ("pyproject.toml", "uv.lock", "uv.toml"):
                digest.update(name.encode())
                member = members.get(name)
                if member is None or not member.isfile():
                    digest.update(b"\0missing\0")
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise BundleError(f"dependency input is unreadable: {source_id}/{name}")
                digest.update(stream.read())
    return digest.hexdigest()


def _read_dependency_key(path: Path, root: Path, key: str) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("input_sha256") != key
        or not isinstance(value.get("sha256"), str)
        or _SHA256.fullmatch(value["sha256"]) is None
        or not isinstance(value.get("size"), int)
        or isinstance(value.get("size"), bool)
        or value["size"] < 0
    ):
        return None
    object_path = root / "sha256" / value["sha256"]
    if (
        not object_path.is_file()
        or object_path.is_symlink()
        or object_path.stat().st_size != value["size"]
        or _file_sha256(object_path) != value["sha256"]
    ):
        return None
    return value


def _build_dependency_cache(
    *,
    uv: str,
    source_dir: Path,
    destination: Path,
    workspace: Path,
    project_ids: tuple[str, ...],
    capabilities: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    version = _dependency_run((uv, "--version")).stdout.strip()
    if version != f"uv {_UV_VERSION}" and not version.startswith(f"uv {_UV_VERSION} "):
        raise BundleError(f"dependency cache requires uv {_UV_VERSION}, got {version}")
    projects = workspace / ".python-projects"
    environments = workspace / ".python-environments"
    kept_root = _kept_dependency_cache()
    kept = (
        _profile_dependency_cache(kept_root, capabilities)
        if kept_root is not None else None
    )
    notes: tuple[str, ...] = ()
    if kept is not None:
        notes = _bind_kept_cache_to_its_location(kept)
    cache = kept if kept is not None else workspace / ".python-dependency-cache"
    cache.mkdir(parents=True, exist_ok=True)
    projects.mkdir()
    environments.mkdir()
    seed = environments / "build-requirements"
    common_environment = os.environ.copy()
    common_environment["UV_CACHE_DIR"] = str(cache)
    _dependency_run(
        (uv, "venv", "--python", _PYTHON_VERSION, "--no-python-downloads", str(seed)),
        env=common_environment,
    )
    _dependency_run(
        (
            uv,
            "pip",
            "install",
            "--python",
            str(seed / "bin/python"),
            "--python-platform",
            _PYTHON_PLATFORM,
            "--no-python-downloads",
            *_BUILD_REQUIREMENTS,
        ),
        env=common_environment,
    )
    shutil.rmtree(seed)
    for source_id in project_ids:
        project = projects / source_id
        project.mkdir()
        with tarfile.open(source_dir / f"{source_id}.tar", "r:") as archive:
            archive.extractall(project, filter="data")
        project_environment = environments / source_id
        environment = common_environment.copy()
        environment["UV_PROJECT_ENVIRONMENT"] = str(project_environment)
        command = [
            uv,
            "sync",
            "--project",
            str(projects / source_id),
            "--frozen",
            "--no-dev",
            "--no-python-downloads",
            "--no-install-project",
            "--no-install-local",
            # Both halves of the target ABI must be pinned. With only the
            # platform pinned, uv resolves against whatever interpreter the
            # workstation happens to offer, and the cache silently fills with
            # wheels the target cannot use while the manifest still claims
            # this Python version.
            "--python",
            _PYTHON_VERSION,
            "--python-platform",
            _PYTHON_PLATFORM,
        ]
        if source_id == "eidolon_data":
            command.extend(("--extra", "api"))
        if source_id == "eidolon_models":
            if "local_asr" in capabilities:
                command.extend(("--extra", "asr"))
            if {"local_laya", "local_laya_participation"} & capabilities:
                command.extend(("--extra", "laya"))
        _dependency_run(tuple(command), env=environment)
        shutil.rmtree(project_environment)
        shutil.rmtree(project)
    _archive_dependency_cache(cache, destination)
    if kept is None:
        shutil.rmtree(cache)
    shutil.rmtree(projects)
    shutil.rmtree(environments)
    return notes


#: Where this machine keeps the dependencies it has already fetched, so that a
#: release costs the index only what actually changed.
#:
#: It is a location rather than a copy on purpose. uv records the entries under
#: ``wheels-v6`` as absolute symlinks into ``archive-v0``, so a cache is bound
#: to the path it was built at: move or clone one and every pointer in it goes
#: dangling. The cache therefore stays put and each build works in it directly.
#:
#: This cannot change what a release installs. That is fixed by each source's
#: lockfile, which pins exact versions and hashes uv verifies on use; a cache
#: only decides whether those bytes come off local disk or the network. Each
#: selected model capability set gets its own cache below this root, so an ASR
#: build cannot leave ASR wheels in a later Laya-only release archive. Leave
#: the variable unset to build from an empty disk and prove that.
KEPT_DEPENDENCY_CACHE_ENV = "EIDOLON_RELEASE_UV_CACHE"

_MODEL_CACHE_CAPABILITIES = frozenset(
    {"local_asr", "local_tts", "local_llm", "local_laya", "local_laya_participation"}
)


def _profile_dependency_cache(root: Path, capabilities: frozenset[str]) -> Path:
    """Keep different model selections from contaminating each other's cache."""

    selected = sorted(capabilities & _MODEL_CACHE_CAPABILITIES)
    name = "-".join(selected) if selected else "baseline"
    return root / "profiles" / name


def _kept_dependency_cache() -> Path | None:
    value = os.environ.get(KEPT_DEPENDENCY_CACHE_ENV, "").strip()
    if not value:
        return None
    cache = Path(value)
    if not cache.is_absolute():
        raise BundleError(f"{KEPT_DEPENDENCY_CACHE_ENV} must be an absolute path")
    return cache


#: The absolute path a kept cache was built at, recorded inside the cache.
#:
#: The comment on ``KEPT_DEPENDENCY_CACHE_ENV`` states the invariant — a uv
#: cache is bound to the path it was built at — but for a long time nothing
#: established it. A cache that had been moved or copied from another root
#: still looked like a cache: every ``wheels-v6`` entry pointed at an
#: ``archive-v0`` under the *old* absolute root, so the first symptom appeared
#: hundreds of lines away, while packaging, as a dangling link that could not
#: name what had actually gone wrong. This file lets the cache say where it
#: belongs, so a relocated one is recognised as relocated the moment it is
#: opened.
_CACHE_ROOT_MARKER = ".eidolon-cache-root"

#: The uv cache subtrees a release carries. Named once because two places now
#: depend on this layout — what gets packaged, and where a moved entry's file
#: is found — and a second copy of the list is how they would come apart.
_CACHE_ROOTS = frozenset({"archive-v0", "wheels-v6", "sdists-v9"})


def _bind_kept_cache_to_its_location(cache: Path) -> tuple[str, ...]:
    """Make the kept cache be this location's cache, repairing a moved one.

    A relocated cache is not damaged, only mis-addressed: uv writes the
    ``wheels-v6`` entries as absolute symlinks into ``archive-v0``, and when the
    directory moves, the files those links name move with it. So the entries can
    be pointed back at the copies that are right here, which costs nothing,
    rather than re-fetched, which costs the whole cache. Only a link whose file
    did not come along is dropped, and uv fetches that one entry again.

    Nothing here can change what a release installs — that is fixed by each
    source's lockfile, whose hashes uv verifies on use — so a mis-addressed
    cache is never a reason to refuse a release.
    """

    marker = cache / _CACHE_ROOT_MARKER
    expected = str(cache)
    if not cache.exists():
        cache.mkdir(parents=True)
        marker.write_text(expected + "\n", encoding="utf-8")
        return ()
    recorded = None
    if marker.is_file() and not marker.is_symlink():
        recorded = marker.read_text(encoding="utf-8").strip()
    if recorded == expected:
        return ()
    entries = [item for item in cache.iterdir() if item.name != _CACHE_ROOT_MARKER]
    if not entries:
        marker.write_text(expected + "\n", encoding="utf-8")
        return ()
    rebased, dropped = _readdress_cache_entries(cache)
    marker.write_text(expected + "\n", encoding="utf-8")
    if not rebased and not dropped:
        return ()
    origin = recorded if recorded else "an unrecorded path"
    return (
        f"the kept uv cache at {expected} was built at {origin}: uv records its "
        f"wheel entries as absolute symlinks, so {rebased + dropped} pointed "
        f"outside this root. {rebased} were re-addressed to the copies present "
        f"here and {dropped} whose files did not come along were dropped for uv "
        "to fetch again; the release content is unaffected.",
    )


def _readdress_cache_entries(cache: Path) -> tuple[int, int]:
    """Point every entry addressed outside this cache at its copy inside it."""

    rebased = 0
    dropped = 0
    for path in sorted(cache.rglob("*")):
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        if not target.is_absolute() or cache == target or cache in target.parents:
            continue
        suffix = _cache_relative_suffix(target)
        landing = None if suffix is None else cache / suffix
        # A target is only ever replaced with a file that is actually here.
        # Nothing is invented: an entry whose file is absent is removed, which
        # is exactly what uv needs to fetch it once.
        if landing is None or not landing.exists():
            path.unlink()
            dropped += 1
            continue
        path.unlink()
        path.symlink_to(landing)
        rebased += 1
    return rebased, dropped


def _cache_relative_suffix(target: Path) -> Path | None:
    """Where inside a uv cache an absolute target names, by the cache's layout."""

    parts = target.parts
    for index in reversed(range(len(parts))):
        part = parts[index]
        if part in _CACHE_ROOTS or part.startswith("simple-v"):
            return Path(*parts[index:])
    return None


def _archive_dependency_cache(cache: Path, destination: Path) -> None:
    # A content-addressed object must describe its content, not the wall clock
    # on which it was packed.  tarfile's w:gz writes the current time into the
    # gzip header and preserves cache mtimes, which made byte-identical locked
    # inputs produce a new 500 MB object for every release.
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", dereference=False) as archive:
                for path in sorted(cache.rglob("*")):
                    relative = path.relative_to(cache)
                    if relative.parts[0] not in _CACHE_ROOTS and not relative.parts[
                        0
                    ].startswith("simple-v"):
                        continue
                    info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if info.issym():
                        # Check the link this archive actually ships, not the absolute
                        # target it happens to have on disk. The two differ, and only
                        # the shipped one decides whether extraction can write outside
                        # the target's cache.
                        target = path.resolve(strict=True)
                        linkname = os.path.relpath(target, path.parent)
                        landing = os.path.normpath(
                            os.path.join(str(relative.parent), linkname)
                        )
                        if landing == os.pardir or landing.startswith(os.pardir + os.sep):
                            raise BundleError(
                                "uv dependency cache entry would extract outside the "
                                f"cache: {relative.as_posix()} -> {target}"
                            )
                        info.linkname = linkname
                        archive.addfile(info)
                    elif info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    elif info.isdir():
                        archive.addfile(info)
                    else:
                        raise BundleError("uv dependency cache contains an unsafe entry")


def _dependency_run(
    command: tuple[str, ...], *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError(f"ARM64 dependency preparation could not run: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise BundleError(f"ARM64 dependency preparation failed: {detail}")
    return result


def _validate_source_archive(
    source_id: str,
    archive: Path,
    *,
    channel_artifacts: Mapping[str, Mapping[str, object]] | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> None:
    required = {"pyproject.toml"}
    if source_id != _SUPPORT_SOURCE_ID:
        required.add("uv.lock")
    if source_id == "eidolon_channel":
        required.update(_CHANNEL_MODEL_PATHS)
    for _destination, (component_id, source) in expected_system_assets(capabilities).items():
        if component_id == source_id:
            required.add(source.as_posix())
    try:
        with tarfile.open(archive, "r:") as stream:
            members = stream.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise BundleError(f"source archive is unreadable: {source_id}") from exc
    names: set[str] = set()
    lfs_pointers: set[str] = set()
    external_pointers: dict[str, tuple[str, int]] = {}
    with tarfile.open(archive, "r:") as stream:
        member_by_name = {
            PurePosixPath(member.name).as_posix().removeprefix("./"): member for member in members
        }
        for path in _CHANNEL_MODEL_PATHS if source_id == "eidolon_channel" else ():
            member = member_by_name.get(path)
            if member is None or not member.isfile() or member.size > 1024:
                continue
            source = stream.extractfile(member)
            if source is None:
                continue
            value = source.read(1025)
            lfs = _LFS_POINTER.fullmatch(value)
            external = _EXTERNAL_ARTIFACT_POINTER.fullmatch(value)
            if lfs is not None:
                lfs_pointers.add(path)
            elif external is not None:
                external_pointers[path] = (
                    external.group(1).decode("ascii"),
                    int(external.group(2)),
                )
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
            raise BundleError(f"source archive has unsafe member: {source_id}")
        normalized = name.as_posix().removeprefix("./")
        names.add(normalized)
    missing = sorted(required - names)
    if missing:
        raise BundleError(f"source archive is incomplete: {source_id}: {', '.join(missing)}")
    if source_id == "eidolon_models":
        for directory, capability in _MODEL_SOURCE_DIRECTORIES.items():
            present = any(
                name == directory or name.startswith(f"{directory}/") for name in names
            )
            if present != (capability in capabilities):
                raise BundleError(
                    f"model source archive selection mismatch: {directory} requires {capability}"
                )
    if lfs_pointers:
        raise BundleError("Channel model artifact is missing or is an unhydrated LFS pointer")
    if source_id == "eidolon_channel" and channel_artifacts is not None:
        if set(channel_artifacts) != _CHANNEL_MODEL_PATHS or set(external_pointers) != set(
            channel_artifacts
        ):
            raise BundleError("Channel source archive does not reference its exact artifacts")
        for path, (digest, size) in external_pointers.items():
            record = channel_artifacts[path]
            if record.get("sha256") != digest or record.get("size") != size:
                raise BundleError(f"Channel artifact pointer drifted: {path}")


def _externalize_channel_archive(
    git: str,
    repository: Path,
    archive: Path,
    *,
    bundle_artifact_root: Path,
    persistent_artifact_root: Path | None,
) -> list[dict[str, object]]:
    """Replace every exact-commit model with a digest-bound small pointer.

    A Git LFS pointer is smudged from that exact commit and verified against
    its own oid/size.  A repository used by a test or an older checkout may
    contain the bytes directly; those bytes are read from ``git archive``, not
    from the working tree.  Either way, the large object is stored once and
    the release archive carries a pointer that target preparation must replace.
    """

    with tempfile.TemporaryDirectory(prefix="eidolon-artifacts-", dir=archive.parent) as raw:
        stage = Path(raw)
        hydrated: dict[str, Path] = {}
        with tarfile.open(archive, "r:") as source:
            for member in source.getmembers():
                normalized = PurePosixPath(member.name).as_posix().removeprefix("./")
                if normalized not in _CHANNEL_MODEL_PATHS or not member.isfile():
                    continue
                stream = source.extractfile(member)
                if stream is None:
                    raise BundleError(f"Channel model archive member is unreadable: {normalized}")
                value = stream.read()
                destination = stage / f"model-{len(hydrated)}"
                if _LFS_POINTER.fullmatch(value) is not None:
                    _smudge_lfs_pointer(git, repository, normalized, value, destination)
                else:
                    destination.write_bytes(value)
                hydrated[normalized] = destination
        if set(hydrated) != _CHANNEL_MODEL_PATHS:
            missing = ", ".join(sorted(_CHANNEL_MODEL_PATHS - set(hydrated)))
            raise BundleError(f"Channel source archive is missing model artifacts: {missing}")

        records: list[dict[str, object]] = []
        pointers: dict[str, bytes] = {}
        for relative, source in sorted(hydrated.items()):
            record = _artifact_record(
                artifact_id=f"channel-model:{relative}",
                kind="channel-model",
                install_path=relative,
                source=source,
                bundle_artifact_root=bundle_artifact_root,
                persistent_artifact_root=persistent_artifact_root,
            )
            records.append(record)
            pointers[relative] = (
                "eidolon-external-artifact-v1\n"
                f"sha256 {record['sha256']}\n"
                f"size {record['size']}\n"
            ).encode("ascii")

        rewritten = stage / "channel.tar"
        with tarfile.open(archive, "r:") as source, tarfile.open(rewritten, "w:") as output:
            for member in source.getmembers():
                normalized = PurePosixPath(member.name).as_posix().removeprefix("./")
                pointer = pointers.get(normalized)
                if pointer is not None:
                    updated = copy.copy(member)
                    updated.size = len(pointer)
                    with tempfile.SpooledTemporaryFile() as stream:
                        stream.write(pointer)
                        stream.seek(0)
                        output.addfile(updated, stream)
                elif member.isfile():
                    stream = source.extractfile(member)
                    if stream is None:
                        raise BundleError("Channel source archive member cannot be read")
                    output.addfile(member, stream)
                else:
                    output.addfile(member)
        os.replace(rewritten, archive)
        return records


def _smudge_lfs_pointer(
    git: str,
    repository: Path,
    relative: str,
    pointer: bytes,
    destination: Path,
) -> None:
    match = _LFS_POINTER.fullmatch(pointer)
    if match is None:
        raise BundleError(f"Channel model has an invalid LFS pointer: {relative}")
    expected_sha256 = match.group(1).decode("ascii")
    expected_size = int(match.group(2))
    try:
        with destination.open("xb") as output:
            result = subprocess.run(
                (git, "-C", str(repository), "lfs", "smudge", "--", relative),
                check=False,
                input=pointer,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=1800,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError(f"Channel LFS model hydration could not run: {relative}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or "no diagnostic output"
        raise BundleError(f"Channel LFS model hydration failed: {relative}: {detail}")
    if destination.stat().st_size != expected_size or _file_sha256(destination) != expected_sha256:
        raise BundleError(f"Channel LFS model hydration digest mismatch: {relative}")


def _git_output(*command: str) -> str:
    return _git_run(*command).stdout


def _git_run(*command: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError(f"Git archive operation could not run: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise BundleError(f"Git archive operation failed: {detail}")
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, document: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
