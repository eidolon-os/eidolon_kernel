"""Self-description consumed by operator tooling instead of repository layout.

An operator tool must be able to prove that the activator it is about to run
speaks the same release format it is about to build, without knowing where the
tool lives on disk or whether its Git worktree is clean.  The activator reports
that itself here, and every release publishes it at a stable, component-neutral
path so callers never spell a component directory name.
"""

from __future__ import annotations

from pathlib import PurePosixPath

TOOL_ID = "eidolon-release"

#: Bumped whenever the operator-facing CLI surface or its JSON output changes.
CLI_CONTRACT_VERSION = 1

#: Wire formats an operator tool has to agree on to interoperate with a release.
BUNDLE_SCHEMA_VERSION = 2
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
    }
