from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
HUB_ROOT = ROOT.parent / "eidolon_hub"
ADMIN_ROOT = ROOT.parent / "eidolon_admin"
SUPERVISORD = ADMIN_ROOT / ".venv/bin/supervisord"
SUPERVISORCTL = ADMIN_ROOT / ".venv/bin/supervisorctl"
HUB_UVICORN = HUB_ROOT / ".venv/bin/uvicorn"
EIDOLOND = ROOT / ".venv/bin/eidolond"

HUB_MANAGEMENT_SECRET = "m2b-hub-management-secret-value-0001"
HUB_READER_TOKEN = "m2b-hub-registry-reader-token-value-0001"
HUB_PROVIDER_TOKEN = "m2b-hub-channel-provider-token-value-0001"
COMPANION_TOKEN = "m2b-unused-companion-authority-token"
OWNER_HEADERS = {"X-Eidolon-Owner": "owner-m2b"}

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
    pytest.fail(f"{message}; last result={last_detail}")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _management_token() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": "m2b-e2e-admin",
                "aud": "eidolon-hub",
                "roles": ["hub-admin"],
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

    hub_settings.write_text(
        f"""onboarding:
  hub_id: eidolon-hub-m2b
  public_base_url: https://hub.m2b.invalid
  retrieval_window_seconds: 1800
discovery:
  mdns:
    enabled: false
channel_provider:
  contract_url: http://127.0.0.1:{_free_port()}/v1
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
  base_url: http://127.0.0.1:{_free_port()}
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
command={HUB_UVICORN} hub.main:app --host 127.0.0.1 --port {hub_port} --log-level warning
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
environment=EIDOLON_KERNEL_SETTINGS_YAML="{kernel_settings}",EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN="{HUB_READER_TOKEN}",EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN="{COMPANION_TOKEN}"

[group:kernel]
programs=kernel-api
""",
        encoding="utf-8",
    )
    return {
        "supervisor": supervisor_config,
        "system": system_settings,
        "system_socket": system_socket,
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


async def _approve_device(hub: httpx.AsyncClient, *, device_id: str, suffix: str) -> None:
    enrollment = await hub.post(
        "/api/device-onboarding/v1/enrollments",
        json={
            "operation": "device.enrollment",
            "request_id": f"enroll-{suffix}",
            "retrieval_token": f"device-generated-retrieval-token-{suffix}-0001",
            "identity": {"device_id": device_id},
            "manifest": {"schema_version": 1, "title": f"M2-B device {suffix}"},
            "display_name": f"M2-B device {suffix}",
            "device_kind": "m2b-e2e",
        },
    )
    assert enrollment.status_code == 200, enrollment.text
    approval = await hub.post(
        f"/api/device-management/v1/devices/{device_id}/approval",
        headers={"Authorization": _management_token()},
        json={
            "operation": "device.approval",
            "request_id": f"approve-{suffix}",
            "owner_id": "owner-m2b",
        },
    )
    assert approval.status_code == 200, approval.text


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
                    await _approve_device(hub, device_id="device-m2b-a", suffix="m2b-a")
                    await _approve_device(hub, device_id="device-m2b-b", suffix="m2b-b")

                    mounted = await kernel.post(
                        "/api/kernel/v1/device-mounts",
                        headers=OWNER_HEADERS,
                        json=_mount_body("device-m2b-a", "mount-m2b-a"),
                    )
                    assert mounted.status_code == 200, mounted.text

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
                            response.status_code == 200 and response.json()["status"] == "ready"
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
                    recovered = await kernel.post(
                        "/api/kernel/v1/device-mounts",
                        headers=OWNER_HEADERS,
                        json=_mount_body("device-m2b-b", "mount-m2b-b"),
                    )
                    assert recovered.status_code == 200, recovered.text

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
