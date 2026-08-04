from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PydanticValidationError

from eidolon_kernel.contracts.bindings import (
    CompanionIdentityWire,
    HubDeviceDirectoryEntryWire,
    MountDeviceRequestWire,
)
from eidolon_kernel.contracts.mappers import commit_to_wire
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.model import DeviceMount
from eidolon_kernel.ports.runtime import CommitResult

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "eidolon_kernel/contracts/schemas"


def test_every_json_schema_is_valid_and_registered() -> None:
    registry = ContractRegistry()
    files = tuple(SCHEMAS.rglob("*.schema.json"))
    assert len(files) == len(registry.schema_names) == 9
    for path in files:
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_runtime_request_and_result_bindings_conform_to_schema_sources() -> None:
    registry = ContractRegistry()
    request = MountDeviceRequestWire(
        operation="device.mount",
        request_id="request-1",
        device_id="device-1",
        companion_id="companion-1",
        expected_revision=0,
        replace_existing=False,
    )
    registry.validate(
        "device-mount/mount-request.schema.json", request.model_dump(mode="json")
    )

    now = datetime(2026, 8, 4, tzinfo=UTC)
    mount = DeviceMount.first(
        device_id="device-1",
        owner_id="owner-1",
        companion_id="companion-1",
        at=now,
        request_id="request-1",
        fingerprint="sha256:" + "a" * 64,
    )
    result = commit_to_wire(CommitResult(mount, 1, False))
    registry.validate(
        "device-mount/mutation-result.schema.json", result.model_dump(mode="json")
    )


def test_schema_and_binding_both_reject_unknown_wire_fields() -> None:
    document = {
        "operation": "device.mount",
        "request_id": "r",
        "device_id": "d",
        "companion_id": "c",
        "expected_revision": 0,
        "replace_existing": False,
        "surprise": True,
    }
    with pytest.raises(ValidationError):
        ContractRegistry().validate("device-mount/mount-request.schema.json", document)
    with pytest.raises(PydanticValidationError):
        MountDeviceRequestWire.model_validate(document)


def test_mount_request_cannot_select_a_target_owner() -> None:
    document = {
        "operation": "device.mount",
        "request_id": "r",
        "device_id": "d",
        "companion_id": "c",
        "expected_revision": 0,
        "replace_existing": False,
        "owner_id": "another-owner",
    }
    with pytest.raises(ValidationError):
        ContractRegistry().validate("device-mount/mount-request.schema.json", document)
    with pytest.raises(PydanticValidationError):
        MountDeviceRequestWire.model_validate(document)


def test_consumed_hub_contract_accepts_only_documented_device_entry_shape() -> None:
    document = {
        "operation": "device.directory-entry",
        "device_id": "device-1",
        "owner_scope": "owner-1",
        "display_name": "Desk",
        "device_kind": "desktop",
        "manifest": {
            "schema_version": 1,
            "title": "Desk",
            "properties": [],
            "actions": [],
            "events": [],
            "media": [],
        },
        "manifest_revision": "sha256:manifest",
        "lifecycle_state": "approved",
        "enrolled_at": "2026-08-04T08:00:00Z",
        "updated_at": "2026-08-04T08:00:00Z",
    }
    ContractRegistry().validate(
        "external/hub-device-directory-entry.schema.json", document
    )
    wire = HubDeviceDirectoryEntryWire.model_validate(document)
    assert wire.owner_scope == "owner-1" and wire.lifecycle_state == "approved"

    document["provider_binding"] = "must-not-leak"
    with pytest.raises(ValidationError):
        ContractRegistry().validate(
            "external/hub-device-directory-entry.schema.json", document
        )

    malformed_manifest = {key: value for key, value in document.items() if key != "provider_binding"}
    malformed_manifest["manifest"] = {
        **malformed_manifest["manifest"],
        "actions": [{"name": "missing-required-action-fields"}],
    }
    with pytest.raises(ValidationError):
        ContractRegistry().validate(
            "external/hub-device-directory-entry.schema.json", malformed_manifest
        )


def test_consumed_companion_contract_is_a_strict_identity_subset() -> None:
    document = {
        "operation": "companion.identity",
        "companion_id": "companion-1",
        "owner_id": "owner-1",
        "lifecycle_state": "active",
    }
    ContractRegistry().validate("external/companion-identity.schema.json", document)
    assert CompanionIdentityWire.model_validate(document).lifecycle_state == "active"

    document["profile_json"] = {"must": "not leak"}
    with pytest.raises(ValidationError):
        ContractRegistry().validate("external/companion-identity.schema.json", document)
