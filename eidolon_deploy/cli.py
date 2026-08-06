"""Root-operator entrypoint for target release sealing and activation."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from eidolon_deploy.activation import (
    ActivationFailed,
    ActivationReceipt,
    ReleaseActivator,
    RollbackFailed,
)
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
        if arguments.operation == "seal":
            path = seal_prepared_release(
                release_id=arguments.release_id,
                revisions=ReleaseRevisions(
                    kernel=arguments.kernel_revision,
                    data=arguments.data_revision,
                    sdk=arguments.sdk_revision,
                ),
            )
            _print_json({"status": "sealed", "descriptor": str(path)})
            return 0

        release = load_release_descriptor(arguments.descriptor)
        host = LinuxDeploymentHost()
        activator = ReleaseActivator(host)
        if arguments.operation == "activate":
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
    seal = operations.add_parser("seal", help="seal an already prepared native release")
    seal.add_argument("release_id")
    seal.add_argument("--kernel-revision", required=True)
    seal.add_argument("--data-revision", required=True)
    seal.add_argument("--sdk-revision", required=True)

    activate = operations.add_parser("activate", help="preflight and activate a release")
    activate.add_argument("descriptor", type=Path)
    activate.add_argument("--dry-run", action="store_true")

    rollback = operations.add_parser("rollback", help="restore an activation snapshot")
    rollback.add_argument("descriptor", type=Path)
    rollback.add_argument("snapshot", type=Path)
    return parser


def _print_receipt(receipt: ActivationReceipt, *, stream=None) -> None:
    document = asdict(receipt)
    document["status"] = receipt.status.value
    document["previous_targets"] = dict(receipt.previous_targets)
    _print_json(document, stream=stream)


def _print_json(document: object, *, stream=None) -> None:
    print(json.dumps(document, sort_keys=True), file=stream or sys.stdout)
