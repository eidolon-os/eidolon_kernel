"""ASGI factory entrypoint."""

from fastapi import FastAPI

from eidolon_kernel.composition.app import create_production_app


def create_app() -> FastAPI:
    return create_production_app().app
