from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KERNEL_PACKAGE = ROOT / "eidolon_kernel"


def _production_imports() -> set[str]:
    imports: set[str] = set()
    for path in KERNEL_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    return imports


def test_kernel_has_no_data_package_or_orm_dependency() -> None:
    imports = _production_imports()
    forbidden = ("eidolon_data", "sqlalchemy", "alembic", "aiosqlite")
    assert not {
        imported
        for imported in imports
        if any(imported == root or imported.startswith(f"{root}.") for root in forbidden)
    }


def test_kernel_contains_no_retired_data_v1_storage_or_api_surface() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in KERNEL_PACKAGE.rglob("*.py"))
    forbidden = {
        "eidolon.sqlite3",
        "eidolon-system.sqlite3",
        "DataStore",
        "DeviceRow",
        "DevicesRepository",
        "eidolon_data.schema",
        "/api/v1/owners",
        "/api/v1/companions",
    }
    assert not {value for value in forbidden if value in source}


def test_kernel_sqlite_does_not_duplicate_system_data_v2_authorities() -> None:
    persistence = (KERNEL_PACKAGE / "adapters/persistence/sqlite.py").read_text(encoding="utf-8")
    data_tables = {
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
    assert not {table for table in data_tables if f"CREATE TABLE {table}" in persistence}
