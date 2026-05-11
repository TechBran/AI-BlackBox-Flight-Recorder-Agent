"""Onboarding wizard backend routes.

Mounted at /onboarding/* by Orchestrator/app.py.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator.onboarding import validators
from Orchestrator.onboarding.secrets_writer import update_env
from Orchestrator.onboarding.state import (
    StepName,
    ALL_STEPS,
    get_state,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# Use the singleton from state.py — DO NOT instantiate OnboardingState() directly.
_state = get_state()


class StateResponse(BaseModel):
    is_complete: bool
    completed_steps: list[str]
    skipped_steps: list[str]
    current_step: str
    all_steps: list[str]


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
    return ValidateResponse(**vars(result))


@router.post("/save")
def save_secrets(req: SaveRequest) -> dict:
    return update_env(req.secrets)


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
    _state.mark_complete()
    return {"ok": True, "is_complete": True}


@router.post("/reset")
def reset() -> dict:
    """Reset onboarding state (for testing or re-onboarding)."""
    _state.reset()
    return _state.snapshot()
