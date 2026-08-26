from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.config import HubSettings

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_E2E = named_device_instance_id("device-e2e")
_DEVICE_OWNER_MISMATCH = named_device_instance_id("device-owner-mismatch")
_DEVICE_MISSING_COMPANION = named_device_instance_id("device-missing-companion")
_DEVICE_DATA_OUTAGE = named_device_instance_id("device-data-outage")

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(
    os.environ.get("EIDOLON_TEST_DATA_SOURCE_ROOT", ROOT.parent / "eidolon_data")
).resolve()
DATA_PYTHON = DATA_ROOT / ".venv/bin/python"
HUB_TOKEN = "kernel-e2e-hub-device-authority-token-0001"
DATA_TOKEN = "kernel-e2e-data-authority-token-0001"
ROSTER_TOKEN = "kernel-e2e-data-memory-runtime-roster-token-0001"
OWNER_HEADERS = {"X-Eidolon-Owner": "owner-e2e"}

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
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


async def _wait_ready(
    process: subprocess.Popen[str],
    *,
    base_url: str,
    uds_path: Path | None = None,
) -> None:
    transport = httpx.AsyncHTTPTransport(uds=str(uds_path)) if uds_path else None
    async with httpx.AsyncClient(
        transport=transport,
        base_url=base_url,
        timeout=0.5,
        trust_env=False,
    ) as client:
        for _ in range(100):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"process exited before readiness\nstdout={stdout}\nstderr={stderr}")
            try:
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    pytest.fail("process did not become ready within five seconds")


def _prepare_data_v2(database: Path) -> None:
    migration_environment = {
        **os.environ,
        "EIDOLON_DATA_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
    }
    subprocess.run(
        [str(DATA_PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=DATA_ROOT,
        env=migration_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    seed_script = """
import asyncio
import os
from eidolon_data import DataSettings, DataStore

async def main():
    store = DataStore.open(DataSettings(sqlite_path=os.environ["EIDOLON_DATA_SQLITE_PATH"]))
    try:
        await store.validate_schema()
        await store.owner_commands.create_owner(owner_id="owner-e2e")
        await store.companion_workspaces.provision_workspace(
            owner_id="owner-e2e",
            companion_id="companion-e2e",
            genome_id="genome-e2e",
            realm_id="realm-e2e",
            kind="conversational",
        )
        await store.owner_commands.create_owner(owner_id="owner-other")
        await store.companion_workspaces.provision_workspace(
            owner_id="owner-other",
            companion_id="companion-other",
            genome_id="genome-other",
            realm_id="realm-other",
            kind="conversational",
        )
    finally:
        await store.close()

asyncio.run(main())
"""
    subprocess.run(
        [str(DATA_PYTHON), "-c", seed_script],
        cwd=DATA_ROOT,
        env={**os.environ, "EIDOLON_DATA_SQLITE_PATH": str(database)},
        check=True,
        capture_output=True,
        text=True,
    )


def _assert_data_v2_schema(database: Path) -> None:
    canonical = {
        "alembic_version",
        "owners",
        "companions",
        "persona_genomes",
        "memory_realms",
        "companion_face_assets",
        "guard_bindings",
        "owner_face_profile_revisions",
        "owner_face_references",
        "audit_outbox",
    }
    with closing(sqlite3.connect(database)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == canonical
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0001_system_data_v2"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _mount_body(device_id: str, request_id: str) -> dict[str, object]:
    return {
        "operation": "device.mount",
        "request_id": request_id,
        "device_id": device_id,
        "expected_revision": 0,
        "replace_existing": False,
    }


def _attach_body(companion_id: str, request_id: str) -> dict[str, object]:
    return {
        "operation": "companion.attach",
        "request_id": request_id,
        "companion_id": companion_id,
        "expected_revision": 1,
    }


async def test_real_kernel_process_consumes_real_data_v2_authority(tmp_path) -> None:
    if not DATA_PYTHON.is_file():
        pytest.skip("eidolon_data development runtime is unavailable")

    database = tmp_path / "eidolon-system.sqlite3"
    kernel_database = tmp_path / "eidolon-kernel.sqlite3"
    kernel_socket = Path("/private/tmp") / f"eidolon-kernel-{uuid4().hex[:12]}.sock"
    dependency_port = _free_port()
    data_port = _free_port()
    dependency_url = f"http://127.0.0.1:{dependency_port}"
    data_url = f"http://127.0.0.1:{data_port}"
    settings = tmp_path / "kernel-settings.yaml"
    if "base_url" in HubSettings.model_fields:
        hub_bootstrap = f"""hub:
  base_url: {dependency_url}
  timeout_seconds: 2
"""
    else:
        hub_bootstrap = f"""system_directory:
  base_url: {dependency_url}
  uds_path: null
  timeout_seconds: 2
hub:
  timeout_seconds: 2
"""
    settings.write_text(
        f"""persistence:
  path: {kernel_database}
{hub_bootstrap}companion_authority:
  timeout_seconds: 2
reconciliation:
  interval_seconds: 3600
deployment:
  mode: trusted-local
  trusted_local_ingress: true
""",
        encoding="utf-8",
    )
    _prepare_data_v2(database)
    _assert_data_v2_schema(database)

    dependency_process: subprocess.Popen[str] | None = None
    data_process: subprocess.Popen[str] | None = None
    kernel_process: subprocess.Popen[str] | None = None
    try:
        dependency_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.process_support:create_dependency_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(dependency_port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "EIDOLON_TEST_HUB_TOKEN": HUB_TOKEN,
                "EIDOLON_TEST_DEPENDENCY_BASE_URL": dependency_url,
                "EIDOLON_TEST_DATA_BASE_URL": data_url,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        data_process = subprocess.Popen(
            [
                str(DATA_PYTHON),
                "-m",
                "uvicorn",
                "eidolon_data.api.companion_authority:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(data_port),
                "--log-level",
                "warning",
            ],
            cwd=DATA_ROOT,
            env={
                **os.environ,
                "EIDOLON_DATA_SQLITE_PATH": str(database),
                "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN": DATA_TOKEN,
                "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN": ROSTER_TOKEN,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        await _wait_ready(dependency_process, base_url=dependency_url)
        await _wait_ready(data_process, base_url=data_url)

        kernel_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "eidolon_kernel.main:create_app",
                "--factory",
                "--uds",
                str(kernel_socket),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "EIDOLON_KERNEL_SETTINGS_YAML": str(settings),
                "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN": HUB_TOKEN,
                "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN": DATA_TOKEN,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        await _wait_ready(
            kernel_process,
            base_url="http://kernel.local",
            uds_path=kernel_socket,
        )

        async with (
            httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(kernel_socket)),
                base_url="http://kernel.local",
            ) as kernel,
            httpx.AsyncClient(base_url=data_url, trust_env=False) as data,
        ):
            assert (
                await data.get(
                    "/companions/companion-e2e",
                    headers={"Authorization": f"Bearer {DATA_TOKEN}"},
                )
            ).status_code == 404

            mounted = await kernel.post(
                "/api/kernel/v1/device-mounts",
                headers=OWNER_HEADERS,
                json=_mount_body(_DEVICE_E2E, "mount-e2e"),
            )
            assert mounted.status_code == 200, mounted.text

            attach_path = f"/api/kernel/v1/device-mounts/devices/{_DEVICE_E2E}/attachment"
            responses = await asyncio.gather(
                *(
                    kernel.post(
                        attach_path,
                        headers=OWNER_HEADERS,
                        json=_attach_body("companion-e2e", "attach-e2e"),
                    )
                    for _ in range(12)
                )
            )
            assert {response.status_code for response in responses} == {200}
            assert {response.json()["audit_position"] for response in responses} == {2}
            assert sum(not response.json()["replayed"] for response in responses) == 1

            for device_id, request_id in (
                (_DEVICE_OWNER_MISMATCH, "mount-owner-mismatch"),
                (_DEVICE_MISSING_COMPANION, "mount-missing-companion"),
                (_DEVICE_DATA_OUTAGE, "mount-data-outage"),
            ):
                response = await kernel.post(
                    "/api/kernel/v1/device-mounts",
                    headers=OWNER_HEADERS,
                    json=_mount_body(device_id, request_id),
                )
                assert response.status_code == 200, response.text

            owner_mismatch = await kernel.post(
                f"/api/kernel/v1/device-mounts/devices/{_DEVICE_OWNER_MISMATCH}/attachment",
                headers=OWNER_HEADERS,
                json=_attach_body("companion-other", "attach-owner-mismatch"),
            )
            missing = await kernel.post(
                f"/api/kernel/v1/device-mounts/devices/{_DEVICE_MISSING_COMPANION}/attachment",
                headers=OWNER_HEADERS,
                json=_attach_body("companion-missing", "attach-missing"),
            )
            assert owner_mismatch.status_code == missing.status_code == 409

            _stop(data_process)
            data_process = None
            unavailable = await kernel.post(
                f"/api/kernel/v1/device-mounts/devices/{_DEVICE_DATA_OUTAGE}/attachment",
                headers=OWNER_HEADERS,
                json=_attach_body("companion-e2e", "attach-data-outage"),
            )
            assert unavailable.status_code == 503

            audit = await kernel.get(
                "/api/kernel/v1/audit/events",
                headers=OWNER_HEADERS,
            )
            assert audit.status_code == 200
            assert len(audit.json()["events"]) == 5
    finally:
        _stop(kernel_process)
        _stop(data_process)
        _stop(dependency_process)
        kernel_socket.unlink(missing_ok=True)
