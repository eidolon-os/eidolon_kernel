from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(
    os.environ.get("EIDOLON_TEST_DATA_SOURCE_ROOT", ROOT.parent / "eidolon_data")
).resolve()
CONSUMED_SCHEMA = ROOT / "eidolon_kernel/contracts/schemas/external/companion-identity.schema.json"
PRODUCER_SCHEMA = DATA_ROOT / "eidolon_data/contracts/schemas/companion/identity.schema.json"

pytestmark = pytest.mark.integration


def _contract_shape(document: dict) -> dict:
    return {
        key: value for key, value in document.items() if key not in {"$id", "title", "description"}
    }


def test_consumed_companion_identity_shape_matches_data_v2_producer() -> None:
    if not PRODUCER_SCHEMA.is_file():
        pytest.skip("eidolon_data sibling checkout is unavailable")
    producer = json.loads(PRODUCER_SCHEMA.read_text(encoding="utf-8"))
    consumer = json.loads(CONSUMED_SCHEMA.read_text(encoding="utf-8"))
    assert _contract_shape(consumer) == _contract_shape(producer)


def test_data_v2_baseline_contains_only_the_canonical_authority_tables() -> None:
    migration = DATA_ROOT / "eidolon_data/db/migrations/versions/0001_system_data_v2.py"
    if not migration.is_file():
        pytest.skip("eidolon_data V2 migration baseline is unavailable")
    source = migration.read_text(encoding="utf-8")
    canonical = {
        "owners",
        "companions",
        "persona_genomes",
        "memory_realms",
        "companion_face_assets",
        "guard_bindings",
        "owner_face_profile_revisions",
        "owner_face_references",
        "audit_outbox",
    }
    retired = {
        "devices",
        "body_commands",
        "runtime_sessions",
        "conversations",
        "turns",
        "messages",
        "jobs",
        "events",
    }
    assert {name for name in canonical if f'"{name}"' in source} == canonical
    assert not {name for name in retired if f'"{name}"' in source}
