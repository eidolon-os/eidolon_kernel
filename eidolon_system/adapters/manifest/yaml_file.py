"""Strict YAML/JSON manifest loader backed by the normative JSON Schema."""

from __future__ import annotations

from pathlib import Path

import yaml

from eidolon_system.contracts.bindings import ServiceManifestWire
from eidolon_system.contracts.mappers import manifest_service_to_domain
from eidolon_system.contracts.registry import SystemContractRegistry
from eidolon_system.domain.model import ServiceCatalog


class YamlServiceManifest:
    def __init__(
        self, path: Path, *, contracts: SystemContractRegistry | None = None
    ) -> None:
        self.path = path.resolve()
        self.contracts = contracts or SystemContractRegistry()

    def load(self) -> ServiceCatalog:
        document = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.contracts.validate("system/manifest.schema.json", document)
        wire = ServiceManifestWire.model_validate(document)
        return ServiceCatalog(
            tuple(manifest_service_to_domain(service) for service in wire.services)
        )
