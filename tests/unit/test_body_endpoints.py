"""What a Body is on this Host, and why it is derived rather than declared."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_kernel.domain.body import (
    DERIVED_ENDPOINT_ID,
    body_endpoint_id,
    derived_endpoint,
    device_of,
)
from eidolon_kernel.domain.errors import InvalidRequest
from tests.support import sample_mount

_DEVICE_1 = named_device_instance_id("device-1")

_CANONICAL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def test_a_body_endpoint_id_is_a_canonical_identifier_and_reads_back() -> None:
    """It is composed, so it must still be legal to send over the canonical wire.

    Composing it rather than allocating one is what lets the endpoint set be
    derived instead of stored, and what lets an assignment survive a device being
    re-claimed. That only works if the composed value is a canonical
    ``Identifier``: a device instance id is eighty characters, and the bound is
    a hundred and twenty-eight.
    """

    value = body_endpoint_id(_DEVICE_1, DERIVED_ENDPOINT_ID)
    assert 3 <= len(value) <= 128
    assert _CANONICAL_IDENTIFIER.fullmatch(value)
    assert device_of(value) == _DEVICE_1

    with pytest.raises(InvalidRequest):
        body_endpoint_id(_DEVICE_1, "e" * 64)
    with pytest.raises(InvalidRequest):
        device_of("no-separator")


def test_the_one_derived_body_says_it_was_derived_and_constrains_nothing() -> None:
    """``optional`` here is a fact about this Host, not a permissive default.

    No firmware in this product declares endpoints — the manifest vocabulary a
    real BOX-3 asserts has no ``endpoints`` array at all — so nothing has told
    this Host that a Companion is required or forbidden on this Body. Saying
    ``optional`` is reporting that. ``source`` is on the record so a screen never
    presents this Host's stand-in as the device's own word, and so the day a
    Manifest does declare endpoints the switch is visible rather than silent.
    """

    endpoint = derived_endpoint(sample_mount())

    assert endpoint.source == "derived"
    assert endpoint.endpoint_id == DERIVED_ENDPOINT_ID
    assert endpoint.roles == ("body",)
    assert endpoint.assignment_policy == "optional"
    assert endpoint.present is True
    assert derived_endpoint(sample_mount(2, active=False)).present is False


def test_no_manifest_this_product_ships_declares_an_endpoint() -> None:
    """The premise the derivation rests on, checked against the firmware itself.

    If a board ever starts declaring endpoints, this fails and the derivation
    has to be replaced by ``ReconcileEndpoints`` rather than quietly continuing
    to invent one Body per device.
    """

    firmware = (
        Path(__file__).resolve().parents[3]
        / "eidolon-client-esp32/main/eidolon/hub_onboarding_protocol.cc"
    )
    if not firmware.is_file():
        pytest.skip("the device firmware repository is not checked out beside this one")
    builder = firmware.read_text(encoding="utf-8")
    start = builder.index("std::string BuildDeviceManifestJson")
    body = builder[start : builder.index("\n}", start)]
    assert '"endpoints"' not in body
    for declared in ("actions", "events", "media", "properties", "schema_version", "title"):
        assert f'\\"{declared}\\"' in body


def test_the_canonical_manifest_schema_still_has_the_field_the_host_fills_in() -> None:
    """The other half: this is a gap in what devices say, not in the contract.

    Device Foundation defines ``ReconcileEndpoints`` with an endpoint list, so
    the derivation is standing in for a producer that does not exist yet — not
    inventing a concept. If the canonical shape changes, the stand-in needs
    revisiting too.
    """

    schema = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "eidolon_sdk/contracts/device_foundation/v1/body-mesh/schemas.schema.json"
        ).read_text(encoding="utf-8")
    )
    endpoint = schema["$defs"]["ReconcileEndpoints"]["properties"]["endpoints"]["items"]
    assert set(endpoint["required"]) == {
        "endpoint_id",
        "roles",
        "assignment_policy",
        "risk_class",
        "concurrency",
    }
