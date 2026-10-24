"""The one config.ini writer (audit O05-ledger-persistence-21,
O06-frontier-agent-11, X03-security-backend-14).

The runtime edits to config.ini (adding and removing an operator, seeding the
Flight Recorder operator, the local-model capability flags) used to write the
process-global CFG back over the file with open('w'): a crash mid-write
truncated the box's one config file, two concurrent edits lost one of them,
and whatever had changed on disk since boot (a hand edit, another writer's
key) was silently reverted to the boot-time copy. update_config_ini() instead
re-reads the file FRESH under a lock, applies one edit to that copy, and
replaces the file atomically (Orchestrator/utils/atomic_json.py). A file that
does not parse is refused, never overwritten.

configparser still drops comments when it writes the file back, as the old
writers did.

Stdlib only.
"""
from __future__ import annotations

import configparser
import io
import os
from pathlib import Path
from typing import Callable, Optional, TypeVar

from Orchestrator.utils import atomic_json

T = TypeVar("T")

# config.py reads "config.ini" relative to the working directory (the service
# runs from the install root), so the writers default to that same file.
DEFAULT_PATH = Path("config.ini")


class ConfigIniError(Exception):
    """config.ini could not be read or parsed, so it was not written."""


def read_config_ini(path: Optional[os.PathLike] = None) -> configparser.ConfigParser:
    """A fresh parse of config.ini (empty when the file does not exist).
    Raises ConfigIniError when the file cannot be read or does not parse."""
    path = Path(path or DEFAULT_PATH)
    parser = configparser.ConfigParser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parser
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigIniError(f"cannot read {path}: {exc}") from exc
    try:
        parser.read_string(text, source=str(path))
    except configparser.Error as exc:
        raise ConfigIniError(f"{path} does not parse ({exc}); refusing to rewrite it") from exc
    return parser


def update_config_ini(edit: Callable[[configparser.ConfigParser], T],
                      path: Optional[os.PathLike] = None) -> T:
    """Fresh read-modify-write of config.ini under a lock, written atomically.

    `edit` receives a parser loaded from the file as it is NOW (not the
    boot-time CFG) and changes it in place; its return value is returned. An
    exception from `edit` writes nothing. The file keeps its mode. Callers that
    keep a live copy (config.CFG, USERS_LIST) refresh it from the result
    afterwards.
    """
    path = Path(path or DEFAULT_PATH)
    with atomic_json.locked(path):
        parser = read_config_ini(path)
        result = edit(parser)
        out = io.StringIO()
        parser.write(out)
        atomic_json.write_text(path, out.getvalue(), mode=None)
        return result


def users_list(parser: configparser.ConfigParser, fallback: str = "") -> list:
    """The [users] list roster from a parser, as config.py parses it."""
    return [u.strip() for u in parser.get("users", "list", fallback=fallback).split(",") if u.strip()]
