"""Independent eidolond ASGI factory and executable."""

from __future__ import annotations

from fastapi import FastAPI

from eidolon_system.composition.app import create_production_app
from eidolon_system.config import load_settings


def create_app() -> FastAPI:
    return create_production_app().app


def run() -> None:
    import uvicorn

    settings = load_settings()
    arguments: dict[str, object] = {"factory": True}
    if settings.interface.uds is not None:
        settings.interface.uds.parent.mkdir(parents=True, exist_ok=True)
        arguments["uds"] = str(settings.interface.uds)
    else:
        arguments["host"] = settings.interface.host
        arguments["port"] = settings.interface.port
    uvicorn.run("eidolon_system.main:create_app", **arguments)
