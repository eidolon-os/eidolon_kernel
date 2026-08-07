from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from eidolon_deploy import prepare_target
from eidolon_deploy.bundle import BundleError, build_source_bundle, validate_source_bundle
from eidolon_deploy.manifest import V2_SYSTEM_ASSETS
from eidolon_deploy.prepare_target import (
    TargetPreparationError,
    _exclusive_lock,
    prepare_target_release,
)
from eidolon_deploy.sealing import ReleaseRevisions


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


def test_bundle_requires_exact_commit_and_complete_fixed_assets(tmp_path: Path) -> None:
    repositories, revisions = _repositories(tmp_path / "repositories")
    bad_revisions = ReleaseRevisions(
        kernel="f" * 40,
        data=revisions.data,
        hub=revisions.hub,
        admin=revisions.admin,
        sdk=revisions.sdk,
    )
    with pytest.raises(BundleError, match="Git archive operation failed"):
        build_source_bundle(
            release_id="20260807-missing-commit",
            repositories=repositories,
            revisions=bad_revisions,
            output=tmp_path / "missing-commit",
        )


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
        release_root = root / "srv/eidolon/releases/20260807-bundle-test"
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

    release_root = root / "srv/eidolon/releases/20260807-bundle-test"
    assert descriptor == release_root / "release.json"
    assert release_root.stat().st_mode & 0o777 == 0o755
    assert (release_root / "eidolon_admin/pyproject.toml").is_file()
    assert len([call for call in calls if call[0] == "native environment preparation"]) == 4
    assert all(
        "--no-python-downloads" in call
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

    assert not (root / "srv/eidolon/releases/20260807-bundle-test").exists()


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
    assert not (tmp_path / "host/srv/eidolon/releases/20260807-bundle-test").exists()


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
