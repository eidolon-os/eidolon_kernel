"""Runtime validator proving that JSON Schema controls accepted wire shapes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


class ContractRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).with_name("schemas")
        self._schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        for path in self.root.rglob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            relative = str(path.relative_to(self.root))
            self._schemas[relative] = schema
            registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        self._registry = registry

    def validate(self, relative: str, document: dict[str, Any]) -> None:
        Draft202012Validator(
            self._schemas[relative],
            registry=self._registry,
            format_checker=FormatChecker(),
        ).validate(document)

    @property
    def schema_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._schemas))
