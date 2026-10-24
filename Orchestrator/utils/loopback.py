"""This Orchestrator's own loopback URL, from the standard library only.

Every in-process caller that talks to the local app (tool executors, SMS
replies, /chat/save mints, the cron executor, voice bridges) builds its URL
here instead of hardcoding http://localhost:9091, so a box served on another
ORCHESTRATOR_PORT still reaches itself.

It lives outside Orchestrator/config.py on purpose: the BlackBox MCP server
runs Orchestrator/tools/blackbox_tools.py in its own lean venv (MCP/venv, no
fastapi), and config.py imports fastapi at module level. Resolution order
matches config.ORCHESTRATOR_PORT: ORCHESTRATOR_PORT env -> config.ini
[server] port -> 9091. When config.py is loaded it passes its own port in, so
the two can never disagree.
"""
from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path

DEFAULT_PORT = 9091


def _config_ini_candidates():
    """Where config.ini may sit: the repo root this module lives in, then the
    current directory (config.py reads config.ini from the cwd)."""
    here = Path(__file__).resolve().parents[2]
    yield here / "config.ini"
    yield Path.cwd() / "config.ini"


def orchestrator_port() -> int:
    # Once the app has loaded config, its ORCHESTRATOR_PORT is the one truth
    # (tests patch it; a box's config.ini may sit elsewhere than the cwd). Looked
    # up in sys.modules, never imported: importing config pulls in fastapi.
    cfg = sys.modules.get("Orchestrator.config")
    port = getattr(cfg, "ORCHESTRATOR_PORT", None) if cfg is not None else None
    if isinstance(port, int) and port > 0:
        return port
    env = (os.getenv("ORCHESTRATOR_PORT") or "").strip()
    if env.isdigit():
        return int(env)
    for ini in _config_ini_candidates():
        if not ini.is_file():
            continue
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read(ini, encoding="utf-8")
            return cfg.getint("server", "port", fallback=DEFAULT_PORT)
        except (configparser.Error, ValueError, OSError):
            continue
    return DEFAULT_PORT


def orchestrator_url(path: str = "", *, scheme: str = "http", host: str = "localhost",
                     port: int | None = None) -> str:
    """``scheme://host:port`` + ``path`` for this box's own API. Read at call time."""
    return f"{scheme}://{host}:{port if port is not None else orchestrator_port()}{path}"
