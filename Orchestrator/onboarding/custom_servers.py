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
# inline). MSG_NO_MODELS / MSG_UNKNOWN_ALIAS are .format templates.
MSG_NO_SERVERS = "No custom model servers configured — add one in the onboarding wizard"
MSG_NO_MODELS = "Server '{alias}' has no discovered models — validate it in the wizard"
MSG_UNKNOWN_ALIAS = "Custom model '{model}' names an unknown or disabled server alias — check the alias in the onboarding wizard"

_PATCHABLE_FIELDS = {
    "alias", "base_url", "api_key", "enabled",
    "context_tokens", "validated_at", "last_models", "model_context",
    "model_modalities",
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
    if "model_context" in fields:
        v = fields["model_context"]
        if not isinstance(v, dict) or not all(
            isinstance(k, str)
            and not isinstance(val, bool) and isinstance(val, int) and val > 0
            for k, val in v.items()
        ):
            raise ValueError(
                "model_context must be a dict of {model_id: positive int tokens}"
            )
    if "model_modalities" in fields:
        v = fields["model_modalities"]
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(val, str) for k, val in v.items()
        ):
            raise ValueError("model_modalities must be a dict of {model_id: modality}")


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
               context_tokens: int = DEFAULT_CONTEXT_TOKENS,
               model_modalities: dict | None = None) -> dict:
    """Register a new server. Returns the created record (a copy)."""
    if isinstance(alias, str):
        alias = alias.strip()
    fields = {"api_key": api_key, "context_tokens": context_tokens}
    if model_modalities is not None:
        fields["model_modalities"] = model_modalities
    _validate_field_types(fields)
    with _LOCK:
        data = _read()
        _validate_alias(alias, data["servers"])
        srv = {
            "id": f"srv-{uuid.uuid4().hex[:8]}",
            "alias": alias,
            "base_url": _normalize_base_url(base_url),
            "api_key": api_key,
            "context_tokens": context_tokens,
            # Per-model overrides (bare model id -> real context tokens) for
            # multi-model hosts (llama-swap) whose models have DIFFERENT
            # windows; auto-learned from llama.cpp exceed_context_size_error
            # 400s (chat_routes) or hand-patched. Kept by redact() — not a secret.
            "model_context": {},
            "enabled": True,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "validated_at": None,
            "last_models": [],
            # Wizard-confirmed {model_id: modality} map (chat/image/tts/stt/...);
            # empty = fall back to name-pattern classify_model() at read time.
            "model_modalities": model_modalities or {},
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


def window_guard_tokens(server: dict | None, model: str | None = None) -> int:
    """Floor-token window-guard budget for a resolved custom server.

    0.6 x the effective context window (the other 40% stays reserved for the
    system prompt, user history, and the reply), floored at 4,000 tokens so a
    tiny window can't starve retrieval entirely. None / a record missing the
    key derive from DEFAULT_CONTEXT_TOKENS. The ONE live formula shared by
    both /chat/stream routes and the tasks.py non-stream guard;
    context_builder's static "custom" entry is a rounded-down snapshot of the
    None case (see PROVIDER_WINDOW_GUARD_TOKENS).

    `model` (the FINAL dispatched BARE id) selects a per-model override from
    the server's model_context map — multi-model hosts (llama-swap) serve
    models with DIFFERENT real windows behind one record, and the server-wide
    context_tokens over-budgets the smaller ones (llama.cpp then 400s the
    whole turn). Absent/None model, a model not in the map, or junk map
    values all fall back to context_tokens (backward compatible).
    """
    srv = server or {}
    ctx = srv.get("context_tokens", DEFAULT_CONTEXT_TOKENS)
    per_model = srv.get("model_context")
    if model and isinstance(per_model, dict):
        learned = per_model.get(model)
        if isinstance(learned, int) and not isinstance(learned, bool) and learned > 0:
            ctx = learned
    return max(4000, int(ctx * 0.6))


# ----------------------------------------------------- model modality (v1)
# OpenAI /v1/models carries NO modality flag, so we classify each discovered
# model by id (SEED); the wizard-confirmed model_modalities map overrides at
# runtime via model_modality(). EDIT the patterns to teach a new local family.
MODALITY_PATTERNS = {
    "image": ("z-image", "zimage", "flux", "qwen-image", "sdxl", "sd3", "sd-turbo",
              "stable-diffusion", "playground-v", "kolors", "hidream", "pixart"),
    "tts":   ("tts", "-speech", "speech-", "kokoro", "piper", "xtts", "bark",
              "vibevoice", "orpheus", "parler", "styletts", "melotts", "chatterbox"),
    "stt":   ("whisper", "-stt", "stt-", "transcrib", "parakeet", "scribe",
              "distil-whisper", "faster-whisper", "canary", "moonshine"),
    "embedding": ("embed", "bge", "gte", "e5-", "nomic-embed", "mxbai", "jina-embed",
                  "arctic-embed", "snowflake-arctic-embed"),
}
_ROUTABLE_MODALITIES = ("image", "tts", "stt", "embedding")  # precedence; else -> chat


def classify_model(model_id: object) -> str:
    """Seed modality for a bare model id: 'image'|'tts'|'stt'|'embedding'|'chat'.

    Name-pattern allowlist + an ``*-image`` suffix fallback; default 'chat'. Only
    a SEED -- the persisted model_modalities map (wizard-confirmed) wins at runtime
    via model_modality(). OpenAI /v1/models has no modality flag, so a
    misclassification is expected and correctable in the wizard."""
    if not isinstance(model_id, str):
        return "chat"
    m = model_id.lower()
    for modality in _ROUTABLE_MODALITIES:
        if any(p in m for p in MODALITY_PATTERNS[modality]):
            return modality
    if m.endswith("-image") or m.endswith("_image"):
        return "image"
    return "chat"


def classify_models(model_ids: list) -> dict:
    """Seed map {model_id: modality} for a discovered model list (used by /validate)."""
    return {m: classify_model(m) for m in model_ids if isinstance(m, str)}


def is_image_model(model_id: object) -> bool:
    """Back-compat wrapper (shipped image callers). Prefer model_modality()."""
    return classify_model(model_id) == "image"


def model_modality(server: dict, model_id: str) -> str:
    """AUTHORITATIVE modality for a model on a server: the wizard-confirmed
    model_modalities map first, name-pattern classify() as fallback (servers
    registered before the confirm feature, or models discovered since)."""
    mm = server.get("model_modalities") if isinstance(server, dict) else None
    if isinstance(mm, dict):
        val = mm.get(model_id)
        if isinstance(val, str) and val:
            return val
    return classify_model(model_id)


def resolve_modality_server(modality: str, model: str | None = None) -> tuple[dict, str] | None:
    """Pick the (server, bare_model) for a request of ``modality``.

    An explicit ``model`` is honored only if it IS that modality AND the resolved
    server hosts it; otherwise the first enabled server hosting a model of that
    modality, and its first such model. ``None`` when unavailable. Fresh read."""
    if model:
        srv, bare = resolve_model(model)
        if srv is not None and model_modality(srv, bare) == modality and bare in (srv.get("last_models") or []):
            return srv, bare
    for srv in list_servers(enabled_only=True):
        for m in (srv.get("last_models") or []):
            if isinstance(m, str) and model_modality(srv, m) == modality:
                return srv, m
    return None


def has_modality_model(modality: str) -> bool:
    """True iff any enabled custom server hosts a model of ``modality``."""
    return resolve_modality_server(modality) is not None


def resolve_image_server(model: str | None = None) -> tuple[dict, str] | None:
    """Back-compat wrapper -> resolve_modality_server('image')."""
    return resolve_modality_server("image", model)


def list_image_models() -> list[str]:
    """Alias-qualified ids ('alias::model') of every image model on every enabled
    server -- the future source for a local-model picker; today used by tests."""
    out = []
    for srv in list_servers(enabled_only=True):
        alias = srv.get("alias", "")
        for m in (srv.get("last_models") or []):
            if isinstance(m, str) and model_modality(srv, m) == "image":
                out.append(qualify(alias, m))
    return out


def resolve_tts_server(model: str | None = None) -> tuple[dict, str] | None:
    """(server, bare_model) for a local text-to-speech request."""
    return resolve_modality_server("tts", model)


def resolve_stt_server(model: str | None = None) -> tuple[dict, str] | None:
    """(server, bare_model) for a local speech-to-text request."""
    return resolve_modality_server("stt", model)


def list_local_tts_voices(server: dict) -> list[str]:
    """Voice ids a local /v1/audio/speech server offers. Probe the (non-standard)
    GET {base_url}/audio/voices; fail-soft to ['default'] (OpenAI-compat TTS
    servers accept a ``voice`` param but rarely advertise a list). Lazy ``requests``
    import keeps this module stdlib-only at import time (lean-venv-safe)."""
    base_url = (server or {}).get("base_url", "")
    if not base_url:
        return ["default"]
    import requests as _rq
    headers = {}
    if server.get("api_key"):
        headers["Authorization"] = f"Bearer {server['api_key']}"
    try:
        r = _rq.get(f"{base_url}/audio/voices", headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json()
        items = data.get("voices") if isinstance(data, dict) else data
        out = []
        for it in (items or []):
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                vid = it.get("id") or it.get("voice") or it.get("name")
                if isinstance(vid, str):
                    out.append(vid)
        return out or ["default"]
    except Exception:
        return ["default"]
