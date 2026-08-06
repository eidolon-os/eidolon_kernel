from __future__ import annotations

from pathlib import Path


def release_document(release_id: str = "20260806-m2d-test") -> dict:
    release_root = f"/srv/eidolon/releases/{release_id}"
    return {
        "schema_version": 1,
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
                "current_link": "/srv/eidolon/current/eidolon_kernel",
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
                "current_link": "/srv/eidolon/current/eidolon_data",
                "source_tree_sha256": "4" * 64,
                "lock_sha256": "5" * 64,
                "environment_sha256": "6" * 64,
                "required_entrypoints": [".venv/bin/uvicorn"],
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
                "source_component_id": "eidolon_kernel",
                "source": source,
                "destination": destination,
                "sha256": character * 64,
                "mode": "0644",
            }
            for source, destination, character in (
                ("deploy/systemd/eidolond.service", "/etc/systemd/system/eidolond.service", "3"),
                ("deploy/systemd/eidolon-data.service", "/etc/systemd/system/eidolon-data.service", "4"),
                ("deploy/systemd/eidolon-kernel.service", "/etc/systemd/system/eidolon-kernel.service", "5"),
                ("config/eidolond.systemd.example.yaml", "/etc/eidolon/eidolond.yaml", "6"),
                ("config/kernel.systemd.example.yaml", "/etc/eidolon/kernel.yaml", "7"),
                ("config/system-services.systemd.example.yaml", "/etc/eidolon/system-services.systemd.example.yaml", "8"),
                ("deploy/polkit/60-eidolon-system-manager.rules", "/etc/polkit-1/rules.d/60-eidolon-system-manager.rules", "9"),
            )
        ],
        "required_secrets": [
            {"path": "/etc/eidolon/data.env", "mode": "0600"},
            {"path": "/etc/eidolon/kernel.env", "mode": "0600"},
        ],
        "affected_units": ["eidolon-data.service", "eidolon-kernel.service"],
        "readiness_checks": [
            {
                "check_id": "eidolond",
                "kind": "unix_http",
                "url": "http://eidolond/health",
                "socket": "/run/eidolon/system.sock",
            },
            {
                "check_id": "kernel",
                "kind": "http",
                "url": "http://127.0.0.1:8083/health",
            },
            {
                "check_id": "data",
                "kind": "http",
                "url": "http://127.0.0.1:8084/health",
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
