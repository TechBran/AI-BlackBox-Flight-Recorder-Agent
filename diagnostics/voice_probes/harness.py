"""Reusable WS-probe harness core: pure helpers + redacting result writer.

Handshake contract per provider (design doc, P0):
  - OpenAI: first server event ``session.created`` after connecting to
    wss://api.openai.com/v1/realtime?model=<id>
  - xAI:    ``session.created`` on wss://api.x.ai/v1/realtime (?model=
    optional — session.created carries the resolved model);
    ``session.updated`` after a session.update
  - Gemini: ``setupComplete`` after sending BidiGenerateContentSetup

This module is network-free (unit-testable). Live probes: probes.py.
Endpoint constants mirror Orchestrator/config.py:499,539,659 — kept literal
here so the harness never imports the service config.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from diagnostics.voice_probes.env import load_service_env

RESULTS_DIR = Path(__file__).resolve().parent / "results"

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime"
GEMINI_LIVE_URL_TEMPLATE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.{api_version}.GenerativeService.BidiGenerateContent"
)

MAX_EVENTS = 60   # cap recorded server events per probe (audio deltas flood)
MAX_STR = 300     # truncate long strings (base64 audio) in recorded events
REDACTED = "***REDACTED***"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def build_openai_url(model: str) -> str:
    return f"{OPENAI_REALTIME_URL}?model={model}"


def build_xai_url(model: str = "") -> str:
    return f"{XAI_REALTIME_URL}?model={model}" if model else XAI_REALTIME_URL


def build_gemini_url(api_version: str, key: str) -> str:
    return GEMINI_LIVE_URL_TEMPLATE.format(api_version=api_version) + f"?key={key}"


def classify_first_event(provider: str, event: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (handshake_ok, resolved_model) from the first server event."""
    if provider == "gemini":
        return ("setupComplete" in event, "")
    ok = event.get("type") == "session.created"
    resolved = ""
    if isinstance(event.get("session"), dict):
        resolved = event["session"].get("model") or ""
    return (ok, resolved)


def truncate_deep(obj: Any, max_str: int = MAX_STR) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else (
            obj[:max_str] + f"...<truncated {len(obj)} chars>"
        )
    if isinstance(obj, dict):
        return {k: truncate_deep(v, max_str) for k, v in obj.items()}
    if isinstance(obj, list):
        return [truncate_deep(v, max_str) for v in obj]
    return obj


def service_secrets() -> List[str]:
    """Every plausible secret value from the service .env, for redaction."""
    env = load_service_env()
    return [
        v for k, v in env.items()
        if v and len(v) >= 8
        and any(t in k for t in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
    ]


def redact_text(text: str, secrets: Optional[List[str]] = None) -> str:
    for s in (secrets if secrets is not None else service_secrets()):
        text = text.replace(s, REDACTED)
    return text


@dataclass
class ProbeResult:
    provider: str            # "openai" | "xai" | "gemini"
    model: str               # "" = provider default (xai no-?model=)
    probe: str               # "handshake" | "server_vad_roundtrip" | "full_tools" | ...
    ok: bool = False
    resolved_model: str = ""
    close_code: Optional[int] = None
    close_reason: str = ""
    error: str = ""
    notes: str = ""
    ts: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)

    def add_event(self, event: Dict[str, Any]) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append(truncate_deep(event))

    def summary(self) -> str:
        bits = [f"{self.provider} {self.model or '(default)'} {self.probe}: "
                f"{'OK' if self.ok else 'FAIL'}"]
        if self.resolved_model:
            bits.append(f"resolved={self.resolved_model}")
        if self.close_code is not None:
            bits.append(f"close={self.close_code} {self.close_reason}")
        if self.error:
            bits.append(f"error={self.error}")
        return redact_text(" | ".join(bits))


def write_results(
    name: str,
    results: List[ProbeResult],
    results_dir: Path = RESULTS_DIR,
    secrets: Optional[List[str]] = None,
) -> Path:
    """Write date-stamped results JSON with every service secret redacted."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{time.strftime('%Y-%m-%d')}-{name}.json"
    payload = {
        "generated": now_iso(),
        "name": name,
        "results": [asdict(r) for r in results],
    }
    path.write_text(redact_text(json.dumps(payload, indent=2, ensure_ascii=False), secrets))
    return path
