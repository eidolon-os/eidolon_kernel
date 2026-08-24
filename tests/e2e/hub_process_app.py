"""Real Hub composition with one explicit process-E2E commissioning adapter."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from hub.admission.application import HmacCommissioningProofVerifier
from hub.composition import resources
from hub.composition.app import create_composed_app
from hub.config import HubConfig, load_hub_config


def _load_process_e2e_commissioning_verifier(
    config: HubConfig,
) -> tuple[HmacCommissioningProofVerifier, bool]:
    """Load a 0600, current-owner registry for this unprivileged process test only."""

    proof = config.commissioning_proof
    if proof.profile != "development-hmac":
        raise RuntimeError("process E2E requires the explicit development-hmac profile")
    path = Path(proof.setup_secret_registry_path or "")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError(
            "process E2E commissioning registry must be a current-owner 0600 regular file"
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("profile") != "eidolon-development-hmac-commissioning-v1":
        raise RuntimeError("process E2E commissioning registry profile differs")
    encoded = document.get("devices")
    if not isinstance(encoded, dict) or not encoded:
        raise RuntimeError("process E2E commissioning registry has no devices")
    try:
        secrets_by_device = {
            str(device_id): base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4))
            for device_id, value in encoded.items()
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError("process E2E commissioning registry is invalid") from exc
    if any(
        not device_id.strip() or len(secret) < 16 for device_id, secret in secrets_by_device.items()
    ):
        raise RuntimeError("process E2E commissioning registry entry is invalid")
    return HmacCommissioningProofVerifier(secrets_by_device.get), True


def create_app():
    """Create the production Hub app with only its commissioning verifier port injected."""

    config = load_hub_config()
    resources.load_commissioning_proof_verifier = _load_process_e2e_commissioning_verifier
    return create_composed_app(config)
