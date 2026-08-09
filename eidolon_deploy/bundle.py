"""Build and validate a commit-pinned source bundle for target preparation."""

from __future__ import annotations

import copy
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

from eidolon_deploy.manifest import V2_SYSTEM_ASSETS
from eidolon_deploy.sealing import ReleaseRevisions

_SOURCE_IDS = (
    "eidolon_kernel",
    "eidolon_data",
    "eidolon_hub",
    "eidolon_admin",
    "eidolon_agent",
    "eidolon_channel",
    "eidolon_memory",
    "eidolon_sdk",
)
_REVISION_BY_SOURCE = {
    "eidolon_kernel": "kernel",
    "eidolon_data": "data",
    "eidolon_hub": "hub",
    "eidolon_admin": "admin",
    "eidolon_agent": "agent",
    "eidolon_channel": "channel",
    "eidolon_memory": "memory",
    "eidolon_sdk": "sdk",
}
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREPARER_NAME = "prepare_target.py"
_MANIFEST_NAME = "bundle.json"
_LFS_POINTER = re.compile(
    rb"\Aversion https://git-lfs.github.com/spec/v1\n"
    rb"oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?\Z"
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
    sources: tuple[BundleSource, ...]
    preparer_sha256: str


def build_source_bundle(
    *,
    release_id: str,
    repositories: Mapping[str, Path],
    revisions: ReleaseRevisions,
    output: Path,
    git: str = "git",
) -> Path:
    """Archive exact Git commits without reading working-tree content."""

    if _RELEASE_ID.fullmatch(release_id) is None:
        raise BundleError("release id is invalid")
    if set(repositories) != set(_SOURCE_IDS):
        raise BundleError(
            "repository set must be exactly Kernel/Data/Hub/Admin/Agent/Channel/Memory/SDK"
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
        records: list[dict[str, str]] = []
        for source_id in _SOURCE_IDS:
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
            _git_run(
                git,
                "-C",
                str(repository),
                "archive",
                "--format=tar",
                f"--output={archive}",
                revision,
            )
            if source_id == "eidolon_channel":
                _hydrate_channel_archive(git, repository, archive)
            _validate_source_archive(source_id, archive)
            records.append(
                {
                    "source_id": source_id,
                    "revision": revision,
                    "archive": archive_relative,
                    "sha256": _file_sha256(archive),
                }
            )

        preparer_source = Path(__file__).with_name(_PREPARER_NAME)
        preparer_destination = temporary / _PREPARER_NAME
        shutil.copyfile(preparer_source, preparer_destination)
        os.chmod(preparer_destination, 0o755)
        document = {
            "schema_version": 1,
            "release_id": release_id,
            "target": {"system": "linux", "machine": "aarch64"},
            "sources": records,
            "preparer": {
                "path": _PREPARER_NAME,
                "sha256": _file_sha256(preparer_destination),
            },
        }
        _atomic_write_json(temporary / _MANIFEST_NAME, document)
        validate_source_bundle(temporary)
        os.replace(temporary, output)
        return output / _MANIFEST_NAME
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def validate_source_bundle(path: Path) -> SourceBundle:
    """Validate the fixed bundle shape and every transferred byte digest."""

    root = path.resolve()
    try:
        document = json.loads((root / _MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError("bundle manifest is unreadable") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "release_id",
        "target",
        "sources",
        "preparer",
    }:
        raise BundleError("bundle manifest shape is invalid")
    release_id = document.get("release_id")
    if (
        document.get("schema_version") != 1
        or not isinstance(release_id, str)
        or _RELEASE_ID.fullmatch(release_id) is None
        or document.get("target") != {"system": "linux", "machine": "aarch64"}
    ):
        raise BundleError("bundle identity or target is invalid")
    wire_sources = document.get("sources")
    if not isinstance(wire_sources, list) or len(wire_sources) != len(_SOURCE_IDS):
        raise BundleError("bundle source set is invalid")
    sources: list[BundleSource] = []
    for index, source_id in enumerate(_SOURCE_IDS):
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
        _validate_source_archive(source_id, archive)
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
    return SourceBundle(
        release_id=release_id,
        sources=tuple(sources),
        preparer_sha256=preparer["sha256"],
    )


def _validate_source_archive(source_id: str, archive: Path) -> None:
    required = {"pyproject.toml"}
    if source_id != "eidolon_sdk":
        required.add("uv.lock")
    if source_id == "eidolon_channel":
        required.update(_CHANNEL_MODEL_PATHS)
    for _destination, (component_id, source) in V2_SYSTEM_ASSETS.items():
        if component_id == source_id:
            required.add(source.as_posix())
    try:
        with tarfile.open(archive, "r:") as stream:
            members = stream.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise BundleError(f"source archive is unreadable: {source_id}") from exc
    names: set[str] = set()
    lfs_pointers: set[str] = set()
    with tarfile.open(archive, "r:") as stream:
        member_by_name = {
            PurePosixPath(member.name).as_posix().removeprefix("./"): member for member in members
        }
        for path in _CHANNEL_MODEL_PATHS if source_id == "eidolon_channel" else ():
            member = member_by_name.get(path)
            if member is None or not member.isfile() or member.size > 1024:
                continue
            source = stream.extractfile(member)
            if source is not None and _LFS_POINTER.fullmatch(source.read(1025)) is not None:
                lfs_pointers.add(path)
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
            raise BundleError(f"source archive has unsafe member: {source_id}")
        normalized = name.as_posix().removeprefix("./")
        names.add(normalized)
    missing = sorted(required - names)
    if missing:
        raise BundleError(f"source archive is incomplete: {source_id}: {', '.join(missing)}")
    if lfs_pointers:
        raise BundleError("Channel model artifact is missing or is an unhydrated LFS pointer")


def _hydrate_channel_archive(git: str, repository: Path, archive: Path) -> None:
    """Replace exact-commit LFS pointers without reading the Channel working tree."""

    pointers: dict[str, bytes] = {}
    with tarfile.open(archive, "r:") as source:
        for member in source.getmembers():
            normalized = PurePosixPath(member.name).as_posix().removeprefix("./")
            if normalized not in _CHANNEL_MODEL_PATHS or not member.isfile() or member.size > 1024:
                continue
            stream = source.extractfile(member)
            if stream is None:
                continue
            value = stream.read(1025)
            if _LFS_POINTER.fullmatch(value) is not None:
                pointers[normalized] = value
    if not pointers:
        return

    with tempfile.TemporaryDirectory(prefix="eidolon-lfs-", dir=archive.parent) as raw:
        stage = Path(raw)
        hydrated: dict[str, Path] = {}
        for index, (relative, pointer) in enumerate(sorted(pointers.items())):
            destination = stage / f"model-{index}"
            _smudge_lfs_pointer(git, repository, relative, pointer, destination)
            hydrated[relative] = destination
        rewritten = stage / "channel.tar"
        with tarfile.open(archive, "r:") as source, tarfile.open(rewritten, "w:") as output:
            for member in source.getmembers():
                normalized = PurePosixPath(member.name).as_posix().removeprefix("./")
                replacement = hydrated.get(normalized)
                if replacement is not None:
                    updated = copy.copy(member)
                    updated.size = replacement.stat().st_size
                    with replacement.open("rb") as stream:
                        output.addfile(updated, stream)
                elif member.isfile():
                    stream = source.extractfile(member)
                    if stream is None:
                        raise BundleError("Channel source archive member cannot be read")
                    output.addfile(member, stream)
                else:
                    output.addfile(member)
        os.replace(rewritten, archive)


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
