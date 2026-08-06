"""Strict local-only Kernel configuration."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersistenceSettings(SettingsModel):
    path: Path = Path("var/eidolon-kernel.sqlite3")


class SystemDirectorySettings(SettingsModel):
    base_url: str = "http://eidolond"
    uds_path: Path | None = Path("var/eidolond.sock")
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("system_directory.base_url must be HTTP(S)")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _local_transport_only(self):
        if self.uds_path is None:
            hostname = urlparse(self.base_url).hostname
            if hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError(
                    "system_directory without UDS must use a loopback HTTP address"
                )
        return self


class HubSettings(SettingsModel):
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)


class CompanionAuthoritySettings(SettingsModel):
    base_url: str = "http://127.0.0.1:8084"
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("companion_authority.base_url must be HTTP(S)")
        return value.rstrip("/")


class ReconciliationSettings(SettingsModel):
    interval_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)


class DeploymentSettings(SettingsModel):
    mode: str = "trusted-local"
    trusted_local_ingress: bool = True

    @field_validator("mode")
    @classmethod
    def _only_v1_mode(cls, value: str) -> str:
        if value != "trusted-local":
            raise ValueError("only trusted-local deployment mode exists in V1")
        return value


class KernelSettings(SettingsModel):
    persistence: PersistenceSettings = PersistenceSettings()
    system_directory: SystemDirectorySettings = SystemDirectorySettings()
    hub: HubSettings = HubSettings()
    companion_authority: CompanionAuthoritySettings = CompanionAuthoritySettings()
    reconciliation: ReconciliationSettings = ReconciliationSettings()
    deployment: DeploymentSettings = DeploymentSettings()


def load_settings(path: Path | None = None) -> KernelSettings:
    project_root = Path(__file__).resolve().parents[1]
    configured = os.environ.get("EIDOLON_KERNEL_SETTINGS_YAML")
    settings_path = path or (Path(configured) if configured else project_root / "config/settings.yaml")
    document = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings = KernelSettings.model_validate(document)
    if not settings.persistence.path.is_absolute():
        settings.persistence.path = (project_root / settings.persistence.path).resolve()
    if (
        settings.system_directory.uds_path is not None
        and not settings.system_directory.uds_path.is_absolute()
    ):
        settings.system_directory.uds_path = (
            project_root / settings.system_directory.uds_path
        ).resolve()
    return settings


def load_hub_token() -> str:
    token = (os.environ.get("EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN") or "").strip()
    if len(token.encode()) < 32:
        raise RuntimeError(
            "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN must contain at least 32 bytes"
        )
    return token


def load_companion_authority_token() -> str:
    token = (
        os.environ.get("EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN") or ""
    ).strip()
    if len(token) < 24:
        raise RuntimeError(
            "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN must contain at least 24 characters"
        )
    return token
