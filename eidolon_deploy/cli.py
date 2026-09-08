"""Root-operator entrypoint for target release sealing and activation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from eidolon_deploy.activation import (
    ActivationFailed,
    ActivationReceipt,
    ForwardFixRequired,
    ReleaseActivator,
    RestoredNotReady,
    RollbackFailed,
    receipt_to_document,
)
from eidolon_deploy.bundle import BundleError, build_source_bundle
from eidolon_deploy.capabilities import require_known_capabilities
from eidolon_deploy.contract import contract_document
from eidolon_deploy.linux import LinuxDeploymentError, LinuxDeploymentHost
from eidolon_deploy.manifest import ReleaseDescriptorError, load_release_descriptor
from eidolon_deploy.sealing import (
    PreparationError,
    ReleaseRevisions,
    seal_prepared_release,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "contract":
            _print_json(contract_document())
            return 0
        if arguments.operation == "bundle":
            revisions = ReleaseRevisions(
                kernel=arguments.kernel_revision,
                data=arguments.data_revision,
                hub=arguments.hub_revision,
                admin=arguments.admin_revision,
                agent=arguments.agent_revision,
                channel=arguments.channel_revision,
                memory=arguments.memory_revision,
                sdk=arguments.sdk_revision,
                models=arguments.models_revision,
            )
            capabilities = _capabilities(arguments.capability)
            repositories = {
                "eidolon_kernel": arguments.kernel_repo,
                "eidolon_data": arguments.data_repo,
                "eidolon_hub": arguments.hub_repo,
                "eidolon_admin": arguments.admin_repo,
                "eidolon_agent": arguments.agent_repo,
                "eidolon_channel": arguments.channel_repo,
                "eidolon_memory": arguments.memory_repo,
                "eidolon_sdk": arguments.sdk_repo,
            }
            if arguments.models_repo is not None:
                repositories["eidolon_models"] = arguments.models_repo
            built = build_source_bundle(
                release_id=arguments.release_id,
                repositories=repositories,
                revisions=revisions,
                output=arguments.output,
                uv=arguments.uv,
                cutover_mode=arguments.cutover_mode,
                capabilities=capabilities,
            )
            _print_json(
                {
                    "status": "bundled",
                    "manifest": str(built.manifest),
                    "notes": list(built.notes),
                }
            )
            return 0
        if arguments.operation == "seal":
            path = seal_prepared_release(
                release_id=arguments.release_id,
                revisions=ReleaseRevisions(
                    kernel=arguments.kernel_revision,
                    data=arguments.data_revision,
                    hub=arguments.hub_revision,
                    admin=arguments.admin_revision,
                    agent=arguments.agent_revision,
                    channel=arguments.channel_revision,
                    memory=arguments.memory_revision,
                    sdk=arguments.sdk_revision,
                    models=arguments.models_revision,
                ),
                cutover_mode=arguments.cutover_mode,
                capabilities=_capabilities(arguments.capability),
            )
            _print_json({"status": "sealed", "descriptor": str(path)})
            return 0

        release = load_release_descriptor(arguments.descriptor)
        host = LinuxDeploymentHost()
        if arguments.operation == "doctor":
            with host.exclusive_activation():
                _print_json({"status": "healthy", **host.doctor(release)})
            return 0
        activator = ReleaseActivator(host)
        if arguments.operation in {"activate", "deploy"}:
            receipt = activator.activate(release, dry_run=arguments.dry_run)
        else:
            snapshot = host.load_snapshot(release, arguments.snapshot)
            receipt = activator.rollback(release, snapshot)
        _print_receipt(receipt)
        return 0
    except ActivationFailed as exc:
        _print_receipt(exc.receipt, stream=sys.stderr)
        return 2
    except ForwardFixRequired as exc:
        _print_receipt(exc.receipt, stream=sys.stderr)
        return 4
    except RestoredNotReady as exc:
        # Its own code: the Host is on the restored release and that release
        # will not start. That is a different next action from a rollback that
        # left the Host somewhere nobody has established.
        _print_receipt(exc.receipt, stream=sys.stderr)
        return 5
    except RollbackFailed as exc:
        _print_json(
            {
                "status": "rollback_failed",
                "activation_error": exc.activation_error,
                "rollback_error": exc.rollback_error,
            },
            stream=sys.stderr,
        )
        return 3
    except (
        LinuxDeploymentError,
        BundleError,
        PreparationError,
        ReleaseDescriptorError,
        OSError,
    ) as exc:
        _print_json({"status": "failed", "error": str(exc)}, stream=sys.stderr)
        return 1


def run() -> None:
    raise SystemExit(main())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eidolon-release",
        description="Offline activation tool for a prepared Eidolon target release.",
    )
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser(
        "contract",
        help="report the release formats this activator speaks",
    )
    bundle = operations.add_parser(
        "bundle", help="archive exact reviewed commits for target-native preparation"
    )
    bundle.add_argument("release_id")
    bundle.add_argument("output", type=Path)
    bundle.add_argument("--kernel-repo", type=Path, required=True)
    bundle.add_argument("--data-repo", type=Path, required=True)
    bundle.add_argument("--hub-repo", type=Path, required=True)
    bundle.add_argument("--admin-repo", type=Path, required=True)
    bundle.add_argument("--agent-repo", type=Path, required=True)
    bundle.add_argument("--channel-repo", type=Path, required=True)
    bundle.add_argument("--memory-repo", type=Path, required=True)
    bundle.add_argument("--sdk-repo", type=Path, required=True)
    # Required exactly when a declared capability needs it, which argparse
    # cannot express, so it is refused in the operation instead.
    bundle.add_argument("--models-repo", type=Path)
    bundle.add_argument("--kernel-revision", required=True)
    bundle.add_argument("--data-revision", required=True)
    bundle.add_argument("--hub-revision", required=True)
    bundle.add_argument("--admin-revision", required=True)
    bundle.add_argument("--agent-revision", required=True)
    bundle.add_argument("--channel-revision", required=True)
    bundle.add_argument("--memory-revision", required=True)
    bundle.add_argument("--sdk-revision", required=True)
    bundle.add_argument("--models-revision")
    bundle.add_argument(
        "--capability",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "a capability the target Host provides, repeatable. Selects the "
            "conditional components, units, assets and readiness checks this "
            "release must carry; an unknown name is refused rather than "
            "selecting nothing."
        ),
    )
    bundle.add_argument("--uv", default="uv", help="exact uv 0.11.15 executable")
    bundle.add_argument(
        "--cutover-mode",
        choices=("reversible", "forward-only"),
        default="reversible",
    )

    seal = operations.add_parser("seal", help="seal an already prepared native release")
    seal.add_argument("release_id")
    seal.add_argument("--kernel-revision", required=True)
    seal.add_argument("--data-revision", required=True)
    seal.add_argument("--hub-revision", required=True)
    seal.add_argument("--admin-revision", required=True)
    seal.add_argument("--agent-revision", required=True)
    seal.add_argument("--channel-revision", required=True)
    seal.add_argument("--memory-revision", required=True)
    seal.add_argument("--sdk-revision", required=True)
    seal.add_argument("--models-revision")
    seal.add_argument(
        "--capability",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "a capability the target Host provides, repeatable. Selects the "
            "conditional components, units, assets and readiness checks this "
            "release must carry; an unknown name is refused rather than "
            "selecting nothing."
        ),
    )
    seal.add_argument(
        "--cutover-mode",
        choices=("reversible", "forward-only"),
        default="reversible",
    )

    activate = operations.add_parser("activate", help="preflight and activate a release")
    activate.add_argument("descriptor", type=Path)
    activate.add_argument("--dry-run", action="store_true")

    deploy = operations.add_parser(
        "deploy", help="preflight and atomically deploy a sealed release"
    )
    deploy.add_argument("descriptor", type=Path)
    deploy.add_argument("--dry-run", action="store_true")

    doctor = operations.add_parser("doctor", help="verify the active release, units and readiness")
    doctor.add_argument("descriptor", type=Path)

    rollback = operations.add_parser("rollback", help="restore an activation snapshot")
    rollback.add_argument("descriptor", type=Path)
    rollback.add_argument("snapshot", type=Path)
    return parser


def _print_receipt(receipt: ActivationReceipt, *, stream=None) -> None:
    _print_json(receipt_to_document(receipt), stream=stream)


def _capabilities(values: list[str]) -> frozenset[str]:
    """Read repeated --capability flags, refusing a name nobody defined.

    Refused here rather than left to select nothing: a misspelt capability
    would otherwise produce a release for a Host that declares nothing, be
    accepted as such, and install none of what the operator asked for.
    """

    try:
        return require_known_capabilities(list(values))
    except ValueError as exc:
        raise SystemExit(f"eidolon-deploy: {exc}") from exc


def _print_json(document: object, *, stream=None) -> None:
    print(json.dumps(document, sort_keys=True), file=stream or sys.stdout)
