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
    provider: Literal["openai", "anthropic", "google", "tailscale", "gmail"]
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
    return _state.snapshot()


@router.post("/step/skip")
def step_skip(req: StepActionRequest) -> dict:
    _state.mark_step_skipped(req.step)
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
