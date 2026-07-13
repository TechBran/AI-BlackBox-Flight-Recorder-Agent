"""Per-provider key validators.

Each validator does a CHEAP call (1 token cost or one cheap metadata API)
to confirm the supplied credential works. Returns ValidationResult with
ok/error/latency_ms so the wizard can show clean per-provider feedback.

Tier-1 (v1 wizard): OpenAI, Anthropic, Google, xAI, Perplexity, Tailscale, Gmail.
Wizard-active (Tier-1): ElevenLabs (key validator below — surfaces plan tier + cloning gate).
Custom (user-registered): OpenAI-compatible LAN servers via validate_custom (models.list doubles as model discovery).
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
    """Validate Perplexity key via cheapest-possible chat completion.

    Perplexity exposes an OpenAI-compatible API at api.perplexity.ai. Same SDK
    reuse pattern as xAI. NOTE: Perplexity rejects max_tokens < 16 with a 400
    ("max_tokens must be at least 16"), so we ask for exactly 16 — the minimum
    valid (and cheapest) probe.
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
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"model": resp.model, "id": resp.id}
    return _measure(_fn)


def validate_custom(base_url: str, api_key: str = "") -> ValidationResult:
    """Validate a user-registered OpenAI-compatible server via models.list.

    Custom-model onboarding (llama.cpp / llama-swap / vLLM on the LAN).
    models.list is zero token cost and instant even on a cold llama-swap box
    (/v1/models never loads a model). Success detail doubles as model
    discovery: the wizard renders detail["models"] and the register route
    persists them. api_key falls back to "none" because the openai SDK
    refuses empty keys and many LAN servers run keyless.
    """
    def _fn():
        from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI
        try:
            with OpenAI(
                api_key=api_key or "none",
                base_url=base_url,
                timeout=10.0,
                max_retries=0,
            ) as client:
                models = client.models.list()
                ids = [m.id for m in models.data]
                # Auto-detect: seed each discovered model's modality (chat/image/
                # tts/stt/...) so the wizard can confirm + route it with no manual
                # per-modality step. Name-pattern only (zero-cost; no endpoint probe
                # that could trigger a model load). The wizard-confirmed map wins.
                from Orchestrator.onboarding.custom_servers import classify_models
                shown = ids[:50]
                modalities = classify_models(shown)
                # Audio capability probe -- Speaches behind Caddy serves audio at
                # /v1/audio/* + /v1/realtime, but those models are NOT in /v1/models.
                # A GET returns 405/307 when the path exists, 404 when it doesn't.
                # Best-effort + fail-soft: a probe failure just means "not detected".
                import httpx
                _hdr = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                def _has(path):
                    try:
                        r = httpx.get(f"{base_url}{path}", headers=_hdr,
                                      timeout=5.0, follow_redirects=False)
                        return r.status_code != 404
                    except Exception:
                        return False
                audio = {"stt": _has("/audio/transcriptions"),
                         "tts": _has("/audio/speech"),
                         "streaming": _has("/realtime")}
                return {"model_count": len(ids), "models": shown,
                        "model_modalities": modalities,
                        "capabilities": sorted(set(modalities.values())),
                        "audio": audio}
        except AuthenticationError as e:
            raise RuntimeError(f"API key rejected (401) by {base_url}") from e
        except APIStatusError as e:
            raise RuntimeError(
                f"HTTP {e.status_code} from {base_url} (is the path /v1 correct?)"
            ) from e
        except APIConnectionError as e:
            raise RuntimeError(f"Server unreachable at {base_url}") from e
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


def validate_cohere(api_key: str) -> ValidationResult:
    """Validate a Cohere key via the zero-cost POST /v1/check-api-key endpoint.

    Reranker upgrade key (M10) — lives in the API-Keys step like every other
    provider. check-api-key costs nothing and returns {valid, organization_name}
    so the wizard shows the org the key belongs to. raw requests, no new SDK.
    """
    def _fn():
        import requests
        r = requests.post(
            "https://api.cohere.ai/v1/check-api-key",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if r.status_code in (401, 403):
            raise RuntimeError("Invalid Cohere API key")
        r.raise_for_status()
        data = r.json() or {}
        return {"organization": data.get("organization_name") or ""}
    return _measure(_fn)


def validate_voyage(api_key: str) -> ValidationResult:
    """Validate a Voyage key via a tiny 1-document POST /v1/rerank.

    Reranker upgrade key (M10). A ONE-document rerank stays under the free-tier
    10K-TPM cap that a full 40-doc rerank exceeds (the M8 live finding) while
    still exercising the key end-to-end. raw requests, no new SDK.
    """
    def _fn():
        import requests
        r = requests.post(
            "https://api.voyageai.com/v1/rerank",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "rerank-2.5",
                "query": "ping",
                "documents": ["pong"],
                "top_k": 1,
            },
            timeout=10,
        )
        if r.status_code in (401, 403):
            raise RuntimeError("Invalid Voyage API key")
        r.raise_for_status()
        data = r.json() or {}
        return {"model": data.get("model") or "rerank-2.5"}
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
