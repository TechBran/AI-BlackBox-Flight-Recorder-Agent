"""Registry of user-added OpenAI-compatible model servers (provider 'custom').

Stored OUTSIDE git (credentials/ is gitignored) so servers survive pulls.
Read FRESH by every consumer -- no import-time constants (E8 lesson).
NOTE: /onboarding/validate probing user-supplied base_urls is LAN-trust by
design; Tailscale is the perimeter (do not add app-layer auth here).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from Orchestrator.utils.paths import resolve  # canonical root resolver: honors
# BLACKBOX_ROOT env var FIRST (state.py precedent; stdlib-only, lean-venv-safe).
# Do NOT hand-roll dirname math -- installed boxes relocate the tree.

logger = logging.getLogger(__name__)
REGISTRY_PATH = resolve("credentials", "custom_models.json")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$")  # no '::' possible
_LOCK = threading.Lock()
SEP = "::"
DEFAULT_CONTEXT_TOKENS = 32768

_PATCHABLE_FIELDS = {
    "alias", "base_url", "api_key", "enabled",
    "context_tokens", "validated_at", "last_models",
}
_EMPTY = {"version": 1, "servers": []}


# ---------------------------------------------------------------- persistence

def _read() -> dict:
    """Load the registry from disk. Fail-soft: NEVER raises.

    Absent file, corrupt JSON, or wrong shape all return an empty registry.
    """
    path = str(REGISTRY_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("custom_servers: corrupt/unreadable registry at %s (%s) -- treating as empty", path, exc)
        return copy.deepcopy(_EMPTY)
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        logger.warning("custom_servers: registry at %s has wrong shape -- treating as empty", path)
        return copy.deepcopy(_EMPTY)
    return data


def _write(data: dict) -> None:
    """Atomically persist the registry (tmp file + os.replace), 0600 perms."""
    path = str(REGISTRY_PATH)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".custom_models.", suffix=".tmp", delete=False,
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


# ----------------------------------------------------------------- validation

def _validate_alias(alias: str, servers: list, exclude_id: str | None = None) -> None:
    if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
        raise ValueError(
            f"Invalid alias {alias!r}: 1-32 chars, letters/digits/space/_/./- only, "
            f"must start with a letter or digit."
        )
    lowered = alias.lower()
    for srv in servers:
        if srv.get("id") != exclude_id and srv.get("alias", "").lower() == lowered:
            raise ValueError(f"Alias {alias!r} is already in use (case-insensitive).")


def _normalize_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("base_url must be a string")
    normalized = base_url.strip().rstrip("/")
    if not (normalized.startswith("http://") or normalized.startswith("https://")):
        raise ValueError(f"base_url must start with http:// or https:// (got {base_url!r})")
    return normalized


# ------------------------------------------------------------------ read API

def list_servers(enabled_only: bool = False) -> list:
    """Return all registered servers (copies -- safe to mutate)."""
    servers = _read()["servers"]
    if enabled_only:
        servers = [s for s in servers if s.get("enabled")]
    return copy.deepcopy(servers)


def get_server(server_id: str) -> dict | None:
    """Return the server with this id (a copy), or None."""
    for srv in _read()["servers"]:
        if srv.get("id") == server_id:
            return copy.deepcopy(srv)
    return None


def list_servers_redacted() -> list:
    """Servers without api_key, plus key_present / key_last4 for UI display."""
    redacted = []
    for srv in list_servers():
        key = srv.pop("api_key", "") or ""
        srv["key_present"] = bool(key)
        srv["key_last4"] = key[-4:] if key else ""
        redacted.append(srv)
    return redacted


# -------------------------------------------------------------- mutation API

def add_server(alias: str, base_url: str, api_key: str = "",
               context_tokens: int = DEFAULT_CONTEXT_TOKENS) -> dict:
    """Register a new server. Returns the created record (a copy)."""
    with _LOCK:
        data = _read()
        _validate_alias(alias, data["servers"])
        srv = {
            "id": f"srv-{uuid.uuid4().hex[:8]}",
            "alias": alias,
            "base_url": _normalize_base_url(base_url),
            "api_key": api_key or "",
            "context_tokens": context_tokens,
            "enabled": True,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "validated_at": None,
            "last_models": [],
        }
        data["servers"].append(srv)
        _write(data)
        return copy.deepcopy(srv)


def update_server(server_id: str, patch: dict) -> dict:
    """Patch an existing server (allowlisted fields only). Returns the updated record."""
    unknown = set(patch) - _PATCHABLE_FIELDS
    if unknown:
        raise ValueError(f"Unpatchable field(s): {sorted(unknown)}")
    with _LOCK:
        data = _read()
        for srv in data["servers"]:
            if srv.get("id") == server_id:
                if "alias" in patch:
                    _validate_alias(patch["alias"], data["servers"], exclude_id=server_id)
                if "base_url" in patch:
                    patch = dict(patch)
                    patch["base_url"] = _normalize_base_url(patch["base_url"])
                srv.update(patch)
                _write(data)
                return copy.deepcopy(srv)
        raise KeyError(f"No custom server with id {server_id!r}")


def delete_server(server_id: str) -> None:
    """Remove a server from the registry."""
    with _LOCK:
        data = _read()
        remaining = [s for s in data["servers"] if s.get("id") != server_id]
        if len(remaining) == len(data["servers"]):
            raise KeyError(f"No custom server with id {server_id!r}")
        data["servers"] = remaining
        _write(data)


# ----------------------------------------------------------------- resolution

def qualify(alias: str, model_id: str) -> str:
    """Build the qualified model name shown in catalogs: '<alias>::<model_id>'."""
    return f"{alias}{SEP}{model_id}"


def resolve_model(model: str) -> tuple:
    """Map a (possibly alias-qualified) model name to (server, bare_model).

    'alias::model' -> the enabled server with that alias (case-insensitive).
    Unqualified    -> first enabled server that listed it in last_models,
                      else the first enabled server.
    No enabled servers (or unknown alias with no fallback) -> (None, model).
    """
    enabled = list_servers(enabled_only=True)

    if SEP in model:
        alias, bare = model.split(SEP, 1)
        lowered = alias.lower()
        for srv in enabled:
            if srv.get("alias", "").lower() == lowered:
                return srv, bare
        # Unknown alias: treat the whole string as an unqualified model name.

    if not enabled:
        return None, model
    for srv in enabled:
        if model in (srv.get("last_models") or []):
            return srv, model
    return enabled[0], model
