"""Self-description consumed by operator tooling instead of repository layout.

An operator tool must be able to prove that the activator it is about to run
speaks the same release format it is about to build, without knowing where the
tool lives on disk or whether its Git worktree is clean.  The activator reports
that itself here, and every release publishes it at a stable, component-neutral
path so callers never spell a component directory name.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

TOOL_ID = "eidolon-release"

#: Bumped whenever the operator-facing CLI surface or its JSON output changes.
CLI_CONTRACT_VERSION = 1

#: Wire formats an operator tool has to agree on to interoperate with a release.
BUNDLE_SCHEMA_VERSION = 3
DESCRIPTOR_SCHEMA_VERSION = 2
SNAPSHOT_SCHEMA_VERSION = 2

#: Component-neutral location of the activator inside a sealed release, relative
#: to ``/opt/eidolon/releases/<release-id>``.  Callers resolve the activator
#: through this path so that moving it between components stays invisible.
ACTIVATOR_RELATIVE_PATH = PurePosixPath(".release/bin/eidolon-release")

#: Interpreter able to import :mod:`eidolon_deploy`, published beside the
#: activator so an operator agent can run against the release without knowing
#: which component provides the environment.
INTERPRETER_RELATIVE_PATH = PurePosixPath(".release/bin/python")

#: Where the published entries physically live today.  Only sealing consumes
#: these; it is the one place allowed to know which component ships the tool.
ACTIVATOR_SOURCE_RELATIVE_PATH = PurePosixPath("eidolon_kernel/.venv/bin/eidolon-release")
INTERPRETER_SOURCE_RELATIVE_PATH = PurePosixPath("eidolon_kernel/.venv/bin/python")


#: Files that define this tool's behaviour. Anything outside this set cannot
#: change what a release does, so it stays out of the digest.
DIGEST_SUFFIXES = (".py", ".json")


def package_digest(package_root: Path | None = None) -> str:
    """Fingerprint this activator's own source.

    An operator tool has to prove it is running the same activator that will
    ship inside the release it is about to build. Contract versions only move
    when the wire format moves, so a behaviour fix leaves them untouched — this
    digest moves with any change at all.

    Normative algorithm, so a consumer can recompute it from Git metadata alone:
    for every file under the package whose suffix is in :data:`DIGEST_SUFFIXES`,
    excluding ``__pycache__``, take its Git blob id, then feed
    ``"<posix-relative-path>:<blob-id>\n"`` into SHA-256 in path order.

    Blob ids rather than raw bytes: a consumer reading ``git ls-tree`` already
    has them, and never has to decode file content to agree with this result.
    """

    root = package_root or Path(__file__).resolve().parent
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.suffix not in DIGEST_SUFFIXES
            or "__pycache__" in path.parts
        ):
            continue
        content = path.read_bytes()
        blob = hashlib.sha1(
            b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
        ).hexdigest()
        entries.append((path.relative_to(root).as_posix(), blob))
    return digest_of_blob_entries(entries)


def digest_of_blob_entries(entries: Iterable[tuple[str, str]]) -> str:
    """Combine ``(relative path, Git blob id)`` pairs into the package digest."""

    digest = hashlib.sha256()
    for relative, blob in sorted(entries):
        digest.update(f"{relative}:{blob}\n".encode("utf-8"))
    return digest.hexdigest()


def contract_document() -> dict[str, object]:
    """Report the formats this activator speaks."""

    return {
        "tool": TOOL_ID,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "descriptor_schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "activator_relative_path": str(ACTIVATOR_RELATIVE_PATH),
        "interpreter_relative_path": str(INTERPRETER_RELATIVE_PATH),
        "package_digest": package_digest(),
    }
