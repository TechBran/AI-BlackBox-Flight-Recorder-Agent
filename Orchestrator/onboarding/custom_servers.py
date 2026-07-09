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
# Serializes read-modify-write within THIS process; the Orchestrator is assumed
# to be the registry's single writer process (no cross-process locking).
_LOCK = threading.Lock()
SEP = "::"
DEFAULT_CONTEXT_TOKENS = 32768

# Customer-facing wizard-guidance messages -- single source shared by the chat
# routes (stream + non-stream) and the cron executor (previously triplicated
# inline). MSG_NO_MODELS is a .format template: MSG_NO_MODELS.format(alias=...).
MSG_NO_SERVERS = "No custom model servers configured — add one in the onboarding wizard"
MSG_NO_MODELS = "Server '{alias}' has no discovered models — validate it in the wizard"

_PATCHABLE_FIELDS = {
    "alias", "base_url", "api_key", "enabled",
    "context_tokens", "validated_at", "last_models",
}
_EMPTY = {"version": 1, "servers": []}


# ---------------------------------------------------------------- persistence

def _quarantine(path: str) -> str | None:
    """Best-effort rename of a corrupt registry so the next _write can't destroy it.

    Returns the quarantine path, or None if the rename failed (fail-soft).
    """
    dest = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def _read() -> dict:
    """Load the registry from disk. Fail-soft: NEVER raises.

    Absent file, corrupt JSON, or wrong shape all return an empty registry.
    Corrupt/wrong-shape files are quarantined (renamed *.corrupt-<ts>) first so
    a subsequent add_server can't permanently overwrite stored servers/keys.
    """
    path = str(REGISTRY_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantined = _quarantine(path)
        logger.warning(
            "custom_servers: corrupt registry at %s (%s) -- quarantined to %s, treating as empty",
            path, exc, quarantined or "<quarantine failed>",
        )
        return copy.deepcopy(_EMPTY)
    except OSError as exc:
        logger.warning("custom_servers: unreadable registry at %s (%s) -- treating as empty", path, exc)
        return copy.deepcopy(_EMPTY)
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        quarantined = _quarantine(path)
        logger.warning(
            "custom_servers: registry at %s has wrong shape -- quarantined to %s, treating as empty",
            path, quarantined or "<quarantine failed>",
        )
        return copy.deepcopy(_EMPTY)
    # A hand-edited file with stray non-dict entries must not crash readers.
    data["servers"] = [s for s in data["servers"] if isinstance(s, dict)]
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


def _validate_field_types(fields: dict) -> None:
    """ValueError on wrong-typed values. (alias/base_url are covered by
    _validate_alias/_normalize_base_url; bool is checked before int because
    bool is an int subclass.)"""
    if "api_key" in fields and not isinstance(fields["api_key"], str):
        raise ValueError("api_key must be a string")
    if "enabled" in fields and not isinstance(fields["enabled"], bool):
        raise ValueError("enabled must be a bool")
    if "context_tokens" in fields:
        v = fields["context_tokens"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError("context_tokens must be a positive int")
    if "validated_at" in fields:
        v = fields["validated_at"]
        if v is not None and not isinstance(v, str):
            raise ValueError("validated_at must be a string or None")
    if "last_models" in fields:
        v = fields["last_models"]
        if not isinstance(v, list) or not all(isinstance(m, str) for m in v):
            raise ValueError("last_models must be a list of strings")


# ------------------------------------------------------------------ read API

def list_servers(enabled_only: bool = False) -> list[dict]:
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


def redact(server: dict) -> dict:
    """Return a copy of a server record safe for API responses: api_key is
    dropped and replaced by key_present / key_last4. Single source of truth
    for the redacted shape (routes must never hand-roll their own masking)."""
    srv = dict(server)
    key = srv.pop("api_key", "") or ""
    srv["key_present"] = bool(key)
    srv["key_last4"] = key[-4:] if key else ""
    return srv


def list_servers_redacted() -> list[dict]:
    """Servers without api_key, plus key_present / key_last4 for UI display."""
    return [redact(srv) for srv in list_servers()]


# -------------------------------------------------------------- mutation API

def add_server(alias: str, base_url: str, api_key: str = "",
               context_tokens: int = DEFAULT_CONTEXT_TOKENS) -> dict:
    """Register a new server. Returns the created record (a copy)."""
    if isinstance(alias, str):
        alias = alias.strip()
    _validate_field_types({"api_key": api_key, "context_tokens": context_tokens})
    with _LOCK:
        data = _read()
        _validate_alias(alias, data["servers"])
        srv = {
            "id": f"srv-{uuid.uuid4().hex[:8]}",
            "alias": alias,
            "base_url": _normalize_base_url(base_url),
            "api_key": api_key,
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
    """Patch an existing server (allowlisted, type-checked fields only).

    Returns the updated record. Unknown field or bad value -> ValueError;
    unknown id -> KeyError.
    """
    unknown = set(patch) - _PATCHABLE_FIELDS
    if unknown:
        raise ValueError(f"Unpatchable field(s): {sorted(unknown)}")
    _validate_field_types(patch)
    with _LOCK:
        data = _read()
        for srv in data["servers"]:
            if srv.get("id") == server_id:
                patch = dict(patch)
                if "alias" in patch:
                    if isinstance(patch["alias"], str):
                        patch["alias"] = patch["alias"].strip()
                    _validate_alias(patch["alias"], data["servers"], exclude_id=server_id)
                if "base_url" in patch:
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


def resolve_model(model: str) -> tuple[dict | None, str]:
    """Map a (possibly alias-qualified) model name to (server, bare_model).

    'alias::model' -> the enabled server with that alias (case-insensitive).
    A syntactically valid alias that matches no ENABLED server fails fast with
    (None, model) -- never silently rerouted to a different server. A prefix
    that cannot be an alias (e.g. 'org/name::tag') is treated as part of an
    unqualified model id.
    Unqualified    -> first enabled server that listed it in last_models,
                      else the first enabled server.
    No enabled servers -> (None, model).
    """
    enabled = list_servers(enabled_only=True)

    if SEP in model:
        alias, bare = model.split(SEP, 1)
        lowered = alias.lower()
        for srv in enabled:
            if srv.get("alias", "").lower() == lowered:
                return srv, bare
        if _ALIAS_RE.match(alias):
            # Valid alias with no enabled match: fail fast rather than routing
            # the request to some other server with a confusing far-end error.
            return None, model
        # Prefix can't be an alias -> whole string is an unqualified model id.

    if not enabled:
        return None, model
    for srv in enabled:
        models = srv.get("last_models")
        if isinstance(models, list) and model in models:
            return srv, model
    return enabled[0], model


def window_guard_tokens(server: dict | None) -> int:
    """Floor-token window-guard budget for a resolved custom server.

    0.6 x the server's context_tokens (the other 40% stays reserved for the
    system prompt, user history, and the reply), floored at 4,000 tokens so a
    tiny window can't starve retrieval entirely. None / a record missing the
    key derive from DEFAULT_CONTEXT_TOKENS. The ONE live formula shared by
    both /chat/stream routes; context_builder's static "custom" entry is a
    rounded-down snapshot of the None case (see PROVIDER_WINDOW_GUARD_TOKENS).
    """
    return max(4000, int((server or {}).get("context_tokens", DEFAULT_CONTEXT_TOKENS) * 0.6))
