from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "eidolon_kernel"

LAYER_IMPORTS = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "application": {"domain", "ports", "application"},
    "contracts": {"domain", "ports", "contracts"},
    "adapters": {"domain", "ports", "contracts", "adapters"},
    "interfaces": {"domain", "ports", "application", "contracts", "interfaces"},
    "composition": {
        "domain",
        "ports",
        "application",
        "contracts",
        "adapters",
        "interfaces",
        "composition",
        "config",
    },
}


def imports(path: Path) -> tuple[str, ...]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return tuple(found)


def test_required_layers_exist_and_import_only_inward() -> None:
    violations = []
    for layer, allowed in LAYER_IMPORTS.items():
        assert (PACKAGE / layer).is_dir()
        for path in (PACKAGE / layer).rglob("*.py"):
            for imported in imports(path):
                prefix = "eidolon_kernel."
                if not imported.startswith(prefix):
                    continue
                target = imported.removeprefix(prefix).split(".", 1)[0]
                if target not in allowed:
                    violations.append(
                        f"{path.relative_to(ROOT)} ({layer}) imports outer layer {target}"
                    )
    assert violations == []


def test_domain_and_application_have_no_framework_or_sibling_dependencies() -> None:
    forbidden = {
        "fastapi",
        "httpx",
        "jsonschema",
        "pydantic",
        "sqlite3",
        "sqlalchemy",
        "grpc",
        "nats",
        "redis",
        "eidolon_hub",
        "eidolon_data",
        "eidolon_agent",
        "eidolon_channel",
    }
    violations = []
    for layer in ("domain", "application"):
        for path in (PACKAGE / layer).rglob("*.py"):
            roots = {name.split(".", 1)[0] for name in imports(path)}
            if roots & forbidden:
                violations.append(f"{path.relative_to(ROOT)}: {sorted(roots & forbidden)}")
    assert violations == []


def test_sqlite_is_confined_and_heavy_infrastructure_is_absent() -> None:
    for path in PACKAGE.rglob("*.py"):
        if "sqlite3" in {name.split(".", 1)[0] for name in imports(path)}:
            assert path.is_relative_to(PACKAGE / "adapters/persistence")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    for forbidden in ("nats", "redis", "grpc", "livekit", "mqtt", "sqlalchemy"):
        assert forbidden not in dependencies


def test_project_is_independent_and_does_not_import_sibling_source() -> None:
    assert (ROOT / ".git").is_dir()
    sibling_roots = {"hub", "eidolon_data", "eidolon_agent", "eidolon_channel"}
    violations = []
    for path in PACKAGE.rglob("*.py"):
        imported_roots = {name.split(".", 1)[0] for name in imports(path)}
        if imported_roots & sibling_roots:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(imported_roots & sibling_roots)}")
    assert violations == []


def test_contract_schemas_are_packaged_and_no_business_modules_leaked_in() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = project["tool"]["setuptools"]["package-data"]
    assert "schemas/*/*.schema.json" in package_data["eidolon_kernel.contracts"]
    forbidden_directories = {"agent", "memory", "media", "channel", "commands", "state"}
    actual = {path.name for path in PACKAGE.iterdir() if path.is_dir()}
    assert actual.isdisjoint(forbidden_directories)


def test_owner_is_the_only_kernel_security_namespace_principal() -> None:
    forbidden_symbols = {"actor_id", "ActorWire", "ActorAuthorizer"}
    violations = []
    sources = tuple(PACKAGE.rglob("*.py")) + tuple(
        (PACKAGE / "contracts/schemas").rglob("*.json")
    )
    for path in sources:
        text = path.read_text(encoding="utf-8")
        present = sorted(symbol for symbol in forbidden_symbols if symbol in text)
        if present:
            violations.append(f"{path.relative_to(ROOT)}: {present}")
    assert violations == []
    assert not (PACKAGE / "contracts/schemas/common/actor.schema.json").exists()
