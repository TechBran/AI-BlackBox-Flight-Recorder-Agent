"""Where the wellness stores live (stdlib only).

  data/wellness/              0700, created by ensure_private_dir()
    enrollments.json          one record per operator (enroll.py), 0600
    revoked.json              tombstones of revoked enrollment ids, 0600
    <op_key>.db (+ -wal/-shm) one SQLite store per operator (store.py), 0600
  credentials/wellness_device_keys.json   the device keys (enroll.py), 0600
  credentials/wellness_sim_pairing.json   simulator pairing secrets, read only
                                          while [wellness] simulator is on

data/ is gitignored and untracked (it survives the updater's reset), never
served under /ui, never copied to Archive/ and never embedded. The module
attributes are read at call time, so the test suite points them at a sandbox
(Orchestrator/tests/box_stores.py).
"""
from __future__ import annotations

import re
from pathlib import Path

from Orchestrator.utils.paths import resolve

DATA_DIR = resolve("data", "wellness")
KEYS_PATH = resolve("credentials", "wellness_device_keys.json")
SIM_PAIRING_PATH = resolve("credentials", "wellness_sim_pairing.json")

_OP_KEY_RE = re.compile(r"[0-9a-f]{16}")


def data_dir() -> Path:
    return Path(DATA_DIR)


def enrollments_path() -> Path:
    return data_dir() / "enrollments.json"


def revoked_path() -> Path:
    return data_dir() / "revoked.json"


def keys_path() -> Path:
    return Path(KEYS_PATH)


def sim_pairing_path() -> Path:
    return Path(SIM_PAIRING_PATH)


def valid_op_key(op_key) -> bool:
    return isinstance(op_key, str) and bool(_OP_KEY_RE.fullmatch(op_key))


def db_path(op_key: str) -> Path:
    """The operator's store. The file name is a random key, never the name."""
    if not valid_op_key(op_key):
        raise ValueError("op_key must be 16 lowercase hex characters")
    return data_dir() / f"{op_key}.db"


def ensure_private_dir() -> Path:
    """data/wellness/ at 0700 (re-applied when it already exists)."""
    from Orchestrator.migrations.tasks_db_location import _ensure_private_dir
    d = data_dir()
    _ensure_private_dir(d)
    return d
