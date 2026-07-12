"""Read provider API keys from the service EnvironmentFile.

The blackbox.service unit loads exactly this file (systemctl cat
blackbox.service -> EnvironmentFile=<repo>/.env), so probing with these keys
exercises the same credentials the live service uses. Values are NEVER logged
or written to results — only redacted output leaves this package.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def load_service_env(path: Path = ENV_FILE) -> Dict[str, str]:
    """Parse KEY=VALUE lines; skip comments/blank/non-kv lines; strip quotes."""
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_key(name: str) -> str:
    """Process env wins (service-injected); fall back to parsing .env."""
    return os.environ.get(name) or load_service_env().get(name, "")
