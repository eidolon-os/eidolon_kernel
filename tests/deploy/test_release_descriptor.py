from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_deploy.manifest import ReleaseDescriptorError, load_release_descriptor
from tests.deploy.support import release_document, with_capability, write_release_document


def test_loads_strict_sealed_release_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    write_release_document(path, release_document())

    release = load_release_descriptor(path)

    assert release.release_id == "20260806-m2d-test"
    assert [component.component_id for component in release.components] == [
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
    ]
    assert release.schema_version == 2
    assert release.components[0].release_path == Path(
        "/opt/eidolon/releases/20260806-m2d-test/eidolon_kernel"
    )
    assert release.support_sources[0].source_id == "eidolon_sdk"
    assert release.readiness_checks[0].socket == Path("/run/eidolon/system.sock")
    assert len(release.system_assets) == 25
    assert len(release.required_secrets) == 11
    assert len(release.affected_units) == 18
    assert len(release.readiness_checks) == 15
    assert Path("/etc/eidolon/lifecycle.env") not in {
        secret.path for secret in release.required_secrets
    }
    assert "eidolon-lifecycle-workflow.service" in release.affected_units
    assert {
        check.check_id: check.url for check in release.readiness_checks
    }["lifecycle-workflow"] == "systemd://eidolon-lifecycle-workflow.service"
    admin = next(
        component
        for component in release.components
        if component.component_id == "eidolon_admin"
    )
    assert Path(".venv/bin/eidolon-lifecycle-workflow") in admin.required_entrypoints
    assert release.database_migrations == ()


def test_rejects_descriptor_or_sidecar_tampering(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    write_release_document(path, release_document())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["release_id"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ReleaseDescriptorError, match="checksum"):
        load_release_descriptor(path)


def test_rejects_missing_invalid_sidecar_and_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text("{}\n")
    with pytest.raises(ReleaseDescriptorError, match="sidecar is missing"):
        load_release_descriptor(path)

    path.with_suffix(".json.sha256").write_text("invalid\n")
    with pytest.raises(ReleaseDescriptorError, match="sidecar is invalid"):
        load_release_descriptor(path)

    import hashlib

    payload = b"not-json\n"
    path.write_bytes(payload)
    path.with_suffix(".json.sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  release.json\n"
    )
    with pytest.raises(ReleaseDescriptorError, match="valid JSON"):
        load_release_descriptor(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update({"surprise": True}), "contract"),
        (
            lambda value: value["components"].append(dict(value["components"][0])),
            "component",
        ),
        (
            lambda value: value["components"][0].update(
                {"release_path": "/opt/eidolon/releases/other/eidolon_kernel"}
            ),
            "release path",
        ),
        (
            lambda value: value["components"][0].update({"current_link": "/tmp/eidolon_kernel"}),
            "current link",
        ),
        (
            lambda value: value["support_sources"][0].update(
                {"release_path": "/opt/eidolon/releases/other/eidolon_sdk"}
            ),
            "support source",
        ),
        (
            lambda value: value["system_assets"][0].update({"destination": "/etc/passwd"}),
            "system asset set",
        ),
        (
            lambda value: value.update({"database_migrations": ["alembic upgrade head"]}),
            "migration",
        ),
    ],
)
def test_rejects_unsafe_or_ambiguous_release_facts(tmp_path, mutate, message) -> None:
    path = tmp_path / "release.json"
    document = release_document()
    mutate(document)
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError, match=message):
        load_release_descriptor(path)


def test_rejects_relative_entrypoint_and_duplicate_readiness_id(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    document = release_document()
    document["components"][0]["required_entrypoints"] = ["../escape"]
    document["readiness_checks"].append(dict(document["readiness_checks"][0]))
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError):
        load_release_descriptor(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update(
                {"components": [dict(value["components"][0]) for _ in range(7)]}
            ),
            "component set",
        ),
        (
            lambda value: value["system_assets"].__setitem__(-1, dict(value["system_assets"][0])),
            "destination must be unique",
        ),
        (
            lambda value: value["system_assets"][0].update({"source": "/tmp/unit"}),
            "source must stay",
        ),
        (
            lambda value: value["required_secrets"].__setitem__(
                -1, dict(value["required_secrets"][0])
            ),
            "secret set",
        ),
        (
            lambda value: value["readiness_checks"][1].update(
                {"url": "http://192.168.1.5:8083/health"}
            ),
            "loopback",
        ),
        (
            lambda value: value["readiness_checks"][0].update(
                {"socket": "/run/eidolon/other.sock"}
            ),
            "system socket",
        ),
        # Preflight now decides whether a bootstrap schema advance may land by
        # reading this one word, so an unrecognised spelling must never load as
        # a plain string that merely happens not to equal "forward-only".
        (
            lambda value: value.update({"cutover_mode": "forward_only"}),
            "is not one of",
        ),
    ],
)
def test_rejects_semantically_unsafe_contract_values(tmp_path: Path, mutate, message: str) -> None:
    path = tmp_path / "release.json"
    document = release_document()
    mutate(document)
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError, match=message):
        load_release_descriptor(path)


def test_rejects_v1_descriptor_instead_of_claiming_full_stack_coverage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release.json"
    document = release_document()
    document["schema_version"] = 1
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError, match="contract"):
        load_release_descriptor(path)


def test_a_host_that_declares_nothing_gets_exactly_the_baseline(tmp_path: Path) -> None:
    """No capability, no additions — and no `capabilities` key needed at all.

    A rollback reads the descriptor of the release it returns to, and those were
    sealed before the field existed. Requiring it would make every older release
    unrestorable, so absence means a Host that declares nothing.
    """

    path = tmp_path / "release.json"
    document = release_document()
    assert "capabilities" not in document
    write_release_document(path, document)

    release = load_release_descriptor(path)

    assert release.capabilities == frozenset()
    assert "eidolon_models" not in {item.component_id for item in release.components}
    assert "eidolon-asr.service" not in release.affected_units


def test_a_declared_capability_adds_its_component_unit_asset_and_check(
    tmp_path: Path,
) -> None:
    """The conditional component could not ship at all before this.

    Every set was compared against a literal, so `eidolon_models` — a component
    that is on a board with an NPU and on no other Host — was refused by the
    contract no matter how it was declared. The sets are computed from the
    Host's capabilities now, and still compared exactly.
    """

    path = tmp_path / "release.json"
    write_release_document(path, with_capability(release_document()))

    release = load_release_descriptor(path)

    assert release.capabilities == frozenset({"local_asr"})
    assert "eidolon_models" in {item.component_id for item in release.components}
    # Order is load-bearing: quiesce sweeps the unit list in reverse, so an
    # addition goes after the baseline rather than anywhere in it.
    assert release.affected_units[-1] == "eidolon-asr.service"
    assert Path("/etc/systemd/system/eidolon-asr.service") in {
        asset.destination for asset in release.system_assets
    }
    assert "asr" in {check.check_id for check in release.readiness_checks}


def test_a_capability_nobody_declared_cannot_bring_a_component(tmp_path: Path) -> None:
    """Shipping it needs the capability said out loud, not just the parts."""

    path = tmp_path / "release.json"
    document = with_capability(release_document())
    document["capabilities"] = []
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError, match="component set"):
        load_release_descriptor(path)


def test_a_declared_capability_without_its_parts_is_refused(tmp_path: Path) -> None:
    """And the other way round: claiming it and shipping the baseline is not a
    release for that Host, it is a Host that will install nothing it asked for."""

    path = tmp_path / "release.json"
    document = release_document()
    document["capabilities"] = ["local_asr"]
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError, match="component set"):
        load_release_descriptor(path)


def test_a_misspelt_capability_is_refused_rather_than_selecting_nothing(
    tmp_path: Path,
) -> None:
    """An open set fails silently: a typo selects no additions, so the expected
    sets come out as the baseline and the release is accepted as a Host that
    runs nothing extra — which is exactly what such a Host would install."""

    path = tmp_path / "release.json"
    document = release_document()
    document["capabilities"] = ["local_asrr"]
    write_release_document(path, document)

    with pytest.raises(ReleaseDescriptorError, match="contract violation|capability"):
        load_release_descriptor(path)


def test_every_count_ceiling_is_the_one_the_tables_can_produce() -> None:
    """Nobody held these to the tables, so they were only found on a board.

    A capability added two assets and the release refused to seal with "is too
    long" — after building everything. Then the next ceiling refused the next
    thing. Every one of them is derivable here: what a Host declaring every
    capability would carry.

    Only the ceilings are derived. The floors are deliberately left where they
    are: this schema also validates descriptors sealed by *earlier* releases,
    which a rollback reads back, and raising a floor would refuse one of those
    rather than anything a current release can produce.
    """

    import json
    from pathlib import Path

    from eidolon_deploy.capabilities import HOST_CAPABILITIES
    from eidolon_deploy.manifest import (
        expected_affected_units,
        expected_components,
        expected_readiness,
        expected_system_assets,
    )

    every = frozenset(HOST_CAPABILITIES)
    ceilings = {
        "components": len(expected_components(every)),
        "system_assets": len(expected_system_assets(every)),
        "affected_units": len(expected_affected_units(every)),
        "readiness_checks": len(expected_readiness(every)),
        "capabilities": len(every),
    }

    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "eidolon_deploy/contracts/schemas/release-descriptor.schema.json"
        ).read_text(encoding="utf-8")
    )

    for name, ceiling in ceilings.items():
        bounds = schema["properties"][name]
        assert bounds["maxItems"] == ceiling, name
        floor = bounds.get("minItems")
        if floor is not None:
            assert floor <= ceiling, name


def test_every_name_the_schema_admits_is_a_name_the_tables_produce() -> None:
    """The counts were not the only copy of the tables in here.

    After the ceilings came the enums: the same unit the tables had just been
    taught was refused by a hand-written list of allowed names. Held in both
    directions, so a removed unit does not linger as an admitted name either.
    """

    import json
    from pathlib import Path

    from eidolon_deploy.capabilities import HOST_CAPABILITIES
    from eidolon_deploy.manifest import expected_affected_units, expected_components

    every = frozenset(HOST_CAPABILITIES)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "eidolon_deploy/contracts/schemas/release-descriptor.schema.json"
        ).read_text(encoding="utf-8")
    )

    # Order is load-bearing for the units — quiesce sweeps the tuple in reverse
    # — so this is a list comparison, not a set one.
    assert schema["properties"]["affected_units"]["items"]["enum"] == list(
        expected_affected_units(every)
    )
    assert set(schema["properties"]["capabilities"]["items"]["enum"]) == set(every)
    assert set(schema["$defs"]["componentId"]["enum"]) >= expected_components(every)
