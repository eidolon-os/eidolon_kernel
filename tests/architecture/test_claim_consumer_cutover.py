from __future__ import annotations

import ast
import json
from pathlib import Path

from eidolon_sdk.device_foundation.v1 import (
    ClaimActivatedEvent,
    ClaimEventCursor,
    ClaimEventPage,
    ClaimRevokedEvent,
    DeviceRef,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "eidolon_kernel"


def test_claim_consumer_baseline_is_frozen_to_published_sdk_and_hub() -> None:
    baseline = json.loads(
        (PACKAGE / "contracts/claim_consumer_baseline.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert baseline == {
        "capability": "ph2-b-kernel-claim-consumer",
        "sdk_commit": "d88196757e8c054befd2c17f7cb9c7a9eb6f5253",
        "hub_commit": "5d6403649e466f557c26a0ba3a94e5869d86df13",
        "stream_id": "admission-claims-v1",
        "endpoint": "/api/admission/v1/claim-events",
        "event_source": "urn:eidolon:authority:admission",
        "audience": "eidolon-claim-consumers",
        "activated_type": "live.eidolon.device.claim-activated.v1",
        "revoked_type": "live.eidolon.device.claim-revoked.v1",
    }
    for binding in (
        ClaimActivatedEvent,
        ClaimRevokedEvent,
        ClaimEventPage,
        ClaimEventCursor,
        DeviceRef,
    ):
        assert binding.__module__.startswith("eidolon_sdk.device_foundation.v1.")


def test_claim_capability_has_no_handwritten_or_legacy_wire_dto() -> None:
    bindings = ast.parse(
        (PACKAGE / "contracts/bindings.py").read_text(encoding="utf-8")
    )
    classes = {
        node.name for node in ast.walk(bindings) if isinstance(node, ast.ClassDef)
    }
    assert classes.isdisjoint(
        {"DeviceRefWire", "HubClaimEventWire", "HubClaimEventPageWire"}
    )
    assert not (
        PACKAGE
        / "contracts/schemas/external/hub-claim-event-page.schema.json"
    ).exists()


def test_claim_consumer_uses_only_canonical_route_and_five_field_device_ref() -> None:
    capability_files = (
        PACKAGE / "adapters/device_registry/hub_http.py",
        PACKAGE / "adapters/persistence/sqlite.py",
        PACKAGE / "application/device_mounts.py",
        PACKAGE / "ports/authorities.py",
        PACKAGE / "ports/runtime.py",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in capability_files)
    assert "/api/admission/v1/claim-events" in text
    assert "/api/device-management/v1/claim-events" not in text
    assert "accepted_manifest_digest" not in text
    assert "ClaimEventPage.model_validate" in text
    assert "ClaimActivatedEvent" in text
    assert "ClaimRevokedEvent" in text


def test_the_claim_consumer_still_knows_nothing_about_body_assignments() -> None:
    """The Claim stream moves devices in and out; it never moves an Eidolon.

    This assertion used to cover the whole package, freezing Body Mesh out of
    the PH2-B cutover. Body Mesh has since landed on purpose, so the wide
    version would now be asserting that a shipped capability does not exist.
    Narrowed to the claim-consumer path, where it still has teeth: a Claim
    arriving must not release or create an assignment as a side effect. Those
    are converged by their own scan against the Companion authority, and a
    revoked Claim that quietly cleared assignments would take away the record of
    who a device answered as — the thing that lets it come back to the same
    Eidolon.
    """

    consumer_files = (
        PACKAGE / "adapters/device_registry/hub_http.py",
        PACKAGE / "application/device_mounts.py",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in consumer_files)
    assert "BodyEndpoint" not in text
    assert "BodyAssignment" not in text
    assert "assignment" not in text.lower()
