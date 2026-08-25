from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_deploy.manifest import ReleaseDescriptorError, load_release_descriptor
from tests.deploy.support import release_document, write_release_document


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
    assert len(release.system_assets) == 23
    assert len(release.required_secrets) == 11
    assert len(release.affected_units) == 15
    assert len(release.readiness_checks) == 14
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
