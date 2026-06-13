"""Per-provider key validators.

Each validator does a CHEAP call (1 token cost or one cheap metadata API)
to confirm the supplied credential works. Returns ValidationResult with
ok/error/latency_ms so the wizard can show clean per-provider feedback.

Tier-1 (v1 wizard): OpenAI, Anthropic, Google, xAI, Perplexity, Tailscale, Gmail.
Wizard-active (Tier-1): ElevenLabs (key validator below — surfaces plan tier + cloning gate).
Tier-2 (v1.1): Twilio, Asterisk.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationResult:
    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict[str, Any] | None = None


def _measure(fn: Callable[[], dict[str, Any]]) -> ValidationResult:
    """Wrap a sync validator with latency measurement + error capture."""
    start = time.perf_counter()
    try:
        detail = fn()
        return ValidationResult(
            ok=True,
            latency_ms=int((time.perf_counter() - start) * 1000),
            detail=detail,
        )
    except Exception as e:
        return ValidationResult(
            ok=False,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


# ──────────────────────────── Tier-1 ────────────────────────────

def validate_openai(api_key: str) -> ValidationResult:
    """Validate OpenAI API key via models.list (no token cost)."""
    def _fn():
        from openai import OpenAI
        with OpenAI(api_key=api_key, timeout=10.0, max_retries=0) as client:
            models = client.models.list()
            return {"model_count": len(models.data)}
    return _measure(_fn)


def validate_anthropic(api_key: str) -> ValidationResult:
    """Validate Anthropic key via cheapest-possible message (1-token completion)."""
    def _fn():
        import anthropic
        with anthropic.Anthropic(api_key=api_key, timeout=10.0, max_retries=0) as client:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"model": resp.model, "id": resp.id}
    return _measure(_fn)


def validate_google(api_key: str) -> ValidationResult:
    """Validate Google AI key via list_models."""
    def _fn():
        from google import genai
        from google.genai.types import HttpOptions
        with genai.Client(
            api_key=api_key,
            http_options=HttpOptions(timeout=10000),  # ms
        ) as client:
            models = list(client.models.list())
            return {"model_count": len(models)}
    return _measure(_fn)


def validate_xai(api_key: str) -> ValidationResult:
    """Validate xAI key via cheapest-possible chat completion (1-token completion).

    xAI exposes an OpenAI-compatible API at api.x.ai/v1, so we reuse the openai SDK
    with a custom base_url. Avoids adding a new SDK dependency.
    """
    def _fn():
        from openai import OpenAI
        with OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            timeout=10.0,
            max_retries=0,
        ) as client:
            resp = client.chat.completions.create(
                model="grok-3-mini",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"model": resp.model, "id": resp.id}
    return _measure(_fn)


def validate_perplexity(api_key: str) -> ValidationResult:
    """Validate Perplexity key via cheapest-possible chat completion (1-token).

    Perplexity exposes an OpenAI-compatible API at api.perplexity.ai. Same SDK
    reuse pattern as xAI.
    """
    def _fn():
        from openai import OpenAI
        with OpenAI(
            api_key=api_key,
            base_url="https://api.perplexity.ai",
            timeout=10.0,
            max_retries=0,
        ) as client:
            resp = client.chat.completions.create(
                model="sonar",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"model": resp.model, "id": resp.id}
    return _measure(_fn)


def validate_elevenlabs(api_key: str) -> ValidationResult:
    """GET /v1/user - cheap metadata call. Surfaces plan tier + feature gates
    (IVC needs Starter+, PVC Creator+) so the wizard tells the customer what
    their key actually unlocks, not just that it works."""
    def _fn():
        import requests
        r = requests.get("https://api.elevenlabs.io/v1/user",
                         headers={"xi-api-key": api_key}, timeout=10)
        if r.status_code == 401:
            raise RuntimeError("Invalid ElevenLabs API key")
        r.raise_for_status()
        sub = r.json().get("subscription", {}) or {}
        tier = sub.get("tier", "free")
        ivc = bool(sub.get("can_use_instant_voice_cloning"))
        return {
            "tier": tier,
            "credits_remaining": (sub.get("character_limit", 0) or 0) - (sub.get("character_count", 0) or 0),
            "features": ("voice cloning available" if ivc
                         else "voice cloning not available on this plan"),
        }
    return _measure(_fn)


def validate_tailscale() -> ValidationResult:
    """Validate Tailscale install + auth via 'tailscale status --json'."""
    def _fn():
        if not shutil.which("tailscale"):
            raise RuntimeError("tailscale binary not found on PATH")
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tailscale status failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        backend = data.get("BackendState", "unknown")
        if backend != "Running":
            raise RuntimeError(f"tailscale not running (BackendState={backend})")
        self_node = data.get("Self", {})
        magicdns_suffix = data.get("MagicDNSSuffix") or ""  # empty string if MagicDNS off for tailnet
        return {
            "hostname": self_node.get("DNSName", "").rstrip("."),
            "ip": (self_node.get("TailscaleIPs") or ["unknown"])[0],
            "online": self_node.get("Online", False),
            "magicdns_suffix": magicdns_suffix,
            "magicdns_enabled": bool(magicdns_suffix),
        }
    return _measure(_fn)


def validate_gmail_oauth(client_id: str, client_secret: str) -> ValidationResult:
    """Validate Gmail OAuth client by attempting to construct an OAuth flow object.
    Does NOT trigger interactive auth — that happens in the wizard browser frame.
    """
    def _fn():
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            {"web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:9091/auth/gmail/callback"],
            }},
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        url, _ = flow.authorization_url()
        return {"auth_url_prefix": url.split("?")[0]}
    return _measure(_fn)
