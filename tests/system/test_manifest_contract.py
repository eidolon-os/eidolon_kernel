from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from eidolon_system.adapters.manifest.yaml_file import YamlServiceManifest
from eidolon_system.config import load_settings
from eidolon_system.contracts.registry import SystemContractRegistry


def manifest_document() -> dict:
    return {
        "version": 1,
        "services": [
            {
                "service_id": "kernel",
                "description": "Sovereign Kernel",
                "required": True,
                "enabled_by_default": True,
                "dependencies": [],
                "host_targets": {"systemd": "eidolon-kernel.service", "fake": "kernel"},
                "endpoints": [
                    {
                        "endpoint_id": "control.http",
                        "protocol": "http",
                        "address": "http://127.0.0.1:8083",
                        "contract": "eidolon.kernel.device-mount.v1",
                        "health_url": "http://127.0.0.1:8083/health",
                    }
                ],
            }
        ],
    }


def test_manifest_is_schema_driven_and_maps_to_domain(tmp_path) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(json.dumps(manifest_document()), encoding="utf-8")
    catalog = YamlServiceManifest(path).load()
    definition = catalog.get("kernel")
    assert definition.required is True
    assert definition.target_for("systemd") == "eidolon-kernel.service"
    assert definition.endpoints[0].contract == "eidolon.kernel.device-mount.v1"


def test_manifest_rejects_unknown_wire_fields(tmp_path) -> None:
    document = manifest_document()
    document["surprise"] = True
    path = tmp_path / "services.yaml"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValidationError):
        YamlServiceManifest(path).load()


def test_all_system_schemas_are_valid() -> None:
    registry = SystemContractRegistry()
    assert set(registry.schema_names) == {
        "system/audit-page.schema.json",
        "system/endpoint.schema.json",
        "system/manifest.schema.json",
        "system/mutation-request.schema.json",
        "system/mutation-result.schema.json",
        "system/service-page.schema.json",
        "system/service-status.schema.json",
    }


def test_repository_profiles_and_seed_manifest_are_self_consistent() -> None:
    root = Path(__file__).resolve().parents[2]
    for settings_name, driver, service_ids in (
        (
            "eidolond.yaml",
            "supervisord",
            ["data", "data-workspace", "hub", "kernel"],
        ),
        (
            "eidolond.systemd.example.yaml",
            "systemd",
            [
                "agent",
                "channel",
                "data",
                "data-workspace",
                "hub",
                "kernel",
                "livekit",
                "memory-discovery",
                "memory-supervisor",
                "nats",
            ],
        ),
    ):
        settings = load_settings(root / "config" / settings_name)
        catalog = YamlServiceManifest(settings.manifest.path).load()
        assert [item.service_id for item in catalog.definitions] == service_ids
        assert all(item.target_for(driver) for item in catalog.definitions)
