from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PydanticValidationError

from eidolon_kernel.contracts.bindings import (
    CompanionIdentityWire,
    HubDeviceDirectoryEntryWire,
    MountDeviceRequestWire,
    ReplaceAssignmentRequestWire,
    SystemServiceEndpointWire,
)
from eidolon_kernel.contracts.mappers import commit_to_wire
from eidolon_kernel.contracts.registry import ContractRegistry
from eidolon_kernel.domain.model import DeviceMount, DeviceRef
from eidolon_kernel.ports.runtime import CommitResult

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_1 = named_device_instance_id("device-1")

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "eidolon_kernel/contracts/schemas"


def test_every_json_schema_is_valid_and_registered() -> None:
    registry = ContractRegistry()
    files = tuple(SCHEMAS.rglob("*.schema.json"))
    # Eleven, not fourteen. The Body Mesh read documents are no longer described
    # twice: their definition is the canonical type the consumers also validate
    # with, and the router checks every response against that instead. What is
    # left here is the shapes this Host alone owns — including the replace
    # request, which is not the canonical command but this authority's transport
    # for it, carrying the ``origin`` the provenance is derived from.
    assert len(files) == len(registry.schema_names) == 11
    for path in files:
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_runtime_request_and_result_bindings_conform_to_schema_sources() -> None:
    registry = ContractRegistry()
    request = MountDeviceRequestWire(
        operation="device.mount",
        request_id="request-1",
        device_id=_DEVICE_1,
        expected_revision=0,
        replace_existing=False,
    )
    registry.validate("device-mount/mount-request.schema.json", request.model_dump(mode="json"))

    now = datetime(2026, 8, 4, tzinfo=UTC)
    mount = DeviceMount.first(
        device_id=_DEVICE_1,
        owner_id="owner-1",
        device_ref=DeviceRef(
            device_instance_id=_DEVICE_1,
            owner_domain_id="owner-1",
            owner_domain_generation=1,
            claim_generation=1,
            trust_epoch=1,
        ),
        at=now,
        request_id="request-1",
        fingerprint="sha256:" + "a" * 64,
    )
    result = commit_to_wire(CommitResult(mount, 1, False))
    registry.validate("device-mount/mutation-result.schema.json", result.model_dump(mode="json"))

    assignment = ReplaceAssignmentRequestWire(
        operation="body.replace-assignment",
        request_id="assign-1",
        expected_assignment_revision=0,
        companion_id="companion-1",
        origin="owner",
    )
    registry.validate(
        "body-mesh/replace-assignment-request.schema.json",
        assignment.model_dump(mode="json"),
    )


def test_schema_and_binding_both_reject_unknown_wire_fields() -> None:
    document = {
        "operation": "device.mount",
        "request_id": "r",
        "device_id": "d",
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
        "device_id": _DEVICE_1,
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
        "device_ref": {
            "device_instance_id": _DEVICE_1,
            "owner_domain_id": "owner-1",
            "owner_domain_generation": 1,
            "claim_generation": 1,
            "trust_epoch": 1,
        },
    }
    ContractRegistry().validate("external/hub-device-directory-entry.schema.json", document)
    wire = HubDeviceDirectoryEntryWire.model_validate(document)
    assert wire.owner_scope == "owner-1" and wire.lifecycle_state == "approved"

    document["provider_binding"] = "must-not-leak"
    with pytest.raises(ValidationError):
        ContractRegistry().validate("external/hub-device-directory-entry.schema.json", document)

    malformed_manifest = {
        key: value for key, value in document.items() if key != "provider_binding"
    }
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
        "kind": "conversational",
        "revision": 1,
    }
    ContractRegistry().validate("external/companion-identity.schema.json", document)
    assert CompanionIdentityWire.model_validate(document).lifecycle_state == "active"

    document["profile_json"] = {"must": "not leak"}
    with pytest.raises(ValidationError):
        ContractRegistry().validate("external/companion-identity.schema.json", document)


def test_consumed_system_directory_contract_is_strict_and_machine_scoped() -> None:
    document = {
        "operation": "system.service-endpoint",
        "service_id": "hub",
        "endpoint_id": "device-authority.http",
        "protocol": "http",
        "address": "http://127.0.0.1:8082",
        "contract": "eidolon.hub.device-directory.v1",
    }
    ContractRegistry().validate("external/system-service-endpoint.schema.json", document)
    assert SystemServiceEndpointWire.model_validate(document).service_id == "hub"
    document["owner_id"] = "must-not-enter-machine-scope"
    with pytest.raises(ValidationError):
        ContractRegistry().validate("external/system-service-endpoint.schema.json", document)


def test_system_directory_producer_and_consumer_contracts_have_identical_shape() -> None:
    root = Path(__file__).resolve().parents[2]
    producer = json.loads(
        (root / "eidolon_system/contracts/schemas/system/endpoint.schema.json").read_text(
            encoding="utf-8"
        )
    )
    consumer = json.loads(
        (
            root / "eidolon_kernel/contracts/schemas/external/system-service-endpoint.schema.json"
        ).read_text(encoding="utf-8")
    )
    for keyword in ("type", "additionalProperties", "required", "properties"):
        assert consumer[keyword] == producer[keyword]
