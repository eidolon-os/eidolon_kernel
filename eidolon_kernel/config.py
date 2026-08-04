"""Strict local-only Kernel configuration."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersistenceSettings(SettingsModel):
    path: Path = Path("var/eidolon-kernel.sqlite3")


class HubSettings(SettingsModel):
    base_url: str = "http://127.0.0.1:8082"
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("hub.base_url must be HTTP(S)")
        return value.rstrip("/")


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
    hub: HubSettings = HubSettings()
    deployment: DeploymentSettings = DeploymentSettings()


def load_settings(path: Path | None = None) -> KernelSettings:
    project_root = Path(__file__).resolve().parents[1]
    configured = os.environ.get("EIDOLON_KERNEL_SETTINGS_YAML")
    settings_path = path or (Path(configured) if configured else project_root / "config/settings.yaml")
    document = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings = KernelSettings.model_validate(document)
    if not settings.persistence.path.is_absolute():
        settings.persistence.path = (project_root / settings.persistence.path).resolve()
    return settings


def load_hub_token() -> str:
    token = (os.environ.get("EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN is required")
    return token
