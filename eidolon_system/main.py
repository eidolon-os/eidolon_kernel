"""Independent eidolond ASGI factory and executable."""

from __future__ import annotations

import socket
import stat
from pathlib import Path

from fastapi import FastAPI

from eidolon_system.composition.app import create_production_app
from eidolon_system.config import load_settings


def create_app() -> FastAPI:
    return create_production_app().app


def _bind_unix_socket(path: Path, mode: int) -> socket.socket:
    """Bind a local listener without uvicorn's unconditional 0666 chmod."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing_mode):
            raise RuntimeError(f"refusing to replace non-socket UDS path: {path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        except OSError as exc:
            raise RuntimeError(f"cannot verify existing UDS path: {path}") from exc
        else:
            raise RuntimeError(f"eidolond UDS is already active: {path}")
        finally:
            probe.close()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(mode)
        listener.set_inheritable(True)
    except Exception:
        listener.close()
        raise
    return listener


def _remove_unix_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(mode):
        path.unlink()


def run() -> None:
    import uvicorn

    settings = load_settings()
    arguments: dict[str, object] = {"factory": True}
    listener: socket.socket | None = None
    socket_path = settings.interface.uds
    if socket_path is not None:
        listener = _bind_unix_socket(
            socket_path,
            settings.interface.uds_mode_bits,
        )
        arguments["fd"] = listener.fileno()
    else:
        arguments["host"] = settings.interface.host
        arguments["port"] = settings.interface.port
    try:
        uvicorn.run("eidolon_system.main:create_app", **arguments)
    finally:
        if listener is not None and socket_path is not None:
            listener.close()
            _remove_unix_socket(socket_path)
