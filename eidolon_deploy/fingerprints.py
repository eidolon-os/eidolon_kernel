"""Deterministic fingerprints for prepared release contents."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)

INSTALLED_DISTRIBUTIONS_SCRIPT = """\
import importlib.metadata as metadata
rows = []
for distribution in metadata.distributions():
    name = distribution.metadata.get('Name')
    if name:
        rows.append(f'{name}=={distribution.version}')
print('\\n'.join(sorted(rows, key=str.casefold)))
"""


def environment_sha256(freeze_output: str) -> str:
    """Hash an installed Python environment independent of output ordering."""

    requirements = sorted(
        (line.strip() for line in freeze_output.splitlines() if line.strip()),
        key=str.casefold,
    )
    canonical = "".join(f"{line}\n" for line in requirements).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def source_tree_sha256(root: Path) -> str:
    """Hash immutable release sources while excluding generated runtime state."""

    if not root.is_dir():
        raise ValueError(f"source tree is not a directory: {root}")

    digest = hashlib.sha256()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        symlink_directories = [
            name for name in directory_names if (Path(directory) / name).is_symlink()
        ]
        if symlink_directories:
            raise ValueError(
                f"source tree symlink is not allowed: {Path(directory) / symlink_directories[0]}"
            )
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _IGNORED_DIRECTORY_NAMES and not name.endswith(".egg-info")
        )
        base = Path(directory)
        for name in sorted(file_names):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = base / name
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            if path.is_symlink():
                raise ValueError(f"source tree symlink is not allowed: {path}")
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise ValueError(f"unsupported source tree entry: {path}")
            digest.update(b"\0")
    return digest.hexdigest()
