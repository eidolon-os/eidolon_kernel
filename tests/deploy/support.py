from __future__ import annotations

from pathlib import Path


def release_document(release_id: str = "20260806-m2d-test") -> dict:
    release_root = f"/opt/eidolon/releases/{release_id}"
    return {
        "schema_version": 2,
        "release_id": release_id,
        "target": {
            "system": "linux",
            "machine": "aarch64",
            "python": "3.13",
        },
        "components": [
            {
                "component_id": "eidolon_kernel",
                "revision": "a" * 40,
                "release_path": f"{release_root}/eidolon_kernel",
                "current_link": "/opt/eidolon/current/eidolon_kernel",
                "source_tree_sha256": "1" * 64,
                "lock_sha256": "2" * 64,
                "environment_sha256": "3" * 64,
                "required_entrypoints": [
                    ".venv/bin/eidolond",
                    ".venv/bin/uvicorn",
                ],
            },
            {
                "component_id": "eidolon_data",
                "revision": "b" * 40,
                "release_path": f"{release_root}/eidolon_data",
                "current_link": "/opt/eidolon/current/eidolon_data",
                "source_tree_sha256": "4" * 64,
                "lock_sha256": "5" * 64,
                "environment_sha256": "6" * 64,
                "required_entrypoints": [".venv/bin/uvicorn"],
            },
            {
                "component_id": "eidolon_hub",
                "revision": "d" * 40,
                "release_path": f"{release_root}/eidolon_hub",
                "current_link": "/opt/eidolon/current/eidolon_hub",
                "source_tree_sha256": "7" * 64,
                "lock_sha256": "8" * 64,
                "environment_sha256": "9" * 64,
                "required_entrypoints": [".venv/bin/uvicorn"],
            },
            {
                "component_id": "eidolon_admin",
                "revision": "e" * 40,
                "release_path": f"{release_root}/eidolon_admin",
                "current_link": "/opt/eidolon/current/eidolon_admin",
                "source_tree_sha256": "a" * 64,
                "lock_sha256": "b" * 64,
                "environment_sha256": "c" * 64,
                "required_entrypoints": [
                    ".venv/bin/eidolon-admin",
                    ".venv/bin/eidolon-bootstrapd",
                    ".venv/bin/eidolon-local-api",
                ],
            },
            {
                "component_id": "eidolon_agent",
                "revision": "f" * 40,
                "release_path": f"{release_root}/eidolon_agent",
                "current_link": "/opt/eidolon/current/eidolon_agent",
                "source_tree_sha256": "d" * 64,
                "lock_sha256": "e" * 64,
                "environment_sha256": "f" * 64,
                "required_entrypoints": [".venv/bin/eidolon-agent"],
            },
            {
                "component_id": "eidolon_channel",
                "revision": "1" * 40,
                "release_path": f"{release_root}/eidolon_channel",
                "current_link": "/opt/eidolon/current/eidolon_channel",
                "source_tree_sha256": "1" * 64,
                "lock_sha256": "2" * 64,
                "environment_sha256": "3" * 64,
                "required_entrypoints": [
                    ".venv/bin/python",
                    ".venv/bin/eidolon-channel-provider",
                ],
            },
            {
                "component_id": "eidolon_memory",
                "revision": "2" * 40,
                "release_path": f"{release_root}/eidolon_memory",
                "current_link": "/opt/eidolon/current/eidolon_memory",
                "source_tree_sha256": "4" * 64,
                "lock_sha256": "5" * 64,
                "environment_sha256": "6" * 64,
                "required_entrypoints": [
                    ".venv/bin/eidolon-memory-supervisor",
                    ".venv/bin/eidolon-memory-discovery",
                ],
            },
        ],
        "support_sources": [
            {
                "source_id": "eidolon_sdk",
                "revision": "c" * 40,
                "release_path": f"{release_root}/eidolon_sdk",
                "source_tree_sha256": "9" * 64,
            }
        ],
        "system_assets": [
            {
                "source_component_id": component,
                "source": source,
                "destination": destination,
                "sha256": character * 64,
                "mode": "0644",
            }
            for component, source, destination, character in (
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolond.service",
                    "/etc/systemd/system/eidolond.service",
                    "1",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-data.service",
                    "/etc/systemd/system/eidolon-data.service",
                    "2",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-data-workspace.service",
                    "/etc/systemd/system/eidolon-data-workspace.service",
                    "f",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-hub.service",
                    "/etc/systemd/system/eidolon-hub.service",
                    "3",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-kernel.service",
                    "/etc/systemd/system/eidolon-kernel.service",
                    "4",
                ),
                (
                    "eidolon_kernel",
                    "config/eidolond.systemd.example.yaml",
                    "/etc/eidolon/eidolond.yaml",
                    "5",
                ),
                (
                    "eidolon_kernel",
                    "config/kernel.systemd.example.yaml",
                    "/etc/eidolon/kernel.yaml",
                    "6",
                ),
                (
                    "eidolon_kernel",
                    "config/system-services.systemd.example.yaml",
                    "/etc/eidolon/system-services.systemd.example.yaml",
                    "8",
                ),
                (
                    "eidolon_kernel",
                    "deploy/polkit/60-eidolon-system-manager.rules",
                    "/etc/polkit-1/rules.d/60-eidolon-system-manager.rules",
                    "9",
                ),
                (
                    "eidolon_admin",
                    "deploy/systemd/eidolon-bootstrapd.service",
                    "/etc/systemd/system/eidolon-bootstrapd.service",
                    "a",
                ),
                (
                    "eidolon_admin",
                    "deploy/systemd/eidolon-local-api.service",
                    "/etc/systemd/system/eidolon-local-api.service",
                    "b",
                ),
                (
                    "eidolon_admin",
                    "deploy/systemd/eidolon-admin.service",
                    "/etc/systemd/system/eidolon-admin.service",
                    "c",
                ),
                (
                    "eidolon_admin",
                    "deploy/polkit/60-eidolon-bootstrap-network.rules",
                    "/etc/polkit-1/rules.d/60-eidolon-bootstrap-network.rules",
                    "d",
                ),
                (
                    "eidolon_admin",
                    "deploy/avahi/eidolon-local-api.service",
                    "/etc/avahi/services/eidolon-local-api.service",
                    "e",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-nats.service",
                    "/etc/systemd/system/eidolon-nats.service",
                    "1",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-livekit.service",
                    "/etc/systemd/system/eidolon-livekit.service",
                    "2",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-memory-supervisor.service",
                    "/etc/systemd/system/eidolon-memory-supervisor.service",
                    "3",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-memory-discovery.service",
                    "/etc/systemd/system/eidolon-memory-discovery.service",
                    "4",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-agent.service",
                    "/etc/systemd/system/eidolon-agent.service",
                    "5",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-channel.service",
                    "/etc/systemd/system/eidolon-channel.service",
                    "6",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-channel-provider.service",
                    "/etc/systemd/system/eidolon-channel-provider.service",
                    "8",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-livekit-launch",
                    "/usr/local/libexec/eidolon-livekit-launch",
                    "7",
                ),
            )
        ],
        "required_secrets": [
            {"path": "/etc/eidolon/data.env", "mode": "0600"},
            {"path": "/etc/eidolon/hub.env", "mode": "0600"},
            {"path": "/etc/eidolon/kernel.env", "mode": "0600"},
            {"path": "/etc/eidolon/admin.env", "mode": "0600"},
            {"path": "/etc/eidolon/local-api.env", "mode": "0600"},
            {"path": "/etc/eidolon/bootstrap.env", "mode": "0600"},
            {
                "path": "/var/lib/eidolon-bootstrap/host_identity.ed25519",
                "mode": "0600",
            },
            {"path": "/etc/eidolon/agent.env", "mode": "0600"},
            {"path": "/etc/eidolon/channel.env", "mode": "0600"},
            {"path": "/etc/eidolon/memory.env", "mode": "0600"},
            {"path": "/etc/eidolon/livekit.env", "mode": "0600"},
        ],
        "affected_units": [
            "eidolon-admin.service",
            "eidolon-local-api.service",
            "eidolon-bootstrapd.service",
            "eidolon-data.service",
            "eidolon-data-workspace.service",
            "eidolon-hub.service",
            "eidolon-kernel.service",
            "eidolon-nats.service",
            "eidolon-livekit.service",
            "eidolon-memory-supervisor.service",
            "eidolon-memory-discovery.service",
            "eidolon-agent.service",
            "eidolon-channel-provider.service",
            "eidolon-channel.service",
        ],
        "readiness_checks": [
            {
                "check_id": "eidolond",
                "kind": "unix_http",
                "url": "http://eidolond/health",
                "socket": "/run/eidolon/system.sock",
                "expected_status": "ready",
            },
            {
                "check_id": "data",
                "kind": "http",
                "url": "http://127.0.0.1:8084/health",
                "expected_status": "ready",
            },
            {
                "check_id": "data-workspace",
                "kind": "http",
                "url": "http://127.0.0.1:8085/health",
                "expected_status": "ready",
            },
            {
                "check_id": "hub",
                "kind": "http",
                "url": "http://127.0.0.1:8082/health",
                "expected_status": "ok",
            },
            {
                "check_id": "kernel",
                "kind": "http",
                "url": "http://127.0.0.1:8083/health",
                "expected_status": "ready",
            },
            {
                "check_id": "admin",
                "kind": "http",
                "url": "http://127.0.0.1:9000/healthz",
                "expected_status": "ready",
            },
            {
                "check_id": "local-api",
                "kind": "https",
                "url": "https://127.0.0.1:9002/healthz",
                "expected_status": "ok",
            },
            {
                "check_id": "nats",
                "kind": "http",
                "url": "http://127.0.0.1:8222/healthz",
                "expected_status": "ok",
            },
            {
                "check_id": "livekit",
                "kind": "tcp",
                "url": "tcp://127.0.0.1:7880",
                "expected_status": "open",
            },
            {
                "check_id": "memory",
                "kind": "http",
                "url": "http://127.0.0.1:8020/api/discovery/agent-routing",
                "expected_status": "http_2xx",
            },
            {
                "check_id": "agent",
                "kind": "http",
                "url": "http://127.0.0.1:8180/readyz",
                "expected_status": "ready",
            },
            {
                "check_id": "channel",
                "kind": "systemd",
                "url": "systemd://eidolon-channel.service",
                "expected_status": "active",
            },
            {
                "check_id": "channel-provider",
                "kind": "http",
                "url": "http://127.0.0.1:8767/health",
                "expected_status": "ok",
            },
        ],
        "database_migrations": [],
    }


def write_release_document(path: Path, document: dict) -> None:
    import hashlib
    import json

    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {path.name}\n",
        encoding="utf-8",
    )
