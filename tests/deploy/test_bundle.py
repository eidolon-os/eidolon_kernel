from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from eidolon_deploy import bundle, prepare_target
from eidolon_deploy.bundle import (
    _CHANNEL_MODEL_PATHS,
    BundleError,
    _smudge_lfs_pointer,
    _validate_source_archive,
    build_source_bundle,
    validate_source_bundle,
)
from eidolon_deploy.manifest import V2_SYSTEM_ASSETS
from eidolon_deploy.prepare_target import (
    TargetPreparationError,
    _exclusive_lock,
    prepare_target_release,
)
from eidolon_deploy.sealing import ReleaseRevisions

_REAL_BUILD_DEPENDENCY_CACHE = bundle._build_dependency_cache


@pytest.fixture(autouse=True)
def isolated_dependency_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build(*, uv, source_dir, destination, workspace) -> None:
        assert uv
        assert source_dir.is_dir()
        assert workspace.is_dir()
        with tarfile.open(destination, "w:") as archive:
            directory = tarfile.TarInfo("archive-v0")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)

    monkeypatch.setattr("eidolon_deploy.bundle._build_dependency_cache", fake_build)


def _run(*command: str) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _repositories(tmp_path: Path) -> tuple[dict[str, Path], ReleaseRevisions]:
    tmp_path.mkdir(parents=True)
    repositories: dict[str, Path] = {}
    revisions: dict[str, str] = {}
    revision_names = {
        "eidolon_kernel": "kernel",
        "eidolon_data": "data",
        "eidolon_hub": "hub",
        "eidolon_admin": "admin",
        "eidolon_agent": "agent",
        "eidolon_channel": "channel",
        "eidolon_memory": "memory",
        "eidolon_sdk": "sdk",
    }
    for source_id, revision_name in revision_names.items():
        repository = tmp_path / source_id
        repository.mkdir()
        _run("git", "-C", str(repository), "init", "-q")
        _run("git", "-C", str(repository), "config", "user.name", "Eidolon Test")
        _run("git", "-C", str(repository), "config", "user.email", "test@eidolon.invalid")
        (repository / "pyproject.toml").write_text(
            f"[project]\nname='{source_id}'\nversion='0.1.0'\n",
            encoding="utf-8",
        )
        if source_id != "eidolon_sdk":
            (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        for _destination, (component_id, source) in V2_SYSTEM_ASSETS.items():
            if component_id == source_id:
                path = repository / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"asset:{source}\n", encoding="utf-8")
        if source_id == "eidolon_channel":
            for relative in _CHANNEL_MODEL_PATHS:
                model = repository / relative
                model.parent.mkdir(parents=True, exist_ok=True)
                model.write_bytes(b"model" * 1024)
        _run("git", "-C", str(repository), "add", ".")
        _run("git", "-C", str(repository), "commit", "-qm", "fixture")
        revisions[revision_name] = _run("git", "-C", str(repository), "rev-parse", "HEAD")
        repositories[source_id] = repository
    return repositories, ReleaseRevisions(**revisions)


def _build_bundle(tmp_path: Path) -> tuple[Path, ReleaseRevisions]:
    repositories, revisions = _repositories(tmp_path / "repositories")
    output = tmp_path / "bundle"
    path = build_source_bundle(
        release_id="20260807-bundle-test",
        repositories=repositories,
        revisions=revisions,
        output=output,
    )
    assert path == output / "bundle.json"
    return output, revisions


def test_bundle_archives_exact_commits_and_rejects_byte_drift(tmp_path: Path) -> None:
    repositories, revisions = _repositories(tmp_path / "repositories")
    (repositories["eidolon_admin"] / "uncommitted.txt").write_text("excluded\n")
    output = tmp_path / "bundle"

    manifest = build_source_bundle(
        release_id="20260807-bundle-test",
        repositories=repositories,
        revisions=revisions,
        output=output,
    )

    bundle = validate_source_bundle(output)
    assert manifest == output / "bundle.json"
    assert [source.source_id for source in bundle.sources] == list(repositories)
    with tarfile.open(output / "sources/eidolon_admin.tar", "r:") as archive:
        assert "uncommitted.txt" not in archive.getnames()

    with (output / "sources/eidolon_admin.tar").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(BundleError, match="checksum mismatch"):
        validate_source_bundle(output)


def test_bundle_rejects_unmanifested_transfer_bytes(tmp_path: Path) -> None:
    output, _ = _build_bundle(tmp_path)
    (output / ".python-dependency-cache").mkdir()

    with pytest.raises(BundleError, match="unexpected entries"):
        validate_source_bundle(output)


def test_bundle_requires_exact_commit_and_complete_fixed_assets(tmp_path: Path) -> None:
    repositories, revisions = _repositories(tmp_path / "repositories")
    bad_revisions = ReleaseRevisions(
        kernel="f" * 40,
        data=revisions.data,
        hub=revisions.hub,
        admin=revisions.admin,
        agent=revisions.agent,
        channel=revisions.channel,
        memory=revisions.memory,
        sdk=revisions.sdk,
    )
    with pytest.raises(BundleError, match="Git archive operation failed"):
        build_source_bundle(
            release_id="20260807-missing-commit",
            repositories=repositories,
            revisions=bad_revisions,
            output=tmp_path / "missing-commit",
        )


def test_bundle_rejects_unhydrated_channel_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories, revisions = _repositories(tmp_path / "repositories")
    model = repositories["eidolon_channel"] / next(iter(_CHANNEL_MODEL_PATHS))
    model.write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 100000\n",
        encoding="utf-8",
    )
    _run("git", "-C", str(repositories["eidolon_channel"]), "add", str(model))
    _run(
        "git",
        "-C",
        str(repositories["eidolon_channel"]),
        "commit",
        "-qm",
        "replace model with pointer",
    )
    pointer_revisions = ReleaseRevisions(
        kernel=revisions.kernel,
        data=revisions.data,
        hub=revisions.hub,
        admin=revisions.admin,
        agent=revisions.agent,
        channel=_run("git", "-C", str(repositories["eidolon_channel"]), "rev-parse", "HEAD"),
        memory=revisions.memory,
        sdk=revisions.sdk,
    )
    monkeypatch.setattr(
        "eidolon_deploy.bundle._smudge_lfs_pointer",
        lambda *_args: (_ for _ in ()).throw(
            BundleError("Channel model artifact is an unhydrated LFS pointer")
        ),
    )

    with pytest.raises(BundleError, match="unhydrated LFS pointer"):
        build_source_bundle(
            release_id="20260807-lfs-pointer",
            repositories=repositories,
            revisions=pointer_revisions,
            output=tmp_path / "pointer",
        )


def test_bundle_hydrates_exact_commit_lfs_models_without_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories, revisions = _repositories(tmp_path / "repositories")
    repository = repositories["eidolon_channel"]
    relative = next(iter(_CHANNEL_MODEL_PATHS))
    model = repository / relative
    hydrated = b"hydrated-model" * 1024
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{hashlib.sha256(hydrated).hexdigest()}\n"
        f"size {len(hydrated)}\n"
    )
    model.write_text(pointer, encoding="utf-8")
    _run("git", "-C", str(repository), "add", str(model))
    _run("git", "-C", str(repository), "commit", "-qm", "store model pointer")
    revision = _run("git", "-C", str(repository), "rev-parse", "HEAD")
    model.write_bytes(b"uncommitted-working-tree-bytes")
    hydrated_revisions = ReleaseRevisions(
        kernel=revisions.kernel,
        data=revisions.data,
        hub=revisions.hub,
        admin=revisions.admin,
        agent=revisions.agent,
        channel=revision,
        memory=revisions.memory,
        sdk=revisions.sdk,
    )

    def fake_smudge(_git, _repository, observed, pointer_bytes, destination):
        assert observed == relative
        assert pointer_bytes == pointer.encode()
        destination.write_bytes(hydrated)

    monkeypatch.setattr("eidolon_deploy.bundle._smudge_lfs_pointer", fake_smudge)
    output = tmp_path / "hydrated"
    build_source_bundle(
        release_id="20260809-lfs-hydrated",
        repositories=repositories,
        revisions=hydrated_revisions,
        output=output,
    )

    with tarfile.open(output / "sources/eidolon_channel.tar", "r:") as archive:
        stream = archive.extractfile(relative)
        assert stream is not None
        assert stream.read() == hydrated


def test_lfs_smudge_verifies_pointer_digest(monkeypatch, tmp_path: Path) -> None:
    hydrated = b"exact-lfs-object"
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{hashlib.sha256(hydrated).hexdigest()}\n"
        f"size {len(hydrated)}\n"
    ).encode()

    def fake_run(command, **kwargs):
        assert command[-2:] == ("--", "model.onnx")
        assert kwargs["input"].startswith(b"version https://git-lfs.github.com/spec/v1\n")
        kwargs["stdout"].write(hydrated)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("eidolon_deploy.bundle.subprocess.run", fake_run)
    destination = tmp_path / "hydrated"
    _smudge_lfs_pointer("git", tmp_path, "model.onnx", pointer, destination)
    assert destination.read_bytes() == hydrated

    wrong_pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{hashlib.sha256(hydrated).hexdigest()}\n"
        "size 999\n"
    ).encode()
    with pytest.raises(BundleError, match="digest mismatch"):
        _smudge_lfs_pointer("git", tmp_path, "model.onnx", wrong_pointer, tmp_path / "wrong")


def test_lfs_smudge_reports_invalid_pointer_process_failure_and_spawn_error(
    monkeypatch, tmp_path: Path
) -> None:
    with pytest.raises(BundleError, match="invalid LFS pointer"):
        _smudge_lfs_pointer("git", tmp_path, "model.onnx", b"invalid", tmp_path / "invalid")

    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{'0' * 64}\n"
        "size 1\n"
    ).encode()
    monkeypatch.setattr(
        "eidolon_deploy.bundle.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 1, b"", b"missing object"),
    )
    with pytest.raises(BundleError, match="hydration failed.*missing object"):
        _smudge_lfs_pointer("git", tmp_path, "model.onnx", pointer, tmp_path / "failed")

    monkeypatch.setattr(
        "eidolon_deploy.bundle.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git-lfs missing")),
    )
    with pytest.raises(BundleError, match="could not run.*git-lfs missing"):
        _smudge_lfs_pointer("git", tmp_path, "model.onnx", pointer, tmp_path / "spawn")


def test_source_archive_validation_rejects_remaining_lfs_pointer(tmp_path: Path) -> None:
    archive_path = tmp_path / "channel.tar"
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{'0' * 64}\n"
        "size 10\n"
    ).encode()
    with tarfile.open(archive_path, "w:") as archive:
        for relative in {"pyproject.toml", "uv.lock", *_CHANNEL_MODEL_PATHS}:
            payload = pointer if relative in _CHANNEL_MODEL_PATHS else b"fixture"
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            archive.addfile(info, fileobj=io.BytesIO(payload))

    with pytest.raises(BundleError, match="unhydrated LFS pointer"):
        _validate_source_archive("eidolon_channel", archive_path)


def test_bundle_rejects_invalid_identity_repository_set_and_existing_output(
    tmp_path: Path,
) -> None:
    repositories, revisions = _repositories(tmp_path / "repositories")
    with pytest.raises(BundleError, match="release id"):
        build_source_bundle(
            release_id="bad/id",
            repositories=repositories,
            revisions=revisions,
            output=tmp_path / "invalid",
        )
    incomplete = dict(repositories)
    incomplete.pop("eidolon_sdk")
    with pytest.raises(BundleError, match="repository set"):
        build_source_bundle(
            release_id="20260807-set",
            repositories=incomplete,
            revisions=revisions,
            output=tmp_path / "set",
        )
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BundleError, match="already exists"):
        build_source_bundle(
            release_id="20260807-existing",
            repositories=repositories,
            revisions=revisions,
            output=existing,
        )

    admin_asset = repositories["eidolon_admin"] / "deploy/systemd/eidolon-admin.service"
    admin_asset.unlink()
    _run("git", "-C", str(repositories["eidolon_admin"]), "add", "-u")
    _run("git", "-C", str(repositories["eidolon_admin"]), "commit", "-qm", "remove asset")
    incomplete = ReleaseRevisions(
        kernel=revisions.kernel,
        data=revisions.data,
        hub=revisions.hub,
        admin=_run("git", "-C", str(repositories["eidolon_admin"]), "rev-parse", "HEAD"),
        agent=revisions.agent,
        channel=revisions.channel,
        memory=revisions.memory,
        sdk=revisions.sdk,
    )
    with pytest.raises(BundleError, match="archive is incomplete"):
        build_source_bundle(
            release_id="20260807-incomplete",
            repositories=repositories,
            revisions=incomplete,
            output=tmp_path / "incomplete",
        )


def test_target_preparation_extracts_builds_and_seals_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle, _ = _build_bundle(tmp_path)
    root = tmp_path / "host"
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)
    calls: list[tuple[str, ...]] = []

    def fake_run(operation: str, *command: str) -> None:
        calls.append((operation, *command))
        release_root = root / "opt/eidolon/releases/20260807-bundle-test"
        if operation == "native environment preparation" and command[-1].endswith("eidolon_kernel"):
            sealer = release_root / "eidolon_kernel/.venv/bin/eidolon-release"
            sealer.parent.mkdir(parents=True, exist_ok=True)
            sealer.write_text("#!/bin/sh\n", encoding="utf-8")
            sealer.chmod(0o755)
        if operation == "release sealing":
            (release_root / "release.json").write_text("{}\n", encoding="utf-8")
            (release_root / "release.json.sha256").write_text("test\n", encoding="utf-8")

    monkeypatch.setattr("eidolon_deploy.prepare_target._run", fake_run)

    descriptor = prepare_target_release(
        bundle,
        uv=uv,
        host_root=root,
        system="linux",
        machine="aarch64",
        require_root=False,
    )

    release_root = root / "opt/eidolon/releases/20260807-bundle-test"
    assert descriptor == release_root / "release.json"
    assert release_root.stat().st_mode & 0o777 == 0o755
    assert (release_root / "eidolon_admin/pyproject.toml").is_file()
    assert len([call for call in calls if call[0] == "native environment preparation"]) == 7
    assert all(
        "--no-python-downloads" in call
        for call in calls
        if call[0] == "native environment preparation"
    )
    assert all(
        "--offline" in call and any(value == "UV_OFFLINE=1" for value in call)
        for call in calls
        if call[0] == "native environment preparation"
    )
    assert all(
        call[call.index("--python-platform") + 1] == "aarch64-manylinux_2_40"
        for call in calls
        if call[0] == "native environment preparation"
    )
    data_call = next(call for call in calls if call[-3].endswith("eidolon_data"))
    assert data_call[-2:] == ("--extra", "api")
    seal_call = calls[-1]
    assert seal_call[0] == "release sealing"
    assert "--admin-revision" in seal_call


def test_target_preparation_cleans_new_release_after_build_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle, _ = _build_bundle(tmp_path)
    root = tmp_path / "host"
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)

    def fail_run(operation: str, *command: str) -> None:
        raise TargetPreparationError(f"{operation} failed")

    monkeypatch.setattr("eidolon_deploy.prepare_target._run", fail_run)
    with pytest.raises(TargetPreparationError, match="failed"):
        prepare_target_release(
            bundle,
            uv=uv,
            host_root=root,
            system="linux",
            machine="aarch64",
            require_root=False,
        )

    assert not (root / "opt/eidolon/releases/20260807-bundle-test").exists()


def test_target_preparation_rejects_wrong_target_and_tampered_preparer(
    tmp_path: Path,
) -> None:
    bundle, _ = _build_bundle(tmp_path)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)

    with pytest.raises(TargetPreparationError, match="linux/aarch64"):
        prepare_target_release(
            bundle,
            uv=uv,
            host_root=tmp_path / "host",
            system="darwin",
            machine="arm64",
            require_root=False,
        )

    with (bundle / "prepare_target.py").open("a", encoding="utf-8") as stream:
        stream.write("# tampered\n")
    with pytest.raises(TargetPreparationError, match="preparer checksum"):
        prepare_target_release(
            bundle,
            uv=uv,
            host_root=tmp_path / "host",
            system="linux",
            machine="aarch64",
            require_root=False,
        )


def test_target_preparation_rejects_unsafe_archive_even_with_updated_checksum(
    tmp_path: Path,
) -> None:
    bundle, _ = _build_bundle(tmp_path)
    archive_path = bundle / "sources/eidolon_kernel.tar"
    unsafe_source = tmp_path / "unsafe-link"
    unsafe_source.symlink_to("/etc/passwd")
    with tarfile.open(archive_path, "w:") as archive:
        archive.add(unsafe_source, arcname="unsafe-link", recursive=False)
    document = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    document["sources"][0]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (bundle / "bundle.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)

    with pytest.raises(TargetPreparationError, match="non-file member"):
        prepare_target_release(
            bundle,
            uv=uv,
            host_root=tmp_path / "host",
            system="linux",
            machine="aarch64",
            require_root=False,
        )
    assert not (tmp_path / "host/opt/eidolon/releases/20260807-bundle-test").exists()


def test_target_preparation_lock_rejects_concurrent_preparer(tmp_path: Path) -> None:
    lock = tmp_path / "prepare.lock"

    with _exclusive_lock(lock):
        with pytest.raises(TargetPreparationError, match="already in progress"):
            with _exclusive_lock(lock):
                raise AssertionError("concurrent preparer entered critical section")


def test_standalone_preparer_cli_reports_success_and_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    descriptor = tmp_path / "release.json"
    monkeypatch.setattr(
        prepare_target,
        "prepare_target_release",
        lambda bundle, uv: descriptor,
    )
    assert prepare_target.main([str(tmp_path / "bundle")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"

    def fail(bundle, uv):
        raise TargetPreparationError("injected failure")

    monkeypatch.setattr(prepare_target, "prepare_target_release", fail)
    assert prepare_target.main([str(tmp_path / "bundle")]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "failed"


def test_dependency_prefetch_pins_both_halves_of_the_target_abi(
    tmp_path: Path, monkeypatch
) -> None:
    """Pinning only the platform lets the workstation's Python decide the ABI.

    uv then caches wheels the target cannot use, while the bundle manifest goes
    on claiming the Python version the release was built for.
    """

    from eidolon_deploy import bundle as bundle_module

    # The autouse fixture replaces the whole prefetch; this test is about the
    # commands it builds, so put the real one back.
    calls: list[tuple[str, ...]] = []

    def fake_run(command, *, env=None):
        command = tuple(command)
        calls.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, f"uv {bundle_module._UV_VERSION}", "")
        if command[1] == "venv":
            seed = Path(command[-1])
            (seed / "bin").mkdir(parents=True, exist_ok=True)
            (seed / "bin/python").write_text("#!/bin/sh\n")
        if env and "UV_PROJECT_ENVIRONMENT" in env:
            Path(env["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(bundle_module, "_dependency_run", fake_run)

    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    for source_id in bundle_module._SOURCE_IDS[:-1]:
        with tarfile.open(source_dir / f"{source_id}.tar", "w") as archive:
            member = tarfile.TarInfo("pyproject.toml")
            payload = b"[project]\nname='x'\nversion='0'\n"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _REAL_BUILD_DEPENDENCY_CACHE(
        uv="uv",
        source_dir=source_dir,
        destination=tmp_path / "python-dependencies.tar.gz",
        workspace=workspace,
    )

    syncs = [call for call in calls if "sync" in call]
    assert len(syncs) == len(bundle_module._SOURCE_IDS) - 1
    for call in syncs:
        assert call[call.index("--python") + 1] == bundle_module._PYTHON_VERSION
        assert call[call.index("--python-platform") + 1] == bundle_module._PYTHON_PLATFORM


def _prefetch_harness(tmp_path: Path, monkeypatch):
    """The real prefetch, with uv replaced by something that records calls."""

    from eidolon_deploy import bundle as bundle_module

    def fake_run(command, *, env=None):
        command = tuple(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, f"uv {bundle_module._UV_VERSION}", "")
        if command[1] == "venv":
            seed = Path(command[-1])
            (seed / "bin").mkdir(parents=True, exist_ok=True)
            (seed / "bin/python").write_text("#!/bin/sh\n")
        if env and "UV_CACHE_DIR" in env:
            # uv fills its cache as it resolves; stand in for that so the
            # archive and the seed have something to carry.
            fetched = Path(env["UV_CACHE_DIR"]) / "archive-v0" / "fetched"
            fetched.parent.mkdir(parents=True, exist_ok=True)
            fetched.write_text("wheel", encoding="utf-8")
        if env and "UV_PROJECT_ENVIRONMENT" in env:
            Path(env["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(bundle_module, "_dependency_run", fake_run)
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    for source_id in bundle_module._SOURCE_IDS[:-1]:
        with tarfile.open(source_dir / f"{source_id}.tar", "w") as archive:
            member = tarfile.TarInfo("pyproject.toml")
            payload = b"[project]\nname='x'\nversion='0'\n"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return source_dir


def _prefetch(tmp_path: Path, source_dir: Path, name: str) -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    destination = tmp_path / f"{name}.tar.gz"
    _REAL_BUILD_DEPENDENCY_CACHE(
        uv="uv", source_dir=source_dir, destination=destination, workspace=workspace
    )
    return destination


def test_a_build_starts_from_what_this_machine_already_fetched(tmp_path, monkeypatch) -> None:
    """Every build used to begin on empty disk and re-download the whole set.

    On a 1 MB/s link that is 600 MB and half an hour, which is how the bundle
    step started blowing the 1800s ceiling it runs under.
    """

    source_dir = _prefetch_harness(tmp_path, monkeypatch)
    kept = tmp_path / "uv-cache"
    monkeypatch.setenv(bundle.KEPT_DEPENDENCY_CACHE_ENV, str(kept))

    _prefetch(tmp_path, source_dir, "first")

    assert (kept / "archive-v0" / "fetched").is_file()

    # The second build finds it still there rather than reaching for the index.
    (kept / "archive-v0" / "from-the-first-build").write_text("wheel", encoding="utf-8")
    _prefetch(tmp_path, source_dir, "second")

    assert (kept / "archive-v0" / "from-the-first-build").is_file()


def test_the_kept_cache_stays_where_uv_built_it(tmp_path, monkeypatch) -> None:
    """uv writes wheels-v6 as absolute symlinks into archive-v0.

    A cache is therefore bound to its path: the first attempt at this cloned
    one between builds, and all 252 pointers in it went dangling the moment
    the directory they were built in was removed.
    """

    source_dir = _prefetch_harness(tmp_path, monkeypatch)
    kept = tmp_path / "uv-cache"
    monkeypatch.setenv(bundle.KEPT_DEPENDENCY_CACHE_ENV, str(kept))
    workspace_before = set(tmp_path.iterdir())

    _prefetch(tmp_path, source_dir, "first")

    # The build workspace is disposable; the cache must not live inside it.
    assert kept.is_dir()
    assert kept not in workspace_before
    assert not (tmp_path / "first" / ".python-dependency-cache").exists()


def test_a_kept_cache_cannot_change_what_a_release_installs(tmp_path, monkeypatch) -> None:
    """The safety argument for keeping one at all.

    A release's contents are fixed by each source's lockfile, so the packaged
    cache may hold more than a cold build's, but never less and never other.
    """

    source_dir = _prefetch_harness(tmp_path, monkeypatch)
    monkeypatch.delenv(bundle.KEPT_DEPENDENCY_CACHE_ENV, raising=False)
    cold = _prefetch(tmp_path, source_dir, "cold")

    monkeypatch.setenv(bundle.KEPT_DEPENDENCY_CACHE_ENV, str(tmp_path / "uv-cache"))
    warm = _prefetch(tmp_path, source_dir, "warm")

    def members(path: Path) -> set[str]:
        with tarfile.open(path, "r:gz") as archive:
            return {member.name for member in archive.getmembers()}

    assert members(cold) <= members(warm)


def test_a_relative_cache_is_refused(tmp_path, monkeypatch) -> None:
    source_dir = _prefetch_harness(tmp_path, monkeypatch)
    monkeypatch.setenv(bundle.KEPT_DEPENDENCY_CACHE_ENV, "relative/cache")

    with pytest.raises(BundleError, match="absolute path"):
        _prefetch(tmp_path, source_dir, "refused")


def test_without_a_kept_cache_a_build_starts_from_empty_disk(tmp_path, monkeypatch) -> None:
    """The escape hatch: prove a release without trusting any local state."""

    source_dir = _prefetch_harness(tmp_path, monkeypatch)
    monkeypatch.delenv(bundle.KEPT_DEPENDENCY_CACHE_ENV, raising=False)

    assert _prefetch(tmp_path, source_dir, "clean").is_file()
