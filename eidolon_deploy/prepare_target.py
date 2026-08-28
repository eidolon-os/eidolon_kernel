#!/usr/bin/env python3
"""Standalone Linux/aarch64 source-bundle preparer.

This file intentionally uses only the Python standard library so a product
image can run the copied bundle tool before any new release venv exists.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator, Sequence

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
_REVISION_FLAGS = {
    "eidolon_kernel": "--kernel-revision",
    "eidolon_data": "--data-revision",
    "eidolon_hub": "--hub-revision",
    "eidolon_admin": "--admin-revision",
    "eidolon_agent": "--agent-revision",
    "eidolon_channel": "--channel-revision",
    "eidolon_memory": "--memory-revision",
    "eidolon_sdk": "--sdk-revision",
}
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTERNAL_ARTIFACT_POINTER = re.compile(
    rb"\Aeidolon-external-artifact-v1\nsha256 ([0-9a-f]{64})\nsize ([0-9]+)\n\Z"
)
_RELEASES = Path("/opt/eidolon/releases")
_LOCK = Path("/run/lock/eidolon-release-prepare.lock")
_DEPENDENCY_CACHE_ROOT = Path("/var/cache/eidolon/release-dependencies")
_ARTIFACT_STORE_ROOT = Path("/var/cache/eidolon/release-artifacts-v1/sha256")
_UV_VERSION = "0.11.15"
_PYTHON_VERSION = "3.13"
_PYTHON_PLATFORM = "aarch64-manylinux_2_40"
_BUILD_REQUIREMENTS = (
    "setuptools==80.9.0",
    "wheel==0.45.1",
    "hatchling==1.27.0",
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


class TargetPreparationError(RuntimeError):
    """The transferred bundle or target preparation failed closed."""


def prepare_target_release(
    bundle: Path,
    *,
    uv: Path = Path("/usr/local/bin/uv"),
    host_root: Path = Path("/"),
    system: str | None = None,
    machine: str | None = None,
    require_root: bool = True,
) -> Path:
    """Extract, build native environments and seal one new release."""

    actual_system = (system or platform.system()).lower()
    actual_machine = (machine or platform.machine()).lower()
    if actual_system != "linux" or actual_machine != "aarch64":
        raise TargetPreparationError(
            f"target preparation requires linux/aarch64, got {actual_system}/{actual_machine}"
        )
    if require_root and os.geteuid() != 0:
        raise TargetPreparationError("target preparation requires root privileges")
    if not uv.is_absolute() or not uv.is_file() or not os.access(uv, os.X_OK):
        raise TargetPreparationError("uv executable is missing or not an absolute executable")

    root = host_root.resolve()
    bundle_root = bundle.resolve()
    document = _validate_bundle(bundle_root)
    release_id = document["release_id"]
    releases = _host_path(root, _RELEASES)
    release_root = releases / release_id
    lock = _host_path(root, _LOCK)
    dependency_cache = _host_path(root, _DEPENDENCY_CACHE_ROOT) / release_id
    artifacts = {value["artifact_id"]: value for value in document["artifacts"]}

    with _exclusive_lock(lock):
        if release_root.exists():
            raise TargetPreparationError("target release directory already exists")
        releases.mkdir(parents=True, exist_ok=True)
        staging = releases / f".{release_id}.{uuid.uuid4().hex}.tmp"
        staging.mkdir(mode=0o700)
        installed = False
        try:
            dependency_cache.mkdir(parents=True, mode=0o700)
            dependency_artifact = artifacts[document["python_dependencies"]["artifact_id"]]
            _extract_dependency_cache(
                _resolve_artifact(root, bundle_root, dependency_artifact), dependency_cache
            )
            for source in document["sources"]:
                destination = staging / source["source_id"]
                destination.mkdir()
                _extract_archive(bundle_root / source["archive"], destination)
            _hydrate_channel_models(root, bundle_root, staging, artifacts)
            os.chmod(staging, 0o755)
            os.replace(staging, release_root)
            installed = True

            for source_id in _SOURCE_IDS[:-1]:
                command = [
                    "/usr/bin/env",
                    f"UV_CACHE_DIR={dependency_cache}",
                    "UV_OFFLINE=1",
                    str(uv),
                    "sync",
                    "--frozen",
                    "--no-dev",
                    "--no-python-downloads",
                    "--no-editable",
                    "--offline",
                    "--python-platform",
                    _PYTHON_PLATFORM,
                    "--project",
                    str(release_root / source_id),
                ]
                if source_id == "eidolon_data":
                    command.extend(("--extra", "api"))
                _run("native environment preparation", *command)

            sealer = release_root / "eidolon_kernel/.venv/bin/eidolon-release"
            if not sealer.is_file() or not os.access(sealer, os.X_OK):
                raise TargetPreparationError("prepared Kernel release entrypoint is missing")
            seal_command = [
                str(sealer),
                "seal",
                release_id,
                "--cutover-mode",
                document["cutover_mode"],
            ]
            for source in document["sources"]:
                seal_command.extend((_REVISION_FLAGS[source["source_id"]], source["revision"]))
            _run("release sealing", *seal_command)
            descriptor = release_root / "release.json"
            checksum = release_root / "release.json.sha256"
            if not descriptor.is_file() or not checksum.is_file():
                raise TargetPreparationError("release sealing did not produce its descriptor")
            return descriptor
        except Exception:
            if installed:
                shutil.rmtree(release_root, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(dependency_cache, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)


def _validate_bundle(root: Path) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise TargetPreparationError("bundle directory is missing or unsafe")
    required_root = {"bundle.json", "prepare_target.py", "sources"}
    allowed_root = required_root | {"artifacts"}
    try:
        actual_root = {item.name for item in root.iterdir()}
        if not required_root <= actual_root or not actual_root <= allowed_root:
            raise TargetPreparationError("bundle root contains unexpected entries")
    except OSError as exc:
        raise TargetPreparationError("bundle root is unreadable") from exc
    try:
        document = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetPreparationError("bundle manifest is unreadable") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "release_id",
        "cutover_mode",
        "target",
        "sources",
        "preparer",
        "artifacts",
        "python_dependencies",
    }:
        raise TargetPreparationError("bundle manifest shape is invalid")
    release_id = document.get("release_id")
    if (
        document.get("schema_version") != 3
        or not isinstance(release_id, str)
        or _RELEASE_ID.fullmatch(release_id) is None
        or document.get("target") != {"system": "linux", "machine": "aarch64"}
        or document.get("cutover_mode") not in {"reversible", "forward-only"}
    ):
        raise TargetPreparationError("bundle identity or target is invalid")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(_CHANNEL_MODEL_PATHS) + 1:
        raise TargetPreparationError("bundle artifact set is invalid")
    artifact_ids: set[str] = set()
    channel_paths: set[str] = set()
    dependency_seen = False
    for value in artifacts:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"artifact_id", "kind", "sha256", "size", "bundle_path", "install_path"}
            or not isinstance(value.get("artifact_id"), str)
            or value["artifact_id"] in artifact_ids
            or not isinstance(value.get("sha256"), str)
            or _SHA256.fullmatch(value["sha256"]) is None
            or not isinstance(value.get("size"), int)
            or isinstance(value.get("size"), bool)
            or value["size"] < 0
            or value.get("bundle_path") != f"artifacts/sha256/{value['sha256']}"
            or not isinstance(value.get("install_path"), str)
        ):
            raise TargetPreparationError("bundle artifact record is invalid")
        artifact_ids.add(value["artifact_id"])
        if value.get("kind") == "dependency-cache":
            if (
                dependency_seen
                or value["artifact_id"] != "python-dependencies"
                or value["install_path"] != ""
            ):
                raise TargetPreparationError("bundle dependency artifact record is invalid")
            dependency_seen = True
        elif value.get("kind") == "channel-model":
            install = PurePosixPath(value["install_path"])
            if (
                install.is_absolute()
                or ".." in install.parts
                or not install.parts
                or not value["artifact_id"].startswith("channel-model:")
                or value["artifact_id"] != f"channel-model:{value['install_path']}"
                or value["install_path"] in channel_paths
            ):
                raise TargetPreparationError("bundle Channel artifact record is invalid")
            channel_paths.add(value["install_path"])
        else:
            raise TargetPreparationError("bundle artifact kind is invalid")
        bundled = root / value["bundle_path"]
        if bundled.exists() or bundled.is_symlink():
            if (
                bundled.is_symlink()
                or not bundled.is_file()
                or bundled.stat().st_size != value["size"]
                or _file_sha256(bundled) != value["sha256"]
            ):
                raise TargetPreparationError(
                    f"bundled artifact checksum mismatch: {value['artifact_id']}"
                )
    if not dependency_seen or channel_paths != _CHANNEL_MODEL_PATHS:
        raise TargetPreparationError("bundle artifact roles are incomplete")
    bundled_root = root / "artifacts"
    if bundled_root.exists() or bundled_root.is_symlink():
        object_root = bundled_root / "sha256"
        expected_objects = {value["sha256"] for value in artifacts}
        if (
            bundled_root.is_symlink()
            or not object_root.is_dir()
            or object_root.is_symlink()
            or {item.name for item in object_root.iterdir()} != expected_objects
            or any(item.is_symlink() or not item.is_file() for item in object_root.iterdir())
        ):
            raise TargetPreparationError("bundled artifact directory is invalid")
    sources = document.get("sources")
    if not isinstance(sources, list) or len(sources) != len(_SOURCE_IDS):
        raise TargetPreparationError("bundle source set is invalid")
    source_root = root / "sources"
    if (
        not source_root.is_dir()
        or source_root.is_symlink()
        or {item.name for item in source_root.iterdir()}
        != {f"{source_id}.tar" for source_id in _SOURCE_IDS}
    ):
        raise TargetPreparationError("bundle sources directory contains unexpected entries")
    for index, source_id in enumerate(_SOURCE_IDS):
        value = sources[index]
        archive = f"sources/{source_id}.tar"
        if (
            not isinstance(value, dict)
            or set(value) != {"source_id", "revision", "archive", "sha256"}
            or value.get("source_id") != source_id
            or value.get("archive") != archive
            or not isinstance(value.get("revision"), str)
            or _REVISION.fullmatch(value["revision"]) is None
            or not isinstance(value.get("sha256"), str)
            or _SHA256.fullmatch(value["sha256"]) is None
        ):
            raise TargetPreparationError(f"bundle source record is invalid: {source_id}")
        archive_path = root / archive
        if (
            not archive_path.is_file()
            or archive_path.is_symlink()
            or _file_sha256(archive_path) != value["sha256"]
        ):
            raise TargetPreparationError(f"bundle source archive checksum mismatch: {source_id}")
    preparer = document.get("preparer")
    if (
        not isinstance(preparer, dict)
        or set(preparer) != {"path", "sha256"}
        or preparer.get("path") != "prepare_target.py"
        or not isinstance(preparer.get("sha256"), str)
        or _SHA256.fullmatch(preparer["sha256"]) is None
    ):
        raise TargetPreparationError("bundle target preparer record is invalid")
    preparer_path = root / "prepare_target.py"
    if (
        not preparer_path.is_file()
        or preparer_path.is_symlink()
        or _file_sha256(preparer_path) != preparer["sha256"]
    ):
        raise TargetPreparationError("bundle target preparer checksum mismatch")
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
        raise TargetPreparationError("bundle Python dependency cache record is invalid")
    expected_index = os.environ.get("UV_DEFAULT_INDEX", "https://pypi.org/simple")
    if dependency_cache["index_url"] != expected_index:
        raise TargetPreparationError("bundle Python dependency index does not match target input")
    return document


def _resolve_artifact(host_root: Path, bundle_root: Path, record: dict) -> Path:
    digest = record["sha256"]
    size = record["size"]
    store_root = _host_path(host_root, _ARTIFACT_STORE_ROOT)
    store_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    if store_root.is_symlink() or not store_root.is_dir():
        raise TargetPreparationError("target artifact store is unsafe")
    target = store_root / digest
    if target.is_file() and not target.is_symlink():
        if target.stat().st_size == size and _file_sha256(target) == digest:
            return target
    elif target.exists() or target.is_symlink():
        raise TargetPreparationError("target artifact object is unsafe")

    carried = bundle_root / record["bundle_path"]
    if (
        not carried.is_file()
        or carried.is_symlink()
        or carried.stat().st_size != size
        or _file_sha256(carried) != digest
    ):
        raise TargetPreparationError(f"required artifact is absent: {record['artifact_id']}")
    temporary = store_root / f".{digest}.{uuid.uuid4().hex}.tmp"
    try:
        _copy_or_link(carried, temporary)
        if temporary.stat().st_size != size or _file_sha256(temporary) != digest:
            raise TargetPreparationError("artifact changed while entering the target store")
        os.chmod(temporary, 0o444)
        os.replace(temporary, target)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return target


def _hydrate_channel_models(
    host_root: Path, bundle_root: Path, staging: Path, artifacts: dict[str, dict]
) -> None:
    models = [value for value in artifacts.values() if value["kind"] == "channel-model"]
    for record in models:
        relative = record["install_path"]
        target = staging / "eidolon_channel" / Path(*PurePosixPath(relative).parts)
        if not target.is_file() or target.is_symlink():
            raise TargetPreparationError(f"Channel artifact pointer is missing: {relative}")
        pointer = _EXTERNAL_ARTIFACT_POINTER.fullmatch(target.read_bytes())
        if (
            pointer is None
            or pointer.group(1).decode("ascii") != record["sha256"]
            or int(pointer.group(2)) != record["size"]
        ):
            raise TargetPreparationError(f"Channel artifact pointer drifted: {relative}")
        source = _resolve_artifact(host_root, bundle_root, record)
        replacement = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            _copy_or_link(source, replacement)
            if (
                replacement.stat().st_size != record["size"]
                or _file_sha256(replacement) != record["sha256"]
            ):
                raise TargetPreparationError(f"Channel artifact hydration failed: {relative}")
            os.chmod(replacement, 0o444)
            os.replace(replacement, target)
        finally:
            if replacement.exists() or replacement.is_symlink():
                replacement.unlink()


def _copy_or_link(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


def _extract_archive(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, "r:*") as stream:
            for member in stream:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise TargetPreparationError("source archive contains an unsafe path")
                target = destination.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = stream.extractfile(member)
                    if source is None:
                        raise TargetPreparationError("source archive member is unreadable")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, member.mode & 0o777)
                else:
                    raise TargetPreparationError("source archive contains a non-file member")
    except (OSError, tarfile.TarError) as exc:
        raise TargetPreparationError("source archive extraction failed") from exc


def _extract_dependency_cache(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, "r:*") as stream:
            members = stream.getmembers()
            for member in members:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise TargetPreparationError("dependency cache contains an unsafe path")
                target = destination.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = stream.extractfile(member)
                    if source is None:
                        raise TargetPreparationError("dependency cache member is unreadable")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, member.mode & 0o777)
                elif member.issym():
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute():
                        raise TargetPreparationError("dependency cache symlink is absolute")
                    resolved = (target.parent / Path(*link.parts)).resolve(strict=False)
                    root = destination.resolve()
                    if root != resolved and root not in resolved.parents:
                        raise TargetPreparationError("dependency cache symlink escapes its root")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                else:
                    raise TargetPreparationError("dependency cache contains an unsafe member")
    except (OSError, tarfile.TarError) as exc:
        raise TargetPreparationError("dependency cache extraction failed") from exc


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TargetPreparationError(
                "another target preparation is already in progress"
            ) from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _run(operation: str, *command: str) -> None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TargetPreparationError(f"{operation} could not run: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise TargetPreparationError(f"{operation} failed: {detail}")


def _host_path(root: Path, value: Path) -> Path:
    if root == Path("/"):
        return value
    return root / value.relative_to("/")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepare_target.py",
        description="Prepare and seal one commit-pinned Eidolon target release.",
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--uv", type=Path, default=Path("/usr/local/bin/uv"))
    arguments = parser.parse_args(argv)
    try:
        descriptor = prepare_target_release(arguments.bundle, uv=arguments.uv)
    except (TargetPreparationError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "prepared", "descriptor": str(descriptor)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
