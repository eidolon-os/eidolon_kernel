"""Strict eidolond configuration with host-specific details isolated."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestSettings(SettingsModel):
    path: Path = Path("config/system-services.yaml")


class PersistenceSettings(SettingsModel):
    path: Path = Path("var/eidolond.sqlite3")


class HostSettings(SettingsModel):
    driver: Literal["auto", "systemd", "supervisord"] = "auto"
    systemctl: str = "systemctl"
    supervisorctl: str = "supervisorctl"
    supervisor_config: Path | None = None
    #: Where the root unit applier listens. Set on the product image, where
    #: eidolond runs as `eidolon` and cannot start a unit itself; left unset for
    #: a root source run, which mutates units directly. Not a systemd-only
    #: notion by accident — the supervisord driver never reads it.
    unit_applier_socket: Path | None = None
    command_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    @model_validator(mode="after")
    def _supervisord_has_config(self):
        if self.driver == "supervisord" and self.supervisor_config is None:
            raise ValueError("host.supervisor_config is required for supervisord")
        return self


class ReconciliationSettings(SettingsModel):
    interval_seconds: float = Field(default=5.0, ge=1.0, le=3600)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)


class InterfaceSettings(SettingsModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)
    uds: Path | None = None
    uds_mode: Literal["0600", "0660"] = "0600"
    uds_group: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_-]{0,63}$")

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("eidolond V1 interface must bind loopback or a Unix socket")
        return value

    @property
    def uds_mode_bits(self) -> int:
        return int(self.uds_mode, 8)


class SystemSettings(SettingsModel):
    manifest: ManifestSettings = ManifestSettings()
    persistence: PersistenceSettings = PersistenceSettings()
    host: HostSettings = HostSettings()
    reconciliation: ReconciliationSettings = ReconciliationSettings()
    interface: InterfaceSettings = InterfaceSettings()


def selected_host_driver(settings: HostSettings, *, system_name: str | None = None) -> str:
    if settings.driver != "auto":
        return settings.driver
    current = system_name or platform.system()
    if current == "Linux":
        return "systemd"
    if current == "Darwin" and settings.supervisor_config is not None:
        return "supervisord"
    raise RuntimeError(f"no eidolond host adapter configured for {current}")


def load_settings(path: Path | None = None) -> SystemSettings:
    project_root = Path(__file__).resolve().parents[1]
    configured = os.environ.get("EIDOLON_SYSTEM_SETTINGS_YAML")
    settings_path = path or (
        Path(configured) if configured else project_root / "config/eidolond.yaml"
    )
    document = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings = SystemSettings.model_validate(document)
    base = settings_path.resolve().parent
    for field in (settings.manifest, settings.persistence):
        if not field.path.is_absolute():
            field.path = (base / field.path).resolve()
    if settings.host.supervisor_config is not None and not settings.host.supervisor_config.is_absolute():
        settings.host.supervisor_config = (base / settings.host.supervisor_config).resolve()
    if "/" in settings.host.supervisorctl and not Path(settings.host.supervisorctl).is_absolute():
        settings.host.supervisorctl = str((base / settings.host.supervisorctl).resolve())
    if "/" in settings.host.systemctl and not Path(settings.host.systemctl).is_absolute():
        settings.host.systemctl = str((base / settings.host.systemctl).resolve())
    if (
        settings.host.unit_applier_socket is not None
        and not settings.host.unit_applier_socket.is_absolute()
    ):
        settings.host.unit_applier_socket = (
            base / settings.host.unit_applier_socket
        ).resolve()
    if settings.interface.uds is not None and not settings.interface.uds.is_absolute():
        settings.interface.uds = (base / settings.interface.uds).resolve()
    return settings
