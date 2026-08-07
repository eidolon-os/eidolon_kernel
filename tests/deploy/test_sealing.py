from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eidolon_deploy.manifest import load_release_descriptor
from eidolon_deploy.sealing import (
    EnvironmentFacts,
    PreparationError,
    ReleaseRevisions,
    SubprocessEnvironmentInspector,
    seal_prepared_release,
)


class FakeInspector:
    def inspect(self, python: Path) -> EnvironmentFacts:
        assert python.name == "python"
        return EnvironmentFacts(
            python_version="3.13",
            freeze_output="pip==25.1\neidolon-test==1.0\n",
        )


def _host_path(root: Path, value: str) -> Path:
    return root / Path(value).relative_to("/")


def _prepared_tree(tmp_path: Path, release_id: str) -> Path:
    root = tmp_path / "root"
    release_root = _host_path(root, f"/srv/eidolon/releases/{release_id}")
    kernel = release_root / "eidolon_kernel"
    data = release_root / "eidolon_data"
    hub = release_root / "eidolon_hub"
    admin = release_root / "eidolon_admin"
    sdk = release_root / "eidolon_sdk"
    for source in (kernel, data, hub, admin, sdk):
        source.mkdir(parents=True)
        (source / "pyproject.toml").write_text(f"[project]\nname='{source.name}'\n")
    for component, entrypoints in (
        (kernel, ("eidolond", "uvicorn")),
        (data, ("uvicorn",)),
        (hub, ("uvicorn",)),
        (admin, ("eidolon-admin", "eidolon-bootstrapd", "eidolon-local-api")),
    ):
        (component / "uv.lock").write_text(f"lock:{component.name}\n")
        python = component / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        for name in entrypoints:
            executable = component / ".venv/bin" / name
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)

    asset_sources = (
        "deploy/systemd/eidolond.service",
        "deploy/systemd/eidolon-data.service",
        "deploy/systemd/eidolon-hub.service",
        "deploy/systemd/eidolon-kernel.service",
        "config/eidolond.systemd.example.yaml",
        "config/kernel.systemd.example.yaml",
        "config/hub.systemd.example.yaml",
        "config/system-services.systemd.example.yaml",
        "deploy/polkit/60-eidolon-system-manager.rules",
    )
    for relative in asset_sources:
        asset = kernel / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(f"asset:{relative}\n")
    admin_assets = (
        "deploy/systemd/eidolon-bootstrapd.service",
        "deploy/systemd/eidolon-local-api.service",
        "deploy/systemd/eidolon-admin.service",
        "deploy/polkit/60-eidolon-bootstrap-network.rules",
        "deploy/avahi/eidolon-local-api.service",
    )
    for relative in admin_assets:
        asset = admin / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(f"asset:{relative}\n")
    return root


def test_seals_fixed_target_release_from_prepared_native_tree(tmp_path: Path) -> None:
    release_id = "20260806-m2d-seal"
    root = _prepared_tree(tmp_path, release_id)

    path = seal_prepared_release(
        host_root=root,
        release_id=release_id,
        revisions=ReleaseRevisions(
            kernel="a" * 40,
            data="b" * 40,
            hub="d" * 40,
            admin="e" * 40,
            sdk="c" * 40,
        ),
        inspector=FakeInspector(),
        system="linux",
        machine="aarch64",
    )

    release = load_release_descriptor(path)
    assert release.release_id == release_id
    assert release.target.python == "3.13"
    assert [item.component_id for item in release.components] == [
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
    ]
    assert release.support_sources[0].source_id == "eidolon_sdk"
    assert {str(item.destination) for item in release.system_assets} == {
        "/etc/systemd/system/eidolond.service",
        "/etc/systemd/system/eidolon-data.service",
        "/etc/systemd/system/eidolon-hub.service",
        "/etc/systemd/system/eidolon-kernel.service",
        "/etc/eidolon/eidolond.yaml",
        "/etc/eidolon/kernel.yaml",
        "/etc/eidolon/hub.yaml",
        "/etc/eidolon/system-services.systemd.example.yaml",
        "/etc/polkit-1/rules.d/60-eidolon-system-manager.rules",
        "/etc/systemd/system/eidolon-bootstrapd.service",
        "/etc/systemd/system/eidolon-local-api.service",
        "/etc/systemd/system/eidolon-admin.service",
        "/etc/polkit-1/rules.d/60-eidolon-bootstrap-network.rules",
        "/etc/avahi/services/eidolon-local-api.service",
    }
    assert json.loads(path.read_text())["database_migrations"] == []


def test_sealing_rejects_wrong_target_or_incomplete_preparation(tmp_path: Path) -> None:
    release_id = "20260806-m2d-invalid"
    root = _prepared_tree(tmp_path, release_id)
    revisions = ReleaseRevisions(
        kernel="a" * 40,
        data="b" * 40,
        hub="d" * 40,
        admin="e" * 40,
        sdk="c" * 40,
    )

    with pytest.raises(PreparationError, match="target host"):
        seal_prepared_release(
            host_root=root,
            release_id=release_id,
            revisions=revisions,
            inspector=FakeInspector(),
            system="darwin",
            machine="arm64",
        )

    (_host_path(root, f"/srv/eidolon/releases/{release_id}") / "eidolon_sdk").rename(
        tmp_path / "missing-sdk"
    )
    with pytest.raises(PreparationError, match="source directory"):
        seal_prepared_release(
            host_root=root,
            release_id=release_id,
            revisions=revisions,
            inspector=FakeInspector(),
            system="linux",
            machine="aarch64",
        )


def test_release_revisions_must_be_full_git_object_ids() -> None:
    with pytest.raises(PreparationError, match="revision"):
        ReleaseRevisions(
            kernel="main",
            data="b" * 40,
            hub="d" * 40,
            admin="e" * 40,
            sdk="c" * 40,
        )


def test_real_environment_inspector_uses_fixed_python_operations() -> None:
    facts = SubprocessEnvironmentInspector().inspect(Path(sys.executable))

    assert facts.python_version == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert "eidolon-kernel==0.1.0" in facts.freeze_output


def test_environment_inspector_fails_closed_when_python_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PreparationError, match="could not run"):
        SubprocessEnvironmentInspector().inspect(tmp_path / "missing-python")


def test_environment_inspector_fails_closed_on_nonzero_process() -> None:
    with pytest.raises(PreparationError, match="inspection failed"):
        SubprocessEnvironmentInspector().inspect(Path("/usr/bin/false"))


def test_sealing_rejects_invalid_id_and_resealing(tmp_path: Path) -> None:
    revisions = ReleaseRevisions(
        kernel="a" * 40,
        data="b" * 40,
        hub="d" * 40,
        admin="e" * 40,
        sdk="c" * 40,
    )
    with pytest.raises(PreparationError, match="release id"):
        seal_prepared_release(
            host_root=tmp_path,
            release_id="bad/id",
            revisions=revisions,
            inspector=FakeInspector(),
            system="linux",
            machine="aarch64",
        )

    release_id = "20260806-m2d-once"
    root = _prepared_tree(tmp_path, release_id)
    seal_prepared_release(
        host_root=root,
        release_id=release_id,
        revisions=revisions,
        inspector=FakeInspector(),
        system="linux",
        machine="aarch64",
    )
    with pytest.raises(PreparationError, match="already sealed"):
        seal_prepared_release(
            host_root=root,
            release_id=release_id,
            revisions=revisions,
            inspector=FakeInspector(),
            system="linux",
            machine="aarch64",
        )
