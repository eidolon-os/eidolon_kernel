from __future__ import annotations

from pathlib import Path


def with_capability(document: dict, capability: str = "local_asr") -> dict:
    """The same document as sealed for a Host that declares one capability.

    Built by adding, not by writing a second fixture: the point of the change
    being tested is that a capability *adds* to one reviewed baseline, so a
    test that hand-wrote the whole expanded set could pass while the addition
    rule was wrong.
    """

    from eidolon_deploy.manifest import (
        expected_affected_units,
        expected_readiness,
        expected_system_assets,
    )

    declared = frozenset({capability})
    release_root = f"/opt/eidolon/releases/{document['release_id']}"
    result = dict(document)
    result["capabilities"] = [capability]
    result["components"] = [
        *document["components"],
        {
            "component_id": "eidolon_models",
            "revision": "e" * 40,
            "release_path": f"{release_root}/eidolon_models",
            "current_link": "/opt/eidolon/current/eidolon_models",
            "source_tree_sha256": "7" * 64,
            "lock_sha256": "8" * 64,
            "environment_sha256": "9" * 64,
            "required_entrypoints": ["scripts/eidolon-asr"],
        },
    ]
    known = {item["destination"] for item in document["system_assets"]}
    result["system_assets"] = [
        *document["system_assets"],
        *(
            {
                "source_component_id": component_id,
                "source": str(source),
                "destination": str(destination),
                "sha256": "a" * 64,
                "mode": "0644",
            }
            for destination, (component_id, source) in expected_system_assets(declared).items()
            if str(destination) not in known
        ),
    ]
    result["affected_units"] = list(expected_affected_units(declared))
    seen = {item["check_id"] for item in document["readiness_checks"]}
    result["readiness_checks"] = [
        *document["readiness_checks"],
        *(
            {
                "check_id": check_id,
                "kind": values[0],
                "url": values[1],
                **({"socket": str(values[2])} if values[2] is not None else {}),
                "expected_status": values[3],
            }
            for check_id, values in expected_readiness(declared).items()
            if check_id not in seen
        ),
    ]
    return result


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
                    ".venv/bin/eidolon-unit-applier",
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
                    ".venv/bin/eidolon-lifecycle-workflow",
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
                    ".venv/bin/eidolon-memory-embedder",
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
                    "config/system-services.yaml",
                    "/etc/eidolon/system-services.yaml",
                    "8",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-unit-applier.socket",
                    "/etc/systemd/system/eidolon-unit-applier.socket",
                    "9",
                ),
                (
                    "eidolon_kernel",
                    "deploy/systemd/eidolon-unit-applier.service",
                    "/etc/systemd/system/eidolon-unit-applier.service",
                    "f",
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
                    "deploy/systemd/eidolon-lifecycle-workflow.service",
                    "/etc/systemd/system/eidolon-lifecycle-workflow.service",
                    "0",
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
                    "deploy/systemd/eidolon-memory-embedder.service",
                    "/etc/systemd/system/eidolon-memory-embedder.service",
                    "9",
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
            "eidolon-lifecycle-workflow.service",
            "eidolon-bootstrapd.service",
            "eidolon-unit-applier.socket",
            "eidolon-unit-applier.service",
            "eidolon-data.service",
            "eidolon-data-workspace.service",
            "eidolon-hub.service",
            "eidolon-kernel.service",
            "eidolon-nats.service",
            "eidolon-livekit.service",
            "eidolon-memory-embedder.service",
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
                "check_id": "lifecycle-workflow",
                "kind": "systemd",
                "url": "systemd://eidolon-lifecycle-workflow.service",
                "expected_status": "active",
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
                "check_id": "memory-embedder",
                "kind": "http",
                "url": "http://127.0.0.1:8760/v1/health",
                "expected_status": "ok",
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
        "cutover_mode": "reversible",
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
