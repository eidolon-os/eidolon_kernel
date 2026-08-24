from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import rfc8785
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eidolon_sdk.device_foundation.v1 import (
    AckClaimGrant,
    AckClaimGrantResult,
    ClaimGrant,
    CollectClaimGrant,
    CollectClaimGrantResult,
    CommissioningProof,
    ControllerActorRef,
    CreateEnrollment,
    CreateEnrollmentResult,
    DecideEnrollment,
    DecideEnrollmentResult,
    DeviceRef,
    HandoffPublicKey,
    HardwareIdentityEvidence,
    ManifestDocument,
    ManifestRef,
    OperationalPublicKey,
)

ROOT = Path(__file__).resolve().parents[2]
HUB_ROOT = ROOT.parent / "eidolon_hub"
ADMIN_ROOT = ROOT.parent / "eidolon_admin"
SUPERVISORD = ADMIN_ROOT / ".venv/bin/supervisord"
SUPERVISORCTL = ADMIN_ROOT / ".venv/bin/supervisorctl"
HUB_UVICORN = HUB_ROOT / ".venv/bin/uvicorn"
HUB_PYTHON = HUB_ROOT / ".venv/bin/python"
EIDOLOND = ROOT / ".venv/bin/eidolond"
OWNER_DIRECTORY_HELPER = Path(__file__).with_name("owner_directory_material.py")
HUB_PROCESS_APP_ROOT = Path(__file__).parent

HUB_MANAGEMENT_SECRET = "m2b-hub-management-secret-value-0001"
HUB_READER_TOKEN = "m2b-hub-registry-reader-token-value-0001"
HUB_PROVIDER_TOKEN = "m2b-hub-channel-provider-token-value-0001"
COMPANION_TOKEN = "m2b-unused-companion-authority-token"
OWNER_DOMAIN_ID = "owner-m2b"
BUSINESS_OWNER_ID = "owner_m2b"
OWNER_HEADERS = {"X-Eidolon-Owner": BUSINESS_OWNER_ID}
SETUP_SECRET = b"m2b-process-e2e-setup-secret-value-0001"

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _process_failure(process: subprocess.Popen[str], label: str) -> str:
    stdout, stderr = process.communicate()
    return f"{label} exited unexpectedly\nstdout={stdout}\nstderr={stderr}"


def _supervisorctl(config: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SUPERVISORCTL), "-c", str(config), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


async def _eventually(
    operation: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    message: str,
    process: subprocess.Popen[str] | None = None,
    diagnostics: Callable[[], str] | None = None,
    timeout: float = 15,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Any = None
    while asyncio.get_running_loop().time() < deadline:
        if process is not None and process.poll() is not None:
            pytest.fail(_process_failure(process, message))
        try:
            result = operation()
            last = await result if hasattr(result, "__await__") else result
            if predicate(last):
                return last
        except (httpx.HTTPError, OSError, subprocess.SubprocessError) as exc:
            last = exc
        await asyncio.sleep(0.1)
    if isinstance(last, httpx.Response):
        last_detail = f"status={last.status_code} body={last.text}"
    else:
        last_detail = repr(last)
    diagnostic_text = f"\n{diagnostics()}" if diagnostics is not None else ""
    pytest.fail(f"{message}; last result={last_detail}{diagnostic_text}")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _management_token() -> str:
    actor = ControllerActorRef(
        principal_id="controller-m2b-e2e",
        owner_domain_id=OWNER_DOMAIN_ID,
        granted_scopes=(
            "device.read",
            "device.claim.approve",
            "device.claim.events.read",
        ),
        authentication_strength="hardware-backed",
    )
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": "controller-m2b-e2e",
                "presenter": "controller-m2b-e2e",
                "aud": "eidolon-admission",
                "actor": actor.model_dump(mode="json"),
                "owner_domain_id": OWNER_DOMAIN_ID,
                "business_owner_id": BUSINESS_OWNER_ID,
                "scopes": list(actor.granted_scopes),
                "roles": ["device-manager"],
                "owner_id": BUSINESS_OWNER_ID,
                "exp": int(time.time()) + 300,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(
        hmac.new(HUB_MANAGEMENT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    )
    return f"Bearer {header}.{payload}.{signature}"


def _write_runtime_configuration(root: Path, *, hub_port: int, kernel_port: int) -> dict[str, Path]:
    supervisor_socket = root / "supervisor.sock"
    system_socket = root / "system.sock"
    supervisor_config = root / "supervisord.conf"
    hub_settings = root / "hub.yaml"
    kernel_settings = root / "kernel.yaml"
    manifest = root / "services.yaml"
    system_settings = root / "eidolond.yaml"
    commissioning_registry = root / "commissioning-secrets.json"
    hub_actor_token = _management_token().removeprefix("Bearer ")

    commissioning_registry.write_text(
        json.dumps(
            {
                "profile": "eidolon-development-hmac-commissioning-v1",
                "devices": {
                    device_id: _b64url(SETUP_SECRET)
                    for device_id in ("device-m2b-a", "device-m2b-b")
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    commissioning_registry.chmod(0o600)
    registry_metadata = commissioning_registry.stat()
    assert registry_metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(registry_metadata.st_mode) == 0o600

    subprocess.run(
        [
            str(HUB_PYTHON),
            str(OWNER_DIRECTORY_HELPER),
            str(root),
            "hub.m2b.invalid",
            "owner-m2b",
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    hub_settings.write_text(
        f"""onboarding:
  owner_domain_id: {OWNER_DOMAIN_ID}
  owner_domain_generation: 1
  trust_epoch: 1
  descriptor_uri: https://hub.m2b.invalid/api/device-onboarding/v1/descriptor
  descriptor_path: {root / "owner-domain-descriptor.json"}
  owner_root_certificate_path: {root / "owner-domain-root.pem"}
  authority_signing_certificate_path: {root / "authority-signing.pem"}
discovery:
  mdns:
    enabled: false
commissioning_proof:
  profile: development-hmac
  setup_secret_registry_path: {commissioning_registry}
persistence:
  path: {root / "hub.sqlite3"}
""",
        encoding="utf-8",
    )
    kernel_settings.write_text(
        f"""persistence:
  path: {root / "kernel.sqlite3"}
system_directory:
  base_url: http://eidolond
  uds_path: {system_socket}
  timeout_seconds: 1
hub:
  timeout_seconds: 1
companion_authority:
  timeout_seconds: 1
reconciliation:
  interval_seconds: 3600
deployment:
  mode: trusted-local
  trusted_local_ingress: true
""",
        encoding="utf-8",
    )
    manifest.write_text(
        f"""version: 1
services:
  - service_id: hub
    required: true
    enabled_by_default: true
    dependencies: []
    host_targets:
      supervisord: hub:hub-api
    endpoints:
      - endpoint_id: device-authority.http
        protocol: http
        address: http://127.0.0.1:{hub_port}
        contract: eidolon.hub.device-directory.v1
        health_url: http://127.0.0.1:{hub_port}/health
  - service_id: kernel
    required: true
    enabled_by_default: true
    dependencies: []
    host_targets:
      supervisord: kernel:kernel-api
    endpoints:
      - endpoint_id: device-mount.http
        protocol: http
        address: http://127.0.0.1:{kernel_port}
        contract: eidolon.kernel.device-mount.v1
        health_url: http://127.0.0.1:{kernel_port}/health
""",
        encoding="utf-8",
    )
    system_settings.write_text(
        f"""manifest:
  path: {manifest}
persistence:
  path: {root / "eidolond.sqlite3"}
host:
  driver: supervisord
  supervisorctl: {SUPERVISORCTL}
  supervisor_config: {supervisor_config}
  command_timeout_seconds: 5
reconciliation:
  interval_seconds: 1
  readiness_timeout_seconds: 0.3
interface:
  host: 127.0.0.1
  port: 8090
  uds: {system_socket}
  uds_mode: "0600"
""",
        encoding="utf-8",
    )
    supervisor_config.write_text(
        f"""[unix_http_server]
file={supervisor_socket}
chmod=0700

[supervisord]
nodaemon=true
logfile={root / "supervisord.log"}
pidfile={root / "supervisord.pid"}
childlogdir={root}

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://{supervisor_socket}

[program:hub-api]
command={HUB_UVICORN} --app-dir {HUB_PROCESS_APP_ROOT} hub_process_app:create_app --factory --host 127.0.0.1 --port {hub_port} --log-level warning
directory={HUB_ROOT}
autostart=false
autorestart=true
startsecs=1
stopsignal=TERM
stopwaitsecs=10
stdout_logfile={root / "hub.log"}
stderr_logfile={root / "hub.err.log"}
environment=EIDOLON_HUB_SETTINGS_YAML="{hub_settings}",EIDOLON_HUB_MANAGEMENT_JWT_SECRET="{HUB_MANAGEMENT_SECRET}",EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN="{HUB_READER_TOKEN}",EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN="{HUB_PROVIDER_TOKEN}"

[group:hub]
programs=hub-api

[program:kernel-api]
command={sys.executable} -m uvicorn eidolon_kernel.main:create_app --factory --host 127.0.0.1 --port {kernel_port} --log-level warning
directory={ROOT}
autostart=false
autorestart=true
startsecs=1
stopsignal=TERM
stopwaitsecs=10
stdout_logfile={root / "kernel.log"}
stderr_logfile={root / "kernel.err.log"}
environment=EIDOLON_KERNEL_SETTINGS_YAML="{kernel_settings}",EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN="{hub_actor_token}",EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN="{COMPANION_TOKEN}"

[group:kernel]
programs=kernel-api
""",
        encoding="utf-8",
    )
    return {
        "supervisor": supervisor_config,
        "system": system_settings,
        "system_socket": system_socket,
        "hub_stderr": root / "hub.err.log",
    }


def _start_eidolond(settings: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(EIDOLOND)],
        cwd=ROOT,
        env={**os.environ, "EIDOLON_SYSTEM_SETTINGS_YAML": str(settings)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _p256_spki(key: ec.EllipticCurvePrivateKey) -> str:
    encoded = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return "p256-spki:" + _b64url(encoded)


def _sign(key: ec.EllipticCurvePrivateKey, document: dict[str, object]) -> str:
    der = key.sign(rfc8785.dumps(document), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _open_claim_grant(
    recipient: ec.EllipticCurvePrivateKey,
    collected: CollectClaimGrantResult,
) -> ClaimGrant:
    if str(HUB_ROOT) not in sys.path:
        sys.path.insert(0, str(HUB_ROOT))
    admission_crypto = importlib.import_module("hub.admission.crypto")
    envelope = collected.wire_envelope
    aad = envelope.aad.model_dump(mode="json")
    encapsulated = admission_crypto._decode(envelope.encapsulated_key)  # noqa: SLF001
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), encapsulated)
    recipient_point = recipient.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    kem_suite = b"KEM" + (16).to_bytes(2, "big")
    shared = admission_crypto._labeled_expand(  # noqa: SLF001
        kem_suite,
        admission_crypto._labeled_extract(  # noqa: SLF001
            kem_suite,
            b"",
            b"eae_prk",
            recipient.exchange(ec.ECDH(), ephemeral),
        ),
        b"shared_secret",
        encapsulated + recipient_point,
        32,
    )
    suite = b"HPKE" + (16).to_bytes(2, "big") + (1).to_bytes(2, "big") * 2
    context = (
        b"\x00"
        + admission_crypto._labeled_extract(  # noqa: SLF001
            suite, b"", b"psk_id_hash", b""
        )
        + admission_crypto._labeled_extract(  # noqa: SLF001
            suite,
            b"",
            b"info_hash",
            b"eidolon-trust-p256-hpke-v1",
        )
    )
    secret = admission_crypto._labeled_extract(  # noqa: SLF001
        suite, shared, b"secret", b""
    )
    key = admission_crypto._labeled_expand(  # noqa: SLF001
        suite, secret, b"key", context, 16
    )
    nonce = admission_crypto._labeled_expand(  # noqa: SLF001
        suite, secret, b"base_nonce", context, 12
    )
    plaintext = AESGCM(key).decrypt(
        nonce,
        admission_crypto._decode(envelope.ciphertext),  # noqa: SLF001
        rfc8785.dumps(aad),
    )
    grant = ClaimGrant.model_validate_json(plaintext)
    envelope.aad.assert_matches_grant(grant)
    return grant


def _create_enrollment(
    *,
    device_id: str,
    suffix: str,
    handoff_key: ec.EllipticCurvePrivateKey,
    operational_key: ec.EllipticCurvePrivateKey,
) -> tuple[CreateEnrollment, ManifestRef]:
    manifest_document = {
        "schema_version": 1,
        "title": f"M2-B device {suffix}",
        "properties": [],
        "actions": [],
        "events": [],
        "media": [],
    }
    manifest = ManifestDocument(
        manifest_id=f"manifest-{suffix}",
        revision=1,
        digest="sha256:" + hashlib.sha256(rfc8785.dumps(manifest_document)).hexdigest(),
        document=manifest_document,
    )
    nonce = f"commissioning-nonce-{suffix}-0001"
    proof_document = f"{device_id}\0{OWNER_DOMAIN_ID}\0{nonce}".encode()
    command = CreateEnrollment(
        device_instance_candidate_id=device_id,
        requested_owner_domain_id=OWNER_DOMAIN_ID,
        hardware_identity_evidence=HardwareIdentityEvidence(
            scheme="manufacturer-p256",
            evidence=f"manufacturer-evidence-{suffix}-0001",
            evidence_digest="sha256:"
            + hashlib.sha256(f"manufacturer-evidence-{suffix}-0001".encode()).hexdigest(),
        ),
        commissioning_proof=CommissioningProof(
            proof=_b64url(hmac.new(SETUP_SECRET, proof_document, hashlib.sha256).digest()),
            nonce=nonce,
        ),
        manifest=manifest,
        handoff_key=HandoffPublicKey(public_key=_p256_spki(handoff_key)),
        operational_key=OperationalPublicKey(public_key=_p256_spki(operational_key)),
    )
    return command, ManifestRef(
        manifest_id=manifest.manifest_id,
        revision=manifest.revision,
        digest=manifest.digest,
    )


async def _activate_claim(hub: httpx.AsyncClient, *, device_id: str, suffix: str) -> DeviceRef:
    handoff_key = ec.generate_private_key(ec.SECP256R1())
    operational_key = ec.generate_private_key(ec.SECP256R1())
    create_command, manifest_ref = _create_enrollment(
        device_id=device_id,
        suffix=suffix,
        handoff_key=handoff_key,
        operational_key=operational_key,
    )
    create_body = create_command.model_dump(mode="json")
    create_body.update(
        command_id=f"create-{suffix}",
        correlation_id=f"commission-{suffix}",
    )
    create_response = await hub.post("/api/admission/v1/enrollments", json=create_body)
    assert create_response.status_code == 201, create_response.text
    created = CreateEnrollmentResult.model_validate(create_response.json())
    assert created.reviewed_manifest_digest == manifest_ref.digest

    decision_command = DecideEnrollment(
        enrollment_id=created.enrollment_id,
        expected_proposal_revision=created.proposal_revision,
        decision="approve",
        target_owner_domain_id=OWNER_DOMAIN_ID,
        target_business_owner_id=BUSINESS_OWNER_ID,
        target_space_id=None,
        reviewed_manifest_ref=manifest_ref,
        initial_assignment_intent=None,
        initial_capability_policy_refs=(),
    )
    decision_body = decision_command.model_dump(mode="json")
    decision_body.update(
        command_id=f"decide-{suffix}",
        correlation_id=f"commission-{suffix}",
    )
    decision_response = await hub.post(
        f"/api/admission/v1/enrollments/{created.enrollment_id}/decisions",
        headers={"Authorization": _management_token()},
        json=decision_body,
    )
    assert decision_response.status_code == 200, decision_response.text
    decision = DecideEnrollmentResult.model_validate(decision_response.json())
    assert decision.decision == "approve"

    collection_document = {
        "contract": "eidolon.device-foundation.claim-grant-collection",
        "enrollment_id": created.enrollment_id,
        "proposal_revision": created.proposal_revision,
        "collection_challenge": created.collection_challenge,
    }
    collect_command = CollectClaimGrant(
        enrollment_id=created.enrollment_id,
        proposal_revision=created.proposal_revision,
        collection_challenge=created.collection_challenge,
        handoff_key_proof=_sign(handoff_key, collection_document),
    )
    collect_body = collect_command.model_dump(mode="json")
    collect_body.update(
        command_id=f"collect-{suffix}",
        correlation_id=f"commission-{suffix}",
    )
    collect_response = await hub.post(
        f"/api/admission/v1/enrollments/{created.enrollment_id}/claim-grants:collect",
        json=collect_body,
    )
    assert collect_response.status_code == 200, collect_response.text
    collected = CollectClaimGrantResult.model_validate(collect_response.json())
    grant = _open_claim_grant(handoff_key, collected)
    assert grant.approval_decision_id == decision.decision_id

    acknowledgement_document = {
        "contract": "eidolon.device-foundation.claim-grant-ack",
        "enrollment_id": created.enrollment_id,
        "grant_id": grant.grant_id,
        "device_ref": grant.device_ref.model_dump(mode="json"),
    }
    ack_command = AckClaimGrant(
        enrollment_id=created.enrollment_id,
        grant_id=grant.grant_id,
        operational_key_proof=_sign(operational_key, acknowledgement_document),
        stored_claim_generation=grant.device_ref.claim_generation,
        stored_trust_epoch=grant.device_ref.trust_epoch,
    )
    ack_body = ack_command.model_dump(mode="json")
    ack_body.update(
        command_id=f"ack-{suffix}",
        correlation_id=f"commission-{suffix}",
    )
    ack_response = await hub.post(
        f"/api/admission/v1/enrollments/{created.enrollment_id}/claim-grants/{grant.grant_id}:ack",
        json=ack_body,
    )
    assert ack_response.status_code == 200, ack_response.text
    active = AckClaimGrantResult.model_validate(ack_response.json())
    assert active.claim_state == "active"
    assert active.device_ref == grant.device_ref
    return active.device_ref


def _mount_body(device_id: str, request_id: str) -> dict[str, object]:
    return {
        "operation": "device.mount",
        "request_id": request_id,
        "device_id": device_id,
        "expected_revision": 0,
        "replace_existing": False,
    }


@pytest.mark.asyncio
async def test_real_single_host_boot_directory_fault_and_mount_recovery() -> None:
    required = (SUPERVISORD, SUPERVISORCTL, HUB_UVICORN, EIDOLOND)
    if not HUB_ROOT.is_dir() or not all(path.is_file() for path in required):
        pytest.skip("real Hub/supervisord development runtimes are unavailable")

    supervisor_process: subprocess.Popen[str] | None = None
    system_process: subprocess.Popen[str] | None = None
    paused_hub_pid: int | None = None
    with tempfile.TemporaryDirectory(prefix="eidolon-m2b-", dir="/private/tmp") as directory:
        runtime_root = Path(directory)
        hub_port = _free_port()
        kernel_port = _free_port()
        paths = _write_runtime_configuration(
            runtime_root,
            hub_port=hub_port,
            kernel_port=kernel_port,
        )
        try:
            supervisor_process = subprocess.Popen(
                [str(SUPERVISORD), "-n", "-c", str(paths["supervisor"])],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            await _eventually(
                lambda: _supervisorctl(paths["supervisor"], "status"),
                lambda result: (
                    "hub:hub-api" in result.stdout and "kernel:kernel-api" in result.stdout
                ),
                message="supervisord did not expose the managed programs",
                process=supervisor_process,
            )

            system_process = _start_eidolond(paths["system"])
            system_transport = httpx.AsyncHTTPTransport(uds=str(paths["system_socket"]))
            async with httpx.AsyncClient(
                transport=system_transport,
                base_url="http://eidolond.local",
                timeout=10,
                trust_env=False,
            ) as system:
                await _eventually(
                    lambda: system.get("/api/system/v1/services"),
                    lambda response: (
                        response.status_code == 200
                        and {item["runtime_state"] for item in response.json()["services"]}
                        == {"ready"}
                    ),
                    message="eidolond did not start and publish Hub/Kernel",
                    process=system_process,
                    diagnostics=lambda: (
                        "Hub stderr:\n" + paths["hub_stderr"].read_text(encoding="utf-8")
                    ),
                )

                async with (
                    httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{hub_port}",
                        timeout=2,
                        trust_env=False,
                    ) as hub,
                    httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{kernel_port}",
                        timeout=2,
                        trust_env=False,
                    ) as kernel,
                ):
                    await _activate_claim(hub, device_id="device-m2b-a", suffix="m2b-a")
                    visible = await hub.get(
                        f"/api/device-management/v1/owners/{BUSINESS_OWNER_ID}"
                        "/devices/device-m2b-a",
                        headers={"Authorization": _management_token()},
                    )
                    assert visible.status_code == 200, (
                        f"{visible.status_code}: {visible.text}\n"
                        f"Hub stderr:\n{paths['hub_stderr'].read_text(encoding='utf-8')}"
                    )

                    mounted = await _eventually(
                        lambda: kernel.get(
                            "/api/kernel/v1/device-mounts/devices/device-m2b-a",
                            headers=OWNER_HEADERS,
                        ),
                        lambda response: response.status_code == 200,
                        message="ClaimActivated did not drive the independent Kernel Mount",
                    )
                    assert mounted.json()["owner_id"] == BUSINESS_OWNER_ID
                    assert mounted.json()["device_ref"]["owner_domain_id"] == OWNER_DOMAIN_ID

                    service = await system.get("/api/system/v1/services/kernel")
                    revision = service.json()["desired"]["revision"]
                    restarted = await system.post(
                        "/api/system/v1/services/kernel/restart",
                        json={
                            "operation": "system.service.restart",
                            "request_id": "restart-kernel-m2b",
                            "expected_revision": revision,
                        },
                    )
                    assert restarted.status_code == 200, restarted.text
                    await _eventually(
                        lambda: kernel.get("/health"),
                        lambda response: (
                            response.status_code == 200
                            and response.json()["device_mount_write_available"] is True
                        ),
                        message="Kernel did not become ready after managed restart",
                    )
                    persisted = await kernel.get(
                        "/api/kernel/v1/device-mounts/devices/device-m2b-a",
                        headers=OWNER_HEADERS,
                    )
                    assert persisted.status_code == 200
                    assert persisted.json()["revision"] == 1

                    pid_result = _supervisorctl(paths["supervisor"], "pid", "hub:hub-api")
                    assert pid_result.returncode == 0, pid_result.stderr
                    paused_hub_pid = int(pid_result.stdout.strip())
                    os.kill(paused_hub_pid, signal.SIGSTOP)

                    await _eventually(
                        lambda: system.get("/api/system/v1/services/hub"),
                        lambda response: (
                            response.status_code == 200
                            and response.json()["runtime_state"] == "degraded"
                        ),
                        message="eidolond did not withdraw the paused Hub",
                    )
                    unresolved = await system.get(
                        "/api/system/v1/services/hub/endpoints/device-authority.http"
                    )
                    assert unresolved.status_code == 503
                    await _eventually(
                        lambda: kernel.get("/health"),
                        lambda response: (
                            response.status_code == 200 and response.json()["status"] == "degraded"
                        ),
                        message="Kernel did not expose Hub-dependent write degradation",
                    )
                    still_readable = await kernel.get(
                        "/api/kernel/v1/device-mounts/devices/device-m2b-a",
                        headers=OWNER_HEADERS,
                    )
                    assert still_readable.status_code == 200
                    failed_mount = await kernel.post(
                        "/api/kernel/v1/device-mounts",
                        headers=OWNER_HEADERS,
                        json=_mount_body("device-m2b-b", "mount-m2b-b"),
                    )
                    assert failed_mount.status_code == 503

                    os.kill(paused_hub_pid, signal.SIGCONT)
                    paused_hub_pid = None
                    await _eventually(
                        lambda: system.get("/api/system/v1/services/hub"),
                        lambda response: (
                            response.status_code == 200
                            and response.json()["runtime_state"] == "ready"
                        ),
                        message="eidolond did not republish the recovered Hub",
                    )
                    await _activate_claim(hub, device_id="device-m2b-b", suffix="m2b-b")
                    recovered = await _eventually(
                        lambda: kernel.get(
                            "/api/kernel/v1/device-mounts/devices/device-m2b-b",
                            headers=OWNER_HEADERS,
                        ),
                        lambda response: response.status_code == 200,
                        message="recovered Hub Claim stream did not drive Kernel Mount",
                    )
                    assert recovered.json()["device_ref"]["owner_domain_id"] == OWNER_DOMAIN_ID

                _stop(system_process)
                system_process = None

            system_process = _start_eidolond(paths["system"])
            restarted_transport = httpx.AsyncHTTPTransport(uds=str(paths["system_socket"]))
            async with httpx.AsyncClient(
                transport=restarted_transport,
                base_url="http://eidolond.local",
                timeout=10,
                trust_env=False,
            ) as restarted_system:
                audit = await _eventually(
                    lambda: restarted_system.get("/api/system/v1/audit/events"),
                    lambda response: (
                        response.status_code == 200 and response.json()["next_position"] == 1
                    ),
                    message="eidolond did not restore desired state and audit after restart",
                    process=system_process,
                )
                assert audit.json()["events"][0]["request_id"] == "restart-kernel-m2b"
        finally:
            if paused_hub_pid is not None:
                os.kill(paused_hub_pid, signal.SIGCONT)
            _stop(system_process)
            _stop(supervisor_process)
