"""Registry of voice-agent presets (provider-agnostic local 'agent builder').

Stored OUTSIDE git (credentials/ is gitignored) so presets survive pulls.
Read FRESH by every consumer -- no import-time constants (E8 lesson).
Persistence conventions copied line-for-line from
Orchestrator/onboarding/custom_servers.py (quarantine, atomic write, 0600).
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

from Orchestrator.utils.paths import resolve  # canonical root resolver (BLACKBOX_ROOT-aware)

logger = logging.getLogger(__name__)
REGISTRY_PATH = resolve("credentials", "voice_agents.json")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.'()-]{0,63}$")
# Serializes read-modify-write within THIS process; the Orchestrator is the
# registry's single writer process (no cross-process locking).
_LOCK = threading.Lock()

PROVIDERS = ("realtime", "gemini-live", "grok-live")
INSTRUCTIONS_MAX_CHARS = 20000   # well under REALTIME_CONTEXT_MAX_CHARS=50000
GREETING_MAX_CHARS = 2000
KEYTERMS_MAX = 100               # xAI hard limit (keyterms <= 100)

_OPTIONAL_STR_FIELDS = ("model", "voice", "instructions", "tool_group_override",
                        "greeting", "language")
_PATCHABLE_FIELDS = {"name", "provider", "model", "voice", "instructions",
                     "tool_group_override", "greeting", "language", "keyterms"}
_EMPTY = {"version": 1, "agents": []}

# Connect-time fields a preset can supply. Precedence: explicit > preset > defaults.
PRESET_CONNECT_FIELDS = ("model", "voice", "greeting", "instructions",
                         "tool_group_override", "language", "keyterms")

# make_phone_call / twilio backend id per preset provider (twilio_routes backend_map keys).
PROVIDER_PHONE_BACKENDS = {"realtime": "openai_realtime",
                           "gemini-live": "gemini_live",
                           "grok-live": "grok_live"}


# ---------------------------------------------------------------- persistence

def _quarantine(path: str) -> str | None:
    """Best-effort rename of a corrupt registry so the next _write can't destroy it."""
    dest = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def _read() -> dict:
    """Load the registry from disk. Fail-soft: NEVER raises."""
    path = str(REGISTRY_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantined = _quarantine(path)
        logger.warning("voice_agents: corrupt registry at %s (%s) -- quarantined to %s",
                       path, exc, quarantined or "<quarantine failed>")
        return copy.deepcopy(_EMPTY)
    except OSError as exc:
        logger.warning("voice_agents: unreadable registry at %s (%s)", path, exc)
        return copy.deepcopy(_EMPTY)
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        quarantined = _quarantine(path)
        logger.warning("voice_agents: registry at %s has wrong shape -- quarantined to %s",
                       path, quarantined or "<quarantine failed>")
        return copy.deepcopy(_EMPTY)
    data["agents"] = [a for a in data["agents"] if isinstance(a, dict)]
    return data


def _write(data: dict) -> None:
    """Atomically persist the registry (tmp file + os.replace), 0600 perms."""
    path = str(REGISTRY_PATH)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                      prefix=".voice_agents.", suffix=".tmp", delete=False)
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

def _validate_name(name: str, agents: list, exclude_id: str | None = None) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"Invalid preset name {name!r}: 1-64 chars, letters/digits/"
                         f"space/_/./'/(/)/- only, must start with a letter or digit.")
    lowered = name.lower()
    for agent in agents:
        if agent.get("id") != exclude_id and agent.get("name", "").lower() == lowered:
            raise ValueError(f"Preset name {name!r} is already in use (case-insensitive).")


def _validate_fields(fields: dict) -> None:
    """ValueError on wrong-typed or oversized values."""
    if "provider" in fields and fields["provider"] not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS} (got {fields['provider']!r})")
    for key in _OPTIONAL_STR_FIELDS:
        if key in fields and not isinstance(fields[key], str):
            raise ValueError(f"{key} must be a string")
    if len(fields.get("instructions", "")) > INSTRUCTIONS_MAX_CHARS:
        raise ValueError(f"instructions exceeds {INSTRUCTIONS_MAX_CHARS} chars")
    if len(fields.get("greeting", "")) > GREETING_MAX_CHARS:
        raise ValueError(f"greeting exceeds {GREETING_MAX_CHARS} chars")
    if "keyterms" in fields:
        v = fields["keyterms"]
        if not isinstance(v, list) or not all(isinstance(k, str) for k in v):
            raise ValueError("keyterms must be a list of strings")
        if len(v) > KEYTERMS_MAX:
            raise ValueError(f"keyterms exceeds the {KEYTERMS_MAX}-item limit (xAI hard cap)")


# ------------------------------------------------------------------ read API

def list_presets(provider: str | None = None) -> list[dict]:
    """Return all presets (copies -- safe to mutate), optionally provider-filtered."""
    agents = _read()["agents"]
    if provider:
        agents = [a for a in agents if a.get("provider") == provider]
    return copy.deepcopy(agents)


def get_preset(preset_id: str) -> dict | None:
    """Return the preset with this id (a copy), or None."""
    for agent in _read()["agents"]:
        if agent.get("id") == preset_id:
            return copy.deepcopy(agent)
    return None


# -------------------------------------------------------------- mutation API

def add_preset(name: str, provider: str, created_by: str = "", model: str = "",
               voice: str = "", instructions: str = "", tool_group_override: str = "",
               greeting: str = "", language: str = "",
               keyterms: list[str] | None = None) -> dict:
    """Register a new preset. Returns the created record (a copy)."""
    if isinstance(name, str):
        name = name.strip()
    keyterms = keyterms or []
    _validate_fields({"provider": provider, "model": model, "voice": voice,
                      "instructions": instructions, "tool_group_override": tool_group_override,
                      "greeting": greeting, "language": language, "keyterms": keyterms})
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        data = _read()
        _validate_name(name, data["agents"])
        agent = {
            "id": f"va-{uuid.uuid4().hex[:8]}",
            "name": name,
            "provider": provider,
            "model": model,
            "voice": voice,
            "instructions": instructions,
            "tool_group_override": tool_group_override,
            "greeting": greeting,
            "language": language,
            "keyterms": list(keyterms),
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        data["agents"].append(agent)
        _write(data)
        return copy.deepcopy(agent)


def update_preset(preset_id: str, patch: dict) -> dict:
    """Patch an existing preset (allowlisted, type-checked fields only).

    Unknown field or bad value -> ValueError; unknown id -> KeyError.
    Bumps updated_at.
    """
    unknown = set(patch) - _PATCHABLE_FIELDS
    if unknown:
        raise ValueError(f"Unpatchable field(s): {sorted(unknown)}")
    _validate_fields(patch)
    with _LOCK:
        data = _read()
        for agent in data["agents"]:
            if agent.get("id") == preset_id:
                patch = dict(patch)
                if "name" in patch:
                    if isinstance(patch["name"], str):
                        patch["name"] = patch["name"].strip()
                    _validate_name(patch["name"], data["agents"], exclude_id=preset_id)
                agent.update(patch)
                agent["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write(data)
                return copy.deepcopy(agent)
        raise KeyError(f"No voice agent preset with id {preset_id!r}")


def delete_preset(preset_id: str) -> None:
    """Remove a preset from the registry."""
    with _LOCK:
        data = _read()
        remaining = [a for a in data["agents"] if a.get("id") != preset_id]
        if len(remaining) == len(data["agents"]):
            raise KeyError(f"No voice agent preset with id {preset_id!r}")
        data["agents"] = remaining
        _write(data)


# ----------------------------------------------------------------- resolution

def resolve_preset(agent_id: str | None, provider: str | None = None) -> dict | None:
    """Fresh-read a preset by id for apply-at-configure.

    Returns None (never raises) for a missing/unknown id, or when `provider`
    is given and doesn't match the preset — callers surface a loud client
    warning and continue without the preset (fresh-box graceful degradation).
    """
    if not agent_id:
        return None
    preset = get_preset(agent_id)
    if preset is None:
        logger.warning("voice_agents: unknown preset id %r", agent_id)
        return None
    if provider is not None and preset.get("provider") != provider:
        logger.warning("voice_agents: preset %r is provider=%r, requested %r — ignoring",
                       agent_id, preset.get("provider"), provider)
        return None
    return preset


def merge_connect_params(explicit: dict, preset: dict | None) -> dict:
    """Precedence merge for WS connect handling: explicit > preset > None.

    Empty values ("", None, [], {}) in `explicit` fall through to the preset;
    empty preset values yield None so each route's existing defaults apply
    unchanged. Returns a dict covering every PRESET_CONNECT_FIELDS key.
    """
    _EMPTYISH = (None, "", [], {})
    merged: dict = {}
    for field in PRESET_CONNECT_FIELDS:
        value = explicit.get(field)
        if value in _EMPTYISH:
            value = (preset or {}).get(field)
        merged[field] = value if value not in _EMPTYISH else None
    return merged


def resolve_phone_role(role: str, backend: str, greeting: str) -> tuple[str, str, str]:
    """make_phone_call server-side 'preset:<id>' resolution.

    Selecting a preset IS the explicit agent choice: its instructions become
    the call persona and its provider determines the phone backend. An
    explicit greeting still wins over the preset's. Non-preset roles pass
    through untouched. Unknown preset id -> KeyError (fail loudly — never
    silently place a call with the literal string 'preset:...' as persona).
    """
    if not (isinstance(role, str) and role.startswith("preset:")):
        return role, backend, greeting
    preset_id = role[len("preset:"):].strip()
    preset = get_preset(preset_id)
    if preset is None:
        raise KeyError(f"Unknown voice agent preset {preset_id!r}")
    return (
        preset.get("instructions") or "",
        PROVIDER_PHONE_BACKENDS.get(preset.get("provider"), backend),
        greeting or preset.get("greeting") or "",
    )
