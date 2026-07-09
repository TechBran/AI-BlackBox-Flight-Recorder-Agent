"""Onboarding wizard backend routes.

Mounted at /onboarding/* by Orchestrator/app.py.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from Orchestrator.onboarding import custom_servers, validators
from Orchestrator.onboarding.secrets_writer import update_env
from Orchestrator.onboarding.state import (
    StepName,
    ALL_STEPS,
    get_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


ALLOWED_REVEAL_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "PERPLEXITY_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
}


def _redact(value: str | None, keep: int = 4) -> str | None:
    """Show last N chars only; full mask if value shorter than 2*keep."""
    if not value:
        return None
    if len(value) < 2 * keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


# Use the singleton from state.py — DO NOT instantiate OnboardingState() directly.
_state = get_state()


def _advance_current_to_next(completed_step: str) -> None:
    """After step X completes/skips, move current_step to the next step in ALL_STEPS.

    No-op if X is unknown OR if X is the final step (no next).
    """
    try:
        idx = ALL_STEPS.index(completed_step)
    except ValueError:
        logger.warning("auto-advance skipped: %r not in ALL_STEPS", completed_step)
        return
    if idx + 1 < len(ALL_STEPS):
        _state.set_current(ALL_STEPS[idx + 1])


class StateResponse(BaseModel):
    is_complete: bool
    completed_steps: list[str]
    skipped_steps: list[str]
    current_step: str
    all_steps: list[str]


class CurrentConfigResponse(BaseModel):
    """Redacted snapshot of current setup state. Sensitive values shown as last-4 only.

    Use GET /onboarding/config/{key}?reveal=1 (T1.4.3) to fetch full value of a single key.
    Loopback-only via T1.3.2 first-run middleware once that lands.
    """
    providers: dict[str, dict]
    operators: list[str]
    paired_devices: list[dict]
    tailscale: dict
    onboarding_state: dict
    stt: dict
    web_search: dict
    image: dict


class ValidateRequest(BaseModel):
    provider: Literal["openai", "anthropic", "google", "xai", "perplexity", "voyage", "cohere", "tailscale", "gmail", "elevenlabs", "custom"]
    credentials: dict[str, str] = {}  # provider-specific shape; tailscale needs none


class ValidateResponse(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict | None = None


class SaveRequest(BaseModel):
    secrets: dict[str, str]  # env-var name -> value


class StepActionRequest(BaseModel):
    step: StepName


@router.get("/state", response_model=StateResponse)
def get_onboarding_state() -> StateResponse:
    return StateResponse(**_state.snapshot())


@router.get("/current-config", response_model=CurrentConfigResponse)
def current_config() -> CurrentConfigResponse:
    """Return a redacted snapshot of what's configured today. Manage-mode UI reads this.

    E8 (Brandon's MSO2 Ultra testing 2026-05-17): API keys ARE saved correctly
    to .env by the /save endpoint, but reading them via Orchestrator.config.*
    module-level constants returns STALE values — those are computed once at
    import time from os.environ and don't refresh when .env is mutated. Customer
    sees 'all keys empty' on wizard re-entry even though .env has them. Fix:
    use dotenv_values() to read .env fresh on each call (doesn't pollute
    os.environ). Bonus: future API key edits via the wizard 'just work' without
    service restart — matches customer expectation for onboarding flows.
    """
    from dotenv import dotenv_values
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    env = dotenv_values(str(ENV_FILE))

    val_at = _state.validated_at()
    providers = {
        "openai": {
            "present": bool(env.get("OPENAI_API_KEY")),
            "last4": _redact(env.get("OPENAI_API_KEY")),
            "validated_at": val_at.get("openai"),
        },
        "anthropic": {
            "present": bool(env.get("ANTHROPIC_API_KEY")),
            "last4": _redact(env.get("ANTHROPIC_API_KEY")),
            "validated_at": val_at.get("anthropic"),
        },
        "google": {
            "present": bool(env.get("GOOGLE_API_KEY")),
            "last4": _redact(env.get("GOOGLE_API_KEY")),
            "validated_at": val_at.get("google"),
        },
        "xai": {
            "present": bool(env.get("XAI_API_KEY")),
            "last4": _redact(env.get("XAI_API_KEY")),
            "validated_at": val_at.get("xai"),
        },
        "perplexity": {
            "present": bool(env.get("PERPLEXITY_API_KEY")),
            "last4": _redact(env.get("PERPLEXITY_API_KEY")),
            "validated_at": val_at.get("perplexity"),
        },
        "elevenlabs": {
            "present": bool(env.get("ELEVENLABS_API_KEY")),
            "last4": _redact(env.get("ELEVENLABS_API_KEY")),
            "validated_at": val_at.get("elevenlabs"),
        },
        "gmail": {
            "present": bool(env.get("GOOGLE_OAUTH_CLIENT_ID") and env.get("GOOGLE_OAUTH_CLIENT_SECRET")),
            "client_id": env.get("GOOGLE_OAUTH_CLIENT_ID") or None,  # public per Google OAuth docs
            "secret_last4": _redact(env.get("GOOGLE_OAUTH_CLIENT_SECRET")),
            "validated_at": val_at.get("gmail"),
        },
    }
    # Tailscale status — live probe (~10-30ms via subprocess)
    try:
        # Tailscale live probe: subprocess to `tailscale status --json` with 5s ceiling
        # (see validators.validate_tailscale). Acceptable for human-driven manage-mode;
        # do NOT call this from auto-polling UI.
        from Orchestrator.onboarding.validators import validate_tailscale
        ts_result = validate_tailscale()
        tailscale = {
            "configured": ts_result.ok,
            "validated_at": val_at.get("tailscale"),
            "detail": ts_result.detail or {},
        }
    except Exception as e:
        logger.exception("current-config tailscale probe failed")
        tailscale = {"configured": False, "validated_at": val_at.get("tailscale"), "detail": {}}
    # Operators — read from admin_routes' module-level USERS_LIST
    try:
        from Orchestrator.routes import admin_routes
        operators = list(admin_routes.USERS_LIST)
    except Exception:
        logger.exception("current-config operator list import failed")
        operators = []
    # Paired devices — read from the persistent registry (E13). Fail-soft to
    # empty list so a malformed/missing paired_devices.json never bricks
    # wizard re-entry; pairing_routes already logs the read failure.
    try:
        from Orchestrator.routes.pairing_routes import list_paired_devices
        paired_devices = list_paired_devices()
    except Exception:
        logger.exception("current-config paired_devices load failed")
        paired_devices = []
    # STT preference — read fresh from .env (E8 pattern) so the wizard sees the
    # provider it just saved WITHOUT a service restart. STT_PROVIDER is a
    # preference, not a secret, so it's surfaced here in current-config rather
    # than gated behind the reveal allowlist. "" / absent == auto (resolver
    # picks whichever credential is present). The optional model-override keys
    # are echoed too so the wizard can show non-default model choices.
    stt = {
        "provider": (env.get("STT_PROVIDER") or "").strip().lower(),  # "" == auto
        "openai_file": (env.get("STT_OPENAI_FILE") or "").strip() or None,
        "google_model": (env.get("STT_GOOGLE_MODEL") or "").strip() or None,
    }
    # Web-search preferences — like STT, these are PREFERENCES not secrets.
    # Reuse the availability module (same module the ToolVault gate uses) so
    # the enabled set is derived in ONE place (DRY). These are live-read by
    # availability on every tool list — they deliberately are NOT in the
    # /restart-status drift checks because they take effect without a restart.
    from Orchestrator.toolvault.availability import enabled_web_search_providers, PROVIDER_ENV
    enabled = enabled_web_search_providers()
    web_search = {
        "providers": {
            prov: {
                "key_present": (True if keyvar is None else bool(env.get(keyvar))),  # duckduckgo is keyless
                "enabled": prov in enabled,
            }
            for prov, keyvar in PROVIDER_ENV.items()
        },
        "enabled": sorted(enabled),
        "default": (env.get("WEB_SEARCH_DEFAULT") or "").strip(),
    }
    # Image-generation preferences — same DRY pattern as web_search: reuse the
    # availability gate so the enabled set is derived in ONE place. Like
    # web_search these are live-read by availability on every tool list, so
    # IMAGE_ENABLED/IMAGE_DEFAULT are deliberately NOT in /restart-status drift
    # checks (they take effect without a restart). No keyless floor — every
    # image provider needs a key.
    from Orchestrator.toolvault.availability import enabled_providers, FEATURES
    img_enabled = enabled_providers("image")
    image = {
        "providers": {
            prov: {"key_present": bool(env.get(keyvar)), "enabled": prov in img_enabled}
            for prov, keyvar in FEATURES["image"]["provider_env"].items()  # gemini/openai/grok, all have a key
        },
        "enabled": sorted(img_enabled),
        "default": (env.get("IMAGE_DEFAULT") or "").strip(),
    }
    return CurrentConfigResponse(
        providers=providers,
        operators=operators,
        paired_devices=paired_devices,
        tailscale=tailscale,
        onboarding_state=_state.snapshot(),
        stt=stt,
        web_search=web_search,
        image=image,
    )


@router.get("/config/{key}")
def get_config_value(key: str, request: Request, reveal: bool = False) -> dict:
    """Return a single config value. With ?reveal=true, returns full cleartext.

    Loopback-only when revealing — refuses if request not from 127.0.0.1 / ::1.
    Without reveal, returns the same redacted ••••XXXX shape as /current-config.
    Both modes require the key to be in ALLOWED_REVEAL_KEYS.

    Caveats:
    - E8 (2026-05-17): NOW reads .env file fresh via dotenv_values() each call,
      so /save mutations are immediately visible without service restart. Old
      behavior (os.getenv stale-since-import) was the root cause of API keys
      appearing empty on wizard re-entry.
    - The loopback gate inspects request.client.host (immediate ASGI peer). DO
      NOT enable uvicorn's --proxy-headers without first reworking this check
      to consult X-Forwarded-For; otherwise any forwarded request would bypass
      the gate.
    """
    if key not in ALLOWED_REVEAL_KEYS:
        raise HTTPException(
            status_code=403,
            detail=f"key {key!r} not in reveal allowlist",
        )
    from dotenv import dotenv_values
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    value = dotenv_values(str(ENV_FILE)).get(key, "") or ""
    if reveal:
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                status_code=403,
                detail="reveal only permitted from loopback",
            )
        logger.info("config reveal: key=%s client=%s", key, client_host)
        return {"key": key, "value": value, "present": bool(value)}
    return {"key": key, "value": _redact(value), "present": bool(value)}


@router.delete("/config/{key}")
def delete_config_value(key: str) -> dict:
    """Delete a single allowlisted env-var from .env.

    Atomic + backup via secrets_writer.remove_env_keys. Same allowlist as
    GET /config/{key}?reveal=true. The deletion takes effect on disk
    immediately, but Orchestrator.config still holds the old value in memory
    until BlackBox restart.
    """
    if key not in ALLOWED_REVEAL_KEYS:
        raise HTTPException(
            status_code=403,
            detail=f"key {key!r} not in allowlist",
        )
    from Orchestrator.onboarding.secrets_writer import remove_env_keys
    try:
        result = remove_env_keys([key])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("config delete: key=%s removed=%s", key, result.get("removed_keys"))
    return {"ok": True, **result}


# ── Custom model servers (provider "custom") — registry CRUD ──

class CustomServerCreate(BaseModel):
    alias: str
    base_url: str
    api_key: str = ""          # default "" — custom_servers.add_server rejects None
    context_tokens: int = custom_servers.DEFAULT_CONTEXT_TOKENS


class CustomServerPatch(BaseModel):
    """Partial update — omitted (None) fields are left unchanged."""
    alias: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    context_tokens: int | None = None


@router.get("/custom-servers")
def list_custom_servers() -> dict:
    """List registered custom model servers (api_key redacted to last-4)."""
    return {"servers": custom_servers.list_servers_redacted()}


@router.post("/custom-servers")
def add_custom_server(req: CustomServerCreate) -> dict:
    """Register an OpenAI-compatible LAN server (llama.cpp / llama-swap / vLLM).

    LAN-trust stance: the user-supplied base_url will later be probed by
    POST /onboarding/validate — that is SSRF-shaped BY DESIGN. Tailscale/LAN
    is the security perimeter; do not add auth or URL allowlisting here.
    """
    try:
        srv = custom_servers.add_server(
            alias=req.alias, base_url=req.base_url,
            api_key=req.api_key, context_tokens=req.context_tokens,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("custom-servers add: id=%s alias=%s", srv["id"], srv["alias"])
    return {"server": custom_servers.redact(srv)}


@router.patch("/custom-servers/{server_id}")
def patch_custom_server(server_id: str, req: CustomServerPatch) -> dict:
    """Partially update a registered server; omitted fields are unchanged.

    A base_url change invalidates the server's validation stamps
    (validated_at/last_models): the old URL's model list must not survive a
    re-point, or resolve_model could route unqualified ids to a server that
    no longer hosts them. Re-validate to re-stamp.
    """
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if "base_url" in patch:
        patch.update({"validated_at": None, "last_models": []})
    try:
        srv = custom_servers.update_server(server_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]) if e.args else "not found")
    logger.info("custom-servers patch: id=%s fields=%s", server_id, sorted(patch))
    return {"server": custom_servers.redact(srv)}


@router.delete("/custom-servers/{server_id}")
def delete_custom_server(server_id: str) -> dict:
    """Remove a server from the registry."""
    try:
        custom_servers.delete_server(server_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]) if e.args else "not found")
    logger.info("custom-servers delete: id=%s", server_id)
    return {"ok": True}


# Provider -> the .env var holding its API key. Lets an ALREADY-CONFIGURED key
# be re-validated (a troubleshooting affordance) WITHOUT the client re-sending
# it: POST /validate {provider} with no credentials reads the stored value
# here. The client never has to hold the raw secret to validate it.
_VALIDATE_KEY_ENV = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
    # M10 reranker upgrade keys — re-validate a stored key without re-sending it.
    "voyage": "VOYAGE_API_KEY", "cohere": "COHERE_API_KEY",
}


def _resolve_stored_creds(provider: str, creds: dict) -> dict:
    """Fill missing credentials from the stored .env values so a configured key
    can be re-validated without re-entering it. Only reads .env when a needed
    credential is actually absent from the request."""
    out = dict(creds or {})
    need_key = provider in _VALIDATE_KEY_ENV and not out.get("api_key")
    need_gmail = provider == "gmail" and not (out.get("client_id") and out.get("client_secret"))
    if not (need_key or need_gmail):
        return out
    from dotenv import dotenv_values
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    env = dotenv_values(str(ENV_FILE))
    if need_key:
        stored = env.get(_VALIDATE_KEY_ENV[provider])
        if stored:
            out["api_key"] = stored
    if need_gmail:
        out.setdefault("client_id", env.get("GOOGLE_OAUTH_CLIENT_ID", "") or "")
        out.setdefault("client_secret", env.get("GOOGLE_OAUTH_CLIENT_SECRET", "") or "")
    return out


@router.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest) -> ValidateResponse:
    creds = _resolve_stored_creds(req.provider, req.credentials)
    try:
        if req.provider == "openai":
            result = validators.validate_openai(creds["api_key"])
        elif req.provider == "anthropic":
            result = validators.validate_anthropic(creds["api_key"])
        elif req.provider == "google":
            result = validators.validate_google(creds["api_key"])
        elif req.provider == "xai":
            result = validators.validate_xai(creds["api_key"])
        elif req.provider == "perplexity":
            result = validators.validate_perplexity(creds["api_key"])
        elif req.provider == "voyage":
            result = validators.validate_voyage(creds["api_key"])
        elif req.provider == "cohere":
            result = validators.validate_cohere(creds["api_key"])
        elif req.provider == "tailscale":
            result = validators.validate_tailscale()
        elif req.provider == "gmail":
            result = validators.validate_gmail_oauth(creds["client_id"], creds["client_secret"])
        elif req.provider == "elevenlabs":
            result = validators.validate_elevenlabs(creds["api_key"])
        elif req.provider == "custom":
            # Custom OpenAI-compatible server probe. LAN-trust stance: probing
            # a user-supplied base_url is SSRF-shaped BY DESIGN — Tailscale/LAN
            # is the security perimeter; do not add auth or URL allowlisting.
            # Stored-server re-validation: credentials may carry ONLY server_id,
            # in which case base_url/api_key resolve from the registry
            # (mirrors _resolve_stored_creds for .env-backed providers).
            server_id = creds.get("server_id") or ""
            base_url = creds.get("base_url") or ""
            api_key = creds.get("api_key") or ""
            if server_id and not base_url:
                stored = custom_servers.get_server(server_id)
                if stored:
                    base_url = stored.get("base_url") or ""
                    if not api_key:
                        api_key = stored.get("api_key") or ""
            elif server_id and base_url:
                # Both supplied: stamp the stored server only if the probed URL
                # IS its stored URL — otherwise a foreign URL's model list would
                # be persisted onto the record (stamps must match the stored
                # base_url; PATCH clears them on re-point for the same reason).
                # The probe itself still runs; it just doesn't stamp.
                stored = custom_servers.get_server(server_id)
                if not stored or (stored.get("base_url") or "") != base_url.rstrip("/"):
                    server_id = ""
            if not base_url:
                raise HTTPException(status_code=400, detail="missing credential field: base_url")
            result = validators.validate_custom(base_url, api_key)
        else:
            raise HTTPException(status_code=400, detail=f"unknown provider {req.provider}")
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"missing credential field: {e.args[0]}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("validator dispatch failed for provider=%s", req.provider)
        return ValidateResponse(ok=False, latency_ms=0, error=f"{type(e).__name__}: {str(e)[:200]}")
    if result.ok:
        if req.provider == "custom":
            # Per-server stamping: "custom:{server_id}", NOT bare "custom" —
            # one server's validation must not mark all custom servers valid.
            # Ad-hoc probes (no server_id) validate but stamp/persist nothing.
            # server_id was set at the top of the custom dispatch branch (the
            # single source — do not re-derive it from req.credentials here).
            if server_id:
                try:
                    custom_servers.update_server(server_id, {
                        "validated_at": datetime.now(timezone.utc).isoformat(),
                        "last_models": list((result.detail or {}).get("models") or []),
                        # Re-validate = "the server config may have changed":
                        # clear auto-learned per-model windows so a RAISED -c
                        # isn't silently under-budgeted forever (auto-learn only
                        # corrects downward — an under-budget never errors).
                        # Still-smaller windows re-learn on the next overflow.
                        "model_context": {},
                    })
                    _state.record_validation(f"custom:{server_id}")
                except (KeyError, ValueError):
                    logger.warning(
                        "validate custom: could not persist result for server_id=%r",
                        server_id, exc_info=True,
                    )
        else:
            _state.record_validation(req.provider)
    return ValidateResponse(**dataclasses.asdict(result))


@router.post("/save")
def save_secrets(req: SaveRequest) -> dict:
    """Write secrets to .env (atomic + backup). Trusted-client endpoint —
    must remain loopback-only after T1.3.2 first-run middleware lands."""
    logger.info("onboarding /save: keys=%s", list(req.secrets.keys()))
    try:
        return update_env(req.secrets)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/step/complete")
def step_complete(req: StepActionRequest) -> dict:
    _state.mark_step_complete(req.step)
    _advance_current_to_next(req.step)
    return _state.snapshot()


@router.post("/step/skip")
def step_skip(req: StepActionRequest) -> dict:
    _state.mark_step_skipped(req.step)
    _advance_current_to_next(req.step)
    return _state.snapshot()


@router.post("/complete")
def complete() -> dict:
    """Mark onboarding fully complete — sentinel file written."""
    logger.info("onboarding /complete: marking done")
    _state.mark_complete()
    return {"ok": True, "is_complete": True}


@router.post("/reset")
def reset() -> dict:
    """Reset onboarding state (for testing or re-onboarding)."""
    logger.info("onboarding /reset: clearing state")
    _state.reset()
    return _state.snapshot()


# ── Tailscale wizard actuator (T4) ──
from Orchestrator.onboarding import tailscale_actuator as ts_act


class TailscaleUpResponse(BaseModel):
    login_url: str


@router.post("/tailscale/up", response_model=TailscaleUpResponse)
async def tailscale_up():
    """Start `tailscale up` and return login URL for browser launch.

    Reviewer C2: refactored to acquire-then-try/except with a `released`
    flag so future code changes can't introduce a lock-leak vector. On
    the success path the lock STAYS held — /poll releases it when the
    backend transitions to Running. On any failure path the lock is
    released exactly once.
    """
    # Existing-URL fast path (no lock acquisition needed)
    if ts_act._up_lock.locked():
        if ts_act._active_up_login_url:
            return TailscaleUpResponse(login_url=ts_act._active_up_login_url)
        raise HTTPException(status_code=409, detail="up already in progress")

    await ts_act._up_lock.acquire()
    released = False
    try:
        url = await ts_act.start_up()
        return TailscaleUpResponse(login_url=url)
    except RuntimeError as e:
        ts_act._up_lock.release()
        released = True
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        if not released:
            ts_act._up_lock.release()
        raise


@router.get("/tailscale/poll")
async def tailscale_poll():
    """Check authentication progress.

    Reviewer C1: release() wrapped in try/except RuntimeError because two
    concurrent /poll calls can both observe Running and race the
    .locked() check — the second .release() would raise
    `RuntimeError: Lock is not acquired`.
    """
    result = await ts_act.poll_up()
    if result.get("state") == "running":
        try:
            ts_act._up_lock.release()
        except RuntimeError:
            # Already released by a concurrent /poll or /cancel — benign.
            pass
    return result


@router.post("/tailscale/cancel")
async def tailscale_cancel():
    """User aborted auth flow."""
    await ts_act.cancel_up()
    if ts_act._up_lock.locked():
        ts_act._up_lock.release()
    return {"ok": True}


@router.post("/tailscale/cert")
async def tailscale_cert():
    """Request HTTPS cert from Tailscale (M2 — detects HTTPS-disabled state)."""
    return await ts_act.request_cert()


@router.post("/tailscale/accept-dns")
async def tailscale_accept_dns():
    """Set device-side --accept-dns=true (idempotent). Tailnet-level MagicDNS
    toggle is separate — see UI banner (M3 / I4 deep-link)."""
    return await ts_act.set_accept_dns()


from fastapi.responses import StreamingResponse

@router.post("/tailscale/install/stream")
async def tailscale_install_stream():
    """SSE stream of Tailscale install progress (E1-reversal: re-uses apt
    repo configured by install.sh Step 1b). 409 if already in progress."""
    try:
        async def gen():
            async with ts_act.operation_lock(ts_act._install_lock, "install"):
                async for chunk in ts_act.stream_install():
                    yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/tailscale/serve")
async def tailscale_serve_setup():
    """Set up Tailscale HTTPS reverse proxy on :443 → http://localhost:9091.
    Replaces v1.1-deferred uvicorn HTTPS plan with Tailscale-handled
    HTTPS termination. Android app pairing requires this."""
    return await ts_act.setup_serve()


# ── E9 (Brandon's MSO2 Ultra testing 2026-05-17): status-aware Restart
#   Service button. E8 fixed the wizard's DISPLAY layer (/current-config
#   reads .env fresh), but chat handlers still hold stale
#   Orchestrator.config.* module-level constants until service restart.
#   Customer adds keys via wizard, sees them displayed, then chat fails.
#   These two endpoints back the done-step's status-aware restart button:
#   /restart-status detects drift, /restart triggers the restart. ──

class RestartStatusResponse(BaseModel):
    needs_restart: bool
    drifted_keys: list[str]
    reason: str | None


@router.get("/restart-status", response_model=RestartStatusResponse)
def restart_status() -> RestartStatusResponse:
    """Detect whether the running process's in-memory config has drifted from
    the .env file on disk. If yes, the customer changed settings (typically
    API keys) via the wizard that the running service hasn't picked up — chat
    handlers will use stale values until restart. Wizard's done step uses
    this to decide whether to surface the 'Restart Service' button as
    actionable vs passive 'up to date'.

    E8 follow-up — pairs with the /current-config fresh-read fix."""
    from dotenv import dotenv_values
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    from Orchestrator import config as cfg

    env = dotenv_values(str(ENV_FILE))
    # Keys whose stale-constant-vs-fresh-disk-value mismatch is customer-visible.
    # E11 followup (Brandon's MSO2 Ultra 2026-05-17): expanded to cover ALL
    # onboarding-writable env vars — JSON service account + Gmail OAuth in
    # addition to the original 5 API keys + Tailscale hostname.
    checks = {
        "OPENAI_API_KEY": cfg.OPENAI_API_KEY,
        "ANTHROPIC_API_KEY": cfg.ANTHROPIC_API_KEY,
        "GOOGLE_API_KEY": cfg.GOOGLE_API_KEY,
        "XAI_API_KEY": cfg.XAI_API_KEY,
        "PERPLEXITY_API_KEY": cfg.PERPLEXITY_API_KEY,
        "BLACKBOX_TAILNET_HOSTNAME": cfg.BLACKBOX_TAILNET_HOSTNAME,
        "GOOGLE_APPLICATION_CREDENTIALS": getattr(cfg, "GOOGLE_APPLICATION_CREDENTIALS", "") or "",
        "GOOGLE_OAUTH_CLIENT_ID": getattr(cfg, "GOOGLE_OAUTH_CLIENT_ID", "") or "",
        "GOOGLE_OAUTH_CLIENT_SECRET": getattr(cfg, "GOOGLE_OAUTH_CLIENT_SECRET", "") or "",
    }
    drifted = []
    for key, running_val in checks.items():
        disk_val = env.get(key, "") or ""
        if (running_val or "") != disk_val:
            drifted.append(key)

    if not drifted:
        return RestartStatusResponse(
            needs_restart=False, drifted_keys=[],
            reason=None,
        )
    return RestartStatusResponse(
        needs_restart=True, drifted_keys=drifted,
        reason=f"{len(drifted)} setting(s) changed since service start: {', '.join(drifted)}",
    )


# ── M1: hub status rollup. FAST read — persisted data only, ZERO probes.
#   (live re-validation lives in GET /onboarding/status/stream below.) ──
from Orchestrator.onboarding import status_rollup


def _collect_status_inputs() -> dict:
    """Cheap, probe-free snapshots for status_rollup.build_status.

    .env is read fresh (E8: dotenv_values, no os.environ pollution). The
    capability dicts reuse the SAME availability gate current_config uses, so
    the enabled set is derived in ONE place. NO subprocess / provider / tailscale
    calls happen here — tailscale state is derived from persisted hints only.
    """
    from dotenv import dotenv_values
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    env = dict(dotenv_values(str(ENV_FILE)))
    snap = _state.snapshot()
    state = {
        "completed_steps": snap["completed_steps"],
        "skipped_steps": snap["skipped_steps"],
        "validated_at": _state.validated_at(),
    }

    # Operators (module-level list; fail-soft to [])
    try:
        from Orchestrator.routes import admin_routes
        operators = list(admin_routes.USERS_LIST)
    except Exception:
        operators = []

    # Paired devices (fail-soft to [])
    try:
        from Orchestrator.routes.pairing_routes import list_paired_devices
        paired = list_paired_devices()
    except Exception:
        paired = []

    # web_search / image — reuse availability gate (DRY with current_config)
    from Orchestrator.toolvault.availability import (
        enabled_web_search_providers, enabled_providers, PROVIDER_ENV, FEATURES,
    )
    ws_enabled = enabled_web_search_providers()
    web_search = {
        "enabled": sorted(ws_enabled),
        "providers": {
            prov: {"key_present": (True if var is None else bool(env.get(var))),
                   "enabled": prov in ws_enabled}
            for prov, var in PROVIDER_ENV.items()
        },
        "default": (env.get("WEB_SEARCH_DEFAULT") or "").strip(),
    }
    img_enabled = enabled_providers("image")
    image = {
        "enabled": sorted(img_enabled),
        "providers": {
            prov: {"key_present": bool(env.get(var)), "enabled": prov in img_enabled}
            for prov, var in FEATURES["image"]["provider_env"].items()
        },
        "default": (env.get("IMAGE_DEFAULT") or "").strip(),
    }

    # embeddings — read-only status dict (no probe side effects). Fail-soft.
    try:
        from Orchestrator.routes.embeddings_routes import embeddings_status
        from fastapi import Response as _Resp
        embeddings = embeddings_status(_Resp())
    except Exception:
        logger.exception("status rollup: embeddings_status failed")
        embeddings = {"active": None, "health": {"state": "ok"}, "stores": [], "models": []}

    # reranker (M13) — additive block for the wizard payload. Fail-soft, and
    # bounded like the embeddings rollup: the reachability probe inside
    # rerank.status() is ~1s-capped + TTL-cached, the hardware probe 60s-cached,
    # and the latency preflight fires at most once per process (and only when
    # [rerank] actually has a live provider).
    try:
        from Orchestrator import rerank as _rerank
        rerank_block = _rerank.status()
    except Exception:
        logger.exception("status rollup: rerank status failed")
        rerank_block = None

    # cli agents — installed/auth markers (filesystem only, no spawn). Fail-soft.
    try:
        cli = cli_agent_status()
    except Exception:
        logger.exception("status rollup: cli_agent_status failed")
        cli = {"providers": {}, "ready": False}

    restart = restart_status().model_dump()

    # MCP remote server -- cheap token-store presence only (no subprocess/probe
    # here; live mcp_up/funnel/oauth refinement happens in the SSE stream).
    try:
        from Orchestrator.routes import mcp_routes as _mcp
        mcp = {"tokens_present": bool(_mcp._load_tokens())}
    except Exception:
        mcp = {"tokens_present": False}

    # Custom model servers (provider 'custom') -- REDACTED records: redact()
    # preserves everything the rollup needs (id/alias/enabled/validated_at)
    # while making api_key leakage structurally impossible on both wire-bound
    # paths; _derive_api_keys field-picks as the second layer. Cheap local
    # JSON read; fail-soft to [].
    try:
        custom = custom_servers.list_servers_redacted()
    except Exception:
        logger.exception("status rollup: custom_servers read failed")
        custom = []

    return dict(
        env=env, state=state, embeddings=embeddings, cli=cli,
        web_search=web_search, image=image, paired=paired, operators=operators,
        restart=restart, mcp=mcp, rerank=rerank_block, custom_servers=custom,
        is_complete=_state.is_complete(),
    )


@router.get("/status")
def onboarding_status() -> dict:
    """Fast persisted rollup for the hub. State derived from PERSISTED data only
    — no provider/tailscale/subprocess probe. Live re-validation is GET
    /onboarding/status/stream."""
    return status_rollup.build_status(**_collect_status_inputs())


@router.get("/status/stream")
async def onboarding_status_stream():
    """SSE live re-validation for the hub (fired on view, never on a timer).

    Emits one `event: section` per section as its probe resolves, then a final
    `event: done`. Reuses the StreamingResponse SSE pattern from logs_stream /
    tailscale_install_stream. The ONLY section needing a live probe today is
    tailscale (serve/HTTPS state); the rest re-emit their persisted-derived
    state. A probe failure falls back to the persisted state — the stream never
    crashes over one bad section."""
    import asyncio
    import json as _json

    def _sse(event: str, payload: dict) -> bytes:
        return f"event: {event}\ndata: {_json.dumps(payload)}\n\n".encode("utf-8")

    async def gen():
        # Start from the fast rollup (persisted truth) so every section has a
        # baseline even if its live probe is a no-op.
        base = status_rollup.build_status(**_collect_status_inputs())
        sections = {s["key"]: s for s in base["sections"]}
        ready_count = 0

        for section in status_rollup.SECTIONS:
            key = section["key"]
            sec = sections[key]
            atts = [a for a in base["attention"] if a.get("section") == key]

            if key == "tailscale":
                # The one genuinely-live probe — refine serve/HTTPS truth.
                try:
                    from Orchestrator.onboarding.validators import validate_tailscale
                    res = await asyncio.to_thread(validate_tailscale)
                    if not res.ok:
                        sec = {**sec, "state": status_rollup.OPTIONAL,
                               "summary": "Not connected"}
                        atts = []
                    # res.ok refines nothing beyond the persisted serve hint here;
                    # the fast-read hint already classified serve-not-set.
                except Exception:
                    logger.exception("status stream: tailscale probe failed")

            elif key == "mcp":
                try:
                    from Orchestrator.routes import mcp_routes as _mcp
                    live = {
                        "tokens_present": bool(_mcp._load_tokens()),
                        "mcp_up": await asyncio.to_thread(_mcp._mcp_up),
                        "funnel_up": await asyncio.to_thread(_mcp._funnel_up),
                        "oauth_ready": await asyncio.to_thread(_mcp._oauth_ready),
                    }
                    st, summary, _it, atts2 = status_rollup._derive_mcp(live)
                    sec = {**sec, "state": st, "summary": summary}
                    atts = [{"section": "mcp", "severity": a["severity"],
                             "message": a["message"], "cta_step": "mcp"} for a in atts2]
                except Exception:
                    logger.exception("status stream: mcp probe failed")

            if sec["state"] == status_rollup.READY:
                ready_count += 1
            yield _sse("section", {
                "key": key, "state": sec["state"], "summary": sec["summary"],
                "attention": atts,
            })
            await asyncio.sleep(0)  # cooperative yield between probes

        yield _sse("done", {"ready_count": ready_count, "total": len(status_rollup.SECTIONS)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/restart")
async def restart_blackbox_service() -> dict:
    """Trigger a service restart. Wizard's done step calls this after the
    customer clicks the 'Restart Service' button. Sudoers grant from T2 +
    this addition allows passwordless systemctl restart blackbox.service.

    Fire-and-forget — the restart kills THIS process so the HTTP response
    may not actually be returned. Wizard JS handles by polling /health
    after a short delay to detect when the service comes back."""
    import subprocess
    logger.info("restart: customer triggered service restart from wizard")
    # Use Popen so we don't await — the restart will SIGTERM us mid-Popen.wait
    subprocess.Popen(
        ["sudo", "-n", "/usr/bin/systemctl", "restart", "blackbox.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "message": "restart triggered — service will be back in ~60-90s"}


@router.get("/cli-agent/spawn-claude-bare-pty")
def cli_agent_spawn_claude_bare_pty() -> dict:
    """Spawn claude via a direct python pty.fork, NO tmux involved.
    Captures stdout for 10 seconds, then kills. Tells us whether claude's
    TUI renders when isolated from our tmux wrapper. If text appears here
    but the modal pane stays blank, the bug is in tmux/session_manager.
    If text also doesn't appear here, the bug is even deeper (claude +
    PTY combo on this machine).
    """
    import pty, select, fcntl, struct, termios, signal, time
    from Orchestrator.routes.cli_agent_routes import provider_bin
    from Orchestrator.cli_agent.path_extension import extended_path_dirs

    claude_bin = provider_bin("claude")
    if not claude_bin:
        return {"error": "claude binary not resolvable"}

    aug_path = os.pathsep.join([os.environ.get("PATH", ""), *extended_path_dirs()])
    # Mirror the env session_manager passes for claude AFTER our scope fix:
    # no DISPLAY, no BROWSER, just keyring + TERM + PATH.
    env = {
        "TERM": "xterm-256color",
        "PATH": aug_path,
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    uid = os.getuid()
    if os.path.isdir(f"/run/user/{uid}"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        if os.path.exists(f"/run/user/{uid}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"

    pid, fd = pty.fork()
    if pid == 0:
        # Child: replace this process with claude
        # Set window size first
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 30, 120, 0, 0))
        except Exception:
            pass
        try:
            os.execvpe(claude_bin, [claude_bin], env)
        except Exception as e:
            os.write(2, f"execvpe failed: {e}\n".encode())
            os._exit(1)

    captured = bytearray()
    deadline = time.time() + 10
    trace: list = []
    answered_da1 = False
    answered_xtversion = False
    try:
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                data = os.read(fd, 8192)
                if not data:
                    trace.append(f"t={time.time()-deadline+10:.1f}s read returned 0 bytes (EOF)")
                    break
                captured.extend(data)
                trace.append(f"t={time.time()-deadline+10:.1f}s read {len(data)} bytes, total={len(captured)}")
                if not answered_da1 and b"\x1b[c" in data:
                    resp = b"\x1b[?64;1;2;6;9;15;18;21;22c"
                    n = os.write(fd, resp)
                    trace.append(f"  -> answered DA1 with {len(resp)} bytes, wrote {n}")
                    answered_da1 = True
                if not answered_xtversion and b"\x1b[>0q" in data:
                    resp = b"\x1bP>|XTerm(372)\x1b\\"
                    n = os.write(fd, resp)
                    trace.append(f"  -> answered XTVERSION with {len(resp)} bytes, wrote {n}")
                    answered_xtversion = True
            except OSError as e:
                trace.append(f"  read errored: {e}")
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    decoded = captured.decode("utf-8", errors="replace")
    import re
    # Strip ANSI for readability + compress blank lines
    cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r", "", decoded)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return {
        "claude_bin": claude_bin,
        "captured_bytes": len(captured),
        "cleaned_text": cleaned[:3500],
        "raw_preview": decoded[:600],
        "answered_da1": answered_da1,
        "answered_xtversion": answered_xtversion,
        "trace": trace,
    }


@router.post("/cli-agent/clear-claude-cloud-auth-cache")
def cli_agent_clear_claude_cloud_auth_cache() -> dict:
    """Quarantine ~/.claude/mcp-needs-auth-cache.json. Claude Code's TUI
    reads this file at startup and tries to refresh OAuth tokens for any
    listed Anthropic-hosted MCP connectors (claude.ai Gmail/Calendar/
    Drive etc). The refresh hangs silently inside our PTY/tmux when
    the OAuth dance can't reach a real browser in time, leaving the
    pane blank forever (Brandon's MSO2 symptom 2026-05-23). Renaming
    the file to .bak makes claude treat connectors as fresh — no
    refresh attempted, TUI renders normally.

    Idempotent: if the file is already absent, returns moved=False.
    """
    import time
    cache = os.path.expanduser("~/.claude/mcp-needs-auth-cache.json")
    if not os.path.exists(cache):
        return {"moved": False, "reason": "file not present"}
    bak = f"{cache}.bak.{int(time.time())}"
    try:
        os.rename(cache, bak)
        return {"moved": True, "from": cache, "to": bak}
    except OSError as e:
        return {"moved": False, "error": str(e)}


@router.post("/cli-agent/reset-tmux")
def cli_agent_reset_tmux() -> dict:
    """Kill the cli-agent tmux server entirely. Next spawn creates a
    fresh one from scratch — clears any cached tmux options / window-
    level state / accumulated server-level env that survived service
    restarts (blackbox.service has KillMode=process specifically to
    preserve tmux across service restarts; that's normally good, but
    can pin bad state).

    Brandon asked for this 2026-05-23 after multiple commits failed to
    move the "Claude blank in Portal modal" symptom. Worth trying as a
    blanket reset before any more surgical investigation.
    """
    import subprocess
    out: dict = {}
    # List sessions before kill
    pre = subprocess.run(
        ["tmux", "list-sessions"], capture_output=True, text=True, timeout=3,
    )
    out["sessions_before"] = pre.stdout.strip().splitlines() if pre.returncode == 0 else []
    out["pre_stderr"] = pre.stderr.strip() if pre.stderr else None
    # Kill the server
    kill = subprocess.run(
        ["tmux", "kill-server"], capture_output=True, text=True, timeout=5,
    )
    out["kill_rc"] = kill.returncode
    out["kill_stderr"] = kill.stderr.strip() if kill.stderr else None
    # Confirm
    post = subprocess.run(
        ["tmux", "list-sessions"], capture_output=True, text=True, timeout=3,
    )
    out["sessions_after"] = post.stdout.strip().splitlines() if post.returncode == 0 else []
    out["post_stderr"] = post.stderr.strip() if post.stderr else None
    return out


@router.get("/cli-agent/claude-doctor")
def cli_agent_claude_doctor() -> dict:
    """Run a battery of quick diagnostic commands against the user's
    claude install — version, MCP server list, config file existence,
    a 3-second pty-less spawn to capture stderr. Used to debug
    'modal is blank when launching claude' bugs on remote machines
    where we can't SSH in to inspect claude's actual behavior.
    """
    import subprocess
    from Orchestrator.cli_agent.path_extension import extended_path_dirs

    out: dict = {}

    # Resolve claude binary (same logic the bridge uses)
    from Orchestrator.routes.cli_agent_routes import provider_bin
    claude_bin = provider_bin("claude")
    out["claude_bin"] = claude_bin
    if not claude_bin:
        out["error"] = "claude binary not resolvable"
        return out

    # Build same augmented PATH as the bridge would
    aug_path = os.pathsep.join([os.environ.get("PATH", ""), *extended_path_dirs()])
    env = {**os.environ, "PATH": aug_path}

    def run(cmd: list[str], timeout: float = 4.0) -> dict:
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                               timeout=timeout)
            return {
                "rc": r.returncode,
                "stdout": r.stdout[-1500:],
                "stderr": r.stderr[-1500:],
            }
        except subprocess.TimeoutExpired as e:
            return {
                "rc": "TIMEOUT",
                "stdout": (e.stdout or b"").decode(errors="replace")[-1500:],
                "stderr": (e.stderr or b"").decode(errors="replace")[-1500:],
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    out["version"] = run([claude_bin, "--version"])
    out["mcp_list"] = run([claude_bin, "mcp", "list"])

    # Check the claude config directory
    cfg_dir = os.path.expanduser("~/.claude")
    out["config_dir_exists"] = os.path.isdir(cfg_dir)
    if out["config_dir_exists"]:
        try:
            out["config_dir_listing"] = sorted(os.listdir(cfg_dir))[:30]
        except OSError as e:
            out["config_dir_listing"] = f"ERROR: {e}"

    # Check MCP venv path that install.sh registered
    blackbox_root = os.environ.get("BLACKBOX_ROOT", "")
    if not blackbox_root:
        # Fall back to deriving from this file's path
        blackbox_root = str(Path(__file__).resolve().parents[2])
    mcp_py = os.path.join(blackbox_root, "MCP/venv/bin/python")
    mcp_server = os.path.join(blackbox_root, "MCP/blackbox_mcp_server.py")
    out["mcp_py_path"] = mcp_py
    out["mcp_py_exists"] = os.path.isfile(mcp_py)
    out["mcp_server_path"] = mcp_server
    out["mcp_server_exists"] = os.path.isfile(mcp_server)

    # tmux + node versions — useful for cross-machine compare
    out["tmux_version"] = run(["tmux", "-V"], timeout=2.0)
    out["node_version"] = run([os.path.join(os.path.dirname(claude_bin), "node"), "--version"], timeout=2.0)

    # Test claude in --print mode (non-interactive, no TUI). If this hangs
    # or errors, claude itself is broken irrespective of our PTY/tmux setup.
    # If it succeeds, the bug is in the interactive/TUI layer.
    print_env = {**env, "TERM": "xterm-256color"}
    out["print_mode_test"] = run(
        [claude_bin, "--print", "say hi in 3 words then stop"],
        timeout=20.0,
    )

    # claude with --debug: claude writes diagnostic info to stderr about
    # what it's loading/connecting to at startup. Captures startup chatter
    # so we can see WHY the TUI hangs. Use --print to keep this from
    # blocking forever; we just want the startup phase logs.
    out["debug_startup_test"] = run(
        [claude_bin, "--debug", "--print", "hi"],
        timeout=20.0,
    )

    # Check the spawn-env signals that influence claude TUI behavior
    out["display_var_in_orchestrator"] = os.environ.get("DISPLAY", "(unset)")
    out["x11_socket_visible"] = os.path.exists("/tmp/.X11-unix/X0")
    uid = os.getuid()
    out["wayland_socket_visible"] = os.path.exists(f"/run/user/{uid}/wayland-0")

    # Dump small config files that might explain a hang
    for fname in ("settings.json", "policy-limits.json", "mcp-needs-auth-cache.json"):
        p = os.path.join(cfg_dir, fname)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    out[f"config_{fname}"] = f.read()[:4000]
            except OSError as e:
                out[f"config_{fname}"] = f"ERROR: {e}"

    # Check for debug log dir (claude writes to ~/.claude/debug when set up)
    debug_dir = os.path.join(cfg_dir, "debug")
    if os.path.isdir(debug_dir):
        try:
            files = sorted(os.listdir(debug_dir))[-5:]
            out["debug_dir_recent"] = files
        except OSError:
            pass

    return out


@router.get("/cli-agent/pane-content")
def cli_agent_pane_content(session_id: str) -> dict:
    """Diagnostic: capture the actual rendered content of a CLI agent
    tmux pane via `tmux capture-pane -J -p`. Plus list the running
    processes inside the pane via tmux's pane_pid + ps walk. Lets us
    see what the user is actually looking at when they say 'the modal
    is blank' — was claude's UI ever drawn? Did it exit? Is it stuck
    at a hidden prompt?
    """
    import subprocess
    if not session_id.startswith("cli-agent-"):
        return {"error": "invalid session_id"}
    # Capture pane content as it would render
    cap = subprocess.run(
        ["tmux", "capture-pane", "-J", "-p", "-t", session_id],
        capture_output=True, text=True, timeout=4,
    )
    # Also capture with escape sequences + alt-screen + scrollback
    cap_alt = subprocess.run(
        ["tmux", "capture-pane", "-J", "-p", "-e", "-S", "-200", "-t", session_id],
        capture_output=True, text=True, timeout=4,
    )
    # Get pane pid + walk children
    pid_proc = subprocess.run(
        ["tmux", "list-panes", "-t", session_id, "-F", "#{pane_pid}"],
        capture_output=True, text=True, timeout=4,
    )
    pane_pid = pid_proc.stdout.strip()
    proc_tree = ""
    if pane_pid:
        try:
            ps = subprocess.run(
                ["ps", "--ppid", pane_pid, "-o", "pid,cmd", "--forest"],
                capture_output=True, text=True, timeout=4,
            )
            proc_tree = ps.stdout
            # Also include the pane_pid itself
            ps_self = subprocess.run(
                ["ps", "-p", pane_pid, "-o", "pid,cmd"],
                capture_output=True, text=True, timeout=4,
            )
            proc_tree = ps_self.stdout + "\n--- children ---\n" + proc_tree
        except Exception as e:
            proc_tree = f"ps error: {e}"
    # Show env of pane process so we can see what claude actually inherits
    pane_env = ""
    if pane_pid:
        try:
            with open(f"/proc/{pane_pid}/environ", "rb") as f:
                raw_env = f.read().replace(b"\x00", b"\n").decode("utf-8", errors="replace")
                # Filter to keys we care about
                interesting = []
                for line in raw_env.split("\n"):
                    if any(line.startswith(p) for p in ("PATH=", "TERM=", "BROWSER=", "DISPLAY=", "WAYLAND_", "LANG=", "LC_", "HOME=", "USER=", "PWD=", "CLAUDE", "NODE_", "DBUS_", "XDG_")):
                        interesting.append(line)
                pane_env = "\n".join(interesting)
        except OSError as e:
            pane_env = f"err: {e}"
    return {
        "session_id": session_id,
        "pane_pid": pane_pid,
        "pane_content_raw": cap.stdout,
        "pane_content_lines": cap.stdout.splitlines(),
        "pane_content_with_escapes": cap_alt.stdout,
        "pane_env_interesting": pane_env,
        "proc_tree": proc_tree,
        "capture_stderr": cap.stderr.strip() if cap.stderr.strip() else None,
    }


@router.get("/cli-agent/url-handlers")
def url_handler_status() -> dict:
    """Return the current xdg-mime default for HTTP(S) and text/html so
    we can verify whether the startup-hook assertion (in startup.py)
    actually persisted. If these return something OTHER than chromium-
    browser.desktop, agy's native URL-open call routes there directly
    and our PATH shim doesn't help.
    """
    import subprocess
    out = {}
    for scheme in ("x-scheme-handler/https", "x-scheme-handler/http", "text/html"):
        try:
            r = subprocess.run(
                ["xdg-mime", "query", "default", scheme],
                capture_output=True, text=True, timeout=4,
            )
            out[scheme] = r.stdout.strip() if r.returncode == 0 else f"ERROR rc={r.returncode}"
        except (subprocess.TimeoutExpired, OSError) as e:
            out[scheme] = f"EXC {e!r}"
    # Also check whether the chromium .desktop file actually exists
    chromium_paths = []
    for d in ("/usr/share/applications", "/var/lib/snapd/desktop/applications"):
        for desktop in ("chromium-browser.desktop", "chromium.desktop", "google-chrome.desktop"):
            p = os.path.join(d, desktop)
            if os.path.isfile(p):
                chromium_paths.append(p)
    out["chromium_desktop_files"] = chromium_paths
    return out


@router.get("/cli-agent/xdg-open-log")
def xdg_open_shim_log(tail: int = 100) -> dict:
    """Return the last N lines of /tmp/blackbox-xdg-open.log so we can
    verify whether the xdg-open / gio shims (Orchestrator/cli_agent/
    path_shims/) are actually being reached when a CLI agent fires
    OAuth. Diagnostic-only endpoint for remote MSO2 debugging — the
    log is silent in normal operation, only the diagnostic-enhanced
    shim writes to it.

    No PII risk: the log contains argv (URLs), parent process cmdline,
    and a few env vars (PATH, BROWSER, DISPLAY). These are exactly what
    we already log to journalctl during normal operation.
    """
    log_path = "/tmp/blackbox-xdg-open.log"
    if not os.path.exists(log_path):
        return {"exists": False, "lines": []}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        safe_tail = max(1, min(int(tail), 1000))
        return {
            "exists": True,
            "total_lines": len(all_lines),
            "lines": [l.rstrip("\n") for l in all_lines[-safe_tail:]],
        }
    except OSError as e:
        return {"exists": True, "error": str(e), "lines": []}


@router.get("/logs/stream")
async def logs_stream(lines: int = 200):
    """Stream blackbox.service logs as Server-Sent Events for the wizard's
    'View Logs' modal. Initial backfill of N lines + follow forward.

    The journalctl -u blackbox.service * sudoers grant allows passwordless
    invocation. The lines parameter is bounded server-side (max 1000) to
    prevent runaway backfill on a long-running service.

    E10 (Brandon's MSO2 Ultra design 2026-05-17): pairs with E9's Restart
    Service button on the done step. Advanced users + customer-support
    scenarios need live log visibility for diagnosis."""
    import asyncio

    safe_lines = max(10, min(int(lines), 1000))

    async def gen():
        # Initial backfill: last N lines, then follow forward
        cmd = [
            "sudo", "-n", "/usr/bin/journalctl",
            "-u", "blackbox.service",
            "--lines", str(safe_lines),
            "--no-pager",
            "--output", "short-iso",
            "--follow",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            yield b"event: start\ndata: streaming logs\n\n"
            while True:
                line = await proc.stdout.readline()
                if not line:
                    # journalctl --follow exited (rare)
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                # SSE format requires data: prefix per line; escape any embedded
                # newlines (shouldn't happen for journalctl single-line records).
                yield f"data: {text}\n\n".encode("utf-8")
        except asyncio.CancelledError:
            # Client disconnected (modal closed) — terminate journalctl
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            raise
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── E7 final (Brandon's MSO2 Ultra testing 2026-05-16): backend-spawned
#   browser open. Tauri's on_navigation doesn't fire for target=_blank,
#   xdg-open delegates to broken gio, Tauri shell can't reliably spawn
#   firefox from its env-stripped webview. Solution: backend (which runs
#   as bbx user with full filesystem access to /run/user/<uid>/) spawns
#   firefox directly with the proper user-session env reconstructed from
#   the UID. Wizard JS POSTs to this endpoint when it intercepts a
#   target=_blank click. Works because subprocess.Popen with explicit env
#   bypasses the inherited-from-systemd env stripping. ──

class OpenUrlRequest(BaseModel):
    url: str


@router.post("/open-url")
async def open_external_url(req: OpenUrlRequest) -> dict:
    """Open a URL in the user's browser. Uses XDG Desktop Portal as the
    canonical cross-session URI dispatch mechanism (org.freedesktop.portal
    .OpenURI). System services like blackbox.service run in a separate
    systemd cgroup/namespace from the user's GNOME session — directly
    spawning firefox doesn't render because snap confinement + GUI
    session integration require user-session-managed processes. The
    portal IS in the user session and handles the handoff. Falls back
    to direct firefox spawn (with stderr capture for diagnostics) if
    portal is unavailable.

    Wired up by Portal/onboarding/onboarding.js's document-level click
    handler intercepting <a target=_blank> clicks."""
    import subprocess
    import glob

    url = req.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="only http(s):// URLs allowed")

    uid = os.getuid()
    env = os.environ.copy()
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    env.setdefault("DISPLAY", ":0")
    if "XAUTHORITY" not in env:
        candidates = (
            glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*")
            + [f"/run/user/{uid}/gdm/Xauthority"]
        )
        for cand in candidates:
            if os.path.exists(cand):
                env["XAUTHORITY"] = cand
                break

    # ATTEMPT 1+2: direct browser spawn (chromium then firefox). Reordered to
    # front per Brandon 2026-05-22: xdg-portal as first attempt was fragile
    # because portal dispatches to the system DEFAULT handler — on MSO2 that
    # default keeps reverting to empty (cause unknown — possibly snap refresh,
    # possibly GNOME initial-setup state), so portal "succeeded" but the URL
    # opened in gnome-text-editor instead of a browser. Direct binary spawn
    # bypasses xdg-settings entirely: if the binary is on PATH, it launches.
    #
    # Try chromium first (deterministic launch — apt-installed binary handles
    # snap confinement via the transitional wrapper at /usr/bin/chromium-browser
    # on Ubuntu 24.04). Firefox second (also works, but snap firefox can be
    # slower to first-launch). Portal/gio kept as last-resort fallbacks for
    # systems without either binary on PATH.
    for browser_name in ("chromium-browser", "chromium", "firefox", "google-chrome"):
        browser_bin = shutil.which(browser_name)
        if not browser_bin:
            continue
        logger.info("open-url: trying direct spawn of %s for %s", browser_name, url)
        try:
            # Detached spawn — start_new_session so it doesn't die when the
            # request handler returns. If still alive after 4s, treat as
            # success (browser stayed up). subprocess.run with timeout is
            # the simplest way to detect "exited immediately = error" vs
            # "still running = launched ok".
            result = subprocess.run(
                [browser_bin, url],
                env=env, capture_output=True, text=True, timeout=4,
                start_new_session=True,
            )
            # Exited within 4s = failure (browser should keep running)
            logger.warning("open-url: %s exited rc=%d stderr=%r",
                           browser_name, result.returncode, result.stderr[:300])
        except subprocess.TimeoutExpired:
            # Still running after 4s = success
            logger.info("open-url: %s SUCCESS (still running after 4s)", browser_name)
            return {"ok": True, "via": f"direct-{browser_name}"}
        except Exception as e:
            logger.warning("open-url: %s spawn errored: %r", browser_name, e)

    # ATTEMPT 3: gio launch firefox snap .desktop (snap-session-aware launch)
    firefox_desktop = "/var/lib/snapd/desktop/applications/firefox_firefox.desktop"
    if os.path.exists(firefox_desktop):
        logger.info("open-url: trying gio launch firefox snap for %s", url)
        try:
            gio = subprocess.run(
                ["gio", "launch", firefox_desktop, url],
                env=env, capture_output=True, text=True, timeout=10,
            )
            if gio.returncode == 0:
                logger.info("open-url: gio launch SUCCESS")
                return {"ok": True, "via": "gio-firefox-snap"}
            logger.warning("open-url: gio launch failed rc=%d stderr=%r",
                           gio.returncode, gio.stderr.strip()[:300])
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("open-url: gio launch errored: %r", e)

    # ATTEMPT 4: XDG Desktop Portal (LAST RESORT — depends on system default
    # handler being set correctly; may route to text editor if not)
    logger.info("open-url: falling back to xdg-portal for %s", url)
    try:
        portal = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.OpenURI.OpenURI",
             "", url, "{}"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        if portal.returncode == 0:
            logger.info("open-url: portal accepted (may dispatch to text editor if default browser unset)")
            return {"ok": True, "via": "portal-fallback",
                    "warning": "dispatched via xdg-portal; depends on system default browser being set"}
        logger.warning("open-url: portal failed rc=%d stderr=%r",
                       portal.returncode, portal.stderr.strip()[:300])
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("open-url: portal call errored: %r", e)

    logger.error("open-url: all methods exhausted (no browser binary on PATH, no firefox snap, portal failed)")
    return {"ok": False, "error": "no browser available — install chromium-browser or firefox"}


# ── E20b (Brandon 2026-05-17): CLI agent wizard step ─────────────────
# After phone pairing, the wizard verifies the three CLI providers
# (Anthropic Claude, Google Gemini, OpenAI Codex) are installed AND
# authenticated. Install.sh Step 1c npm-installs the binaries at boot,
# but each provider requires a one-time interactive auth (Anthropic
# Console / Google OAuth / OpenAI auth). Backend exposes:
#   GET /onboarding/cli-agent/status  → per-provider {installed, auth}
#   POST /onboarding/cli-agent/spawn-terminal  → opens gnome-terminal
#     in the user session running the install OR auth command for the
#     requested provider. Same portal→gio→direct fallback chain as
#     /open-url because the same cross-cgroup process-spawning problem
#     applies: blackbox.service runs in a different systemd namespace
#     from the user's GNOME session and direct subprocess.Popen of a
#     GUI app fails silently.

# Per-provider auth markers. Each CLI writes credentials to a
# well-known location on first successful login; presence of that
# path = authenticated. Heuristic, not authoritative — a malformed
# config file would still show "auth ok" here, but the user can
# always re-run "Sign in" if the CLI complains later.
#
# Antigravity is an exception: it stores credentials in the OS
# keyring (Linux secret-service), so there is no file to check.
# Empty list signals to the status builder that auth state is
# unknown/unobservable; the response then sets
# `authenticated: None` (per D2b of 2026-05-22 plan) and the wizard
# UI renders "Click Launch to sign in" instead of an auth check.
_AUTH_MARKERS = {
    "claude":      [".claude/.credentials.json", ".claude/auth.json", ".claude/config.json"],
    "gemini":      [".gemini/oauth_creds.json", ".gemini/google_account_id", ".gemini/settings.json"],
    "codex":       [".codex/auth.json", ".codex/config.toml"],
    "antigravity": [],  # OS keyring — no file to check (D2b)
}

# Per-provider install commands. Generalized 2026-05-22 from the
# previous `_CLI_INSTALL_PKGS` dict (provider → npm package name)
# to `INSTALL_COMMANDS` (provider → full subprocess.run arg list).
# The 3 npm-based providers keep equivalent behavior:
# `subprocess.run(["npm", "install", "-g", <pkg>])`. Antigravity
# uses a curl-piped shell installer, hence the `bash -c` wrapper.
# This is what install.sh's Step 1c runs for each provider; we
# expose it as a manual rescue if the user blew away their
# node_modules / agy binary or upgraded mid-flow.
INSTALL_COMMANDS: dict[str, list[str]] = {
    "claude":      ["npm", "install", "-g", "@anthropic-ai/claude-code"],
    "gemini":      ["npm", "install", "-g", "@google/gemini-cli"],
    "codex":       ["npm", "install", "-g", "@openai/codex"],
    "antigravity": ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"],
}

# Per-provider commands. Auth command launches the provider's
# interactive login flow.
#
# Antigravity has no separate login command — auth triggers
# implicitly on the first interactive `agy` launch via the OS
# keyring. `None` signals to the wizard (Track 2) to render a
# "Launch & Sign In" button instead of "Login" for this provider.
# (D3b of 2026-05-22 plan.)
_CLI_AUTH_CMD: dict[str, str | None] = {
    # Claude prompts on first interactive launch; the /login slash command
    # forces the flow even if a stale ANTHROPIC_API_KEY is set.
    "claude":      "claude",
    # Gemini auto-prompts for Google OAuth on first interactive launch.
    "gemini":      "gemini",
    # Codex has an explicit login subcommand.
    "codex":       "codex login",
    # Antigravity: no separate command. See note above.
    "antigravity": None,
}


def _build_user_env() -> dict:
    """Reconstruct the user-session env needed to spawn GUI processes
    in the GNOME session from blackbox.service's systemd cgroup.
    Mirrors the env-rebuild done in /open-url; factored here so the
    /spawn-terminal endpoint can reuse it without duplicating.
    """
    import glob
    uid = os.getuid()
    env = os.environ.copy()
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    env.setdefault("DISPLAY", ":0")
    if "XAUTHORITY" not in env:
        candidates = (
            glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*")
            + [f"/run/user/{uid}/gdm/Xauthority"]
        )
        for cand in candidates:
            if os.path.exists(cand):
                env["XAUTHORITY"] = cand
                break
    return env


@router.get("/cli-agent/status")
def cli_agent_status() -> dict:
    """Per-provider install + auth status for the wizard's CLI agents step.

    Probes via two mechanisms:
      1. Binary resolution — reuses Orchestrator.routes.cli_agent_routes
         ._resolve_provider_bin, which walks an extended PATH that
         includes ~/.nvm/versions/node/<ver>/bin so it finds the npm
         globals install.sh's Step 1c laid down.
      2. Auth marker presence — checks for any one of a small list of
         credential-file paths under the user's home dir per provider.

    Returns:
        {
          "providers": {
            "claude": {"installed": bool, "authenticated": bool,
                       "bin_path": str|None, "package": str},
            "gemini": {...},
            "codex":  {...},
            "antigravity": {"installed": bool,
                            "authenticated": None,
                            "auth_method": "implicit_on_launch",
                            "bin_path": str|None,
                            "install_command": list[str]}
          },
          "ready": bool          # all installed AND (authenticated OR auth_method == implicit)
        }

    Antigravity (added 2026-05-22 per Track 1 plan) has a different
    shape: `authenticated` is always None because credentials live in
    the OS keyring (no file to inspect), and `auth_method` is the
    string the wizard uses to pick the right CTA copy. For the `ready`
    calculation, antigravity counts as ready when merely installed,
    since auth is verified only by the user actually launching the
    CLI (D2b).
    """
    from pathlib import Path
    from Orchestrator.routes.cli_agent_routes import _resolve_provider_bin, _PROVIDER_BINARY_NAMES

    home = Path.home()
    out: dict[str, dict] = {}
    all_ready = True
    for prov, cmd in INSTALL_COMMANDS.items():
        bin_name = _PROVIDER_BINARY_NAMES.get(prov, prov)
        bin_path = _resolve_provider_bin(prov)
        installed = bool(bin_path) and bin_path != bin_name and Path(bin_path).is_file()
        markers = _AUTH_MARKERS.get(prov, [])
        if markers:
            authenticated: bool | None = any((home / marker).exists() for marker in markers)
        else:
            # No file markers (e.g., antigravity uses OS keyring) → auth
            # state unobservable. Wizard handles via "Launch & Sign In" UI.
            authenticated = None
        entry: dict = {
            "installed": installed,
            "authenticated": authenticated,
            "bin_path": bin_path if installed else None,
            "install_command": cmd,
        }
        # Keep `package` for backwards compatibility with existing
        # consumers that read it on the npm-based providers. Derive
        # from the install command's last element (the package name).
        if cmd[:2] == ["npm", "install"]:
            entry["package"] = cmd[-1]
        if authenticated is None:
            # Currently only antigravity. Surface auth method so the
            # wizard renders the right CTA (D2b/D3b of 2026-05-22 plan).
            entry["auth_method"] = "implicit_on_launch"
            # Ready iff installed — auth verified out-of-band by user launch.
            provider_ready = installed
        else:
            provider_ready = installed and authenticated
        out[prov] = entry
        if not provider_ready:
            all_ready = False
    return {"providers": out, "ready": all_ready}


class SpawnTerminalRequest(BaseModel):
    provider: Literal["claude", "gemini", "codex", "antigravity"]
    mode: Literal["install", "auth"]


@router.post("/cli-agent/spawn-terminal")
def cli_agent_spawn_terminal(req: SpawnTerminalRequest) -> dict:
    """Spawn gnome-terminal in the user's GNOME session running the
    requested CLI command. Same portal→gio→direct cascade as
    /open-url — direct subprocess.Popen of gnome-terminal from
    blackbox.service's cgroup hits the same cross-namespace wall.

    Mode "install" runs the provider's install command (npm for
    claude/gemini/codex, curl-piped bash for antigravity), wrapped
    in an nvm-loaded shell so npm installs land at the right node
    version.
    Mode "auth" runs the provider's interactive login command. For
    antigravity there is no separate login command — the wizard
    (Track 2) should not request mode=auth for antigravity; we
    fall back to launching `agy` itself, which triggers the OAuth
    flow on first run via the OS keyring.

    Both commands are wrapped so the terminal stays open after exit:
    user sees the final output + presses Enter to dismiss. Without
    this, gnome-terminal closes the second the command exits and
    any error message is lost.
    """
    import shlex
    import subprocess

    prov = req.provider
    install_cmd_list = INSTALL_COMMANDS[prov]
    # Quote each arg safely so it round-trips through bash -c.
    install_cmd_str = " ".join(shlex.quote(a) for a in install_cmd_list)
    # Friendly label for the "Installing X..." line. For npm packages
    # use the package name; for antigravity show the binary name.
    install_label = install_cmd_list[-1] if install_cmd_list[:2] == ["npm", "install"] else prov

    # Prepend our xdg-open PATH shim and set BROWSER so any CLI inside
    # the spawned terminal (claude/gemini/codex/agy) that fires
    # `xdg-open <url>` for OAuth routes to a real browser binary,
    # NOT to whatever GNOME's xdg-mime defaults happen to be set to
    # (which reliably drift to gnome-text-editor on MSO2). Mirrors the
    # injection done by TmuxSessionManager for the modal path. gnome-
    # terminal-server forks the new terminal session itself, which can
    # drop env we pass via subprocess; baking export lines into the
    # inner bash guarantees the shim wins regardless.
    from Orchestrator.cli_agent.path_extension import path_shim_dir
    _shim_dir = shlex.quote(path_shim_dir())
    _shim_xdg = shlex.quote(os.path.join(path_shim_dir(), "xdg-open"))
    browser_setup = (
        f'export PATH={_shim_dir}:"$PATH"; '
        f'export BROWSER={_shim_xdg}; '
    )

    if req.mode == "install":
        inner = (
            f'{browser_setup}'
            f'export NVM_DIR="$HOME/.nvm"; '
            f'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
            f'echo "Installing {install_label}..."; '
            f'{install_cmd_str}; '
            f'echo; echo "Done. Press Enter to close."; read'
        )
    else:  # auth
        # Antigravity has no separate login command — launching the
        # binary itself triggers OAuth via the OS keyring on first
        # run. For npm-based providers, use the configured login cmd.
        cmd = _CLI_AUTH_CMD.get(prov) or "agy"
        inner = (
            f'{browser_setup}'
            f'export NVM_DIR="$HOME/.nvm"; '
            f'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
            f'echo "Starting {prov} authentication..."; '
            f'echo "Follow the prompts. Press Enter when done."; '
            f'echo; '
            f'{cmd}; '
            f'echo; echo "Auth flow finished. Press Enter to close."; read'
        )

    env = _build_user_env()

    # Attempt 1: portal launch on org.gnome.Terminal.desktop. The
    # portal can't pass arbitrary command args to a .desktop file's
    # exec, so this is best-effort — it opens a terminal but won't
    # run the inner command. Fall through immediately to gio/direct.
    # (Kept here for parity with /open-url cascade; gio is the
    # canonical path for terminals on GNOME.)

    # Attempt 2: gio launch on the gnome-terminal .desktop file with
    # the inner command as an arg. Works in some configurations.
    gt_desktop = "/usr/share/applications/org.gnome.Terminal.desktop"
    if os.path.exists(gt_desktop):
        logger.info("spawn-terminal: trying gio launch (%s, %s)", prov, req.mode)
        try:
            # gio launch can't reliably pass command args to gnome-terminal —
            # it'd open an interactive shell without running our command.
            # So skip gio for the typical case and go direct.
            pass
        except Exception:
            pass

    # Attempt 3: direct gnome-terminal spawn. This works when the env
    # is correctly reconstructed (which _build_user_env() does).
    logger.info("spawn-terminal: trying direct gnome-terminal (%s, %s)", prov, req.mode)
    try:
        proc = subprocess.Popen(
            ["gnome-terminal", "--", "bash", "-c", inner],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Give it ~1s to die-on-startup so we can surface a real error.
        try:
            rc = proc.wait(timeout=1.5)
            stderr = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "")[:500]
            logger.warning("spawn-terminal: gnome-terminal exited rc=%d stderr=%r", rc, stderr)
            return {"ok": False, "via": "gnome-terminal-direct",
                    "error": f"gnome-terminal exited rc={rc}",
                    "stderr": stderr}
        except subprocess.TimeoutExpired:
            logger.info("spawn-terminal: gnome-terminal SUCCESS (still running after 1.5s)")
            return {"ok": True, "via": "gnome-terminal-direct"}
    except FileNotFoundError:
        logger.error("spawn-terminal: gnome-terminal not on PATH")
        return {"ok": False, "error": "gnome-terminal not installed"}
    except Exception as e:
        logger.exception("spawn-terminal: unexpected error")
        return {"ok": False, "error": str(e)}
