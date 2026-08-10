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
    ReleaseActivator,
    RollbackFailed,
    receipt_to_document,
)
from eidolon_deploy.bundle import BundleError, build_source_bundle
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
            )
            path = build_source_bundle(
                release_id=arguments.release_id,
                repositories={
                    "eidolon_kernel": arguments.kernel_repo,
                    "eidolon_data": arguments.data_repo,
                    "eidolon_hub": arguments.hub_repo,
                    "eidolon_admin": arguments.admin_repo,
                    "eidolon_agent": arguments.agent_repo,
                    "eidolon_channel": arguments.channel_repo,
                    "eidolon_memory": arguments.memory_repo,
                    "eidolon_sdk": arguments.sdk_repo,
                },
                revisions=revisions,
                output=arguments.output,
                uv=arguments.uv,
            )
            _print_json({"status": "bundled", "manifest": str(path)})
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
                ),
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
    bundle.add_argument("--kernel-revision", required=True)
    bundle.add_argument("--data-revision", required=True)
    bundle.add_argument("--hub-revision", required=True)
    bundle.add_argument("--admin-revision", required=True)
    bundle.add_argument("--agent-revision", required=True)
    bundle.add_argument("--channel-revision", required=True)
    bundle.add_argument("--memory-revision", required=True)
    bundle.add_argument("--sdk-revision", required=True)
    bundle.add_argument("--uv", default="uv", help="exact uv 0.11.15 executable")

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


def _print_json(document: object, *, stream=None) -> None:
    print(json.dumps(document, sort_keys=True), file=stream or sys.stdout)
