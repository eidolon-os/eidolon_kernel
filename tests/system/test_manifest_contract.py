from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from eidolon_system.adapters.manifest.yaml_file import YamlServiceManifest
from eidolon_system.config import load_settings
from eidolon_system.contracts.registry import SystemContractRegistry
from eidolon_system.domain.errors import InvalidManifest


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


#: The only places a service is allowed to be absent from a driver's world, and
#: why. Anything else added here is a deliberate edit with a reason attached,
#: which is the whole point: the previous arrangement let a service go missing
#: from one Host's manifest by simply never being written into the other file.
EXTERNAL_BY_DESIGN = {
    ("nats", "supervisord"): (
        "a macOS source run shares one already-listening NATS with whatever "
        "started it, so there is no supervisord program to target"
    ),
}

HOST_DRIVERS = ("supervisord", "systemd")

PROFILES = (("eidolond.yaml", "supervisord"), ("eidolond.systemd.example.yaml", "systemd"))


def repository_catalog():
    root = Path(__file__).resolve().parents[2]
    return YamlServiceManifest(root / "config/system-services.yaml").load()


def test_both_repository_profiles_read_the_same_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = {load_settings(root / "config" / name).manifest.path for name, _ in PROFILES}

    # The macOS source run and the Pi product image disagreed about the service
    # set itself — 4 against 11 — because each profile pointed at its own copy.
    # One path is what makes that disagreement unwritable rather than merely
    # policed, so it is asserted before anything about the contents.
    assert paths == {(root / "config/system-services.yaml").resolve()}


def test_every_service_is_reachable_from_every_host_driver() -> None:
    catalog = repository_catalog()

    missing = {
        (definition.service_id, driver)
        for definition in catalog.definitions
        for driver in HOST_DRIVERS
        if driver not in definition.host_targets
    }
    assert not missing, (
        f"these services would not exist on that Host at all: {sorted(missing)}. "
        "A driver a service does not name is a service that Host never runs; "
        "say `external` if that is meant."
    )


def test_unmanaged_services_are_declared_one_by_one() -> None:
    catalog = repository_catalog()

    external = {
        (definition.service_id, driver)
        for definition in catalog.definitions
        for driver in HOST_DRIVERS
        if not definition.manages(driver)
    }
    assert external == set(EXTERNAL_BY_DESIGN)


def test_an_unmanaged_service_can_still_be_observed() -> None:
    catalog = repository_catalog()

    for service_id, _driver in EXTERNAL_BY_DESIGN:
        definition = catalog.get(service_id)
        # Dependents gate on `ready`, and a service eidolond does not start can
        # only earn that word from a health endpoint of its own.
        assert any(endpoint.health_url for endpoint in definition.endpoints), (
            f"{service_id} is not managed on every Host and publishes no health "
            "endpoint, so nothing could ever report it ready"
        )


def test_each_profile_can_act_on_everything_it_manages() -> None:
    root = Path(__file__).resolve().parents[2]
    for settings_name, driver in PROFILES:
        settings = load_settings(root / "config" / settings_name)
        catalog = YamlServiceManifest(settings.manifest.path).load()
        for definition in catalog.definitions:
            if definition.manages(driver):
                assert definition.target_for(driver)
            else:
                with pytest.raises(InvalidManifest):
                    definition.target_for(driver)


def test_service_set_is_the_full_product_topology() -> None:
    assert [item.service_id for item in repository_catalog().definitions] == [
        "agent",
        "channel",
        "channel-provider",
        "data",
        "data-workspace",
        "hub",
        "kernel",
        "livekit",
        "memory-discovery",
        "memory-supervisor",
        "nats",
    ]
