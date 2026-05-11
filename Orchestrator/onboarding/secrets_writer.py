"""Atomic .env file writer with backup."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from Orchestrator.utils.paths import resolve

ENV_FILE = resolve(".env")


def update_env(updates: dict[str, str]) -> dict:
    """Atomically update key=value pairs in .env, preserving structure.

    - Existing keys: replaced in-place (preserves comment ordering)
    - New keys: appended in a labeled section
    - Backup created at .env.backup.<timestamp> before writing
    - Atomic rename via os.replace
    """
    if not ENV_FILE.exists():
        ENV_FILE.touch()

    ts = int(time.time())
    backup = ENV_FILE.with_suffix(f".backup.{ts}")
    shutil.copy2(ENV_FILE, backup)

    lines = ENV_FILE.read_text().splitlines(keepends=True)
    seen_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                seen_keys.add(k)
                continue
        new_lines.append(line)

    new_keys = [k for k in updates if k not in seen_keys]
    if new_keys:
        new_lines.append("\n# Added by onboarding wizard\n")
        for k in new_keys:
            new_lines.append(f"{k}={updates[k]}\n")

    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("".join(new_lines))
    os.replace(tmp, ENV_FILE)

    return {"backup": str(backup), "updated_keys": list(updates.keys())}
