from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eidolon_deploy.contract import (
    ACTIVATOR_RELATIVE_PATH,
    ACTIVATOR_SOURCE_RELATIVE_PATH,
    INTERPRETER_RELATIVE_PATH,
    INTERPRETER_SOURCE_RELATIVE_PATH,
)
from eidolon_deploy.manifest import (
    V2_COMPONENT_ENTRYPOINTS,
    V2_SYSTEM_ASSETS,
    load_release_descriptor,
)
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
    release_root = _host_path(root, f"/opt/eidolon/releases/{release_id}")
    sources = {
        source_id: release_root / source_id
        for source_id in (*V2_COMPONENT_ENTRYPOINTS, "eidolon_sdk")
    }
    for source in sources.values():
        source.mkdir(parents=True)
        (source / "pyproject.toml").write_text(f"[project]\nname='{source.name}'\n")
    for component_id, entrypoints in V2_COMPONENT_ENTRYPOINTS.items():
        component = sources[component_id]
        (component / "uv.lock").write_text(f"lock:{component.name}\n")
        python = component / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        extra = (
            (Path(".venv/bin/eidolon-release"),) if component_id == "eidolon_kernel" else ()
        )
        for relative in (*entrypoints, *extra):
            executable = component / relative
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
    for _destination, (source_id, relative) in V2_SYSTEM_ASSETS.items():
        asset = sources[source_id] / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(f"asset:{relative}\n")
    return root


def _revisions(**overrides: str) -> ReleaseRevisions:
    values = {
        "kernel": "a" * 40,
        "data": "b" * 40,
        "hub": "d" * 40,
        "admin": "e" * 40,
        "agent": "f" * 40,
        "channel": "1" * 40,
        "memory": "2" * 40,
        "sdk": "c" * 40,
    }
    values.update(overrides)
    return ReleaseRevisions(**values)


def test_seals_fixed_target_release_from_prepared_native_tree(tmp_path: Path) -> None:
    release_id = "20260806-m2d-seal"
    root = _prepared_tree(tmp_path, release_id)

    path = seal_prepared_release(
        host_root=root,
        release_id=release_id,
        revisions=_revisions(),
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
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
    ]
    assert release.support_sources[0].source_id == "eidolon_sdk"
    assert {item.destination for item in release.system_assets} == set(V2_SYSTEM_ASSETS)
    assert json.loads(path.read_text())["database_migrations"] == []


def test_sealing_rejects_wrong_target_or_incomplete_preparation(tmp_path: Path) -> None:
    release_id = "20260806-m2d-invalid"
    root = _prepared_tree(tmp_path, release_id)
    revisions = _revisions()

    with pytest.raises(PreparationError, match="target host"):
        seal_prepared_release(
            host_root=root,
            release_id=release_id,
            revisions=revisions,
            inspector=FakeInspector(),
            system="darwin",
            machine="arm64",
        )

    (_host_path(root, f"/opt/eidolon/releases/{release_id}") / "eidolon_sdk").rename(
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
        _revisions(kernel="main")


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
    revisions = _revisions()
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


def test_sealing_publishes_a_component_neutral_activator(tmp_path: Path) -> None:
    """Operator tooling resolves the activator without naming a component."""

    release_id = "20260811-activator"
    root = _prepared_tree(tmp_path, release_id)

    seal_prepared_release(
        host_root=root,
        release_id=release_id,
        revisions=_revisions(),
        inspector=FakeInspector(),
        system="linux",
        machine="aarch64",
    )

    release_root = _host_path(root, f"/opt/eidolon/releases/{release_id}")
    for entry_relative, source_relative in (
        (ACTIVATOR_RELATIVE_PATH, ACTIVATOR_SOURCE_RELATIVE_PATH),
        (INTERPRETER_RELATIVE_PATH, INTERPRETER_SOURCE_RELATIVE_PATH),
    ):
        published = release_root / entry_relative
        source = release_root / source_relative
        # Not a symlink: CPython finds pyvenv.cfg beside the invoked path, so a
        # symlinked interpreter silently becomes the system Python.
        assert not published.is_symlink()
        assert os.access(published, os.X_OK)
        assert str(source) in published.read_text(encoding="utf-8")

    interpreter = release_root / INTERPRETER_RELATIVE_PATH
    source = release_root / INTERPRETER_SOURCE_RELATIVE_PATH
    source.write_text('#!/bin/sh\necho "$@" from-release-venv\n', encoding="utf-8")
    source.chmod(0o755)
    result = subprocess.run(
        [str(interpreter), "-c", "marker"], capture_output=True, text=True, check=True
    )
    assert "from-release-venv" in result.stdout
    assert "-c marker" in result.stdout


def test_sealing_refuses_a_release_without_its_activator(tmp_path: Path) -> None:
    release_id = "20260811-no-activator"
    root = _prepared_tree(tmp_path, release_id)
    release_root = _host_path(root, f"/opt/eidolon/releases/{release_id}")
    (release_root / ACTIVATOR_SOURCE_RELATIVE_PATH).unlink()

    with pytest.raises(PreparationError, match="operator entry"):
        seal_prepared_release(
            host_root=root,
            release_id=release_id,
            revisions=_revisions(),
            inspector=FakeInspector(),
            system="linux",
            machine="aarch64",
        )


def test_package_digest_is_reproducible_from_a_tree_without_this_package(
    tmp_path: Path,
) -> None:
    """The algorithm is normative: a consumer recomputes it from Git objects.

    This vector is shared with eidolon_ops, which recomputes the same digest
    from the pinned Kernel revision to prove the activator it runs is the one
    that will ship. If either side drifts, one of the two tests fails.
    """

    from eidolon_deploy.contract import package_digest

    root = tmp_path / "pkg"
    (root / "contracts/schemas").mkdir(parents=True)
    (root / "bundle.py").write_text("bundle\n", encoding="utf-8")
    (root / "cli.py").write_text("cli\n", encoding="utf-8")
    (root / "contracts/schemas/x.schema.json").write_text("{}\n", encoding="utf-8")
    # Neither compiled output nor unrelated files may move the digest.
    (root / "__pycache__").mkdir()
    (root / "__pycache__/cli.cpython-313.pyc").write_bytes(b"\x00compiled")
    (root / "README.md").write_text("ignored\n", encoding="utf-8")

    # Shared with eidolon_ops, which recomputes this from `git ls-tree` blob ids.
    assert package_digest(root) == (
        "280d4ab1ee9356bb66788af6769817df8d4069d88310e47999ff0d899c004883"
    )
