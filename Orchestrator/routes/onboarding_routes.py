"""Onboarding wizard backend routes.

Mounted at /onboarding/* by Orchestrator/app.py.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from Orchestrator.onboarding import validators
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


class ValidateRequest(BaseModel):
    provider: Literal["openai", "anthropic", "google", "xai", "perplexity", "tailscale", "gmail"]
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
    """Return a redacted snapshot of what's configured today. Manage-mode UI reads this."""
    from Orchestrator.config import (
        OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
        XAI_API_KEY, PERPLEXITY_API_KEY,
        GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
    )
    val_at = _state.validated_at()
    providers = {
        "openai": {
            "present": bool(OPENAI_API_KEY),
            "last4": _redact(OPENAI_API_KEY),
            "validated_at": val_at.get("openai"),
        },
        "anthropic": {
            "present": bool(ANTHROPIC_API_KEY),
            "last4": _redact(ANTHROPIC_API_KEY),
            "validated_at": val_at.get("anthropic"),
        },
        "google": {
            "present": bool(GOOGLE_API_KEY),
            "last4": _redact(GOOGLE_API_KEY),
            "validated_at": val_at.get("google"),
        },
        "xai": {
            "present": bool(XAI_API_KEY),
            "last4": _redact(XAI_API_KEY),
            "validated_at": val_at.get("xai"),
        },
        "perplexity": {
            "present": bool(PERPLEXITY_API_KEY),
            "last4": _redact(PERPLEXITY_API_KEY),
            "validated_at": val_at.get("perplexity"),
        },
        "gmail": {
            "present": bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET),
            "client_id": GOOGLE_OAUTH_CLIENT_ID or None,  # public per Google OAuth docs
            "secret_last4": _redact(GOOGLE_OAUTH_CLIENT_SECRET),
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
    paired_devices: list[dict] = []  # TODO(Phase 2.10): replace with pairing_routes.list_claims() — expected shape [{token, device_kind, claimed_at, hostname}]
    return CurrentConfigResponse(
        providers=providers,
        operators=operators,
        paired_devices=paired_devices,
        tailscale=tailscale,
        onboarding_state=_state.snapshot(),
    )


@router.get("/config/{key}")
def get_config_value(key: str, request: Request, reveal: bool = False) -> dict:
    """Return a single config value. With ?reveal=true, returns full cleartext.

    Loopback-only when revealing — refuses if request not from 127.0.0.1 / ::1.
    Without reveal, returns the same redacted ••••XXXX shape as /current-config.
    Both modes require the key to be in ALLOWED_REVEAL_KEYS.

    Caveats:
    - The value is read from os.getenv at request time, but Orchestrator.config
      loads .env once at process start. After /save mutates .env, this endpoint
      reflects the OLD value until BlackBox restart. Mirror behavior of /save.
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
    value = os.getenv(key, "")
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


@router.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest) -> ValidateResponse:
    creds = req.credentials
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
        elif req.provider == "tailscale":
            result = validators.validate_tailscale()
        elif req.provider == "gmail":
            result = validators.validate_gmail_oauth(creds["client_id"], creds["client_secret"])
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
