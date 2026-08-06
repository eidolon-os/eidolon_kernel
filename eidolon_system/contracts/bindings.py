"""Strict runtime bindings; JSON Schema remains the wire contract source."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestEndpointWire(ContractModel):
    endpoint_id: str
    protocol: str
    address: str
    contract: str
    health_url: str | None = None


class ManifestServiceWire(ContractModel):
    service_id: str
    description: str = ""
    required: bool = False
    enabled_by_default: bool = True
    dependencies: tuple[str, ...] = ()
    host_targets: dict[str, str]
    endpoints: tuple[ManifestEndpointWire, ...] = ()

    @field_validator("dependencies", "endpoints", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ServiceManifestWire(ContractModel):
    version: Literal[1]
    services: tuple[ManifestServiceWire, ...]

    @field_validator("services", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DesiredStateWire(ContractModel):
    service_id: str
    enabled: bool
    revision: int = Field(ge=1)
    updated_at: datetime


class EndpointWire(ContractModel):
    operation: Literal["system.service-endpoint"] = "system.service-endpoint"
    service_id: str
    endpoint_id: str
    protocol: str
    address: str
    contract: str


class ServiceStatusWire(ContractModel):
    operation: Literal["system.service-status"] = "system.service-status"
    service_id: str
    required: bool
    desired: DesiredStateWire
    runtime_state: Literal["unknown", "inactive", "starting", "ready", "degraded", "blocked", "failed"]
    detail: str | None = None
    observed_at: datetime
    endpoints: tuple[EndpointWire, ...] = ()

    @field_validator("endpoints", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class ServicePageWire(ContractModel):
    operation: Literal["system.service-page"] = "system.service-page"
    services: tuple[ServiceStatusWire, ...] = ()

    @field_validator("services", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class MutationRequestWire(ContractModel):
    operation: Literal[
        "system.service.enable", "system.service.disable", "system.service.restart"
    ]
    request_id: str = Field(min_length=1, max_length=96)
    expected_revision: int = Field(ge=1, strict=True)


class MutationResultWire(ContractModel):
    operation: Literal["system.service-mutation-result"] = "system.service-mutation-result"
    state: DesiredStateWire
    audit_position: int = Field(ge=1)
    replayed: bool


class AuditEventWire(ContractModel):
    position: int = Field(ge=1)
    service_id: str
    operation: str
    desired_revision: int = Field(ge=1)
    enabled: bool
    request_id: str
    fingerprint: str
    occurred_at: datetime


class AuditPageWire(ContractModel):
    operation: Literal["system.audit-page"] = "system.audit-page"
    next_position: int = Field(ge=0)
    events: tuple[AuditEventWire, ...] = ()

    @field_validator("events", mode="before")
    @classmethod
    def _arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value
