# Voice Agent Pipeline Upgrade Pass — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Production upgrade of all three realtime voice providers — Gemini rescue (confirmed 1007 tool-schema root cause + reconnect/silence fixes), OpenAI gpt-realtime-2.1 layer, Grok model addressing + full feature surface, provider-agnostic Voice Agent Presets, xAI-provisioned phone number (sovereign line), Android/Portal uplift, translation mode, affective dialog, and xAI voice cloning.

**Architecture:** The Orchestrator stays the sole protocol bridge (server-side relay; keys, tools, and transcripts never leave the box). Tool lists move from import-time freezes to configure-time reads. Every model/voice list becomes catalog-driven from `/realtime/status`, `/gemini-live/status`, `/grok-live/status`. Presets are a gitignored fresh-read registry applied at session-configure time. All catalog changes are gated on empirical WS probes (Phase 0) — never docs alone.

**Tech Stack:** FastAPI + websockets (Python 3, Orchestrator venv), ToolVault v2 modules, Android Kotlin/Compose (gradle 9, JDK17, offline), Portal vanilla-JS modules, Tailscale Funnel for the xAI webhook, pytest + fake-WS fixtures, systemd (`blackbox.service` runs LIVE from this working tree — every task must leave the tree importable).

**Design doc:** `docs/plans/2026-07-11-voice-agent-upgrade-pass-design.md` (approved). Recon evidence: session scratchpad `recon/*.json` (10-agent investigation, 2026-07-11).

**Phase order & dependencies:** P0 (probes) → P1a (Gemini rescue) + P1b (cross-route hardening) → P2 (OpenAI/Grok modernization; consumes P0 results) → P3a/P3b/P3c (Android data → Android UI → Portal) → P4 (presets) → P5 (xAI phone; consumes P4) → P6a/P6b/P6c (extras). P0.7 (Gemini full-tool probe) runs AFTER P1.1. Each phase is independently shippable; restart `blackbox.service` only at phase boundaries.

---

## Phase 0 — Live wire probes

Every probe in this phase makes **real, paid API calls** (seconds of connect time; negligible cost). Probes RECORD outcomes — a rejected model id is a valid finding, not a task failure. All results land in `diagnostics/voice_probes/results/<date>-<name>.json` (keys redacted); later phases (catalog gating in P2, translate gating in P6) consume these files. Keys come from the service EnvironmentFile: `systemctl cat blackbox.service` → `EnvironmentFile=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.env` (verified: contains `OPENAI_API_KEY=`, `GOOGLE_API_KEY=`, `XAI_API_KEY=`). Never print or persist key material.

**Ordering note:** P0.1–P0.6 run now. **P0.7 (Gemini full tool-group probe) must run AFTER P1.1** (the `update_sheet_values` schema fix) — pre-fix it reproduces the known 1007 and proves nothing new.

### Task P0.1: Probe harness core (env loader, URL builders, classifier, redacting result writer)

**Files:**
- Create: diagnostics/voice_probes/__init__.py
- Create: diagnostics/voice_probes/env.py
- Create: diagnostics/voice_probes/harness.py
- Modify: pytest.ini:10-12 (markers block)
- Test: Orchestrator/tests/test_voice_probes_harness.py

**Step 1: Write the failing test**

```python
"""Offline unit tests for the voice-probe harness (diagnostics/voice_probes/).

Pure helpers only — no network. Live probes live in
diagnostics/voice_probes/test_live_probes.py (marker: probe_live), which sits
OUTSIDE pytest.ini's testpaths so the default suite never dials a provider.
"""
import json

from diagnostics.voice_probes.env import load_service_env
from diagnostics.voice_probes.harness import (
    ProbeResult,
    build_gemini_url,
    build_openai_url,
    build_xai_url,
    classify_first_event,
    truncate_deep,
    write_results,
)


def test_load_service_env_parses_and_strips(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "OPENAI_API_KEY=sk-aaa111\n"
        'XAI_API_KEY="xai-bbb222"\n'
        "EMPTY=\n"
        "not a kv line\n"
    )
    env = load_service_env(env_file)
    assert env["OPENAI_API_KEY"] == "sk-aaa111"
    assert env["XAI_API_KEY"] == "xai-bbb222"  # quotes stripped
    assert env["EMPTY"] == ""
    assert "not a kv line" not in env


def test_load_service_env_missing_file_is_empty(tmp_path):
    assert load_service_env(tmp_path / "nope.env") == {}


def test_url_builders():
    assert build_openai_url("gpt-realtime-2.1") == (
        "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    )
    assert build_xai_url() == "wss://api.x.ai/v1/realtime"
    assert build_xai_url("grok-voice-latest") == (
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    )
    url = build_gemini_url("v1alpha", "SEKRETKEY123")
    assert url.startswith(
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
    )
    assert url.endswith("?key=SEKRETKEY123")


def test_classify_first_event():
    ok, resolved = classify_first_event(
        "xai",
        {"type": "session.created", "session": {"model": "grok-voice-think-fast-1.0"}},
    )
    assert ok and resolved == "grok-voice-think-fast-1.0"
    ok, _ = classify_first_event("openai", {"type": "error", "error": {"message": "unknown model"}})
    assert not ok
    ok, _ = classify_first_event("gemini", {"setupComplete": {}})
    assert ok
    ok, _ = classify_first_event("gemini", {"serverContent": {}})
    assert not ok


def test_truncate_deep_caps_long_strings():
    obj = {"audio": "A" * 5000, "nested": [{"delta": "B" * 5000}], "n": 7}
    out = truncate_deep(obj, max_str=300)
    assert len(out["audio"]) < 400 and "truncated 5000" in out["audio"]
    assert "truncated 5000" in out["nested"][0]["delta"]
    assert out["n"] == 7


def test_write_results_redacts_secrets(tmp_path):
    r = ProbeResult(
        provider="gemini", model="m", probe="handshake",
        error="HTTP 403 for url ?key=SEKRETKEY123",
    )
    path = write_results("unit", [r], results_dir=tmp_path, secrets=["SEKRETKEY123"])
    text = path.read_text()
    assert "SEKRETKEY123" not in text
    assert "***REDACTED***" in text
    payload = json.loads(text)
    assert payload["results"][0]["probe"] == "handshake"
    assert path.name.endswith("-unit.json")  # date-stamped prefix


def test_probe_result_event_cap_and_summary():
    r = ProbeResult(
        provider="xai", model="", probe="handshake", ok=True,
        resolved_model="grok-voice-think-fast-1.0",
    )
    for i in range(200):
        r.add_event({"type": f"e{i}"})
    assert len(r.events) == 60  # MAX_EVENTS cap
    s = r.summary()
    assert "(default)" in s and "resolved=grok-voice-think-fast-1.0" in s and "OK" in s
```

Write this to `Orchestrator/tests/test_voice_probes_harness.py`.

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_probes_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'diagnostics.voice_probes'`

**Step 3: Write minimal implementation**

`diagnostics/voice_probes/__init__.py`:

```python
"""WS-probe harness for realtime voice providers (voice upgrade pass, P0)."""
```

`diagnostics/voice_probes/env.py`:

```python
"""Read provider API keys from the service EnvironmentFile.

The blackbox.service unit loads exactly this file (systemctl cat
blackbox.service -> EnvironmentFile=<repo>/.env), so probing with these keys
exercises the same credentials the live service uses. Values are NEVER logged
or written to results — only redacted output leaves this package.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def load_service_env(path: Path = ENV_FILE) -> Dict[str, str]:
    """Parse KEY=VALUE lines; skip comments/blank/non-kv lines; strip quotes."""
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_key(name: str) -> str:
    """Process env wins (service-injected); fall back to parsing .env."""
    return os.environ.get(name) or load_service_env().get(name, "")
```

`diagnostics/voice_probes/harness.py`:

```python
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
```

Then register the live-probe marker — in `pytest.ini`, the markers block currently reads (lines 10-12):

```ini
# Custom markers.
markers =
    real_fetchers: opt out of the hermetic patch_hf_fetchers autouse fixture so the test exercises the REAL _fetch_hf_models/_fetch_hf_tree (with only _http_get_json patched).
```

Append one line so it becomes:

```ini
# Custom markers.
markers =
    real_fetchers: opt out of the hermetic patch_hf_fetchers autouse fixture so the test exercises the REAL _fetch_hf_models/_fetch_hf_tree (with only _http_get_json patched).
    probe_live: LIVE WS probe against a provider realtime endpoint (network + paid API calls; run explicitly with `pytest diagnostics/voice_probes -m probe_live` — never in CI/default suite).
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_probes_harness.py -v && Orchestrator/venv/bin/python -m pytest --markers | grep probe_live`
Expected: PASS (7 passed) and the `probe_live` marker line printed. Default suite is untouched (testpaths = Orchestrator/tests; this test file is offline-pure).

**Step 5: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/__init__.py diagnostics/voice_probes/env.py diagnostics/voice_probes/harness.py Orchestrator/tests/test_voice_probes_harness.py pytest.ini
git commit -m "feat(diagnostics): voice-probe harness core — env loader, URL builders, redacting result writer (P0.1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P0.2: Live probe functions + CLI (`python -m diagnostics.voice_probes.run`)

**Files:**
- Create: diagnostics/voice_probes/probes.py
- Create: diagnostics/voice_probes/run.py
- Verify (live, no unit test — network code; the pure parts were TDD'd in P0.1): exact commands in Step 3

**Step 1: Write the probe functions**

`diagnostics/voice_probes/probes.py` (connection idiom mirrors `Orchestrator/routes/realtime_routes.py:282-289` — websockets 15.x `additional_headers`, explicit open/ping/close timeouts):

```python
"""Live WS probes — network + paid API calls.

Invoked by the run.py CLI and the probe_live pytest suite; NEVER imported by
the service. Failure capture: WS close code/reason (Gemini setup rejections
arrive as close 1007/1008), HTTP status at upgrade (OpenAI unknown-model
rejections), error events, timeouts.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional, Sequence

import websockets

from diagnostics.voice_probes.env import get_key
from diagnostics.voice_probes.harness import (
    ProbeResult,
    build_gemini_url,
    build_openai_url,
    build_xai_url,
    classify_first_event,
    now_iso,
    redact_text,
)

# Mirrors the box's connect idiom (Orchestrator/routes/realtime_routes.py:282-289).
CONNECT_KW = dict(open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10)


async def _recv_json(ws, timeout: float) -> Dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def _capture_failure(result: ProbeResult, exc: BaseException) -> None:
    if isinstance(exc, websockets.exceptions.ConnectionClosed):
        frame = exc.rcvd
        result.close_code = frame.code if frame else None
        result.close_reason = redact_text(frame.reason if frame else "")
        result.error = "connection closed"
    elif isinstance(exc, websockets.exceptions.InvalidStatus):
        body = ""
        try:
            body = exc.response.body.decode("utf-8", "replace")[:500]
        except Exception:
            pass
        result.error = redact_text(f"HTTP {exc.response.status_code} at WS upgrade: {body}")
    elif isinstance(exc, asyncio.TimeoutError):
        result.error = "timeout waiting for server event"
    else:
        result.error = redact_text(f"{type(exc).__name__}: {exc}")


async def _probe_openai_style(
    provider: str,
    url: str,
    key_name: str,
    model: str,
    probe: str,
    session_patch: Optional[Dict],
    audio_pcm: Optional[bytes],
    listen_s: float,
    timeout: float,
) -> ProbeResult:
    result = ProbeResult(provider=provider, model=model, probe=probe, ts=now_iso())
    key = get_key(key_name)
    if not key:
        result.error = f"{key_name} not set in service env (.env)"
        return result
    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {key}"}, **CONNECT_KW
        ) as ws:
            first = await _recv_json(ws, timeout)
            result.add_event(first)
            result.ok, result.resolved_model = classify_first_event(provider, first)
            if session_patch is not None and result.ok:
                await ws.send(json.dumps({"type": "session.update", "session": session_patch}))
                for _ in range(10):
                    event = await _recv_json(ws, timeout)
                    result.add_event(event)
                    if event.get("type") in ("session.updated", "error"):
                        result.ok = event.get("type") == "session.updated"
                        break
            if audio_pcm and result.ok:
                # ~100ms chunks of 24kHz s16le mono, paced like a live mic.
                for i in range(0, len(audio_pcm), 4800):
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio_pcm[i:i + 4800]).decode(),
                    }))
                    await asyncio.sleep(0.02)
            if listen_s > 0 and result.ok:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + listen_s
                while loop.time() < deadline:
                    try:
                        event = await _recv_json(ws, deadline - loop.time())
                    except asyncio.TimeoutError:
                        break
                    result.add_event(event)
        types = sorted({e.get("type", "?") for e in result.events if isinstance(e, dict)})
        result.notes = (result.notes + " " if result.notes else "") + \
            "event_types=" + ",".join(types)
    except Exception as exc:
        _capture_failure(result, exc)
    return result


async def probe_openai(
    model: str, *, probe: str = "handshake",
    session_patch: Optional[Dict] = None, timeout: float = 15.0,
) -> ProbeResult:
    return await _probe_openai_style(
        "openai", build_openai_url(model), "OPENAI_API_KEY",
        model, probe, session_patch, None, 0.0, timeout,
    )


async def probe_xai(
    model: str = "", *, probe: str = "handshake",
    session_patch: Optional[Dict] = None,
    audio_pcm: Optional[bytes] = None, listen_s: float = 0.0,
    timeout: float = 15.0,
) -> ProbeResult:
    return await _probe_openai_style(
        "xai", build_xai_url(model), "XAI_API_KEY",
        model, probe, session_patch, audio_pcm, listen_s, timeout,
    )


async def probe_gemini(
    model: str, *, probe: str = "handshake",
    tools: Optional[List[Dict]] = None,
    api_version: str = "v1beta",
    setup_extra: Optional[Dict] = None,
    response_modalities: Optional[Sequence[str]] = ("AUDIO",),
    timeout: float = 20.0,
) -> ProbeResult:
    """Send BidiGenerateContentSetup; ok iff setupComplete arrives.

    Setup shape mirrors Orchestrator/routes/gemini_live_routes.py:429-448.
    response_modalities=None omits generationConfig entirely (server default —
    used by the translate-shape probe).
    """
    result = ProbeResult(provider="gemini", model=model, probe=probe, ts=now_iso())
    key = get_key("GOOGLE_API_KEY")
    if not key:
        result.error = "GOOGLE_API_KEY not set in service env (.env)"
        return result
    setup: Dict[str, Any] = {"model": f"models/{model}"}
    if response_modalities is not None:
        setup["generationConfig"] = {"responseModalities": list(response_modalities)}
    if tools is not None:
        setup["tools"] = tools
        n = sum(len(t.get("functionDeclarations", []))
                for t in tools if isinstance(t, dict))
        result.notes = f"{n} functionDeclarations"
    if setup_extra:
        setup.update(setup_extra)
    try:
        async with websockets.connect(build_gemini_url(api_version, key), **CONNECT_KW) as ws:
            await ws.send(json.dumps({"setup": setup}))
            first = await _recv_json(ws, timeout)
            result.add_event(first)
            result.ok, _ = classify_first_event("gemini", first)
    except Exception as exc:
        _capture_failure(result, exc)
    return result
```

**Step 2: Write the CLI with canned suites**

`diagnostics/voice_probes/run.py`:

```python
"""Voice-probe CLI.

Single probe:
    python -m diagnostics.voice_probes.run --provider openai --model gpt-realtime-2.1
    python -m diagnostics.voice_probes.run --provider xai            # no ?model= (default resolution)
    python -m diagnostics.voice_probes.run --provider gemini --model gemini-3.1-flash-live-preview

Canned suites (each writes diagnostics/voice_probes/results/<date>-<suite>.json):
    python -m diagnostics.voice_probes.run --suite openai-models
    python -m diagnostics.voice_probes.run --suite xai
    python -m diagnostics.voice_probes.run --suite gemini-tools     # AFTER P1.1 schema fix
    python -m diagnostics.voice_probes.run --suite translate

Exit code is 0 as long as the harness ran — a rejected model is a RECORDED
finding, not a failure. Run from the repo root with the Orchestrator venv.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import List

from diagnostics.voice_probes.harness import ProbeResult, write_results
from diagnostics.voice_probes.probes import probe_gemini, probe_openai, probe_xai

ASSETS = Path(__file__).resolve().parent / "assets"

# The backend's exact server_vad knobs (Orchestrator/routes/grok_live_routes.py:441-446)
# — the round-trip probe checks whether xAI echoes them back in session.updated
# or silently drops them (docs don't list threshold/padding on the xAI schema).
SERVER_VAD_KNOBS = {
    "turn_detection": {
        "type": "server_vad",
        "threshold": 0.7,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 900,
    }
}

# audio.input probes (P2 gates). Both are session.update round-trips through
# _probe_openai_style: ok=True iff a session.updated event arrives (patch
# ACCEPTED); the echoed session object inside the recorded session.updated
# event is the finding itself (extracted in P0.4 Step 4).
#   - input_rate_16k: does xAI accept audio.input.format rate=16000?
#     Gates the P2.15 Branch A/B choice.
#   - transcription_shape: is an explicit audio.input.transcription opt-in
#     accepted, and what shape does the server echo back? Gates P2.11.
INPUT_16K_PATCH = {
    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 16000}}}
}
TRANSCRIPTION_PATCH = {
    "audio": {"input": {"transcription": {}}}
}


async def suite_openai_models(args) -> List[ProbeResult]:
    # gpt-realtime-2.1 / -mini: expected OK (GA 2026-07-06, research-confirmed).
    # gpt-realtime-2025-08-28: docs say valid, our May test saw close-4000 — re-probe.
    # gpt-live-1 / -mini: ChatGPT-only per docs — expect rejection; RECORD the exact shape.
    models = [
        "gpt-realtime-2.1",
        "gpt-realtime-2.1-mini",
        "gpt-realtime-2025-08-28",
        "gpt-live-1",
        "gpt-live-1-mini",
    ]
    return [await probe_openai(m) for m in models]


async def suite_xai(args) -> List[ProbeResult]:
    results = [await probe_xai("", probe="default_model_resolution")]
    results.append(await probe_xai("grok-voice-latest"))
    results.append(await probe_xai("grok-voice-think-fast-1.0"))
    results.append(await probe_xai(
        "grok-voice-latest", probe="server_vad_roundtrip",
        session_patch=SERVER_VAD_KNOBS,
    ))
    # 16 kHz input-format round-trip (P2.15 Branch A/B gate): ok=True means
    # xAI accepted audio.input.format.rate=16000; the echoed format lives in
    # the recorded session.updated event.
    results.append(await probe_xai(
        "grok-voice-latest", probe="input_rate_16k",
        session_patch=INPUT_16K_PATCH,
    ))
    # Explicit input-transcription opt-in (P2.11 gate): ok=True means the bare
    # {} shape is accepted; the echoed audio.input.transcription object in the
    # recorded session.updated event is the accepted shape P2.11 mirrors.
    results.append(await probe_xai(
        "grok-voice-latest", probe="transcription_shape",
        session_patch=TRANSCRIPTION_PATCH,
    ))
    # Transcription-by-default: NO transcription opt-in in the session; send real
    # speech and record whether any input-transcription events arrive unprompted.
    asset = Path(args.speech_asset)
    audio = asset.read_bytes() if asset.exists() else None
    r = await probe_xai(
        "grok-voice-latest", probe="transcription_default",
        session_patch=SERVER_VAD_KNOBS, audio_pcm=audio, listen_s=12.0,
    )
    if audio is None:
        r.notes = (
            f"no speech asset at {asset} — transcription-by-default UNRESOLVED "
            "(run: python -m diagnostics.voice_probes.make_speech_asset); " + r.notes
        )
    results.append(r)
    return results


async def suite_gemini_tools(args) -> List[ProbeResult]:
    # DEPENDS ON P1.1 (update_sheet_values schema fix). Pre-fix this records the
    # known 1007 at properties[values].items.items. Requires the repo venv
    # (imports Orchestrator.tools.tool_registry) run from the repo root.
    from Orchestrator.tools.tool_registry import get_gemini_live_tools
    tools = get_gemini_live_tools("gemini_live")
    results = [await probe_gemini("gemini-3.1-flash-live-preview", probe="bare")]
    for m in ("gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"):
        results.append(await probe_gemini(m, probe="full_tools", tools=tools))
    return results


async def suite_translate(args) -> List[ProbeResult]:
    # Session shapes for the translation voice mode (Workstream 5 gate).
    # session.created's session object reveals the translate model's default
    # config fields; Gemini translate probed with AUDIO and with server-default
    # modalities (3.1 rejects TEXT — the translate model's tolerance is unknown).
    results = [await probe_openai("gpt-realtime-translate", probe="translate_handshake")]
    results.append(await probe_gemini(
        "gemini-3.5-live-translate-preview", probe="translate_minimal_audio",
    ))
    results.append(await probe_gemini(
        "gemini-3.5-live-translate-preview", probe="translate_no_modalities",
        response_modalities=None,
    ))
    return results


SUITES = {
    "openai-models": suite_openai_models,
    "xai": suite_xai,
    "gemini-tools": suite_gemini_tools,
    "translate": suite_translate,
}


async def _single(args) -> List[ProbeResult]:
    if args.provider == "openai":
        return [await probe_openai(args.model or "gpt-realtime-2.1")]
    if args.provider == "xai":
        return [await probe_xai(args.model)]
    return [await probe_gemini(
        args.model or "gemini-3.1-flash-live-preview", api_version=args.api_version,
    )]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m diagnostics.voice_probes.run")
    parser.add_argument("--provider", choices=["openai", "xai", "gemini"])
    parser.add_argument("--model", default="")
    parser.add_argument("--suite", choices=sorted(SUITES))
    parser.add_argument("--out", default="", help="results file name stem override")
    parser.add_argument("--api-version", default="v1beta",
                        help="gemini only: v1beta | v1alpha")
    parser.add_argument("--speech-asset", default=str(ASSETS / "speech_24k.pcm"))
    args = parser.parse_args(argv)

    if args.suite:
        results = asyncio.run(SUITES[args.suite](args))
        name = args.out or args.suite
    elif args.provider:
        results = asyncio.run(_single(args))
        name = args.out or (
            f"{args.provider}-{(args.model or 'default').replace('/', '_')}-adhoc"
        )
    else:
        parser.error("--provider or --suite required")

    for r in results:
        print(r.summary())
    print(f"results: {write_results(name, results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 3: Verify — import check, then one live handshake per provider**

Run (from repo root, service venv):

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -c "import diagnostics.voice_probes.run; print('import ok')"
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --provider gemini --model gemini-3.1-flash-live-preview
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --provider openai --model gpt-realtime-2
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --provider xai
```

Expected:
- `import ok`
- `gemini gemini-3.1-flash-live-preview handshake: OK` (bare 3.1 setup is probe-confirmed good — 2026-07-11 diagnosis)
- `openai gpt-realtime-2 handshake: OK` (the codebase's current known-good model)
- `xai (default) handshake: OK | resolved=<model id>` — the resolved id is the first real answer of this phase; note it.
- Each prints `results: diagnostics/voice_probes/results/2026-07-11-...-adhoc.json`. Confirm no key material: `grep -rE "sk-|AIza|xai-" diagnostics/voice_probes/results/ | grep -v REDACTED` → no output.

If any handshake FAILs here, STOP and debug the harness against that provider (systematic-debugging) before proceeding — P0.3–P0.7 all stand on these three functions.

**Step 4: Run the offline suite to confirm the tree stays green**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_probes_harness.py -q`
Expected: PASS (7 passed)

**Step 5: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/probes.py diagnostics/voice_probes/run.py
git commit -m "feat(diagnostics): live WS probes (openai/xai/gemini) + probe CLI with canned suites (P0.2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

Do NOT commit the `*-adhoc.json` scratch files from Step 3 (delete them or leave untracked).

---

### Task P0.3: OpenAI model-matrix probe run

**Files:**
- Create (generated): diagnostics/voice_probes/results/2026-07-11-openai-models.json
- No code changes — this is a probe-run task; test steps replaced by exact verification commands.

**Step 1: Run the suite**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --suite openai-models
```

Expected: 5 summary lines + `results: diagnostics/voice_probes/results/2026-07-11-openai-models.json`. Expected findings (RECORD whatever actually happens — divergence is data, not an error):
- `gpt-realtime-2.1`: OK — this is the P2 catalog default; if it FAILS, flag to Brandon immediately (blocks Workstream 2) but still record.
- `gpt-realtime-2.1-mini`: OK.
- `gpt-realtime-2025-08-28`: unknown — docs say valid, our May test saw close-4000. Either outcome resolves the contradiction; the recorded result decides whether P2 lists it.
- `gpt-live-1`, `gpt-live-1-mini`: FAIL expected (ChatGPT-only per research) — the recorded rejection shape (error event vs HTTP status) is the deliverable; P2 cites it when excluding gpt-live from the catalog.

**Step 2: Verify the results file**

```bash
Orchestrator/venv/bin/python -c "
import json
d = json.load(open('diagnostics/voice_probes/results/2026-07-11-openai-models.json'))
assert len(d['results']) == 5, len(d['results'])
for r in d['results']:
    print(r['model'], '->', 'OK' if r['ok'] else f\"FAIL close={r['close_code']} err={r['error'][:120]}\")
"
grep -cE "sk-[A-Za-z0-9]" diagnostics/voice_probes/results/2026-07-11-openai-models.json || echo "no key leakage"
```

Expected: 5 lines printed, `no key leakage` (grep finds nothing).

**Step 3: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/results/2026-07-11-openai-models.json
git commit -m "chore(diagnostics): P0 probe results — OpenAI realtime model matrix (2.1, 2.1-mini, 2025-08-28 re-probe, gpt-live rejection evidence)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P0.4: xAI probe run (default model, model ids, VAD knob round-trip, transcription default)

**Files:**
- Create: diagnostics/voice_probes/make_speech_asset.py
- Modify: .gitignore (append one line: `diagnostics/voice_probes/assets/`)
- Create (generated, NOT committed): diagnostics/voice_probes/assets/speech_24k.pcm
- Create (generated): diagnostics/voice_probes/results/2026-07-11-xai.json

**Step 1: Write the speech-asset generator**

The transcription probe needs real speech (silence/tones won't trip server_vad). Generate a ~4s clip as raw 24kHz s16le mono PCM — exactly xAI's default input format — via OpenAI TTS (`response_format=pcm` is 24kHz s16le mono). Best-effort: model ids tried in order because TTS naming has churned.

`diagnostics/voice_probes/make_speech_asset.py`:

```python
"""Generate assets/speech_24k.pcm (24kHz s16le mono) for transcription probes.

Uses OpenAI POST /v1/audio/speech with response_format=pcm. Best-effort:
tries model ids in order; exits 1 with a clear message if all fail (the xai
suite then marks transcription_default UNRESOLVED instead of crashing).
"""
import json
import sys
import urllib.request
from pathlib import Path

from diagnostics.voice_probes.env import get_key

ASSET = Path(__file__).resolve().parent / "assets" / "speech_24k.pcm"
MODELS = ["gpt-4o-mini-tts", "tts-1"]
TEXT = ("Testing, one, two, three. This is a transcription probe "
        "for the voice pipeline. The quick brown fox jumps over the lazy dog.")


def main() -> int:
    key = get_key("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY not set in service env")
        return 1
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps({"model": model, "voice": "alloy",
                             "input": TEXT, "response_format": "pcm"}).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
        )
        try:
            audio = urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            print(f"{model}: {type(e).__name__}: {e}")
            continue
        if len(audio) > 24000:  # > 0.5s of 24kHz s16le
            ASSET.write_bytes(audio)
            print(f"wrote {ASSET} ({len(audio)} bytes, model={model})")
            return 0
    print("all TTS model ids failed — transcription probe will run UNRESOLVED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Then: `echo "diagnostics/voice_probes/assets/" >> /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.gitignore`

**Step 2: Generate the asset**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -m diagnostics.voice_probes.make_speech_asset
```

Expected: `wrote .../assets/speech_24k.pcm (<N> bytes, model=...)` with N > 100000. If it exits 1, proceed anyway — the suite records `transcription_default` as UNRESOLVED with a note (acceptable degraded outcome; flag it in the commit message).

**Step 3: Run the xAI suite**

```bash
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --suite xai
```

Expected: 7 summary lines + `results: diagnostics/voice_probes/results/2026-07-11-xai.json`:
- `default_model_resolution`: OK with `resolved=<id>` — answers what no-`?model=` resolves to (this is what every production session has silently been using; feeds the P2 catalog + the "cosmetic grok-voice-agent label" removal).
- `grok-voice-latest`, `grok-voice-think-fast-1.0`: OK expected (both research-confirmed current).
- `server_vad_roundtrip`: OK; the answer is INSIDE the recorded `session.updated` event (Step 4).
- `input_rate_16k`: `"ok": true` means xAI ACCEPTED `audio.input.format.rate: 16000` (P2.15 executes Branch A); `"ok": false` (error event / close) means rejected (P2.15 executes Branch B). The echoed format is inside the recorded `session.updated` event (Step 4).
- `transcription_shape`: `"ok": true` means the explicit `audio.input.transcription: {}` opt-in is accepted; the echoed transcription object in the recorded `session.updated` event is the accepted shape P2.11 mirrors.
- `transcription_default`: OK; `event_types=` in notes reveals whether any input-transcription event type appears without opt-in (feeds WS2 "explicitly configure input transcription").

**Step 4: Extract the four findings from the results file**

```bash
Orchestrator/venv/bin/python -c "
import json
d = json.load(open('diagnostics/voice_probes/results/2026-07-11-xai.json'))

def _audio_input(e):
    return (((e.get('session') or {}).get('audio') or {}).get('input') or {})

for r in d['results']:
    if r['probe'] == 'server_vad_roundtrip':
        for e in r['events']:
            if e.get('type') == 'session.updated':
                print('turn_detection echoed as:',
                      json.dumps((e.get('session') or {}).get('turn_detection'), indent=2))
    if r['probe'] == 'input_rate_16k':
        print('input_rate_16k ok:', r['ok'], '| close:', r['close_code'], '| err:', r['error'][:120])
        for e in r['events']:
            if e.get('type') == 'session.updated':
                print('input format echoed as:', json.dumps(_audio_input(e).get('format')))
    if r['probe'] == 'transcription_shape':
        print('transcription_shape ok:', r['ok'])
        for e in r['events']:
            if e.get('type') == 'session.updated':
                print('accepted transcription shape:', json.dumps(_audio_input(e).get('transcription')))
    if r['probe'] == 'transcription_default':
        print('transcription_default event types:', r['notes'])
"
```

Expected: the echoed `turn_detection` block prints (if `threshold`/`prefix_padding_ms`/`silence_duration_ms` are absent or reset to defaults, xAI drops those knobs — record that conclusion in the commit message; it decides whether the phone-bridge VAD retune in WS2 is a no-op); `input_rate_16k ok: True` with the echoed input format (the P2.15 Branch A/B verdict); `transcription_shape ok: True` with the echoed transcription object (the shape P2.11 writes into session.update); and the transcription event-type list prints.

**Step 5: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/make_speech_asset.py .gitignore diagnostics/voice_probes/results/2026-07-11-xai.json
git commit -m "chore(diagnostics): P0 probe results — xAI default-model resolution, model ids, server_vad round-trip, 16k input-format round-trip, transcription shape + default (P0.4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

(Include the extracted findings — resolved default model, knob-echo verdict, 16k-rate verdict, accepted transcription shape, transcription-by-default verdict — as plain sentences in the commit body. P2.1/P2.11/P2.15 gate on the `ok` fields of these results.)

---

### Task P0.5: Translate-model session-shape probes (gpt-realtime-translate, gemini-3.5-live-translate-preview)

**Files:**
- Create (generated): diagnostics/voice_probes/results/2026-07-11-translate.json
- No code changes — probe-run task.

**Step 1: Run the suite**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --suite translate
```

Expected: 3 summary lines + results path. Outcomes are genuinely unknown (this is the Workstream 5 gate — "Translation models' wire shapes unverified"):
- `openai gpt-realtime-translate translate_handshake`: if OK, the recorded `session.created` event contains the model's default session object — the fields present there (e.g. anything language/translation-shaped) are exactly what P6 needs to build the target-language picker. If FAIL, record the rejection (translate may require `session.type: "transcription"` or a different connect shape — that finding scopes P6).
- `gemini gemini-3.5-live-translate-preview translate_minimal_audio` / `translate_no_modalities`: at least one accepting variant (setupComplete) OR two recorded rejections with close reasons. gemini-3.5-live-translate-preview is confirmed to exist in `models.list()` (2026-07-11 diagnosis, probe 3), so a rejection means the setup shape is wrong, not the model id — the close reason tells P6 what the model wants.

**Step 2: Verify the results file**

```bash
Orchestrator/venv/bin/python -c "
import json
d = json.load(open('diagnostics/voice_probes/results/2026-07-11-translate.json'))
assert len(d['results']) == 3, len(d['results'])
for r in d['results']:
    print(r['provider'], r['model'], r['probe'], '->',
          'OK' if r['ok'] else f\"FAIL close={r['close_code']} reason={r['close_reason'][:120]} err={r['error'][:120]}\")
"
```

Expected: 3 lines, each either OK or a FAIL with a captured close code/reason or error (an empty FAIL with no captured cause means the harness missed something — debug before committing).

**Step 3: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/results/2026-07-11-translate.json
git commit -m "chore(diagnostics): P0 probe results — translate-model session shapes (gpt-realtime-translate, gemini-3.5-live-translate-preview) gating WS5 (P0.5)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P0.6: probe_live pytest smoke suite (the harness's second face)

**Files:**
- Create: diagnostics/voice_probes/test_live_probes.py
- Test: the file itself (live, run explicitly by path — `pytest.ini` testpaths keeps it out of the default suite)

**Step 1: Write the live test suite**

`diagnostics/voice_probes/test_live_probes.py`:

```python
"""Live voice-probe smoke suite (design WS6 — replaces the voice-blind test_grok.sh).

NOT collected by the default run (pytest.ini: testpaths = Orchestrator/tests).
Run explicitly, from the repo root, whenever touching voice code or before any
catalog change:

    Orchestrator/venv/bin/python -m pytest diagnostics/voice_probes/test_live_probes.py -m probe_live -v

Makes real, paid API calls. Skips per-provider when the key is absent
(fresh-box gate: graceful degradation, no hard failure on an unconfigured box).
"""
import asyncio

import pytest

from diagnostics.voice_probes.env import get_key
from diagnostics.voice_probes.probes import probe_gemini, probe_openai, probe_xai

pytestmark = pytest.mark.probe_live


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.skipif(not get_key("OPENAI_API_KEY"), reason="OPENAI_API_KEY not in service env")
def test_openai_flagship_handshake():
    r = _run(probe_openai("gpt-realtime-2.1"))
    assert r.ok, r.summary()


@pytest.mark.skipif(not get_key("XAI_API_KEY"), reason="XAI_API_KEY not in service env")
def test_xai_default_handshake_resolves_model():
    r = _run(probe_xai(""))
    assert r.ok, r.summary()
    assert r.resolved_model, "session.created did not carry a model id"


@pytest.mark.skipif(not get_key("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not in service env")
def test_gemini_bare_setup_completes():
    r = _run(probe_gemini("gemini-3.1-flash-live-preview"))
    assert r.ok, r.summary()


@pytest.mark.skipif(not get_key("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not in service env")
@pytest.mark.xfail(
    reason="known 1007: update_sheet_values inner array lacks items — "
    "P1.1 fixes the schema; P1.10's verification step removes this xfail",
    strict=False,
)
def test_gemini_full_toolgroup_setup_completes():
    """The full gemini_live tool group must be accepted by setup (WS1 guard)."""
    from Orchestrator.tools.tool_registry import get_gemini_live_tools
    r = _run(probe_gemini(
        "gemini-3.1-flash-live-preview",
        tools=get_gemini_live_tools("gemini_live"),
    ))
    assert r.ok, r.summary()
```

Note for the P1 section author/executor: **P1.10's verification step OWNS removing this `xfail` marker** — it re-runs this file post-fix (P1.1 lands the schema fix; P1.10 restarts + live-verifies) and deletes the decorator once `test_gemini_full_toolgroup_setup_completes` passes. Cross-phase note; do not remove it now.

**Step 2: Run the live suite**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -m pytest diagnostics/voice_probes/test_live_probes.py -m probe_live -v
```

Expected: `3 passed, 1 xfailed` (all three keys are set on this box). If `test_openai_flagship_handshake` fails AND P0.3 recorded gpt-realtime-2.1 as rejected, change that test's model id to the newest OK model from `results/2026-07-11-openai-models.json` and flag to Brandon.

**Step 3: Verify the default suite still ignores it**

```bash
Orchestrator/venv/bin/python -m pytest --collect-only -q 2>/dev/null | grep -c test_live_probes || echo "not collected by default"
```

Expected: `not collected by default` (grep count 0 → non-zero exit → echo fires).

**Step 4: Run the offline harness tests once more (tree green)**
Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_probes_harness.py -q`
Expected: PASS (7 passed)

**Step 5: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/test_live_probes.py
git commit -m "test(diagnostics): probe_live pytest smoke suite — per-provider handshakes + xfailed full-toolgroup guard (P0.6)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P0.7: Gemini full tool-group probe run — ⚠ DEFERRED: execute AFTER P1.1

**⚠ DEPENDENCY:** This task must run AFTER P1.1 lands (the `ToolVault/tools/update_sheet_values/schema.json` inner-`items` fix). If executing this plan sequentially, SKIP this task now and return to it immediately after P1.1's commit. Pre-fix, the run only reproduces the already-documented 1007 (`function_declarations[53].parameters.properties[values].items.items: missing field`). This is design WS1 item 8: the post-fix probe must pass on BOTH models so a *second* latent schema violation (which would fail at the next index) is caught before the service is declared rescued.

**Files:**
- Create (generated): diagnostics/voice_probes/results/2026-07-11-gemini-tools.json (date stamp will be the actual run date — fine)
- No code changes — probe-run task.

**Step 1: Confirm P1.1 has landed**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -c "
import json
v = json.load(open('ToolVault/tools/update_sheet_values/schema.json'))['parameters']['properties']['values']
assert 'items' in v.get('items', {}), 'P1.1 NOT landed — inner array still lacks items; STOP and run P1.1 first'
print('P1.1 landed:', v)
"
```

Expected: `P1.1 landed: {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}}}` (or equivalent with an inner `items`). If the assert fires, STOP — do not run the suite.

**Step 2: Run the suite**

```bash
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --suite gemini-tools
```

This exact invocation (`python -m diagnostics.voice_probes.run --suite gemini-tools`, repo root, Orchestrator venv) is the STABLE entry point that P1.10's live-verification step re-runs — do not rename the suite or move the module.

Expected: 3 summary lines + results path, ALL OK:
- `gemini gemini-3.1-flash-live-preview bare: OK` (control — isolates any failure to the tools payload)
- `gemini gemini-3.1-flash-live-preview full_tools: OK` with `56 functionDeclarations` (or the current count) in notes
- `gemini gemini-2.5-flash-native-audio-latest full_tools: OK`

If either `full_tools` probe FAILs with a 1007 at a *different* `function_declarations[N]` index: that is the predicted second latent schema violation — identify the tool at that index, report it as a new P1 work item (another schema fix + it must be caught by P1's recursive validator rule), and re-run this task after that fix. Do not commit a failing result as final.

**Step 3: Verify + re-run the live pytest guard**

```bash
Orchestrator/venv/bin/python -c "
import json, glob
p = sorted(glob.glob('diagnostics/voice_probes/results/*-gemini-tools.json'))[-1]
d = json.load(open(p))
assert all(r['ok'] for r in d['results']), [r['summary'] if False else (r['probe'], r['close_reason'], r['error']) for r in d['results'] if not r['ok']]
print('all', len(d['results']), 'gemini probes OK:', p)
"
Orchestrator/venv/bin/python -m pytest diagnostics/voice_probes/test_live_probes.py -m probe_live -v -k gemini
```

Expected: `all 3 gemini probes OK: ...`; pytest shows `test_gemini_bare_setup_completes PASSED` and `test_gemini_full_toolgroup_setup_completes` as `XPASS` (or `PASSED` if P1.10's verification step already removed the xfail per P0.6's cross-phase note).

**Step 4: Commit**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
git add diagnostics/voice_probes/results/*-gemini-tools.json
git commit -m "chore(diagnostics): P0 probe results — full gemini_live tool-group setup accepted on 3.1-preview AND 2.5-latest post-P1.1 (WS1 item 8) (P0.7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

## Phase 1a — Gemini rescue

**Goal:** every Gemini Live session has been dead since 2026-06-20 (WS close 1007 from one invalid tool schema, silenced by a reconnect loop that never respawns its listener). This phase fixes the root cause, makes the failure class impossible to reintroduce silently, and rebuilds the reconnect/transcription path. Root cause and line references are from the 2026-07-11 recon (`scratchpad/recon/geminiCode.json`, `geminiDiag.json`, `geminiResearch.json`) and were re-verified against the working tree.

**Conventions for every task below:**
- Repo root: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc` — run everything from there.
- Pytest: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest <file> -x -q`
- Line numbers are as of the 2026-07-11 working tree and **drift as tasks land** — anchor on the named function/symbol, not the number.
- The service runs LIVE from this tree: every task must leave the tree importable. The running *process* keeps its old code until the single restart in P1.10.
- **Gemini only.** `grok_live_routes.py` and `realtime_routes.py` have the same frozen-tool-snapshot and listener-respawn defects — they are fixed with the identical pattern in the cross-cutting hardening phase. Do NOT touch them here.

---

### Task P1.1: update_sheet_values schema — inner `items` for the 2D values array

**Files:**
- Modify: ToolVault/tools/update_sheet_values/schema.json:18-22, 32

Pure-config task (the executable regression guard lands in P1.2) — verification commands replace test steps.

**Step 1: Fix the schema**

The executor (`ToolVault/tools/update_sheet_values/executor.py:20-23`) only requires `values` to be a list and passes rows straight to the Sheets API with USER_ENTERED parsing — so `{"type": "string"}` is the correct inner cell type: strings round-trip everything (Sheets coerces `"42"` → number, `"=B2*1.1"` → formula, `"5/1/2026"` → date), and Google's Live API validator requires *some* `items` on every array.

Replace the `values` property (schema.json lines 18-22):

```json
      "values": {
        "type": "array",
        "items": {
          "type": "array",
          "items": {"type": "string"}
        },
        "description": "A 2D array of cell values: an array of rows, each row an array of cell strings. Send every cell as a string — Sheets USER_ENTERED parsing coerces it (\"42\" becomes a number, \"=B2*1.1\" a formula, \"5/1/2026\" a date). E.g. [[\"Name\", \"Total\"], [\"Widget\", \"=B2*1.1\"]]."
      },
```

And update the `example` field (line 32) to match the declared type:

```json
  "example": "update_sheet_values(spreadsheet_id=\"1AbC...\", range=\"Sheet1!A1\", values=[[\"Name\", \"Total\"], [\"Widget\", \"42\"]])",
```

**Step 2: Verify the schema shape**

Run: `Orchestrator/venv/bin/python -c "import json; v=json.load(open('ToolVault/tools/update_sheet_values/schema.json'))['parameters']['properties']['values']; assert v['items'] == {'type': 'array', 'items': {'type': 'string'}}, v; print('inner items OK')"`
Expected: `inner items OK`

**Step 3: Verify the module still validates**

Run: `Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate; echo "exit=$?"`
Expected: `ToolVault validation: OK` ... `exit=0`

**Step 4: Reload ToolVault (live chat injector picks up the fix; NO restart yet)**

Run: `curl -sf -X POST http://localhost:9091/toolvault/reload`
Expected: JSON success response. NOTE: the live *voice* route still holds the import-time `GEMINI_LIVE_TOOLS` snapshot in the running process — that is unfrozen in P1.3 and the process restarts in P1.10. Do not manually re-mint or restart here.

**Step 5: Commit**

```bash
git add ToolVault/tools/update_sheet_values/schema.json
git commit -m "fix(toolvault): update_sheet_values 2D values array declares inner items — Gemini Live 1007 root cause"
```

---

### Task P1.2: ToolVault validator — recursive array-items rule + all-modules regression scan

**Files:**
- Modify: Orchestrator/toolvault/validate.py:59-81 (add helper after `_cli_agent_mcp_group_errors`), :136-138 (wire into `validate_all`)
- Test: Orchestrator/tests/test_toolvault_array_items.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_toolvault_array_items.py`:

```python
"""P1.2 — every declared array parameter carries an `items` schema.

Root cause 2026-07-11: update_sheet_values declared values as array-of-array
with NO inner items; Google's BidiGenerateContent setup validator rejected the
ENTIRE 56-tool setup with WS close 1007, killing every Gemini Live session
since 2026-06-20 (ff43d8b). This rule turns that class of regression into a
CI failure instead of a silent voice outage.
"""
from Orchestrator.toolvault import validate


def _tool(params: dict) -> dict:
    return {"name": "t", "parameters": params}


def test_outer_array_without_items_is_flagged():
    errors = validate._array_items_errors(_tool({
        "type": "object",
        "properties": {"xs": {"type": "array"}},
    }))
    assert len(errors) == 1
    assert "parameters.properties.xs" in errors[0]


def test_2d_array_missing_inner_items_is_flagged():
    # The EXACT pre-fix update_sheet_values shape.
    errors = validate._array_items_errors(_tool({
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "array"}},
        },
    }))
    assert len(errors) == 1
    assert "parameters.properties.values.items" in errors[0]


def test_complete_2d_array_is_clean():
    errors = validate._array_items_errors(_tool({
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
    }))
    assert errors == []


def test_non_dict_and_missing_parameters_never_raise():
    assert validate._array_items_errors(None) == []
    assert validate._array_items_errors({"name": "t"}) == []
    assert validate._array_items_errors({"name": "t", "parameters": "nope"}) == []


def test_all_toolvault_modules_declare_array_items():
    """Regression scan over EVERY real module folder (the CI gate)."""
    report = validate.validate_all()
    offenders = {
        folder: [m for m in msgs if "lacks required 'items'" in m]
        for folder, msgs in report["errors"].items()
    }
    offenders = {f: msgs for f, msgs in offenders.items() if msgs}
    assert offenders == {}, f"array params missing items: {offenders}"
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_toolvault_array_items.py -x -q`
Expected: FAIL with `AttributeError: module 'Orchestrator.toolvault.validate' has no attribute '_array_items_errors'`

**Step 3: Write minimal implementation**

In `Orchestrator/toolvault/validate.py`, insert after `_cli_agent_mcp_group_errors` (after line 81), following the existing return-list-of-error-strings / never-raises helper convention:

```python
def _array_items_errors(data) -> list:
    """Guard: every ``"type": "array"`` node anywhere in ``parameters`` MUST
    carry an ``items`` schema (a dict).

    Google's Gemini Live BidiGenerateContent setup validator rejects the ENTIRE
    multi-tool setup with WS close 1007 (``...items: missing field``) when any
    declared array lacks ``items`` — one bad tool kills every voice session for
    every operator, silently (2026-07-11 root cause: update_sheet_values' 2D
    ``values`` param, dead since 2026-06-20). This check walks the whole
    parameters tree (properties, items, nested anything) so N-dimensional
    arrays are covered.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict):
        return []
    params = data.get("parameters")
    if not isinstance(params, dict):
        return []
    errors: list = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "array" and not isinstance(node.get("items"), dict):
                errors.append(
                    f"array at {path} lacks required 'items' schema — Gemini Live "
                    f"rejects the whole tool setup with WS close 1007"
                )
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(params, "parameters")
    return errors
```

Wire it into `validate_all()` directly after the `_cli_agent_mcp_group_errors` extend (line ~138):

```python
            # Every declared array MUST carry an `items` schema — Gemini Live's
            # setup validator rejects the whole tool payload otherwise (P1.2).
            folder_errors.extend(_array_items_errors(data))
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_toolvault_array_items.py -x -q && Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate`
Expected: 5 passed; `ToolVault validation: OK` (exit 0 — proves the P1.1 fix left zero remaining violations across all modules)

**Step 5: Commit**

```bash
git add Orchestrator/toolvault/validate.py Orchestrator/tests/test_toolvault_array_items.py
git commit -m "feat(toolvault): CI-gate rejects any array parameter lacking items (recursive walk, all modules)"
```

---

### Task P1.3: Un-freeze GEMINI_LIVE_TOOLS — read the tool group fresh at configure time

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py:94-98 (delete the import-time snapshot), :442 (`"tools":` inside `configure_gemini_session`'s `setup_config`)
- Test: Orchestrator/tests/test_gemini_live_tools_fresh.py

NOTE: `GROK_LIVE_TOOLS` / `REALTIME_TOOLS` get the identical change in the hardening phase — gemini ONLY here.

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_live_tools_fresh.py`:

```python
"""P1.3 — the gemini_live tool list is read FRESH at configure time.

GEMINI_LIVE_TOOLS was an import-time snapshot (old line 98): /toolvault/reload
never reached live voice sessions, so the 2026-06-20 schema regression (and its
fix!) required a full service restart to even take effect. Pin: each
configure_gemini_session call pulls get_gemini_live_tools("gemini_live") anew,
and the frozen module constant is gone.

grok/openai routes get the same change in the hardening phase — this file
intentionally covers ONLY gemini.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr


@pytest.fixture
def no_fossils(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(glr, "build_fossil_context", _stub)


def _make_session():
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.voice = ""
    # Inert values for the P1.4 session-persisted config (MagicMock attrs are
    # truthy and would hijack the None-fallbacks once P1.4 lands).
    session.model = None
    session.vad_sensitivity_start = None
    session.vad_sensitivity_end = None
    session.thinking_level = None
    session.custom_role = ""
    session.phone_mode = False
    return session


def test_frozen_snapshot_is_gone():
    assert not hasattr(glr, "GEMINI_LIVE_TOOLS"), (
        "GEMINI_LIVE_TOOLS import-time snapshot must not come back — it is why "
        "the 2026-06-20 schema regression needed a restart to even diagnose"
    )


@pytest.mark.asyncio
async def test_configure_reads_tools_fresh_each_call(no_fossils, monkeypatch):
    calls = []

    def fake_get_tools(group):
        calls.append(group)
        return [{"functionDeclarations": [{"name": f"tool_v{len(calls)}"}]}]

    monkeypatch.setattr(glr, "get_gemini_live_tools", fake_get_tools)

    for expected in ("tool_v1", "tool_v2"):
        session = _make_session()
        await glr.configure_gemini_session(session, "test_operator", "Orus")
        payload = json.loads(session.gemini_ws.send.await_args.args[0])
        names = [
            fd["name"]
            for t in payload["setup"]["tools"]
            for fd in t["functionDeclarations"]
        ]
        assert names == [expected]

    assert calls == ["gemini_live", "gemini_live"]
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_tools_fresh.py -x -q`
Expected: FAIL — `test_frozen_snapshot_is_gone` AssertionError (the module attribute exists)

**Step 3: Write minimal implementation**

In `Orchestrator/routes/gemini_live_routes.py`, delete lines 94-98:

```python
# =============================================================================
# Tool Definitions for Gemini Live
# =============================================================================

GEMINI_LIVE_TOOLS = get_gemini_live_tools("gemini_live")
```

and in `configure_gemini_session`'s `setup_config` (line 442) replace:

```python
        "tools": GEMINI_LIVE_TOOLS,
```

with:

```python
        # P1.3 — read the tool group FRESH per session-configure so
        # /toolvault/reload (and schema fixes) reach live voice without a
        # restart. The registry's mtime cache makes this cheap. grok/openai
        # routes get the same un-freeze in the hardening phase.
        "tools": get_gemini_live_tools("gemini_live"),
```

(`get_gemini_live_tools` is already imported at line 79 — keep it.)

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_tools_fresh.py Orchestrator/tests/test_live_models.py -q`
Expected: PASS (all — test_live_models pins the rest of the setup payload shape)

**Step 5: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_live_tools_fresh.py
git commit -m "fix(gemini-live): read gemini_live tool group fresh at configure time — /toolvault/reload now reaches voice sessions"
```

---

### Task P1.4: Persist model/VAD/thinking/custom_role/phone_mode on GeminiLiveSession

**Files:**
- Modify: Orchestrator/models.py:131-138 (GeminiLiveSession "Reconnection state" block)
- Modify: Orchestrator/routes/gemini_live_routes.py:215-225 (signature), :256-258 (fallback block after the `gemini_ws` guard), :498-501 (persist after send)
- Modify: Orchestrator/tests/test_live_models.py:68-77 (`_make_gemini_session` gets inert new-field defaults)
- Create: Orchestrator/tests/gemini_live_fakes.py (shared fakes for P1.4-P1.8)
- Test: Orchestrator/tests/test_gemini_live_reconnect_config.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/gemini_live_fakes.py`:

```python
"""Shared fakes for the Gemini Live pytest suites (Phase 1a).

Real GeminiLiveSession dataclass + minimal async fakes for both websocket ends.
FakePortalWS satisfies _safe_ws_send's CONNECTED check and records every frame.
"""
from unittest.mock import AsyncMock

from starlette.websockets import WebSocketState

from Orchestrator.models import GeminiLiveSession


class FakePortalWS:
    def __init__(self):
        self.application_state = WebSocketState.CONNECTED
        self.sent: list = []
        self.closed: list = []

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED

    def frames(self, type_):
        return [f for f in self.sent if f.get("type") == type_]


class FakeGeminiWS:
    """Async-iterable fake of the upstream websockets connection.

    ``messages`` are yielded in order; ``closing_exc`` (if set) is raised after
    they are exhausted — simulating a WS close frame mid-listen.
    """
    def __init__(self, messages=None, closing_exc=None):
        self._messages = list(messages or [])
        self._closing_exc = closing_exc
        self.send = AsyncMock()
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._closing_exc is not None:
            raise self._closing_exc
        raise StopAsyncIteration


def make_session(**overrides) -> GeminiLiveSession:
    session = GeminiLiveSession(session_id="test-session", operator="test_operator")
    session.portal_ws = FakePortalWS()
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def stub_fossil_context(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub
    )
```

Create `Orchestrator/tests/test_gemini_live_reconnect_config.py`:

```python
"""P1.4 — session config survives the reconnect reconfigure.

gemini_reconnect calls configure_gemini_session(session, operator, voice) with
no model/VAD/thinking/custom_role/phone_mode kwargs. Before P1.4 that reverted
the session to the default model with default VAD/thinking and dropped any
outbound-call custom role (recon finding #5). Pin: (a) configure persists the
validated config onto the session; (b) the EXACT bare reconnect call shape
re-emits the same setup payload extensions.
"""
import json

import pytest

from Orchestrator.routes.gemini_live_routes import configure_gemini_session
from Orchestrator.tests.gemini_live_fakes import (
    FakeGeminiWS,
    make_session,
    stub_fossil_context,
)


@pytest.fixture
def no_fossils(monkeypatch):
    stub_fossil_context(monkeypatch)


def _last_setup(ws: FakeGeminiWS) -> dict:
    return json.loads(ws.send.await_args.args[0])["setup"]


@pytest.mark.asyncio
async def test_configure_persists_config_on_session(no_fossils):
    session = make_session(gemini_ws=FakeGeminiWS())

    await configure_gemini_session(
        session,
        "test_operator",
        "Orus",
        model="gemini-3.1-flash-live-preview",
        vad_sensitivity_start="LOW",
        vad_sensitivity_end="HIGH",
        thinking_level="low",
        phone_mode=False,
    )

    assert session.model == "gemini-3.1-flash-live-preview"
    assert session.vad_sensitivity_start == "LOW"
    assert session.vad_sensitivity_end == "HIGH"
    assert session.thinking_level == "low"
    assert session.custom_role == ""
    assert session.phone_mode is False


@pytest.mark.asyncio
async def test_bare_reconfigure_reuses_persisted_config(no_fossils):
    session = make_session(gemini_ws=FakeGeminiWS())

    await configure_gemini_session(
        session,
        "test_operator",
        "Orus",
        model="gemini-3.1-flash-live-preview",
        vad_sensitivity_start="LOW",
        vad_sensitivity_end="HIGH",
        thinking_level="low",
    )

    # Second configure: the EXACT call shape gemini_reconnect uses.
    session.gemini_ws = FakeGeminiWS()
    await configure_gemini_session(session, session.operator, session.voice)

    setup = _last_setup(session.gemini_ws)
    assert setup["model"] == "models/gemini-3.1-flash-live-preview"
    aad = setup["realtimeInputConfig"]["automaticActivityDetection"]
    assert aad["startOfSpeechSensitivity"] == "START_SENSITIVITY_LOW"
    assert aad["endOfSpeechSensitivity"] == "END_SENSITIVITY_HIGH"
    assert setup["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_reconnect_config.py -x -q`
Expected: FAIL with `AttributeError: 'GeminiLiveSession' object has no attribute 'model'`

**Step 3: Write minimal implementation**

**(a)** `Orchestrator/models.py` — inside `GeminiLiveSession`, after `intentional_disconnect` (line 137), add:

```python
    # Persisted session config (P1.4) — configure_gemini_session writes the
    # validated values here and falls back to them when a caller passes None,
    # so gemini_reconnect's bare (session, operator, voice) reconfigure no
    # longer reverts model/VAD/thinking/custom_role/phone_mode to defaults.
    model: Optional[str] = None                  # Resolved model id (None until first configure)
    vad_sensitivity_start: Optional[str] = None  # "LOW" | "MEDIUM" | "HIGH"
    vad_sensitivity_end: Optional[str] = None    # "LOW" | "MEDIUM" | "HIGH"
    thinking_level: Optional[str] = None         # "minimal" | "low" | "medium" | "high"
    custom_role: str = ""                        # Outbound-call persona override
    phone_mode: bool = False                     # Phone-tuned server VAD
```

**(b)** `Orchestrator/routes/gemini_live_routes.py` — `configure_gemini_session` signature (lines 218-220): change

```python
    custom_role: str = "",
    phone_mode: bool = False,
```

to

```python
    custom_role: Optional[str] = None,
    phone_mode: Optional[bool] = None,
```

**(c)** Immediately after the `if not session.gemini_ws: return` guard (lines 256-257), insert:

```python
    # P1.4 — fall back to session-persisted values. None means "not specified
    # by this caller": gemini_reconnect passes only (session, operator, voice),
    # so before this fallback every reconnect silently reverted the session to
    # the default model with default VAD/thinking and dropped custom_role/
    # phone_mode (recon finding #5). Explicit values (including "" / False)
    # still win over the persisted ones — phone bridge call sites unchanged.
    if model is None:
        model = session.model
    if vad_sensitivity_start is None:
        vad_sensitivity_start = session.vad_sensitivity_start
    if vad_sensitivity_end is None:
        vad_sensitivity_end = session.vad_sensitivity_end
    if thinking_level is None:
        thinking_level = session.thinking_level
    if custom_role is None:
        custom_role = session.custom_role
    if phone_mode is None:
        phone_mode = session.phone_mode
```

**(d)** At the function tail (currently lines 498-501), after `session.voice = voice`, add:

```python
    # P1.4 — persist the validated config so the reconnect path reconfigures
    # with exactly what this session was opened with.
    session.model = resolved_model
    session.vad_sensitivity_start = vad_sensitivity_start
    session.vad_sensitivity_end = vad_sensitivity_end
    session.thinking_level = thinking_level
    session.custom_role = custom_role
    session.phone_mode = phone_mode
```

**(e)** `Orchestrator/tests/test_live_models.py` — in `_make_gemini_session()` (lines 68-77), add before `return session` (MagicMock attributes are truthy and would hijack the new None-fallbacks):

```python
    session.model = None
    session.vad_sensitivity_start = None
    session.vad_sensitivity_end = None
    session.thinking_level = None
    session.custom_role = ""
    session.phone_mode = False
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_reconnect_config.py Orchestrator/tests/test_live_models.py Orchestrator/tests/test_gemini_live_tools_fresh.py -q`
Expected: PASS (all)

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/gemini_live_fakes.py Orchestrator/tests/test_gemini_live_reconnect_config.py Orchestrator/tests/test_live_models.py
git commit -m "fix(gemini-live): persist model/VAD/thinking/custom_role/phone_mode on the session — reconnect stops reverting config"
```

---

### Task P1.5: gemini_reconnect respawns the listener; reconnect counter = consecutive failures

**Files:**
- Modify: Orchestrator/models.py (GeminiLiveSession — add `listener_task` next to the reconnection-state fields)
- Modify: Orchestrator/routes/gemini_live_routes.py — `handle_gemini_message` setupComplete branch (:762-770), `gemini_reconnect` success branch (:1384-1409), WS endpoint local-var/spawn/teardown (:1578, :1664-1666, :1710-1717)
- Test: Orchestrator/tests/test_gemini_live_reconnect_respawn.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_live_reconnect_respawn.py`:

```python
"""P1.5 — reconnect respawns the Gemini listener; counter = consecutive failures.

Recon finding #1 (THE months-of-flakiness bug): gemini_listener was spawned
exactly once at WS connect; gemini_reconnect re-dialed and re-sent setup but
NOTHING ever read the new socket — a permanently mute session that reported
"reconnected", looping forever because reconnect_count reset to 0 on every
"success". Pattern ported from phone/bridge.py:_gemini_listener_with_reconnect.

Pins: (a) a successful gemini_reconnect spawns a NEW gemini_listener task;
(b) reconnect_count is NOT reset by gemini_reconnect itself; (c) setupComplete
(proof a listener READ the new socket) resets it.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import FakeGeminiWS, make_session


@pytest.mark.asyncio
async def test_successful_reconnect_respawns_listener_and_keeps_count(monkeypatch):
    session = make_session()
    session.gemini_ws = None  # old socket already gone

    async def fake_connect(sess):
        sess.gemini_ws = FakeGeminiWS()
        sess.status = "connected"
        return True

    spawned = []

    async def fake_listener(sess):
        spawned.append(sess)

    monkeypatch.setattr(glr, "connect_to_gemini", fake_connect)
    monkeypatch.setattr(glr, "configure_gemini_session", AsyncMock())
    monkeypatch.setattr(glr, "gemini_listener", fake_listener)

    await glr.gemini_reconnect(session)
    await asyncio.sleep(0)  # let the spawned listener task run

    assert spawned == [session], "reconnect must spawn a fresh gemini_listener"
    assert session.listener_task is not None
    assert session.reconnect_count == 1, (
        "reconnect_count must NOT reset on 'setup sent' — only setupComplete "
        "(a real read from the new socket) may reset it"
    )
    assert session.is_reconnecting is False
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_setup_complete_resets_failure_count():
    session = make_session()
    session.reconnect_count = 3

    await glr.handle_gemini_message(session, {"setupComplete": {}})

    assert session.reconnect_count == 0
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_reconnect_respawn.py -x -q`
Expected: FAIL — first test AssertionError `spawned == [session]` (old code never respawns); second test AssertionError (count stays 3)

**Step 3: Write minimal implementation**

**(a)** `Orchestrator/models.py` — in `GeminiLiveSession`, next to `is_reconnecting`:

```python
    listener_task: Optional[Any] = None        # CURRENT gemini_listener asyncio.Task — respawned by gemini_reconnect, cancelled at WS teardown (P1.5)
```

**(b)** `handle_gemini_message` setupComplete branch (lines 762-770) — replace with:

```python
    # Check for setupComplete
    if "setupComplete" in event:
        print(f"[GEMINI-LIVE] Session setup complete")
        # A listener just READ from the current Gemini socket — the connection
        # is genuinely healthy. Reset the consecutive-failure counter HERE, not
        # in gemini_reconnect: "setup sent" is not health — a rejected setup
        # (e.g. WS close 1007) closes the socket right after, and resetting on
        # send made max_reconnects unreachable (infinite mute-reconnect cycle).
        session.reconnect_count = 0
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "setup_complete",
                "data": event.get("setupComplete", {})
            })
        return
```

**(c)** `gemini_reconnect` success branch (lines 1384-1409) — replace with:

```python
        # Reconnect
        if await connect_to_gemini(session):
            # Reconfigure — configure_gemini_session falls back to the session-
            # persisted model/VAD/thinking/custom_role/phone_mode (P1.4), so
            # this bare call no longer reverts the session to defaults.
            await configure_gemini_session(session, session.operator, session.voice)

            # Re-emit provenance after reconfigure so client UI stays in sync with the
            # newly-rebuilt system context (see Task 3 code review).
            if session.provenance:
                await _safe_ws_send(session.portal_ws, {
                    "type": "provenance",
                    "data": session.provenance
                })

            # CRITICAL (recon finding #1): respawn the listener. The previous
            # gemini_listener's `async for` was bound to the OLD closed socket
            # and has already exited — without a new task NOTHING ever reads
            # from the new connection (setupComplete, audio, tool calls pile
            # up unread) while the client is told "reconnected". Pattern
            # ported from phone/bridge.py:_gemini_listener_with_reconnect.
            if session.listener_task and not session.listener_task.done():
                session.listener_task.cancel()
            session.listener_task = asyncio.create_task(gemini_listener(session))

            # NOTE: reconnect_count is NOT reset here — see setupComplete in
            # handle_gemini_message (consecutive-failure semantics).
            session.is_reconnecting = False
            session.last_ai_message_time = time.time()
            session.status = "connected"

            print(f"[GEMINI-LIVE] Reconnected successfully on attempt {attempt}")

            # Notify Portal
            await _safe_ws_send(session.portal_ws, {
                "type": "reconnected",
                "data": {"attempt": attempt}
            })
```

**(d)** WS endpoint — line 1578: delete `gemini_task = None` (keep `keepalive_task = None`). Lines 1664-1666: replace the spawn with:

```python
                    # Start Gemini listener task and keepalive. The listener
                    # task lives on the SESSION (session.listener_task): the
                    # reconnect path respawns it, so a local variable here
                    # would go stale after the first reconnect and leak the
                    # live task at teardown.
                    session.listener_task = asyncio.create_task(gemini_listener(session))
                    keepalive_task = asyncio.create_task(gemini_keepalive_loop(session))
```

Teardown `finally` block (lines 1710-1717) — replace the `if gemini_task:` cancel block with:

```python
        # Cleanup — cancel whichever listener task is CURRENT (reconnects
        # respawn it; see session.listener_task).
        if session.listener_task:
            session.listener_task.cancel()
            try:
                await session.listener_task
            except asyncio.CancelledError:
                pass
            session.listener_task = None
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_reconnect_respawn.py Orchestrator/tests/test_gemini_live_reconnect_config.py Orchestrator/tests/test_live_models.py -q`
Expected: PASS (all)

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_live_reconnect_respawn.py
git commit -m "fix(gemini-live): respawn gemini_listener on reconnect; reconnect counter = consecutive failures (reset only on setupComplete)"
```

---

### Task P1.6: Honor goAway.timeLeft — graceful pre-deadline reconnect

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py — new `_goaway_delay_seconds` helper + constant above `gemini_reconnect` (~line 1339), goAway branch in `handle_gemini_message` (:1316-1321)
- Test: Orchestrator/tests/test_gemini_live_goaway.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_live_goaway.py`:

```python
"""P1.6 — goAway.timeLeft is honored: graceful reconnect BEFORE the deadline.

Google warns via goAway {timeLeft} ~before the ~10-min connection cut. The old
code reconnected IMMEDIATELY on goAway (throwing away the remaining window);
the fix schedules the reconnect for (timeLeft - margin), falling back to
immediate when the field is missing/unparseable (= old behavior).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import make_session


def test_goaway_delay_parsing():
    f = glr._goaway_delay_seconds
    assert f({"timeLeft": "10s"}) == pytest.approx(8.0)   # 2s safety margin
    assert f({"timeLeft": "9.5s"}) == pytest.approx(7.5)
    assert f({"timeLeft": "1s"}) == 0.0                    # floored at 0
    assert f({"timeLeft": {"seconds": 5}}) == pytest.approx(3.0)
    assert f({}) == 0.0                                    # missing -> immediate
    assert f({"timeLeft": "garbage"}) == 0.0               # unparseable -> immediate


@pytest.mark.asyncio
async def test_goaway_zero_timeleft_reconnects_immediately(monkeypatch):
    session = make_session()
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)

    await glr.handle_gemini_message(session, {"goAway": {"timeLeft": "0s"}})
    await asyncio.sleep(0.05)

    reconnect.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_goaway_long_timeleft_defers_reconnect(monkeypatch):
    session = make_session()
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)

    await glr.handle_gemini_message(session, {"goAway": {"timeLeft": "600s"}})
    await asyncio.sleep(0.05)

    reconnect.assert_not_awaited()  # scheduled ~598s out, not fired now

    # Cancel the deferred task so the loop closes clean.
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_goaway.py -x -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_goaway_delay_seconds'`

**Step 3: Write minimal implementation**

**(a)** Above `gemini_reconnect` (under the "Reconnection and Keepalive" banner, ~line 1338), add:

```python
GOAWAY_RECONNECT_MARGIN_SEC = 2.0  # reconnect this many seconds BEFORE Google's stated deadline


def _goaway_delay_seconds(goaway: dict) -> float:
    """Seconds to wait before the graceful pre-deadline reconnect.

    ``goAway.timeLeft`` is a protobuf Duration, JSON-encoded as a string like
    "10s" / "9.5s" (a {"seconds": N, "nanos": M} dict is tolerated for safety).
    Returns max(0, timeLeft - margin); 0 (reconnect immediately — the pre-P1.6
    behavior) when the field is missing or unparseable. Never raises.
    """
    time_left = (goaway or {}).get("timeLeft")
    seconds = 0.0
    try:
        if isinstance(time_left, str) and time_left.endswith("s"):
            seconds = float(time_left[:-1])
        elif isinstance(time_left, dict):
            seconds = float(time_left.get("seconds", 0)) + float(time_left.get("nanos", 0)) / 1e9
    except (TypeError, ValueError):
        seconds = 0.0
    return max(0.0, seconds - GOAWAY_RECONNECT_MARGIN_SEC)
```

**(b)** Replace the goAway branch in `handle_gemini_message` (lines 1316-1321) with:

```python
    # Check for goAway (server will drop this connection at timeLeft — schedule
    # a graceful reconnect BEFORE the deadline instead of waiting for the cut)
    if "goAway" in event:
        goaway = event.get("goAway") or {}
        delay = _goaway_delay_seconds(goaway)
        print(f"[GEMINI-LIVE] Server sending goAway (timeLeft={goaway.get('timeLeft')!r}) - graceful reconnect in {delay:.1f}s")
        if not session.intentional_disconnect and not session.is_reconnecting:
            async def _goaway_reconnect():
                if delay > 0:
                    await asyncio.sleep(delay)
                if not session.intentional_disconnect and not session.is_reconnecting:
                    await gemini_reconnect(session)
            asyncio.create_task(_goaway_reconnect())
        return
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_goaway.py Orchestrator/tests/test_gemini_live_reconnect_respawn.py -q`
Expected: PASS (all)

**Step 5: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_live_goaway.py
git commit -m "feat(gemini-live): honor goAway.timeLeft — graceful reconnect before Google's deadline"
```

---

### Task P1.7: Forward WS close code/reason to the client; terminal disconnect CLOSES the portal WS

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py — new `_close_portal_ws` helper after `_safe_ws_send` (:84-92), `gemini_reconnect` give-up branch (:1348-1357), `gemini_listener` (:1481-1507)
- Test: Orchestrator/tests/test_gemini_live_close_forwarding.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_live_close_forwarding.py`:

```python
"""P1.7 — silence kill: WS close code/reason reach the client; terminal
disconnect CLOSES the portal WS.

Silence layer 1 of the 2026-07-11 outage: Google rejects a bad setup by
CLOSING the socket with a code/reason (1007 'items: missing field'); the old
listener printed it and reconnected with the same broken setup while the
portal WS sat open answering pings ("Connected — listening" forever).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import FakeGeminiWS, make_session


@pytest.mark.asyncio
async def test_close_code_and_reason_forwarded_as_error(monkeypatch):
    exc = ConnectionClosedError(
        Close(1007, "function_declarations[53]...items: missing field"), None
    )
    session = make_session(gemini_ws=FakeGeminiWS(closing_exc=exc))
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)

    await glr.gemini_listener(session)
    await asyncio.sleep(0)

    errors = session.portal_ws.frames("error")
    assert len(errors) == 1
    assert errors[0]["code"] == 1007
    assert "1007" in errors[0]["data"]          # data stays a string (client contract)
    assert "missing field" in errors[0]["reason"]
    reconnect.assert_awaited_once()             # close still triggers reconnect


@pytest.mark.asyncio
async def test_mid_reconnect_close_sends_no_contradictory_disconnected():
    exc = ConnectionClosedError(Close(1000, ""), None)
    session = make_session(gemini_ws=FakeGeminiWS(closing_exc=exc))
    session.is_reconnecting = True  # gemini_reconnect owns this close

    await glr.gemini_listener(session)

    assert session.portal_ws.frames("disconnected") == []
    assert session.portal_ws.frames("error") == []
    assert session.status != "disconnected"  # reaper grace clock must NOT start mid-recovery


@pytest.mark.asyncio
async def test_max_reconnects_closes_portal_ws(monkeypatch):
    session = make_session()
    session.reconnect_count = session.max_reconnects
    monkeypatch.setattr(glr, "save_session_to_blackbox", AsyncMock())

    await glr.gemini_reconnect(session)

    assert session.portal_ws.frames("disconnected"), "client must be told"
    assert session.portal_ws.closed, "portal WS must be CLOSED on terminal disconnect"
    assert session.portal_ws.closed[0][0] == 1011
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_close_forwarding.py -x -q`
Expected: FAIL — test 1 AssertionError `len(errors) == 1` (old code only prints the close); test 3 AssertionError on `portal_ws.closed`

**Step 3: Write minimal implementation**

**(a)** After `_safe_ws_send` (line ~92), add:

```python
async def _close_portal_ws(session, reason: str = "gemini terminal disconnect"):
    """Terminal disconnect: actually CLOSE the client WS.

    Before P1.7 the portal WS stayed open answering pings while the Gemini
    side was permanently dead, so Portal/Android showed "Connected — listening"
    forever (silence layer 2 of the 2026-07-11 outage). Closing it kicks the
    endpoint's receive loop into its finally-block cleanup. Safe on the phone
    bridge's PhoneWebSocketAdapter too (any failure is swallowed).
    """
    ws = session.portal_ws
    if ws is None:
        return
    try:
        await ws.close(code=1011, reason=reason[:120])
    except Exception:
        pass
```

**(b)** `gemini_reconnect` give-up branch (lines 1348-1357): after `await save_session_to_blackbox(session)`, add before `return`:

```python
        # Terminal (P1.7): close the client WS — a dead session must not sit
        # there answering pings as if connected.
        await _close_portal_ws(session, reason="max reconnects reached")
```

**(c)** Replace `gemini_listener`'s two `except` blocks (lines 1494-1507) with:

```python
    except websockets.exceptions.ConnectionClosed as e:
        rcvd = getattr(e, "rcvd", None)
        close_code = getattr(rcvd, "code", None)
        close_reason = getattr(rcvd, "reason", "") or ""
        print(f"[GEMINI-LIVE] Gemini connection closed: code={close_code} reason={close_reason!r}")
        if not session.intentional_disconnect and not session.is_reconnecting:
            # Forward the close to the client (P1.7). Setup rejections (e.g.
            # the 1007 invalid-tool-schema that killed every session Jun 26 →
            # Jul 11) arrive ONLY as WS close frames — swallowing them was
            # silence layer 1. `data` stays a string (existing client
            # contract); code/reason ride as additive top-level fields.
            await _safe_ws_send(session.portal_ws, {
                "type": "error",
                "data": f"Gemini connection closed (code={close_code}): {close_reason or 'no reason given'}",
                "code": close_code,
                "reason": close_reason,
            })
            print(f"[GEMINI-LIVE] Unexpected disconnect - triggering reconnect")
            asyncio.create_task(gemini_reconnect(session))
        elif session.intentional_disconnect:
            session.status = "disconnected"
            await _safe_ws_send(session.portal_ws, {
                "type": "disconnected",
                "data": "Gemini connection closed"
            })
        # else: mid-reconnect close of the OLD socket — gemini_reconnect owns
        # status + client notifications. The old code sent a contradictory
        # "disconnected" here and started the reaper grace clock mid-recovery
        # (recon finding #8).
    except Exception as e:
        print(f"[GEMINI-LIVE] Gemini listener error: {e}")
        session.status = "error"
        # Terminal: nothing re-triggers a reconnect from this path — tell the
        # client and CLOSE instead of leaving a zombie WS answering pings.
        await _safe_ws_send(session.portal_ws, {
            "type": "error",
            "data": f"Gemini listener error: {e}",
        })
        await _close_portal_ws(session, reason="gemini listener error")
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_close_forwarding.py Orchestrator/tests/test_gemini_live_reconnect_respawn.py Orchestrator/tests/test_gemini_live_goaway.py -q`
Expected: PASS (all)

**Step 5: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_live_close_forwarding.py
git commit -m "fix(gemini-live): forward Gemini WS close code/reason to the client; terminal disconnect closes the portal WS"
```

---

### Task P1.8: Native input/output transcription; Whisper /stt/json demoted to fallback-only

**Files:**
- Modify: Orchestrator/models.py (GeminiLiveSession — 2 new fields)
- Modify: Orchestrator/routes/gemini_live_routes.py — `configure_gemini_session` setup_config (:427-446), `handle_portal_message` audio_commit (:670-704), `handle_gemini_message` serverContent parsing (:776-793), AI-starts-speaking Whisper site (:820-822), turn-complete block (:884-895)
- Modify: Orchestrator/live_session_reaper.py:77-80 (`release_payload` clears the new buffer)
- Test: Orchestrator/tests/test_gemini_live_native_transcription.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_live_native_transcription.py`:

```python
"""P1.8 — native input/output transcription; Whisper hop = fallback only.

Google's Live API transcribes both sides in-session (inputAudioTranscription /
outputAudioTranscription setup fields; serverContent.inputTranscription /
.outputTranscription objects with a .text field). The post-hoc Whisper
/stt/json hop stays as fallback ONLY — this removes the /stt/json quota
dependency from the voice path (box STT was silently dead on quota Jul 08).
"""
import json
from unittest.mock import AsyncMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import (
    FakeGeminiWS,
    make_session,
    stub_fossil_context,
)


@pytest.fixture
def no_fossils(monkeypatch):
    stub_fossil_context(monkeypatch)


@pytest.mark.asyncio
async def test_setup_enables_native_transcription(no_fossils):
    session = make_session(gemini_ws=FakeGeminiWS())
    await glr.configure_gemini_session(session, "test_operator", "Orus")
    setup = json.loads(session.gemini_ws.send.await_args.args[0])["setup"]
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}


@pytest.mark.asyncio
async def test_input_transcription_accumulates_and_flushes_on_turn_complete():
    session = make_session()

    await glr.handle_gemini_message(
        session, {"serverContent": {"inputTranscription": {"text": "hello "}}}
    )
    await glr.handle_gemini_message(
        session, {"serverContent": {"inputTranscription": {"text": "world"}}}
    )
    assert session.native_transcription_active is True
    assert session.input_transcript_buffer == "hello world"
    deltas = session.portal_ws.frames("user_transcript_delta")
    assert [d["data"] for d in deltas] == ["hello ", "world"]

    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})

    user_turns = [m for m in session.conversation if m["role"] == "user"]
    assert [m["content"] for m in user_turns] == ["hello world"]
    finals = session.portal_ws.frames("user_transcript")
    assert [f["data"] for f in finals] == ["hello world"]
    assert session.input_transcript_buffer == ""


@pytest.mark.asyncio
async def test_output_transcription_feeds_assistant_transcript():
    session = make_session()

    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "Sure, "}}}
    )
    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "done."}}}
    )
    deltas = session.portal_ws.frames("transcript_delta")
    assert [d["data"] for d in deltas] == ["Sure, ", "done."]

    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})

    assistant = [m for m in session.conversation if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == ["Sure, done."]


@pytest.mark.asyncio
async def test_whisper_skipped_when_native_transcription_active(monkeypatch):
    session = make_session()
    session.native_transcription_active = True
    session.user_audio_buffer = ["QUJD"]
    whisper = AsyncMock(return_value="whisper says hi")
    monkeypatch.setattr(glr, "transcribe_user_audio", whisper)

    await glr.handle_portal_message(session, {"type": "audio_commit"})

    whisper.assert_not_awaited()
    assert session.user_audio_buffer == []


@pytest.mark.asyncio
async def test_whisper_fallback_when_no_native_transcription(monkeypatch):
    session = make_session()
    session.user_audio_buffer = ["QUJD"]
    whisper = AsyncMock(return_value="fallback transcript")
    monkeypatch.setattr(glr, "transcribe_user_audio", whisper)

    await glr.handle_portal_message(session, {"type": "audio_commit"})

    whisper.assert_awaited_once()
    assert [m["content"] for m in session.conversation if m["role"] == "user"] == [
        "fallback transcript"
    ]
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_native_transcription.py -x -q`
Expected: FAIL — first test `KeyError: 'inputAudioTranscription'`; second test `AttributeError: 'GeminiLiveSession' object has no attribute 'native_transcription_active'`

**Step 3: Write minimal implementation**

**(a)** `Orchestrator/models.py` — in `GeminiLiveSession`, next to `transcript_buffer`:

```python
    input_transcript_buffer: str = ""            # Native inputTranscription accumulation, flushed per turn (P1.8)
    native_transcription_active: bool = False    # True once native transcription observed — Whisper is fallback-only (P1.8)
```

**(b)** `configure_gemini_session` — in `setup_config` (after the `contextWindowCompression` entry, ~line 443-446), add two keys:

```python
        "contextWindowCompression": {
            "slidingWindow": {}
        },
        # P1.8 — native transcription for both directions: Google transcribes
        # in-session, so the post-hoc Whisper /stt/json hop is fallback-only
        # (removes the STT quota dependency from the voice path).
        "inputAudioTranscription": {},
        "outputAudioTranscription": {}
```

**(c)** `handle_gemini_message` — replace the field-name-sniffing block (lines 776-793, from `# Log all keys in serverContent...` through the `user_transcript` send) with:

```python
        # Native transcription (P1.8): BidiGenerateContentTranscription objects
        # with a .text field — the REAL field names (the old code sniffed
        # inputTranscript/userTranscript/etc., none of which exist, and would
        # have crashed slicing a dict — recon finding #14).
        input_tx = server_content.get("inputTranscription")
        if isinstance(input_tx, dict) and input_tx.get("text"):
            session.native_transcription_active = True
            session.input_transcript_buffer += input_tx["text"]
            session.user_audio_buffer = []  # native owns the transcript — drop the Whisper buffer
            await _safe_ws_send(session.portal_ws, {
                "type": "user_transcript_delta",
                "data": input_tx["text"]
            })

        output_tx = server_content.get("outputTranscription")
        if isinstance(output_tx, dict) and output_tx.get("text"):
            session.native_transcription_active = True
            # Feed the same buffer part.text used to fill — native-audio models
            # are NOT guaranteed to emit part.text (recon: empty assistant
            # lines in saved transcripts).
            session.transcript_buffer += output_tx["text"]
            await _safe_ws_send(session.portal_ws, {
                "type": "transcript_delta",
                "data": output_tx["text"]
            })
```

**(d)** AI-starts-speaking Whisper site (line ~822) — add the guard to the existing condition:

```python
                    if not session.is_speaking and session.user_audio_buffer and not session.native_transcription_active:
```

**(e)** Turn-complete block (lines 884-895) — insert the user flush between `session.is_speaking = False` and the assistant append:

```python
            # P1.8 — flush the native user transcript FIRST so the ledger
            # conversation stays user → assistant ordered.
            if session.input_transcript_buffer.strip():
                user_text = session.input_transcript_buffer.strip()
                session.conversation.append({
                    "role": "user",
                    "content": user_text,
                    "timestamp": now_utc_iso(),
                    "source": "voice"
                })
                await _safe_ws_send(session.portal_ws, {
                    "type": "user_transcript",
                    "data": user_text
                })
                session.input_transcript_buffer = ""
```

**(f)** `handle_portal_message` audio_commit branch — wrap the Whisper call (line ~686, `transcript = await transcribe_user_audio(session)` and its `if transcript:` block) in:

```python
        if session.native_transcription_active:
            # P1.8 — native transcription owns the user transcript: skip the
            # Whisper hop entirely and drop the buffered audio.
            session.user_audio_buffer = []
        else:
            # FALLBACK ONLY: post-hoc Whisper via /stt/json.
            transcript = await transcribe_user_audio(session)
            if transcript:
                ...  # existing body unchanged, re-indented one level
```

**(g)** `Orchestrator/live_session_reaper.py` `release_payload` (after line 80) — mirror the existing hasattr pattern:

```python
    if hasattr(session, "input_transcript_buffer"):
        session.input_transcript_buffer = ""
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_native_transcription.py Orchestrator/tests/test_live_models.py Orchestrator/tests/test_live_session_reaper.py -q`
Expected: PASS (all)

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/gemini_live_routes.py Orchestrator/live_session_reaper.py Orchestrator/tests/test_gemini_live_native_transcription.py
git commit -m "feat(gemini-live): native input/output transcription; Whisper /stt/json hop demoted to fallback-only"
```

---

### Task P1.9: Default model = gemini-3.1-flash-live-preview (env override still wins)

**Files:**
- Modify: Orchestrator/config.py:540 (default literal), :545-549 (catalog `default` flag)
- Test: Orchestrator/tests/test_gemini_live_default_model.py

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_live_default_model.py`:

```python
"""P1.9 — Gemini Live default model = gemini-3.1-flash-live-preview.

Research 2026-07-11: 3.1-flash-live-preview is THE recommended Live model
(probe-confirmed alive); the 2.5 native-audio line is deprecated. Android
already defaults to 3.1 — this aligns config.py and /gemini-live/status
(one canonical default). GEMINI_LIVE_MODEL env override must still win.
"""
import os

import pytest

from Orchestrator.config import GEMINI_LIVE_MODEL, GEMINI_LIVE_MODELS


def test_catalog_default_is_31_preview():
    defaults = [m["id"] for m in GEMINI_LIVE_MODELS if m.get("default")]
    assert defaults == ["gemini-3.1-flash-live-preview"]


def test_module_default_resolves_env_then_31_preview():
    # Same expression as config.py — pins the fallback literal when the env
    # var is unset, and stays true when an operator overrides it.
    assert GEMINI_LIVE_MODEL == os.getenv(
        "GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"
    )


@pytest.mark.asyncio
async def test_status_endpoint_reflects_default():
    from Orchestrator.routes.gemini_live_routes import gemini_live_status

    data = await gemini_live_status()
    assert data["model_default"] == GEMINI_LIVE_MODEL
    assert [m["id"] for m in data["models"] if m.get("default")] == [
        "gemini-3.1-flash-live-preview"
    ]
```

**Step 2: Run test to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_default_model.py -x -q`
Expected: FAIL — `test_catalog_default_is_31_preview` AssertionError (`['gemini-2.5-flash-native-audio-latest']`)

**Step 3: Write minimal implementation**

`Orchestrator/config.py` line 540 — replace:

```python
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-latest")  # GA-track alias - bumped from -preview-12-2025
```

with:

```python
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")  # THE recommended Live model (research 2026-07-11); deliberate GA-rule exception, matches Android default. Env override wins.
```

Lines 545-549 — replace the catalog with (3.1 first + default; 2.5 lines kept, labeled deprecated):

```python
GEMINI_LIVE_MODELS: List[Dict] = [
    {"id": "gemini-3.1-flash-live-preview", "name": "Gemini 3.1 Flash Live (Preview, thinkingLevel)", "default": True},
    {"id": "gemini-2.5-flash-native-audio-latest", "name": "Gemini 2.5 Flash Live (Latest — deprecated line)"},
    {"id": "gemini-2.5-flash-native-audio-preview-12-2025", "name": "Gemini 2.5 Flash Live (Dec 2025 pin — deprecated)"},
]
```

**Step 4: Run test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_live_default_model.py Orchestrator/tests/test_live_models.py Orchestrator/tests/test_gemini_live_reconnect_config.py -q`
Expected: PASS (all)

**Step 5: Commit**

```bash
git add Orchestrator/config.py Orchestrator/tests/test_gemini_live_default_model.py
git commit -m "fix(config): Gemini Live default model = gemini-3.1-flash-live-preview (env override still wins)"
```

---

### Task P1.10: Live verification — restart + full tool-group probe on BOTH model lines (P0 harness)

Verification task (design workstream 1 item 8) — runs the durable P0 probe harness (landed in P0.2; P0.7 runs the same suite right after P1.1) against the RESTARTED service. The only repo change is deleting the now-satisfied xfail marker P0.6 placed on the gemini-tools live guard. Restart is pre-authorized.

**Files:**
- Modify: diagnostics/voice_probes/test_live_probes.py (remove the P0.6 `@pytest.mark.xfail` on `test_gemini_full_toolgroup_setup_completes`)
- Create (generated): diagnostics/voice_probes/results/<date>-gemini-tools.json (same-day re-run overwrites P0.7's file — fine; this run is the post-restart evidence)

**Step 1: Restart the service and wait for warm-up**

Run: `sudo systemctl restart blackbox.service && sleep 90 && curl -sf http://localhost:9091/gemini-live/status | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['model_default']); assert d['model_default'].startswith('gemini-3.1-flash-live-preview') or __import__('os').getenv('GEMINI_LIVE_MODEL'), d['model_default']"`
Expected: `gemini-3.1-flash-live-preview` (or the env-override value if one is set on this box)

**Step 2: Run the P0 harness gemini-tools suite**

Run (the exact P0.7 CLI — the durable harness landed in P0.2, do NOT write a one-off script):

```bash
Orchestrator/venv/bin/python -m diagnostics.voice_probes.run --suite gemini-tools
```

Expected: 3 summary lines + `results: diagnostics/voice_probes/results/<date>-gemini-tools.json`, ALL OK:
- `gemini gemini-3.1-flash-live-preview bare: OK` (control — isolates any failure to the tools payload)
- `gemini gemini-3.1-flash-live-preview full_tools: OK` with `56 functionDeclarations` (or the current count) in notes
- `gemini gemini-2.5-flash-native-audio-latest full_tools: OK`

Any `full_tools` FAIL with a 1007 at a *different* `function_declarations[N]` index = a second latent schema violation → STOP, report it, and fix that module's schema the P1.1 way (the P1.2 test should then be extended to cover why it was missed). If that happens while `python -m Orchestrator.toolvault.validate` is green, the P1.2 validator has a gap — investigate BOTH.

**Step 3: Assert the result file, then remove the P0.6 xfail marker**

Assert every probe in the result file is OK:

```bash
Orchestrator/venv/bin/python -c "
import json, glob
p = sorted(glob.glob('diagnostics/voice_probes/results/*-gemini-tools.json'))[-1]
d = json.load(open(p))
assert all(r['ok'] for r in d['results']), [(r['probe'], r['close_reason'], r['error']) for r in d['results'] if not r['ok']]
print('all', len(d['results']), 'gemini probes OK:', p)
"
```

Expected: `all 3 gemini probes OK: ...`

Then delete the entire `@pytest.mark.xfail(...)` decorator P0.6 placed on `test_gemini_full_toolgroup_setup_completes` in `diagnostics/voice_probes/test_live_probes.py` — the P1.1 schema fix it anticipated has now landed and been verified live (this fulfills P0.6's cross-phase note; if an earlier pass already removed it, skip the edit). Re-run the live guard:

```bash
Orchestrator/venv/bin/python -m pytest diagnostics/voice_probes/test_live_probes.py -m probe_live -v -k gemini
```

Expected: `test_gemini_bare_setup_completes PASSED` and `test_gemini_full_toolgroup_setup_completes PASSED` (a plain PASS — no longer xfail/XPASS).

**Step 4: Confirm the journal is clean since restart**

Run: `journalctl -u blackbox.service --since "-10 minutes" --no-pager | grep -F "1007" | head -5`
Expected: no output. Also run the full new-suite pass once: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_toolvault_array_items.py Orchestrator/tests/test_gemini_live_tools_fresh.py Orchestrator/tests/test_gemini_live_reconnect_config.py Orchestrator/tests/test_gemini_live_reconnect_respawn.py Orchestrator/tests/test_gemini_live_goaway.py Orchestrator/tests/test_gemini_live_close_forwarding.py Orchestrator/tests/test_gemini_live_native_transcription.py Orchestrator/tests/test_gemini_live_default_model.py Orchestrator/tests/test_live_models.py -q` → all pass.

**Step 5: Commit**

```bash
git add diagnostics/voice_probes/test_live_probes.py diagnostics/voice_probes/results/*-gemini-tools.json
git commit -m "test(diagnostics): drop P0.6 xfail on the gemini full-toolgroup live guard — P1.1 schema fix verified post-restart (P1.10)"
```

(If the xfail was already removed by an earlier pass and the results file is unchanged, there is nothing to commit — skip.) End-to-end Portal/Android voice validation is Brandon's acceptance step, not this task.

---

## Phase 1b — Cross-route hardening (Workstream 6)

> **Section execution notes:**
> - Repo root: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`. All commands run from there. Pytest = `Orchestrator/venv/bin/python -m pytest`.
> - The service runs LIVE from this working tree — every task ends with an importable, green tree before commit.
> - Line numbers below were read from the working tree on 2026-07-11 **before** any Phase 1 edits. P1.1–P1.19 (Gemini rescue) touch `gemini_live_routes.py` and `models.py`, so numbers WILL drift — every edit below therefore anchors on unique `old_string` text, not line position. If an anchor is missing, re-read the file; do not guess.
> - Do NOT touch `GEMINI_LIVE_TOOLS` (gemini_live_routes.py:98), the Gemini reconnect path (`gemini_reconnect`), or `GeminiLiveSession` fields — those belong to P1.1–P1.19.
> - Execute with @superpowers:executing-plans; each task follows @superpowers:test-driven-development.

---

### Task P1.20: Shared voice-WS tool-error helpers + test fakes

**Files:**
- Create: `Orchestrator/routes/voice_ws_shared.py`
- Create: `Orchestrator/tests/voice_ws_fakes.py`
- Test: `Orchestrator/tests/test_voice_ws_shared.py`

All three voice routes (realtime_routes.py:695-1084, grok_live_routes.py:613-1016, gemini_live_routes.py:937-1290) run tool dispatch with no per-tool try/except: an executor raise propagates to the listener's per-message catch and the model's tool call dangles forever (dead air). This task builds the shared, provider-shaped error responders; P1.25–P1.27 wire them in.

**Step 1: Write the failing test**

Create `Orchestrator/tests/voice_ws_fakes.py`:

```python
"""Shared fake WebSocket doubles for the P1b voice-route hardening tests."""
import json

from starlette.websockets import WebSocketState


class FakeUpstreamWS:
    """Stands in for a `websockets` client connection (OpenAI/Grok/Gemini side)."""

    def __init__(self):
        self.sent = []          # decoded JSON frames, in send order
        self.closed = False

    async def send(self, payload: str):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


class FakeStreamWS(FakeUpstreamWS):
    """FakeUpstreamWS that also async-iterates a scripted list of inbound frames."""

    def __init__(self, messages=None):
        super().__init__()
        self._messages = list(messages or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakePortalWS:
    """Stands in for the FastAPI client WebSocket (routes check application_state)."""

    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)
```

Create `Orchestrator/tests/test_voice_ws_shared.py`:

```python
"""Unit tests for Orchestrator/routes/voice_ws_shared.py (P1b cross-route hardening)."""
import asyncio

from Orchestrator.routes import voice_ws_shared as vs
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS, FakePortalWS


FC_EVENT = {
    "type": "response.function_call_arguments.done",
    "call_id": "call-42",
    "name": "web_fetch",
    "arguments": "{\"url\": \"https://x\"}",
}

GEMINI_EVENT = {
    "toolCall": {
        "functionCalls": [
            {"id": "fc-1", "name": "get_current_time", "args": {}},
            {"id": "fc-2", "name": "web_fetch", "args": {"url": "https://x"}},
        ]
    }
}


def test_openai_style_tool_error_answers_call_id():
    async def run():
        upstream, portal = FakeUpstreamWS(), FakePortalWS()
        ok = await vs.send_openai_style_tool_error(
            upstream, portal, FC_EVENT, RuntimeError("boom"))
        assert ok is True
        assert [m["type"] for m in upstream.sent] == \
            ["conversation.item.create", "response.create"]
        item = upstream.sent[0]["item"]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == "call-42"
        assert "web_fetch" in item["output"] and "boom" in item["output"]
        assert portal.sent[0]["type"] == "tool_result"
        assert portal.sent[0]["data"]["error"] is True
    asyncio.run(run())


def test_openai_style_tool_error_ignores_non_tool_events():
    async def run():
        upstream = FakeUpstreamWS()
        ok = await vs.send_openai_style_tool_error(
            upstream, None, {"type": "response.done"}, RuntimeError("x"))
        assert ok is False
        assert upstream.sent == []
    asyncio.run(run())


def test_openai_style_tool_error_never_raises_on_dead_upstream():
    class DeadWS:
        async def send(self, payload):
            raise ConnectionError("closed")

    async def run():
        ok = await vs.send_openai_style_tool_error(
            DeadWS(), None, FC_EVENT, RuntimeError("boom"))
        assert ok is False
    asyncio.run(run())


def test_gemini_tool_error_answers_all_unanswered():
    async def run():
        upstream, portal = FakeUpstreamWS(), FakePortalWS()
        ok = await vs.send_gemini_tool_error(
            upstream, portal, GEMINI_EVENT, RuntimeError("boom"), answered_ids=None)
        assert ok is True
        responses = upstream.sent[0]["toolResponse"]["functionResponses"]
        assert [r["id"] for r in responses] == ["fc-1", "fc-2"]
        assert all("boom" in r["response"]["result"] for r in responses)
        assert len(portal.sent) == 2
    asyncio.run(run())


def test_gemini_tool_error_skips_answered_ids():
    async def run():
        upstream = FakeUpstreamWS()
        ok = await vs.send_gemini_tool_error(
            upstream, None, GEMINI_EVENT, RuntimeError("boom"), answered_ids={"fc-1"})
        assert ok is True
        responses = upstream.sent[0]["toolResponse"]["functionResponses"]
        assert [r["id"] for r in responses] == ["fc-2"]
    asyncio.run(run())


def test_gemini_tool_error_noop_when_all_answered():
    async def run():
        upstream = FakeUpstreamWS()
        ok = await vs.send_gemini_tool_error(
            upstream, None, GEMINI_EVENT, RuntimeError("boom"),
            answered_ids={"fc-1", "fc-2"})
        assert ok is False
        assert upstream.sent == []
    asyncio.run(run())
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_ws_shared.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'Orchestrator.routes.voice_ws_shared'`

**Step 3: Write minimal implementation**

Create `Orchestrator/routes/voice_ws_shared.py`:

```python
#!/usr/bin/env python3
"""
voice_ws_shared.py - Cross-route helpers shared by the three realtime voice
WebSocket bridges (OpenAI realtime_routes, xAI grok_live_routes, Google
gemini_live_routes).

P1b hardening (2026-07-11 voice-agent upgrade pass, workstream 6):
- Tool-dispatch exception -> error payload back to the model, so a raised
  executor NEVER dangles a function call id (previously: silent dead turn,
  the model waits forever on a tool result and the user hears dead air).
- save_voice_transcript(): transcript persistence via POST /chat/save
  (direct persistence + turns_threshold=1 auto-mint) instead of POST /chat
  (full LLM round-trip, ~400x more expensive — CLAUDE.md anti-pattern).

Keep this module LIGHT (json/aiohttp/starlette only): it is imported by all
three voice routes and, through them, the phone bridge chain.
"""

import json
from typing import Dict, Optional, Set

from starlette.websockets import WebSocketState


def tool_error_text(name: str, exc: BaseException) -> str:
    """Error payload returned to the model in place of a tool result."""
    return (
        f"Tool '{name}' failed: {type(exc).__name__}: {exc}. "
        "Briefly tell the user the action failed, then continue the conversation."
    )


async def _safe_portal_send(websocket, data: dict) -> bool:
    """Best-effort JSON send to the client WS; never raises.

    Local copy of the routes' _safe_ws_send — importing it from a route module
    here would create an import cycle (routes import this module).
    """
    try:
        if websocket and hasattr(websocket, "application_state") \
                and websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
            return True
    except Exception:
        pass
    return False


async def send_openai_style_tool_error(upstream_ws, portal_ws,
                                       event: Dict, exc: BaseException) -> bool:
    """Answer a dangling OpenAI-schema function call with an error payload.

    Used by BOTH the OpenAI Realtime and xAI Grok routes (identical wire
    format): sends conversation.item.create/function_call_output for the
    event's call_id followed by response.create, and notifies the portal
    with an error-flagged tool_result. No-op (returns False) when the event
    is not a function-call event. Never raises.
    """
    if not event or event.get("type") != "response.function_call_arguments.done":
        return False

    call_id = event.get("call_id", "")
    name = event.get("name", "")
    result = tool_error_text(name, exc)

    sent = False
    if upstream_ws:
        try:
            await upstream_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }))
            await upstream_ws.send(json.dumps({"type": "response.create"}))
            sent = True
        except Exception as send_err:
            print(f"[VOICE-SHARED] Could not deliver tool error for '{name}' "
                  f"(call_id={call_id}): {send_err}")

    await _safe_portal_send(portal_ws, {
        "type": "tool_result",
        "data": {"name": name, "result_length": len(result), "error": True},
    })
    return sent


async def send_gemini_tool_error(gemini_ws, portal_ws, event: Dict,
                                 exc: BaseException,
                                 answered_ids: Optional[Set[str]] = None) -> bool:
    """Answer dangling Gemini functionCalls with error functionResponses.

    A Gemini toolCall event carries a LIST of functionCalls; a raise mid-loop
    dangles every not-yet-answered id. `answered_ids` (recorded by the dispatch
    loop) prevents double-answering ids that already got a real response.
    No-op (returns False) when the event has no toolCall or nothing is
    unanswered. Never raises.
    """
    tool_call = (event or {}).get("toolCall")
    if not tool_call:
        return False

    answered = answered_ids or set()
    pending = [fc for fc in tool_call.get("functionCalls", [])
               if fc.get("id", "") not in answered]
    if not pending:
        return False

    responses = [{
        "id": fc.get("id", ""),
        "name": fc.get("name", ""),
        "response": {"result": tool_error_text(fc.get("name", ""), exc)},
    } for fc in pending]

    sent = False
    if gemini_ws:
        try:
            await gemini_ws.send(json.dumps(
                {"toolResponse": {"functionResponses": responses}}))
            sent = True
        except Exception as send_err:
            print(f"[VOICE-SHARED] Could not deliver Gemini tool error "
                  f"({len(responses)} call(s)): {send_err}")

    for fc in pending:
        await _safe_portal_send(portal_ws, {
            "type": "tool_result",
            "data": {"name": fc.get("name", ""), "result_length": 0, "error": True},
        })
    return sent
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_ws_shared.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add Orchestrator/routes/voice_ws_shared.py Orchestrator/tests/voice_ws_fakes.py Orchestrator/tests/test_voice_ws_shared.py
git commit -m "feat(voice): shared tool-error responders for the 3 voice WS routes (P1b)"
```

---

### Task P1.21: `save_voice_transcript` — the /chat/save persistence helper

**Files:**
- Modify: `Orchestrator/routes/voice_ws_shared.py` (created in P1.20)
- Test: `Orchestrator/tests/test_voice_ws_shared.py`

All three routes currently persist session transcripts via `POST /chat` — a full Gemini LLM round-trip (realtime_routes.py:97-163, grok_live_routes.py:94-161, gemini_live_routes.py:566-634). The `/chat/save` contract (chat_routes.py:7399-7427): body `{operator, user_message, assistant_response, model?, tokens?}`, `assistant_response` required, server-side `TURNS_THRESHOLD=1` auto-mint fires `perform_mint()` inline → snapshot minted + embedded by the time the 200 returns. Do NOT call `/mint` afterward.

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_ws_shared.py`:

```python
# ---------------------------------------------------------------------------
# save_voice_transcript (POST /chat/save, clear-only-on-200 contract)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def json(self):
        return {"success": True, "minted": True, "snap_id": "SNAP-20260711-TEST"}

    async def text(self):
        return "server error body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_aiohttp(log, status):
    class _FakePost:
        def __init__(self, url, **kwargs):
            log.append({"url": url, **kwargs})
            self._resp = _FakeResp(status)

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    class FakeClientSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            return _FakePost(url, **kwargs)

    class FakeTimeout:
        def __init__(self, total=None):
            self.total = total

    class FakeAiohttpModule:
        ClientSession = FakeClientSession
        ClientTimeout = FakeTimeout

    return FakeAiohttpModule


def test_save_voice_transcript_true_on_200(monkeypatch):
    log = []
    monkeypatch.setattr(vs, "aiohttp", _fake_aiohttp(log, 200))
    ok = asyncio.run(vs.save_voice_transcript(
        operator="system",
        user_message="[Voice Session Transcript] test session t-1",
        session_summary="=== Test Voice Session ===\n[User]: hi",
        model_label="test-voice",
        log_prefix="[TEST]",
    ))
    assert ok is True
    assert log[0]["url"] == "http://localhost:9091/chat/save"
    body = log[0]["json"]
    assert body["operator"] == "system"
    assert body["user_message"].startswith("[Voice Session Transcript]")
    assert body["assistant_response"].startswith("=== Test Voice Session ===")
    assert body["model"] == "test-voice"
    assert body["tokens"] == {"prompt": 0, "completion": 0}


def test_save_voice_transcript_false_on_500(monkeypatch):
    monkeypatch.setattr(vs, "aiohttp", _fake_aiohttp([], 500))
    ok = asyncio.run(vs.save_voice_transcript(
        operator="system", user_message="m", session_summary="s",
        model_label="test-voice", log_prefix="[TEST]"))
    assert ok is False


def test_save_voice_transcript_false_on_exception(monkeypatch):
    class Exploding:
        ClientTimeout = staticmethod(lambda total=None: None)

        class ClientSession:
            def __init__(self, *a, **k):
                raise ConnectionError("no server")

    monkeypatch.setattr(vs, "aiohttp", Exploding)
    ok = asyncio.run(vs.save_voice_transcript(
        operator="system", user_message="m", session_summary="s",
        model_label="test-voice", log_prefix="[TEST]"))
    assert ok is False
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_ws_shared.py -v -k save_voice_transcript`
Expected: FAIL with `AttributeError: ... has no attribute 'aiohttp'` (or `'save_voice_transcript'`)

**Step 3: Write minimal implementation**

In `Orchestrator/routes/voice_ws_shared.py`, add below `import json`:

```python
import aiohttp
```

and append at end of file:

```python
CHAT_SAVE_URL = "http://localhost:9091/chat/save"


async def save_voice_transcript(operator: str, user_message: str,
                                session_summary: str, model_label: str,
                                log_prefix: str) -> bool:
    """Persist a voice-session transcript via POST /chat/save.

    Direct persistence: the backend's turns_threshold=1 auto-mint fires
    perform_mint() inline (embedding included), so the snapshot is searchable
    when the 200 returns. NEVER POST /chat here (full LLM round-trip) and
    NEVER call /mint afterward (duplicate snapshot).

    Returns True ONLY on HTTP 200 — callers must clear their conversation
    buffer only when this returns True, so a failed save can be retried by
    a later teardown path.
    """
    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                CHAT_SAVE_URL,
                json={
                    "operator": operator,
                    "user_message": user_message,
                    "assistant_response": session_summary,
                    "model": model_label,
                    "tokens": {"prompt": 0, "completion": 0},
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    print(f"{log_prefix} Transcript saved via /chat/save "
                          f"(minted={body.get('minted')}, snap_id={body.get('snap_id')})")
                    return True
                error = await resp.text()
                print(f"{log_prefix} /chat/save failed: {resp.status} - {error[:200]}")
                return False
    except Exception as e:
        print(f"{log_prefix} /chat/save error: {e}")
        return False
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_ws_shared.py -v`
Expected: PASS (9 tests)

**Step 5: Commit**

```bash
git add Orchestrator/routes/voice_ws_shared.py Orchestrator/tests/test_voice_ws_shared.py
git commit -m "feat(voice): save_voice_transcript via /chat/save with clear-only-on-200 contract (P1b)"
```

---

### Task P1.22: OpenAI route — transcript save via /chat/save, clear only on 200

**Files:**
- Modify: `Orchestrator/routes/realtime_routes.py:43-57` (config import), `:97-163` (`save_session_to_blackbox`)
- Test: `Orchestrator/tests/test_voice_transcript_save.py`

Note: `save_session_to_blackbox` is also imported by `phone/bridge.py:3292` — the name and signature MUST NOT change.

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_transcript_save.py`:

```python
"""P1b: voice routes persist transcripts via /chat/save and clear ONLY on success."""
import asyncio

import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import RealtimeSession


def _session(cls, sid):
    s = cls(session_id=sid, operator="system")
    s.conversation = [
        {"role": "user", "content": "hi there", "timestamp": "2026-07-11T00:00:00Z"},
        {"role": "assistant", "content": "hello", "timestamp": "2026-07-11T00:00:01Z"},
    ]
    return s


def _run_save(monkeypatch, module, save_fn, session, ok):
    captured = {}

    async def fake_save(**kwargs):
        captured.update(kwargs)
        return ok

    monkeypatch.setattr(module, "save_voice_transcript", fake_save)
    asyncio.run(save_fn(session))
    return captured


def test_realtime_save_clears_on_success(monkeypatch):
    session = _session(RealtimeSession, "t-rt-ok")
    captured = _run_save(monkeypatch, rt, rt.save_session_to_blackbox, session, ok=True)
    assert session.conversation == []
    assert captured["operator"] == "system"
    assert "OpenAI Realtime Voice Session" in captured["session_summary"]
    assert "[User]: hi there" in captured["session_summary"]
    assert "[AI]: hello" in captured["session_summary"]
    assert captured["user_message"].startswith("[Voice Session Transcript]")


def test_realtime_save_keeps_transcript_on_failure(monkeypatch):
    session = _session(RealtimeSession, "t-rt-fail")
    _run_save(monkeypatch, rt, rt.save_session_to_blackbox, session, ok=False)
    assert len(session.conversation) == 2, \
        "conversation must be KEPT after a failed save (retry on next teardown)"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_transcript_save.py -v`
Expected: FAIL with `AttributeError: <module 'Orchestrator.routes.realtime_routes'> does not have the attribute 'save_voice_transcript'`

**Step 3: Write minimal implementation**

In `Orchestrator/routes/realtime_routes.py`:

(a) Delete the line `    GEMINI_MODEL_DEFAULT,` from the `from Orchestrator.config import (...)` block (line 55 — it was only used by the old `/chat` save).

(b) Below the line `from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK` add:

```python
from Orchestrator.routes.voice_ws_shared import save_voice_transcript
```

(c) Replace the ENTIRE `save_session_to_blackbox` function (from `async def save_session_to_blackbox(session: RealtimeSession):` down to and including `    session.conversation = []` at line 163) with:

```python
async def save_session_to_blackbox(session: RealtimeSession):
    """
    Save the OpenAI Realtime session conversation to the BlackBox ledger.

    Called on disconnect/cleanup (endpoint finally, reconnect exhaustion,
    phone bridge teardown). P1b: persists via POST /chat/save (direct
    persistence + auto-mint; no LLM round-trip) and clears
    session.conversation ONLY after a confirmed 200 so a failed save can be
    retried by a later teardown path.
    """
    if not session.conversation:
        print(f"[REALTIME] No conversation to save for session {session.session_id}")
        return

    if not session.operator:
        print(f"[REALTIME] No operator set, cannot save session {session.session_id}")
        return

    # Sort conversation by timestamp to ensure correct order
    sorted_conversation = sorted(
        session.conversation,
        key=lambda x: x.get("timestamp", "")
    )

    # Format conversation as readable transcript
    transcript_lines = []
    for msg in sorted_conversation:
        role = "User" if msg["role"] == "user" else "AI"
        transcript_lines.append(f"[{role}]: {msg['content']}")

    transcript = "\n\n".join(transcript_lines)

    session_summary = f"""=== OpenAI Realtime Voice Session ===
Session ID: {session.session_id}
Timestamp: {now_utc_iso()}
Messages: {len(session.conversation)}

--- Transcript ---
{transcript}
--- End Session ---"""

    print(f"[REALTIME] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")

    saved = await save_voice_transcript(
        operator=session.operator,
        user_message=f"[Voice Session Transcript] OpenAI Realtime voice session {session.session_id}",
        session_summary=session_summary,
        model_label="openai-realtime-voice",
        log_prefix="[REALTIME]",
    )

    # Clear ONLY after a confirmed 200 (previously cleared unconditionally,
    # permanently losing the transcript after a failed save).
    if saved:
        session.conversation = []
    else:
        print(f"[REALTIME] Save FAILED — keeping {len(session.conversation)} turns for a later retry")
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_transcript_save.py -v && Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes"`
Expected: PASS (2 tests), import exits 0

**Step 5: Commit**

```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_voice_transcript_save.py
git commit -m "fix(realtime): transcript save via /chat/save; clear conversation only on confirmed 200 (P1b)"
```

---

### Task P1.23: Grok route — transcript save via /chat/save (fixes the :161 clear-after-failed-save bug)

**Files:**
- Modify: `Orchestrator/routes/grok_live_routes.py:44-54` (config import), `:94-161` (`save_grok_session_to_blackbox`)
- Test: `Orchestrator/tests/test_voice_transcript_save.py`

`save_grok_session_to_blackbox` is imported by `phone/bridge.py:3298` — name/signature MUST NOT change. Line 161 (`session.conversation = []` after a failed save) is the confirmed transcript-loss bug.

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_transcript_save.py`:

```python
import Orchestrator.routes.grok_live_routes as gk
from Orchestrator.models import GrokLiveSession


def test_grok_save_clears_on_success(monkeypatch):
    session = _session(GrokLiveSession, "t-gk-ok")
    captured = _run_save(monkeypatch, gk, gk.save_grok_session_to_blackbox, session, ok=True)
    assert session.conversation == []
    assert "Grok Voice Agent Session" in captured["session_summary"]
    assert "[AI (Grok)]: hello" in captured["session_summary"]
    assert captured["model_label"] == "grok-live-voice"


def test_grok_save_keeps_transcript_on_failure(monkeypatch):
    session = _session(GrokLiveSession, "t-gk-fail")
    _run_save(monkeypatch, gk, gk.save_grok_session_to_blackbox, session, ok=False)
    assert len(session.conversation) == 2, \
        "grok_live_routes previously cleared even after a FAILED save (line 161 bug)"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_transcript_save.py -v -k grok`
Expected: FAIL with `AttributeError: ... does not have the attribute 'save_voice_transcript'`

**Step 3: Write minimal implementation**

In `Orchestrator/routes/grok_live_routes.py`:

(a) Delete `    GEMINI_MODEL_DEFAULT,` from the config import block (line 52).

(b) Below `from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK` add:

```python
from Orchestrator.routes.voice_ws_shared import save_voice_transcript
```

(c) Replace the body of `save_grok_session_to_blackbox` from the line `    print(f"[GROK-LIVE] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")` through `    session.conversation = []` (line 161) with — keeping everything above (guards, sorting, `session_summary` construction with the `=== Grok Voice Agent Session ===` header) unchanged:

```python
    print(f"[GROK-LIVE] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")

    saved = await save_voice_transcript(
        operator=session.operator,
        user_message=f"[Grok Voice Session Transcript] Grok voice session {session.session_id}",
        session_summary=session_summary,
        model_label="grok-live-voice",
        log_prefix="[GROK-LIVE]",
    )

    # P1b BUGFIX: previously cleared UNCONDITIONALLY — a failed save
    # permanently lost the transcript. Clear only on confirmed 200.
    if saved:
        session.conversation = []
    else:
        print(f"[GROK-LIVE] Save FAILED — keeping {len(session.conversation)} turns for a later retry")
```

(The old `try: async with aiohttp.ClientSession() ... except ... session.conversation = []` block is what gets deleted.)

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_transcript_save.py -v && Orchestrator/venv/bin/python -c "import Orchestrator.routes.grok_live_routes"`
Expected: PASS (4 tests), import exits 0

**Step 5: Commit**

```bash
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_voice_transcript_save.py
git commit -m "fix(grok-live): /chat/save persistence; stop clearing transcript after a FAILED save (P1b)"
```

---

### Task P1.24: Gemini route — transcript save via /chat/save

**Files:**
- Modify: `Orchestrator/routes/gemini_live_routes.py:55-70` (config import), `:566-634` (`save_session_to_blackbox`)
- Test: `Orchestrator/tests/test_voice_transcript_save.py`

Same pattern as P1.22/P1.23. Touch ONLY the save function and imports — the surrounding file is being modified by P1.1–P1.19; anchor by text.

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_transcript_save.py`:

```python
import Orchestrator.routes.gemini_live_routes as gm
from Orchestrator.models import GeminiLiveSession


def test_gemini_save_clears_on_success(monkeypatch):
    session = _session(GeminiLiveSession, "t-gm-ok")
    captured = _run_save(monkeypatch, gm, gm.save_session_to_blackbox, session, ok=True)
    assert session.conversation == []
    assert "Gemini Live Voice Session" in captured["session_summary"]
    assert captured["model_label"] == "gemini-live-voice"


def test_gemini_save_keeps_transcript_on_failure(monkeypatch):
    session = _session(GeminiLiveSession, "t-gm-fail")
    _run_save(monkeypatch, gm, gm.save_session_to_blackbox, session, ok=False)
    assert len(session.conversation) == 2
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_transcript_save.py -v -k gemini`
Expected: FAIL with `AttributeError: ... does not have the attribute 'save_voice_transcript'`

**Step 3: Write minimal implementation**

In `Orchestrator/routes/gemini_live_routes.py`:

(a) If `GEMINI_MODEL_DEFAULT` (config import, line 62) has no remaining uses after this edit, delete it from the import block — verify with `grep -n GEMINI_MODEL_DEFAULT Orchestrator/routes/gemini_live_routes.py` first (P1a edits may have added uses).

(b) Below `from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK` add:

```python
from Orchestrator.routes.voice_ws_shared import save_voice_transcript
```

(c) In `save_session_to_blackbox` (gemini version, `=== Gemini Live Voice Session ===` summary), replace everything from `    print(f"[GEMINI-LIVE] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")` through the trailing `    session.conversation = []` with:

```python
    print(f"[GEMINI-LIVE] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")

    saved = await save_voice_transcript(
        operator=session.operator,
        user_message=f"[Voice Session Transcript] Gemini Live voice session {session.session_id}",
        session_summary=session_summary,
        model_label="gemini-live-voice",
        log_prefix="[GEMINI-LIVE]",
    )

    # Clear ONLY after a confirmed 200 (P1b cross-route contract).
    if saved:
        session.conversation = []
    else:
        print(f"[GEMINI-LIVE] Save FAILED — keeping {len(session.conversation)} turns for a later retry")
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_transcript_save.py -v && Orchestrator/venv/bin/python -c "import Orchestrator.routes.gemini_live_routes"`
Expected: PASS (6 tests), import exits 0

**Step 5: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_voice_transcript_save.py
git commit -m "fix(gemini-live): /chat/save persistence with clear-only-on-200 (P1b)"
```

---

### Task P1.25: OpenAI route — never dangle a tool call; parse-error on malformed arguments

**Files:**
- Modify: `Orchestrator/routes/realtime_routes.py:701-704` (args parse), `:1308-1311` (listener per-message catch)
- Test: `Orchestrator/tests/test_voice_tool_dispatch_errors.py`

Two defects: (1) any raise inside the dispatch chain (:716-1059) propagates to `openai_listener`'s per-message catch and the `function_call_output` is never sent — the model waits forever; (2) malformed arguments JSON falls back to `arguments = {}` and the tool executes with wrong args, masking the real cause.

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_tool_dispatch_errors.py`:

```python
"""P1b: tool-dispatch exceptions and malformed args must answer the model, never dangle."""
import asyncio
import json

import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS, FakeStreamWS


def test_realtime_malformed_args_returns_parse_error(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("tool must NOT execute on malformed args")
    monkeypatch.setattr(rt, "execute_search_snapshots", boom)

    async def run():
        session = RealtimeSession(session_id="t-badargs", operator="system")
        session.openai_ws = FakeUpstreamWS()
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-7", "name": "search_snapshots",
            "arguments": "{not valid json",
        }
        await rt.handle_openai_message(session, event)
        assert [m["type"] for m in session.openai_ws.sent] == \
            ["conversation.item.create", "response.create"]
        item = session.openai_ws.sent[0]["item"]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == "call-7"
        assert "Malformed tool-call arguments" in item["output"]
    asyncio.run(run())


def test_realtime_listener_answers_dangling_call_on_dispatch_crash(monkeypatch):
    async def crash(session, event):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(rt, "handle_openai_message", crash)

    async def run():
        session = RealtimeSession(session_id="t-crash", operator="system")
        fc_event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-9", "name": "roll_dice", "arguments": "{}",
        }
        ws = FakeStreamWS([json.dumps(fc_event)])
        session.openai_ws = ws
        await rt.openai_listener(session)
        assert [m["type"] for m in ws.sent] == \
            ["conversation.item.create", "response.create"], \
            "a dispatch crash must still answer the call_id with an error payload"
        item = ws.sent[0]["item"]
        assert item["call_id"] == "call-9"
        assert "executor exploded" in item["output"]
    asyncio.run(run())


def test_realtime_normal_dispatch_still_answers():
    async def run():
        session = RealtimeSession(session_id="t-ok", operator="system")
        session.openai_ws = FakeUpstreamWS()
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-1", "name": "get_current_time", "arguments": "{}",
        }
        await rt.handle_openai_message(session, event)
        assert [m["type"] for m in session.openai_ws.sent] == \
            ["conversation.item.create", "response.create"]
        assert "Current date and time" in session.openai_ws.sent[0]["item"]["output"]
    asyncio.run(run())
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tool_dispatch_errors.py -v`
Expected: FAIL — `test_realtime_malformed_args...` errors with `AssertionError: tool must NOT execute on malformed args`; `test_realtime_listener_answers...` fails with `assert [] == ['conversation.item.create', 'response.create']`; `test_realtime_normal_dispatch_still_answers` PASSES (baseline guard).

**Step 3: Write minimal implementation**

In `Orchestrator/routes/realtime_routes.py`:

(a) Extend the P1.22 import to:

```python
from Orchestrator.routes.voice_ws_shared import (
    save_voice_transcript,
    send_openai_style_tool_error,
)
```

(b) In `handle_openai_message`, replace:

```python
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            arguments = {}
```

with:

```python
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as parse_err:
            # P1b: malformed arguments must NOT execute with {} — that masks
            # the real cause (e.g. search_snapshots "No search query provided").
            # Return a parse error so the model can retry with valid JSON.
            print(f"[REALTIME] Malformed tool arguments for {name}: {parse_err}")
            await send_openai_style_tool_error(
                session.openai_ws, session.portal_ws, event,
                ValueError(f"Malformed tool-call arguments JSON: {parse_err}. "
                           f"Raw arguments: {arguments_str[:200]}"),
            )
            return
```

(c) In `openai_listener`, replace:

```python
            except Exception as e:
                print(f"[REALTIME] Error handling OpenAI message: {e}")
```

with:

```python
            except Exception as e:
                print(f"[REALTIME] Error handling OpenAI message: {e}")
                # P1b: never dangle a tool call — if this event was a function
                # call, answer it with an error payload + response.create so
                # the model recovers instead of waiting forever (dead air).
                await send_openai_style_tool_error(
                    session.openai_ws, session.portal_ws, event, e)
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tool_dispatch_errors.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_voice_tool_dispatch_errors.py
git commit -m "fix(realtime): tool-dispatch crashes and malformed args answer the model instead of dangling the call_id (P1b)"
```

---

### Task P1.26: Grok route — same dispatch-error + parse-error hardening

**Files:**
- Modify: `Orchestrator/routes/grok_live_routes.py:624-628` (args parse), `:1254-1257` (listener per-message catch)
- Test: `Orchestrator/tests/test_voice_tool_dispatch_errors.py`

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_tool_dispatch_errors.py`:

```python
import Orchestrator.routes.grok_live_routes as gk
from Orchestrator.models import GrokLiveSession


def test_grok_malformed_args_returns_parse_error(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("tool must NOT execute on malformed args")
    monkeypatch.setattr(gk, "execute_grok_search_snapshots", boom)

    async def run():
        session = GrokLiveSession(session_id="t-gk-badargs", operator="system")
        session.grok_ws = FakeUpstreamWS()
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-8", "name": "search_snapshots",
            "arguments": "{not valid json",
        }
        await gk.handle_grok_message(session, event)
        assert [m["type"] for m in session.grok_ws.sent] == \
            ["conversation.item.create", "response.create"]
        item = session.grok_ws.sent[0]["item"]
        assert item["call_id"] == "call-8"
        assert "Malformed tool-call arguments" in item["output"]
    asyncio.run(run())


def test_grok_listener_answers_dangling_call_on_dispatch_crash(monkeypatch):
    async def crash(session, event):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(gk, "handle_grok_message", crash)

    async def run():
        session = GrokLiveSession(session_id="t-gk-crash", operator="system")
        fc_event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-10", "name": "roll_dice", "arguments": "{}",
        }
        ws = FakeStreamWS([json.dumps(fc_event)])
        session.grok_ws = ws
        await gk.grok_listener(session)
        assert [m["type"] for m in ws.sent] == \
            ["conversation.item.create", "response.create"]
        item = ws.sent[0]["item"]
        assert item["call_id"] == "call-10"
        assert "executor exploded" in item["output"]
    asyncio.run(run())
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tool_dispatch_errors.py -v -k grok`
Expected: FAIL — malformed-args test errors with the AssertionError sentinel; listener test fails with `assert [] == [...]`

**Step 3: Write minimal implementation**

In `Orchestrator/routes/grok_live_routes.py`:

(a) Extend the P1.23 import to:

```python
from Orchestrator.routes.voice_ws_shared import (
    save_voice_transcript,
    send_openai_style_tool_error,
)
```

(b) In `handle_grok_message`, replace:

```python
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            print(f"[GROK-LIVE] ERROR: Failed to parse arguments JSON")
            arguments = {}
```

with:

```python
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as parse_err:
            # P1b: malformed arguments must NOT execute with {} — return a
            # parse error to the model so it can retry with valid JSON.
            print(f"[GROK-LIVE] Malformed tool arguments for {name}: {parse_err}")
            await send_openai_style_tool_error(
                session.grok_ws, session.portal_ws, event,
                ValueError(f"Malformed tool-call arguments JSON: {parse_err}. "
                           f"Raw arguments: {arguments_str[:200]}"),
            )
            return
```

(c) In `grok_listener`, replace:

```python
            except Exception as e:
                print(f"[GROK-LIVE] Error handling Grok message: {e}")
```

with:

```python
            except Exception as e:
                print(f"[GROK-LIVE] Error handling Grok message: {e}")
                # P1b: never dangle a tool call — answer function-call events
                # with an error payload so Grok recovers instead of stalling.
                await send_openai_style_tool_error(
                    session.grok_ws, session.portal_ws, event, e)
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tool_dispatch_errors.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_voice_tool_dispatch_errors.py
git commit -m "fix(grok-live): tool-dispatch crashes and malformed args answer the model instead of dangling the call_id (P1b)"
```

---

### Task P1.27: Gemini route — dispatch-error responder with answered-id tracking

**Files:**
- Modify: `Orchestrator/routes/gemini_live_routes.py:912-920` (toolCall branch head), `:1289-1290` (functionResponse send), `:1492-1493` (listener per-message catch)
- Test: `Orchestrator/tests/test_voice_tool_dispatch_errors.py`

Gemini's `toolCall` event carries a LIST of functionCalls; a raise mid-loop dangles every not-yet-answered id. Track answered ids on the session (plain attribute — do NOT add a `GeminiLiveSession` dataclass field; P1a owns models.py edits for Gemini) so the listener-level responder answers only the dangling ones. Gemini needs no malformed-args handling — `fc.get("args", {})` arrives pre-parsed from the protocol.

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_tool_dispatch_errors.py`:

```python
import Orchestrator.routes.gemini_live_routes as gm
from Orchestrator.models import GeminiLiveSession


def test_gemini_listener_answers_dangling_calls_on_dispatch_crash(monkeypatch):
    async def crash(session, event):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(gm, "handle_gemini_message", crash)

    async def run():
        session = GeminiLiveSession(session_id="t-gm-crash", operator="system")
        event = {"toolCall": {"functionCalls": [
            {"id": "fc-a", "name": "web_fetch", "args": {"url": "https://x"}},
            {"id": "fc-b", "name": "roll_dice", "args": {}},
        ]}}
        ws = FakeStreamWS([json.dumps(event)])
        session.gemini_ws = ws
        await gm.gemini_listener(session)
        assert len(ws.sent) == 1, "crash must produce ONE toolResponse frame"
        responses = ws.sent[0]["toolResponse"]["functionResponses"]
        assert [r["id"] for r in responses] == ["fc-a", "fc-b"]
        assert all("executor exploded" in r["response"]["result"] for r in responses)
    asyncio.run(run())


def test_gemini_dispatch_records_answered_ids():
    async def run():
        session = GeminiLiveSession(session_id="t-gm-ids", operator="system")
        session.gemini_ws = FakeUpstreamWS()
        event = {"toolCall": {"functionCalls": [
            {"id": "fc-t", "name": "get_current_time", "args": {}},
        ]}}
        await gm.handle_gemini_message(session, event)
        assert session.answered_tool_call_ids == {"fc-t"}, \
            "dispatch must record answered ids so the error responder never double-answers"
        responses = session.gemini_ws.sent[0]["toolResponse"]["functionResponses"]
        assert responses[0]["id"] == "fc-t"
    asyncio.run(run())
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tool_dispatch_errors.py -v -k gemini`
Expected: FAIL — listener test with `assert 0 == 1` (no toolResponse frame); answered-ids test with `AttributeError: 'GeminiLiveSession' object has no attribute 'answered_tool_call_ids'`

**Step 3: Write minimal implementation**

In `Orchestrator/routes/gemini_live_routes.py`:

(a) Below `from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK` (P1.24 already added `save_voice_transcript` — extend that import):

```python
from Orchestrator.routes.voice_ws_shared import (
    save_voice_transcript,
    send_gemini_tool_error,
)
```

(b) In `handle_gemini_message`, after:

```python
        session.pending_tool_call = True
        print(f"[GEMINI-LIVE] Tool call detected")
```

add:

```python
        # P1b: reset per-event answered-id tracking. The listener-level error
        # responder uses this to answer ONLY dangling ids after a mid-loop crash.
        session.answered_tool_call_ids = set()
```

(c) After the functionResponse send:

```python
                await session.gemini_ws.send(json.dumps(tool_response))
                print(f"[GEMINI-LIVE] Sent tool response for {name}: {len(result)} chars")
```

add (same 16-space indent as the two lines above):

```python
                session.answered_tool_call_ids.add(call_id)
```

(d) In `gemini_listener`, replace:

```python
            except Exception as e:
                print(f"[GEMINI-LIVE] Error handling Gemini message: {e}")
```

with:

```python
            except Exception as e:
                print(f"[GEMINI-LIVE] Error handling Gemini message: {e}")
                # P1b: never dangle a functionCall — answer every id this event
                # carried that the dispatch loop did not already answer.
                await send_gemini_tool_error(
                    session.gemini_ws, session.portal_ws, event, e,
                    getattr(session, "answered_tool_call_ids", None))
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tool_dispatch_errors.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_voice_tool_dispatch_errors.py
git commit -m "fix(gemini-live): dispatch crashes answer all dangling functionCall ids via error functionResponses (P1b)"
```

---

### Task P1.28: Un-freeze REALTIME_TOOLS + GROK_LIVE_TOOLS — read at configure time

**Files:**
- Modify: `Orchestrator/routes/realtime_routes.py:87-91` (frozen constant), `:514-543` (config_event), `Orchestrator/routes/grok_live_routes.py:84-88` (frozen constant), `:435-468` (config_event + debug prints)
- Test: `Orchestrator/tests/test_voice_tools_configure_time.py`

Both constants are import-time snapshots — `POST /toolvault/reload` never reaches live voice; a restart is required for schema edits (this exact freeze is why the June 20 tool addition detonated invisibly on Gemini). Match the P1a Gemini change: call `get_openai_realtime_tools(group)` inside the configure function. `tool_registry.reset_cache()` (invoked by `/toolvault/reload`) then makes the next session/reconnect pick up edits. No other module imports these constants (verified: only self-references).

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_tools_configure_time.py`:

```python
"""P1b: voice routes must read ToolVault tools at session-configure time, not import time."""
import asyncio

import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS


def _patch_light(monkeypatch, module):
    """Avoid heavy retrieval/persona work inside configure_* during the test."""
    monkeypatch.setattr(module, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))
    monkeypatch.setattr(module, "get_persona",
                        lambda operator, modality: "Test persona.")


def test_no_import_time_tool_snapshots():
    assert not hasattr(rt, "REALTIME_TOOLS"), \
        "REALTIME_TOOLS import-time freeze must be removed (toolvault/reload blind spot)"
    assert not hasattr(gk, "GROK_LIVE_TOOLS"), \
        "GROK_LIVE_TOOLS import-time freeze must be removed (toolvault/reload blind spot)"


def test_grok_configure_reads_tools_fresh_each_time(monkeypatch):
    _patch_light(monkeypatch, gk)
    calls = []

    def fake_get(group):
        calls.append(group)
        return [{"type": "function", "name": f"tool_v{len(calls)}", "parameters": {}}]

    monkeypatch.setattr(gk, "get_openai_realtime_tools", fake_get)

    async def run():
        session = GrokLiveSession(session_id="t-gk-tools", operator="system")
        ws = FakeUpstreamWS()
        session.grok_ws = ws
        await gk.configure_grok_session(session, "system", "Ara")
        await gk.configure_grok_session(session, "system", "Ara")
        assert calls == ["grok_live", "grok_live"]
        assert ws.sent[-1]["session"]["tools"][0]["name"] == "tool_v2", \
            "second configure must carry the FRESH tool list"
    asyncio.run(run())


def test_realtime_configure_reads_tools_fresh_each_time(monkeypatch):
    _patch_light(monkeypatch, rt)
    calls = []

    def fake_get(group):
        calls.append(group)
        return [{"type": "function", "name": f"tool_v{len(calls)}", "parameters": {}}]

    monkeypatch.setattr(rt, "get_openai_realtime_tools", fake_get)

    async def run():
        session = RealtimeSession(session_id="t-rt-tools", operator="system")
        ws = FakeUpstreamWS()
        session.openai_ws = ws
        await rt.configure_openai_session(session, "system", "ash")
        await rt.configure_openai_session(session, "system", "ash")
        assert calls == ["realtime", "realtime"]
        assert ws.sent[-1]["session"]["tools"][0]["name"] == "tool_v2"
    asyncio.run(run())
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tools_configure_time.py -v`
Expected: FAIL — `test_no_import_time_tool_snapshots` with `AssertionError: REALTIME_TOOLS import-time freeze must be removed`; the fresh-read tests with `assert [] == ['grok_live', 'grok_live']` (the frozen constant means the patched getter is never called)

**Step 3: Write minimal implementation**

(a) `Orchestrator/routes/realtime_routes.py` — replace:

```python
# =============================================================================
# Tool Definitions for GPT Realtime
# =============================================================================

REALTIME_TOOLS = get_openai_realtime_tools("realtime")
```

with:

```python
# =============================================================================
# Tool Definitions — read FRESH at session-configure time (P1b)
# =============================================================================
# No import-time snapshot here: get_openai_realtime_tools("realtime") is called
# inside configure_openai_session, so POST /toolvault/reload (which busts the
# registry cache) reaches the NEXT voice session/reconnect without a restart.
```

(b) In `configure_openai_session`, directly above the `# Configure session — GA wire format (Beta deprecated 2026-05-19).` comment, insert:

```python
    # P1b: read tools FRESH (not at import) so /toolvault/reload reaches voice.
    realtime_tools = get_openai_realtime_tools("realtime")
```

and change `            "tools": REALTIME_TOOLS,` to `            "tools": realtime_tools,`.

(c) `Orchestrator/routes/grok_live_routes.py` — replace:

```python
# =============================================================================
# Tool Definitions for Grok Voice Agent
# =============================================================================

GROK_LIVE_TOOLS = get_openai_realtime_tools("grok_live")
```

with:

```python
# =============================================================================
# Tool Definitions — read FRESH at session-configure time (P1b)
# =============================================================================
# No import-time snapshot here: get_openai_realtime_tools("grok_live") is
# called inside configure_grok_session, so POST /toolvault/reload reaches the
# NEXT voice session/reconnect without a restart.
```

(d) In `configure_grok_session`, directly above `    # Configure session - Grok uses nested audio format structure`, insert:

```python
    # P1b: read tools FRESH (not at import) so /toolvault/reload reaches voice.
    grok_live_tools = get_openai_realtime_tools("grok_live")
```

then change `            "tools": GROK_LIVE_TOOLS,` to `            "tools": grok_live_tools,` and the two debug prints to:

```python
    print(f"[GROK-LIVE] Number of tools: {len(grok_live_tools)}")
    print(f"[GROK-LIVE] Tool names: {[t['name'] for t in grok_live_tools]}")
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_tools_configure_time.py -v && Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes, Orchestrator.routes.grok_live_routes"`
Expected: PASS (3 tests), import exits 0

**Step 5: Commit**

```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_voice_tools_configure_time.py
git commit -m "fix(voice): un-freeze REALTIME_TOOLS/GROK_LIVE_TOOLS — read ToolVault at configure time so /toolvault/reload reaches voice (P1b)"
```

---

### Task P1.29: OpenAI route — respawn the listener on reconnect

**Files:**
- Modify: `Orchestrator/models.py:96-102` (RealtimeSession reconnection-state block), `Orchestrator/routes/realtime_routes.py:1203-1236` (`openai_reconnect` try block), `:1479` (endpoint spawn), `:1546-1551` (endpoint finally)
- Test: `Orchestrator/tests/test_voice_listener_respawn.py`

Audit result (verified in source): `openai_listener` has exactly ONE spawn site (endpoint line 1479); `openai_reconnect` (:1168-1245) re-dials and reconfigures but never re-attaches a listener to the NEW ws — the old listener's `async for` is bound to the closed ws object and its task has already exited. After ANY staleness/close-triggered reconnect the session is a mute one-way pipe reporting "reconnected" — the exact Gemini bug class fixed in P1a. Fix identically: track the task on the session, cancel-then-respawn in reconnect, and cancel via the session field at teardown (the endpoint's local `openai_task` is stale after a respawn — cancelling only it would leak the live listener).

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_listener_respawn.py`:

```python
"""P1b: OpenAI + Grok reconnect paths must respawn their upstream listener task."""
import asyncio

import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS


async def _stuck_task(started):
    started.set()
    await asyncio.sleep(3600)


def _respawn_check(monkeypatch, module, session, reconnect_fn,
                   listener_name, connect_name, configure_name, ws_attr):
    async def run():
        started = asyncio.Event()
        session.listener_task = asyncio.create_task(_stuck_task(started))
        await started.wait()
        old_task = session.listener_task

        spawned = []

        async def fake_listener(s):
            spawned.append(s)

        async def fake_connect(s, *a, **k):
            setattr(s, ws_attr, FakeUpstreamWS())
            return True

        async def fake_configure(s, *a, **k):
            return None

        monkeypatch.setattr(module, listener_name, fake_listener)
        monkeypatch.setattr(module, connect_name, fake_connect)
        monkeypatch.setattr(module, configure_name, fake_configure)

        await reconnect_fn(session)

        # Old listener must be cancelled (it is bound to the OLD ws object).
        try:
            await asyncio.wait_for(old_task, timeout=2)
        except asyncio.CancelledError:
            pass
        assert old_task.done(), "old listener task was never cancelled"

        await asyncio.sleep(0)  # let the respawned task run
        assert spawned == [session], "reconnect must respawn the listener on the NEW ws"
        assert session.listener_task is not old_task
        assert session.status == "connected"
    asyncio.run(run())


def test_openai_reconnect_respawns_listener(monkeypatch):
    session = RealtimeSession(session_id="t-rt-respawn", operator="system")
    _respawn_check(monkeypatch, rt, session, rt.openai_reconnect,
                   "openai_listener", "connect_to_openai",
                   "configure_openai_session", "openai_ws")
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_listener_respawn.py -v`
Expected: FAIL with `asyncio.TimeoutError` (old listener never cancelled) or `AssertionError: reconnect must respawn the listener on the NEW ws`

**Step 3: Write minimal implementation**

(a) `Orchestrator/models.py` — in `RealtimeSession`, below the line `    intentional_disconnect: bool = False       # User clicked disconnect` (inside the `# Reconnection state` block of the REALTIME dataclass, not the Gemini/Grok ones), add:

```python
    listener_task: Optional[Any] = None        # asyncio.Task reading the upstream ws — cancelled+respawned on reconnect (P1b)
```

(b) `Orchestrator/routes/realtime_routes.py` — in `openai_reconnect`, replace:

```python
    try:
        # Close old connection
        if session.openai_ws:
```

with:

```python
    try:
        # P1b: cancel the old listener FIRST — it is bound to the OLD ws
        # object; left running it would observe our close() below and emit a
        # spurious "disconnected" to the client mid-recovery.
        if session.listener_task and not session.listener_task.done():
            session.listener_task.cancel()

        # Close old connection
        if session.openai_ws:
```

(c) Still in `openai_reconnect`, replace:

```python
            # Reset state
            session.reconnect_count = 0
```

with:

```python
            # P1b: respawn the listener on the NEW upstream ws. Without this
            # the session is a permanently mute one-way pipe that still
            # reports "reconnected" (same defect class as the Gemini P1a fix;
            # the phone bridge always had its own respawn loop).
            session.listener_task = asyncio.create_task(openai_listener(session))

            # Reset state
            session.reconnect_count = 0
```

(d) In the endpoint, replace:

```python
                    openai_task = asyncio.create_task(openai_listener(session))
```

with:

```python
                    openai_task = asyncio.create_task(openai_listener(session))
                    session.listener_task = openai_task
```

(e) In the endpoint `finally`, replace:

```python
        if openai_task:
            openai_task.cancel()
            try:
                await openai_task
            except asyncio.CancelledError:
                pass
```

with:

```python
        # P1b: cancel the CURRENT listener — after a reconnect this is a
        # different task than the locally-captured openai_task.
        listener = session.listener_task or openai_task
        if listener:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
        session.listener_task = None
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_listener_respawn.py -v`
Expected: PASS (1 test, ~0.5s — reconnect backoff sleep)

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_voice_listener_respawn.py
git commit -m "fix(realtime): reconnect respawns openai_listener on the new upstream ws — mute-after-reconnect bug (P1b)"
```

---

### Task P1.30: Grok route — respawn the listener on reconnect

**Files:**
- Modify: `Orchestrator/models.py:164-170` (GrokLiveSession reconnection-state block), `Orchestrator/routes/grok_live_routes.py:1149-1182` (`grok_reconnect` try block), `:1378` (endpoint spawn), `:1446-1451` (endpoint finally)
- Test: `Orchestrator/tests/test_voice_listener_respawn.py`

Same audit result as P1.29: `grok_listener` has ONE spawn site (endpoint line 1378); `grok_reconnect` (:1114-1191) never re-attaches. Apply the identical fix.

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_listener_respawn.py`:

```python
def test_grok_reconnect_respawns_listener(monkeypatch):
    session = GrokLiveSession(session_id="t-gk-respawn", operator="system")
    _respawn_check(monkeypatch, gk, session, gk.grok_reconnect,
                   "grok_listener", "connect_to_grok",
                   "configure_grok_session", "grok_ws")
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_listener_respawn.py -v -k grok`
Expected: FAIL with `asyncio.TimeoutError` or `AssertionError: reconnect must respawn the listener on the NEW ws`

**Step 3: Write minimal implementation**

(a) `Orchestrator/models.py` — in `GrokLiveSession`, below its `    intentional_disconnect: bool = False       # User clicked disconnect` line, add:

```python
    listener_task: Optional[Any] = None        # asyncio.Task reading the upstream ws — cancelled+respawned on reconnect (P1b)
```

(b) `Orchestrator/routes/grok_live_routes.py` — in `grok_reconnect`, replace:

```python
    try:
        # Close old connection
        if session.grok_ws:
```

with:

```python
    try:
        # P1b: cancel the old listener FIRST — it is bound to the OLD ws
        # object; left running it would observe our close() below and emit a
        # spurious "disconnected" to the client mid-recovery.
        if session.listener_task and not session.listener_task.done():
            session.listener_task.cancel()

        # Close old connection
        if session.grok_ws:
```

(c) Still in `grok_reconnect`, replace:

```python
            # Reset state
            session.reconnect_count = 0
```

with:

```python
            # P1b: respawn the listener on the NEW upstream ws (parity with
            # the OpenAI/Gemini fixes — mute-after-reconnect bug class).
            session.listener_task = asyncio.create_task(grok_listener(session))

            # Reset state
            session.reconnect_count = 0
```

(d) In the endpoint, replace:

```python
                    grok_task = asyncio.create_task(grok_listener(session))
```

with:

```python
                    grok_task = asyncio.create_task(grok_listener(session))
                    session.listener_task = grok_task
```

(e) In the endpoint `finally`, replace:

```python
        if grok_task:
            grok_task.cancel()
            try:
                await grok_task
            except asyncio.CancelledError:
                pass
```

with:

```python
        # P1b: cancel the CURRENT listener — after a reconnect this is a
        # different task than the locally-captured grok_task.
        listener = session.listener_task or grok_task
        if listener:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
        session.listener_task = None
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_listener_respawn.py -v`
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_voice_listener_respawn.py
git commit -m "fix(grok-live): reconnect respawns grok_listener on the new upstream ws — mute-after-reconnect bug (P1b)"
```

---

### Task P1.31: Fix the `get_recent_snapshots` / `list_recent_snapshots` prompt-tool mismatch (all 3 routes)

**Files:**
- Modify: `Orchestrator/routes/realtime_routes.py:375,400,476,718`, `Orchestrator/routes/grok_live_routes.py:320,423,426,859`, `Orchestrator/routes/gemini_live_routes.py:296,321,417,1144`
- Test: `Orchestrator/tests/test_voice_prompt_tool_names.py`

The system prompts mandate `get_recent_snapshots(count=3)` but the declared ToolVault groups ship only `list_recent_snapshots` (verified: `ToolVault/tools/list_recent_snapshots/` exists, no `get_recent_snapshots` module) — models can only call declared functions, so the mandated first call fails or gets substituted through the catch-all, bypassing each route's specialized inline handler (system-sees-all scoping for outbound-call handoff). **Chosen single approach for all three routes:** prompts say `list_recent_snapshots`, and each inline dispatch handler matches BOTH spellings so the declared name hits the specialized handler (not the generic ToolVault executor).

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_prompt_tool_names.py`:

```python
"""P1b tripwire: voice prompts must only mandate tools the declared groups contain."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROUTES = [
    REPO / "Orchestrator/routes/realtime_routes.py",
    REPO / "Orchestrator/routes/grok_live_routes.py",
    REPO / "Orchestrator/routes/gemini_live_routes.py",
]


def test_no_stale_get_recent_snapshots_references():
    for path in ROUTES:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "get_recent_snapshots" in line:
                assert "list_recent_snapshots" in line, (
                    f"{path.name}:{i} references get_recent_snapshots — NOT a declared "
                    f"ToolVault tool (models can only call declared functions): {line.strip()}"
                )


def test_declared_groups_carry_list_recent_snapshots():
    from Orchestrator.tools.tool_registry import (
        get_gemini_live_tools,
        get_openai_realtime_tools,
    )
    for group in ("realtime", "grok_live"):
        names = [t["name"] for t in get_openai_realtime_tools(group)]
        assert "list_recent_snapshots" in names
        assert "get_recent_snapshots" not in names
    assert "list_recent_snapshots" in json.dumps(get_gemini_live_tools("gemini_live"))
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_prompt_tool_names.py -v`
Expected: FAIL — `test_no_stale_get_recent_snapshots_references` reports the first stale line (`realtime_routes.py:375 ... search_snapshots and get_recent_snapshots ...`); `test_declared_groups_carry_list_recent_snapshots` PASSES (premise check)

**Step 3: Write minimal implementation**

Apply to EACH of the three route files (Edit with `replace_all: true` — each string occurs once per file except where noted):

(a) `You have access to search_snapshots and get_recent_snapshots for memory/context.`
→ `You have access to search_snapshots and list_recent_snapshots for memory/context.`

(b) `Use get_recent_snapshots immediately!`
→ `Use list_recent_snapshots immediately!`

(c) `IMMEDIATELY use get_recent_snapshots(count=3)`
→ `IMMEDIATELY use list_recent_snapshots(count=3)`

(d) Dispatch alias — in `realtime_routes.py` and `grok_live_routes.py` replace:

```python
        elif name == "get_recent_snapshots":
```

with:

```python
        elif name in ("get_recent_snapshots", "list_recent_snapshots"):
            # list_recent_snapshots is the declared ToolVault name; legacy
            # get_recent_snapshots is kept as a dispatch alias for list_recent_snapshots
            # so this specialized handler (system-sees-all scoping for
            # outbound-call context handoff) serves both instead of falling
            # to the catch-all.
```

and in `gemini_live_routes.py` (one indent level deeper) replace:

```python
            elif name == "get_recent_snapshots":
```

with:

```python
            elif name in ("get_recent_snapshots", "list_recent_snapshots"):
                # list_recent_snapshots is the declared ToolVault name; legacy
                # spelling kept as an alias for the specialized handler.
```

(The inline handlers already read `count` — matching `list_recent_snapshots`'s schema parameter — no body changes needed.)

**Step 4: Run test to verify it passes**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_prompt_tool_names.py Orchestrator/tests/test_voice_tool_dispatch_errors.py -v`
Expected: PASS (2 + 7 tests — the dispatch tests guard against alias-edit breakage)

**Step 5: Commit**

```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/routes/grok_live_routes.py Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_voice_prompt_tool_names.py
git commit -m "fix(voice): prompts mandate the declared list_recent_snapshots tool; dispatch aliases keep the specialized handler (P1b)"
```

---

### Task P1.32: Correct stale comments — "~21 tools" and "GPT-4o Realtime"

**Files:**
- Modify: `Orchestrator/tools/tool_registry.py:13-20`, `Orchestrator/routes/realtime_routes.py:1-17,1334`, `Orchestrator/models.py:76-81`

Comment-only task — no behavior change, so no new test; exact verification commands replace the TDD steps.

**Step 1: Apply the edits**

(a) `Orchestrator/tools/tool_registry.py` — replace:

```
Groups control which tools appear for each consumer:
  chat         - REST chat handlers (all providers, ~32 tools)
  chat_cu      - Computer Use agent (chat minus use_computer itself)
  realtime     - OpenAI Realtime voice WebSocket (~21 tools)
  gemini_live  - Gemini Live voice WebSocket (~21 tools)
  grok_live    - Grok Live voice WebSocket (~21 tools)
  phone        - Phone bridge / blackbox_tools.py (~24 tools)
  mcp          - MCP server for Claude Code (~30 tools)
```

with:

```
Groups control which tools appear for each consumer. Membership is declared
per-tool in ToolVault/tools/<name>/schema.json "groups" arrays; counts drift
as tools land, so none are baked in here (each of the three voice groups
carried 56 tools as of 2026-07-11 — NOT the ~21 this docstring once claimed):
  chat         - REST chat handlers (all providers)
  chat_cu      - Computer Use agent (chat minus use_computer itself)
  realtime     - OpenAI Realtime voice WebSocket
  gemini_live  - Gemini Live voice WebSocket
  grok_live    - Grok Live voice WebSocket
  phone        - Phone bridge / blackbox_tools.py
  mcp          - MCP server for Claude Code
```

(b) `Orchestrator/routes/realtime_routes.py` — replace:

```
realtime_routes.py - GPT-4o Realtime API WebSocket Bridge

This module provides a WebSocket bridge between the Portal frontend and
OpenAI's GPT-4o Realtime API, enabling real-time voice conversations with
```

with:

```
realtime_routes.py - OpenAI Realtime API (GA) WebSocket Bridge

This module provides a WebSocket bridge between the Portal frontend and
OpenAI's Realtime API (gpt-realtime model family; the GPT-4o Realtime line
shut down 2026-05-07), enabling real-time voice conversations with
```

and replace `    WebSocket endpoint for GPT-4o Realtime API bridge.` with `    WebSocket endpoint for the OpenAI Realtime API bridge.`

(c) `Orchestrator/models.py` — replace `# GPT-4o Realtime API Session Management` with `# OpenAI Realtime API Session Management`, and `    """Represents a GPT-4o Realtime API session for an operator."""` with `    """Represents an OpenAI Realtime API session for an operator."""`

**Step 2: Verify no stale strings remain**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && grep -n "GPT-4o" Orchestrator/routes/realtime_routes.py Orchestrator/models.py; grep -n "~21 tools\|~32 tools\|~24 tools\|~30 tools" Orchestrator/tools/tool_registry.py; echo "grep-clean"`
Expected: only `grep-clean` printed (both greps empty)

**Step 3: Verify the tree still imports and the phase's tests are green**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes, Orchestrator.models, Orchestrator.tools.tool_registry" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_ws_shared.py Orchestrator/tests/test_voice_transcript_save.py Orchestrator/tests/test_voice_tool_dispatch_errors.py Orchestrator/tests/test_voice_tools_configure_time.py Orchestrator/tests/test_voice_listener_respawn.py Orchestrator/tests/test_voice_prompt_tool_names.py -q`
Expected: import exits 0; all Phase-1b tests PASS (29 tests: 9 + 6 + 7 + 3 + 2 + 2)

**Step 4: Commit**

```bash
git add Orchestrator/tools/tool_registry.py Orchestrator/routes/realtime_routes.py Orchestrator/models.py
git commit -m "docs(voice): correct stale '~21 tools' group counts and GPT-4o Realtime references (P1b)"
```

---

**Phase 1b exit criteria:** all 6 new test files green (29 tests — test_voice_ws_shared 9, test_voice_transcript_save 6, test_voice_tool_dispatch_errors 7, test_voice_tools_configure_time 3, test_voice_listener_respawn 2, test_voice_prompt_tool_names 2); `python -c "import Orchestrator.routes.realtime_routes, Orchestrator.routes.grok_live_routes, Orchestrator.routes.gemini_live_routes"` clean; no `REALTIME_TOOLS`/`GROK_LIVE_TOOLS` import-time snapshots; no `get_recent_snapshots` reference without its `list_recent_snapshots` alias; transcripts persist via `/chat/save` and survive failed saves. Live-service smoke (after the P1 rollout restart, together with P1a): open a Portal voice session per provider, force a tool error (e.g. `web_fetch` with an unroutable URL), confirm the model speaks the failure instead of going silent; then `sudo journalctl -u blackbox.service | grep "Transcript saved via /chat/save"` after disconnect.

---

### Task P2.1: P0 probe-results gate (no code — hard stop check)

**Files:**
- Read: diagnostics/voice_probes/results/ (written by Phase 0)

Every config/catalog edit in this phase is gated on the P0 live probes. This task verifies the results exist BEFORE any file is touched. If any check fails, STOP the phase and run/re-run the P0 probe tasks.

**Step 1: Verify the results directory exists and is non-empty**
Run: `ls -la /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/`
Expected: at least one `*.json` results file. If the directory is missing or empty: **STOP — Phase 0 has not run.**

**Step 2: Verify OpenAI model probe results**
Run:
```bash
Orchestrator/venv/bin/python -c "
import json, glob
p = sorted(glob.glob('/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/*-openai-models.json'))[-1]
for r in json.load(open(p))['results']:
    print(r['model'], '->', 'OK' if r['ok'] else f\"FAIL close={r['close_code']} err={r['error'][:120]}\")
"
```
Expected: result entries for `gpt-realtime-2.1` AND `gpt-realtime-2.1-mini` with `"ok": true` (P0's `ProbeResult` schema records `ok`/`close_code`/`close_reason`/`error` — there is NO `accepted`/`verdict` key), plus a re-probe result for `gpt-realtime-2025-08-28` (either `ok` value is a valid finding — it decides the P2.2 Step 3 NOTE). If 2.1/2.1-mini show `"ok": false`: **STOP — do not execute P2.2.**

**Step 3: Verify Grok probe results (model, transcription, sample-rate)**
Run:
```bash
Orchestrator/venv/bin/python -c "
import json, glob
p = sorted(glob.glob('/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/*-xai.json'))[-1]
for r in json.load(open(p))['results']:
    print(r['probe'], r['model'] or '(default)', '->', 'OK' if r['ok'] else f\"FAIL close={r['close_code']} err={r['error'][:120]}\", '|', r['notes'][:160])
"
```
Expected (probe names + result keys are exactly what P0.4's xai suite writes — `ProbeResult.ok`, not "accepted"/"verdict"): (a) the `handshake` results for `grok-voice-latest` and `grok-voice-think-fast-1.0` with `"ok": true` at `wss://api.x.ai/v1/realtime?model=`; (b) the `transcription_default` result's `event_types=` note (is input transcription on without opt-in?) AND the `transcription_shape` result with `"ok": true` — its recorded `session.updated` event carries the accepted `audio.input.transcription` shape that P2.11 mirrors; (c) the `input_rate_16k` result with `"ok": true` — xAI accepted `audio.input.format.rate: 16000` (drives the P2.15 branch choice; `"ok": false` here → P2.15 Branch B). Record the three verdicts in the task notes — P2.11 and P2.15 branch on them.

**Step 4: Commit nothing** — read-only task. Output the three verdicts as the task result.

---

### Task P2.2: OpenAI Realtime catalog — add gpt-realtime-2.1 (new default) + 2.1-mini, pin marin/cedar

**Files:**
- Modify: Orchestrator/config.py:500 (default), Orchestrator/config.py:505-524 (catalog + comment)
- Test: Orchestrator/tests/test_live_models.py:131,142-147,318-330

**Step 1: Write the failing test**
Edit `Orchestrator/tests/test_live_models.py`. Replace the count assertion at line 131:
```python
    # Exactly the 7 chat-category models, no whisper/translate
    assert len(models) == 7, f"expected 7 chat models, got {len(models)}: {model_ids}"
```
Replace the default-model assertions at lines 142-147:
```python
    # gpt-realtime-2.1 (newest GA, 2026-07-06, P0 WS-probe-verified) present +
    # flagged default. gpt-realtime-2 stays in the catalog (same price, superseded).
    default_models = [m for m in models if m.get("default") is True]
    assert len(default_models) == 1, f"expected exactly one default model, got {default_models}"
    assert default_models[0]["id"] == "gpt-realtime-2.1"
    assert "gpt-realtime-2.1-mini" in model_ids
    assert "gpt-realtime-2" in model_ids
```
In `test_allowlist_casing_precision` after line 322 (`assert any(m["id"] == "gpt-realtime-1.5" ...)`), add:
```python
    assert any(m["id"] == "gpt-realtime-2.1" for m in OPENAI_REALTIME_MODELS)
    assert any(m["id"] == "gpt-realtime-2.1-mini" for m in OPENAI_REALTIME_MODELS)
```
Append a new test at end of file (marin/cedar are already in `OPENAI_REALTIME_VOICES` at config.py:528-531 — this pins them against regression, per design "ensure marin and cedar in the voice catalog"):
```python
@pytest.mark.asyncio
async def test_realtime_status_serves_marin_and_cedar():
    """marin/cedar are OpenAI's recommended premium voices — pin them in the
    served catalog (config.py OPENAI_REALTIME_VOICES via /realtime/status)."""
    resp = await realtime_status()
    assert "marin" in resp["voices"]
    assert "cedar" in resp["voices"]
    assert resp["voice_default"] in resp["voices"]
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_live_models.py -x -q`
Expected: FAIL with `expected 7 chat models, got 5`

**Step 3: Write minimal implementation**
In `Orchestrator/config.py` replace line 500:
```python
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")  # Newest GA (2026-07-06), P0 WS-probe-verified 2026-07-11
```
Replace the comment block + catalog at lines 505-524 with:
```python
# OpenAI Realtime model catalog — empirically WS-connection-tested 2026-05-19
# (GA endpoint) and re-probed 2026-07-11 for the 2.1 generation
# (see diagnostics/voice_probes/results/). Findings:
#   - gpt-realtime-2.1 / gpt-realtime-2.1-mini (GA 2026-07-06) are the newest;
#     same price as gen-2, better ASR/interruptions/noise, WS-probe-verified.
#   - gpt-realtime-2 kept (superseded flagship, still GA).
#   - gpt-realtime-mini-2025-12-15 pin kept — NOT affected by the 2026-07-23
#     shutdown of the 2025-10-06 mini snapshots.
#   - gpt-realtime-2025-08-28: listed by /v1/models but was REJECTED at the WS
#     endpoint (close 4000) in May 2026; keep out of the catalog unless the
#     2026-07-11 re-probe result says accepted.
# Routes filter category=="chat" when serving the dropdown; specialized variants
# (translate, transcribe) are exposed via env-var override only.
OPENAI_REALTIME_MODELS: List[Dict] = [
    # Conversational variants (UI dropdown) — all WS-connection-verified on GA endpoint
    {"id": "gpt-realtime-2.1", "name": "GPT Realtime 2.1 (Newest GA)", "default": True, "category": "chat"},
    {"id": "gpt-realtime-2.1-mini", "name": "GPT Realtime 2.1 Mini (cheap, newest)", "category": "chat"},
    {"id": "gpt-realtime-2", "name": "GPT Realtime 2", "category": "chat"},
    {"id": "gpt-realtime", "name": "GPT Realtime (GA alias)", "category": "chat"},
    {"id": "gpt-realtime-1.5", "name": "GPT Realtime 1.5 (pinned)", "category": "chat"},
    {"id": "gpt-realtime-mini", "name": "GPT Realtime Mini (cheap, alias)", "category": "chat"},
    {"id": "gpt-realtime-mini-2025-12-15", "name": "GPT Realtime Mini (Dec 2025 pin)", "category": "chat"},
    # Specialized variants (NOT in main dropdown; audit I4)
    {"id": "gpt-realtime-translate", "name": "GPT Realtime Translate", "category": "translate"},
    {"id": "gpt-realtime-whisper", "name": "GPT Realtime Whisper (STT-only)", "category": "transcribe"},
]
```
NOTE: if the P2.1 re-probe showed `gpt-realtime-2025-08-28` is now ACCEPTED, additionally append `{"id": "gpt-realtime-2025-08-28", "name": "GPT Realtime (Aug 2025 pin)", "category": "chat"}`, delete the `rejected` guard block at test_live_models.py:324-330, and bump the count assertion to 8. Otherwise leave both as written.

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_live_models.py -x -q`
Expected: PASS (all tests)

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/tests/test_live_models.py
git commit -m "feat(realtime): gpt-realtime-2.1 family in catalog, 2.1 new default (P0 probe-verified)"
```

---

### Task P2.3: OpenAI noise_reduction — session param + near_field default on the phone bridge

**Files:**
- Modify: Orchestrator/config.py:536 (insert allowlist after `OPENAI_REALTIME_VAD_EAGERNESS`)
- Modify: Orchestrator/routes/realtime_routes.py:43-57 (import), :300-310 (signature), :349-352 (after idle clamp), :543-545 (before send)
- Test: Orchestrator/tests/test_realtime_p2_upgrades.py (create)

**Step 1: Write the failing test**
Create `Orchestrator/tests/test_realtime_p2_upgrades.py`:
```python
"""P2 — OpenAI Realtime GA session upgrades: noise_reduction + transcription delay.

Follows the fixtures/conventions of test_live_models.py (stubbed fossil
context, MagicMock session, single-send payload extraction).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.routes.realtime_routes import configure_openai_session


@pytest.fixture
def stub_fossil_context(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _stub
    )


def _make_openai_session(session_id="test-session"):
    session = MagicMock()
    session.session_id = session_id
    session.openai_ws = MagicMock()
    session.openai_ws.send = AsyncMock()
    session.provenance = {}
    session.context_injected = False
    return session


def _extract_payload(send_mock):
    assert send_mock.await_count == 1
    return json.loads(send_mock.await_args.args[0])


# ---------------------------------------------------------------------------
# noise_reduction (GA schema: audio.input.noise_reduction = {type} | null)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noise_reduction_emitted_when_requested(stub_fossil_context):
    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="test_operator", voice="ash",
        noise_reduction="far_field",
    )
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert audio_input["noise_reduction"] == {"type": "far_field"}


@pytest.mark.asyncio
async def test_noise_reduction_absent_by_default_for_portal(stub_fossil_context):
    session = _make_openai_session(session_id="portal-uuid-1234")
    await configure_openai_session(session=session, operator="test_operator", voice="ash")
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert "noise_reduction" not in audio_input


@pytest.mark.asyncio
async def test_noise_reduction_defaults_near_field_on_phone_bridge(stub_fossil_context):
    """phone/bridge.py sessions are keyed 'phone-<sid>' and call
    configure_openai_session positionally — the near_field default must apply
    with NO new argument at the call sites (signature stays backward-compatible)."""
    session = _make_openai_session(session_id="phone-CA1234567890")
    await configure_openai_session(session=session, operator="system", voice="ash")
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert audio_input["noise_reduction"] == {"type": "near_field"}


@pytest.mark.asyncio
async def test_noise_reduction_off_sends_explicit_null(stub_fossil_context):
    session = _make_openai_session(session_id="phone-CA999")
    await configure_openai_session(
        session=session, operator="system", voice="ash", noise_reduction="off",
    )
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert "noise_reduction" in audio_input
    assert audio_input["noise_reduction"] is None


@pytest.mark.asyncio
async def test_noise_reduction_invalid_ignored_with_warning(stub_fossil_context, capsys):
    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="test_operator", voice="ash",
        noise_reduction="ultra_field",
    )
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert "noise_reduction" not in audio_input
    out = capsys.readouterr().out
    assert "noise_reduction" in out and "WARNING" in out
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_realtime_p2_upgrades.py -x -q`
Expected: FAIL with `TypeError: configure_openai_session() got an unexpected keyword argument 'noise_reduction'`

**Step 3: Write minimal implementation**
(a) `Orchestrator/config.py` — insert after line 536 (`OPENAI_REALTIME_VAD_EAGERNESS = ...`):
```python
# GA session.audio.input.noise_reduction = {"type": near_field|far_field} | null.
# "off" is our sentinel for an explicit null (disable provider default).
OPENAI_REALTIME_NOISE_REDUCTION_TYPES = ("near_field", "far_field", "off")
```
(b) `Orchestrator/routes/realtime_routes.py` — add `OPENAI_REALTIME_NOISE_REDUCTION_TYPES,` to the config import block (after line 51 `OPENAI_REALTIME_VAD_EAGERNESS,`).
(c) Signature (line 309, after `create_response: Optional[bool] = None,`) — append:
```python
    noise_reduction: Optional[str] = None,
```
(d) After the idle_timeout_ms clamp (line 352), insert:
```python
    # noise_reduction (GA 2026 schema) — allowlist-validated, then phone default.
    if noise_reduction is not None and noise_reduction not in OPENAI_REALTIME_NOISE_REDUCTION_TYPES:
        print(f"[REALTIME] WARNING: noise_reduction {noise_reduction!r} not in {OPENAI_REALTIME_NOISE_REDUCTION_TYPES}; ignoring")
        noise_reduction = None
    # Phone-bridge sessions are keyed "phone-<sid>" (phone/bridge.py) and call
    # this function positionally — telephony defaults to near_field (applied
    # upstream before VAD + model). Portal/Android stay unset unless the client
    # opts in via connect message / query param.
    if noise_reduction is None and session.session_id.startswith("phone-"):
        noise_reduction = "near_field"
```
(e) Immediately before `await session.openai_ws.send(json.dumps(config_event))` (line 545), insert:
```python
    # Additive GA field — omitted entirely when unset (provider default applies).
    if noise_reduction == "off":
        config_event["session"]["audio"]["input"]["noise_reduction"] = None
    elif noise_reduction is not None:
        config_event["session"]["audio"]["input"]["noise_reduction"] = {"type": noise_reduction}
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_realtime_p2_upgrades.py Orchestrator/tests/test_live_models.py -x -q`
Expected: PASS (new tests + no regressions in the existing configure tests)

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_realtime_p2_upgrades.py
git commit -m "feat(realtime): session.audio.input.noise_reduction — near_field default on phone bridge"
```

---

### Task P2.4: OpenAI transcription `delay` knob (gpt-realtime-whisper latency/accuracy)

**Files:**
- Modify: Orchestrator/config.py (insert after the `OPENAI_REALTIME_NOISE_REDUCTION_TYPES` line added in P2.3)
- Modify: Orchestrator/routes/realtime_routes.py (import block; signature; validation block; pre-send mutation — anchors below)
- Test: Orchestrator/tests/test_realtime_p2_upgrades.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_realtime_p2_upgrades.py`:
```python
# ---------------------------------------------------------------------------
# transcription delay (gpt-realtime-whisper: minimal|low|medium|high|xhigh)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcription_delay_emitted_when_valid(stub_fossil_context):
    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="test_operator", voice="ash",
        transcription_delay="low",
    )
    transcription = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]["transcription"]
    assert transcription["delay"] == "low"
    assert "model" in transcription  # STT_OPENAI_STREAM still present


@pytest.mark.asyncio
async def test_transcription_delay_absent_by_default(stub_fossil_context):
    session = _make_openai_session()
    await configure_openai_session(session=session, operator="test_operator", voice="ash")
    transcription = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]["transcription"]
    assert "delay" not in transcription


@pytest.mark.asyncio
async def test_transcription_delay_invalid_ignored(stub_fossil_context, capsys):
    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="test_operator", voice="ash",
        transcription_delay="warp_speed",
    )
    transcription = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]["transcription"]
    assert "delay" not in transcription
    assert "transcription_delay" in capsys.readouterr().out
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_realtime_p2_upgrades.py -x -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'transcription_delay'`

**Step 3: Write minimal implementation**
(a) `Orchestrator/config.py` — directly under `OPENAI_REALTIME_NOISE_REDUCTION_TYPES`:
```python
# audio.input.transcription.delay for gpt-realtime-whisper (per-minute STT):
# latency/accuracy trade-off knob, per developers.openai.com realtime-transcription.
OPENAI_REALTIME_TRANSCRIPTION_DELAYS = ("minimal", "low", "medium", "high", "xhigh")
```
(b) `realtime_routes.py` — add `OPENAI_REALTIME_TRANSCRIPTION_DELAYS,` to the config import block; append to the `configure_openai_session` signature (after `noise_reduction`):
```python
    transcription_delay: Optional[str] = None,
```
(c) After the noise_reduction validation block (added in P2.3), insert:
```python
    if transcription_delay is not None and transcription_delay not in OPENAI_REALTIME_TRANSCRIPTION_DELAYS:
        print(f"[REALTIME] WARNING: transcription_delay {transcription_delay!r} not in {OPENAI_REALTIME_TRANSCRIPTION_DELAYS}; ignoring")
        transcription_delay = None
```
(d) In the pre-send mutation block (added in P2.3, before `await session.openai_ws.send(...)`):
```python
    if transcription_delay is not None:
        config_event["session"]["audio"]["input"]["transcription"]["delay"] = transcription_delay
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_realtime_p2_upgrades.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_realtime_p2_upgrades.py
git commit -m "feat(realtime): expose gpt-realtime-whisper transcription.delay session knob"
```

---

### Task P2.5: OpenAI WS endpoint plumbing — noise_reduction + transcription_delay via query param / connect message

**Files:**
- Modify: Orchestrator/routes/realtime_routes.py:1355-1374 (query params), :1440-1445 (connect merge), :1456-1466 (configure call) — pre-P2.3/P2.4 numbering; locate by quoted anchors
- Test: Orchestrator/tests/test_realtime_p2_endpoint.py (create)

**Step 1: Write the failing test**
Create `Orchestrator/tests/test_realtime_p2_endpoint.py`:
```python
"""P2 — /ws/realtime endpoint plumbing for noise_reduction + transcription_delay.

Drives the real WS endpoint with FastAPI TestClient; upstream dial, session
config, background loops, and teardown save are stubbed at module attrs
(same override-at-imported-name pattern as test_live_models.py)."""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import Orchestrator.routes.realtime_routes as rr
from Orchestrator.checkpoint import app


@pytest.fixture
def relay_stubs(monkeypatch):
    monkeypatch.setattr(rr, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(rr, "WEBSOCKETS_AVAILABLE", True)
    connect_mock = AsyncMock(return_value=True)
    configure_mock = AsyncMock()
    monkeypatch.setattr(rr, "connect_to_openai", connect_mock)
    monkeypatch.setattr(rr, "configure_openai_session", configure_mock)
    monkeypatch.setattr(rr, "save_session_to_blackbox", AsyncMock())

    async def _noop(session):
        return None

    monkeypatch.setattr(rr, "openai_listener", _noop)
    monkeypatch.setattr(rr, "openai_keepalive_loop", _noop)
    return connect_mock, configure_mock


def _drive_connect(path, connect_msg):
    client = TestClient(app)
    with client.websocket_connect(path) as ws:
        ws.send_text(json.dumps(connect_msg))
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "connected"
        ws.send_text(json.dumps({"type": "disconnect"}))


def test_query_params_reach_configure(relay_stubs):
    _, configure_mock = relay_stubs
    _drive_connect(
        "/ws/realtime/p2-ep-1?noise_reduction=far_field&transcription_delay=low",
        {"type": "connect", "operator": "test_operator", "voice": "ash"},
    )
    kwargs = configure_mock.await_args.kwargs
    assert kwargs["noise_reduction"] == "far_field"
    assert kwargs["transcription_delay"] == "low"


def test_connect_json_wins_over_query_params(relay_stubs):
    _, configure_mock = relay_stubs
    _drive_connect(
        "/ws/realtime/p2-ep-2?noise_reduction=far_field",
        {"type": "connect", "operator": "test_operator",
         "noise_reduction": "near_field", "transcription_delay": "minimal"},
    )
    kwargs = configure_mock.await_args.kwargs
    assert kwargs["noise_reduction"] == "near_field"
    assert kwargs["transcription_delay"] == "minimal"


def test_params_default_none_when_absent(relay_stubs):
    _, configure_mock = relay_stubs
    _drive_connect("/ws/realtime/p2-ep-3", {"type": "connect", "operator": "test_operator"})
    kwargs = configure_mock.await_args.kwargs
    assert kwargs["noise_reduction"] is None
    assert kwargs["transcription_delay"] is None
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_realtime_p2_endpoint.py -x -q`
Expected: FAIL with `KeyError: 'noise_reduction'` (configure never receives the kwarg)

**Step 3: Write minimal implementation**
In `realtime_routes.py`, after the `url_create_response` block (anchor: `_create_str = websocket.query_params.get("create_response")`, lines 1371-1374), add:
```python
    url_noise_reduction = websocket.query_params.get("noise_reduction")
    url_transcription_delay = websocket.query_params.get("transcription_delay")
```
In the connect branch after `create_response = data.get("create_response", url_create_response)` (line 1445), add:
```python
                noise_reduction = data.get("noise_reduction", url_noise_reduction)
                transcription_delay = data.get("transcription_delay", url_transcription_delay)
```
Extend the `configure_openai_session(...)` call (lines 1456-1466) with:
```python
                        noise_reduction=noise_reduction,
                        transcription_delay=transcription_delay,
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_realtime_p2_endpoint.py Orchestrator/tests/test_realtime_p2_upgrades.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_realtime_p2_endpoint.py
git commit -m "feat(realtime): plumb noise_reduction + transcription_delay through /ws/realtime connect + query params"
```

---

### Task P2.6: Clean stale "GPT-4o Realtime" headers (comment-only)

**Files:**
- Modify: Orchestrator/routes/realtime_routes.py:3,6,124,1334
- Modify: Orchestrator/models.py:77,81
- Modify: Orchestrator/config.py:498

The GPT-4o realtime line was SHUT DOWN 2026-05-07; the bridge speaks to the gpt-realtime family. The GA-migration "Beta" comments at realtime_routes.py:514-515 and :656 are accurate historical rationale — LEAVE them.

**Step 1: Apply the comment edits**
- realtime_routes.py:3 → `realtime_routes.py - OpenAI Realtime API WebSocket Bridge (gpt-realtime family)`
- realtime_routes.py:6 → `OpenAI's Realtime API (gpt-realtime-2.1 generation), enabling real-time voice conversations with`
- realtime_routes.py:124 → `    session_summary = f"""=== OpenAI Realtime Voice Session ===`
- realtime_routes.py:1334 → `    WebSocket endpoint for the OpenAI Realtime API bridge.`
- models.py:77 → `# OpenAI Realtime API Session Management`
- models.py:81 → `    """Represents an OpenAI Realtime API session for an operator."""`
- config.py:498 → `# OpenAI Realtime API (gpt-realtime voice conversations)`

**Step 2: Verify no stale references remain**
Run: `grep -n "GPT-4o" /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/realtime_routes.py /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/models.py /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/config.py`
Expected: no output (exit 1)

**Step 3: Verify the tree still imports (service runs live from this tree)**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes; import Orchestrator.models; print('ok')"`
Expected: `ok`

**Step 4: Run the realtime test files**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_live_models.py Orchestrator/tests/test_realtime_p2_upgrades.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/models.py Orchestrator/config.py
git commit -m "docs(realtime): retire stale GPT-4o header comments (line shut down 2026-05-07)"
```

---

### Task P2.7: Grok voice model catalog in config.py

**Files:**
- Modify: Orchestrator/config.py:658-662 (Grok Live block)
- Test: Orchestrator/tests/test_grok_live_p2.py (create)

**Step 1: Write the failing test**
Create `Orchestrator/tests/test_grok_live_p2.py`:
```python
"""P2 — Grok Voice Agent modernization: catalog, model addressing, session params.

Conventions mirror test_live_models.py (stubbed fossil context, MagicMock
sessions, single-send payload extraction)."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.config import (
    GROK_LIVE_MODEL,
    GROK_LIVE_MODELS,
    GROK_LIVE_VOICES,
)


def test_grok_catalog_contents():
    """P0 probe (diagnostics/voice_probes/results/) verified both ids at
    wss://api.x.ai/v1/realtime?model=. grok-voice-fast-1.0 is deprecated
    upstream; 'grok-voice-agent' was never a real model id — neither belongs."""
    ids = {m["id"] for m in GROK_LIVE_MODELS}
    assert ids == {"grok-voice-latest", "grok-voice-think-fast-1.0"}

    defaults = [m for m in GROK_LIVE_MODELS if m.get("default") is True]
    assert len(defaults) == 1
    assert defaults[0]["id"] == "grok-voice-latest"
    assert GROK_LIVE_MODEL == "grok-voice-latest"

    assert "grok-voice-agent" not in ids
    assert "grok-voice-fast-1.0" not in ids


def test_grok_voices_unchanged():
    """Voice list is a separate contract (Portal/Android hydrate from
    /grok-live/status) — the catalog addition must not disturb it."""
    assert GROK_LIVE_VOICES == ["Ara", "Rex", "Sal", "Eve", "Leo"]
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'GROK_LIVE_MODEL'`

**Step 3: Write minimal implementation**
In `Orchestrator/config.py`, replace lines 658-662 with:
```python
# xAI Grok Voice Agent API (Grok real-time voice conversations)
GROK_LIVE_URL = "wss://api.x.ai/v1/realtime"
GROK_LIVE_MODEL = os.getenv("GROK_LIVE_MODEL", "grok-voice-latest")  # alias -> newest (currently grok-voice-think-fast-1.0)
# Grok voice model catalog — P0 WS-probe-verified 2026-07-11 (see
# diagnostics/voice_probes/results/). grok-voice-fast-1.0 is deprecated
# upstream; the legacy "grok-voice-agent" string was a cosmetic label, never
# a real model id — the code previously sent NO model at all.
GROK_LIVE_MODELS: List[Dict] = [
    {"id": "grok-voice-latest", "name": "Grok Voice (Latest alias)", "default": True},
    {"id": "grok-voice-think-fast-1.0", "name": "Grok Voice Think Fast 1.0 (flagship pin)"},
]
GROK_LIVE_VOICES = ["Ara", "Rex", "Sal", "Eve", "Leo"]  # Available voices
GROK_LIVE_DEFAULT_VOICE = "Rex"         # Default voice for phone
GROK_LIVE_SAMPLE_RATE = 24000           # PCM16 audio at 24kHz (same as OpenAI Realtime)
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): model catalog — grok-voice-latest default + think-fast-1.0 pin (P0 probe-verified)"
```

---

### Task P2.8: connect_to_grok sends ?model= (allowlist-validated) + session.model field

**Files:**
- Modify: Orchestrator/models.py:162 (GrokLiveSession — insert field after `voice`)
- Modify: Orchestrator/routes/grok_live_routes.py:44-54 (imports), :241-275 (connect_to_grok)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# connect_to_grok — model bound at the WS URL (mirrors OpenAI/Gemini patterns)
# ---------------------------------------------------------------------------

from Orchestrator.models import GrokLiveSession


@pytest.fixture
def fake_grok_dial(monkeypatch):
    """Capture the websockets.connect URL without any network."""
    import Orchestrator.routes.grok_live_routes as gl
    monkeypatch.setattr(gl, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(gl, "WEBSOCKETS_AVAILABLE", True)
    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return MagicMock()

    monkeypatch.setattr(gl.websockets, "connect", fake_connect)
    return captured


@pytest.mark.asyncio
async def test_connect_to_grok_default_model_in_url(fake_grok_dial):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t1", created_at="")
    assert await connect_to_grok(session) is True
    assert fake_grok_dial["url"] == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert session.model == "grok-voice-latest"


@pytest.mark.asyncio
async def test_connect_to_grok_pinned_model(fake_grok_dial):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t2", created_at="")
    assert await connect_to_grok(session, model="grok-voice-think-fast-1.0") is True
    assert fake_grok_dial["url"].endswith("?model=grok-voice-think-fast-1.0")
    assert session.model == "grok-voice-think-fast-1.0"


@pytest.mark.asyncio
async def test_connect_to_grok_invalid_model_falls_back(fake_grok_dial, capsys):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t3", created_at="")
    assert await connect_to_grok(session, model="grok-voice-agent") is True
    assert fake_grok_dial["url"].endswith("?model=grok-voice-latest")
    assert session.model == "grok-voice-latest"
    out = capsys.readouterr().out
    assert "WARNING" in out and "grok-voice-agent" in out
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `TypeError: connect_to_grok() got an unexpected keyword argument 'model'` (or URL assertion mismatch on the first test)

**Step 3: Write minimal implementation**
(a) `Orchestrator/models.py` — inside `GrokLiveSession`, after line 162 (`voice: str = "Ara"...`), insert:
```python
    model: str = ""                      # Resolved xAI model id (set by connect_to_grok)
```
(b) `grok_live_routes.py` — extend the config import block (after `GROK_LIVE_DEFAULT_VOICE,` line 48):
```python
    GROK_LIVE_MODEL,
    GROK_LIVE_MODELS,
```
(c) Replace `connect_to_grok` (lines 241-280) signature + dial section:
```python
async def connect_to_grok(session: 'GrokLiveSession', model: Optional[str] = None) -> bool:
    """
    Establish WebSocket connection to xAI Grok Voice Agent API.

    Args:
        model: Optional model id override, validated against GROK_LIVE_MODELS
            (mirrors the OpenAI connect_to_openai / Gemini configure patterns).
            Invalid values fall back to GROK_LIVE_MODEL with a logged warning.
            Per xAI docs the model is bound at WS-connect via URL query.

    Returns True if connection successful, False otherwise.
    """
    if not WEBSOCKETS_AVAILABLE:
        print("[GROK-LIVE] Cannot connect - websockets library not installed")
        return False

    if not XAI_API_KEY:
        print("[GROK-LIVE] Cannot connect - XAI_API_KEY not set")
        return False

    # Resolve + validate model (allowlist from GROK_LIVE_MODELS)
    _allowed_model_ids = {m["id"] for m in GROK_LIVE_MODELS}
    if model and model not in _allowed_model_ids:
        print(f"[GROK-LIVE] WARNING: model {model!r} not in GROK_LIVE_MODELS allowlist; falling back to default {GROK_LIVE_MODEL!r}")
        model = None
    resolved_model = model or GROK_LIVE_MODEL
    session.model = resolved_model

    try:
        headers = {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json"
        }

        url = f"{GROK_LIVE_URL}?model={resolved_model}"
        print(f"[GROK-LIVE] Connecting to xAI: {url}")
        # websockets 15.x uses additional_headers instead of extra_headers
        # Add explicit ping settings to prevent connection drops
        session.grok_ws = await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
        session.status = "connected"
        session.last_activity = now_utc_iso()
        print(f"[GROK-LIVE] Connected to xAI for session {session.session_id} (model={resolved_model})")
        return True

    except Exception as e:
        print(f"[GROK-LIVE] Connection failed: {e}")
        session.status = "error"
        return False
```
NOTE: phone/bridge.py:1886 calls `connect_to_grok(self._ai_session)` positionally — the new kwarg default keeps it working (it now gets the default model, which is the fix).

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/models.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): bind model at WS URL (?model=), allowlist-validated; session.model field"
```

---

### Task P2.9: Grok WS endpoint model plumbing + real model in connected event + status catalog

**Files:**
- Modify: Orchestrator/routes/grok_live_routes.py:1349-1365 (connect branch), :1398-1406 (connected event, kill "grok-voice-agent" at 1403), :1477-1488 (/grok-live/status)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# Endpoint plumbing + /grok-live/status catalog surface
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient

from Orchestrator.checkpoint import app
from Orchestrator.routes.grok_live_routes import grok_live_status


@pytest.mark.asyncio
async def test_grok_status_serves_models_and_default():
    resp = await grok_live_status()
    assert resp["model_default"] == "grok-voice-latest"
    assert {m["id"] for m in resp["models"]} == {
        "grok-voice-latest", "grok-voice-think-fast-1.0",
    }
    # Existing contract stays additive (3-surface rule)
    assert resp["voices"] == GROK_LIVE_VOICES
    assert "default_voice" in resp and "sample_rate" in resp


@pytest.fixture
def grok_relay_stubs(monkeypatch):
    import Orchestrator.routes.grok_live_routes as gl
    monkeypatch.setattr(gl, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(gl, "WEBSOCKETS_AVAILABLE", True)

    connect_mock = AsyncMock()

    async def fake_connect(session, model=None):
        session.model = model or gl.GROK_LIVE_MODEL
        session.status = "connected"
        connect_mock(session, model=model)
        return True

    configure_mock = AsyncMock()
    monkeypatch.setattr(gl, "connect_to_grok", fake_connect)
    monkeypatch.setattr(gl, "configure_grok_session", configure_mock)
    monkeypatch.setattr(gl, "save_grok_session_to_blackbox", AsyncMock())

    async def _noop(session):
        return None

    monkeypatch.setattr(gl, "grok_listener", _noop)
    monkeypatch.setattr(gl, "grok_keepalive_loop", _noop)
    return connect_mock, configure_mock


def test_connected_event_reports_resolved_model(grok_relay_stubs):
    connect_mock, _ = grok_relay_stubs
    client = TestClient(app)
    with client.websocket_connect("/ws/grok-live/p2-grok-ep-1") as ws:
        ws.send_text(json.dumps({
            "type": "connect", "operator": "test_operator",
            "model": "grok-voice-think-fast-1.0",
        }))
        assert ws.receive_json()["type"] == "status"
        connected = ws.receive_json()
        ws.send_text(json.dumps({"type": "disconnect"}))

    assert connected["type"] == "connected"
    # The cosmetic "grok-voice-agent" label is dead — real resolved model only.
    assert connected["data"]["model"] == "grok-voice-think-fast-1.0"
    assert connect_mock.call_args.kwargs["model"] == "grok-voice-think-fast-1.0"


def test_model_query_param_fallback(grok_relay_stubs):
    connect_mock, _ = grok_relay_stubs
    client = TestClient(app)
    with client.websocket_connect("/ws/grok-live/p2-grok-ep-2?model=grok-voice-think-fast-1.0") as ws:
        ws.send_text(json.dumps({"type": "connect", "operator": "test_operator"}))
        ws.receive_json()
        connected = ws.receive_json()
        ws.send_text(json.dumps({"type": "disconnect"}))

    assert connected["data"]["model"] == "grok-voice-think-fast-1.0"
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `KeyError: 'model_default'`

**Step 3: Write minimal implementation**
(a) Connect branch — after `role = data.get("role", "")` (line 1354), add:
```python
                # Model: JSON connect message wins over URL query param
                # (same merge rule as /ws/realtime — Android uses query params).
                model = data.get("model", websocket.query_params.get("model"))
```
(b) Line 1363: `if await connect_to_grok(session):` → `if await connect_to_grok(session, model=model):`
(c) Connected event (line 1403): `"model": "grok-voice-agent",` → `"model": session.model,`
(d) `/grok-live/status` (lines 1480-1488) — add after `"api_key_configured"`:
```python
        "model_default": GROK_LIVE_MODEL,
        "models": GROK_LIVE_MODELS,
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q` then live-check `curl -s http://localhost:9091/grok-live/status | python3 -m json.tool` (after the next service restart; live process still runs pre-change code — that is expected)
Expected: PASS; curl shows `models` + `model_default` once restarted (restart is pre-authorized but defer to end of phase)

**Step 5: Commit**
```bash
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): model selection via connect/query param; real model in connected event; status catalog"
```

---

### Task P2.10: Grok reasoning.effort (high|none), gated to think-fast models

**Files:**
- Modify: Orchestrator/config.py (Grok block — insert after `GROK_LIVE_MODELS`)
- Modify: Orchestrator/routes/grok_live_routes.py:282 (signature), :293-299 (validation area), :461-464 (payload), :1349-1365 (endpoint pass-through)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# reasoning.effort — think-fast models only (mirror Gemini thinkingLevel gate)
# ---------------------------------------------------------------------------

from Orchestrator.routes.grok_live_routes import configure_grok_session


@pytest.fixture
def stub_grok_fossil_context(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.grok_live_routes.build_fossil_context", _stub
    )


def _make_grok_session(model="grok-voice-latest"):
    session = MagicMock()
    session.session_id = "test-grok"
    session.grok_ws = MagicMock()
    session.grok_ws.send = AsyncMock()
    session.model = model
    session.provenance = {}
    session.context_injected = False
    return session


def _extract_grok_payload(send_mock):
    assert send_mock.await_count == 1
    return json.loads(send_mock.await_args.args[0])


@pytest.mark.asyncio
async def test_reasoning_effort_emitted_for_capable_model(stub_grok_fossil_context):
    session = _make_grok_session(model="grok-voice-think-fast-1.0")
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 reasoning_effort="high")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert payload["session"]["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_reasoning_effort_suppressed_for_unknown_model(stub_grok_fossil_context, capsys):
    session = _make_grok_session(model="")  # legacy session with no resolved model
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 reasoning_effort="high")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "reasoning" not in payload["session"]
    assert "reasoning" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_reasoning_effort_invalid_value_ignored(stub_grok_fossil_context, capsys):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 reasoning_effort="maximum_overdrive")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "reasoning" not in payload["session"]
    assert "WARNING" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_reasoning_absent_by_default(stub_grok_fossil_context):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "reasoning" not in payload["session"]
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `TypeError: configure_grok_session() got an unexpected keyword argument 'reasoning_effort'`

**Step 3: Write minimal implementation**
(a) `Orchestrator/config.py` — directly under `GROK_LIVE_MODELS`:
```python
# reasoning.effort exists ONLY on the newest voice generation (think-fast).
# Emitting it on other models risks an upstream reject — capability-gate like
# GEMINI_LIVE_THINKING_CAPABLE_MODELS.
GROK_LIVE_REASONING_EFFORTS = ("high", "none")
GROK_LIVE_REASONING_CAPABLE_MODELS: frozenset = frozenset({
    "grok-voice-latest",            # alias currently resolves to think-fast-1.0
    "grok-voice-think-fast-1.0",
})
```
(b) `grok_live_routes.py` imports — add `GROK_LIVE_REASONING_EFFORTS,` and `GROK_LIVE_REASONING_CAPABLE_MODELS,` to the config import block.
(c) Signature (line 282):
```python
async def configure_grok_session(session: 'GrokLiveSession', operator: str, voice: str = "Ara", custom_role: str = "", reasoning_effort: Optional[str] = None):
```
(phone/bridge.py:1893-1898 and :1970+ call with `voice=`/`custom_role=` kwargs — unchanged.)
(d) After the voice validation (lines 296-299), insert:
```python
    # reasoning.effort — allowlist + capability gate (think-fast generation only).
    if reasoning_effort is not None and reasoning_effort not in GROK_LIVE_REASONING_EFFORTS:
        print(f"[GROK-LIVE] WARNING: reasoning_effort {reasoning_effort!r} not in {GROK_LIVE_REASONING_EFFORTS}; ignoring")
        reasoning_effort = None
    if reasoning_effort is not None and session.model not in GROK_LIVE_REASONING_CAPABLE_MODELS:
        print(f"[GROK-LIVE] reasoning_effort ignored — model {session.model!r} is not reasoning-capable")
        reasoning_effort = None
```
(e) Immediately before the `print(f"[GROK-LIVE] ===== SENDING SESSION CONFIG =====")` line (466), insert:
```python
    if reasoning_effort is not None:
        config_event["session"]["reasoning"] = {"effort": reasoning_effort}
```
(f) Endpoint pass-through — in the connect branch after the `model = ...` line added in P2.9:
```python
                reasoning_effort = data.get("reasoning_effort", websocket.query_params.get("reasoning_effort"))
```
and extend the configure call (line 1365): `await configure_grok_session(session, operator, voice, custom_role=role, reasoning_effort=reasoning_effort)`

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): reasoning.effort session param, capability-gated to think-fast models"
```

---

### Task P2.11: Grok input transcription explicitly configured in session.update

**Files:**
- Read FIRST: diagnostics/voice_probes/results/ (xAI transcription probe verdict from P2.1)
- Modify: Orchestrator/routes/grok_live_routes.py:447-453 (audio.input block)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Confirm the probe-verified field shape**
Run:
```bash
Orchestrator/venv/bin/python -c "
import json, glob
p = sorted(glob.glob('/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/*-xai.json'))[-1]
r = [x for x in json.load(open(p))['results'] if x['probe'] == 'transcription_shape'][0]
print('transcription_shape ok =', r['ok'])
for e in r['events']:
    if e.get('type') == 'session.updated':
        print('accepted shape:', json.dumps((((e.get('session') or {}).get('audio') or {}).get('input') or {}).get('transcription')))
"
```
Expected: `transcription_shape ok = True` (P0.4's `transcription_shape` probe sent `session.update` with `audio.input.transcription: {}` and got `session.updated` back) plus the echoed shape. The bare `{}` matches xAI docs (`docs.x.ai` voice-agent session schema — nested object under `audio.input` carrying optional `language_hint`). If the echoed shape differs (e.g. a required `model` key), substitute that shape in Step 4's dict — the test asserts only that the key exists as a dict. If `ok = False`: STOP — re-run P0.4 and reconcile before touching this task.

**Step 2: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# Input transcription — explicit opt-in (user turns must reach saved transcripts)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_input_transcription_explicitly_configured(stub_grok_fossil_context):
    """Recon 2026-07-11: session.update never configured input transcription —
    user turns silently relied on an undocumented xAI default. Mirror
    realtime_routes.py:531 (audio.input.transcription) explicitly."""
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    audio_input = _extract_grok_payload(session.grok_ws.send)["session"]["audio"]["input"]
    assert isinstance(audio_input.get("transcription"), dict)
```

**Step 3: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `assert isinstance(None, dict)` being false

**Step 4: Write minimal implementation**
In the `config_event` audio input block (lines 447-453), change:
```python
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": GROK_LIVE_SAMPLE_RATE
                    },
                    # Explicit input-transcription opt-in (P0.4 transcription_shape
                    # probe 2026-07-11 — accepted shape echoed in session.updated).
                    # Without it, conversation.item.input_audio_transcription.*
                    # events are not guaranteed and saved transcripts lose all
                    # user turns. Shape per docs.x.ai voice-agent session schema
                    # (language_hint merged in by the ASR-biasing params task).
                    "transcription": {}
                },
```

**Step 5: Run test to verify it passes, then commit**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS
```bash
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "fix(grok-live): explicitly configure input audio transcription in session.update"
```

---

### Task P2.12: Grok session resumption — enable + capture conversation.id

**Files:**
- Modify: Orchestrator/models.py (GrokLiveSession — insert after the `model` field added in P2.8)
- Modify: Orchestrator/routes/grok_live_routes.py:436-464 (session dict), :1092-1094 (conversation.created handler)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# Session resumption — resumption.enabled + conversation.id capture
# ---------------------------------------------------------------------------

from Orchestrator.routes.grok_live_routes import handle_grok_message


@pytest.mark.asyncio
async def test_resumption_enabled_in_session_update(stub_grok_fossil_context):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert payload["session"]["resumption"] == {"enabled": True}


@pytest.mark.asyncio
async def test_conversation_created_captures_id():
    session = GrokLiveSession(session_id="t-resume", created_at="")
    await handle_grok_message(session, {
        "type": "conversation.created",
        "conversation": {"id": "conv_abc123"},
    })
    assert session.conversation_id == "conv_abc123"


@pytest.mark.asyncio
async def test_conversation_created_without_id_is_harmless():
    session = GrokLiveSession(session_id="t-resume-2", created_at="")
    await handle_grok_message(session, {"type": "conversation.created"})
    assert session.conversation_id is None
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL — `KeyError: 'resumption'` (and GrokLiveSession has no `conversation_id`)

**Step 3: Write minimal implementation**
(a) `Orchestrator/models.py` — in `GrokLiveSession`, directly under the `model` field:
```python
    conversation_id: Optional[str] = None  # xAI conversation.id (session resumption; set by conversation.created)
```
(b) `grok_live_routes.py` — in the `config_event` session dict, after `"tool_choice": "auto"...` (line 462), add:
```python
            "tool_choice": "auto",  # Force Grok to actually use tools when appropriate
            # Session resumption (xAI): reconnect with ?conversation_id= replays
            # cached turns server-side instead of a full context rebuild.
            "resumption": {"enabled": True}
```
(c) Replace the `conversation.created` handler (lines 1092-1094):
```python
    elif event_type == "conversation.created":
        # Conversation initialized — capture the id for session resumption
        # (grok_reconnect dials ?conversation_id= to replay cached turns).
        conv_id = (event.get("conversation") or {}).get("id") or event.get("conversation_id")
        if conv_id:
            session.conversation_id = conv_id
            print(f"[GROK-LIVE] Conversation created: {conv_id} (resumption armed)")
        else:
            print(f"[GROK-LIVE] Conversation created (no id in event)")
```

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/models.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): session resumption — resumption.enabled + conversation.id capture"
```

---

### Task P2.13: Grok reconnect resumes via ?conversation_id= instead of rebuilding context

**Files:**
- Modify: Orchestrator/routes/grok_live_routes.py — `connect_to_grok` (add conversation_id to URL) and `grok_reconnect`:1158-1182 (pre-P2.x numbering; anchor `# Reconnect`)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# Reconnect resumes the server-side conversation (no context rebuild)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_to_grok_appends_conversation_id(fake_grok_dial):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t-resume-url", created_at="")
    assert await connect_to_grok(session, model="grok-voice-latest",
                                 conversation_id="conv_xyz") is True
    assert fake_grok_dial["url"] == (
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest&conversation_id=conv_xyz"
    )


@pytest.mark.asyncio
async def test_reconnect_with_conversation_id_skips_rebuild(monkeypatch):
    import Orchestrator.routes.grok_live_routes as gl
    session = GrokLiveSession(session_id="t-rc-1", created_at="")
    session.model = "grok-voice-latest"
    session.conversation_id = "conv_resume_me"
    session.operator = "test_operator"

    connect_mock = AsyncMock(return_value=True)
    configure_mock = AsyncMock()
    monkeypatch.setattr(gl, "connect_to_grok", connect_mock)
    monkeypatch.setattr(gl, "configure_grok_session", configure_mock)

    await gl.grok_reconnect(session)

    assert connect_mock.await_args.kwargs["conversation_id"] == "conv_resume_me"
    assert connect_mock.await_args.kwargs["model"] == "grok-voice-latest"
    configure_mock.assert_not_awaited()  # resumed — no context rebuild
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_reconnect_without_conversation_id_rebuilds(monkeypatch):
    import Orchestrator.routes.grok_live_routes as gl
    session = GrokLiveSession(session_id="t-rc-2", created_at="")
    session.operator = "test_operator"

    connect_mock = AsyncMock(return_value=True)
    configure_mock = AsyncMock()
    monkeypatch.setattr(gl, "connect_to_grok", connect_mock)
    monkeypatch.setattr(gl, "configure_grok_session", configure_mock)

    await gl.grok_reconnect(session)

    configure_mock.assert_awaited_once()  # legacy full-rebuild path preserved
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `TypeError: connect_to_grok() got an unexpected keyword argument 'conversation_id'`

**Step 3: Write minimal implementation**
(a) `connect_to_grok` — signature becomes:
```python
async def connect_to_grok(session: 'GrokLiveSession', model: Optional[str] = None, conversation_id: Optional[str] = None) -> bool:
```
and the URL line becomes:
```python
        url = f"{GROK_LIVE_URL}?model={resolved_model}"
        if conversation_id:
            # Resumption: xAI replays cached turns for this conversation
            url += f"&conversation_id={conversation_id}"
```
(b) In `grok_reconnect`, replace the reconnect block (anchor: `# Reconnect` / `if await connect_to_grok(session):` through the provenance re-emit):
```python
        # Reconnect — resume the server-side conversation when we have an id
        # (xAI replays cached turns; avoids a full context rebuild).
        resume_id = session.conversation_id
        if await connect_to_grok(session, model=session.model or None,
                                 conversation_id=resume_id):
            if resume_id:
                print(f"[GROK-LIVE] Resumed conversation {resume_id} — session rebuild skipped")
            else:
                # No resumption id — full reconfigure (rebuilds context)
                await configure_grok_session(session, session.operator, session.voice)

                # Re-emit provenance after reconfigure so client UI stays in sync
                # with the newly-rebuilt system context (see Task 3 code review).
                if session.provenance:
                    await _safe_ws_send(session.portal_ws, {
                        "type": "provenance",
                        "data": session.provenance
                    })
```
(the existing `# Reset state` block onward is unchanged and now serves both paths).

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS (note: the two reconnect tests each sleep ~0.5s — backoff delay)

**Step 5: Commit**
```bash
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): reconnect via ?conversation_id= resumption instead of full context rebuild"
```

---

### Task P2.14: Grok ASR biasing — replace map, keyterms (contact-seeded), language_hint

**Files:**
- Modify: Orchestrator/routes/grok_live_routes.py — helper above `configure_grok_session`; signature; payload mutations; endpoint connect branch
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Step 1: Write the failing test**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# ASR biasing — replace / keyterms (contact-seeded) / language_hint
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_contacts(monkeypatch):
    def _fake_load():
        return {
            "test_operator": {
                "id1": {"name": "Ada Lovelace", "phone": "+15550001"},
                "id2": {"name": "", "phone": "+15550002"},          # empty — skipped
                "id3": {"name": "Zed", "phone": "+15550003"},
            }
        }
    monkeypatch.setattr("Orchestrator.contacts.load_contacts", _fake_load)


@pytest.mark.asyncio
async def test_keyterms_seeded_from_contact_names(stub_grok_fossil_context, stub_contacts):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert payload["session"]["keyterms"] == ["Ada Lovelace", "Zed"]


@pytest.mark.asyncio
async def test_explicit_keyterms_override_contacts_and_are_capped(stub_grok_fossil_context, stub_contacts):
    session = _make_grok_session()
    over_limit = [f"term{i}" for i in range(120)] + ["x" * 51]  # >100 + >50 chars
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 keyterms=over_limit)
    payload = _extract_grok_payload(session.grok_ws.send)
    keyterms = payload["session"]["keyterms"]
    assert len(keyterms) == 100                # xAI cap: 100 terms
    assert all(len(k) <= 50 for k in keyterms) # xAI cap: 50 chars
    assert "Ada Lovelace" not in keyterms      # explicit list wins over seeding


@pytest.mark.asyncio
async def test_replace_and_language_hint_emitted(stub_grok_fossil_context, stub_contacts):
    session = _make_grok_session()
    await configure_grok_session(
        session, "test_operator", voice="Ara",
        replace_map={"BlackBox": "black box"}, language_hint="en-US",
    )
    payload = _extract_grok_payload(session.grok_ws.send)
    assert payload["session"]["replace"] == {"BlackBox": "black box"}
    assert payload["session"]["audio"]["input"]["transcription"]["language_hint"] == "en-US"


@pytest.mark.asyncio
async def test_empty_contact_book_omits_keyterms(stub_grok_fossil_context, monkeypatch):
    """Fresh-box gate: empty book / unknown operator -> no keyterms field at all."""
    monkeypatch.setattr("Orchestrator.contacts.load_contacts", lambda: {})
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "keyterms" not in payload["session"]
```

**Step 2: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'keyterms'` (or KeyError on 'keyterms')

**Step 3: Write minimal implementation**
(a) Insert helper directly above `configure_grok_session`:
```python
def _contact_keyterms(operator: str, limit: int = 100) -> list:
    """Operator's contact names for xAI ASR keyterm biasing (caps: 100 x 50 chars).

    Best-effort — any failure (missing file, fresh box, unknown operator)
    returns []. Uses load_contacts (read-only; no seed-book write)."""
    try:
        from Orchestrator.contacts import load_contacts
        book = load_contacts().get(operator, {}) or {}
        names: list = []
        for contact in book.values():
            if not isinstance(contact, dict):
                continue
            name = (contact.get("name") or "").strip()
            if name and len(name) <= 50 and name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        return names
    except Exception as e:
        print(f"[GROK-LIVE] contact keyterms unavailable: {e}")
        return []
```
(b) Extend the `configure_grok_session` signature (after `reasoning_effort`):
```python
    replace_map: Optional[Dict[str, str]] = None,
    keyterms: Optional[list] = None,
    language_hint: Optional[str] = None,
```
(c) After the reasoning_effort validation block, insert:
```python
    # ASR biasing — seed keyterms from the operator's contact book when the
    # client didn't supply any (names are what voice ASR most often mangles).
    if keyterms is None:
        keyterms = _contact_keyterms(operator)
    keyterms = [k for k in keyterms if isinstance(k, str) and 0 < len(k) <= 50][:100]
```
(d) In the pre-send mutation block (next to the `reasoning` mutation from P2.10):
```python
    if keyterms:
        config_event["session"]["keyterms"] = keyterms
    if replace_map and isinstance(replace_map, dict):
        config_event["session"]["replace"] = replace_map
    if language_hint:
        config_event["session"]["audio"]["input"]["transcription"]["language_hint"] = language_hint
```
(e) Endpoint connect branch — after the `reasoning_effort = ...` line:
```python
                language_hint = data.get("language_hint", websocket.query_params.get("language_hint"))
                replace_map = data.get("replace") if isinstance(data.get("replace"), dict) else None
                keyterms = data.get("keyterms") if isinstance(data.get("keyterms"), list) else None
```
and extend the configure call: `..., reasoning_effort=reasoning_effort, replace_map=replace_map, keyterms=keyterms, language_hint=language_hint)`

**Step 4: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_live_p2.py
git commit -m "feat(grok-live): replace/keyterms/language_hint ASR biasing; keyterms seeded from operator contacts"
```

---

### Task P2.15: Fix the Grok 16k/24k input sample-rate mismatch (probe-branched)

**Files (BRANCH A — default, backend declares 16k input; use unless the P2.1 sample-rate verdict says otherwise):**
- Modify: Orchestrator/config.py:662 region (split constant)
- Modify: Orchestrator/routes/grok_live_routes.py:49 (import), :447-460 (rates), :1221-1230 (keepalive), :1486 (status)
- Modify: Orchestrator/asterisk/voice_bridge.py:74
- Modify: Orchestrator/phone/bridge.py:510, :656-658
- Modify: Portal/modules/grok-live.js:219 (mic resample target)
- Test: Orchestrator/tests/test_grok_live_p2.py (append)

**Files (BRANCH B — only if the P0 probe verdict says xAI mishandles/rejects 16k input):**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt:288-291` — add `VoiceBackend.GROK_LIVE -> 24000` to the `when`; run `./gradlew :app:testDebugUnitTest --offline` from the Android project dir; backend/asterisk/Portal stay at 24k. Skip all Branch A steps.

Context: Android mics capture Grok at 16 kHz (`VoiceScreen.kt:288-291` — only GPT_REALTIME gets 24000) while the backend declares 24 kHz to xAI — every Android Grok session sends audio the model interprets at the wrong rate. xAI accepts `audio/pcm` at 16000 (docs + P0.4's `input_rate_16k` probe, `"ok": true` in `results/*-xai.json`). Branch A makes 16k the single input truth (output stays 24k everywhere — client playback paths are 24k).

**Step 1: Confirm the probe verdict, pick the branch**
Run:
```bash
Orchestrator/venv/bin/python -c "
import json, glob
p = sorted(glob.glob('/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/*-xai.json'))[-1]
r = [x for x in json.load(open(p))['results'] if x['probe'] == 'input_rate_16k'][0]
print('input_rate_16k ok =', r['ok'], '| close =', r['close_code'], '| err =', r['error'][:120])
"
```
Expected: `input_rate_16k ok = True` (P0.4's `input_rate_16k` probe sent `session.update` with `audio.input.format` `{type: 'audio/pcm', rate: 16000}` and got `session.updated` back — xAI accepts 16k input) → proceed with Branch A. If `ok = False` (error event / close captured) → execute Branch B instead (single Kotlin edit + gradle test + note that backend files stay untouched) and skip to Step 5.

**Step 2: Write the failing test (Branch A)**
Append to `Orchestrator/tests/test_grok_live_p2.py`:
```python
# ---------------------------------------------------------------------------
# Sample rates — ONE truth: 16k input (matches Android capture), 24k output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_declares_16k_input_24k_output(stub_grok_fossil_context):
    from Orchestrator.config import GROK_LIVE_INPUT_SAMPLE_RATE, GROK_LIVE_OUTPUT_SAMPLE_RATE
    assert GROK_LIVE_INPUT_SAMPLE_RATE == 16000
    assert GROK_LIVE_OUTPUT_SAMPLE_RATE == 24000

    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    audio = _extract_grok_payload(session.grok_ws.send)["session"]["audio"]
    assert audio["input"]["format"]["rate"] == 16000
    assert audio["output"]["format"]["rate"] == 24000


def test_asterisk_map_matches_declared_input_rate():
    from Orchestrator.asterisk.voice_bridge import AsteriskVoiceBridge
    assert AsteriskVoiceBridge.INPUT_RATES["grok_live"] == 16000
```

**Step 3: Run test to verify it fails**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'GROK_LIVE_INPUT_SAMPLE_RATE'`

**Step 4: Write minimal implementation (Branch A)**
(a) `Orchestrator/config.py` — replace the `GROK_LIVE_SAMPLE_RATE = 24000` line:
```python
GROK_LIVE_INPUT_SAMPLE_RATE = 16000     # PCM16 mic input — matches Android capture (VoiceScreen.kt); P0 probe-verified xAI accepts 16k
GROK_LIVE_OUTPUT_SAMPLE_RATE = 24000    # PCM16 AI output — matches Portal/Android/phone playback (unchanged)
GROK_LIVE_SAMPLE_RATE = GROK_LIVE_OUTPUT_SAMPLE_RATE  # legacy alias — status back-compat only
```
(b) `grok_live_routes.py` — import both new constants (config import block); in the session.update audio dict set input `"rate": GROK_LIVE_INPUT_SAMPLE_RATE` and output `"rate": GROK_LIVE_OUTPUT_SAMPLE_RATE`.
(c) Keepalive (lines 1221-1230): replace comment + byte count — 20ms @16kHz = 320 samples:
```python
                if session.grok_ws:
                    # 20ms at 16kHz (input rate) = 320 samples, PCM16 = 640 bytes of zeros
                    silence_bytes = b'\x00' * 640
```
(d) Status (line 1486 region): keep `"sample_rate": GROK_LIVE_SAMPLE_RATE,` and add:
```python
        "input_sample_rate": GROK_LIVE_INPUT_SAMPLE_RATE,
        "output_sample_rate": GROK_LIVE_OUTPUT_SAMPLE_RATE,
```
(e) `asterisk/voice_bridge.py:74`: `"grok_live": 24000,` → `"grok_live": 16000,`
(f) `phone/bridge.py:656-658` (GROK_LIVE branch of send_pcm16): comment → `# Grok: 16kHz PCM16 input (backend session declares 16k; P2.15)`, `target_rate = 24000` → `target_rate = 16000`. And `phone/bridge.py:510` (`_send_grok_keepalive`): `AudioConverter.phone_to_ai(silence, 24000)` → `AudioConverter.phone_to_ai(silence, 16000)`.
(g) `Portal/modules/grok-live.js:219`: `const targetRate = 24000;  // Grok expects 24kHz` → `const targetRate = 16000;  // Grok input rate — must match the backend's session.update audio.input.format.rate (16k)`. Update the function's doc comment ("Resampled audio at 24kHz" → 16kHz). Leave the playback path (`sourceRate = 24000` at :263) untouched. In Portal/index.html, read the current `?v=genuiNN` cache-bust number and increment it by one (e.g. `?v=genui41` → `?v=genui42` — read the actual current value first; do not guess).
**Live-window skew note:** static files serve straight from the tree — the mic resample flips to 16 kHz the moment the grok-live.js edit saves, while the RUNNING backend keeps declaring 24 kHz until restart. Any Grok session opened in that window sends mismatched audio. Close the window by restarting the service immediately after this task's commit (Step 6 — restart is pre-authorized).

**Step 5: Run test to verify it passes**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_live_p2.py -x -q && Orchestrator/venv/bin/python -c "import Orchestrator.phone.bridge, Orchestrator.asterisk.voice_bridge; print('ok')"`
Expected: PASS + `ok`

**Step 6: Commit, then restart immediately**
```bash
git add Orchestrator/config.py Orchestrator/routes/grok_live_routes.py Orchestrator/asterisk/voice_bridge.py Orchestrator/phone/bridge.py Portal/modules/grok-live.js Portal/index.html Orchestrator/tests/test_grok_live_p2.py
git commit -m "fix(grok-live): 16k input / 24k output — one sample-rate truth across Android/Portal/asterisk/phone (P0 probe-verified)"
sudo systemctl restart blackbox.service   # pre-authorized — do NOT defer to end of phase: closes the mic-16k/backend-24k skew window from Step 4(g) (60-90s warm-up)
```
After warm-up, confirm `curl -s http://localhost:9091/grok-live/status` shows `input_sample_rate: 16000` (the phase-end restart in the exit criteria then becomes a no-op re-check for this task).

---

### Task P2.16: test_grok.sh — retire deprecated chat model id

**Files:**
- Modify: test_grok.sh:15

`grok-4-1-fast-reasoning` is on xAI's May 2026 deprecation list (server-side redirect); the box's REST-chat default is `grok-4.3` (config.py XAI_MODEL_DEFAULT). Pure config task — no unit test; verified by running the script against the live service.

**Step 1: Edit the model id**
In `test_grok.sh` line 15: `"model": "grok-4-1-fast-reasoning",` → `"model": "grok-4.3",`

**Step 2: Verify the edit**
Run: `grep -n "grok-" /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/test_grok.sh`
Expected: exactly one hit: `15:    "model": "grok-4.3",`

**Step 3: Run the smoke test live (service running, XAI key configured)**
Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && bash test_grok.sh`
Expected: streamed response text (SSE chunks) answering the "what model are you" prompt, ending with `Test complete!` — no `model not found` / deprecation error in the output.

**Step 4: Commit**
```bash
git add test_grok.sh
git commit -m "chore(test): test_grok.sh chat smoke uses grok-4.3 (grok-4-1-fast-reasoning deprecated)"
```

---

**Phase 2 exit criteria:** all P2 test files green (`Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_live_models.py Orchestrator/tests/test_realtime_p2_upgrades.py Orchestrator/tests/test_realtime_p2_endpoint.py Orchestrator/tests/test_grok_live_p2.py -q` from repo root); then `sudo systemctl restart blackbox.service` (pre-authorized; 60-90s warm-up) and confirm `curl -s http://localhost:9091/realtime/status` shows `model_default: gpt-realtime-2.1` and `curl -s http://localhost:9091/grok-live/status` shows the Grok model catalog + `input_sample_rate: 16000`. The WS-probe harness from P0 (`diagnostics/voice_probes/`) doubles as the live smoke for both providers post-restart.

---

## Phase 3a — Android voice data layer (VoiceClient + tests; NO UI work)

All paths below are relative to the Android app root unless absolute:
`APP="/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"`

Line numbers verified against the working tree on 2026-07-11 (HEAD `ab90bbc`). If they have drifted, locate edits by the quoted anchor text. Every task leaves the tree compiling and the unit suite green. Test command shorthand used throughout:

```
cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "<class FQN>"
```

---

### Task P3.1: Testability seam — injectable WebSocketClient + VoiceClientParseTest harness

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/api/WebSocketClient.kt:27,38,93,95`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt:42-43`
- Create: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/voice/FakeWebSocketClient.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Create `app/src/test/java/com/aiblackbox/portal/data/voice/FakeWebSocketClient.kt`:

```kotlin
package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow
import okhttp3.OkHttpClient

/**
 * Test double for [WebSocketClient]. connect() returns a Channel-backed flow the
 * test pushes [WsMessage]s into; closing the channel ends the collect (= socket
 * gone, exactly like the real callbackFlow). send() records outbound frames and
 * returns a settable result. close() mirrors okhttp: Disconnected, then closed.
 */
class FakeWebSocketClient : WebSocketClient(OkHttpClient()) {
    val incoming = Channel<WsMessage>(Channel.UNLIMITED)
    val sent = mutableListOf<String>()
    var sendResult = true
    var closeCount = 0
    var lastUrl: String? = null

    override fun connect(url: String): Flow<WsMessage> {
        lastUrl = url
        return incoming.receiveAsFlow()
    }

    override fun send(text: String): Boolean {
        sent += text
        return sendResult
    }

    override fun close() {
        closeCount++
        incoming.trySend(WsMessage.Disconnected)
        incoming.close()
    }
}
```

Create `app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`:

```kotlin
package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WsMessage
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for VoiceClient's server-message parsing and state machine,
 * driven through FakeWebSocketClient (no network, no Android framework —
 * android.util.Log is covered by unitTests.returnDefaultValues=true).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceClientParseTest {

    private lateinit var fake: FakeWebSocketClient
    private lateinit var voice: VoiceClient
    private val events = mutableListOf<VoiceEvent>()

    /** Connect through the fake socket; optionally complete the backend-ready handshake. */
    private fun TestScope.startConnected(confirm: Boolean = true) {
        fake = FakeWebSocketClient()
        voice = VoiceClient(OkHttpClient(), "ws://box.test", wsFactory = { fake })
        voice.events.onEach { events.add(it) }.launchIn(backgroundScope)
        voice.connect(VoiceBackend.GEMINI_LIVE, "op-test", "Orus", backgroundScope)
        runCurrent()
        fake.incoming.trySend(WsMessage.Connected)
        runCurrent()
        if (confirm) serverSends("""{"type":"connected"}""")
    }

    private fun TestScope.serverSends(json: String) {
        fake.incoming.trySend(WsMessage.Text(json))
        runCurrent()
    }

    @Test
    fun `transport open stays CONNECTING until server confirms backend ready`() = runTest {
        startConnected(confirm = false)
        assertEquals(VoiceState.CONNECTING, voice.state.value)
        // The connect handshake frame went out on transport open
        assertTrue(fake.sent.any { it.contains("\"type\":\"connect\"") && it.contains("op-test") })
        assertTrue(fake.lastUrl!!.contains("/ws/gemini-live/"))

        serverSends("""{"type":"connected"}""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
    }
}
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — `:app:compileDebugUnitTestKotlin` compilation errors: `This type is final, so it cannot be inherited from` (FakeWebSocketClient) and `Cannot find a parameter with this name: wsFactory`.

**Step 3: Write minimal implementation**

In `WebSocketClient.kt` make the class and its three public members open (four one-word edits):

- Line 27: `class WebSocketClient(baseClient: OkHttpClient) {` → `open class WebSocketClient(baseClient: OkHttpClient) {`
- Line 38: `fun connect(url: String): Flow<WsMessage> = flow {` → `open fun connect(url: String): Flow<WsMessage> = flow {`
- Line 93: `fun send(text: String): Boolean = webSocket?.send(text) ?: false` → `open fun send(text: String): Boolean = webSocket?.send(text) ?: false`
- Line 95: `fun close() {` → `open fun close() {`

In `VoiceClient.kt` replace lines 42-43:

```kotlin
class VoiceClient(private val client: OkHttpClient, private val baseWsUrl: String) {
    private val wsClient = WebSocketClient(client)
```

with:

```kotlin
class VoiceClient(
    private val client: OkHttpClient,
    private val baseWsUrl: String,
    // Testability seam (voice upgrade pass P3.1): production uses the real
    // WebSocketClient; unit tests inject FakeWebSocketClient. The reconnect
    // loop (P3.8) also uses this to open a fresh socket per leg.
    private val wsFactory: (OkHttpClient) -> WebSocketClient = { WebSocketClient(it) },
) {
    private var wsClient = wsFactory(client)
```

The existing 2-arg call site (`VoiceScreen.kt:180`) is unaffected (defaulted param).

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (1 test)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/api/WebSocketClient.kt \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/FakeWebSocketClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "test(voice): testability seam — open WebSocketClient, injectable ws factory, establish VoiceClientParseTest"
```

---

### Task P3.2: parseMessage handles server `status` frames

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (VoiceEvent sealed class :34-40; parseMessage — insert before the `"error" ->` case, currently :335)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientParseTest`:

```kotlin
    @Test
    fun `status frame emits Status event without changing state`() = runTest {
        startConnected()
        serverSends("""{"type":"status","message":"Connecting to Gemini Live..."}""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertEquals(
            "Connecting to Gemini Live...",
            events.filterIsInstance<VoiceEvent.Status>().single().message
        )
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — compilation error `Unresolved reference 'Status'`.

**Step 3: Write minimal implementation**

In the `VoiceEvent` sealed class (after `data object Disconnected : VoiceEvent()`, line 39) add:

```kotlin
    /** Informational server progress, e.g. "Connecting to Gemini Live..." (gemini_live_routes.py:1620). */
    data class Status(val message: String) : VoiceEvent()
```

In `parseMessage`, insert a new case immediately BEFORE the `"error" ->` case (anchor: `"error" -> {`, line 335):

```kotlin
                // ---- Session-health frames (2026-07-11 voice upgrade pass) ----

                "status" -> {
                    val msg = obj["message"]?.jsonPrimitive?.content ?: data
                    if (msg.isNotBlank()) _events.emit(VoiceEvent.Status(msg))
                    android.util.Log.d("VoiceClient", "Server status: $msg")
                }
```

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (2 tests)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): handle server 'status' frames — VoiceEvent.Status (was silently dropped)"
```

---

### Task P3.3: RECONNECTING state + server `reconnecting`/`reconnected` handling

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (VoiceState enum :32; VoiceEvent :34-41; parseMessage — insert after the `"status"` case from P3.2)
- Modify: `.../ui/voice/VoiceScreen.kt:682-688` (exhaustive `when(voiceState)` — one new arm, compilation-forced only, no other UI work)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientParseTest`:

```kotlin
    @Test
    fun `server reconnecting drives RECONNECTING and reconnected restores CONNECTED`() = runTest {
        startConnected()
        serverSends("""{"type":"audio_delta","data":"AAAA"}""")
        assertEquals(VoiceState.SPEAKING, voice.state.value)

        serverSends("""{"type":"reconnecting","message":"Gemini connection lost - reconnecting"}""")
        assertEquals(VoiceState.RECONNECTING, voice.state.value)
        assertFalse(voice.isAISpeaking.value)
        assertEquals(
            "Gemini connection lost - reconnecting",
            events.filterIsInstance<VoiceEvent.Reconnecting>().single().message
        )

        serverSends("""{"type":"reconnected"}""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertTrue(events.last() is VoiceEvent.Reconnected)
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — compilation errors `Unresolved reference 'RECONNECTING'` / `'Reconnecting'`.

**Step 3: Write minimal implementation**

1. VoiceState enum (line 32) becomes:

```kotlin
enum class VoiceState { DISCONNECTED, CONNECTING, CONNECTED, SPEAKING, LISTENING, RECONNECTING, ERROR }
```

2. Add to `VoiceEvent` (after `Status` from P3.2):

```kotlin
    /** Backend lost its upstream provider socket and is retrying (server frame OR client leg-drop). */
    data class Reconnecting(val message: String) : VoiceEvent()
    /** The session is live again after a reconnect. */
    data object Reconnected : VoiceEvent()
```

3. In `parseMessage`, insert immediately after the `"status"` case:

```kotlin
                "reconnecting" -> {
                    // Backend lost its upstream (Gemini/OpenAI/xAI) socket and is
                    // retrying — surface it instead of showing "Connected" forever.
                    _isAISpeaking.value = false
                    aiStoppedSpeakingAt = System.currentTimeMillis()
                    _state.value = VoiceState.RECONNECTING
                    val msg = obj["message"]?.jsonPrimitive?.content
                        ?: data.ifBlank { "Reconnecting to voice backend..." }
                    _events.emit(VoiceEvent.Reconnecting(msg))
                    android.util.Log.w("VoiceClient", "Server reconnecting: $msg")
                }

                "reconnected" -> {
                    lastPongTime = System.currentTimeMillis()
                    _state.value = VoiceState.CONNECTED
                    _events.emit(VoiceEvent.Reconnected)
                    android.util.Log.i("VoiceClient", "Server reconnected upstream")
                }
```

4. `VoiceScreen.kt` — the `when (voiceState)` at lines 682-688 is exhaustive with no `else`; the new enum entry breaks compilation without this arm. Insert after `VoiceState.CONNECTING -> "Connecting..."` (line 682):

```kotlin
            VoiceState.RECONNECTING -> "Reconnecting..."
```

(That is the ONLY VoiceScreen change in this phase — real UI treatment is the Phase 3b UI workstream.)

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (3 tests)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): RECONNECTING state + server reconnecting/reconnected frames handled"
```

---

### Task P3.4: Terminal server `disconnected` → ERROR + socket close (kills the silent dead-session)

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (VoiceEvent; parseMessage — insert after `"reconnected"` case; `WsMessage.Disconnected` handler :157-162; connect() reset; new field near :84)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientParseTest`:

```kotlin
    @Test
    fun `terminal disconnected flips to ERROR closes socket and surfaces reason`() = runTest {
        startConnected()
        serverSends("""{"type":"disconnected","data":"Connection lost after multiple reconnection attempts"}""")
        assertEquals(VoiceState.ERROR, voice.state.value)
        assertEquals(
            "Connection lost after multiple reconnection attempts",
            events.filterIsInstance<VoiceEvent.ServerDisconnected>().single().reason
        )
        // Error also emitted so existing VoiceScreen error surfacing fires unchanged
        assertTrue(events.any { it is VoiceEvent.Error })
        assertEquals(1, fake.closeCount)
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — compilation error `Unresolved reference 'ServerDisconnected'`.

**Step 3: Write minimal implementation**

1. Add to `VoiceEvent` (after `Reconnected`):

```kotlin
    /** TERMINAL: backend gave up on its upstream connection. The session is dead. */
    data class ServerDisconnected(val reason: String) : VoiceEvent()
```

2. Add a field next to `lastPongTime` (line 83-84):

```kotlin
    // Set when the server declares the session terminally dead ({"type":"disconnected"});
    // the reconnect loop (P3.8) must NOT resurrect a server-declared-dead session.
    @Volatile
    private var serverTerminal = false
```

3. In `connect()`, after `currentVoice = voice` (line 101) add:

```kotlin
        serverTerminal = false
```

4. In `parseMessage`, insert after the `"reconnected"` case:

```kotlin
                "disconnected" -> {
                    // TERMINAL: e.g. "Connection lost after multiple reconnection
                    // attempts" (gemini_live_routes.py:1350-1354). Without this case
                    // the UI showed "Connected — listening" forever while the mic
                    // streamed into a dead pipe (the silent Gemini failure,
                    // design doc 2026-07-11).
                    val reason = obj["message"]?.jsonPrimitive?.content
                        ?: data.ifBlank { "Voice backend disconnected" }
                    serverTerminal = true
                    _isAISpeaking.value = false
                    _currentAiText.value = ""
                    _state.value = VoiceState.ERROR
                    _events.emit(VoiceEvent.ServerDisconnected(reason))
                    // Also emit Error so existing VoiceScreen surfacing (persistent
                    // text + toast + haptic) fires with zero UI changes.
                    _events.emit(VoiceEvent.Error(reason))
                    android.util.Log.e("VoiceClient", "Server terminal disconnect: $reason")
                    wsClient.close()
                }
```

5. The close above makes the transport emit `WsMessage.Disconnected`, whose handler (lines 157-162) would overwrite ERROR with DISCONNECTED and mask the failure. Replace that handler:

```kotlin
                    is WsMessage.Disconnected -> {
                        // Preserve a terminal ERROR (server-declared disconnect or
                        // connect timeout): the socket close that FOLLOWS the failure
                        // must not repaint it as a clean disconnect.
                        if (_state.value != VoiceState.ERROR) _state.value = VoiceState.DISCONNECTED
                        _isAISpeaking.value = false
                        _currentAiText.value = ""
                        _events.emit(VoiceEvent.Disconnected)
                    }
```

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (4 tests)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): terminal server 'disconnected' -> ERROR + socket close, ERROR preserved through teardown"
```

---

### Task P3.5: `else` branch — unknown message types logged, never silently dropped

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (parseMessage `when` — add else after the `"error" ->` case)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientParseTest`:

```kotlin
    @Test
    fun `unknown message types are inert - no state change no events no crash`() = runTest {
        startConnected()
        val eventsBefore = events.size
        // NOT in this list: tool_call / tool_result / image_task / video_task /
        // music_task — those five get real parsing (VoiceEvent.Tool) in P3.9a.
        serverSends("""{"type":"some_future_frame","data":"x"}""")
        serverSends("""{"type":"session_stats","data":{"turns":3}}""")
        serverSends("""not even json""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertEquals(eventsBefore, events.size)
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS already for the assertions — so make the else observable: this task is behavior-by-log. Run the test anyway; it should PASS (the `when` currently falls through silently). The failing signal here is code-review-level (no else exists). Proceed to Step 3 and keep the test as a regression guard that unknown types never corrupt state.

**Step 3: Write minimal implementation**

In `parseMessage`, add as the final branch of the `when(type)` (after the `"error" -> { ... }` case's closing brace):

```kotlin
                else -> android.util.Log.w(
                    "VoiceClient",
                    "Unhandled server message type '$type': ${raw.take(160)}"
                )
```

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (5 tests)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): log unknown voice WS frame types instead of silently dropping them"
```

---

### Task P3.6: CONNECTING timeout (15s) → ERROR instead of infinite "Connecting..."

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (companion :86-90; new field + arm helper; connect(); `"connected"` parse case :238-241; disconnect() :168-177)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientParseTest`:

```kotlin
    @Test
    fun `CONNECTING times out to ERROR when backend never becomes ready`() = runTest {
        startConnected(confirm = false)
        assertEquals(VoiceState.CONNECTING, voice.state.value)
        advanceTimeBy(VoiceClient.CONNECT_TIMEOUT_MS + 1)
        runCurrent()
        assertEquals(VoiceState.ERROR, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Error && it.message.contains("ready") })
        assertTrue(fake.closeCount >= 1)
    }

    @Test
    fun `connect timeout does not fire once backend confirmed`() = runTest {
        startConnected()
        advanceTimeBy(VoiceClient.CONNECT_TIMEOUT_MS + 1)
        runCurrent()
        assertEquals(VoiceState.CONNECTED, voice.state.value)
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — compilation error `Unresolved reference 'CONNECT_TIMEOUT_MS'`.

**Step 3: Write minimal implementation**

1. Companion object (lines 86-90) — add:

```kotlin
        // Recon 2026-07-11 silent-failure #2: no bound on the CONNECTING→CONNECTED
        // wait — a hung backend setup left the UI at "Connecting..." forever while
        // server pongs kept the keepalive happy.
        const val CONNECT_TIMEOUT_MS = 15_000L
```

2. New field next to `keepaliveJob` (line 77):

```kotlin
    private var connectTimeoutJob: Job? = null
```

3. New private function (place after `startKeepalive()`):

```kotlin
    // Bounded wait for the backend-ready confirm ("connected"/"setup_complete").
    // Guarded on state so a confirm/error that already arrived makes this a no-op.
    private fun armConnectTimeout(scope: CoroutineScope) {
        connectTimeoutJob?.cancel()
        connectTimeoutJob = scope.launch {
            delay(CONNECT_TIMEOUT_MS)
            if (_state.value == VoiceState.CONNECTING) {
                android.util.Log.w("VoiceClient", "Backend not ready after ${CONNECT_TIMEOUT_MS}ms — failing")
                _state.value = VoiceState.ERROR
                _events.emit(VoiceEvent.Error("Voice backend did not become ready within ${CONNECT_TIMEOUT_MS / 1000}s"))
                wsClient.close()
            }
        }
    }
```

4. In `connect()`, immediately after `keepaliveJob?.cancel()` (line 104) add:

```kotlin
        armConnectTimeout(scope)
```

5. In the `"connected", "setup_complete" ->` parse case (line 238), add as the first statement:

```kotlin
                    connectTimeoutJob?.cancel()
```

6. In `disconnect()` (line 168), add as the first statement:

```kotlin
        connectTimeoutJob?.cancel()
```

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (7 tests)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): 15s CONNECTING timeout -> ERROR instead of infinite Connecting"
```

---

### Task P3.7: Mic-path send-failure detection (SttStreamClient parity)

**Files:**
- Modify: `.../data/voice/VoiceClient.kt:179-193` (`sendAudioChunk` / `sendAudioCommit`)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientParseTest` (assertions deliberately limited to return value + close, so they stay valid when P3.8 changes what FOLLOWS a dead leg):

```kotlin
    @Test
    fun `audio send failure on a live session returns false and drops the socket`() = runTest {
        startConnected()
        fake.sendResult = false
        val ok = voice.sendAudioChunk("QUJD")
        runCurrent()
        assertFalse(ok)
        assertEquals(1, fake.closeCount)
    }

    @Test
    fun `audio send success returns true and keeps the socket`() = runTest {
        startConnected()
        assertTrue(voice.sendAudioChunk("QUJD"))
        runCurrent()
        assertEquals(0, fake.closeCount)
        assertTrue(fake.sent.any { it.contains("\"type\":\"audio_input\"") })
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — first test: `assertFalse` fails (sendAudioChunk returns Unit today → compilation error `Type mismatch: inferred type is Unit but Boolean was expected`).

**Step 3: Write minimal implementation**

Replace lines 179-193 (`sendAudioChunk` + `sendAudioCommit`) with:

```kotlin
    /**
     * Send a base64-encoded PCM16 audio chunk (mic input). Returns delivery result —
     * recon 2026-07-11 silent-failure #5: send() was fire-and-forget, so on a dead
     * socket every mic chunk dropped silently for 15-30s until the keepalive noticed
     * (SttStreamClient.kt:369-372 is the proven contrast). The Phase 3b mic loop
     * breaks on false; the client itself drops the dead leg immediately.
     */
    fun sendAudioChunk(base64Audio: String): Boolean {
        val msg = buildJsonObject {
            put("type", "audio_input")
            put("data", base64Audio)
        }
        val ok = wsClient.send(msg.toString())
        if (!ok) onSendFailure("audio_input")
        return ok
    }

    /** Signal end of user speech turn — server triggers AI response. */
    fun sendAudioCommit(): Boolean {
        val msg = buildJsonObject { put("type", "audio_commit") }
        val ok = wsClient.send(msg.toString())
        android.util.Log.d("VoiceClient", "Sent audio_commit (delivered=$ok)")
        if (!ok) onSendFailure("audio_commit")
        return ok
    }

    // A failed send on a session we believe is live = dead socket. Close the leg so
    // the transport surfaces Disconnected NOW (and, after P3.8, the reconnect loop
    // resumes) instead of waiting for the keepalive pong timeout.
    private fun onSendFailure(frameType: String) {
        val s = _state.value
        if (s == VoiceState.CONNECTED || s == VoiceState.SPEAKING || s == VoiceState.LISTENING) {
            android.util.Log.w("VoiceClient", "$frameType send failed — socket dead, dropping leg")
            wsClient.close()
        }
    }
```

(Callers in `VoiceScreen.kt` ignore the new return values — Kotlin permits discarding results, no UI change needed.)

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (9 tests)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): detect dead socket on audio send failure — close leg immediately (SttStreamClient parity)"
```

---

### Task P3.8: Client reconnect-with-resume loop (bounded attempts, backoff, observable RECONNECTING)

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (connect() :92-166 rewritten; new fields; companion consts; `"connected"` parse case; disconnect())
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientReconnectTest.kt` (new)

**Step 1: Write the failing test**

Create `app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientReconnectTest.kt`:

```kotlin
package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WsMessage
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Reconnect-with-resume state machine (pattern ported from SttStreamClient.kt:166-235). */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceClientReconnectTest {

    private val fakes = mutableListOf<FakeWebSocketClient>()
    private val events = mutableListOf<VoiceEvent>()
    private lateinit var voice: VoiceClient

    private fun TestScope.startConfirmed() {
        voice = VoiceClient(
            OkHttpClient(), "ws://box.test",
            wsFactory = { FakeWebSocketClient().also { f -> fakes.add(f) } },
        )
        voice.events.onEach { events.add(it) }.launchIn(backgroundScope)
        voice.connect(VoiceBackend.GEMINI_LIVE, "op-test", "Orus", backgroundScope)
        runCurrent()
        confirmLeg(0)
    }

    private fun TestScope.confirmLeg(i: Int) {
        fakes[i].incoming.trySend(WsMessage.Connected); runCurrent()
        fakes[i].incoming.trySend(WsMessage.Text("""{"type":"connected"}""")); runCurrent()
    }

    /** Server-side drop: transport Disconnected, then the flow ends. */
    private fun TestScope.dropLeg(i: Int) {
        fakes[i].incoming.trySend(WsMessage.Disconnected)
        fakes[i].incoming.close()
        runCurrent()
    }

    @Test
    fun `dropped leg reconnects with backoff and resumes on server confirm`() = runTest {
        startConfirmed()
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertEquals(1, fakes.size)

        dropLeg(0)
        assertEquals(VoiceState.RECONNECTING, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Reconnecting })

        advanceTimeBy(VoiceClient.RECONNECT_BASE_DELAY_MS + 1); runCurrent()
        assertEquals(2, fakes.size)  // fresh leg socket opened
        confirmLeg(1)
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Reconnected })
        // Fresh session id per leg — server builds a clean session
        assertNotEquals(fakes[0].lastUrl, fakes[1].lastUrl)
    }

    @Test
    fun `reconnect attempts are bounded - exhaustion ends in ERROR`() = runTest {
        startConfirmed()
        dropLeg(0)
        for (attempt in 1..VoiceClient.MAX_RECONNECTS) {
            assertEquals(VoiceState.RECONNECTING, voice.state.value)
            advanceTimeBy(VoiceClient.RECONNECT_BASE_DELAY_MS * attempt + 1); runCurrent()
            // fresh leg opened — kill it before it confirms
            fakes.last().incoming.close(); runCurrent()
        }
        assertEquals(VoiceState.ERROR, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Error && it.message.contains("reconnect") })
    }

    @Test
    fun `user disconnect never reconnects`() = runTest {
        startConfirmed()
        voice.disconnect(); runCurrent()
        assertEquals(VoiceState.DISCONNECTED, voice.state.value)
        advanceTimeBy(120_000); runCurrent()
        assertEquals(1, fakes.size)  // no new leg
        assertEquals(VoiceState.DISCONNECTED, voice.state.value)
    }

    @Test
    fun `server terminal disconnected never reconnects`() = runTest {
        startConfirmed()
        fakes[0].incoming.trySend(WsMessage.Text(
            """{"type":"disconnected","data":"Connection lost after multiple reconnection attempts"}"""))
        runCurrent()
        assertEquals(VoiceState.ERROR, voice.state.value)
        advanceTimeBy(120_000); runCurrent()
        assertEquals(1, fakes.size)  // server said dead — stay dead
        assertEquals(VoiceState.ERROR, voice.state.value)
    }
}
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientReconnectTest"`
Expected: FAIL — compilation error `Unresolved reference 'RECONNECT_BASE_DELAY_MS'` (and `MAX_RECONNECTS`).

**Step 3: Write minimal implementation**

1. Companion object — add:

```kotlin
        // Reconnect-with-resume (ported from SttStreamClient.kt:166-235): bounded,
        // linear backoff, attempts NEVER reset mid-session (design doc: reset-on-
        // success is how the server-side loop defeated its own max_reconnects).
        const val MAX_RECONNECTS = 10
        const val RECONNECT_BASE_DELAY_MS = 1_000L
```

2. Fields — next to `serverTerminal` add:

```kotlin
    @Volatile
    private var userDisconnected = false
    @Volatile
    private var reconnectAttempts = 0
```

3. Replace the whole `connect()` function (currently lines 92-166, as amended by P3.6) with:

```kotlin
    fun connect(
        backend: VoiceBackend,
        operator: String,
        voice: String,
        scope: CoroutineScope,
        sessionConfig: VoiceSessionConfig? = null,
    ) {
        this.scope = scope
        currentOperator = operator
        currentVoice = voice
        userDisconnected = false
        serverTerminal = false
        reconnectAttempts = 0
        _state.value = VoiceState.CONNECTING
        connectionJob?.cancel()
        keepaliveJob?.cancel()
        armConnectTimeout(scope)

        connectionJob = scope.launch {
            // One logical voice session across transient WS drops (Tailscale
            // idle-reap, network blips). Each iteration is one physical socket leg.
            while (isActive && !userDisconnected && !serverTerminal &&
                _state.value != VoiceState.ERROR
            ) {
                val legWs = wsFactory(client)
                wsClient = legWs
                val url = buildUrl(backend, sessionConfig)
                android.util.Log.d("VoiceClient", "Connecting to: $url")
                try {
                    legWs.connect(url).collect { msg ->
                        when (msg) {
                            is WsMessage.Connected -> {
                                // Stay at CONNECTING/RECONNECTING until the server
                                // confirms the provider backend is actually ready.
                                lastPongTime = System.currentTimeMillis()
                                val connectMsg = buildJsonObject {
                                    put("type", "connect")
                                    put("operator", currentOperator)
                                    put("voice", currentVoice)
                                }
                                legWs.send(connectMsg.toString())
                                android.util.Log.d("VoiceClient", "WS leg open, sent connect, waiting for backend ready...")
                                startKeepalive()
                            }
                            is WsMessage.Text -> parseMessage(msg.text)
                            is WsMessage.Closing ->
                                android.util.Log.w("VoiceClient", "Server closing: ${msg.code} ${msg.reason}")
                            is WsMessage.Error ->
                                android.util.Log.e("VoiceClient", "WS transport error: ${msg.error.message}")
                            is WsMessage.Disconnected ->
                                android.util.Log.d("VoiceClient", "WS leg disconnected")
                        }
                    }
                } catch (e: Exception) {
                    android.util.Log.e("VoiceClient", "Connection loop error: ${e.message}")
                }
                // This leg's socket is gone. Terminal exits: user hangup, server-
                // declared dead session, or a state already forced to ERROR
                // (connect timeout / max attempts).
                if (!isActive || userDisconnected || serverTerminal ||
                    _state.value == VoiceState.ERROR
                ) break
                reconnectAttempts++
                if (reconnectAttempts > MAX_RECONNECTS) {
                    _state.value = VoiceState.ERROR
                    _events.emit(VoiceEvent.Error("Voice connection lost after $MAX_RECONNECTS reconnect attempts"))
                    break
                }
                _isAISpeaking.value = false
                _currentAiText.value = ""
                _state.value = VoiceState.RECONNECTING
                _events.emit(VoiceEvent.Reconnecting("Connection dropped — reconnecting (attempt $reconnectAttempts)"))
                android.util.Log.w("VoiceClient", "Voice WS dropped — reconnecting (attempt $reconnectAttempts)")
                delay(RECONNECT_BASE_DELAY_MS * reconnectAttempts)
            }
            if (userDisconnected && _state.value != VoiceState.ERROR) {
                _state.value = VoiceState.DISCONNECTED
                _isAISpeaking.value = false
                _currentAiText.value = ""
                _events.emit(VoiceEvent.Disconnected)
            }
        }
    }

    /** One URL per leg — FRESH session id so the server builds a clean session. */
    private fun buildUrl(backend: VoiceBackend, sessionConfig: VoiceSessionConfig?): String = buildString {
        append(baseWsUrl)
        append(backend.wsPath)
        append('/')
        append(UUID.randomUUID().toString())
        append("?operator=").append(currentOperator)
        append("&voice=").append(currentVoice)
        sessionConfig?.let { cfg ->
            cfg.model?.let { append("&model=").append(it) }
            cfg.vadType?.let { append("&vad_type=").append(it) }
            cfg.vadEagerness?.let { append("&vad_eagerness=").append(it) }
            cfg.idleTimeoutMs?.let { append("&idle_timeout_ms=").append(it) }
            cfg.vadStart?.let { append("&vad_sensitivity_start=").append(it) }
            cfg.vadEnd?.let { append("&vad_sensitivity_end=").append(it) }
            cfg.thinkingLevel?.let { append("&thinking_level=").append(it) }
        }
    }
```

Note this REPLACES the old `WsMessage.Disconnected` handler from P3.4 (state/event handling for leg-ends now lives in the loop; the ERROR-preserve rule survives via the loop's break condition).

4. In the `"connected", "setup_complete" ->` parse case, replace the body with:

```kotlin
                "connected", "setup_complete" -> {
                    connectTimeoutJob?.cancel()
                    val wasReconnecting = _state.value == VoiceState.RECONNECTING
                    lastPongTime = System.currentTimeMillis()
                    _state.value = VoiceState.CONNECTED
                    if (wasReconnecting) _events.emit(VoiceEvent.Reconnected)
                    android.util.Log.d("VoiceClient", "Server message: $type")
                }
```

5. In `disconnect()`, add as the FIRST statement (before `connectTimeoutJob?.cancel()`):

```kotlin
        userDisconnected = true
```

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientReconnectTest" --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (13 tests — both classes; the P3.1-P3.7 tests were written leg-agnostic and must still pass)

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientReconnectTest.kt \
  && git commit -m "feat(voice): client reconnect-with-resume loop — bounded attempts, linear backoff, observable RECONNECTING (SttStreamClient pattern)"
```

---

### Task P3.9: Keepalive drops the leg for reconnect instead of terminal ERROR

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (startKeepalive — currently :200-228 region; new `KeepaliveAction` enum + companion decision function)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientReconnectTest.kt`

**Step 1: Write the failing test**

Add to `VoiceClientReconnectTest`:

```kotlin
    @Test
    fun `keepalive decision table`() {
        assertEquals(KeepaliveAction.BREAK, VoiceClient.keepaliveDecision(VoiceState.DISCONNECTED, 0))
        assertEquals(KeepaliveAction.BREAK, VoiceClient.keepaliveDecision(VoiceState.ERROR, 0))
        // No socket to ping between legs — and a stale pong must not kill the reconnect
        assertEquals(KeepaliveAction.SKIP, VoiceClient.keepaliveDecision(VoiceState.RECONNECTING, 999_999))
        assertEquals(KeepaliveAction.DROP_LEG,
            VoiceClient.keepaliveDecision(VoiceState.CONNECTED, VoiceClient.PONG_TIMEOUT_MS + 1))
        assertEquals(KeepaliveAction.PING, VoiceClient.keepaliveDecision(VoiceState.CONNECTED, 0))
        assertEquals(KeepaliveAction.PING, VoiceClient.keepaliveDecision(VoiceState.SPEAKING, 0))
        assertEquals(KeepaliveAction.PING, VoiceClient.keepaliveDecision(VoiceState.CONNECTING, 0))
    }

    @Test
    fun `ping send failure drops the leg and reconnects instead of terminal ERROR`() = runTest {
        startConfirmed()
        fakes[0].sendResult = false
        advanceTimeBy(VoiceClient.KEEPALIVE_INTERVAL_MS + 1); runCurrent()
        // Failed ping -> leg closed -> reconnect loop takes over (NOT ERROR)
        assertEquals(1, fakes[0].closeCount)
        assertEquals(VoiceState.RECONNECTING, voice.state.value)
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientReconnectTest"`
Expected: FAIL — compilation error `Unresolved reference 'KeepaliveAction'`.

**Step 3: Write minimal implementation**

1. Add a top-level enum in `VoiceClient.kt` (below the `VoiceEvent` sealed class):

```kotlin
/** What the keepalive tick should do — pure decision, unit-tested directly. */
internal enum class KeepaliveAction { BREAK, SKIP, DROP_LEG, PING }
```

2. Add to the companion object:

```kotlin
        /**
         * Keepalive tick decision (pure — testable without a clock seam; the loop
         * feeds it real elapsed-since-pong). RECONNECTING skips: there is no leg
         * socket to ping, and the reconnect loop owns recovery. Pong timeout and
         * ping failure DROP THE LEG so the reconnect loop resumes the session —
         * pre-P3.9 they flipped terminal ERROR and stranded the user.
         */
        internal fun keepaliveDecision(state: VoiceState, timeSincePongMs: Long): KeepaliveAction = when {
            state == VoiceState.DISCONNECTED || state == VoiceState.ERROR -> KeepaliveAction.BREAK
            state == VoiceState.RECONNECTING -> KeepaliveAction.SKIP
            timeSincePongMs > PONG_TIMEOUT_MS -> KeepaliveAction.DROP_LEG
            else -> KeepaliveAction.PING
        }
```

3. Replace the body of `startKeepalive()` (the whole function, currently lines 200-228 region):

```kotlin
    // Application-level keepalive matching Portal pattern. Dead-leg detection hands
    // off to the reconnect loop (close the leg) rather than declaring terminal ERROR.
    private fun startKeepalive() {
        keepaliveJob?.cancel()
        keepaliveJob = scope?.launch {
            while (isActive) {
                delay(KEEPALIVE_INTERVAL_MS)
                when (keepaliveDecision(_state.value, System.currentTimeMillis() - lastPongTime)) {
                    KeepaliveAction.BREAK -> break
                    KeepaliveAction.SKIP -> continue
                    KeepaliveAction.DROP_LEG -> {
                        android.util.Log.w("VoiceClient", "No pong in ${System.currentTimeMillis() - lastPongTime}ms — dropping leg for reconnect")
                        wsClient.close()
                    }
                    KeepaliveAction.PING -> {
                        val ping = buildJsonObject { put("type", "ping") }
                        if (!wsClient.send(ping.toString())) {
                            android.util.Log.w("VoiceClient", "Ping send failed — dropping leg for reconnect")
                            wsClient.close()
                        }
                    }
                }
            }
        }
    }
```

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientReconnectTest" --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: PASS (15 tests)

Then run the FULL suite as the phase gate (~35s):
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline`
Expected: `BUILD SUCCESSFUL` — zero failures across the whole app (pre-existing tests, e.g. ConstantsLiveDefaultsTest, unaffected).

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientReconnectTest.kt \
  && git commit -m "feat(voice): keepalive pong-timeout/ping-failure drops the leg for reconnect instead of terminal ERROR"
```

---

### Task P3.9a: VoiceEvent.Tool — tool_call/tool_result/media-task frames parsed (feeds the Phase 3b transcript chips)

**Files:**
- Modify: `.../data/voice/VoiceClient.kt` (VoiceEvent — after `ServerDisconnected` from P3.4; parseMessage — insert after the `"disconnected"` case from P3.4, before the `"error" ->` case)
- Test: `.../app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt`

Backend emission shapes (verified against `Orchestrator/routes/gemini_live_routes.py`):
- `{"type":"tool_call","data":{"name":<tool>,"arguments":{...}}}` (:930-934)
- `{"type":"tool_result","data":{"name":<tool>,"result_length":<int>}}` (:1293-1298)
- `{"type":"image_task","data":{"task_id":..,"prompt":..,"count":..}}` (:978-983)
- `{"type":"video_task","data":{"task_id":..,"prompt":..,"duration":..,"resolution":..}}` (:1026-1031)
- `{"type":"music_task","data":{"task_id":..,"prompt":..,"sample_count":..}}` (:1059-1065)

NOTE: `data` is an OBJECT for all five, so the string-primitive `val data` extracted at the top of `parseMessage` yields `""` for them — parse `obj["data"]?.jsonObject` in these cases.

**Step 1: Write the failing test**

Add to `VoiceClientParseTest`:

```kotlin
    @Test
    fun `tool and media-task frames emit VoiceEvent Tool without changing state`() = runTest {
        startConnected()
        serverSends("""{"type":"tool_call","data":{"name":"search_snapshots","arguments":{"query":"upload bug"}}}""")
        serverSends("""{"type":"tool_result","data":{"name":"search_snapshots","result_length":2048}}""")
        serverSends("""{"type":"image_task","data":{"task_id":"t-1","prompt":"sunset over water","count":2}}""")
        serverSends("""{"type":"video_task","data":{"task_id":"t-2","prompt":"drone shot","duration":8,"resolution":"720p"}}""")
        serverSends("""{"type":"music_task","data":{"task_id":"t-3","prompt":"epic orchestral","sample_count":1}}""")

        assertEquals(VoiceState.CONNECTED, voice.state.value)
        val tools = events.filterIsInstance<VoiceEvent.Tool>()
        assertEquals(5, tools.size)
        assertEquals(VoiceEvent.Tool("tool_call", "search_snapshots", """{"query":"upload bug"}"""), tools[0])
        assertEquals(VoiceEvent.Tool("tool_result", "search_snapshots", "2048 chars"), tools[1])
        assertEquals(VoiceEvent.Tool("image_task", "", "sunset over water"), tools[2])
        assertEquals(VoiceEvent.Tool("video_task", "", "drone shot"), tools[3])
        assertEquals(VoiceEvent.Tool("music_task", "", "epic orchestral"), tools[4])
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest"`
Expected: FAIL — compilation error `Unresolved reference 'Tool'`.

**Step 3: Write minimal implementation**

1. Add to `VoiceEvent` (after `ServerDisconnected` from P3.4):

```kotlin
    /**
     * Tool/media-task activity surfaced by the backend bridge (gemini_live_routes.py).
     * kind ∈ tool_call | tool_result | image_task | video_task | music_task;
     * name = tool name (blank for media tasks); detail = compact human summary
     * (args JSON / result size / prompt). Consumed by the Phase 3b transcript chips (P3.17).
     */
    data class Tool(val kind: String, val name: String, val detail: String) : VoiceEvent()
```

2. In `parseMessage`, insert immediately AFTER the `"disconnected"` case from P3.4 (before `"error" ->`):

```kotlin
                // ---- Tool/media-task activity (P3.9a — rendered as chips in 3b).
                // data is an OBJECT for these frames; the string `data` above is "".
                "tool_call", "tool_result", "image_task", "video_task", "music_task" -> {
                    val d = try { obj["data"]?.jsonObject } catch (_: Exception) { null }
                    val name = try {
                        d?.get("name")?.jsonPrimitive?.content
                    } catch (_: Exception) { null } ?: ""
                    val detail = when (type) {
                        "tool_call" -> try {
                            d?.get("arguments")?.jsonObject?.takeIf { it.isNotEmpty() }?.toString()
                        } catch (_: Exception) { null } ?: ""
                        "tool_result" -> try {
                            d?.get("result_length")?.jsonPrimitive?.content?.let { "$it chars" }
                        } catch (_: Exception) { null } ?: ""
                        else -> try {  // image_task / video_task / music_task
                            d?.get("prompt")?.jsonPrimitive?.content
                        } catch (_: Exception) { null } ?: ""
                    }
                    _events.emit(VoiceEvent.Tool(type, name, detail))
                    android.util.Log.d("VoiceClient", "Tool frame $type: $name ${detail.take(80)}")
                }
```

(`jsonObject` is already imported in `VoiceClient.kt` — used by the `"provenance"` case.)

**Step 4: Run test to verify it passes**
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceClientParseTest" --tests "com.aiblackbox.portal.data.voice.VoiceClientReconnectTest"`
Expected: PASS (16 tests — 10 parse + 6 reconnect)

Then run the FULL suite as the phase gate (~35s):
Run: `cd "$APP" && ./gradlew :app:testDebugUnitTest --offline`
Expected: `BUILD SUCCESSFUL` — zero failures across the whole app.

**Step 5: Commit**
```bash
cd "$APP" && git add \
  app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt \
  app/src/test/java/com/aiblackbox/portal/data/voice/VoiceClientParseTest.kt \
  && git commit -m "feat(voice): parse tool_call/tool_result/image_task/video_task/music_task frames -> VoiceEvent.Tool (feeds transcript chips)"
```

---

**Phase 3a exit criteria:** `VoiceClient` handles `status`/`reconnecting`/`reconnected`/`disconnected` + surfaces `tool_call`/`tool_result`/`image_task`/`video_task`/`music_task` as `VoiceEvent.Tool` + logs unknown frames; CONNECTING is bounded at 15s; a dead socket is detected within one mic chunk; transport drops transparently reconnect (≤10 attempts, linear backoff, `RECONNECTING` observable via `state` and `VoiceEvent.Reconnecting`/`Reconnected`); 16 new unit tests in `app/src/test/java/com/aiblackbox/portal/data/voice/` all green with the full suite. UI treatment of the new states/events (banner, tool chips, mic-loop break on `sendAudioChunk == false`, toasts) is Phase 3b.

---

## Phase 3b — Android voice UI uplift (P3.10–P3.19)

**Interface provided by P3.1–P3.9a (data layer, drafted separately — do NOT re-create; if the landed P3a names differ, adapt the references here, not the P3a code):**
- `VoiceState.RECONNECTING` added to the enum in `VoiceClient.kt`; P3a added minimal exhaustive-`when` arms in `VoiceScreen.kt` to keep the tree green.
- New events on `VoiceEvent`: `data class Status(val message: String)`, `data class Reconnecting(val message: String)`, `data object Reconnected`, `data class ServerDisconnected(val reason: String)`, `data class Tool(val kind: String, val name: String, val detail: String)` (kind ∈ `tool_call`/`tool_result`/`image_task`/`video_task`/`music_task`, from P3.9a).
- Terminal states: server `{"type":"disconnected"}` flips state to `ERROR` (NOT `DISCONNECTED`) and emits `ServerDisconnected(reason)` + `Error(reason)`; CONNECTING timeout (15s) and reconnect exhaustion also land at `ERROR` with a `VoiceEvent.Error`; only a user hangup (`disconnect()`) lands at `DISCONNECTED`. P3a preserves `ERROR` through the socket teardown that follows.

All paths below are relative to the app root `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal` unless absolute. Gradle gate (also the compile gate for UI-only tasks):
`cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline`
Commit steps use: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"`.

### Task P3.10: VoiceCatalog — tolerant /status payload parser + fallback helpers

**Files:**
- Create: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceCatalog.kt
- Test: app/src/test/java/com/aiblackbox/portal/data/voice/VoiceCatalogTest.kt

**Step 1: Write the failing test**

```kotlin
package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceCatalogTest {
    @Test fun `parses object-shaped models plus voices default and presets`() {
        val raw = """
        {"status":"ok",
         "models":[{"id":"gpt-realtime-2.1","label":"GPT Realtime 2.1"},{"id":"gpt-realtime-2.1-mini"}],
         "model_default":"gpt-realtime-2.1",
         "voices":["ash","marin","cedar"],
         "presets":[{"id":"sales-agent","name":"Sales Agent"}]}
        """.trimIndent()
        val cat = VoiceCatalog.parse(raw)!!
        assertEquals(
            listOf(
                VoiceCatalogOption("gpt-realtime-2.1", "GPT Realtime 2.1"),
                VoiceCatalogOption("gpt-realtime-2.1-mini", "gpt-realtime-2.1-mini"),
            ),
            cat.models
        )
        assertEquals("gpt-realtime-2.1", cat.modelDefault)
        assertEquals(listOf("ash", "marin", "cedar"), cat.voices)
        assertEquals(listOf(VoiceCatalogOption("sales-agent", "Sales Agent")), cat.presets)
    }

    @Test fun `parses string-shaped models`() {
        val cat = VoiceCatalog.parse("""{"models":["grok-voice-latest","grok-voice-think-fast-1.0"]}""")!!
        assertEquals(2, cat.models.size)
        assertEquals("grok-voice-latest", cat.models[0].id)
        assertEquals("grok-voice-latest", cat.models[0].label)
    }

    @Test fun `missing fields yield empty catalog not null`() {
        val cat = VoiceCatalog.parse("""{"status":"ok","api_key_configured":true}""")!!
        assertTrue(cat.models.isEmpty())
        assertTrue(cat.voices.isEmpty())
        assertTrue(cat.presets.isEmpty())
        assertNull(cat.modelDefault)
    }

    @Test fun `garbage returns null`() {
        assertNull(VoiceCatalog.parse("not json"))
        assertNull(VoiceCatalog.parse("[1,2,3]"))
    }

    @Test fun `fallback helpers prefer non-empty catalog`() {
        val cat = VoiceCatalog(models = listOf(VoiceCatalogOption("m1", "M1")), voices = listOf("v1"))
        assertEquals(listOf("v1"), cat.voicesOrFallback(listOf("fb")))
        assertEquals(listOf("m1" to "M1"), cat.modelsOrFallback(listOf("fb" to "FB")))
        val absent: VoiceCatalog? = null
        assertEquals(listOf("fb"), absent.voicesOrFallback(listOf("fb")))
        assertEquals(listOf("fb" to "FB"), VoiceCatalog().modelsOrFallback(listOf("fb" to "FB")))
    }
}
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.VoiceCatalogTest"`
Expected: FAIL — compile error `Unresolved reference: VoiceCatalog`

**Step 3: Write minimal implementation**

```kotlin
package com.aiblackbox.portal.data.voice

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class VoiceCatalogOption(val id: String, val label: String)

/**
 * Hydrated live-provider catalog parsed from GET /realtime/status,
 * /gemini-live/status, /grok-live/status (VoiceBackend.statusPath).
 * Tolerant: every field optional; models accepted as ["id",...] OR
 * [{"id":..,"label"/"name":..},...]. Pure JVM — no android.util.Log,
 * never throws (unit-testable without Robolectric).
 */
data class VoiceCatalog(
    val models: List<VoiceCatalogOption> = emptyList(),
    val voices: List<String> = emptyList(),
    val modelDefault: String? = null,
    val presets: List<VoiceCatalogOption> = emptyList(),
) {
    companion object {
        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        fun parse(raw: String): VoiceCatalog? = try {
            val obj = json.parseToJsonElement(raw).jsonObject
            val models = obj["models"]?.jsonArray?.mapNotNull { el ->
                try {
                    val o = el.jsonObject
                    val id = o["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                    VoiceCatalogOption(
                        id,
                        o["label"]?.jsonPrimitive?.content
                            ?: o["name"]?.jsonPrimitive?.content ?: id
                    )
                } catch (_: Exception) {
                    try { VoiceCatalogOption(el.jsonPrimitive.content, el.jsonPrimitive.content) }
                    catch (_: Exception) { null }
                }
            }.orEmpty()
            val voices = obj["voices"]?.jsonArray?.mapNotNull {
                try { it.jsonPrimitive.content } catch (_: Exception) { null }
            }.orEmpty()
            val modelDefault = try {
                obj["model_default"]?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }
            } catch (_: Exception) { null }
            val presets = obj["presets"]?.jsonArray?.mapNotNull { el ->
                try {
                    val o = el.jsonObject
                    val id = o["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                    VoiceCatalogOption(
                        id,
                        o["name"]?.jsonPrimitive?.content
                            ?: o["label"]?.jsonPrimitive?.content ?: id
                    )
                } catch (_: Exception) { null }
            }.orEmpty()
            VoiceCatalog(models, voices, modelDefault, presets)
        } catch (_: Exception) {
            null
        }
    }
}

/** Catalog voices when hydrated + non-empty, else the Constants fallback. */
fun VoiceCatalog?.voicesOrFallback(fallback: List<String>): List<String> =
    this?.voices?.takeIf { it.isNotEmpty() } ?: fallback

/** Catalog models as (id, label) when hydrated + non-empty, else the Constants fallback. */
fun VoiceCatalog?.modelsOrFallback(fallback: List<Pair<String, String>>): List<Pair<String, String>> =
    this?.models?.takeIf { it.isNotEmpty() }?.map { it.id to it.label } ?: fallback
```

**Step 4: Run test to verify it passes**
Run: same command as Step 2
Expected: PASS (5 tests)

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceCatalog.kt" \
        "$APP/app/src/test/java/com/aiblackbox/portal/data/voice/VoiceCatalogTest.kt" && \
git commit -m "feat(android-voice): tolerant VoiceCatalog parser for /status hydration (P3.10)"
```

### Task P3.11: Constants — fallback-only lists (realtime 2.1 family, Grok models/voices/efforts) + defaults test

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/util/Constants.kt:113-131, 163-166, 183 (insert after)
- Test: app/src/test/java/com/aiblackbox/portal/util/ConstantsLiveDefaultsTest.kt

**Step 1: Write the failing test**
Append inside `class ConstantsLiveDefaultsTest` (existing 3 gemini tests unchanged — gemini default stays `gemini-3.1-flash-live-preview`):

```kotlin
    @Test fun `realtime default is gpt-realtime-2_1 and 2_1 family is listed`() {
        assertEquals("gpt-realtime-2.1", Constants.LIVE_MODEL_DEFAULTS["realtime"])
        val ids = Constants.MODEL_CONFIG["realtime"].orEmpty().map { it.first }
        assertTrue("gpt-realtime-2.1" in ids)
        assertTrue("gpt-realtime-2.1-mini" in ids)
    }

    @Test fun `grok live fallback models include latest alias and think-fast pin`() {
        val ids = Constants.MODEL_CONFIG["grok-live"].orEmpty().map { it.first }
        assertTrue("" in ids) // Auto — backend resolves grok-voice-latest
        assertTrue("grok-voice-latest" in ids)
        assertTrue("grok-voice-think-fast-1.0" in ids)
        assertEquals("", Constants.LIVE_MODEL_DEFAULTS["grok-live"])
    }

    @Test fun `grok live fallback voices exist and default is a member`() {
        assertTrue(Constants.VOICES_GROK_LIVE.isNotEmpty())
        assertTrue(Constants.DEFAULT_GROK_LIVE_VOICE in Constants.VOICES_GROK_LIVE)
    }

    @Test fun `grok reasoning efforts are high and none`() {
        assertEquals(listOf("high", "none"), Constants.GROK_LIVE_REASONING_EFFORTS)
    }
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.util.ConstantsLiveDefaultsTest"`
Expected: FAIL — `Unresolved reference: VOICES_GROK_LIVE`

**Step 3: Write minimal implementation**
In Constants.kt replace the comment + `"realtime"` and `"grok-live"` entries of `MODEL_CONFIG` (lines 113-123 and 129-131; `"gemini-live"` 124-128 unchanged):

```kotlin
        // OFFLINE FALLBACKS ONLY (P3.11) — the live rosters hydrate from
        // GET /realtime/status | /grok-live/status at voice-screen open
        // (VoiceViewModel catalog fetch; provider-API-as-SoT). 2.1 family per
        // design 2026-07-11 (GA 2026-07-06); backend P0/P2 probes gate the catalog.
        "realtime" to listOf(
            "gpt-realtime-2.1" to "GPT Realtime 2.1 (Newest GA)",
            "gpt-realtime-2.1-mini" to "GPT Realtime 2.1 Mini",
            "gpt-realtime-2" to "GPT Realtime 2",
            "gpt-realtime" to "GPT Realtime (GA alias)",
            "gpt-realtime-1.5" to "GPT Realtime 1.5 (pinned)",
            "gpt-realtime-mini" to "GPT Realtime Mini (cheap, alias)",
            "gpt-realtime-mini-2025-12-15" to "GPT Realtime Mini (Dec 2025 pin)"
        ),
```

```kotlin
        "grok-live" to listOf(
            "" to "Auto (grok-voice-latest)",
            "grok-voice-latest" to "Grok Voice (latest alias)",
            "grok-voice-think-fast-1.0" to "Grok Voice Think Fast 1.0 (pinned)"
        ),
```

Replace `LIVE_MODEL_DEFAULTS` (lines 163-166):

```kotlin
    /** Default model id per live provider — offline fallback; catalog model_default overrides. */
    val LIVE_MODEL_DEFAULTS: Map<String, String> = mapOf(
        "realtime" to "gpt-realtime-2.1",
        "gemini-live" to "gemini-3.1-flash-live-preview",
        "grok-live" to "",  // Auto — backend resolves grok-voice-latest
    )
```

Insert after `const val DEFAULT_GEMINI_LIVE_VOICE = "Orus"` (line 183):

```kotlin
    /** Grok Live voices — offline fallback (hydrated from GET /grok-live/status). */
    val VOICES_GROK_LIVE: List<String> = listOf("Ara", "Rex", "Sal", "Eve", "Leo")
    const val DEFAULT_GROK_LIVE_VOICE = "Ara"

    /** Grok Live reasoning.effort values (grok-voice-think-fast-1.0 background reasoning). */
    val GROK_LIVE_REASONING_EFFORTS: List<String> = listOf("high", "none")
```

**Step 4: Run test to verify it passes**
Run: same command as Step 2, then the full gate `./gradlew :app:testDebugUnitTest --offline`
Expected: PASS (7 tests in class; full suite green)

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/util/Constants.kt" \
        "$APP/app/src/test/java/com/aiblackbox/portal/util/ConstantsLiveDefaultsTest.kt" && \
git commit -m "feat(android-voice): Constants become fallbacks-only — realtime 2.1 family, Grok models/voices/efforts (P3.11)"
```

### Task P3.12: Catalog hydration — fetch statusPath at screen open, dropdowns consume catalog-or-fallback

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt:144-146 (fields), 176-225 (initialize), 621-632 (voice lists), 649-660 (collect), 705-712, 834-840, 846-868, 1036-1115 (config blocks)
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt:16-24
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt:117-125
- Test: gradle compile gate (UI wiring — no new JVM-testable logic; parser + helpers tested in P3.10)

**Step 1: Add `agentId` to the session config + wire URL param**
VoiceSessionConfig.kt — add field to the data class (after `thinkingLevel`):

```kotlin
    val thinkingLevel: String? = null,
    /** P3.12: voice-agent preset id → ?agent= on the WS URL (workstream 3). */
    val agentId: String? = null,
```

VoiceClient.kt — inside the `sessionConfig?.let { cfg ->` block (after the `thinkingLevel` line, :124):

```kotlin
                    cfg.agentId?.let { append("&agent=").append(it) }
```

**Step 2: Hydrate catalogs in VoiceViewModel**
In VoiceScreen.kt add fields after `private var voiceClient: VoiceClient? = null` (line 110):

```kotlin
    // P3.12: per-provider catalogs hydrated from GET {statusPath} at screen open.
    private val _catalogs = MutableStateFlow<Map<VoiceBackend, VoiceCatalog>>(emptyMap())
    val catalogs: StateFlow<Map<VoiceBackend, VoiceCatalog>> = _catalogs.asStateFlow()
    private var catalogFetchJob: Job? = null
```

In `initialize(origin)` after `voiceClient = VoiceClient(...)` (line 180) add (NOTE: confirm `BlackBoxApi.get(path): String` is suspend — it is what `ChatRepository.getModels` calls at ChatRepository.kt:142-144):

```kotlin
            // P3.12: hydrate models/voices/model_default/presets from the /status
            // endpoints (provider-API-as-SoT). Constants lists are fallbacks-only.
            catalogFetchJob?.cancel()
            catalogFetchJob = viewModelScope.launch(Dispatchers.IO) {
                val api = BlackBoxApi(origin)
                VoiceBackend.entries.forEach { b ->
                    try {
                        VoiceCatalog.parse(api.get(b.statusPath))?.let { cat ->
                            _catalogs.value = _catalogs.value + (b to cat)
                            android.util.Log.d("VoiceVM", "Catalog ${b.id}: " +
                                "${cat.models.size} models, ${cat.voices.size} voices, " +
                                "${cat.presets.size} presets, default=${cat.modelDefault}")
                        }
                    } catch (e: Exception) {
                        android.util.Log.w("VoiceVM", "Catalog fetch ${b.id} failed: ${e.message}")
                    }
                }
            }
```

Add imports: `com.aiblackbox.portal.data.voice.VoiceCatalog`, `com.aiblackbox.portal.data.voice.voicesOrFallback`, `com.aiblackbox.portal.data.voice.modelsOrFallback`.

**Step 3: Dropdowns consume catalog-or-fallback**
Replace lines 621-632 (voice lists + `voicesForBackend`):

```kotlin
// Provider-specific voice lists — OFFLINE FALLBACKS (P3.12): the hydrated
// catalog from GET {statusPath} wins when present.
private fun voicesForBackend(backend: VoiceBackend, catalog: VoiceCatalog?): List<String> = when (backend) {
    VoiceBackend.GPT_REALTIME -> catalog.voicesOrFallback(Constants.VOICES_GPT_REALTIME)
    VoiceBackend.GEMINI_LIVE -> catalog.voicesOrFallback(Constants.VOICES_GEMINI_LIVE)
    VoiceBackend.GROK_LIVE -> catalog.voicesOrFallback(Constants.VOICES_GROK_LIVE)
}
```

In the composable, add below `val backend by viewModel.backend.collectAsState()` (line 649):

```kotlin
    val catalogs by viewModel.catalogs.collectAsState()
```

Line 709 (`VoiceBackend.GROK_LIVE -> voicesForBackend(backend).first()`) becomes:

```kotlin
            VoiceBackend.GROK_LIVE -> Constants.DEFAULT_GROK_LIVE_VOICE
```

Voice dropdown (line 836): `options = voicesForBackend(backend, catalogs[backend]).map { it to voiceLabel(backend, it) },`

Add a `modelOptions: List<Pair<String, String>>` parameter to `RealtimeConfigBlock` (after `connected: Boolean`, line 1038) and `GeminiConfigBlock` (line 1098); delete `val modelOpts = Constants.MODEL_CONFIG[...].orEmpty()` (lines 1048 and 1108) and use `options = modelOptions` in each Model `LabeledDropdown`. At the call sites (lines 847, 858) add:

```kotlin
                            modelOptions = catalogs[VoiceBackend.GPT_REALTIME]
                                .modelsOrFallback(Constants.MODEL_CONFIG["realtime"].orEmpty()),
```
```kotlin
                            modelOptions = catalogs[VoiceBackend.GEMINI_LIVE]
                                .modelsOrFallback(Constants.MODEL_CONFIG["gemini-live"].orEmpty()),
```

**Step 4: Verify**
Run: full gradle gate
Expected: BUILD SUCCESSFUL, all existing tests PASS
Also verify the endpoints answer on this box: `curl -s http://localhost:9091/gemini-live/status | head -c 300` → JSON (any shape; parser is tolerant).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" && \
git commit -m "feat(android-voice): hydrate models/voices/presets from /status endpoints; statusPath no longer dead code (P3.12)"
```

### Task P3.13: Settings persistence — hoist config into VoiceViewModel with DataStore write-through; kill "Brandon" fallback; preset dropdown

**Files:**
- Create: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceAgentPreset.kt
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt:112-116, 144, 172-174, 227-228, 230-251, 703-757, 840 (after), 846-868, 912
- Test: app/src/test/java/com/aiblackbox/portal/data/VoiceAgentPresetTest.kt (+ gradle compile gate for the DataStore/Compose wiring — BlackBoxStore.getString/setString are the proven generic prefs path, BlackBoxStore.kt:87-91)

NOTE on preset source: presets do NOT come from the `/status` catalogs (no status endpoint serves a `presets` field — `VoiceCatalog.presets` stays empty until a server someday sends one). They hydrate from `GET /voice-agents` (shape `{"agents":[{id,name,provider,...}]}`, `provider` ∈ `realtime`/`gemini-live`/`grok-live` = `VoiceBackend.id`), the Phase 4 preset-registry contract (same endpoint the Portal helper P3.24 consumes). 404-tolerant: pre-P4 boxes get an empty list and the dropdown hides. **P4.11 builds on this fetch** — Phase 4 ships the server-side registry + apply-at-configure; the Android parser/fetch/dropdown land HERE.

**Step 1: ViewModel state + persistence**
Line 144: `private var currentOperator = "Brandon"` → `private var currentOperator = ""` with comment `// empty-until-store-emits (never hard-code operator; fresh-box rule)`.

After the `_voice` declaration (line 116) add:

```kotlin
    // ── P3.13: persisted voice-agent settings (DataStore write-through via
    // BlackBoxStore.getString/setString; keys prefixed "va_"). null ↔ "".
    private val _realtimeModel = MutableStateFlow(Constants.LIVE_MODEL_DEFAULTS["realtime"] ?: "")
    val realtimeModel: StateFlow<String> = _realtimeModel.asStateFlow()
    private val _realtimeVadType = MutableStateFlow("server_vad")
    val realtimeVadType: StateFlow<String> = _realtimeVadType.asStateFlow()
    private val _realtimeVadEagerness = MutableStateFlow("medium")
    val realtimeVadEagerness: StateFlow<String> = _realtimeVadEagerness.asStateFlow()
    private val _realtimeIdleTimeoutText = MutableStateFlow("")
    val realtimeIdleTimeoutText: StateFlow<String> = _realtimeIdleTimeoutText.asStateFlow()
    private val _geminiModel = MutableStateFlow(Constants.LIVE_MODEL_DEFAULTS["gemini-live"] ?: "")
    val geminiModel: StateFlow<String> = _geminiModel.asStateFlow()
    private val _geminiVadStart = MutableStateFlow<String?>(null)
    val geminiVadStart: StateFlow<String?> = _geminiVadStart.asStateFlow()
    private val _geminiVadEnd = MutableStateFlow<String?>(null)
    val geminiVadEnd: StateFlow<String?> = _geminiVadEnd.asStateFlow()
    private val _geminiThinkingLevel = MutableStateFlow<String?>(null)
    val geminiThinkingLevel: StateFlow<String?> = _geminiThinkingLevel.asStateFlow()
    private val _selectedPresetId = MutableStateFlow("")
    val selectedPresetId: StateFlow<String> = _selectedPresetId.asStateFlow()
    // P3.13: voice-agent preset roster from GET /voice-agents (P4 registry;
    // 404-tolerant — empty list pre-P4, dropdown hides). P4.11 builds on this fetch.
    private val _presets = MutableStateFlow<List<VoiceAgentPreset>>(emptyList())
    val presets: StateFlow<List<VoiceAgentPreset>> = _presets.asStateFlow()
```

Create `app/src/main/java/com/aiblackbox/portal/data/voice/VoiceAgentPreset.kt` (same `{"agents":[...]}` contract the Portal preset helper consumes):

```kotlin
package com.aiblackbox.portal.data.voice

import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject

/** One voice-agent preset from GET /voice-agents (server resolves the rest at configure). */
@Serializable
data class VoiceAgentPreset(
    val id: String,
    val name: String,
    val provider: String,   // matches VoiceBackend.id: realtime | gemini-live | grok-live
)

object VoiceAgentPresets {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** Parse {"agents":[...]} — ANY malformed input degrades to emptyList (fresh-box safe). */
    fun parse(body: String): List<VoiceAgentPreset> = try {
        val agents = json.parseToJsonElement(body).jsonObject["agents"] ?: return emptyList()
        json.decodeFromJsonElement(ListSerializer(VoiceAgentPreset.serializer()), agents)
    } catch (e: Exception) {
        emptyList()
    }
}
```

Create the parser test `app/src/test/java/com/aiblackbox/portal/data/VoiceAgentPresetTest.kt`:

```kotlin
package com.aiblackbox.portal.data

import com.aiblackbox.portal.data.voice.VoiceAgentPresets
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceAgentPresetTest {

    @Test
    fun `parses agents array with unknown fields ignored`() {
        val body = """{"agents":[
            {"id":"va-1","name":"Pizza","provider":"grok-live","voice":"Rex","extra":123},
            {"id":"va-2","name":"Calm","provider":"realtime"}
        ]}"""
        val presets = VoiceAgentPresets.parse(body)
        assertEquals(2, presets.size)
        assertEquals("Pizza", presets[0].name)
        assertEquals("grok-live", presets[0].provider)
    }

    @Test
    fun `empty registry parses to empty list`() {
        assertTrue(VoiceAgentPresets.parse("""{"agents":[]}""").isEmpty())
    }

    @Test
    fun `malformed body degrades to empty list not crash`() {
        assertTrue(VoiceAgentPresets.parse("{oops").isEmpty())
        assertTrue(VoiceAgentPresets.parse("").isEmpty())
        assertTrue(VoiceAgentPresets.parse("""{"agents":"nope"}""").isEmpty())
    }
}
```

In `initialize(origin)`, after the P3.12 catalog-fetch job, add the preset fetch (`BlackBoxApi.get` throws on non-2xx — a 404 from a pre-P4 box is caught and leaves the empty list):

```kotlin
            // P3.13: hydrate voice-agent presets from GET /voice-agents
            // ({"agents":[{id,name,provider,...}]}). 404-tolerant pre-P4.
            viewModelScope.launch(Dispatchers.IO) {
                try {
                    _presets.value = VoiceAgentPresets.parse(BlackBoxApi(origin).get("/voice-agents"))
                    android.util.Log.d("VoiceVM", "Presets: ${_presets.value.size}")
                } catch (e: Exception) {
                    android.util.Log.w("VoiceVM", "voice-agents fetch failed (pre-P4 box?): ${e.message}")
                }
            }
```

Add imports: `com.aiblackbox.portal.data.voice.VoiceAgentPreset`, `com.aiblackbox.portal.data.voice.VoiceAgentPresets`.

Replace `setBackend`/`setVoice` (lines 227-228) with:

```kotlin
    private fun persist(key: String, value: String) {
        viewModelScope.launch { store.setString(key, value) }
    }

    private fun defaultVoiceFor(backend: VoiceBackend): String = when (backend) {
        VoiceBackend.GPT_REALTIME -> Constants.DEFAULT_GPT_REALTIME_VOICE
        VoiceBackend.GEMINI_LIVE -> Constants.DEFAULT_GEMINI_LIVE_VOICE
        VoiceBackend.GROK_LIVE -> Constants.DEFAULT_GROK_LIVE_VOICE
    }

    fun setBackend(backend: VoiceBackend) {
        _backend.value = backend
        persist("va_backend", backend.id)
        // Restore this backend's persisted voice, else its canonical default.
        viewModelScope.launch {
            val saved = store.getString("va_voice_${backend.id}").first()
            _voice.value = saved.ifBlank { defaultVoiceFor(backend) }
        }
    }

    fun setVoice(voice: String) {
        _voice.value = voice
        persist("va_voice_${_backend.value.id}", voice)
    }

    fun setRealtimeModel(v: String) { _realtimeModel.value = v; persist("va_model_realtime", v) }
    fun setRealtimeVadType(v: String) { _realtimeVadType.value = v; persist("va_vad_type", v) }
    fun setRealtimeVadEagerness(v: String) { _realtimeVadEagerness.value = v; persist("va_vad_eagerness", v) }
    fun setRealtimeIdleTimeout(v: String) { _realtimeIdleTimeoutText.value = v; persist("va_idle_timeout", v) }
    fun setGeminiModel(v: String) { _geminiModel.value = v; persist("va_model_gemini-live", v) }
    fun setGeminiVadStart(v: String?) { _geminiVadStart.value = v; persist("va_gem_vad_start", v ?: "") }
    fun setGeminiVadEnd(v: String?) { _geminiVadEnd.value = v; persist("va_gem_vad_end", v ?: "") }
    fun setGeminiThinkingLevel(v: String?) { _geminiThinkingLevel.value = v; persist("va_gem_thinking", v ?: "") }
    fun setPreset(id: String) { _selectedPresetId.value = id; persist("va_preset", id) }
```

Extend the `init` block (lines 172-174) with a one-shot load:

```kotlin
    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
        // P3.13: one-shot restore of persisted voice-agent settings.
        viewModelScope.launch {
            val savedBackend = store.getString("va_backend").first()
            VoiceBackend.entries.firstOrNull { it.id == savedBackend }?.let { _backend.value = it }
            val savedVoice = store.getString("va_voice_${_backend.value.id}").first()
            _voice.value = savedVoice.ifBlank { defaultVoiceFor(_backend.value) }
            store.getString("va_model_realtime").first().takeIf { it.isNotBlank() }?.let { _realtimeModel.value = it }
            store.getString("va_vad_type").first().takeIf { it.isNotBlank() }?.let { _realtimeVadType.value = it }
            store.getString("va_vad_eagerness").first().takeIf { it.isNotBlank() }?.let { _realtimeVadEagerness.value = it }
            store.getString("va_idle_timeout").first().takeIf { it.isNotBlank() }?.let { _realtimeIdleTimeoutText.value = it }
            store.getString("va_model_gemini-live").first().takeIf { it.isNotBlank() }?.let { _geminiModel.value = it }
            store.getString("va_gem_vad_start").first().takeIf { it.isNotBlank() }?.let { _geminiVadStart.value = it }
            store.getString("va_gem_vad_end").first().takeIf { it.isNotBlank() }?.let { _geminiVadEnd.value = it }
            store.getString("va_gem_thinking").first().takeIf { it.isNotBlank() }?.let { _geminiThinkingLevel.value = it }
            store.getString("va_preset").first().takeIf { it.isNotBlank() }?.let { _selectedPresetId.value = it }
        }
    }
```

Add import `kotlinx.coroutines.flow.first`.

In the P3.12 catalog-fetch loop, after the `_catalogs.value = ...` line, apply `model_default` (persisted pref wins):

```kotlin
                            cat.modelDefault?.let { def ->
                                when (b) {
                                    VoiceBackend.GPT_REALTIME ->
                                        if (store.getString("va_model_realtime").first().isBlank()) _realtimeModel.value = def
                                    VoiceBackend.GEMINI_LIVE ->
                                        if (store.getString("va_model_gemini-live").first().isBlank()) _geminiModel.value = def
                                    VoiceBackend.GROK_LIVE -> Unit // P3.19
                                }
                            }
```

**Step 2: buildSessionConfig moves into the ViewModel; connect() parameterless**
Replace `fun connect(sessionConfig: VoiceSessionConfig? = null) {` (line 230) with `fun connect() {` and add as first statement `val sessionConfig = buildSessionConfig()`. Add above `connect()`:

```kotlin
    /** P3.13: assemble the per-provider session config from persisted settings. */
    fun buildSessionConfig(): VoiceSessionConfig? {
        val preset = _selectedPresetId.value.takeIf { it.isNotBlank() }
        return when (_backend.value) {
            VoiceBackend.GPT_REALTIME -> VoiceSessionConfig(
                model = _realtimeModel.value.takeIf { it.isNotBlank() },
                vadType = _realtimeVadType.value.takeIf { it.isNotBlank() },
                vadEagerness = if (_realtimeVadType.value == "semantic_vad") _realtimeVadEagerness.value else null,
                idleTimeoutMs = if (_realtimeVadType.value == "server_vad")
                    _realtimeIdleTimeoutText.value.trim().toIntOrNull() else null,
                agentId = preset,
            )
            VoiceBackend.GEMINI_LIVE -> {
                val thinkingAllowed = _geminiModel.value in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS
                VoiceSessionConfig(
                    model = _geminiModel.value.takeIf { it.isNotBlank() },
                    vadStart = _geminiVadStart.value,
                    vadEnd = _geminiVadEnd.value,
                    thinkingLevel = if (thinkingAllowed) _geminiThinkingLevel.value else null,
                    agentId = preset,
                )
            }
            VoiceBackend.GROK_LIVE -> preset?.let { VoiceSessionConfig(agentId = it) } // model/effort: P3.19
        }
    }
```

**Step 3: Rewire the composable**
- Delete the voice-reset `LaunchedEffect(backend)` block (lines 703-712, comment included) — `setBackend` now owns it.
- Replace the remember-var block + composable `buildSessionConfig` (lines 720-757) with:

```kotlin
    // ── Live-models config — hoisted to the ViewModel, DataStore-persisted (P3.13) ──
    val realtimeModel by viewModel.realtimeModel.collectAsState()
    val realtimeVadType by viewModel.realtimeVadType.collectAsState()
    val realtimeVadEagerness by viewModel.realtimeVadEagerness.collectAsState()
    val realtimeIdleTimeoutText by viewModel.realtimeIdleTimeoutText.collectAsState()
    val geminiModel by viewModel.geminiModel.collectAsState()
    val geminiVadStart by viewModel.geminiVadStart.collectAsState()
    val geminiVadEnd by viewModel.geminiVadEnd.collectAsState()
    val geminiThinkingLevel by viewModel.geminiThinkingLevel.collectAsState()
    val selectedPresetId by viewModel.selectedPresetId.collectAsState()
    val presets by viewModel.presets.collectAsState()
```

- In the config-block call sites (lines 846-868) replace every lambda with the matching setter reference: `onModelChange = viewModel::setRealtimeModel`, `onVadTypeChange = viewModel::setRealtimeVadType`, `onVadEagernessChange = viewModel::setRealtimeVadEagerness`, `onIdleTimeoutChange = viewModel::setRealtimeIdleTimeout`, `onModelChange = viewModel::setGeminiModel`, `onVadStartChange = viewModel::setGeminiVadStart`, `onVadEndChange = viewModel::setGeminiVadEnd`, `onThinkingLevelChange = viewModel::setGeminiThinkingLevel` (state values already read from the new `collectAsState` vals).
- Line 912: `viewModel.connect(buildSessionConfig())` → `viewModel.connect()`.
- After the Voice `LabeledDropdown` (line 840) insert:

```kotlin
                    // P3.13: voice-agent preset — hydrated from GET /voice-agents,
                    // filtered to this backend's provider alias; hidden when none
                    // (fresh box / pre-P4 box). Selection rides the agentId connect
                    // param established in P3.12.
                    val presetOpts = presets.filter { it.provider == backend.id }
                    if (presetOpts.isNotEmpty()) {
                        LabeledDropdown(
                            label = "Agent preset",
                            options = listOf("" to "None") + presetOpts.map { it.id to it.name },
                            selectedId = selectedPresetId,
                            enabled = !isConnected,
                            onSelect = viewModel::setPreset,
                        )
                    }
```

**Step 4: Verify**
Run: `--tests "com.aiblackbox.portal.data.VoiceAgentPresetTest"` (PASS, 3 tests), then the full gradle gate
Expected: BUILD SUCCESSFUL, all tests PASS. Also: `grep -n '"Brandon"' "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"` → no matches. And confirm 404 tolerance is exercised on this box today: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9091/voice-agents` → `404` until Phase 4 lands (the dropdown must stay hidden).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceAgentPreset.kt" \
        "$APP/app/src/test/java/com/aiblackbox/portal/data/VoiceAgentPresetTest.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): persist backend/voice/model/vad/thinking in DataStore; preset picker from GET /voice-agents; drop Brandon operator fallback (P3.13)"
```

### Task P3.14: Barge-in — sendInterrupt + tap-on-waveform stops the AI

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt:193 (after sendAudioCommit)
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt (VM after `toggleMic`, waveform call site — originally lines 967-972)
- Test: gradle compile gate (needs a live WS to exercise; no fake-WS harness exists — P3a covers client parse tests)

**Step 1: VoiceClient.sendInterrupt**
After `sendAudioCommit()` (line 193) add:

```kotlin
    /** P3.14 barge-in: ask the server to cancel the in-flight AI response.
     *  All three bridges accept {"type":"interrupt"} (gemini_live_routes.py:645,
     *  realtime response.cancel realtime_routes.py:614, grok equivalent). */
    fun sendInterrupt() {
        val msg = buildJsonObject { put("type", "interrupt") }
        wsClient.send(msg.toString())
        _isAISpeaking.value = false
        aiStoppedSpeakingAt = System.currentTimeMillis()
        android.util.Log.d("VoiceClient", "Sent interrupt")
    }
```

**Step 2: ViewModel.interrupt — hard-stop local playback**
In VoiceScreen.kt after `toggleMic()` (line 274) add:

```kotlin
    /** P3.14 barge-in: flush queued AI audio locally + cancel the response server-side. */
    fun interrupt() {
        try { voiceClient?.sendInterrupt() } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "interrupt: ${e.message}")
        }
        audioPlaybackQueue.clear()
        preBufferAccumulated = 0
        preBufferReady = false
        synchronized(audioTrackLock) {
            try {
                audioTrack?.pause()
                audioTrack?.flush()
                audioTrack?.play()
            } catch (_: Exception) {}
        }
        _amplitude.value = 0f
        _waveSpeaker.value = WaveSpeaker.IDLE
    }
```

**Step 3: Tap-on-waveform**
Replace the `VoiceWaveform(...)` call (lines 967-972 pre-P3.13 numbering) with:

```kotlin
        // ── HD flowing-ribbon waveform — tap to barge-in while the AI speaks (P3.14) ──
        Box(
            modifier = Modifier.fillMaxWidth().clickFeedback {
                if (voiceState == VoiceState.SPEAKING) viewModel.interrupt()
            }
        ) {
            VoiceWaveform(
                amplitude = amplitude,
                speaker = waveSpeaker,
                modifier = Modifier.fillMaxWidth(),
            )
        }
```

**Step 4: Verify**
Run: full gradle gate → BUILD SUCCESSFUL, tests PASS.
`grep -n "interrupt" "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"` → sendInterrupt + interrupt() + waveform tap sites present. Device check deferred to the phase-end Fold validation pass.

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): barge-in — tap waveform sends interrupt + flushes local AI audio (P3.14)"
```

### Task P3.15: Conditional mic auto-mute — hold for Grok only, open-mic for OpenAI/Gemini behind AEC

**Files:**
- Create: app/src/main/java/com/aiblackbox/portal/data/voice/MicMutePolicy.kt
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt:349-363 (mic loop)
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt:99-101 (store backend), 266-276 (echo suppression)
- Test: app/src/test/java/com/aiblackbox/portal/data/voice/MicMutePolicyTest.kt

**Step 1: Write the failing test**

```kotlin
package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MicMutePolicyTest {
    @Test fun `grok holds mic while AI speaks and during post-speech window`() {
        assertTrue(shouldHoldMic(VoiceBackend.GROK_LIVE, isAiSpeaking = true, msSinceAiStopped = 999_999))
        assertTrue(shouldHoldMic(VoiceBackend.GROK_LIVE, isAiSpeaking = false, msSinceAiStopped = 500))
        assertFalse(shouldHoldMic(VoiceBackend.GROK_LIVE, isAiSpeaking = false, msSinceAiStopped = 1300))
    }

    @Test fun `openai and gemini keep the mic open even while AI speaks`() {
        assertFalse(shouldHoldMic(VoiceBackend.GPT_REALTIME, isAiSpeaking = true, msSinceAiStopped = 0))
        assertFalse(shouldHoldMic(VoiceBackend.GEMINI_LIVE, isAiSpeaking = true, msSinceAiStopped = 0))
        assertFalse(shouldHoldMic(VoiceBackend.GEMINI_LIVE, isAiSpeaking = false, msSinceAiStopped = 100))
    }
}
```

**Step 2: Run test to verify it fails**
Run: `cd ".../AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.voice.MicMutePolicyTest"` (full app-root path as in the gate)
Expected: FAIL — `Unresolved reference: shouldHoldMic`

**Step 3: Write minimal implementation**
MicMutePolicy.kt:

```kotlin
package com.aiblackbox.portal.data.voice

/**
 * P3.15: client-side mic gating during AI speech.
 * Grok: echo-prone — hold the mic while the AI speaks + POST_SPEECH_DELAY_MS after.
 * OpenAI/Gemini: leave the mic OPEN so server VAD hears barge-ins — the AEC stack
 * (VOICE_COMMUNICATION source + AcousticEchoCanceler + MODE_IN_COMMUNICATION)
 * suppresses speaker echo. Pure function — unit-tested.
 */
fun shouldHoldMic(
    backend: VoiceBackend,
    isAiSpeaking: Boolean,
    msSinceAiStopped: Long,
    postSpeechDelayMs: Long = VoiceClient.POST_SPEECH_DELAY_MS,
): Boolean {
    if (backend != VoiceBackend.GROK_LIVE) return false
    return isAiSpeaking || msSinceAiStopped < postSpeechDelayMs
}
```

Wire the mic loop — replace VoiceScreen.kt lines 349-363 (from `// Auto-mute while AI is speaking...` through the closing `}` of `if (client != null)`) with:

```kotlin
                            // P3.15: provider-conditional mic hold — Grok holds during AI
                            // speech (echo-prone); OpenAI/Gemini stay open behind AEC so
                            // server VAD hears barge-ins. Do NOT send audio_commit here.
                            val client = voiceClient
                            if (client != null) {
                                val timeSinceStop = System.currentTimeMillis() - client.aiStoppedSpeakingAt
                                if (shouldHoldMic(_backend.value, client.isAISpeaking.value, timeSinceStop)) {
                                    wasSendingAudio = false
                                    continue
                                }
                            }
```

Add import `com.aiblackbox.portal.data.voice.shouldHoldMic`.

Gate the echo-transcript suppression the same way: in VoiceClient.kt `connect()` add `currentBackend = backend` next to `currentOperator = operator` (line 100) plus field `private var currentBackend: VoiceBackend? = null` (near line 78). In `"user_transcript"` (lines 266-276) change the suppression condition to:

```kotlin
                    val isEchoWindow = _isAISpeaking.value || timeSinceAiStopped < POST_SPEECH_DELAY_MS
                    // P3.15: only Grok runs client-muted; elsewhere a transcript during
                    // AI speech is a genuine barge-in, not echo.
                    val suppress = currentBackend == VoiceBackend.GROK_LIVE && isEchoWindow
                    if (data.isNotBlank() && !suppress) {
```
(and the `else if (isEchoWindow)` log branch → `else if (suppress)`).

**Step 4: Run test to verify it passes**
Run: Step 2 command, then the full gate
Expected: PASS; full suite green

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/MicMutePolicy.kt" \
        "$APP/app/src/test/java/com/aiblackbox/portal/data/voice/MicMutePolicyTest.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): mic auto-mute Grok-only; open-mic barge-in for OpenAI/Gemini behind AEC (P3.15)"
```

### Task P3.16: Reconnect/disconnect banner driven by P3a session-health states

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt (VM events collector ~212-219; toast `when` ~680-692; banner after error text ~781-784; status line ~942-946)
- Test: gradle compile gate (Compose UI; VoiceClient state transitions are tested in P3a)

**Step 1: ViewModel — surface Status text**
Add near `_error` (line 128):

```kotlin
    // P3.16: transient server status line ("Connecting to Gemini Live...", etc).
    private val _statusText = MutableStateFlow("")
    val statusText: StateFlow<String> = _statusText.asStateFlow()
```

Replace the events collector body (lines 213-219) with:

```kotlin
            viewModelScope.launch {
                voiceClient?.events?.collect { event ->
                    when (event) {
                        is VoiceEvent.Error -> _error.value = event.message
                        is VoiceEvent.Status -> _statusText.value = event.message
                        else -> Unit
                    }
                }
            }
```

In the state collector (after `_voiceState.value = state`, line 185) add: `if (state == VoiceState.CONNECTED) _statusText.value = ""`.

**Step 2: Composable — toast arm + banners**
At the top of the composable add `val statusText by viewModel.statusText.collectAsState()`.

In the state-toast `when` (lines 681-688): if P3a already added a `VoiceState.RECONNECTING ->` arm, set it to `"Reconnecting..."`; otherwise add that arm.

After the `error?.let { ... }` block (lines 781-784) insert:

```kotlin
        // P3.16: session-health banner — driven by the P3a RECONNECTING state and
        // the terminal disconnected handling (backend no longer fails silently).
        if (voiceState == VoiceState.RECONNECTING) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(Neutral200)
                    .border(1.dp, GlassBorder, RoundedCornerShape(10.dp))
                    .padding(horizontal = 12.dp, vertical = 8.dp)
            ) {
                Text("⟳", color = BbxAccent)
                Spacer(Modifier.width(8.dp))
                Text(
                    "Reconnecting to ${backend.displayName}…",
                    style = MaterialTheme.typography.bodySmall, color = BbxWhite
                )
            }
            Spacer(Modifier.height(8.dp))
        }
        // Terminal server disconnect / reconnect exhaustion land at ERROR (P3a
        // preserves ERROR through the socket teardown); a user hangup lands at
        // DISCONNECTED. Show the session-ended banner for BOTH terminal states,
        // but only when a session actually happened (transcript non-empty).
        if ((voiceState == VoiceState.ERROR || voiceState == VoiceState.DISCONNECTED) &&
            transcript.isNotEmpty()
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(Neutral200)
                    .border(1.dp, GlassBorder, RoundedCornerShape(10.dp))
                    .padding(horizontal = 12.dp, vertical = 8.dp)
            ) {
                Text(
                    "Session ended — tap ▶ to reconnect",
                    style = MaterialTheme.typography.bodySmall, color = BbxRed
                )
            }
            Spacer(Modifier.height(8.dp))
        }
```

In the status Column, after the `"${backend.displayName} · $voice"` Text (line 946) add:

```kotlin
                if (statusText.isNotBlank() &&
                    (voiceState == VoiceState.CONNECTING || voiceState == VoiceState.RECONNECTING)
                ) {
                    Text(statusText, style = MaterialTheme.typography.labelSmall, color = BbxDim)
                }
```

**Step 3: Verify**
Run: full gradle gate
Expected: BUILD SUCCESSFUL, all tests PASS (exhaustive `when`s compile ⇒ RECONNECTING handled everywhere).

**Step 4: (no-op)** — UI task, no unit-test step.

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): reconnecting/session-ended banners + live status line from P3a health events (P3.16)"
```

### Task P3.17: Tool-call chips in the transcript

**Files:**
- Create: app/src/main/java/com/aiblackbox/portal/data/voice/TranscriptMerge.kt
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt (transcript collector ~205-207; events collector from P3.16; connect() clears; LazyColumn items ~1000-1017)
- Test: app/src/test/java/com/aiblackbox/portal/data/voice/TranscriptMergeTest.kt

**Step 1: Write the failing test**

```kotlin
package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TranscriptMergeTest {
    @Test fun `merge interleaves by timestamp`() {
        val server = listOf(
            TranscriptEntry("user", "hi", timestamp = 100),
            TranscriptEntry("assistant", "hello", timestamp = 300),
        )
        val local = listOf(TranscriptEntry("tool_call", "🔧 search_snapshots", timestamp = 200))
        assertEquals(listOf("hi", "🔧 search_snapshots", "hello"),
            mergeTranscript(server, local).map { it.text })
    }

    @Test fun `chip roles are everything except user and assistant`() {
        assertTrue(isChipRole("tool_call"))
        assertTrue(isChipRole("image_task"))
        assertFalse(isChipRole("user"))
        assertFalse(isChipRole("assistant"))
    }

    @Test fun `tool chip labels`() {
        assertEquals("🔧 search_snapshots", toolChipText("tool_call", "search_snapshots", ""))
        assertEquals("✔ search_snapshots — 3 hits", toolChipText("tool_result", "search_snapshots", "3 hits"))
        assertTrue(toolChipText("image_task", "", "sunset over water").contains("image"))
        assertTrue(toolChipText("music_task", "", "").contains("music"))
    }
}
```

**Step 2: Run test to verify it fails**
Run: gate with `--tests "com.aiblackbox.portal.data.voice.TranscriptMergeTest"`
Expected: FAIL — `Unresolved reference: mergeTranscript`

**Step 3: Write minimal implementation**
TranscriptMerge.kt:

```kotlin
package com.aiblackbox.portal.data.voice

/** P3.17: any role beyond user/assistant renders as a compact chip. */
fun isChipRole(role: String): Boolean = role != "user" && role != "assistant"

/** Compact chip label for tool activity. Pure — no android.util.Log. */
fun toolChipText(kind: String, name: String, detail: String): String {
    val suffix = detail.take(80).let { if (it.isBlank()) "" else " — $it" }
    return when (kind) {
        "tool_call" -> "🔧 ${name.ifBlank { "tool" }}$suffix"
        "tool_result" -> "✔ ${name.ifBlank { "tool" }}$suffix"
        "image_task" -> "🖼 image task$suffix"
        "video_task" -> "🎬 video task$suffix"
        "music_task" -> "🎵 music task$suffix"
        else -> "$kind$suffix"
    }
}

/** Merge the server transcript with locally-injected entries (chips, typed text)
 *  in timestamp order. sortedBy is stable: equal stamps keep server-before-local. */
fun mergeTranscript(server: List<TranscriptEntry>, local: List<TranscriptEntry>): List<TranscriptEntry> =
    (server + local).sortedBy { it.timestamp }
```

VoiceScreen.kt ViewModel: add fields near `_transcript` (line 121):

```kotlin
    // P3.17: server transcript + locally-injected entries (tool chips, typed text).
    private val _serverTranscript = MutableStateFlow<List<TranscriptEntry>>(emptyList())
    private val _localEntries = MutableStateFlow<List<TranscriptEntry>>(emptyList())
```

Add helper (near `sendTypedText`-to-be / after `interrupt()`):

```kotlin
    private fun addLocalEntry(entry: TranscriptEntry) {
        _localEntries.value = _localEntries.value + entry
        _transcript.value = mergeTranscript(_serverTranscript.value, _localEntries.value)
    }
```

Replace the transcript collector body (lines 205-207):

```kotlin
            viewModelScope.launch {
                voiceClient?.transcript?.collect {
                    _serverTranscript.value = it
                    _transcript.value = mergeTranscript(it, _localEntries.value)
                }
            }
```

In the P3.16 events `when`, add above `else -> Unit`:

```kotlin
                        is VoiceEvent.Tool -> addLocalEntry(
                            TranscriptEntry(role = event.kind,
                                text = toolChipText(event.kind, event.name, event.detail))
                        )
```

In `connect()`, next to `_transcript.value = emptyList()` add `_serverTranscript.value = emptyList()` and `_localEntries.value = emptyList()`.
Add imports: `com.aiblackbox.portal.data.voice.mergeTranscript`, `toolChipText`, `isChipRole`.

LazyColumn `items(transcript)` block (lines 1000-1017) — wrap the existing bubble in an else and add the chip branch:

```kotlin
            items(transcript) { entry ->
                if (isChipRole(entry.role)) {
                    // P3.17: compact tool-activity chip
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                        Box(
                            modifier = Modifier
                                .clip(RoundedCornerShape(50))
                                .background(Neutral200)
                                .border(1.dp, GlassBorder, RoundedCornerShape(50))
                                .padding(horizontal = 12.dp, vertical = 5.dp)
                        ) {
                            Text(entry.text, style = MaterialTheme.typography.labelSmall, color = BbxDim)
                        }
                    }
                } else {
                    val isUser = entry.role == "user"
                    // ... existing Row/Box bubble unchanged ...
                }
            }
```

**Step 4: Run test to verify it passes**
Run: Step 2 command, then the full gate
Expected: PASS; full suite green

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/TranscriptMerge.kt" \
        "$APP/app/src/test/java/com/aiblackbox/portal/data/voice/TranscriptMergeTest.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): tool_call/result + media-task chips rendered in the voice transcript (P3.17)"
```

### Task P3.18: Text-input row during a voice session

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt (VM after `addLocalEntry`; UI after the waveform Box + Spacer, before provenance)
- Test: gradle compile gate (thin wiring over VoiceClient.sendText:195-198 + P3.17's tested addLocalEntry/merge)

**Step 1: ViewModel**

```kotlin
    /** P3.18: typed text during a voice session — shows as a local user bubble. */
    fun sendTypedText(text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        try {
            voiceClient?.sendText(t)
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "sendText: ${e.message}")
            _error.value = "Send failed: ${e.message}"
            return
        }
        addLocalEntry(TranscriptEntry(role = "user", text = t))
    }
```

**Step 2: UI row** — insert after the waveform Box's trailing `Spacer(Modifier.height(12.dp))` (line 973 pre-P3.14 numbering):

```kotlin
        // P3.18: typed input to the live agent (VoiceClient.sendText).
        if (isConnected) {
            var typedText by remember { mutableStateOf("") }
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                OutlinedTextField(
                    value = typedText,
                    onValueChange = { typedText = it },
                    placeholder = { Text("Type to the agent…", color = Neutral500) },
                    singleLine = true,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                    ),
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                IconButton(onClick = {
                    val t = typedText.trim()
                    if (t.isNotEmpty()) {
                        viewModel.sendTypedText(t)
                        typedText = ""
                    }
                }) {
                    Text("➤", color = BbxAccent, style = MaterialTheme.typography.titleMedium)
                }
            }
            Spacer(Modifier.height(8.dp))
        }
```

**Step 3: Verify**
Run: full gradle gate
Expected: BUILD SUCCESSFUL, all tests PASS

**Step 4: (no-op)** — UI task.

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): text-input row during voice sessions wired to VoiceClient.sendText (P3.18)"
```

### Task P3.19: Grok config UI un-deferred — model + reasoning.effort

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt (after `agentId`)
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt (URL builder, after the `agentId` line from P3.12)
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt (VM flows/setters/load/buildSessionConfig; `VoiceBackend.GROK_LIVE -> Unit` call site; new GrokConfigBlock; stale "out of scope" comments)
- Test: gradle compile gate + existing ConstantsLiveDefaultsTest (efforts list locked in P3.11)

**Step 1: Wire the config field**
VoiceSessionConfig.kt — add after `agentId`:

```kotlin
    /** P3.19: Grok Live reasoning.effort (high|none, grok-voice-think-fast-1.0). */
    val reasoningEffort: String? = null,
```

VoiceClient.kt — in the `sessionConfig?.let { cfg ->` block, after the `agent` line:

```kotlin
                    cfg.reasoningEffort?.let { append("&reasoning_effort=").append(it) }
```

**Step 2: ViewModel state + persistence + defaults**
Add flows next to the gemini ones (P3.13 block):

```kotlin
    private val _grokModel = MutableStateFlow(Constants.LIVE_MODEL_DEFAULTS["grok-live"] ?: "")
    val grokModel: StateFlow<String> = _grokModel.asStateFlow()
    private val _grokReasoningEffort = MutableStateFlow<String?>(null)
    val grokReasoningEffort: StateFlow<String?> = _grokReasoningEffort.asStateFlow()
```

Setters next to `setGeminiThinkingLevel`:

```kotlin
    fun setGrokModel(v: String) { _grokModel.value = v; persist("va_model_grok-live", v) }
    fun setGrokReasoningEffort(v: String?) { _grokReasoningEffort.value = v; persist("va_grok_effort", v ?: "") }
```

In the init one-shot load add:

```kotlin
            store.getString("va_model_grok-live").first().takeIf { it.isNotBlank() }?.let { _grokModel.value = it }
            store.getString("va_grok_effort").first().takeIf { it.isNotBlank() }?.let { _grokReasoningEffort.value = it }
```

In the catalog-default `when` (P3.13), replace `VoiceBackend.GROK_LIVE -> Unit // P3.19` with:

```kotlin
                                    VoiceBackend.GROK_LIVE ->
                                        if (store.getString("va_model_grok-live").first().isBlank()) _grokModel.value = def
```

In `buildSessionConfig()`, replace the GROK branch:

```kotlin
            VoiceBackend.GROK_LIVE -> VoiceSessionConfig(
                model = _grokModel.value.takeIf { it.isNotBlank() },
                reasoningEffort = _grokReasoningEffort.value,
                agentId = preset,
            )
```

**Step 3: UI block**
In the composable add `val grokModel by viewModel.grokModel.collectAsState()` and `val grokReasoningEffort by viewModel.grokReasoningEffort.collectAsState()` next to the other config vals. Replace `VoiceBackend.GROK_LIVE -> Unit // out of scope` (line 869) with:

```kotlin
                        VoiceBackend.GROK_LIVE -> GrokConfigBlock(
                            connected = isConnected,
                            modelOptions = catalogs[VoiceBackend.GROK_LIVE]
                                .modelsOrFallback(Constants.MODEL_CONFIG["grok-live"].orEmpty()),
                            model = grokModel,
                            onModelChange = viewModel::setGrokModel,
                            reasoningEffort = grokReasoningEffort,
                            onReasoningEffortChange = viewModel::setGrokReasoningEffort,
                        )
```

Append after `GeminiConfigBlock` (end of file):

```kotlin
/** P3.19: Grok Live config — model + reasoning.effort (high|none). */
@Composable
private fun GrokConfigBlock(
    connected: Boolean,
    modelOptions: List<Pair<String, String>>,
    model: String,
    onModelChange: (String) -> Unit,
    reasoningEffort: String?,
    onReasoningEffortChange: (String?) -> Unit,
) {
    LabeledDropdown(
        label = "Model",
        options = modelOptions,
        selectedId = model,
        enabled = !connected,  // bound at upstream WS connect time (?model=)
        onSelect = onModelChange,
    )
    val effortOpts: List<Pair<String, String>> =
        listOf("__auto__" to "auto") + Constants.GROK_LIVE_REASONING_EFFORTS.map { it to it }
    LabeledDropdown(
        label = "Reasoning effort",
        options = effortOpts,
        selectedId = reasoningEffort ?: "__auto__",
        enabled = !connected,
        onSelect = { onReasoningEffortChange(if (it == "__auto__") null else it) },
    )
}
```

Remove the stale "Grok Live out of scope" wording from the comment above `voicesForBackend` (originally lines 622-623).

**Step 4: Verify**
Run: full gradle gate
Expected: BUILD SUCCESSFUL, all tests PASS. `grep -n "out of scope" "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"` → no matches.

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && APP="AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && \
git add "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" \
        "$APP/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" && \
git commit -m "feat(android-voice): Grok config un-deferred — model + reasoning.effort dropdowns, persisted (P3.19)"
```

---

### Task P3.20: Gemini Live module — handle full server event set (text_delta, user_transcript_delta, speech_started/stopped)

gemini-live.js's message switch (lines 1015-1162) is missing four events the other two voice modules already handle and which the P1 Gemini rescue (native input/output transcription) makes live: `text_delta`, `user_transcript_delta`, `speech_started`, `speech_stopped`. `status`, `tool_result`, and all task events are ALREADY handled in all three modules — do not re-add them. gpt-realtime.js and grok-live.js already handle the full set (verified 2026-07-11); they need no switch changes.

**Files:**
- Test: Orchestrator/tests/test_portal_voice_parity.py (create)
- Modify: Portal/modules/gemini-live.js:21 (import), :113 (state), :1048-1051 (text_delta), :1120-1131 (user_transcript rewrite + new cases)

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_portal_voice_parity.py`:

```python
"""Portal voice panels <-> voice-bridge contract guard (P3c Portal voice parity).

The three Portal voice modules (gpt-realtime.js / gemini-live.js /
grok-live.js) are hand-bound to the Orchestrator voice-bridge WS event
vocabulary and the P2/P4 status/preset contracts. There is no JS test
infra, so -- mirroring test_portal_embeddings_card_parity.py -- this is a
deliberate source-text test: it asserts each file still references the
real event names / endpoints / connect-message fields.

NOTE: keep these literals greppable in the JS (no string concatenation)
or update this test alongside.
"""
from pathlib import Path

import pytest

PORTAL = Path(__file__).resolve().parents[2] / "Portal"

# Grown task-by-task through Phase 3c. Each entry: relative path -> literals.
FILE_LITERALS = {
    "modules/gemini-live.js": [
        "case 'text_delta':",
        "case 'user_transcript_delta':",
        "case 'speech_started':",
        "case 'speech_stopped':",
        "appendBubble",
    ],
}

CASES = [(f, lit) for f, lits in FILE_LITERALS.items() for lit in lits]


@pytest.mark.parametrize(
    "relpath,literal", CASES, ids=[f"{f}::{lit}" for f, lit in CASES]
)
def test_portal_voice_contract_literals(relpath, literal):
    src = (PORTAL / relpath).read_text(encoding="utf-8")
    assert literal in src, (
        f"Portal/{relpath} no longer references {literal!r} -- the voice "
        "panel has drifted from the voice-bridge contract (P3c parity)."
    )
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 4 failures (`case 'text_delta':`, `case 'user_transcript_delta':`, `case 'speech_started':`, `case 'speech_stopped':` not in gemini-live.js). The `appendBubble` case also fails.

**Step 3: Write minimal implementation**

Four edits to `Portal/modules/gemini-live.js` (line numbers are pre-edit; anchor on exact text):

3a. Line 21 — import `appendBubble`:
```js
// OLD:
import { addBubble } from './chat-bubbles.js';
// NEW:
import { addBubble, appendBubble } from './chat-bubbles.js';
```

3b. After line 113 (`let transcriptBuffer = '';`, keep the blank line after) insert:
```js
/** Accumulated interim user-transcript text for the in-progress utterance */
let userTranscriptBuffer = '';

/** Transient (non-persisted) live user bubble built from interim deltas */
let liveUserBubble = null;
```

3c. After the `transcript_delta` case (lines 1048-1051, ends `break;`) insert:
```js
        case 'text_delta':
            // Text-modality delta (backend may emit for text-only turns) —
            // same accumulation path as transcript_delta.
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;
```

3d. Replace the entire `case 'user_transcript':` block (lines 1120-1131, from `case 'user_transcript':` through its `break;`) with:
```js
        case 'user_transcript_delta':
            // Incremental (interim) user transcription chunk — live word-by-
            // word. Mirrors grok-live.js/gpt-realtime.js: accumulate into a
            // buffer and render a transient (non-persisted) user bubble that
            // updates as chunks arrive; the final user_transcript commits it.
            // Pre-native-transcription backends never emit this — unchanged.
            if (msg.data) {
                userTranscriptBuffer += msg.data;
                if (!liveUserBubble) {
                    // appendBubble adds to the DOM WITHOUT persisting to
                    // history, so interim chunks don't spam localStorage.
                    liveUserBubble = appendBubble('user', userTranscriptBuffer);
                } else {
                    const span = liveUserBubble.querySelector('.bubble-text');
                    if (span) span.textContent = userTranscriptBuffer;
                }
            }
            break;

        case 'user_transcript':
            if (msg.data) {
                console.log('[GEMINI-LIVE] User voice transcript:', msg.data);
                if (liveUserBubble) {
                    // Drop the transient live bubble, then persist via
                    // addBubble so there's exactly one final bubble.
                    liveUserBubble.remove();
                    liveUserBubble = null;
                }
                addBubble('user', msg.data);
                sessionConversation.push({
                    role: 'user',
                    content: msg.data,
                    timestamp: new Date().toISOString(),
                    source: 'voice'
                });
            }
            // Reset for the next utterance (covers delta-less backends too).
            userTranscriptBuffer = '';
            break;

        case 'speech_started':
            console.log('[GEMINI-LIVE] User speech detected');
            break;

        case 'speech_stopped':
            console.log('[GEMINI-LIVE] User speech stopped');
            break;
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/gemini-live.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: no syntax error from node; PASS (5 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/gemini-live.js Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): gemini-live handles text_delta/user_transcript_delta/speech events + voice parity test

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.21: Grok panel — model dropdown hydrated from /grok-live/status models/model_default

P2 added `models: [{id, name}]` + `model_default` to `GET /grok-live/status`. Hydrate a new Grok model dropdown from it (mirror of `fetchRealtimeCatalog`/`populateRealtimeModelDropdown` in gpt-realtime.js:1871-1913) and send `model` in the connect message + reconnect replay.

**Files:**
- Modify: Portal/modules/grok-live.js:32 (SEL), :58 (state), :770-772 (connect reads), :795-800 (connect msg), :1104-1108 (reconnect msg), :1283 (new functions), :1299 (init hook)
- Modify: Portal/modules/voice-agents-modal.js:88-102 (GROK_SELECTORS)
- Modify: Portal/index.html:1935-1945 (grok pane)
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Write the failing test**

In `FILE_LITERALS`, extend the dict (keep existing entries):
```python
    "modules/grok-live.js": [
        "populateGrokModelDropdown",
        "bb_grok_live_catalog",
        "model_default",
        "connectMsg.model",
        "reconnectMsg.model",
    ],
    "index.html": [
        "vaGrokModelSelect",
    ],
    "modules/voice-agents-modal.js": [
        "vaGrokModelSelect",
    ],
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 7 new failures (all P3.21 literals missing).

**Step 3: Write minimal implementation**

3a. `Portal/index.html` — inside the Grok pane (`<div class="va-pane" data-pane="grok-live" style="display:none;">`, line 1935), insert BEFORE the Voice row (line 1936):
```html
          <div class="va-row">
            <label for="vaGrokModelSelect">Model</label>
            <select id="vaGrokModelSelect"></select>
          </div>
```

3b. `Portal/modules/voice-agents-modal.js` — in `GROK_SELECTORS` (line 88), add above `voiceSelect`:
```js
    modelSelect: 'vaGrokModelSelect',
```

3c. `Portal/modules/grok-live.js` — SEL (line 32), add after `voiceSelect: 'grokVoiceSelect',`:
```js
    modelSelect: 'grokModelSelect',
```

3d. State — after `let selectedVoice = 'Ara';` (line 58) insert:
```js
/**
 * Config captured at connect, replayed on reconnect — prevents silent
 * server-default downgrade on network blip (T14 F1 pattern, gpt-realtime.js).
 */
let currentGrokModel = null;
```

3e. `connect()` — after the voice read (lines 770-772) insert:
```js
    // Read model from dropdown (hydrated from /grok-live/status models[] — P2)
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const selectedModel = (modelSelect && modelSelect.value) ? modelSelect.value : undefined;
    currentGrokModel = selectedModel || null;
```

3f. Replace the connect send (lines 795-800):
```js
        // OLD:
        // Send connect message with operator and voice
        ws.send(JSON.stringify({
            type: 'connect',
            operator: operator,
            voice: selectedVoice
        }));
        // NEW:
        // Build connect message; omit undefined fields to keep the wire clean
        const connectMsg = {
            type: 'connect',
            operator: operator,
            voice: selectedVoice
        };
        if (selectedModel) connectMsg.model = selectedModel;
        ws.send(JSON.stringify(connectMsg));
```

3g. Replace the reconnect send in `reconnectToExistingSession()` (lines 1104-1108):
```js
        // OLD:
        ws.send(JSON.stringify({
            type: 'connect',
            operator: currentOperator,
            voice: selectedVoice
        }));
        // NEW:
        // Restore config from module state — prevents silent server-default
        // downgrade on network blip (T14 F1 pattern from gpt-realtime.js).
        const reconnectMsg = {
            type: 'connect',
            operator: currentOperator,
            voice: selectedVoice
        };
        if (currentGrokModel) reconnectMsg.model = currentGrokModel;
        ws.send(JSON.stringify(reconnectMsg));
```

3h. New functions — insert after `checkGrokLiveAvailable()` closes (line 1282, before the `// UI Initialization` banner at 1284):
```js
/**
 * Fetch Grok Live catalog from /grok-live/status with 5min sessionStorage
 * cache. Mirrors gpt-realtime.js fetchRealtimeCatalog() (audit M3 — no
 * JS-side catalog).
 * @returns {Promise<Object|null>} status/catalog object, or null on failure
 */
async function fetchGrokLiveCatalog() {
    const CACHE_TTL_MS = 5 * 60 * 1000;  // 5 minutes
    const cacheKey = 'bb_grok_live_catalog';

    try {
        const cached = JSON.parse(sessionStorage.getItem(cacheKey) || 'null');
        if (cached && Date.now() - cached.ts < CACHE_TTL_MS && cached.data) {
            console.log(`[GROK-LIVE] Catalog cache hit (age ${Math.round((Date.now() - cached.ts) / 1000)}s)`);
            return cached.data;
        }
    } catch (_) { /* corrupted cache — fall through */ }

    try {
        const res = await fetch('/grok-live/status');
        if (res.ok) {
            const data = await res.json();
            try {
                sessionStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), data }));
            } catch (_) { /* sessionStorage full or disabled */ }
            return data;
        }
    } catch (err) {
        console.error('[GROK-LIVE] Failed to fetch catalog:', err);
    }
    return null;
}

/**
 * Populate the Grok model dropdown from catalog models[] (P2 contract:
 * models: [{id, name}], model_default). No-op when fields absent (pre-P2
 * backend): connect() then omits `model` and the backend default applies.
 */
function populateGrokModelDropdown(catalog) {
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    if (!modelSelect || !catalog || !Array.isArray(catalog.models)) return;
    modelSelect.innerHTML = '';
    catalog.models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.name || m.id;
        if (m.id === catalog.model_default) opt.selected = true;
        modelSelect.appendChild(opt);
    });
    console.log(`[GROK-LIVE] Model dropdown populated with ${catalog.models.length} entries, default=${catalog.model_default}`);
}
```

3i. `initGrokLiveUI()` — after `console.log('[GROK-LIVE] Initializing UI...');` (line 1299) insert:
```js
    // Populate model dropdown from /grok-live/status (5min sessionStorage cache)
    fetchGrokLiveCatalog().then(catalog => {
        if (catalog) populateGrokModelDropdown(catalog);
    });
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/grok-live.js && node --input-type=module --check < Portal/modules/voice-agents-modal.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q && curl -s http://localhost:9091/grok-live/status | python3 -c "import json,sys; d=json.load(sys.stdin); print('models:', [m.get('id') for m in d.get('models', [])], 'default:', d.get('model_default'))"`
Expected: PASS (12 passed); curl line prints a non-empty models list with `grok-voice-latest` and a default (P2 contract). If models is empty, STOP — P2's status fields have not landed; do not proceed on a guessed contract.

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/grok-live.js Portal/modules/voice-agents-modal.js Portal/index.html Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): Grok model dropdown hydrated from /grok-live/status; model sent on connect + reconnect replay

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.22: Grok panel — reasoning.effort control

**Files:**
- Modify: Portal/modules/grok-live.js (SEL, state, connect reads/msg, reconnect msg — all anchored on P3.21's post-edit text)
- Modify: Portal/modules/voice-agents-modal.js (GROK_SELECTORS)
- Modify: Portal/index.html (grok pane, after the Voice row)
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Probe the P2 backend param name (pure-probe step, replaces test-first for the wire name)**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && grep -n "reasoning" Orchestrator/routes/grok_live_routes.py | head -20`
Expected: lines showing the connect-message param P2 accepts — expected name `reasoning_effort`. **If the grep shows a different accepted param name, use THAT name everywhere below (JS field + parity literal).** If NO reasoning param exists in the file, STOP — P2's reasoning support has not landed.

**Step 2: Write the failing test**
Append to the `"modules/grok-live.js"` list in `FILE_LITERALS`:
```python
        "connectMsg.reasoning_effort",
        "reconnectMsg.reasoning_effort",
```
Append to `"index.html"` and `"modules/voice-agents-modal.js"` lists:
```python
        "vaGrokReasoningSelect",
```
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 4 new failures.

**Step 3: Write minimal implementation**

3a. `Portal/index.html` — in the Grok pane, insert after the Voice row's closing `</div>` (the row containing `vaGrokVoiceSelect`):
```html
          <div class="va-row">
            <label for="vaGrokReasoningSelect">Reasoning</label>
            <select id="vaGrokReasoningSelect">
              <option value="" selected>Server default</option>
              <option value="high">high (background reasoning)</option>
              <option value="none">none (fastest)</option>
            </select>
          </div>
```

3b. `voice-agents-modal.js` GROK_SELECTORS — add after `modelSelect`:
```js
    reasoningSelect: 'vaGrokReasoningSelect',
```

3c. `grok-live.js` SEL — add after `modelSelect: 'grokModelSelect',`:
```js
    reasoningSelect: 'grokReasoningSelect',
```

3d. State — after `let currentGrokModel = null;` add:
```js
let currentGrokReasoningEffort = null;
```

3e. `connect()` — after the P3.21 model-read block (`currentGrokModel = selectedModel || null;`) insert:
```js
    // reasoning.effort (high|none) — grok-voice-think-fast background reasoning
    const reasoningSelect = SEL.reasoningSelect ? $(SEL.reasoningSelect) : null;
    const reasoningEffort = (reasoningSelect && reasoningSelect.value) ? reasoningSelect.value : undefined;
    currentGrokReasoningEffort = reasoningEffort || null;
```

3f. In the connect message build (after `if (selectedModel) connectMsg.model = selectedModel;`):
```js
        if (reasoningEffort) connectMsg.reasoning_effort = reasoningEffort;
```

3g. In the reconnect message build (after `if (currentGrokModel) reconnectMsg.model = currentGrokModel;`):
```js
        if (currentGrokReasoningEffort) reconnectMsg.reasoning_effort = currentGrokReasoningEffort;
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/grok-live.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: PASS (16 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/grok-live.js Portal/modules/voice-agents-modal.js Portal/index.html Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): Grok reasoning.effort control (high|none) on the voice panel

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.23: GPT Realtime panel — noise_reduction control

**Files:**
- Modify: Portal/modules/gpt-realtime.js:36-37 (SEL), :71 (state), :1189-1205 (connect reads), :1228-1231 (connect msg), :1610-1613 (reconnect msg)
- Modify: Portal/modules/voice-agents-modal.js:49-68 (REALTIME_SELECTORS)
- Modify: Portal/index.html:1876-1879 (realtime pane, after the idle-timeout row)
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Probe the P2 backend param name**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && grep -n "noise_reduction" Orchestrator/routes/realtime_routes.py | head -20`
Expected: lines showing P2's accepted connect param `noise_reduction` (and near_field/far_field handling). **If a different param name appears, use it below.** If no hits, STOP — P2's noise_reduction has not landed.

**Step 2: Write the failing test**
Add a `"modules/gpt-realtime.js"` entry to `FILE_LITERALS`:
```python
    "modules/gpt-realtime.js": [
        "connectMsg.noise_reduction",
        "reconnectMsg.noise_reduction",
    ],
```
Append `"vaRealtimeNoiseSelect"` to both the `"index.html"` and `"modules/voice-agents-modal.js"` lists.
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 4 new failures.

**Step 3: Write minimal implementation**

3a. `Portal/index.html` — after the idle-timeout row closes (line 1879, `</div>` of `vaRealtimeIdleRow`), insert:
```html
          <div class="va-row">
            <label for="vaRealtimeNoiseSelect">Noise reduction</label>
            <select id="vaRealtimeNoiseSelect">
              <option value="" selected>Server default</option>
              <option value="near_field">near_field (headset / handheld)</option>
              <option value="far_field">far_field (laptop / room mic)</option>
              <option value="off">off</option>
            </select>
          </div>
```

3b. `voice-agents-modal.js` REALTIME_SELECTORS — add after `idleRow: 'vaRealtimeIdleRow',`:
```js
    noiseSelect: 'vaRealtimeNoiseSelect',
```

3c. `gpt-realtime.js` SEL — add after `idleRow: null,` (line 37):
```js
    noiseSelect: 'realtimeNoiseSelect',
```

3d. State — after `let currentRealtimeIdleTimeoutMs = null;` (line 71):
```js
let currentRealtimeNoiseReduction = null;
```

3e. `connect()` — after the `idleTimeoutMs` computation (line 1197) insert:
```js
    // noise_reduction (near_field|far_field|off) — gpt-realtime-2.1 feature (P2)
    const noiseSelect = SEL.noiseSelect ? $(SEL.noiseSelect) : null;
    const noiseReduction = (noiseSelect && noiseSelect.value) ? noiseSelect.value : undefined;
```
And after `currentRealtimeIdleTimeoutMs = ...;` (line 1205):
```js
    currentRealtimeNoiseReduction = noiseReduction || null;
```

3f. Connect message — after `if (idleTimeoutMs !== undefined) connectMsg.idle_timeout_ms = idleTimeoutMs;` (line 1231):
```js
        if (noiseReduction) connectMsg.noise_reduction = noiseReduction;
```

3g. Reconnect replay — after `if (currentRealtimeIdleTimeoutMs !== null) reconnectMsg.idle_timeout_ms = currentRealtimeIdleTimeoutMs;` (line 1613):
```js
        if (currentRealtimeNoiseReduction) reconnectMsg.noise_reduction = currentRealtimeNoiseReduction;
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/gpt-realtime.js && node --input-type=module --check < Portal/modules/voice-agents-modal.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: PASS (20 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/gpt-realtime.js Portal/modules/voice-agents-modal.js Portal/index.html Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): GPT Realtime noise_reduction control (near_field/far_field/off)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.24: Shared voice-preset helper module (GET /voice-agents)

Presets ship in P4 AFTER this phase — the helper MUST degrade gracefully when `GET /voice-agents` 404s or errors: the preset row stays hidden and every panel behaves exactly as today. No sessionStorage cache (presets are user-edited; a page reload must show a new preset).

**Files:**
- Create: Portal/modules/voice-presets.js
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Write the failing test**
Add to `FILE_LITERALS`:
```python
    "modules/voice-presets.js": [
        "/voice-agents",
        "filterPresetsByProvider",
        "populatePresetDropdown",
        "None (manual config)",
    ],
```
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 4 errors (FileNotFoundError: voice-presets.js does not exist).

**Step 2: (covered above — probe/verify step)**
Also run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9091/voice-agents`
Expected: `404` today (P4 not landed) or `200` if P4 landed first — BOTH are fine; the helper handles both.

**Step 3: Write minimal implementation**

Create `Portal/modules/voice-presets.js`:
```js
/**
 * voice-presets.js
 * Shared Voice Agent preset support for the three voice panels
 * (gpt-realtime.js / gemini-live.js / grok-live.js).
 *
 * Fetches GET /voice-agents (P4 preset-registry contract:
 * [{id, name, provider, ...}]) and populates a per-panel <select>.
 * The endpoint may not exist yet (P4 ships after P3) — on ANY failure
 * (network error, non-200, empty list after provider filtering) the
 * preset row stays hidden and the panel behaves exactly as before.
 *
 * The selected preset id is sent as `agent` in the WS connect message;
 * backend precedence is explicit params > preset > defaults (design doc
 * workstream 3), so sending both is safe.
 */

/**
 * Filter presets to one panel's provider family.
 * Alias sets are deliberately generous (e.g. 'openai'|'realtime') so the
 * panels tolerate whichever canonical provider string P4 lands.
 * @param {Array} presets - raw /voice-agents list
 * @param {Array<string>} aliases - accepted provider strings
 * @returns {Array} presets whose .provider matches (case-insensitive)
 */
export function filterPresetsByProvider(presets, aliases) {
    if (!Array.isArray(presets)) return [];
    const accept = new Set(aliases.map(a => String(a).toLowerCase()));
    return presets.filter(p => p && p.id && accept.has(String(p.provider || '').toLowerCase()));
}

/**
 * Fetch the preset registry. Fresh fetch on every call — presets are
 * user-edited, so no sessionStorage cache. Returns [] on any failure.
 * @returns {Promise<Array>}
 */
export async function fetchVoicePresets() {
    try {
        const res = await fetch('/voice-agents');
        if (!res.ok) return [];
        const data = await res.json();
        if (Array.isArray(data)) return data;
        if (data && Array.isArray(data.agents)) return data.agents;
        if (data && Array.isArray(data.presets)) return data.presets;
        return [];
    } catch (err) {
        console.log('[VOICE-PRESETS] /voice-agents unavailable (pre-P4 is fine):', err.message);
        return [];
    }
}

/**
 * Populate a preset <select> and unhide its .va-row wrapper.
 * First option = "None (manual config)" (empty value → connect() omits
 * the agent field entirely). No-op on empty preset list: the row stays
 * hidden (rows ship with style="display:none;" in index.html).
 * @param {HTMLSelectElement|null} selectEl
 * @param {Array} presets - already provider-filtered
 */
export function populatePresetDropdown(selectEl, presets) {
    if (!selectEl || !Array.isArray(presets) || presets.length === 0) return;
    selectEl.innerHTML = '';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'None (manual config)';
    none.selected = true;
    selectEl.appendChild(none);
    presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name || p.id;
        selectEl.appendChild(opt);
    });
    const row = selectEl.closest('.va-row');
    if (row) row.style.display = '';
    console.log(`[VOICE-PRESETS] Preset dropdown populated with ${presets.length} presets`);
}
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/voice-presets.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: PASS (24 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/voice-presets.js Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): shared voice-preset helper — GET /voice-agents with graceful pre-P4 degradation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.25: Preset dropdown — GPT Realtime panel

**Files:**
- Modify: Portal/modules/gpt-realtime.js (import, SEL, state, connect, reconnect, initRealtimeUI — anchors below)
- Modify: Portal/modules/voice-agents-modal.js (REALTIME_SELECTORS)
- Modify: Portal/index.html (realtime pane, first row)
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Write the failing test**
Append to the `"modules/gpt-realtime.js"` list:
```python
        "voice-presets.js",
        "connectMsg.agent",
        "reconnectMsg.agent",
```
Append `"vaRealtimePresetSelect"` to both the `"index.html"` and `"modules/voice-agents-modal.js"` lists.
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 5 new failures.

**Step 2: Run test to verify it fails**
(Confirmed by Step 1 run above — 5 failures, existing 24 still pass.)

**Step 3: Write minimal implementation**

3a. `Portal/index.html` — insert as the FIRST row of the realtime pane (immediately after `<div class="va-pane va-pane-active" data-pane="realtime">`, line 1840; hidden until presets exist):
```html
          <div class="va-row" id="vaRealtimePresetRow" style="display:none;">
            <label for="vaRealtimePresetSelect">Preset</label>
            <select id="vaRealtimePresetSelect"></select>
          </div>
```

3b. `voice-agents-modal.js` REALTIME_SELECTORS — add after `modelSelect: 'vaRealtimeModelSelect',`:
```js
    presetSelect: 'vaRealtimePresetSelect',
```

3c. `gpt-realtime.js` — add import after the existing chat-bubbles import at the top of the file:
```js
import { fetchVoicePresets, filterPresetsByProvider, populatePresetDropdown } from './voice-presets.js';
```
(If gpt-realtime.js does not import chat-bubbles, place it with the other module imports at the top — anchor on `from './state-management.js';`.)

3d. SEL — add after `modelSelect: 'realtimeModelSelect',` (line 32):
```js
    presetSelect: 'realtimePresetSelect',
```

3e. State — after `let currentRealtimeNoiseReduction = null;` (added in P3.23):
```js
let currentRealtimePresetId = null;
```

3f. `connect()` — after the noiseReduction read (P3.23) insert:
```js
    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;
    currentRealtimePresetId = presetId || null;
```
And after `if (noiseReduction) connectMsg.noise_reduction = noiseReduction;`:
```js
        if (presetId) connectMsg.agent = presetId;
```

3g. Reconnect replay — after the `reconnectMsg.noise_reduction` line (P3.23):
```js
        if (currentRealtimePresetId) reconnectMsg.agent = currentRealtimePresetId;
```

3h. `initRealtimeUI()` — after the `fetchRealtimeCatalog().then(...)` block (lines 1956-1959 pre-edit):
```js
    // Preset dropdown from GET /voice-agents (P4 registry; row hidden pre-P4)
    fetchVoicePresets().then(presets => {
        const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
        populatePresetDropdown(presetSelect, filterPresetsByProvider(presets, ['openai', 'realtime', 'gpt-realtime']));
    });
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/gpt-realtime.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: PASS (29 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/gpt-realtime.js Portal/modules/voice-agents-modal.js Portal/index.html Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): preset dropdown on GPT Realtime panel (agent= in connect msg)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.26: Preset dropdown — Gemini Live panel

Identical shape to P3.25, applied to gemini-live.js.

**Files:**
- Modify: Portal/modules/gemini-live.js (import :19-21, SEL :31-48, state after :71, connect :863-873, reconnect :1214-1223, initGeminiLiveUI :1542-1548)
- Modify: Portal/modules/voice-agents-modal.js (GEMINI_SELECTORS :70-86)
- Modify: Portal/index.html (gemini pane :1889)
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Write the failing test**
Append to the `"modules/gemini-live.js"` list:
```python
        "voice-presets.js",
        "connectMsg.agent",
        "reconnectMsg.agent",
```
Append `"vaGeminiPresetSelect"` to the `"index.html"` and `"modules/voice-agents-modal.js"` lists.

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 5 new failures.

**Step 3: Write minimal implementation**

3a. `Portal/index.html` — first row of the gemini pane (after `<div class="va-pane" data-pane="gemini-live" style="display:none;">`, line 1889):
```html
          <div class="va-row" id="vaGeminiPresetRow" style="display:none;">
            <label for="vaGeminiPresetSelect">Preset</label>
            <select id="vaGeminiPresetSelect"></select>
          </div>
```

3b. `voice-agents-modal.js` GEMINI_SELECTORS — add after `modelSelect: 'vaGeminiModelSelect',`:
```js
    presetSelect: 'vaGeminiPresetSelect',
```

3c. `gemini-live.js` — add import after line 21 (`import { addBubble, appendBubble } from './chat-bubbles.js';`):
```js
import { fetchVoicePresets, filterPresetsByProvider, populatePresetDropdown } from './voice-presets.js';
```

3d. SEL — add after `modelSelect: 'geminiModelSelect',`:
```js
    presetSelect: 'geminiPresetSelect',
```

3e. State — after `let currentGeminiThinkingLevel = null;` (line 71):
```js
let currentGeminiPresetId = null;
```

3f. `connect()` — after the thinkingLevel computation (line 832) insert:
```js
    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;
```
After `currentGeminiThinkingLevel = thinkingLevel || null;` (line 840):
```js
    currentGeminiPresetId = presetId || null;
```
After `if (thinkingLevel) connectMsg.thinking_level = thinkingLevel;` (line 872):
```js
        if (presetId) connectMsg.agent = presetId;
```

3g. Reconnect replay — after `if (currentGeminiThinkingLevel) reconnectMsg.thinking_level = currentGeminiThinkingLevel;` (line 1222):
```js
        if (currentGeminiPresetId) reconnectMsg.agent = currentGeminiPresetId;
```

3h. `initGeminiLiveUI()` — after the `fetchGeminiLiveCatalog().then(...)` block (lines 1542-1548 pre-edit):
```js
    // Preset dropdown from GET /voice-agents (P4 registry; row hidden pre-P4)
    fetchVoicePresets().then(presets => {
        const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
        populatePresetDropdown(presetSelect, filterPresetsByProvider(presets, ['gemini', 'gemini-live', 'google']));
    });
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/gemini-live.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: PASS (34 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/gemini-live.js Portal/modules/voice-agents-modal.js Portal/index.html Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): preset dropdown on Gemini Live panel (agent= in connect msg)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.27: Preset dropdown — Grok Live panel

**Files:**
- Modify: Portal/modules/grok-live.js (import :19-21, SEL, state, connect, reconnect, initGrokLiveUI — anchored on P3.21/P3.22 post-edit text)
- Modify: Portal/modules/voice-agents-modal.js (GROK_SELECTORS)
- Modify: Portal/index.html (grok pane)
- Test: Orchestrator/tests/test_portal_voice_parity.py

**Step 1: Write the failing test**
Append to the `"modules/grok-live.js"` list:
```python
        "voice-presets.js",
        "connectMsg.agent",
        "reconnectMsg.agent",
```
Append `"vaGrokPresetSelect"` to the `"index.html"` and `"modules/voice-agents-modal.js"` lists.

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 5 new failures.

**Step 3: Write minimal implementation**

3a. `Portal/index.html` — first row of the grok pane (immediately after `<div class="va-pane" data-pane="grok-live" style="display:none;">`, before the P3.21 Model row):
```html
          <div class="va-row" id="vaGrokPresetRow" style="display:none;">
            <label for="vaGrokPresetSelect">Preset</label>
            <select id="vaGrokPresetSelect"></select>
          </div>
```

3b. `voice-agents-modal.js` GROK_SELECTORS — add after `reasoningSelect: 'vaGrokReasoningSelect',`:
```js
    presetSelect: 'vaGrokPresetSelect',
```

3c. `grok-live.js` — add import after the chat-bubbles import (line 21):
```js
import { fetchVoicePresets, filterPresetsByProvider, populatePresetDropdown } from './voice-presets.js';
```

3d. SEL — add after `reasoningSelect: 'grokReasoningSelect',`:
```js
    presetSelect: 'grokPresetSelect',
```

3e. State — after `let currentGrokReasoningEffort = null;`:
```js
let currentGrokPresetId = null;
```

3f. `connect()` — after the P3.22 reasoning-read block:
```js
    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;
    currentGrokPresetId = presetId || null;
```
After `if (reasoningEffort) connectMsg.reasoning_effort = reasoningEffort;` in the connect build:
```js
        if (presetId) connectMsg.agent = presetId;
```

3g. Reconnect replay — after `if (currentGrokReasoningEffort) reconnectMsg.reasoning_effort = currentGrokReasoningEffort;`:
```js
        if (currentGrokPresetId) reconnectMsg.agent = currentGrokPresetId;
```

3h. `initGrokLiveUI()` — after the P3.21 `fetchGrokLiveCatalog().then(...)` block:
```js
    // Preset dropdown from GET /voice-agents (P4 registry; row hidden pre-P4)
    fetchVoicePresets().then(presets => {
        const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
        populatePresetDropdown(presetSelect, filterPresetsByProvider(presets, ['grok', 'grok-live', 'xai']));
    });
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && node --input-type=module --check < Portal/modules/grok-live.js && node --input-type=module --check < Portal/modules/voice-agents-modal.js && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: PASS (39 passed).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/modules/grok-live.js Portal/modules/voice-agents-modal.js Portal/index.html Orchestrator/tests/test_portal_voice_parity.py && git commit -m "feat(portal-voice): preset dropdown on Grok Live panel (agent= in connect msg)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

### Task P3.28: Cache-buster bump + Phase 3c smoke verification

Pure-config task — no unit test; exact verification commands replace the TDD loop. CLAUDE.md mandates a `?v=genuiXX` bump after Portal changes so browsers pick up the edited modules.

**Files:**
- Modify: Portal/index.html:11 and Portal/index.html:21

**Step 1: Bump the cache-buster**
Do NOT assume the current number — other work may have bumped it since this plan was written. Read it first:

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && grep -o "?v=genui[0-9]*" Portal/index.html | sort -u`
Expected: exactly ONE distinct `?v=genuiNN` value, appearing on the CSS `<link>` (line ~11) and the JS `<script>` tag (line ~21). Call that number NN; the new version is NN+1.

Edit BOTH occurrences to `?v=genui<NN+1>` and replace both trailing comments with (substituting the incremented number):
```
<!-- v<NN+1>: P3c Portal voice parity — Gemini event-set completion, Grok model+reasoning controls, GPT noise_reduction, voice-agent preset dropdowns -->
```
Resulting line shape (with `<NN+1>` substituted):
```html
  <link rel="stylesheet" href="/ui/styles/main.css?v=genui<NN+1>"/> <!-- v<NN+1>: P3c Portal voice parity — Gemini event-set completion, Grok model+reasoning controls, GPT noise_reduction, voice-agent preset dropdowns -->
```
```html
  <script type="module" src="/ui/app-modular.js?v=genui<NN+1>"></script> <!-- v<NN+1>: P3c Portal voice parity — Gemini event-set completion, Grok model+reasoning controls, GPT noise_reduction, voice-agent preset dropdowns -->
```

**Step 2: Verify the bump and that no stale version remains**
Run (substitute the real numbers): `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && grep -c "?v=genui<NN+1>" Portal/index.html && grep -c "?v=genui<NN>" Portal/index.html; true`
Expected: `2` then `0`.

**Step 3: Live smoke — served Portal carries the new version and modules parse**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && curl -s http://localhost:9091/ | grep -o "genui[0-9]*" | sort -u && for f in gpt-realtime gemini-live grok-live voice-presets voice-agents-modal; do node --input-type=module --check < Portal/modules/$f.js && echo "$f OK"; done`
Expected: exactly one version printed — `genui<NN+1>` — and five `... OK` lines. (The Orchestrator serves Portal from the working tree — no restart needed for static files.)

**Step 4: Full parity suite green**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py Orchestrator/tests/test_portal_embeddings_card_parity.py -q`
Expected: PASS (39 voice-parity + existing embeddings-parity tests, 0 failures).

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add Portal/index.html && git commit -m "chore(portal): bump cache-buster to genui<NN+1> for P3c voice-parity module changes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014LMB2VyAqiueu7hV8teiHe"
```

---

**Phase 3c notes for the executor (read before P3.20):**
- Line numbers above were verified 2026-07-11 against the pre-P3c working tree; earlier phases (P1/P2, P3.1-P3.19) do not touch these Portal files, but ALWAYS anchor Edit calls on the exact quoted text, not line numbers alone. Within this phase, later tasks reference post-edit text from earlier tasks (called out inline).
- Verified already-handled events (do NOT duplicate): all three modules handle `connected/status/audio_delta/transcript_delta/response_complete/tool_call/tool_result/image_task/video_task/music_task/user_transcript/error/reconnecting/reconnected/pong/disconnected` plus a `default:` unknown-type logger; gpt-realtime.js and grok-live.js additionally handle `text_delta/user_transcript_delta/speech_started/speech_stopped` — only gemini-live.js is missing those four (P3.20).
- The service runs live from this working tree; every task leaves the tree green (`node --input-type=module --check` gates each edited ES module, pytest gates the contract).
- The three modules contain em-dashes in comments — if an Edit call fails on an em-dash block, fall back to `sed -i` line-range edits per the known Edit-tool limitation.

---

## Phase 4 — Voice Agent Presets

Provider-agnostic local "agent builder": a gitignored preset registry (`credentials/voice_agents.json`) modeled line-for-line on `Orchestrator/onboarding/custom_servers.py`, CRUD routes, `?agent=<id>` apply-at-configure on all three voice WS endpoints (precedence: explicit params > preset fields > defaults; preset instructions ride the existing `custom_role` persona-replacement branch), `make_phone_call role="preset:<id>"`, and Portal + Android preset dropdowns. Fresh-box: empty registry returns `[]`, every UI degrades to "no presets". The service runs LIVE from this tree — every task leaves it importable.

Note on line numbers: verified against the working tree on 2026-07-11, but Phases 1–3 land first and WILL drift them. Every step gives a grep anchor — re-locate before editing.

### Task P4.1: Preset registry module (persistence + validation + CRUD)

**Files:**
- Create: Orchestrator/voice_agents/__init__.py
- Create: Orchestrator/voice_agents/registry.py
- Test: Orchestrator/tests/test_voice_agents_registry.py

**Step 0: Verify credentials/ is gitignored (registry file must never be committable)**

Run: `grep -n "^credentials/" /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.gitignore`
Expected: `27:credentials/` (line 28 also has `**/credentials/`). If BOTH are missing, append `credentials/` to .gitignore before proceeding — do not skip this check.

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_agents_registry.py` (pattern: `Orchestrator/tests/test_custom_servers.py` — monkeypatch `REGISTRY_PATH` to tmp_path so tests never touch the real `credentials/voice_agents.json`):

```python
# Orchestrator/tests/test_voice_agents_registry.py
"""Voice-agent preset registry: round-trip, corruption quarantine, atomicity, 0600."""
import json, os, stat
import pytest
from Orchestrator.voice_agents import registry as va


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "voice_agents.json"
    monkeypatch.setattr(va, "REGISTRY_PATH", str(path))
    return path


def test_list_presets_absent_file_returns_empty(reg):
    assert va.list_presets() == []


def test_corrupt_file_quarantined_and_empty(reg):
    reg.write_text("{not json")
    assert va.list_presets() == []            # fail-soft, never raises
    quarantined = [p for p in reg.parent.iterdir() if ".corrupt-" in p.name]
    assert len(quarantined) == 1              # original preserved for forensics


def test_wrong_shape_quarantined(reg):
    reg.write_text(json.dumps({"version": 1, "agents": "nope"}))
    assert va.list_presets() == []
    assert any(".corrupt-" in p.name for p in reg.parent.iterdir())


def test_add_preset_persists_round_trip(reg):
    p = va.add_preset(name="Pizza Bot", provider="grok-live", created_by="Brandon",
                      voice="Rex", instructions="You order pizzas.", greeting="Hi!")
    assert p["id"].startswith("va-")
    assert p["created_at"] and p["updated_at"]
    on_disk = json.loads(reg.read_text())
    assert on_disk["version"] == 1
    assert on_disk["agents"][0]["name"] == "Pizza Bot"
    assert va.get_preset(p["id"])["voice"] == "Rex"


def test_registry_file_is_0600(reg):
    va.add_preset(name="a", provider="realtime")
    assert stat.S_IMODE(os.stat(reg).st_mode) == 0o600


def test_atomic_write_no_tmp_left_behind(reg):
    va.add_preset(name="a", provider="realtime")
    leftovers = [p for p in reg.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_provider_must_be_known(reg):
    with pytest.raises(ValueError):
        va.add_preset(name="x", provider="elevenlabs")


def test_name_unique_case_insensitive(reg):
    va.add_preset(name="Bot", provider="realtime")
    with pytest.raises(ValueError):
        va.add_preset(name="bot", provider="gemini-live")


def test_instructions_size_cap(reg):
    with pytest.raises(ValueError):
        va.add_preset(name="big", provider="realtime",
                      instructions="x" * (va.INSTRUCTIONS_MAX_CHARS + 1))


def test_keyterms_validated(reg):
    with pytest.raises(ValueError):
        va.add_preset(name="k", provider="grok-live", keyterms=["ok", 42])
    with pytest.raises(ValueError):
        va.add_preset(name="k2", provider="grok-live",
                      keyterms=[f"t{i}" for i in range(va.KEYTERMS_MAX + 1)])


def test_update_bumps_updated_at_and_delete(reg):
    p = va.add_preset(name="a", provider="realtime")
    va.update_preset(p["id"], {"voice": "marin"})
    got = va.get_preset(p["id"])
    assert got["voice"] == "marin"
    assert got["updated_at"] >= got["created_at"]
    with pytest.raises(ValueError):
        va.update_preset(p["id"], {"id": "va-hax"})   # unpatchable field
    with pytest.raises(KeyError):
        va.update_preset("va-nope", {"voice": "x"})
    va.delete_preset(p["id"])
    assert va.list_presets() == []
    with pytest.raises(KeyError):
        va.delete_preset(p["id"])
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agents_registry.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'Orchestrator.voice_agents'`

**Step 3: Write minimal implementation**

Create `Orchestrator/voice_agents/__init__.py`:

```python
"""Voice-agent presets — provider-agnostic local 'agent builder' (P4)."""
```

Create `Orchestrator/voice_agents/registry.py` (persistence layer copied line-for-line from `Orchestrator/onboarding/custom_servers.py` — same quarantine/atomic-write/0600/fresh-read discipline):

```python
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
```

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agents_registry.py -x -q`
Expected: PASS (12 passed)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/voice_agents/__init__.py Orchestrator/voice_agents/registry.py Orchestrator/tests/test_voice_agents_registry.py
git commit -m "feat(voice-agents): preset registry — fresh-read, atomic, 0600, corrupt-quarantine (custom_servers conventions)"
```

### Task P4.2: Preset resolution + precedence helpers

**Files:**
- Modify: Orchestrator/voice_agents/registry.py (append at end of file)
- Test: Orchestrator/tests/test_voice_agents_resolution.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_voice_agents_resolution.py
"""resolve_preset / merge_connect_params / resolve_phone_role.

Precedence contract (design doc W3): explicit params > preset fields > defaults.
"""
import pytest
from Orchestrator.voice_agents import registry as va


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))
    return tmp_path


def test_resolve_preset_by_id(reg):
    p = va.add_preset(name="Bot", provider="realtime", voice="marin")
    assert va.resolve_preset(p["id"])["voice"] == "marin"


def test_resolve_preset_unknown_returns_none(reg):
    assert va.resolve_preset("va-nope") is None
    assert va.resolve_preset("") is None
    assert va.resolve_preset(None) is None


def test_resolve_preset_provider_mismatch_returns_none(reg):
    p = va.add_preset(name="Bot", provider="grok-live")
    assert va.resolve_preset(p["id"], provider="realtime") is None
    assert va.resolve_preset(p["id"], provider="grok-live") is not None


def test_merge_explicit_wins_over_preset():
    preset = {"model": "p-model", "voice": "p-voice", "instructions": "p-inst"}
    merged = va.merge_connect_params(
        {"model": "x-model", "voice": "", "instructions": None}, preset)
    assert merged["model"] == "x-model"        # explicit wins
    assert merged["voice"] == "p-voice"        # "" falls through to preset
    assert merged["instructions"] == "p-inst"  # None falls through to preset


def test_merge_no_preset_yields_explicit_or_none():
    merged = va.merge_connect_params({"voice": "ash"}, None)
    assert merged["voice"] == "ash"
    assert merged["model"] is None             # defaults stay with the route


def test_merge_empty_preset_values_yield_none():
    merged = va.merge_connect_params({}, {"model": "", "keyterms": []})
    assert merged["model"] is None and merged["keyterms"] is None


def test_resolve_phone_role_passthrough_when_not_preset(reg):
    assert va.resolve_phone_role("Be a pirate", "openai_realtime", "hi") == \
        ("Be a pirate", "openai_realtime", "hi")


def test_resolve_phone_role_substitutes_preset(reg):
    p = va.add_preset(name="Pizza", provider="grok-live",
                      instructions="You order pizzas.", greeting="Hello!")
    role, backend, greeting = va.resolve_phone_role(f"preset:{p['id']}", "openai_realtime", "")
    assert role == "You order pizzas."
    assert backend == "grok_live"              # preset provider drives the backend
    assert greeting == "Hello!"                # preset fills empty greeting
    # explicit greeting wins over the preset's
    assert va.resolve_phone_role(f"preset:{p['id']}", "x", "custom")[2] == "custom"


def test_resolve_phone_role_unknown_id_raises(reg):
    with pytest.raises(KeyError):
        va.resolve_phone_role("preset:va-nope", "openai_realtime", "")
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agents_resolution.py -x -q`
Expected: FAIL with `AttributeError: module 'Orchestrator.voice_agents.registry' has no attribute 'resolve_preset'`

**Step 3: Write minimal implementation**

Append to `Orchestrator/voice_agents/registry.py`:

```python
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
```

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agents_resolution.py Orchestrator/tests/test_voice_agents_registry.py -q`
Expected: PASS (21 passed)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/voice_agents/registry.py Orchestrator/tests/test_voice_agents_resolution.py
git commit -m "feat(voice-agents): resolve_preset + explicit>preset>default merge + phone-role resolution"
```

### Task P4.3: CRUD routes — GET/POST /voice-agents, PATCH/DELETE /voice-agents/{id}

**Files:**
- Create: Orchestrator/routes/voice_agent_routes.py
- Test: Orchestrator/tests/test_voice_agent_routes.py

**Step 1: Write the failing test**

Pattern: `Orchestrator/tests/test_custom_servers_routes.py` (TestClient over a minimal FastAPI app mounting the router — do NOT import the full Orchestrator app):

```python
# Orchestrator/tests/test_voice_agent_routes.py
"""/voice-agents CRUD via TestClient over a minimal app (test_custom_servers_routes pattern)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.voice_agents import registry as va
from Orchestrator.routes import voice_agent_routes as var


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(var.router)
    return TestClient(app)


def test_empty_registry_returns_empty_list(client, tmp_registry):
    r = client.get("/voice-agents")
    assert r.status_code == 200
    assert r.json() == {"agents": []}          # fresh-box gate


def test_crud_roundtrip(client, tmp_registry):
    r = client.post("/voice-agents", json={
        "name": "Pizza Bot", "provider": "grok-live", "voice": "Rex",
        "instructions": "You order pizzas.", "greeting": "Hi!",
        "created_by": "Brandon"})
    assert r.status_code == 200
    agent = r.json()["agent"]
    aid = agent["id"]
    assert agent["provider"] == "grok-live"

    listing = client.get("/voice-agents").json()["agents"]
    assert [a["id"] for a in listing] == [aid]

    # provider filter
    assert client.get("/voice-agents?provider=realtime").json()["agents"] == []
    assert len(client.get("/voice-agents?provider=grok-live").json()["agents"]) == 1

    r = client.patch(f"/voice-agents/{aid}", json={"greeting": "Yo!"})
    assert r.status_code == 200
    assert r.json()["agent"]["greeting"] == "Yo!"

    assert client.delete(f"/voice-agents/{aid}").status_code == 200
    assert client.get("/voice-agents").json()["agents"] == []


def test_post_unknown_provider_400(client, tmp_registry):
    r = client.post("/voice-agents", json={"name": "x", "provider": "alexa"})
    assert r.status_code == 400


def test_post_model_validated_against_catalog(client, tmp_registry):
    # realtime + gemini-live have config catalogs — a junk model must 400.
    r = client.post("/voice-agents", json={
        "name": "x", "provider": "realtime", "model": "gpt-6-realtime-fake"})
    assert r.status_code == 400
    assert "model" in r.json()["detail"].lower()
    # a real catalog id is accepted
    from Orchestrator.config import OPENAI_REALTIME_MODELS
    good = OPENAI_REALTIME_MODELS[0]["id"]
    assert client.post("/voice-agents", json={
        "name": "y", "provider": "realtime", "model": good}).status_code == 200


def test_post_oversized_instructions_400(client, tmp_registry):
    r = client.post("/voice-agents", json={
        "name": "big", "provider": "realtime",
        "instructions": "x" * (va.INSTRUCTIONS_MAX_CHARS + 1)})
    assert r.status_code == 400


def test_patch_unknown_id_404_and_delete_unknown_404(client, tmp_registry):
    assert client.patch("/voice-agents/va-nope", json={"name": "x"}).status_code == 404
    assert client.delete("/voice-agents/va-nope").status_code == 404


def test_patch_model_revalidated_against_stored_provider(client, tmp_registry):
    aid = client.post("/voice-agents", json={
        "name": "x", "provider": "realtime"}).json()["agent"]["id"]
    r = client.patch(f"/voice-agents/{aid}", json={"model": "not-a-model"})
    assert r.status_code == 400
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agent_routes.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'Orchestrator.routes.voice_agent_routes'`

**Step 3: Write minimal implementation**

Create `Orchestrator/routes/voice_agent_routes.py`:

```python
"""voice_agent_routes.py — CRUD for voice-agent presets (P4).

APIRouter module (onboarding_routes/credentials_routes precedent) so tests can
mount it on a minimal FastAPI app; registered in app.py via include_router.
Registry semantics live in Orchestrator/voice_agents/registry.py — this layer
adds only HTTP mapping + provider-catalog model validation.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator.voice_agents import registry

logger = logging.getLogger(__name__)
router = APIRouter()

# Provider -> config catalog attr. getattr at REQUEST time (not import) so a
# catalog added later (e.g. GROK_LIVE_MODELS in Phase 2) is picked up without
# touching this module. Missing/empty catalog -> model validation skipped.
_CATALOG_ATTRS = {
    "realtime": "OPENAI_REALTIME_MODELS",
    "gemini-live": "GEMINI_LIVE_MODELS",
    "grok-live": "GROK_LIVE_MODELS",
}


def _validate_model(provider: str, model: Optional[str]) -> None:
    if not model:
        return
    from Orchestrator import config
    catalog = getattr(config, _CATALOG_ATTRS.get(provider, ""), None)
    if not catalog:
        return  # no live catalog for this provider (yet) — accept as-is
    known = {m["id"] for m in catalog if isinstance(m, dict) and m.get("id")}
    if model not in known:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown {provider} model {model!r}. Known models: {sorted(known)}")


class PresetCreate(BaseModel):
    name: str
    provider: str
    model: str = ""
    voice: str = ""
    instructions: str = ""
    tool_group_override: str = ""
    greeting: str = ""
    language: str = ""
    keyterms: List[str] = []
    created_by: str = ""


class PresetPatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    voice: Optional[str] = None
    instructions: Optional[str] = None
    tool_group_override: Optional[str] = None
    greeting: Optional[str] = None
    language: Optional[str] = None
    keyterms: Optional[List[str]] = None


@router.get("/voice-agents")
def list_voice_agents(provider: Optional[str] = None) -> dict:
    """All presets (no secrets stored — full records are safe to return)."""
    return {"agents": registry.list_presets(provider=provider)}


@router.post("/voice-agents")
def create_voice_agent(req: PresetCreate) -> dict:
    if req.provider not in registry.PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"provider must be one of {registry.PROVIDERS}")
    _validate_model(req.provider, req.model)
    try:
        agent = registry.add_preset(
            name=req.name, provider=req.provider, created_by=req.created_by,
            model=req.model, voice=req.voice, instructions=req.instructions,
            tool_group_override=req.tool_group_override, greeting=req.greeting,
            language=req.language, keyterms=req.keyterms)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("voice-agents add: id=%s name=%s provider=%s",
                agent["id"], agent["name"], agent["provider"])
    return {"agent": agent}


@router.patch("/voice-agents/{preset_id}")
def patch_voice_agent(preset_id: str, req: PresetPatch) -> dict:
    patch = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if "model" in patch or "provider" in patch:
        current = registry.get_preset(preset_id)
        if current is None:
            raise HTTPException(status_code=404,
                                detail=f"No voice agent preset {preset_id!r}")
        provider = patch.get("provider", current.get("provider"))
        _validate_model(provider, patch.get("model", current.get("model")))
    try:
        agent = registry.update_preset(preset_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("voice-agents patch: id=%s fields=%s", preset_id, sorted(patch))
    return {"agent": agent}


@router.delete("/voice-agents/{preset_id}")
def delete_voice_agent(preset_id: str) -> dict:
    try:
        registry.delete_preset(preset_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("voice-agents delete: id=%s", preset_id)
    return {"ok": True}
```

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agent_routes.py -q`
Expected: PASS (7 passed)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/voice_agent_routes.py Orchestrator/tests/test_voice_agent_routes.py
git commit -m "feat(voice-agents): CRUD routes GET/POST /voice-agents, PATCH/DELETE /voice-agents/{id} with catalog model validation"
```

### Task P4.4: Register routes in app.py + live smoke

Pure-wiring task — verification commands instead of TDD.

**Files:**
- Modify: Orchestrator/app.py:137-138 (append after the mcp_router block)

**Step 1: Add the router registration**

In `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/app.py`, directly after (anchor: `grep -n "mcp_router" Orchestrator/app.py`):

```python
from Orchestrator.routes.mcp_routes import router as mcp_router
app.include_router(mcp_router)
```

insert:

```python
from Orchestrator.routes.voice_agent_routes import router as voice_agent_router
app.include_router(voice_agent_router)
```

(Keep it BEFORE the `FirstRunMiddleware` block at line ~140 — same section as every other include_router.)

**Step 2: Import gate (tree must stay green — service runs live from this tree)**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -c "from Orchestrator.routes.voice_agent_routes import router; print(len(router.routes), 'routes')"`
Expected: `4 routes`

**Step 3: Restart the service (pre-authorized) and wait for warm-up**

Run: `sudo systemctl restart blackbox.service && sleep 75 && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9091/voice-agents`
Expected: `200`

**Step 4: Live CRUD smoke against the running service**

```bash
curl -s http://localhost:9091/voice-agents
# Expected: {"agents":[]}  (fresh registry)
AID=$(curl -s -X POST http://localhost:9091/voice-agents -H "Content-Type: application/json" \
  -d '{"name":"Smoke Test","provider":"realtime","voice":"marin","instructions":"Smoke."}' | python3 -c "import sys,json;print(json.load(sys.stdin)['agent']['id'])")
curl -s -X PATCH http://localhost:9091/voice-agents/$AID -H "Content-Type: application/json" -d '{"greeting":"hi"}' | grep -o '"greeting": *"hi"'
curl -s -X DELETE http://localhost:9091/voice-agents/$AID
# Expected: {"ok":true}; then GET returns {"agents":[]} again
ls -la credentials/voice_agents.json   # Expected: -rw------- (0600)
git status --porcelain credentials/ | grep voice_agents ; echo "exit=$?"
# Expected: no output, exit=1 (gitignored)
```

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/app.py
git commit -m "feat(voice-agents): register /voice-agents router in app.py"
```

### Task P4.5: Apply-at-configure — OpenAI realtime WS accepts ?agent= / connect.agent

**Files:**
- Modify: Orchestrator/routes/realtime_routes.py:300-310 (configure signature), :540 (tools line), :1355-1374 (URL params), :1434-1445 (connect merge), :1456-1466 (configure call)
- Test: Orchestrator/tests/test_voice_preset_configure.py

**Step 1: Write the failing test**

Tests the two behaviors the preset rides on: custom_role REPLACES the persona (existing branch at realtime_routes.py:367-384 — reused, not a third path) and the new `tool_group_override` kwarg swaps the tool group at configure time:

```python
# Orchestrator/tests/test_voice_preset_configure.py
"""configure_openai_session: custom_role persona replacement + tool_group_override (P4)."""
import asyncio, json
import pytest

from Orchestrator.models import RealtimeSession
from Orchestrator.routes import realtime_routes as rt


class FakeWS:
    def __init__(self):
        self.sent = []
    async def send(self, payload):
        self.sent.append(json.loads(payload))


@pytest.fixture
def quiet_context(monkeypatch):
    # Skip the heavy fossil-context build — not under test here.
    monkeypatch.setattr(rt, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))


def _configure(**kwargs):
    session = RealtimeSession(session_id="t-p4")
    session.openai_ws = FakeWS()
    asyncio.run(rt.configure_openai_session(session, "system", "ash", **kwargs))
    return session.openai_ws.sent[0]


def test_custom_role_replaces_persona(quiet_context):
    cfg = _configure(custom_role="You are Pepper the pizza-order bot.")
    assert cfg["type"] == "session.update"
    instructions = cfg["session"]["instructions"]
    assert instructions.startswith("You are Pepper the pizza-order bot.")
    assert "IDENTITY:\nYou are the voice interface" not in instructions


def test_tool_group_override_swaps_tool_group(quiet_context):
    cfg = _configure(tool_group_override="gemini_live")
    sent = [t["name"] for t in cfg["session"]["tools"]]
    expected = [t["name"] for t in rt.get_openai_realtime_tools("gemini_live")]
    assert sent == expected


def test_no_override_keeps_default_tools(quiet_context):
    # P1.28 deleted the frozen REALTIME_TOOLS constant (tools are read at
    # configure time) — compare against the live group read, same as the route.
    cfg = _configure()
    assert [t["name"] for t in cfg["session"]["tools"]] == \
        [t["name"] for t in rt.get_openai_realtime_tools("realtime")]
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_preset_configure.py -x -q`
Expected: FAIL with `TypeError: configure_openai_session() got an unexpected keyword argument 'tool_group_override'`

**Step 3: Write minimal implementation**

3a. Top of `realtime_routes.py` (with the other local imports):

```python
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
```

3b. `configure_openai_session` signature (anchor `def configure_openai_session`, currently :300-310) — add one kwarg after `create_response` (Optional=None keeps phone/bridge.py positional call sites unchanged, audit C2):

```python
    create_response: Optional[bool] = None,
    tool_group_override: Optional[str] = None,
```

3c. Tools line — P1.28 (always executed before this phase) replaced the frozen `REALTIME_TOOLS` constant with a configure-time read. Anchor on the post-P1.28 line `"tools": get_openai_realtime_tools("realtime"),` and apply the override to the group argument:

```python
            "tools": get_openai_realtime_tools(tool_group_override or "realtime"),
```

3d. URL params block (anchor `url_vad_eagerness = websocket.query_params.get`, currently :1359) — add after it:

```python
    url_agent = websocket.query_params.get("agent")
```

3e. Connect branch (anchor `# Merge rule: JSON connect message wins`, currently :1432-1445) — replace the five lines

```python
                operator = data.get("operator", url_operator or "")
                voice = data.get("voice", url_voice or "ash")  # Default to ash if not specified
                greeting = data.get("greeting", "")
                role = data.get("role", "")
```

with:

```python
                # Voice-agent preset (P4): ?agent=<id> or "agent" in the JSON
                # connect message. Precedence: explicit params > preset fields
                # > defaults. Unknown/mismatched preset -> loud client warning,
                # session continues without it (fresh-box degradation).
                agent_id = data.get("agent", url_agent)
                preset = resolve_preset(agent_id, provider="realtime") if agent_id else None
                if agent_id and preset is None:
                    await _safe_ws_send(websocket, {
                        "type": "warning",
                        "data": f"Voice agent preset {agent_id!r} not found for provider 'realtime' — continuing without preset"
                    })
                merged = merge_connect_params({
                    "model": data.get("model", url_model),
                    "voice": data.get("voice", url_voice),
                    "greeting": data.get("greeting", ""),
                    "instructions": data.get("role", ""),
                }, preset)
                operator = data.get("operator", url_operator or "")
                voice = merged["voice"] or "ash"          # route default unchanged
                greeting = merged["greeting"] or ""
                role = merged["instructions"] or ""       # preset instructions ride the custom_role branch
                tool_group_override = merged["tool_group_override"]
```

then change the existing `model = data.get("model", url_model)` line (currently :1440) to:

```python
                model = merged["model"]
```

3f. Configure call (anchor `custom_role=role,` inside the connect branch, currently :1456-1466) — add:

```python
                        tool_group_override=tool_group_override,
```

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_preset_configure.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes; print('import ok')"`
Expected: PASS (3 passed) then `import ok`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_voice_preset_configure.py
git commit -m "feat(voice-agents): /ws/realtime accepts ?agent=/connect.agent — preset merge + tool_group_override at configure"
```

### Task P4.6: Apply-at-configure — Gemini Live WS

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py:215-225 (configure signature), :442 (tools line), :1542-1547 (URL params), :1603-1638 (connect merge + configure call)
- Test: Orchestrator/tests/test_voice_preset_configure.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_preset_configure.py`:

```python
# ---------------------------------------------------------------- gemini live

from Orchestrator.models import GeminiLiveSession
from Orchestrator.routes import gemini_live_routes as gm


@pytest.fixture
def quiet_gemini_context(monkeypatch):
    monkeypatch.setattr(gm, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))


def test_gemini_custom_role_and_tool_group_override(quiet_gemini_context):
    session = GeminiLiveSession(session_id="t-p4-gm")
    session.gemini_ws = FakeWS()
    asyncio.run(gm.configure_gemini_session(
        session, "system", "Orus",
        custom_role="You are Pepper the pizza-order bot.",
        tool_group_override="realtime"))
    cfg = session.gemini_ws.sent[0]
    assert "setup" in cfg
    assert "You are Pepper the pizza-order bot." in json.dumps(cfg)
    assert cfg["setup"]["tools"] == gm.get_gemini_live_tools("realtime")
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_preset_configure.py -x -q -k gemini`
Expected: FAIL with `TypeError: configure_gemini_session() got an unexpected keyword argument 'tool_group_override'`

**Step 3: Write minimal implementation**

3a. Top of `gemini_live_routes.py` (local imports):

```python
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
```

3b. `configure_gemini_session` signature (anchor `def configure_gemini_session`, currently :215-225) — add after `thinking_level`:

```python
    tool_group_override: Optional[str] = None,
```

3c. Tools line — P1.3 (always executed before this phase) replaced the frozen `GEMINI_LIVE_TOOLS` constant with a configure-time read. Anchor on the post-P1.3 line `"tools": get_gemini_live_tools("gemini_live"),` and apply the override to the group argument:

```python
        "tools": get_gemini_live_tools(tool_group_override or "gemini_live"),
```

(`get_gemini_live_tools` is already imported at module level — see :98.)

3d. URL params (anchor `url_thinking_level = websocket.query_params.get`, currently :1547) — add after it:

```python
    url_agent = websocket.query_params.get("agent")
```

3e. Connect branch (anchor `operator = data.get("operator", url_operator or "")` at :1608) — replace :1608-1617 (`operator`/`voice`/`greeting`/`role`/`model` assignments; keep the `vad_sensitivity_*`/`thinking_level` lines) with:

```python
                # Voice-agent preset (P4) — precedence: explicit > preset > defaults.
                agent_id = data.get("agent", url_agent)
                preset = resolve_preset(agent_id, provider="gemini-live") if agent_id else None
                if agent_id and preset is None:
                    await _safe_ws_send(websocket, {
                        "type": "warning",
                        "data": f"Voice agent preset {agent_id!r} not found for provider 'gemini-live' — continuing without preset"
                    })
                merged = merge_connect_params({
                    "model": data.get("model", url_model),
                    "voice": data.get("voice", url_voice),
                    "greeting": data.get("greeting", ""),
                    "instructions": data.get("role", ""),
                }, preset)
                operator = data.get("operator", url_operator or "")
                voice = merged["voice"] or GEMINI_LIVE_DEFAULT_VOICE
                greeting = merged["greeting"] or ""
                role = merged["instructions"] or ""
                model = merged["model"]
                tool_group_override = merged["tool_group_override"]
```

3f. Configure call (anchor `thinking_level=thinking_level,` at :1637) — add:

```python
                        tool_group_override=tool_group_override,
```

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_preset_configure.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.routes.gemini_live_routes; print('import ok')"`
Expected: PASS (4 passed) then `import ok`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_voice_preset_configure.py
git commit -m "feat(voice-agents): /ws/gemini-live accepts ?agent=/connect.agent — preset merge + tool_group_override"
```

### Task P4.7: Apply-at-configure — Grok Live WS

**Files:**
- Modify: Orchestrator/routes/grok_live_routes.py:282 (configure signature), :461 (tools line), :1291-1293 (URL param), :1349-1365 (connect merge + configure call)
- Test: Orchestrator/tests/test_voice_preset_configure.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_preset_configure.py`:

```python
# ------------------------------------------------------------------ grok live

from Orchestrator.models import GrokLiveSession
from Orchestrator.routes import grok_live_routes as gk


@pytest.fixture
def quiet_grok_context(monkeypatch):
    monkeypatch.setattr(gk, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))


def test_grok_custom_role_and_tool_group_override(quiet_grok_context):
    session = GrokLiveSession(session_id="t-p4-gk")
    session.grok_ws = FakeWS()
    asyncio.run(gk.configure_grok_session(
        session, "system", "Rex",
        custom_role="You are Pepper the pizza-order bot.",
        tool_group_override="realtime"))
    cfg = session.grok_ws.sent[0]
    assert cfg["type"] == "session.update"
    assert "You are Pepper the pizza-order bot." in json.dumps(cfg)
    sent = [t["name"] for t in cfg["session"]["tools"]]
    assert sent == [t["name"] for t in gk.get_openai_realtime_tools("realtime")]
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_preset_configure.py -x -q -k grok`
Expected: FAIL with `TypeError: configure_grok_session() got an unexpected keyword argument 'tool_group_override'`

**Step 3: Write minimal implementation**

3a. Top of `grok_live_routes.py` (local imports):

```python
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
```

3b. Signature (anchor `async def configure_grok_session`, currently :282):

```python
async def configure_grok_session(session: 'GrokLiveSession', operator: str, voice: str = "Ara",
                                 custom_role: str = "", tool_group_override: Optional[str] = None):
```

3c. Tools line — P1.28 (always executed before this phase) replaced the frozen `GROK_LIVE_TOOLS` constant with a configure-time read. Insert `tools = get_openai_realtime_tools(tool_group_override or "grok_live")` above the `config_event = {` block (replacing the post-P1.28 inline read) and use it:

```python
            "tools": tools,
```

(also update the two tool-count debug prints just below to `len(tools)`).

3d. WS endpoint — after `await websocket.accept()` / the accepted print (currently :1292-1293), add:

```python
    # P4: Grok route reads only the preset id from the URL (other Grok URL
    # params are Phase 3 scope).
    url_agent = websocket.query_params.get("agent")
```

3e. Connect branch — replace :1351-1354 (`operator`/`voice`/`greeting`/`role` assignments) with:

```python
                # Voice-agent preset (P4) — precedence: explicit > preset > defaults.
                agent_id = data.get("agent", url_agent)
                preset = resolve_preset(agent_id, provider="grok-live") if agent_id else None
                if agent_id and preset is None:
                    await _safe_ws_send(websocket, {
                        "type": "warning",
                        "data": f"Voice agent preset {agent_id!r} not found for provider 'grok-live' — continuing without preset"
                    })
                merged = merge_connect_params({
                    "model": data.get("model"),
                    "voice": data.get("voice"),
                    "greeting": data.get("greeting", ""),
                    "instructions": data.get("role", ""),
                }, preset)
                operator = data.get("operator", "")
                voice = merged["voice"] or GROK_LIVE_DEFAULT_VOICE
                greeting = merged["greeting"] or ""
                role = merged["instructions"] or ""
                tool_group_override = merged["tool_group_override"]
```

3f. Configure call (anchor `await configure_grok_session(session, operator, voice, custom_role=role`, currently :1365) — thread the preset's Grok-specific fields too. `keyterms=`/`language_hint=` exist on `configure_grok_session` post-P2.14 (always landed before this phase); a preset with neither passes `None`/`[]` and changes nothing:

```python
                    await configure_grok_session(session, operator, voice, custom_role=role,
                                                 tool_group_override=tool_group_override,
                                                 keyterms=merged["keyterms"],
                                                 language_hint=merged["language"])
```

MODEL NOTE: P2.8 (always landed before this phase) gave `connect_to_grok` its `model=` kwarg. In this connect branch the explicit model already flows as `connect_to_grok(session, model=data.get("model") or ...)` — change that argument to `merged["model"]` so preset-supplied models ride the same allowlist-validated path (explicit still wins inside `merge_connect_params`).

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_preset_configure.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.routes.grok_live_routes; print('import ok')"`
Expected: PASS (5 passed) then `import ok`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_voice_preset_configure.py
git commit -m "feat(voice-agents): /ws/grok-live accepts ?agent=/connect.agent — preset merge + tool_group_override"
```

### Task P4.8: make_phone_call role accepts "preset:<id>" (server-side resolution)

The three voice routes' `make_phone_call` dispatches (realtime_routes.py:942-971, gemini_live_routes.py:1099-1128, grok_live_routes.py:814-843) all POST `role` verbatim to `POST /twilio/call` — so ONE resolution point in twilio_routes covers them all. No edits to the three dispatches needed.

**Files:**
- Modify: Orchestrator/routes/twilio_routes.py:889-911 (inside `initiate_outbound_call`, before the backend_map at :905-910)
- Modify: ToolVault/tools/make_phone_call/schema.json (role description)
- Test: Orchestrator/tests/test_twilio_preset_role.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_twilio_preset_role.py
"""POST /twilio/call resolves role='preset:<id>' server-side (P4).

Calls the route function directly (twilio_routes registers on the shared app
via decorators — no router to mount). Unknown preset must fail LOUDLY before
any Twilio interaction; resolution must run before the backend_map.
"""
import asyncio
import pytest

from Orchestrator.voice_agents import registry as va
from Orchestrator.routes import twilio_routes as tw


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))


def _call(role, greeting=""):
    req = tw.OutboundCallRequest(to="+15551234567", role=role, greeting=greeting)
    return asyncio.run(tw.initiate_outbound_call(req)), req


def test_unknown_preset_errors_before_anything_else(reg):
    result, _ = _call("preset:va-nope")
    assert "error" in result
    assert "preset" in result["error"].lower()


def test_preset_substitutes_role_backend_greeting(reg):
    p = va.add_preset(name="Pizza", provider="grok-live",
                      instructions="You order pizzas.", greeting="Hello!")
    result, req = _call(f"preset:{p['id']}")
    # Resolution mutates the request before the Twilio-cred checks (which
    # error out on this box-less test env — that's fine, we assert the merge).
    assert req.role == "You order pizzas."
    assert req.backend == "grok_live"          # preset provider drives backend
    assert req.greeting == "Hello!"


def test_plain_role_untouched(reg):
    _, req = _call("Be a pirate", greeting="hi")
    assert req.role == "Be a pirate" and req.greeting == "hi"
```

**Step 2: Run test to verify it fails**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_twilio_preset_role.py -x -q`
Expected: FAIL — `test_unknown_preset_errors_before_anything_else` gets a Twilio-config error (or no error) instead of a preset error; `req.role` still `"preset:va-nope"`.

**Step 3: Write minimal implementation**

In `Orchestrator/routes/twilio_routes.py`, at the TOP of `initiate_outbound_call`'s body (anchor `async def initiate_outbound_call(call_request: OutboundCallRequest)`, currently :862; insert before the `REQUESTS_AVAILABLE` check at :881 so preset errors surface even on unconfigured boxes):

```python
    # P4: role="preset:<id>" resolves server-side — preset instructions become
    # the call persona, preset provider selects the backend, preset greeting
    # fills an empty greeting. Unknown preset fails LOUDLY (never dial with
    # the literal 'preset:...' string as persona).
    if isinstance(call_request.role, str) and call_request.role.startswith("preset:"):
        from Orchestrator.voice_agents.registry import resolve_phone_role
        try:
            call_request.role, call_request.backend, call_request.greeting = \
                resolve_phone_role(call_request.role, call_request.backend,
                                   call_request.greeting)
        except KeyError as e:
            return {"error": f"Voice agent preset not found: {e}"}
```

Then update `ToolVault/tools/make_phone_call/schema.json` — replace the `role` description with:

```json
        "description": "The PERSONA/CHARACTER for the AI voice agent — define WHO they are. This becomes the agent's system prompt before the call starts. OR pass 'preset:<id>' to use a saved voice-agent preset (see GET /voice-agents): the preset's instructions become the persona, its provider selects the backend, and its greeting is used when none is given."
```

Validate + reload ToolVault (v2 workflow):

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate
curl -s -X POST http://localhost:9091/toolvault/reload
```

Expected: validator exits 0; reload returns success JSON.

**Step 4: Run test to verify it passes**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_twilio_preset_role.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.routes.twilio_routes; print('import ok')"`
Expected: PASS (3 passed) then `import ok`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/twilio_routes.py ToolVault/tools/make_phone_call/schema.json Orchestrator/tests/test_twilio_preset_role.py
git commit -m "feat(voice-agents): make_phone_call role='preset:<id>' resolved server-side at /twilio/call"
```

### Task P4.9: Portal — modal-open preset refresh + dropdowns-go-live E2E

Frontend task — verification commands instead of pytest. P3.24–P3.27 already shipped the entire Portal preset surface (`Portal/modules/voice-presets.js` + hidden preset rows, SEL keys, and `connectMsg.agent`/`reconnectMsg.agent` wiring in all three panels) — dormant pre-P4 because `GET /voice-agents` 404'd. Do NOT create a second module (the pre-review draft's `voice-agent-presets.js` is exactly the duplication this task replaces). This task adds the one missing behavior — refresh the dropdowns on every modal open so a preset created seconds ago appears without a reload — and verifies the whole P3c stack against the now-live registry.

**Files:**
- Modify: Portal/modules/voice-presets.js (append one export)
- Modify: Portal/modules/voice-agents-modal.js (import + openModal hook)
- Test: Orchestrator/tests/test_portal_voice_parity.py (append literals)

**Step 1: Write the failing test**

Append `"refreshAllPresetDropdowns"` to BOTH the `"modules/voice-presets.js"` and `"modules/voice-agents-modal.js"` lists in `FILE_LITERALS`.

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q`
Expected: FAIL — 2 new failures (literal missing in both files).

**Step 2: Append the refresh helper to voice-presets.js**

```javascript
/**
 * Re-fetch the registry and repopulate every panel's preset dropdown.
 * Called on every Voice Agents modal open (and after P4.10 manage-UI
 * saves/deletes) so registry edits appear without a page reload.
 * Empty (or emptied) registry: the select is cleared and its va-row
 * re-hidden — the panel returns to the exact pre-P4 look.
 * Alias arrays MUST stay identical to the per-panel init hooks
 * (P3.25-27) — copy them from gpt-realtime.js / gemini-live.js /
 * grok-live.js if they differ from the ones below.
 * @returns {Promise<Array>} the fetched presets (P4.10 manage UI reuses them)
 */
export async function refreshAllPresetDropdowns() {
    const presets = await fetchVoicePresets();
    const panels = [
        ['vaRealtimePresetSelect', ['openai', 'realtime', 'gpt-realtime']],
        ['vaGeminiPresetSelect', ['google', 'gemini', 'gemini-live']],
        ['vaGrokPresetSelect', ['grok', 'xai', 'grok-live']],
    ];
    for (const [id, aliases] of panels) {
        const sel = document.getElementById(id);
        if (!sel) continue;
        const scoped = filterPresetsByProvider(presets, aliases);
        if (scoped.length === 0) {
            sel.innerHTML = '';
            const row = sel.closest('.va-row');
            if (row) row.style.display = 'none';
        } else {
            populatePresetDropdown(sel, scoped);
        }
    }
    return presets;
}
```

**Step 3: Hook the modal**

In `Portal/modules/voice-agents-modal.js` add the import at the top (next to the other module imports):

```javascript
import { refreshAllPresetDropdowns } from './voice-presets.js';
```

and inside `openModal()` (:145-149), after `ensureProvidersInit()`:

```javascript
    refreshAllPresetDropdowns();
```

**Step 4: Verify (unit + live E2E)**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_portal_voice_parity.py -q   # PASS
node --input-type=module --check < Portal/modules/voice-presets.js && node --input-type=module --check < Portal/modules/voice-agents-modal.js && echo js-ok
test ! -f Portal/modules/voice-agent-presets.js && echo "no duplicate module — ok"
```

Live E2E (registry is live as of P4.4):

```bash
AID=$(curl -s -X POST http://localhost:9091/voice-agents -H "Content-Type: application/json" \
  -d '{"name":"Dropdown Probe","provider":"grok-live"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['agent']['id'])")
```

Browser (hard refresh): open the Voice Agents modal → Grok tab: the Preset row is VISIBLE listing "Dropdown Probe" (the other tabs' rows stay hidden — no presets for those providers); select it, Connect → `journalctl -u blackbox.service --since "-2 min" | grep -i preset` shows the preset applied at configure (P4.7's log line); disconnect. Then `curl -s -X DELETE http://localhost:9091/voice-agents/$AID`, close and reopen the modal → the Grok preset row is hidden again.

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Portal/modules/voice-presets.js Portal/modules/voice-agents-modal.js Orchestrator/tests/test_portal_voice_parity.py
git commit -m "feat(portal): refresh voice-preset dropdowns on modal open — P3c wiring goes live against /voice-agents"
```

### Task P4.10: Portal — minimal preset manage UI (Presets tab)

**Files:**
- Modify: Portal/index.html:1833-1837 (tab strip) + after :1952 (new pane) + the `?v=genuiNN` cache-buster
- Modify: Portal/modules/voice-presets.js (append manage logic — SAME module P3.24 created; no second module)
- Modify: Portal/modules/voice-agents-modal.js (init hook)

**Step 1: Add the Presets tab + pane markup**

In `Portal/index.html` tab strip (:1833-1837) add:

```html
          <button class="va-tab" data-provider="presets" role="tab" type="button">Presets</button>
```

After the grok pane closes (:1952), add:

```html
        <!-- Presets manage pane (P4) -->
        <div class="va-pane" data-pane="presets" style="display:none;">
          <div class="va-row">
            <label for="vaPresetList">Saved</label>
            <select id="vaPresetList"><option value="">— new preset —</option></select>
          </div>
          <div class="va-row"><label for="vaPresetName">Name</label>
            <input id="vaPresetName" type="text" maxlength="64" placeholder="Pizza Bot"></div>
          <div class="va-row"><label for="vaPresetProvider">Provider</label>
            <select id="vaPresetProvider">
              <option value="realtime">GPT Realtime</option>
              <option value="gemini-live">Gemini Live</option>
              <option value="grok-live">Grok Live</option>
            </select></div>
          <div class="va-row"><label for="vaPresetModel">Model</label>
            <input id="vaPresetModel" type="text" placeholder="(provider default)"></div>
          <div class="va-row"><label for="vaPresetVoice">Voice</label>
            <input id="vaPresetVoice" type="text" placeholder="(provider default)"></div>
          <div class="va-row"><label for="vaPresetGreeting">Greeting</label>
            <input id="vaPresetGreeting" type="text" maxlength="2000"></div>
          <div class="va-row"><label for="vaPresetInstructions">Instructions</label>
            <textarea id="vaPresetInstructions" rows="5" placeholder="Persona/system prompt — replaces the default persona"></textarea></div>
          <div class="va-controls">
            <button id="vaPresetSave" class="btn btn-primary" type="button">Save</button>
            <button id="vaPresetDelete" class="btn btn-warn" type="button" disabled>Delete</button>
          </div>
          <div class="va-status" id="vaPresetStatus">No presets yet — fill the form and Save.</div>
        </div>
```

(`setupTabs()` in voice-agents-modal.js iterates `.va-tab`/`.va-pane` generically — the 4th tab needs no JS changes for switching.)

Then bump the Portal cache-buster: read the CURRENT `?v=genuiNN` number in `Portal/index.html` (:11 and :21) and increment it by one (do NOT hard-code a number — earlier phases have already bumped it), updating the trailing comment to "vNNN: P4 presets manage tab".

**Step 2: Append manage logic to voice-presets.js (P3.24's module)**

```javascript
// ------------------------------------------------------------------ manage UI

const $id = (i) => document.getElementById(i);
let managePresets = [];

function fillForm(p) {
    $id('vaPresetName').value = p?.name || '';
    $id('vaPresetProvider').value = p?.provider || 'realtime';
    $id('vaPresetModel').value = p?.model || '';
    $id('vaPresetVoice').value = p?.voice || '';
    $id('vaPresetGreeting').value = p?.greeting || '';
    $id('vaPresetInstructions').value = p?.instructions || '';
    $id('vaPresetDelete').disabled = !p;
}

function renderManageList(presets) {
    managePresets = presets;
    const list = $id('vaPresetList');
    if (!list) return;
    const current = list.value;
    list.innerHTML = '<option value="">— new preset —</option>';
    presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} (${p.provider})`;
        list.appendChild(opt);
    });
    if ([...list.options].some(o => o.value === current)) list.value = current;
    const status = $id('vaPresetStatus');
    if (status) status.textContent = presets.length
        ? `${presets.length} preset(s)` : 'No presets yet — fill the form and Save.';
}

async function savePreset() {
    const id = $id('vaPresetList').value;
    const body = {
        name: $id('vaPresetName').value.trim(),
        provider: $id('vaPresetProvider').value,
        model: $id('vaPresetModel').value.trim(),
        voice: $id('vaPresetVoice').value.trim(),
        greeting: $id('vaPresetGreeting').value,
        instructions: $id('vaPresetInstructions').value,
    };
    const res = await fetch(id ? `/voice-agents/${id}` : '/voice-agents', {
        method: id ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const status = $id('vaPresetStatus');
    if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
        status.textContent = `Save failed: ${detail}`;   // fail LOUDLY, keep form
        return;
    }
    status.textContent = id ? 'Updated.' : 'Created.';
    renderManageList(await refreshAllPresetDropdowns());
}

async function deletePreset() {
    const id = $id('vaPresetList').value;
    if (!id) return;
    const res = await fetch(`/voice-agents/${id}`, { method: 'DELETE' });
    $id('vaPresetStatus').textContent = res.ok ? 'Deleted.' : `Delete failed: HTTP ${res.status}`;
    $id('vaPresetList').value = '';
    fillForm(null);
    renderManageList(await refreshAllPresetDropdowns());
}

export function initPresetManageUI() {
    if (!$id('vaPresetSave')) return;   // markup absent — degrade silently
    $id('vaPresetSave').addEventListener('click', savePreset);
    $id('vaPresetDelete').addEventListener('click', deletePreset);
    $id('vaPresetList').addEventListener('change', () => {
        fillForm(managePresets.find(p => p.id === $id('vaPresetList').value) || null);
    });
}

export async function refreshManageUI() {
    renderManageList(await refreshAllPresetDropdowns());
}
```

**Step 3: Hook into the modal**

In `Portal/modules/voice-agents-modal.js`: extend the P4.9 import to `import { refreshAllPresetDropdowns, initPresetManageUI, refreshManageUI } from './voice-presets.js';`; call `initPresetManageUI()` inside `ensureProvidersInit()` (:110-117); replace the `refreshAllPresetDropdowns()` call P4.9 put in `openModal()` with `refreshManageUI()` (which refreshes the panel dropdowns AND the manage list).

**Step 4: Verify**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
node --input-type=module --check < Portal/modules/voice-presets.js && node --input-type=module --check < Portal/modules/voice-agents-modal.js && echo js-ok
grep -c 'data-pane="presets"' Portal/index.html   # Expected: 1
```

Browser check (hard refresh): Voice Agents modal → Presets tab → create "Browser Test" (provider Grok Live) → it appears in the Grok pane's Preset dropdown (row unhides) → edit greeting → Save → Delete → the Grok preset row hides again and the manage pane returns to "No presets yet — fill the form and Save."

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Portal/index.html Portal/modules/voice-presets.js Portal/modules/voice-agents-modal.js
git commit -m "feat(portal): Presets manage tab in Voice Agents modal — create/edit/delete over /voice-agents"
```

### Task P4.11: Android — preset dropdown goes live (E2E verification of P3.12/P3.13)

Verification-only task — NO new code. P3.12 shipped `VoiceSessionConfig.agentId` + the `&agent=` URL param; P3.13 shipped the `VoiceAgentPreset.kt` parser (+ `VoiceAgentPresetTest`), the DataStore-persisted `_selectedPresetId`, the `/voice-agents` fetch, and the settings-pane dropdown (hidden pre-P4 because the registry 404'd). Manage UI stays in the Portal by design — Android is select-and-connect only. This task proves the dormant plumbing end-to-end against the live registry. Android root: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal` (run gradle from there).

**Step 1: Unit gate still green**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline`
Expected: BUILD SUCCESSFUL — `VoiceAgentPresetTest` 3/3 among them (~35s).

**Step 2: Live E2E on device**

```bash
AID=$(curl -s -X POST http://localhost:9091/voice-agents -H "Content-Type: application/json" \
  -d '{"name":"Fold Probe","provider":"gemini-live","voice":"Kore","instructions":"You are the Fold probe. Say only: fold ok."}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['agent']['id'])")
```

On the device (app pointed at this box): Voice screen → backend Gemini Live → the settings pane shows the "Agent preset" dropdown listing "Fold Probe" → select it → Connect → speak → the agent answers in the preset persona/voice. Backend check: `journalctl -u blackbox.service --since "-3 min" | grep -i preset` shows the preset resolved and applied (P4.6's path) and the WS URL in the connect log carries `agent=<id>`. Kill and reopen the app → the preset selection PERSISTED (P3.13 DataStore write-through).

**Step 3: Fresh-box degrade**

`curl -s -X DELETE http://localhost:9091/voice-agents/$AID` → re-enter the Voice screen → the preset dropdown is hidden (empty registry) and voice sessions connect exactly as pre-P4.

**Step 4: Record**

No commit (nothing changed). If any step failed, the defect lives in P3.12/P3.13 (Android) or P4.3/P4.6 (backend) — fix it at the source through the normal task flow and re-run this verification; do not patch around it here.

### Task P4.12: Phase acceptance — full suite, live E2E, fresh-box gate

Verification-only task (no new code; fixes only if a check fails).

**Step 1: Full backend suite green**

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_agents_registry.py Orchestrator/tests/test_voice_agents_resolution.py Orchestrator/tests/test_voice_agent_routes.py Orchestrator/tests/test_voice_preset_configure.py Orchestrator/tests/test_twilio_preset_role.py -q`
Expected: all pass, 0 failures. Then the repo-wide regression gate: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests -q -x --ignore=Orchestrator/tests/test_cli_agent` — no NEW failures vs the pre-phase baseline (record baseline before starting if any pre-existing reds).

**Step 2: Restart + live end-to-end preset session (NOTE: makes one brief REAL OpenAI realtime connection — pennies of session.update tokens, no audio)**

```bash
sudo systemctl restart blackbox.service && sleep 75
AID=$(curl -s -X POST http://localhost:9091/voice-agents -H "Content-Type: application/json" \
  -d '{"name":"E2E Probe","provider":"realtime","voice":"marin","instructions":"You are the E2E probe agent. Say only: probe ok."}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['agent']['id'])")
Orchestrator/venv/bin/python - <<EOF
import asyncio, json, websockets

async def main():
    uri = f"ws://localhost:9091/ws/realtime/p4-e2e-probe?agent=$AID"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "connect", "operator": "system"}))
        for _ in range(12):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            print(msg.get("type"), str(msg.get("data"))[:80])
            if msg.get("type") == "connected":
                print("E2E OK — preset applied at configure")
                break
            assert msg.get("type") != "warning", f"preset not resolved: {msg}"
        await ws.send(json.dumps({"type": "disconnect"}))

asyncio.run(main())
EOF
curl -s -X DELETE http://localhost:9091/voice-agents/$AID
```

Expected: `connected` event, `E2E OK — preset applied at configure`, no `warning`. Also `journalctl -u blackbox.service --since "-3 min" | grep "REALTIME" | grep -i voice` shows `Voice selected: marin` (preset voice won because no explicit voice was sent).

**Step 3: Bogus-preset loud failure**

Repeat the probe with `?agent=va-nope` — expected: a `warning` event containing `va-nope` arrives, then `connected` (session proceeds on defaults). Disconnect immediately.

**Step 4: Fresh-box degradation gate**

```bash
curl -s http://localhost:9091/voice-agents            # {"agents":[]}
curl -s "http://localhost:9091/voice-agents?provider=grok-live"   # {"agents":[]}
git status --porcelain | grep -E "credentials/" ; echo "exit=$?"  # exit=1 — nothing committable
```

Portal (hard refresh): every panel's Preset row is HIDDEN (empty registry — pre-P4 look); the Presets manage tab shows "No presets yet — fill the form and Save." Android: Preset dropdown hidden entirely (empty list) — voice flows unchanged.

**Step 5: Commit (only if Steps 1-4 forced fixes) and record**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git status --porcelain   # expect clean; if fixes were needed, git add the EXPLICIT touched paths and commit:
# git commit -m "fix(voice-agents): P4 acceptance fixes (<what>)"
```

Phase complete — per CLAUDE.md, mint a dev snapshot via `/snapshot-dev` at session end covering P4 (registry + CRUD + 3-route apply-at-configure + phone preset roles + Portal/Android surfaces).

---

## Phase 5 — xAI phone number (sovereign line)

**Scope (design workstream 3, phone half):** provision the account's free xAI number with a signed-webhook attach, verify `realtime.call.incoming` webhooks (Standard Webhooks HMAC), bridge each verified call into a `GrokLiveSession` (`phone-xai-<call_id>`) that rides the existing grok_live machinery (config, tools, transcript-to-ledger via the P1b `/chat/save` path, reaper), wire `/refer` + `/hangup` as agent-invocable ToolVault tools, and expose ONLY the webhook path publicly via Tailscale Funnel (MCP-remote pattern). Twilio remains untouched (additive second line).

**Dependencies:** P1b (transcript save via `/chat/save` inside `save_grok_session_to_blackbox`) and P4 (`voice-agents` preset registry) land before this phase. P5 degrades gracefully if the preset registry is absent (guarded import). P2.8/P2.13 rewrite `connect_to_grok` (`?model=` allowlist resolution, `&conversation_id=` resumption) and land FIRST — P5.4 is written as an ADDITIVE diff on the post-P2.13 function and shows the complete merged result.

**Flagged uncertainties for live validation (P5.8):** (a) the exact `origin` enum value for the free xAI-provisioned number (`byo_trunk` is documented for BYO only); (b) the exact response field name carrying the once-returned signing secret (we persist the FULL raw response so it can never be lost); (c) whether `input_audio_buffer.append` keepalive silence is honored/harmful on SIP-attached calls (audio flows xAI-side — the `call_id` session IS the audio path; P5.5 gates the silence injection off for call sessions, keeping stale-detection).

---

### Task P5.1: Standard-Webhooks signature verification (security-critical)

**Files:**
- Create: Orchestrator/xai_phone/__init__.py
- Create: Orchestrator/xai_phone/signature.py
- Test: Orchestrator/tests/test_xai_phone_signature.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_phone_signature.py
"""Standard-Webhooks HMAC verification for the xAI voice webhook.

Security-critical: this is the ONLY auth on the one publicly-funneled path.
Covers: valid / tampered body / wrong secret / stale / future-dated /
replayed id / missing+malformed headers / multi-signature header /
whsec_-prefixed and raw-string secrets / case-insensitive headers.
"""
import base64
import hashlib
import hmac

import pytest

from Orchestrator.xai_phone.signature import ReplayCache, verify_signature

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-long!!").decode()
BODY = b'{"type":"realtime.call.incoming","call_id":"call-123"}'
NOW = 1_800_000_000.0


def sign(body: bytes, msg_id="msg_1", ts=None, secret=SECRET, version="v1"):
    ts = str(int(NOW)) if ts is None else ts
    if secret.startswith("whsec_"):
        key = base64.b64decode(secret[len("whsec_"):])
    else:
        key = secret.encode()
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": f"{version},{base64.b64encode(mac).decode()}",
    }


def _fresh():
    return ReplayCache()


def test_valid_signature_accepted():
    ok, reason = verify_signature(SECRET, sign(BODY), BODY, now=NOW, replay_cache=_fresh())
    assert ok, reason


def test_tampered_body_rejected():
    ok, reason = verify_signature(SECRET, sign(BODY), BODY + b"x", now=NOW, replay_cache=_fresh())
    assert not ok and reason == "signature mismatch"


def test_wrong_secret_rejected():
    other = "whsec_" + base64.b64encode(b"a-completely-different-key!!!!!!").decode()
    ok, reason = verify_signature(other, sign(BODY), BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "signature mismatch"


def test_stale_timestamp_rejected():
    headers = sign(BODY, ts=str(int(NOW) - 301))
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "timestamp outside tolerance"


def test_future_timestamp_rejected():
    headers = sign(BODY, ts=str(int(NOW) + 301))
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "timestamp outside tolerance"


def test_replay_rejected():
    cache = _fresh()
    headers = sign(BODY, msg_id="msg_replay")
    ok1, _ = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=cache)
    ok2, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=cache)
    assert ok1
    assert not ok2 and reason == "replayed webhook-id"


def test_missing_headers_rejected():
    ok, reason = verify_signature(SECRET, {}, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "missing webhook headers"


def test_malformed_timestamp_rejected():
    headers = sign(BODY)
    headers["webhook-timestamp"] = "not-a-number"
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "malformed timestamp"


def test_empty_secret_rejected():
    ok, reason = verify_signature("", sign(BODY), BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "no signing secret configured"


def test_case_insensitive_headers():
    headers = {k.upper(): v for k, v in sign(BODY, msg_id="msg_upper").items()}
    ok, _ = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert ok


def test_multiple_signatures_one_valid():
    headers = sign(BODY, msg_id="msg_multi")
    headers["webhook-signature"] = "v2,Z2FyYmFnZQ== " + headers["webhook-signature"]
    ok, _ = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert ok


def test_raw_string_secret_supported():
    raw = "plain-secret-no-prefix"
    headers = sign(BODY, msg_id="msg_raw", secret=raw)
    ok, _ = verify_signature(raw, headers, BODY, now=NOW, replay_cache=_fresh())
    assert ok


def test_replay_cache_bounded():
    cache = ReplayCache(maxsize=2)
    assert not cache.seen_before("a")
    assert not cache.seen_before("b")
    assert not cache.seen_before("c")   # evicts "a"
    assert cache.seen_before("b")
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_phone_signature.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'Orchestrator.xai_phone'`

**Step 3: Write minimal implementation**

Create `Orchestrator/xai_phone/__init__.py`:

```python
"""xAI sovereign phone line: provisioning, signed-webhook verification, call attach, call control."""
```

Create `Orchestrator/xai_phone/signature.py`:

```python
"""Standard-Webhooks signature verification for the xAI voice webhook.

xAI signs `realtime.call.incoming` webhooks with the Standard Webhooks scheme
(https://www.standardwebhooks.com/):

    signed_content = f"{webhook-id}.{webhook-timestamp}." + raw_body
    signature      = base64( HMAC-SHA256(secret, signed_content) )

delivered via three headers:
    webhook-id:        unique message id (also the replay key)
    webhook-timestamp: unix seconds
    webhook-signature: space-separated list of "v1,<base64sig>"

Security properties enforced here:
  * constant-time compare (hmac.compare_digest) — no timing oracle;
  * timestamp tolerance ±TOLERANCE_SEC (stale AND future-dated rejected);
  * replay rejection: a webhook-id is accepted at most once per process
    (bounded first-seen-wins cache; ids outside tolerance can't replay anyway).

The secret is used as raw bytes; a `whsec_` prefix (Standard Webhooks portable
secret format) is stripped and base64-decoded when present.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections import OrderedDict

TOLERANCE_SEC = 300  # ±5 minutes
_REPLAY_CACHE_MAX = 4096


class ReplayCache:
    """Bounded first-seen-wins webhook-id cache (per-process)."""

    def __init__(self, maxsize: int = _REPLAY_CACHE_MAX):
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._maxsize = maxsize

    def seen_before(self, webhook_id: str) -> bool:
        if webhook_id in self._seen:
            return True
        self._seen[webhook_id] = time.time()
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


_default_replay_cache = ReplayCache()


def _secret_bytes(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[len("whsec_"):], validate=True)
        except (binascii.Error, ValueError):
            pass  # fall through: treat the whole string as raw bytes
    return secret.encode("utf-8")


def verify_signature(
    secret: str,
    headers: dict,
    body: bytes,
    *,
    now: float | None = None,
    tolerance: int = TOLERANCE_SEC,
    replay_cache: ReplayCache | None = None,
) -> tuple[bool, str]:
    """Verify a Standard-Webhooks-signed request.

    Returns (ok, reason). `reason` is for server-side logs ONLY — never echo
    it to the caller beyond a generic 401.
    """
    if not secret:
        return False, "no signing secret configured"

    lowered = {str(k).lower(): v for k, v in headers.items()}
    msg_id = lowered.get("webhook-id", "")
    timestamp = lowered.get("webhook-timestamp", "")
    sig_header = lowered.get("webhook-signature", "")
    if not msg_id or not timestamp or not sig_header:
        return False, "missing webhook headers"

    try:
        ts = int(timestamp)
    except ValueError:
        return False, "malformed timestamp"
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        return False, "timestamp outside tolerance"

    signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(
        hmac.new(_secret_bytes(secret), signed_content, hashlib.sha256).digest()
    ).decode("ascii")

    valid = False
    for candidate in sig_header.split(" "):
        if "," not in candidate:
            continue
        version, sig = candidate.split(",", 1)
        if version == "v1" and hmac.compare_digest(sig, expected):
            valid = True
    if not valid:
        return False, "signature mismatch"

    cache = _default_replay_cache if replay_cache is None else replay_cache
    if cache.seen_before(msg_id):
        return False, "replayed webhook-id"
    return True, "ok"
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_phone_signature.py -q`
Expected: PASS (13 passed)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/xai_phone/__init__.py Orchestrator/xai_phone/signature.py Orchestrator/tests/test_xai_phone_signature.py
git commit -m "feat(xai-phone): standard-webhooks signature verification (constant-time, ±5min, replay-guarded)"
```

---

### Task P5.2: Provisioning module + credential store

**Files:**
- Create: Orchestrator/xai_phone/provisioning.py
- Test: Orchestrator/tests/test_xai_phone_provisioning.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_phone_provisioning.py
"""Provisioning + credential store for the xAI sovereign line.

House conventions (custom_servers.py precedent): STORE_PATH monkeypatched to
tmp_path so no test touches credentials/xai_phone.json; _api_post monkeypatched
so no test hits api.x.ai.
"""
import json
import os
import stat

import pytest

from Orchestrator.xai_phone import provisioning as pv


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "xai_phone.json"
    monkeypatch.setattr(pv, "STORE_PATH", str(path))
    return path


@pytest.fixture
def fake_api(monkeypatch):
    calls = []

    async def _fake_post(path, payload):
        calls.append((path, payload))
        return {
            "id": "pn-1",
            "phone_number": "+15550100",
            "signing_secret": "whsec_c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0ISE=",
        }

    monkeypatch.setattr(pv, "_api_post", _fake_post)
    return calls


def test_status_unprovisioned(store):
    s = pv.get_status()
    assert s["provisioned"] is False
    assert s["phone_number"] is None


@pytest.mark.asyncio
async def test_provision_persists_number_and_secret(store, fake_api):
    status = await pv.provision_number("BlackBox line", "https://box.ts.net:10000/xai/voice/incoming")
    assert status["provisioned"] is True
    assert status["phone_number"] == "+15550100"
    assert status["has_signing_secret"] is True
    assert "signing_secret" not in status            # status NEVER leaks the secret
    assert "raw_response" not in status
    on_disk = json.loads(store.read_text())
    assert on_disk["phone_number"] == "+15550100"
    assert on_disk["signing_secret"].startswith("whsec_")
    assert on_disk["raw_response"]["id"] == "pn-1"   # secret returned ONCE: keep everything
    assert fake_api[0][0] == "/v2/phone-numbers"
    assert fake_api[0][1] == {
        "origin": pv.ORIGIN_PROVISIONED,
        "name": "BlackBox line",
        "webhook": "https://box.ts.net:10000/xai/voice/incoming",
    }


@pytest.mark.asyncio
async def test_store_file_is_0600(store, fake_api):
    await pv.provision_number("line", "https://x/hook")
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600


@pytest.mark.asyncio
async def test_provision_idempotent_refuses_second_call(store, fake_api):
    await pv.provision_number("line", "https://x/hook")
    with pytest.raises(pv.AlreadyProvisionedError):
        await pv.provision_number("line", "https://x/hook")
    assert len(fake_api) == 1                        # API NOT called again


@pytest.mark.asyncio
async def test_provision_force_reprovisions_and_keeps_preset(store, fake_api):
    await pv.provision_number("line", "https://x/hook")
    pv.set_default_preset_id("preset-abc")
    status = await pv.provision_number("line2", "https://y/hook", force=True)
    assert len(fake_api) == 2
    assert status["default_preset_id"] == "preset-abc"


@pytest.mark.asyncio
async def test_secret_extraction_fallback_field_names(store, monkeypatch):
    async def _fake_post(path, payload):
        return {"phone_number": "+1555", "webhook": {"secret": "nested-secret"}}
    monkeypatch.setattr(pv, "_api_post", _fake_post)
    await pv.provision_number("line", "https://x/hook")
    assert pv.get_signing_secret() == "nested-secret"


def test_corrupt_store_quarantined(store):
    store.write_text("{not json")
    assert pv.read_store() == {}
    assert not store.exists()                        # renamed to *.corrupt-<ts>
    assert any(p.name.startswith("xai_phone.json.corrupt-") for p in store.parent.iterdir())


def test_default_preset_roundtrip(store):
    assert pv.get_default_preset_id() is None
    pv.set_default_preset_id("preset-1")
    assert pv.get_default_preset_id() == "preset-1"
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_phone_provisioning.py -q`
Expected: FAIL with `ImportError: cannot import name 'provisioning'`

**Step 3: Write minimal implementation**

Create `Orchestrator/xai_phone/provisioning.py`:

```python
"""xAI sovereign phone line — provisioning + credential store.

POST https://api.x.ai/v2/phone-numbers provisions the account's free number
and returns the webhook signing secret ONCE. The full raw response is
persisted verbatim to credentials/xai_phone.json (gitignored via the
`credentials/` rule, 0600, atomic writes) so a mis-guessed response field
name can never lose the secret.

Store conventions follow Orchestrator/onboarding/custom_servers.py:
fresh read per call, tmp-file + os.replace atomic writes, corrupt-file
quarantine (*.corrupt-<ts>). Single-writer process assumption (Orchestrator).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from Orchestrator.config import XAI_API_KEY
from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

STORE_PATH = str(resolve("credentials", "xai_phone.json"))
XAI_API_BASE = "https://api.x.ai"
# UNCERTAIN (recon xaiResearch.json): docs confirm `origin` is required and
# 'byo_trunk' is the value for customer-owned numbers; the enum value for the
# free xAI-provisioned number is undocumented. Live validation (Task P5.8)
# confirms; adjust ONLY this constant if the API rejects it.
ORIGIN_PROVISIONED = "provisioned"

_LOCK = threading.Lock()


class AlreadyProvisionedError(RuntimeError):
    """A number is already provisioned; pass force=True to re-provision."""


# ---------------------------------------------------------------- persistence

def _quarantine(path: str) -> Optional[str]:
    dest = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def read_store() -> dict:
    """Load the store fresh from disk. Fail-soft: NEVER raises."""
    path = str(STORE_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantined = _quarantine(path)
        logger.warning("[XAI-PHONE] corrupt store at %s (%s) — quarantined to %s",
                       path, exc, quarantined or "<quarantine failed>")
        return {}
    except OSError as exc:
        logger.warning("[XAI-PHONE] unreadable store at %s (%s)", path, exc)
        return {}
    if not isinstance(data, dict):
        quarantined = _quarantine(path)
        logger.warning("[XAI-PHONE] wrong-shape store at %s — quarantined to %s",
                       path, quarantined or "<quarantine failed>")
        return {}
    return data


def _write_store(data: dict) -> None:
    """Atomically persist the store (tmp file + os.replace), 0600 perms."""
    path = str(STORE_PATH)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".xai_phone.", suffix=".tmp", delete=False,
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


# ------------------------------------------------------------------ read API

def get_status() -> dict:
    """Public line status. NEVER includes the signing secret or raw response."""
    data = read_store()
    return {
        "provisioned": bool(data.get("phone_number")),
        "phone_number": data.get("phone_number") or None,
        "webhook_url": data.get("webhook_url") or None,
        "has_signing_secret": bool(data.get("signing_secret")),
        "default_preset_id": data.get("default_preset_id"),
        "provisioned_at": data.get("provisioned_at"),
    }


def get_signing_secret() -> str:
    return read_store().get("signing_secret", "") or ""


def get_default_preset_id() -> Optional[str]:
    return read_store().get("default_preset_id") or None


def set_default_preset_id(preset_id: Optional[str]) -> None:
    with _LOCK:
        data = read_store()
        data["default_preset_id"] = preset_id
        _write_store(data)


# --------------------------------------------------------------- provisioning

async def _api_post(path: str, payload: dict) -> dict:
    """POST to the xAI REST API. Module-level so tests monkeypatch it."""
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY not configured (Orchestrator/config.py:446)")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{XAI_API_BASE}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()


def _extract_secret(resp: dict) -> str:
    """The signing secret is returned ONCE; field name unconfirmed — try all
    plausible spellings, including nested under 'webhook'."""
    for key in ("signing_secret", "webhook_secret", "webhook_signing_secret", "secret"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    nested = resp.get("webhook")
    if isinstance(nested, dict):
        for key in ("signing_secret", "secret"):
            if isinstance(nested.get(key), str) and nested[key]:
                return nested[key]
    return ""


async def provision_number(name: str, webhook_url: str, *, force: bool = False) -> dict:
    """Provision the account's free number with a webhook attach. Idempotent:
    refuses if already provisioned unless force=True. Returns get_status()."""
    existing = read_store()
    if existing.get("phone_number") and not force:
        raise AlreadyProvisionedError(
            f"Already provisioned: {existing['phone_number']} "
            f"(webhook {existing.get('webhook_url')}). Pass force=true to re-provision."
        )

    resp = await _api_post("/v2/phone-numbers", {
        "origin": ORIGIN_PROVISIONED,
        "name": name,
        "webhook": webhook_url,
    })

    secret = _extract_secret(resp)
    if not secret:
        logger.warning("[XAI-PHONE] no signing secret found in provisioning response "
                       "— check raw_response in %s", STORE_PATH)
    phone_number = resp.get("phone_number") or resp.get("number") or ""

    with _LOCK:
        _write_store({
            "version": 1,
            "phone_number": phone_number,
            "webhook_url": webhook_url,
            "name": name,
            "signing_secret": secret,
            "default_preset_id": existing.get("default_preset_id"),
            "provisioned_at": datetime.now(timezone.utc).isoformat(),
            "raw_response": resp,  # secret is returned ONCE — keep everything
        })
    logger.info("[XAI-PHONE] provisioned %s (webhook %s)", phone_number or "<no number in resp>", webhook_url)
    return get_status()
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_phone_provisioning.py -q`
Expected: PASS (9 passed)

Also verify the store path is already gitignored (no .gitignore change needed):
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git check-ignore -v credentials/xai_phone.json`
Expected: matches the `credentials/` rule (.gitignore line 27)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/xai_phone/provisioning.py Orchestrator/tests/test_xai_phone_provisioning.py
git commit -m "feat(xai-phone): number provisioning + 0600 atomic credential store (idempotent, secret-safe)"
```

---

### Task P5.3: HTTP surface — POST /xai/phone/provision + GET /xai/phone/status (with webhook preflight)

**Files:**
- Create: Orchestrator/routes/xai_phone_routes.py
- Modify: Orchestrator/app.py:137-138 (append router include)
- Test: Orchestrator/tests/test_xai_phone_routes.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_phone_routes.py
"""HTTP surface for the xAI sovereign line (house pattern: TestClient over a
minimal FastAPI app mounting the router — test_custom_servers_routes.py precedent)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.routes import xai_phone_routes as xr
from Orchestrator.xai_phone import provisioning as pv


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "xai_phone.json"
    monkeypatch.setattr(pv, "STORE_PATH", str(path))
    return path


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(xr.router)
    return TestClient(app)


@pytest.fixture
def fake_api(monkeypatch):
    async def _fake_post(path, payload):
        return {"phone_number": "+15550100", "signing_secret": "whsec_abc"}
    monkeypatch.setattr(pv, "_api_post", _fake_post)


def test_status_unprovisioned(client):
    r = client.get("/xai/phone/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provisioned"] is False
    assert "signing_secret" not in body


def test_provision_happy_path(client, fake_api):
    r = client.post("/xai/phone/provision", json={
        "name": "BlackBox line",
        "webhook_url": "https://box.ts.net:10000/xai/voice/incoming",
    })
    assert r.status_code == 200
    assert r.json()["phone_number"] == "+15550100"
    assert "signing_secret" not in r.json()


def test_provision_second_call_409_unless_force(client, fake_api):
    first = client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "https://x/hook"})
    assert first.status_code == 200
    second = client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "https://x/hook"})
    assert second.status_code == 409
    forced = client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "https://x/hook", "force": True})
    assert forced.status_code == 200


def test_provision_rejects_missing_or_insecure_webhook(client, fake_api):
    assert client.post("/xai/phone/provision", json={"name": "l"}).status_code == 400
    assert client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "http://insecure/hook"}).status_code == 400


def test_status_preflight_reports_webhook_reachability(client, fake_api, monkeypatch):
    client.post("/xai/phone/provision", json={"name": "l", "webhook_url": "https://x/hook"})

    async def fake_unsigned_post(url):
        assert url == "https://x/hook"
        return 401                       # unsigned POST rejected = endpoint live + enforcing
    monkeypatch.setattr(xr, "_unsigned_post", fake_unsigned_post)

    r = client.get("/xai/phone/status?preflight=true")
    assert r.status_code == 200
    assert r.json()["webhook_preflight"]["ok"] is True

    async def fake_unreachable(url):
        raise xr.httpx.ConnectError("no route")
    monkeypatch.setattr(xr, "_unsigned_post", fake_unreachable)
    r = client.get("/xai/phone/status?preflight=true")
    assert r.json()["webhook_preflight"]["ok"] is False
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_phone_routes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'Orchestrator.routes.xai_phone_routes'`

**Step 3: Write minimal implementation**

Create `Orchestrator/routes/xai_phone_routes.py`:

```python
"""xAI sovereign phone line — HTTP surface.

Routes:
    GET  /xai/phone/status      line status (?preflight=true adds a webhook
                                reachability probe); never leaks the secret
    POST /xai/phone/provision   idempotent provisioning (409 unless force)
    POST /xai/voice/incoming    signed telephony webhook (added in Task P5.6)

Uses the newer APIRouter convention (sms_routes.py precedent), included from
Orchestrator/app.py.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from Orchestrator.xai_phone import provisioning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xai", tags=["xai-phone"])


async def _unsigned_post(url: str) -> int:
    """Bare unsigned POST; returns the HTTP status. Module-level so tests
    monkeypatch it (provisioning._api_post precedent)."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        resp = await client.post(url, content=b"{}")
        return resp.status_code


async def _preflight_webhook(webhook_url: str | None) -> dict:
    """Is the public webhook URL reachable AND enforcing signatures?
    An unsigned POST must come back 401 — that proves both at once."""
    if not webhook_url:
        return {"ok": False, "detail": "no webhook_url provisioned"}
    try:
        status_code = await _unsigned_post(webhook_url)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"unreachable: {exc.__class__.__name__}: {exc}"}
    return {
        "ok": status_code == 401,
        "status_code": status_code,
        "detail": "unsigned POST rejected with 401 (reachable + enforcing)"
        if status_code == 401
        else f"expected 401 for unsigned POST, got {status_code} — check funnel target/path",
    }


@router.get("/phone/status")
async def xai_phone_status(preflight: bool = False):
    status = provisioning.get_status()
    if preflight:
        status["webhook_preflight"] = await _preflight_webhook(status.get("webhook_url"))
    return status


@router.post("/phone/provision")
async def xai_phone_provision(payload: dict):
    name = str(payload.get("name") or "").strip()
    webhook_url = str(payload.get("webhook_url") or "").strip()
    force = bool(payload.get("force", False))
    if not name or not webhook_url:
        raise HTTPException(status_code=400, detail="name and webhook_url are required")
    if not webhook_url.startswith("https://"):
        raise HTTPException(status_code=400,
                            detail="webhook_url must be a public https:// URL (Tailscale Funnel — scripts/xai_phone_funnel.sh)")
    try:
        return await provisioning.provision_number(name, webhook_url, force=force)
    except provisioning.AlreadyProvisionedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:  # XAI_API_KEY missing
        raise HTTPException(status_code=503, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502,
                            detail=f"xAI API error {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"xAI API unreachable: {exc}")
```

Modify `Orchestrator/app.py` — after the mcp_router block (lines 137-138):

```python
from Orchestrator.routes.mcp_routes import router as mcp_router
app.include_router(mcp_router)

from Orchestrator.routes.xai_phone_routes import router as xai_phone_router
app.include_router(xai_phone_router)
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_phone_routes.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.app; print('app imports OK')"`
Expected: PASS (6 passed) and `app imports OK` (the service runs live from this tree — the import gate is mandatory)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/xai_phone_routes.py Orchestrator/app.py Orchestrator/tests/test_xai_phone_routes.py
git commit -m "feat(xai-phone): /xai/phone/provision + /xai/phone/status routes with webhook preflight"
```

---

### Task P5.4: `connect_to_grok` call_id XOR model/conversation_id + `GrokLiveSession.call_id`

**NOTE for executor:** P2.8 (adds the `model` kwarg, GROK_LIVE_MODELS allowlist resolution and `session.model`) and P2.13 (adds the `conversation_id` resumption kwarg) execute BEFORE this task and both rewrite `connect_to_grok`. This task is an ADDITIVE diff on the post-P2.13 function: it appends a `call_id` kwarg (last, keyword-only in practice — every caller uses kwargs) plus the call-attach branch, and Step 3 shows the COMPLETE merged post-P2.13+P5.4 function. Do NOT apply this to the pre-P2 function — if P2.8/P2.13 have not landed in this tree, land them first. Pre-P2 line numbers (241-280) will have shifted; anchor on the `async def connect_to_grok` def. The `session.call_id` fallback exists so `grok_reconnect` → `connect_to_grok(session, model=session.model or None, conversation_id=resume_id)` (P2.13's call shape) rejoins the same live call on a `phone-xai-*` session instead of silently demoting it to a non-call `?model=` dial.

**Files:**
- Modify: Orchestrator/models.py (add `call_id` field to `GrokLiveSession` — anchor on the `provenance:` field, the dataclass's last field pre-P2; P2.8/P2.12 have already inserted `model`/`conversation_id`, shifting line numbers)
- Modify: Orchestrator/routes/grok_live_routes.py (`connect_to_grok` — the post-P2.13 version)
- Test: Orchestrator/tests/test_xai_call_connect.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_call_connect.py
"""connect_to_grok URL parameterization: ?call_id= (SIP attach) XOR
?model=[&conversation_id=] (P2.8/P2.13), with a session.call_id fallback so
reconnects rejoin the live call instead of demoting it to a non-call dial."""
import pytest

import Orchestrator.routes.grok_live_routes as glr
from Orchestrator.models import GrokLiveSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS  # P1b shared double


@pytest.fixture
def capture(monkeypatch):
    urls = []

    async def fake_connect(url, **kwargs):
        urls.append(url)
        return FakeUpstreamWS()

    monkeypatch.setattr(glr, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(glr, "WEBSOCKETS_AVAILABLE", True)
    monkeypatch.setattr(glr.websockets, "connect", fake_connect)
    return urls


@pytest.mark.asyncio
async def test_connect_with_call_id(capture):
    session = GrokLiveSession(session_id="phone-xai-c1")
    ok = await glr.connect_to_grok(session, call_id="c1")
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?call_id=c1"]
    assert session.call_id == "c1"
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_connect_with_model(capture):
    session = GrokLiveSession(session_id="s1")
    ok = await glr.connect_to_grok(session, model="grok-voice-latest")
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?model=grok-voice-latest"]
    assert session.call_id == ""


@pytest.mark.asyncio
async def test_explicit_call_id_excludes_model_and_conversation_id(capture):
    session = GrokLiveSession(session_id="s1")
    with pytest.raises(ValueError):
        await glr.connect_to_grok(session, call_id="c1", model="grok-voice-latest")
    with pytest.raises(ValueError):
        await glr.connect_to_grok(session, call_id="c1", conversation_id="conv_1")
    assert capture == []                             # never dialed


@pytest.mark.asyncio
async def test_reconnect_falls_back_to_session_call_id(capture):
    session = GrokLiveSession(session_id="phone-xai-c9", call_id="c9")
    ok = await glr.connect_to_grok(session)          # bare reconnect shape
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?call_id=c9"]


@pytest.mark.asyncio
async def test_call_id_fallback_beats_model_and_conversation_id(capture):
    # grok_reconnect (post-P2.13) passes model=/conversation_id= from the
    # session; on a phone-xai session the call_id fallback must WIN — the
    # args are swallowed (logged, not raised) and the SAME call is rejoined.
    session = GrokLiveSession(session_id="phone-xai-c9", call_id="c9")
    ok = await glr.connect_to_grok(session, model="grok-voice-latest",
                                   conversation_id="conv_stale")
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?call_id=c9"]


@pytest.mark.asyncio
async def test_plain_connect_resolves_default_model(capture):
    # Post-P2.8 a plain connect is NOT a bare URL: it resolves to the default
    # allowlisted model bound at the WS URL.
    session = GrokLiveSession(session_id="s1")
    ok = await glr.connect_to_grok(session)
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?model={glr.GROK_LIVE_MODEL}"]
    assert session.model == glr.GROK_LIVE_MODEL
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_call_connect.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'call_id'` (GrokLiveSession) / `connect_to_grok() got an unexpected keyword argument 'call_id'`. `test_connect_with_model` and `test_plain_connect_resolves_default_model` already pass post-P2.8 — that is expected.

**Step 3: Write minimal implementation**

In `Orchestrator/models.py`, add one field to `GrokLiveSession` after the `provenance` field (P2.8/P2.12 already added `model` after `voice` and `conversation_id` — anchor on `provenance:`, not line numbers):

```python
    call_id: str = ""                    # xAI SIP call id (phone-xai-* sessions); reconnects reuse it
```

In `Orchestrator/routes/grok_live_routes.py`, replace the post-P2.13 `connect_to_grok` (signature `(session, model=None, conversation_id=None)`) with the complete merged function below. The diff vs. P2.13 is ADDITIVE: the trailing `call_id` kwarg, the XOR guard, the call-attach branch (which skips model resolution entirely), and the connected-log variant — the P2.8 allowlist resolution and the P2.13 `&conversation_id=` append are byte-identical to what P2 landed:

```python
async def connect_to_grok(session: 'GrokLiveSession',
                          model: Optional[str] = None,
                          conversation_id: Optional[str] = None,
                          call_id: Optional[str] = None) -> bool:
    """
    Establish WebSocket connection to xAI Grok Voice Agent API.

    URL parameterization — call attach XOR model dial:
      * call_id — attach to a live SIP call (wss://.../realtime?call_id=...).
        The call_id session IS the call's audio path: audio flows xAI-side,
        there is no local audio pump, and xAI binds the model + conversation
        server-side — so model/conversation_id are EXCLUDED from the URL.
        Passing call_id together with model or conversation_id raises
        ValueError (caller bug). Persisted to session.call_id so reconnects
        rejoin the SAME call.
      * model — validated against GROK_LIVE_MODELS (P2.8); invalid values
        fall back to GROK_LIVE_MODEL with a logged warning; the resolved id
        is stamped on session.model and bound at the WS URL (?model=).
      * conversation_id — xAI session resumption (P2.13): appended as
        &conversation_id= so xAI replays cached turns.

    Fallback precedence: when call_id is NOT passed but session.call_id is
    set (a phone-xai-* session), the call attach WINS over any
    model/conversation_id args — grok_reconnect's post-P2.13 call shape
    (model=session.model or None, conversation_id=resume_id) must rejoin the
    live call, never silently demote it to a non-call ?model= session. The
    swallowed args are logged, not raised (they come from generic reconnect
    code, not a caller bug).

    Returns True if connection successful, False otherwise.
    """
    if call_id and (model or conversation_id):
        raise ValueError(
            "connect_to_grok: call_id is mutually exclusive with "
            "model/conversation_id (a SIP call attach carries no model or "
            "resumption params — xAI binds them server-side)")

    if not WEBSOCKETS_AVAILABLE:
        print("[GROK-LIVE] Cannot connect - websockets library not installed")
        return False

    if not XAI_API_KEY:
        print("[GROK-LIVE] Cannot connect - XAI_API_KEY not set")
        return False

    effective_call_id = call_id or getattr(session, "call_id", "")
    resolved_model = ""
    if effective_call_id:
        if model or conversation_id:
            print(f"[GROK-LIVE] session {session.session_id} has "
                  f"call_id={effective_call_id!r} — ignoring model/conversation_id "
                  f"(call-attach precedence)")
    else:
        # Resolve + validate model (allowlist from GROK_LIVE_MODELS) — P2.8
        _allowed_model_ids = {m["id"] for m in GROK_LIVE_MODELS}
        if model and model not in _allowed_model_ids:
            print(f"[GROK-LIVE] WARNING: model {model!r} not in GROK_LIVE_MODELS allowlist; falling back to default {GROK_LIVE_MODEL!r}")
            model = None
        resolved_model = model or GROK_LIVE_MODEL
        session.model = resolved_model

    try:
        headers = {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json"
        }

        if effective_call_id:
            # SIP call attach — call_id is the ONLY query param (XOR above).
            url = f"{GROK_LIVE_URL}?call_id={effective_call_id}"
            session.call_id = effective_call_id
        else:
            url = f"{GROK_LIVE_URL}?model={resolved_model}"
            if conversation_id:
                # Resumption: xAI replays cached turns for this conversation — P2.13
                url += f"&conversation_id={conversation_id}"

        print(f"[GROK-LIVE] Connecting to xAI: {url}")
        # websockets 15.x uses additional_headers instead of extra_headers
        # Add explicit ping settings to prevent connection drops
        session.grok_ws = await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
        session.status = "connected"
        session.last_activity = now_utc_iso()
        if effective_call_id:
            print(f"[GROK-LIVE] Connected to xAI for session {session.session_id} (call_id={effective_call_id})")
        else:
            print(f"[GROK-LIVE] Connected to xAI for session {session.session_id} (model={resolved_model})")
        return True

    except Exception as e:
        print(f"[GROK-LIVE] Connection failed: {e}")
        session.status = "error"
        return False
```

No change to `grok_reconnect` is needed: its P2.13 call shape (`model=session.model or None, conversation_id=resume_id`) hits the fallback branch on a `phone-xai-*` session and rejoins `?call_id=`; on portal sessions (`session.call_id == ""`) it behaves exactly as P2.13 landed it.

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_call_connect.py Orchestrator/tests/test_grok_live_p2.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.app; print('app imports OK')"`
Expected: PASS — 6 passed in test_xai_call_connect.py, the full P2 suite still green (proves the additive merge didn't regress P2.8/P2.13), and `app imports OK`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/models.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_xai_call_connect.py
git commit -m "feat(xai-phone): connect_to_grok accepts call_id XOR model/conversation_id; call sessions rejoin on reconnect"
```

---

### Task P5.5: Call attach bridge (webhook → GrokLiveSession, reaper-safe)

**Files:**
- Create: Orchestrator/xai_phone/call_bridge.py
- Modify: Orchestrator/routes/grok_live_routes.py:1221-1230 (gate keepalive silence off for call sessions)
- Test: Orchestrator/tests/test_xai_call_bridge.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_call_bridge.py
"""Webhook -> session attach flow with a fake xAI WS.

Verifies: registry entry keyed phone-xai-<call_id>; preset-driven configure;
listener+keepalive spawned; finalize mirrors the portal-WS finally block
(save via P1b path, disconnected status, last_activity stamped, payload
released); the never-reap-non-disconnected invariant holds throughout."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

import Orchestrator.routes.grok_live_routes as glr
from Orchestrator.live_session_reaper import is_reapable
from Orchestrator.models import GROK_LIVE_SESSIONS
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS  # P1b shared double
from Orchestrator.xai_phone import call_bridge


@pytest.fixture(autouse=True)
def clean_registry():
    GROK_LIVE_SESSIONS.clear()
    yield
    GROK_LIVE_SESSIONS.clear()


@pytest.fixture
def wired(monkeypatch):
    """Wire fakes for every grok_live_routes function call_bridge late-imports."""
    state = {"connects": [], "configured": {}, "saves": [], "hangup": asyncio.Event()}

    async def fake_connect(session, model=None, conversation_id=None, call_id=None):
        state["connects"].append(call_id)
        session.grok_ws = FakeUpstreamWS()
        session.status = "connected"
        if call_id:
            session.call_id = call_id
        return True

    async def fake_configure(session, operator, voice="Ara", custom_role=""):
        state["configured"].update(operator=operator, voice=voice, custom_role=custom_role)

    async def fake_listener(session):
        await state["hangup"].wait()          # "xAI closed the WS" when set

    async def fake_keepalive(session):
        await asyncio.sleep(3600)

    async def fake_save(session):
        state["saves"].append(session.session_id)

    monkeypatch.setattr(glr, "connect_to_grok", fake_connect)
    monkeypatch.setattr(glr, "configure_grok_session", fake_configure)
    monkeypatch.setattr(glr, "grok_listener", fake_listener)
    monkeypatch.setattr(glr, "grok_keepalive_loop", fake_keepalive)
    monkeypatch.setattr(glr, "save_grok_session_to_blackbox", fake_save)
    monkeypatch.setattr(call_bridge, "_resolve_default_preset",
                        lambda: {"voice": "Eve", "instructions": "Front-desk agent."})
    return state


@pytest.mark.asyncio
async def test_attach_creates_connected_session_with_preset(wired):
    sid = await call_bridge.attach_call("call-42")
    assert sid == "phone-xai-call-42"
    session = GROK_LIVE_SESSIONS[sid]
    assert session.status == "connected"
    assert session.call_id == "call-42"
    assert session.operator == "system"
    assert wired["connects"] == ["call-42"]
    assert wired["configured"] == {"operator": "system", "voice": "Eve",
                                   "custom_role": "Front-desk agent."}
    # never-reap-non-disconnected invariant: a live call (no portal_ws!) is safe
    assert not is_reapable(session, time.time() + 10_000)
    wired["hangup"].set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_finalize_on_ws_close_saves_and_becomes_reapable(wired):
    sid = await call_bridge.attach_call("call-7")
    session = GROK_LIVE_SESSIONS[sid]
    wired["hangup"].set()                     # call ends
    await asyncio.sleep(0.05)
    assert session.status == "disconnected"
    assert session.intentional_disconnect is True   # no reconnect churn on a dead call
    assert session.grok_ws is None
    assert wired["saves"] == [sid]                  # transcript persisted (P1b /chat/save path)
    now = datetime.now(timezone.utc).timestamp()
    assert not is_reapable(session, now)            # grace window holds
    assert is_reapable(session, now + 121)          # evicted after grace


@pytest.mark.asyncio
async def test_duplicate_webhook_does_not_double_attach(wired):
    sid1 = await call_bridge.attach_call("call-9")
    sid2 = await call_bridge.attach_call("call-9")
    assert sid1 == sid2
    assert wired["connects"] == ["call-9"]          # connected exactly once
    wired["hangup"].set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_connect_failure_leaves_reapable_session(wired, monkeypatch):
    async def failing_connect(session, model=None, conversation_id=None, call_id=None):
        return False
    monkeypatch.setattr(glr, "connect_to_grok", failing_connect)
    sid = await call_bridge.attach_call("call-dead")
    assert sid is None
    session = GROK_LIVE_SESSIONS["phone-xai-call-dead"]
    assert session.status == "disconnected"
    now = datetime.now(timezone.utc).timestamp()
    assert is_reapable(session, now + 121)


def test_no_default_preset_resolves_empty(monkeypatch):
    from Orchestrator.xai_phone import provisioning as pv
    monkeypatch.setattr(pv, "get_default_preset_id", lambda: None)
    assert call_bridge._resolve_default_preset() == {}
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_call_bridge.py -q`
Expected: FAIL with `ImportError: cannot import name 'call_bridge'`

**Step 3: Write minimal implementation**

Create `Orchestrator/xai_phone/call_bridge.py`:

```python
"""Attach an inbound xAI SIP call to a GrokLiveSession.

A verified `realtime.call.incoming` webhook (routes/xai_phone_routes.py) hands
us a call_id; opening wss://api.x.ai/v1/realtime?call_id={call_id} attaches
this process as the call's agent. AUDIO FLOWS xAI-SIDE: the caller's SIP leg
is the audio path — there is NO local audio pump (unlike phone/bridge.py's
Asterisk leg). We drive only session config, tool dispatch and transcripts
through the existing grok_live_routes machinery, with portal_ws left None —
every portal send goes through _safe_ws_send, which no-ops on None. This is
the same drive-without-a-portal-WS shape as phone/bridge.py:1863-1932
(_start_grok), which reuses connect_to_grok + configure_grok_session.

Reaper safety (live_session_reaper.py:48-66): while the call is live the
session keeps status="connected" and is NEVER reaped (the invariant protects
sessions without a portal_ws); _finalize_call flips it to "disconnected" and
stamps last_activity, so the reaper evicts it after the grace window.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from Orchestrator.live_session_reaper import release_payload
from Orchestrator.models import GROK_LIVE_SESSIONS, GrokLiveSession
from Orchestrator.volume import now_utc_iso
from Orchestrator.xai_phone import provisioning

logger = logging.getLogger(__name__)

# The inbound line is a system surface (outbound-call precedent in
# configure_grok_session's is_system_operator branch) unless the preset says otherwise.
DEFAULT_OPERATOR = "system"

_ACTIVE_STATUSES = ("connecting", "connected", "responding")


def _resolve_default_preset() -> dict:
    """Line default preset: default_preset_id in credentials/xai_phone.json
    -> P4 voice-agent preset registry.

    Guarded import: P5 keeps working if P4's registry is absent (fresh box,
    partial deploy) — falls back to empty defaults. NOTE: adjust the import
    below if P4 landed its registry under a different module path.
    """
    preset_id = provisioning.get_default_preset_id()
    if not preset_id:
        return {}
    try:
        from Orchestrator.voice_agents.registry import get_preset  # P4 module
    except ImportError:
        logger.warning("[XAI-PHONE] voice-agent preset registry unavailable; using defaults")
        return {}
    preset = get_preset(preset_id)
    if not preset:
        logger.warning("[XAI-PHONE] default_preset_id %r not found; using defaults", preset_id)
        return {}
    return preset


async def attach_call(call_id: str, payload: Optional[dict] = None) -> Optional[str]:
    """Attach to an incoming xAI SIP call. Returns the session_id, or None on failure."""
    # Late imports: grok_live_routes imports half the Orchestrator — keep this
    # module cheap to import for the webhook route and for tests.
    from Orchestrator.config import GROK_LIVE_DEFAULT_VOICE
    from Orchestrator.routes.grok_live_routes import (
        configure_grok_session,
        connect_to_grok,
        grok_keepalive_loop,
        grok_listener,
        save_grok_session_to_blackbox,
    )

    session_id = f"phone-xai-{call_id}"
    existing = GROK_LIVE_SESSIONS.get(session_id)
    if existing and existing.status in _ACTIVE_STATUSES:
        logger.warning("[XAI-PHONE] duplicate incoming webhook for %s — ignoring", call_id)
        return session_id

    preset = _resolve_default_preset()
    session = GrokLiveSession(
        session_id=session_id,
        operator=preset.get("created_by") or DEFAULT_OPERATOR,
        status="connecting",
        created_at=now_utc_iso(),
        call_id=call_id,
    )
    GROK_LIVE_SESSIONS[session_id] = session

    if not await connect_to_grok(session, call_id=call_id):
        session.status = "disconnected"
        session.last_activity = now_utc_iso()  # reaper evicts after grace
        logger.error("[XAI-PHONE] failed to attach to call %s", call_id)
        return None

    await configure_grok_session(
        session,
        session.operator,
        voice=preset.get("voice") or GROK_LIVE_DEFAULT_VOICE,
        custom_role=preset.get("instructions") or "",
    )

    listener_task = asyncio.create_task(grok_listener(session))
    keepalive_task = asyncio.create_task(grok_keepalive_loop(session))
    asyncio.create_task(
        _finalize_call(session, listener_task, keepalive_task, save_grok_session_to_blackbox)
    )
    logger.info("[XAI-PHONE] attached to call %s as session %s", call_id, session_id)
    return session_id


async def _finalize_call(session, listener_task, keepalive_task, save_fn) -> None:
    """Teardown when xAI closes the call WS (hangup/transfer/drop).

    Mirrors the portal-WS finally block (grok_live_routes.py:1440-1471):
    save transcript (P1b /chat/save path), close, mark disconnected, stamp
    last_activity (starts the reaper grace clock), release buffers.
    """
    try:
        await listener_task
    except asyncio.CancelledError:
        pass
    finally:
        # A hung-up call_id is dead — suppress reconnect churn from the
        # listener's close handler / keepalive stale detection.
        session.intentional_disconnect = True
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass
        try:
            await save_fn(session)
        except Exception as e:
            logger.error("[XAI-PHONE] transcript save failed for %s: %s", session.session_id, e)
        if session.grok_ws:
            try:
                await session.grok_ws.close()
            except Exception:
                pass
            session.grok_ws = None
        session.status = "disconnected"
        session.last_activity = now_utc_iso()  # start reaper grace clock
        release_payload(session)
        logger.info("[XAI-PHONE] call session %s finalized", session.session_id)
```

Also modify `Orchestrator/routes/grok_live_routes.py` (keepalive loop, lines 1221-1230 pre-P2 — anchor on the `# Send keepalive` comment): the 20ms-silence injection writes into `input_audio_buffer.append`, but on a SIP-attached call the audio path is the SIP leg — injected silence could corrupt live call audio (UNCERTAIN; flagged for live validation in P5.8). Gate it, keeping stale-detection intact. Edit (old → new; the outer comment's stale "PCM16@24kHz" is corrected while we're here — P2.15 Branch A made the input rate 16kHz and already fixed the inner byte-count comment to 640 bytes @16k):

```python
            # Send keepalive: 20ms of silence as PCM16@24kHz
            try:
                if session.grok_ws:
```
→
```python
            # Send keepalive: 20ms of PCM16 silence at the declared input
            # rate (16kHz post-P2.15 — byte count below matches).
            # SKIPPED for SIP-attached calls (session.call_id set): the call's
            # audio flows xAI-side and injected buffer silence could corrupt it
            # (uncertain per xAI docs — live-validated in P5.8). Stale detection
            # above still applies.
            try:
                if session.grok_ws and not session.call_id:
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_call_bridge.py Orchestrator/tests/test_xai_call_connect.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.app; print('app imports OK')"`
Expected: PASS (11 passed — 5 bridge + 6 connect) and `app imports OK`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/xai_phone/call_bridge.py Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_xai_call_bridge.py
git commit -m "feat(xai-phone): call attach bridge — webhook call_id to reaper-safe GrokLiveSession"
```

---

### Task P5.6: Webhook endpoint POST /xai/voice/incoming

**Files:**
- Modify: Orchestrator/routes/xai_phone_routes.py (append webhook route; file created in P5.3)
- Test: Orchestrator/tests/test_xai_voice_webhook.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_voice_webhook.py
"""POST /xai/voice/incoming — the only publicly-funneled path.
Unsigned/stale/replayed => 401 before any processing; unprovisioned => 503;
verified realtime.call.incoming => spawns attach; other events acked, ignored."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.xai_phone.signature as sig_mod
from Orchestrator.routes import xai_phone_routes as xr
from Orchestrator.xai_phone import provisioning as pv

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-long!!").decode()


def sign(body: bytes, msg_id: str, ts: str | None = None):
    ts = str(int(time.time())) if ts is None else ts
    key = base64.b64decode(SECRET[len("whsec_"):])
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": "v1," + base64.b64encode(mac).decode(),
    }


@pytest.fixture(autouse=True)
def fresh_replay_cache(monkeypatch):
    monkeypatch.setattr(sig_mod, "_default_replay_cache", sig_mod.ReplayCache())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "STORE_PATH", str(tmp_path / "xai_phone.json"))
    pv._write_store({"version": 1, "phone_number": "+15550100",
                     "webhook_url": "https://x/hook", "signing_secret": SECRET})


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(xr, "_spawn_attach", lambda call_id, event: calls.append(call_id))
    return calls


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(xr.router)
    return TestClient(app)


def test_verified_incoming_call_spawns_attach(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body, headers=sign(body, "m1"))
    assert r.status_code == 200
    assert r.json()["handled"] is True
    assert spawned == ["call-123"]


def test_unsigned_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body)
    assert r.status_code == 401
    assert spawned == []


def test_tampered_body_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    headers = sign(body, "m2")
    r = client.post("/xai/voice/incoming", content=body + b" ", headers=headers)
    assert r.status_code == 401
    assert spawned == []


def test_stale_timestamp_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body,
                    headers=sign(body, "m3", ts=str(int(time.time()) - 400)))
    assert r.status_code == 401
    assert spawned == []


def test_replay_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    headers = sign(body, "m4")
    assert client.post("/xai/voice/incoming", content=body, headers=headers).status_code == 200
    assert client.post("/xai/voice/incoming", content=body, headers=headers).status_code == 401
    assert spawned == ["call-123"]                 # attached exactly once


def test_other_event_types_acked_not_attached(client, store, spawned):
    body = json.dumps({"type": "realtime.call.ended", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body, headers=sign(body, "m5"))
    assert r.status_code == 200
    assert r.json()["handled"] is False
    assert spawned == []


def test_unprovisioned_returns_503(client, tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(pv, "STORE_PATH", str(tmp_path / "empty.json"))
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "c"}).encode()
    r = client.post("/xai/voice/incoming", content=body)
    assert r.status_code == 503
    assert spawned == []


def test_missing_call_id_rejected_400(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming"}).encode()
    r = client.post("/xai/voice/incoming", content=body, headers=sign(body, "m6"))
    assert r.status_code == 400
    assert spawned == []
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voice_webhook.py -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_spawn_attach'` (and 404s)

**Step 3: Write minimal implementation**

In `Orchestrator/routes/xai_phone_routes.py`: extend the imports at the top —

```python
import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from Orchestrator.xai_phone import provisioning
from Orchestrator.xai_phone.signature import verify_signature
```

— and append at the end of the file:

```python
# =============================================================================
# Signed telephony webhook (the ONLY publicly exposed path — see
# scripts/xai_phone_funnel.sh; everything else on :9091 stays tailnet-only)
# =============================================================================

def _spawn_attach(call_id: str, event: dict) -> None:
    """Fire-and-forget call attach. Module-level so tests monkeypatch it;
    late import keeps the route importable without the grok stack."""
    from Orchestrator.xai_phone.call_bridge import attach_call
    asyncio.create_task(attach_call(call_id, event))


@router.post("/voice/incoming")
async def xai_voice_incoming(request: Request):
    """xAI telephony webhook (Standard Webhooks HMAC scheme).

    Verification order is deliberate: raw body read -> signature check
    (constant-time, ±5min tolerance, replay-guarded) -> ONLY then JSON parse.
    Unsigned/stale/replayed requests get a generic 401 (reason logged
    server-side, never echoed). A webhook must be answered fast — the call
    attach runs as a background task.
    """
    body = await request.body()

    secret = provisioning.get_signing_secret()
    if not secret:
        # Fail closed, but distinguishable from a bad signature so a funnel
        # preflight against an unprovisioned box is diagnosable.
        raise HTTPException(status_code=503, detail="xAI phone line not provisioned")

    ok, reason = verify_signature(secret, dict(request.headers), body)
    if not ok:
        logger.warning("[XAI-PHONE] rejected webhook: %s", reason)
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event_type = str(event.get("type", ""))
    if event_type != "realtime.call.incoming":
        logger.info("[XAI-PHONE] ignoring webhook event type %r", event_type)
        return {"ok": True, "handled": False}

    call_id = str(event.get("call_id") or (event.get("data") or {}).get("call_id") or "")
    if not call_id:
        raise HTTPException(status_code=400, detail="missing call_id")

    _spawn_attach(call_id, event)
    return {"ok": True, "handled": True, "session_id": f"phone-xai-{call_id}"}
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voice_webhook.py Orchestrator/tests/test_xai_phone_routes.py -q && Orchestrator/venv/bin/python -c "import Orchestrator.app; print('app imports OK')"`
Expected: PASS (14 passed) and `app imports OK`

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/routes/xai_phone_routes.py Orchestrator/tests/test_xai_voice_webhook.py
git commit -m "feat(xai-phone): signed /xai/voice/incoming webhook — verify-then-attach, 401 fail-closed"
```

---

### Task P5.7: Call-control tools — transfer_call + hangup_call (ToolVault modules)

**Files:**
- Create: Orchestrator/xai_phone/call_control.py
- Create: ToolVault/tools/transfer_call/schema.json
- Create: ToolVault/tools/transfer_call/executor.py
- Create: ToolVault/tools/hangup_call/schema.json
- Create: ToolVault/tools/hangup_call/executor.py
- Test: Orchestrator/tests/test_xai_call_control.py

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_xai_call_control.py
"""transfer_call / hangup_call — scoped to an active xAI call session.

REST endpoints (recon xaiResearch.json api_details):
    POST /v1/realtime/calls/{call_id}/refer  {"target_uri": ...}
    POST /v1/realtime/calls/{call_id}/hangup
"""
import pytest

from Orchestrator.models import GROK_LIVE_SESSIONS, GrokLiveSession
from Orchestrator.xai_phone import call_control as cc


@pytest.fixture(autouse=True)
def clean_registry():
    GROK_LIVE_SESSIONS.clear()
    yield
    GROK_LIVE_SESSIONS.clear()


def _add_call(call_id: str, status: str = "connected"):
    sid = f"phone-xai-{call_id}"
    GROK_LIVE_SESSIONS[sid] = GrokLiveSession(session_id=sid, call_id=call_id, status=status)


@pytest.fixture
def posted(monkeypatch):
    calls = []

    async def fake_post(call_id, action, payload=None):
        calls.append((call_id, action, payload))
        return True, f"{action} accepted for call {call_id}"

    monkeypatch.setattr(cc, "_call_post", fake_post)
    return calls


# --------------------------------------------------------------- scope guard

@pytest.mark.asyncio
async def test_no_active_call_fails_gracefully(posted):
    ok, msg = await cc.hangup_call()
    assert not ok and "No active xAI phone call" in msg
    assert posted == []


@pytest.mark.asyncio
async def test_disconnected_session_is_not_active(posted):
    _add_call("c1", status="disconnected")
    ok, msg = await cc.hangup_call()
    assert not ok and posted == []


@pytest.mark.asyncio
async def test_single_active_call_resolved_implicitly(posted):
    _add_call("c1")
    ok, msg = await cc.hangup_call()
    assert ok
    assert posted == [("c1", "hangup", None)]


@pytest.mark.asyncio
async def test_multiple_active_calls_require_explicit_call_id(posted):
    _add_call("c1")
    _add_call("c2")
    ok, msg = await cc.hangup_call()
    assert not ok and "Multiple active calls" in msg
    ok, _ = await cc.hangup_call(call_id="c2")
    assert ok and posted == [("c2", "hangup", None)]


@pytest.mark.asyncio
async def test_explicit_unknown_call_id_rejected(posted):
    _add_call("c1")
    ok, msg = await cc.hangup_call(call_id="nope")
    assert not ok and "not an active xAI call" in msg
    assert posted == []


# ----------------------------------------------------------------- transfer

@pytest.mark.asyncio
async def test_transfer_requires_target_uri(posted):
    _add_call("c1")
    ok, msg = await cc.transfer_call("")
    assert not ok and "target_uri" in msg
    assert posted == []


@pytest.mark.asyncio
async def test_transfer_posts_refer_with_target(posted):
    _add_call("c1")
    ok, _ = await cc.transfer_call("tel:+15550100")
    assert ok
    assert posted == [("c1", "refer", {"target_uri": "tel:+15550100"})]


# --------------------------------------------------------- toolvault modules

@pytest.mark.asyncio
async def test_executors_load_and_dispatch(posted, monkeypatch):
    from Orchestrator.toolvault import registry
    from Orchestrator.toolvault.context import ToolContext

    _add_call("c1")
    ctx = ToolContext(operator="system", base_url="http://localhost:9091")

    transfer = registry.get_executor("transfer_call")
    hangup = registry.get_executor("hangup_call")
    assert transfer and hangup

    res = await transfer({"target_uri": "tel:+15550100"}, ctx)
    assert res.success
    res = await hangup({}, ctx)
    assert res.success
    assert [(c, a) for c, a, _ in posted] == [("c1", "refer"), ("c1", "hangup")]


@pytest.mark.asyncio
async def test_executor_fails_gracefully_outside_call():
    from Orchestrator.toolvault import registry
    from Orchestrator.toolvault.context import ToolContext

    hangup = registry.get_executor("hangup_call")
    res = await hangup({}, ToolContext(operator="system", base_url="http://localhost:9091"))
    assert res.success is False
    assert "No active xAI phone call" in res.result
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_call_control.py -q`
Expected: FAIL with `ImportError: cannot import name 'call_control'`

**Step 3: Write minimal implementation**

Create `Orchestrator/xai_phone/call_control.py`:

```python
"""In-call control for the xAI sovereign line: transfer (SIP REFER) + hangup.

    POST https://api.x.ai/v1/realtime/calls/{call_id}/refer  {"target_uri": ...}
    POST https://api.x.ai/v1/realtime/calls/{call_id}/hangup

Scoped BY DESIGN to an active xAI call: callers may omit call_id, in which
case the single active phone-xai-* session supplies it; with zero (or 2+)
active calls and no explicit call_id the operation fails gracefully. This is
the session-context check that keeps the tools inert outside a live call.
Twilio calls are a separate line and are NOT controllable here.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from Orchestrator.config import XAI_API_KEY
from Orchestrator.models import GROK_LIVE_SESSIONS
from Orchestrator.xai_phone.provisioning import XAI_API_BASE

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("connecting", "connected", "responding")


def active_call_ids() -> list[str]:
    """call_ids of live xAI SIP call sessions (phone-xai-* and not torn down)."""
    return [
        s.call_id
        for s in GROK_LIVE_SESSIONS.values()
        if getattr(s, "call_id", "")
        and s.session_id.startswith("phone-xai-")
        and s.status in _ACTIVE_STATUSES
    ]


def _resolve_call_id(call_id: Optional[str]) -> tuple[Optional[str], str]:
    active = active_call_ids()
    if call_id:
        if call_id not in active:
            return None, f"call_id {call_id!r} is not an active xAI call (active: {active or 'none'})"
        return call_id, ""
    if not active:
        return None, ("No active xAI phone call — transfer_call/hangup_call only work "
                      "inside a live xAI phone-line session")
    if len(active) > 1:
        return None, f"Multiple active calls ({', '.join(active)}) — pass call_id explicitly"
    return active[0], ""


async def _call_post(call_id: str, action: str, payload: Optional[dict] = None) -> tuple[bool, str]:
    """POST a call-control action. Module-level so tests monkeypatch it."""
    if not XAI_API_KEY:
        return False, "XAI_API_KEY not configured"
    url = f"{XAI_API_BASE}/v1/realtime/calls/{call_id}/{action}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json=payload if payload is not None else {},
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            )
        if resp.status_code >= 400:
            return False, f"xAI {action} failed: HTTP {resp.status_code} {resp.text[:200]}"
        return True, f"{action} accepted for call {call_id}"
    except httpx.HTTPError as exc:
        return False, f"xAI {action} request error: {exc}"


async def transfer_call(target_uri: str, call_id: Optional[str] = None) -> tuple[bool, str]:
    if not target_uri:
        return False, "target_uri is required (e.g. tel:+15550100 or sip:agent@example.com)"
    resolved, err = _resolve_call_id(call_id)
    if not resolved:
        return False, err
    return await _call_post(resolved, "refer", {"target_uri": target_uri})


async def hangup_call(call_id: Optional[str] = None) -> tuple[bool, str]:
    resolved, err = _resolve_call_id(call_id)
    if not resolved:
        return False, err
    return await _call_post(resolved, "hangup")
```

Create `ToolVault/tools/transfer_call/schema.json`:

```json
{
  "name": "transfer_call",
  "description": "Transfer the CURRENT live xAI phone-line call to another phone number or SIP destination (SIP REFER). Only works while an xAI sovereign-line call is active — use when the caller asks to be transferred, e.g. 'put me through to +1-555-0100'. Does NOT control Twilio calls.",
  "category": "telephony",
  "groups": ["chat", "grok_live"],
  "tier": 2,
  "parameters": {
    "type": "object",
    "properties": {
      "target_uri": {
        "type": "string",
        "description": "Transfer destination: tel:+15550100 or sip:agent@example.com."
      },
      "call_id": {
        "type": "string",
        "description": "xAI call id. Omit to target the single active call (the normal case)."
      }
    },
    "required": ["target_uri"]
  },
  "returns": "Confirmation the transfer was accepted, or a clear failure reason.",
  "example": "transfer_call(target_uri=\"tel:+15550100\")",
  "notes": "Scoped to the xAI sovereign phone line (phone-xai-* sessions): fails gracefully when no xAI call is active, and requires an explicit call_id if more than one call is live."
}
```

Create `ToolVault/tools/transfer_call/executor.py`:

```python
"""Executor for transfer_call — SIP REFER on the active xAI phone-line call."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.xai_phone.call_control import transfer_call


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    target_uri = str(params.get("target_uri", "")).strip()
    call_id = str(params.get("call_id", "")).strip() or None
    ok, message = await transfer_call(target_uri, call_id=call_id)
    return ToolResult(success=ok, result=message)
```

Create `ToolVault/tools/hangup_call/schema.json`:

```json
{
  "name": "hangup_call",
  "description": "End the CURRENT live xAI phone-line call. Only works while an xAI sovereign-line call is active — use when the conversation is complete or the caller asks to hang up. Does NOT control Twilio calls.",
  "category": "telephony",
  "groups": ["chat", "grok_live"],
  "tier": 2,
  "parameters": {
    "type": "object",
    "properties": {
      "call_id": {
        "type": "string",
        "description": "xAI call id. Omit to target the single active call (the normal case)."
      }
    },
    "required": []
  },
  "returns": "Confirmation the hangup was accepted, or a clear failure reason.",
  "example": "hangup_call()",
  "notes": "Scoped to the xAI sovereign phone line (phone-xai-* sessions): fails gracefully when no xAI call is active, and requires an explicit call_id if more than one call is live."
}
```

Create `ToolVault/tools/hangup_call/executor.py`:

```python
"""Executor for hangup_call — end the active xAI phone-line call."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.xai_phone.call_control import hangup_call


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    call_id = str(params.get("call_id", "")).strip() or None
    ok, message = await hangup_call(call_id=call_id)
    return ToolResult(success=ok, result=message)
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_call_control.py -q && Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate && curl -s -X POST http://localhost:9091/toolvault/reload`
Expected: PASS (10 passed); validator exits 0 with both new modules valid; reload returns JSON (tools live in grok_live sessions via the group injection at grok_live_routes.py:88 — post-P1's unfreeze they reach live sessions without restart; the response.function_call_arguments.done catch-all at grok_live_routes.py:983-991 dispatches them)

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add Orchestrator/xai_phone/call_control.py ToolVault/tools/transfer_call/schema.json ToolVault/tools/transfer_call/executor.py ToolVault/tools/hangup_call/schema.json ToolVault/tools/hangup_call/executor.py Orchestrator/tests/test_xai_call_control.py
git commit -m "feat(xai-phone): transfer_call + hangup_call ToolVault tools scoped to active xAI call sessions"
```

---

### Task P5.8: Public exposure (Tailscale Funnel) + live provisioning validation

Pure-config/probe task — no unit tests; exact verification commands instead. Follows the MCP-remote topology (MCP/deploy/REMOTE_SETUP.md): **:9091 stays tailnet-only**; Funnel exposes ONLY the HMAC-authed webhook path, on its own funnel port (10000 — 8443 is taken by the MCP server; funnel supports 443/8443/10000 only). The design's gate "verify signed-webhook verification before exposing" is satisfied by P5.1/P5.6 landing first — do NOT run this task until they are committed.

**Files:**
- Create: scripts/xai_phone_funnel.sh

**Step 1: Write the script**

Create `scripts/xai_phone_funnel.sh` (then `chmod +x scripts/xai_phone_funnel.sh`):

```bash
#!/usr/bin/env bash
# xAI sovereign phone line — public webhook exposure via Tailscale Funnel.
#
# TOPOLOGY (mirrors MCP/deploy/REMOTE_SETUP.md):
#   * Backend :9091 stays TAILNET-ONLY (it has no app-layer auth by design —
#     Tailscale is the perimeter).
#   * Funnel exposes ONLY the path /xai/voice/incoming, on its own funnel
#     port (10000; 8443 is the MCP server). That endpoint is HMAC-authed
#     (Standard Webhooks signatures, 401 fail-closed), so it — and nothing
#     else on :9091 — is safe to make public.
#
# ORDER OF OPERATIONS (first-time setup):
#   1. scripts/xai_phone_funnel.sh up            -> prints the public webhook URL
#   2. curl -s -X POST http://localhost:9091/xai/phone/provision \
#        -H 'Content-Type: application/json' \
#        -d '{"name":"BlackBox line","webhook_url":"<URL FROM STEP 1>"}'
#   3. scripts/xai_phone_funnel.sh status        -> preflight must report ok:true
#
# Usage: scripts/xai_phone_funnel.sh {up|down|status}
set -euo pipefail

PORT=10000
WEBHOOK_PATH="/xai/voice/incoming"
BACKEND="http://127.0.0.1:9091${WEBHOOK_PATH}"

cmd="${1:-status}"
host=$(tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
public_url="https://${host}:${PORT}${WEBHOOK_PATH}"

case "$cmd" in
  up)
    tailscale funnel --bg --https="${PORT}" --set-path="${WEBHOOK_PATH}" "${BACKEND}"
    echo "Public webhook URL: ${public_url}"
    echo "Verify (expect 503 before provisioning, 401 after):"
    echo "  curl -s -o /dev/null -w '%{http_code}\\n' -X POST ${public_url}"
    ;;
  down)
    tailscale funnel --https="${PORT}" --set-path="${WEBHOOK_PATH}" off
    echo "Funnel route removed for ${WEBHOOK_PATH}"
    ;;
  status)
    tailscale funnel status
    echo "--- backend preflight (GET /xai/phone/status?preflight=true) ---"
    curl -s "http://127.0.0.1:9091/xai/phone/status?preflight=true" | python3 -m json.tool
    ;;
  *)
    echo "usage: $0 {up|down|status}" >&2
    exit 1
    ;;
esac
```

**Step 2: Verify the funnel exposes ONLY the webhook path**
Run:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && chmod +x scripts/xai_phone_funnel.sh && scripts/xai_phone_funnel.sh up && tailscale funnel status
```
Expected: funnel status shows `https://<host>.ts.net:10000` with the single path `/xai/voice/incoming` → `http://127.0.0.1:9091/xai/voice/incoming`; `:443` unchanged; `:8443` (MCP) unchanged. Then confirm the perimeter holds — a non-webhook backend path must NOT be publicly reachable:
```bash
H=$(tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://$H:10000/xai/voice/incoming"   # expect 503 (unprovisioned) or 401 (provisioned) — endpoint live, fail-closed
curl -s -o /dev/null -w '%{http_code}\n' "https://$H:10000/timeline"                     # expect 404 — nothing else on :9091 is exposed
```

**Step 3: Live provisioning (one-shot; consumes the account's free number)**
Run (with the URL printed by step 2):
```bash
curl -s -X POST http://localhost:9091/xai/phone/provision -H 'Content-Type: application/json' \
  -d "{\"name\":\"BlackBox line\",\"webhook_url\":\"https://$H:10000/xai/voice/incoming\"}" | python3 -m json.tool
```
Expected: `"provisioned": true` with a real `phone_number` and `"has_signing_secret": true`. **If the API rejects `origin`** (P5.2's flagged uncertainty), read the error, check `credentials/xai_phone.json` was NOT written, adjust `ORIGIN_PROVISIONED` in Orchestrator/xai_phone/provisioning.py to the documented value, and retry. If `has_signing_secret` is false, extract the secret's actual field name from `raw_response` in `credentials/xai_phone.json` and extend `_extract_secret`.

**Step 4: End-to-end preflight + live call smoke**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && scripts/xai_phone_funnel.sh status`
Expected: `webhook_preflight.ok: true` (unsigned public POST → 401).
Then the live smoke (requires a human): call the provisioned number from a real phone. Verify in order:
1. `journalctl -u blackbox.service -f | grep XAI-PHONE` shows `rejected webhook` NEVER firing for the real call, and `attached to call <id> as session phone-xai-<id>`;
2. the agent answers and converses (this validates the audio-flows-xAI-side assumption — no local pump);
3. ask the agent to hang up → `hangup accepted` in the log and the call ends (validates the call-control tools in-session);
4. after hangup: `curl -s http://localhost:9091/grok-live/sessions` shows the phone-xai session `disconnected`, and it disappears within ~3 minutes (reaper grace 120s + sweep 60s);
5. the transcript snapshot appears via the P1b `/chat/save` path (check `list_recent_snapshots` / journalctl for the save);
6. watch for audio artifacts every ~15s — if present, confirm the keepalive silence gate (`not session.call_id`, P5.5) is active in the running service.
Record any deviations (origin value, secret field name, keepalive behavior, reconnect-with-call_id semantics) in the plan doc's P0 probe notes.

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add scripts/xai_phone_funnel.sh
git commit -m "feat(xai-phone): path-scoped Tailscale Funnel script for the signed voice webhook"
```

---

## Phase 6a — Translation voice mode

New 4th voice mode on the OpenAI + Gemini bridges (Grok greyed out — no xAI translate model exists). `mode=translate&target_language=<BCP-47>` accepted on `/ws/realtime` and `/ws/gemini-live` as URL query param AND JSON connect field (same precedence as existing params: JSON wins, URL fills missing). OpenAI binds `gpt-realtime-translate` at the upstream WS URL; Gemini binds `models/gemini-3.5-live-translate-preview` in setup. Translation sessions branch BEFORE the persona/context build: minimal instructions, NO tool declarations, no snapshot context — fastest possible setup. All changes additive and gated on `mode == "translate"`; the default voice path is pinned by regression tests.

> Line numbers below were verified 2026-07-11 against the pre-P1 tree. Phases P1–P5 land first and WILL drift them — every edit also gives a unique code anchor. Trust the anchor, re-verify the line.

### Task P6.1: P0 probe-results gate (no code)

**Files:**
- Read: diagnostics/voice_probes/results/ (created by P0)

This is a verification-only gate. Phase 6a is probe-gated per the design doc ("Translation models' wire shapes unverified — P0 probes gate the feature").

**Step 1: Confirm the P0 translate probe results exist**

Run:
```bash
ls -la "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/" | grep -i translate
```
Expected: the single COMBINED translate probe result file written by P0.5 — `2026-07-11-translate.json` (date stamp = the actual P0 run date). One file contains BOTH probe entries: the OpenAI `gpt-realtime-translate` handshake AND the Gemini `gemini-3.5-live-translate-preview` setup variants.

**Step 2: Read the combined probe JSON and record the confirmed facts**

Run:
```bash
cat "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/"*translate*
```
Confirm and write down (you will need these in P6.3/P6.5/P6.9):
1. OpenAI: `gpt-realtime-translate` accepted at `wss://api.openai.com/v1/realtime?model=gpt-realtime-translate` (no close-4000), and whether the GA `session.update` shape (`session.type: "realtime"`, `instructions`, `audio.input/output`, no `tools`) was accepted or whether the probe found a different shape (e.g. a dedicated target-language field).
2. Gemini: `gemini-3.5-live-translate-preview` did NOT close 1008 "model not found" (this model is single-source recon — gemini-skills repo only), and whether a plain BidiGenerateContentSetup with `systemInstruction` + no `tools` reached `setupComplete`.

**Step 3: Gate decision**

- Both probes PASS with the assumed shapes → proceed to P6.2 exactly as written.
- A probe passed but with a DIFFERENT session shape → proceed, but flag P6.9 (the adaptation task) as mandatory and carry the probe's actual field names forward.
- OpenAI probe FAILED (model rejected) → STOP Phase 6a entirely, report to Brandon.
- Gemini probe FAILED (model not found) → proceed with the OpenAI half only (P6.2–P6.4, P6.7/P6.8 with the Gemini toggle also greyed out); P6.5/P6.6 are skipped and P6.9 records the decision.

No commit (nothing changed).

---

### Task P6.2: Shared translate helper module + config constants

**Files:**
- Create: Orchestrator/routes/voice_translate.py
- Modify: Orchestrator/config.py:500 (after `OPENAI_REALTIME_MODEL`) and :540 (after `GEMINI_LIVE_MODEL`)
- Test: Orchestrator/tests/test_voice_translate.py (new file)

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_voice_translate.py`:

```python
"""P6a — Translation voice mode: helper validation + minimal-session invariants.

Conventions mirror Orchestrator/tests/test_live_models.py (stubbed fossil
context, MagicMock sessions, single-send payload extraction).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# -----------------------------------------------------------------------------
# Shared fixtures/helpers (used by P6.3-P6.6 tests appended below)
# -----------------------------------------------------------------------------

@pytest.fixture
def stub_fossil_context(monkeypatch):
    """Stub build_fossil_context in both route modules (no real snapshot I/O)."""
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _stub)
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub)


def _boom(*args, **kwargs):
    raise AssertionError(
        "build_fossil_context must NEVER be called in translate mode "
        "(translate branch must run BEFORE the persona/context build)")


def _make_openai_session():
    session = MagicMock()
    session.openai_ws = MagicMock()
    session.openai_ws.send = AsyncMock()
    session.provenance = {}
    session.context_injected = False
    return session


def _make_gemini_session():
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.context_injected = False
    session.voice = ""
    return session


def _extract_payload(send_mock):
    assert send_mock.await_count == 1, (
        f"expected exactly one upstream send, got {send_mock.await_count}")
    return json.loads(send_mock.await_args.args[0])


# -----------------------------------------------------------------------------
# P6.2 — resolve_translate_params / build_translate_instructions / constants
# -----------------------------------------------------------------------------

def test_translate_model_constants():
    from Orchestrator.config import (
        OPENAI_REALTIME_TRANSLATE_MODEL, GEMINI_LIVE_TRANSLATE_MODEL,
        OPENAI_REALTIME_MODELS,
    )
    assert OPENAI_REALTIME_TRANSLATE_MODEL == "gpt-realtime-translate"
    assert GEMINI_LIVE_TRANSLATE_MODEL == "gemini-3.5-live-translate-preview"
    # connect_to_openai validates against the allowlist — the translate model
    # MUST be present there or translate sessions silently bind the chat default.
    assert OPENAI_REALTIME_TRANSLATE_MODEL in {m["id"] for m in OPENAI_REALTIME_MODELS}


def test_gemini_translate_model_not_in_chat_dropdown():
    from Orchestrator.config import GEMINI_LIVE_MODELS, GEMINI_LIVE_TRANSLATE_MODEL
    assert GEMINI_LIVE_TRANSLATE_MODEL not in {m["id"] for m in GEMINI_LIVE_MODELS}


def test_mode_translate_recognized():
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", "es") == (True, "es")


def test_non_translate_modes_pass_through():
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params(None, "es")[0] is False
    assert resolve_translate_params("", "es")[0] is False
    assert resolve_translate_params("normal", "es")[0] is False


def test_bcp47_region_subtags_accepted():
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", "pt-BR") == (True, "pt-BR")
    assert resolve_translate_params("translate", "zh-CN") == (True, "zh-CN")


def test_invalid_target_language_falls_back_to_en(capsys):
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", "not a lang!!") == (True, "en")
    out = capsys.readouterr().out
    assert "WARNING" in out and "target_language" in out


def test_missing_target_language_falls_back_to_en(capsys):
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", None) == (True, "en")


def test_instructions_are_minimal_and_name_the_language():
    from Orchestrator.routes.voice_translate import build_translate_instructions
    text = build_translate_instructions("fr")
    assert "fr" in text
    assert "translat" in text.lower()
    assert len(text) < 1000  # minimal by design — NOT the persona build
```

**Step 2: Run test to verify it fails**

Run (from repo root `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`):
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py -x -q
```
Expected: FAIL with `ImportError: cannot import name 'OPENAI_REALTIME_TRANSLATE_MODEL'` (first test collected).

**Step 3: Write minimal implementation**

3a. In `Orchestrator/config.py`, directly below `OPENAI_REALTIME_MODEL = ...` (line 500, anchor: `# Newest GA, valid since Beta header dropped`):

```python
# P6a translation voice mode — dedicated realtime translation model
# ($0.034/min, 16K ctx). Also listed in OPENAI_REALTIME_MODELS with
# category="translate" (hidden from the chat dropdown); MUST stay in that
# allowlist — connect_to_openai validates against it.
OPENAI_REALTIME_TRANSLATE_MODEL = os.getenv(
    "OPENAI_REALTIME_TRANSLATE_MODEL", "gpt-realtime-translate")
```

3b. In `Orchestrator/config.py`, directly below `GEMINI_LIVE_MODEL = ...` (line 540, anchor: `# GA-track alias - bumped from -preview-12-2025`):

```python
# P6a translation voice mode — dedicated Gemini Live translation model.
# Single-source recon (google-gemini/gemini-skills), P0-probe-gated
# (diagnostics/voice_probes/results/). Deliberately NOT in GEMINI_LIVE_MODELS
# (kept out of the chat dropdown); the translate branch binds it directly.
GEMINI_LIVE_TRANSLATE_MODEL = os.getenv(
    "GEMINI_LIVE_TRANSLATE_MODEL", "gemini-3.5-live-translate-preview")
```

3c. Create `Orchestrator/routes/voice_translate.py`:

```python
"""
Translation voice mode — shared helpers (P6a, design doc
docs/plans/2026-07-11-voice-agent-upgrade-pass-design.md workstream 5).

/ws/realtime and /ws/gemini-live both accept mode=translate +
target_language=<BCP-47>. This module owns validation and the minimal
translation prompt. Grok has NO translate model — grok_live_routes must
not import this.

UI pickers hardcode a top-20 list + free-text "Other"; the backend
therefore validates SHAPE only (any well-formed BCP-47 tag passes),
never membership.
"""
import re
from typing import Optional, Tuple

TRANSLATE_MODE = "translate"

DEFAULT_TARGET_LANGUAGE = "en"

# Language-tag shape check: primary subtag (2-3 letters) + optional subtags.
# Deliberately permissive — membership is a UI concern.
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def resolve_translate_params(
    mode: Optional[str],
    target_language: Optional[str],
    log_prefix: str = "[VOICE-TRANSLATE]",
) -> Tuple[bool, str]:
    """Validate client-supplied mode + target_language.

    Returns (is_translate, resolved_target_language). Any mode other than
    "translate" (incl. None/"") -> (False, DEFAULT_TARGET_LANGUAGE).
    Malformed/missing target_language under translate mode -> warn +
    DEFAULT_TARGET_LANGUAGE. Never raises — a voice session must not die
    on bad client input (matches the route-wide allowlist-warn pattern).
    """
    if mode != TRANSLATE_MODE:
        return False, DEFAULT_TARGET_LANGUAGE
    lang = (target_language or "").strip()
    if not lang or not _BCP47_RE.match(lang):
        print(f"{log_prefix} WARNING: target_language {target_language!r} is not "
              f"a valid BCP-47 tag; falling back to {DEFAULT_TARGET_LANGUAGE!r}")
        return True, DEFAULT_TARGET_LANGUAGE
    return True, lang


def build_translate_instructions(target_language: str) -> str:
    """Minimal system prompt for translation sessions.

    Deliberately tiny: no persona, no tool guidance, no snapshot context —
    the entire point of translate mode is fastest-possible session setup.
    """
    return (
        f"You are a real-time speech interpreter. Translate everything you "
        f"hear into the language with BCP-47 tag '{target_language}'. "
        f"Speak ONLY the translation — no commentary, no answers, no "
        f"questions, no explanations. Preserve the speaker's tone, register, "
        f"and intent. If an utterance is already in '{target_language}', "
        f"repeat it verbatim."
    )
```

**Step 4: Run test to verify it passes**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py -x -q
```
Expected: PASS (8 passed). Tree stays importable:
```bash
Orchestrator/venv/bin/python -c "import Orchestrator.routes.voice_translate; import Orchestrator.config"
```
Expected: exits 0, no output.

**Step 5: Commit**

```bash
git add Orchestrator/routes/voice_translate.py Orchestrator/config.py Orchestrator/tests/test_voice_translate.py
git commit -m "feat(voice-translate): shared mode/BCP-47 validation helpers + translate model constants (P6.2)"
```

---

### Task P6.3: OpenAI translate session branch in configure_openai_session

**Files:**
- Modify: Orchestrator/routes/realtime_routes.py:300-310 (signature), :352-359 (branch insertion point), :74 (imports)
- Test: Orchestrator/tests/test_voice_translate.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_translate.py`:

```python
# -----------------------------------------------------------------------------
# P6.3 — OpenAI translate session branch
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_translate_session_minimal(monkeypatch):
    # If the translate branch runs BEFORE the persona/context build, this
    # booby-trapped context builder is never reached.
    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _boom)
    from Orchestrator.routes.realtime_routes import configure_openai_session

    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="op", voice="marin",
        mode="translate", target_language="es",
    )
    payload = _extract_payload(session.openai_ws.send)
    assert payload["type"] == "session.update"
    s = payload["session"]
    # Tool-free by design (fast setup — no 56-tool ride-along)
    assert "tools" not in s and "tool_choice" not in s
    # Minimal instructions naming the target language, not the persona build
    assert "es" in s["instructions"]
    assert len(s["instructions"]) < 1000
    # GA shape + user voice preserved
    assert s["type"] == "realtime"
    assert s["audio"]["output"]["voice"] == "marin"
    assert s["audio"]["input"]["turn_detection"]["type"] == "server_vad"


@pytest.mark.asyncio
async def test_openai_default_path_unchanged(stub_fossil_context):
    """Regression pin: mode=None must still build the full persona session."""
    from Orchestrator.routes.realtime_routes import configure_openai_session

    session = _make_openai_session()
    await configure_openai_session(session=session, operator="op", voice="ash")
    payload = _extract_payload(session.openai_ws.send)
    s = payload["session"]
    assert "tools" in s               # full tool catalog still declared
    assert len(s["instructions"]) > 1000  # persona/context build still runs
```

**Step 2: Run test to verify it fails**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py -x -q
```
Expected: FAIL — `test_openai_translate_session_minimal` raises `TypeError: configure_openai_session() got an unexpected keyword argument 'mode'`.

**Step 3: Write minimal implementation**

3a. Add the import (anchor: line 74, `from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK`) — add below it:

```python
from Orchestrator.routes.voice_translate import (
    resolve_translate_params,
    build_translate_instructions,
)
```

3b. Extend the `configure_openai_session` signature (line 300-310) — add two kwargs at the END of the parameter list, after `create_response: Optional[bool] = None,`:

```python
    mode: Optional[str] = None,
    target_language: Optional[str] = None,
```

And append to the docstring Args section:

```
        mode: Optional session mode — "translate" builds a minimal tool-free
            translation session (P6a); anything else = normal voice session.
        target_language: BCP-47 target for translate mode; malformed/missing
            values fall back to "en" with a logged warning.
```

3c. Insert the translate branch AFTER the idle_timeout_ms validation block (ends line ~352, anchor: `idle_timeout_ms = None` inside the `(T14 F2)` clamp) and BEFORE `context, provenance = build_context_for_operator(operator, user_text="")` (line 359):

```python
    # ── Translation mode (P6a): minimal session — NO persona/context/tools ──
    # Branch BEFORE the persona/context build: fastest possible setup is the
    # entire point (design doc workstream 5). Session shape = GA voice shape
    # minus tools/tool_choice, confirmed by the P0 probe
    # (diagnostics/voice_probes/results/*-translate.json — combined P0.5 file,
    # openai gpt-realtime-translate entry).
    is_translate, resolved_target_language = resolve_translate_params(
        mode, target_language, log_prefix="[REALTIME]")
    if is_translate:
        session.provenance = {}  # no snapshot retrieval in translate mode
        config_event = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": build_translate_instructions(resolved_target_language),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": STT_OPENAI_STREAM},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.7,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 800,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": voice,
                        "speed": 1.0,
                    },
                },
                # NO tools / tool_choice — translation sessions are tool-free.
            },
        }
        await session.openai_ws.send(json.dumps(config_event))
        session.context_injected = True
        print(f"[REALTIME] TRANSLATE session configured "
              f"(target={resolved_target_language}, voice={voice})")
        return
```

**Step 4: Run test to verify it passes**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py Orchestrator/tests/test_live_models.py -q
```
Expected: PASS (all — including the pre-existing test_live_models.py suite, proving the default path is untouched).

**Step 5: Commit**

```bash
git add Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_voice_translate.py
git commit -m "feat(voice-translate): OpenAI minimal tool-free translate session branch (P6.3)"
```

---

### Task P6.4: /ws/realtime plumbing — query param + JSON field, model force, reconnect persistence

**Files:**
- Modify: Orchestrator/models.py (RealtimeSession dataclass, anchor `# Global storage for Realtime sessions`)
- Modify: Orchestrator/routes/realtime_routes.py:43-57 (config import), :1355-1374 (URL params), :1440-1466 (connect handler), :1213-1215 (reconnect)
- Test: Orchestrator/tests/test_voice_translate.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_translate.py`:

```python
# -----------------------------------------------------------------------------
# P6.4 — RealtimeSession persists translate mode across reconnects
# -----------------------------------------------------------------------------

def test_realtime_session_persists_translate_fields():
    from Orchestrator.models import RealtimeSession
    s = RealtimeSession(session_id="t")
    assert s.mode == "" and s.target_language == ""  # default = normal session
    s.mode = "translate"
    s.target_language = "es"
    assert (s.mode, s.target_language) == ("translate", "es")
```

**Step 2: Run test to verify it fails**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py::test_realtime_session_persists_translate_fields -x -q
```
Expected: FAIL with `AttributeError: 'RealtimeSession' object has no attribute 'mode'`.

**Step 3: Write minimal implementation**

3a. `Orchestrator/models.py` — append two fields at the END of the `RealtimeSession` dataclass (insert immediately above the `# Global storage for Realtime sessions` comment, after the `provenance:` field):

```python
    # P6a translation mode — persisted so reconnects rebuild the SAME session
    # type instead of degrading into a full persona/tool session.
    mode: str = ""                       # "" = normal, "translate" = translation mode
    target_language: str = ""            # BCP-47 target when mode == "translate"
```

3b. `Orchestrator/routes/realtime_routes.py` — add `OPENAI_REALTIME_TRANSLATE_MODEL,` to the `from Orchestrator.config import (...)` block (after `OPENAI_REALTIME_MODELS,`, line ~47).

3c. URL params — after `url_model = websocket.query_params.get("model")` (line 1357):

```python
    # P6a translation mode — same precedence as every other param
    # (JSON connect message wins, URL query fills missing).
    url_mode = websocket.query_params.get("mode")
    url_target_language = websocket.query_params.get("target_language")
```

3d. Connect handler — after `create_response = data.get("create_response", url_create_response)` (line 1445), before `session.operator = operator`:

```python
                mode = data.get("mode", url_mode)
                target_language = data.get("target_language", url_target_language)
                is_translate, _ = resolve_translate_params(
                    mode, target_language, log_prefix="[REALTIME]")
                if is_translate:
                    # Translate sessions ALWAYS bind the dedicated translate
                    # model — backend override beats any client model pick.
                    model = OPENAI_REALTIME_TRANSLATE_MODEL
                # Persist for reconnect (P6a — see reconnect path below)
                session.mode = mode if is_translate else ""
                session.target_language = target_language or "" if is_translate else ""
```

3e. Same connect handler — extend the `configure_openai_session(` call (line 1456-1466): add after `create_response=create_response,`:

```python
                        mode=mode,
                        target_language=target_language,
```

3f. Reconnect path (line 1213-1215, anchor `# Reconnect` + `if await connect_to_openai(session):`) — replace:

```python
        # Reconnect
        if await connect_to_openai(session):
            # Reconfigure session
            await configure_openai_session(session, session.operator)
```

with:

```python
        # Reconnect — translate sessions must rebind the translate model and
        # must NOT degrade into a full persona/tool session (P6a).
        if await connect_to_openai(
            session,
            model=OPENAI_REALTIME_TRANSLATE_MODEL if session.mode == "translate" else None,
        ):
            # Reconfigure session
            await configure_openai_session(
                session,
                session.operator,
                mode=session.mode or None,
                target_language=session.target_language or None,
            )
```

**Step 4: Run test to verify it passes**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py Orchestrator/tests/test_live_models.py -q \
  && Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes" \
  && grep -n "url_mode\|OPENAI_REALTIME_TRANSLATE_MODEL" Orchestrator/routes/realtime_routes.py | head
```
Expected: PASS; import exits 0; grep shows the import line, the two URL-param lines, the model-force line, and the reconnect line.

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/realtime_routes.py Orchestrator/tests/test_voice_translate.py
git commit -m "feat(voice-translate): /ws/realtime mode=translate plumbing — query+JSON precedence, model force, reconnect persistence (P6.4)"
```

---

### Task P6.5: Gemini translate setup branch in configure_gemini_session

Skip this task and P6.6 entirely if the P6.1 gate showed the Gemini probe FAILED (model not found) — record the skip in the commit log of P6.9.

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py:215-225 (signature), :275-282 (branch insertion point), :47-64 + :81 (imports)
- Test: Orchestrator/tests/test_voice_translate.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_translate.py`:

```python
# -----------------------------------------------------------------------------
# P6.5 — Gemini translate setup branch
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gemini_translate_setup_minimal(monkeypatch):
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _boom)
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    from Orchestrator.config import GEMINI_LIVE_TRANSLATE_MODEL

    session = _make_gemini_session()
    await configure_gemini_session(
        session, "op", "Kore", mode="translate", target_language="de")
    payload = _extract_payload(session.gemini_ws.send)
    setup = payload["setup"]
    # Dedicated translate model, regardless of any client model pick
    assert setup["model"] == f"models/{GEMINI_LIVE_TRANSLATE_MODEL}"
    # Tool-free, no thinking config, no compression — minimal pipe
    assert "tools" not in setup
    assert "thinkingConfig" not in setup.get("generationConfig", {})
    # Minimal instructions naming the target language
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert "de" in text and len(text) < 1000
    # User voice preserved
    assert (setup["generationConfig"]["speechConfig"]["voiceConfig"]
            ["prebuiltVoiceConfig"]["voiceName"] == "Kore")


@pytest.mark.asyncio
async def test_gemini_default_path_unchanged(stub_fossil_context):
    """Regression pin: mode=None must still build the full tool session."""
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session

    session = _make_gemini_session()
    await configure_gemini_session(session, "op", "Kore")
    setup = _extract_payload(session.gemini_ws.send)["setup"]
    assert "tools" in setup
    assert len(setup["systemInstruction"]["parts"][0]["text"]) > 1000
```

**Step 2: Run test to verify it fails**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py -x -q
```
Expected: FAIL — `TypeError: configure_gemini_session() got an unexpected keyword argument 'mode'`.

**Step 3: Write minimal implementation**

3a. Imports: add `GEMINI_LIVE_TRANSLATE_MODEL,` to the `from Orchestrator.config import (...)` block (after `GEMINI_LIVE_MODELS,`, line ~51). Below `from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK` (line 81) add:

```python
from Orchestrator.routes.voice_translate import (
    resolve_translate_params,
    build_translate_instructions,
)
```

3b. Extend the `configure_gemini_session` signature (line 215-225) — add at the END, after `thinking_level: Optional[str] = None,`:

```python
    mode: Optional[str] = None,
    target_language: Optional[str] = None,
```

Append to the docstring Args:

```
        mode: Optional session mode — "translate" builds a minimal tool-free
            translation session on GEMINI_LIVE_TRANSLATE_MODEL (P6a).
        target_language: BCP-47 target for translate mode; malformed/missing
            values fall back to "en" with a logged warning.
```

3c. Insert the translate branch AFTER the thinking_level validation (ends line ~275, anchor: `thinking_level = None` following the `GEMINI_LIVE_THINKING_LEVELS` warning) and BEFORE `context, provenance = build_context_for_operator(operator, user_text="")` (line 282):

```python
    # ── Translation mode (P6a): minimal setup — NO persona/context/tools ──
    # Dedicated model, tool-free, tiny prompt (design doc workstream 5).
    # Gated on the P0 probe (diagnostics/voice_probes/results/*-translate.json,
    # combined P0.5 file — gemini entries).
    # Ignores any client model pick — the translate model always wins.
    is_translate, resolved_target_language = resolve_translate_params(
        mode, target_language, log_prefix="[GEMINI-LIVE]")
    if is_translate:
        session.provenance = {}  # no snapshot retrieval in translate mode
        setup_message = {
            "setup": {
                "model": f"models/{GEMINI_LIVE_TRANSLATE_MODEL}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": voice}
                        }
                    },
                },
                "systemInstruction": {
                    "parts": [{"text": build_translate_instructions(resolved_target_language)}]
                },
                # NO tools / thinkingConfig / contextWindowCompression —
                # translation sessions are short-lived tool-free pipes.
            }
        }
        await session.gemini_ws.send(json.dumps(setup_message))
        session.context_injected = True
        session.voice = voice
        print(f"[GEMINI-LIVE] TRANSLATE session configured "
              f"(target={resolved_target_language}, "
              f"model={GEMINI_LIVE_TRANSLATE_MODEL}, voice={voice})")
        return
```

**Step 4: Run test to verify it passes**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py Orchestrator/tests/test_live_models.py -q
```
Expected: PASS (all).

**Step 5: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_voice_translate.py
git commit -m "feat(voice-translate): Gemini minimal tool-free translate setup branch on gemini-3.5-live-translate-preview (P6.5)"
```

---

### Task P6.6: /ws/gemini-live plumbing — query param + JSON field, reconnect persistence

**Files:**
- Modify: Orchestrator/models.py (GeminiLiveSession dataclass, anchor `# Global storage for Gemini Live sessions`)
- Modify: Orchestrator/routes/gemini_live_routes.py:1542-1547 (URL params), :1608-1638 (connect handler), :1385-1387 (reconnect)
- Test: Orchestrator/tests/test_voice_translate.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_voice_translate.py`:

```python
# -----------------------------------------------------------------------------
# P6.6 — GeminiLiveSession persists translate mode across reconnects
# -----------------------------------------------------------------------------

def test_gemini_session_persists_translate_fields():
    from Orchestrator.models import GeminiLiveSession
    s = GeminiLiveSession(session_id="t")
    assert s.mode == "" and s.target_language == ""
    s.mode = "translate"
    s.target_language = "ja"
    assert (s.mode, s.target_language) == ("translate", "ja")
```

**Step 2: Run test to verify it fails**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py::test_gemini_session_persists_translate_fields -x -q
```
Expected: FAIL with `AttributeError: 'GeminiLiveSession' object has no attribute 'mode'`.

**Step 3: Write minimal implementation**

3a. `Orchestrator/models.py` — append at the END of the `GeminiLiveSession` dataclass (immediately above `# Global storage for Gemini Live sessions`):

```python
    # P6a translation mode — persisted so reconnects rebuild the SAME session
    # type instead of degrading into a full persona/tool session.
    mode: str = ""                       # "" = normal, "translate" = translation mode
    target_language: str = ""            # BCP-47 target when mode == "translate"
```

3b. URL params — after `url_thinking_level = websocket.query_params.get("thinking_level")` (line 1547):

```python
    # P6a translation mode — same precedence as every other param
    # (JSON connect message wins, URL query fills missing).
    url_mode = websocket.query_params.get("mode")
    url_target_language = websocket.query_params.get("target_language")
```

3c. Connect handler — after `thinking_level = data.get("thinking_level", url_thinking_level)` (line 1617), before `session.operator = operator`:

```python
                mode = data.get("mode", url_mode)
                target_language = data.get("target_language", url_target_language)
                is_translate, _ = resolve_translate_params(
                    mode, target_language, log_prefix="[GEMINI-LIVE]")
                # Persist for reconnect (P6a — see gemini_reconnect path)
                session.mode = mode if is_translate else ""
                session.target_language = target_language or "" if is_translate else ""
```

3d. Extend the `configure_gemini_session(` call at line 1629-1638 — add after `thinking_level=thinking_level,`:

```python
                        mode=mode,
                        target_language=target_language,
```

3e. Reconnect path (line 1387, anchor `# Reconfigure with resumption handle`) — replace:

```python
            await configure_gemini_session(session, session.operator, session.voice)
```

with:

```python
            await configure_gemini_session(
                session,
                session.operator,
                session.voice,
                mode=session.mode or None,
                target_language=session.target_language or None,
            )
```

**Step 4: Run test to verify it passes**

Run:
```bash
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py Orchestrator/tests/test_live_models.py -q \
  && Orchestrator/venv/bin/python -c "import Orchestrator.routes.gemini_live_routes" \
  && grep -n "url_mode\|session.mode" Orchestrator/routes/gemini_live_routes.py | head
```
Expected: PASS; import exits 0; grep shows the URL-param, persist, and reconnect lines.

**Step 5: Commit**

```bash
git add Orchestrator/models.py Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_voice_translate.py
git commit -m "feat(voice-translate): /ws/gemini-live mode=translate plumbing — query+JSON precedence, reconnect persistence (P6.6)"
```

---

### Task P6.7: Portal — Translate toggle + target-language picker (Grok greyed out)

**Files:**
- Modify: Portal/index.html:1876-1880 (realtime pane), :1916-1926 (gemini pane), :1945 (grok pane), :11+:21 (version bump)
- Modify: Portal/modules/voice-agents-modal.js:49-68 + :70-87 (selector tables), :110-117 (ensureProvidersInit)
- Modify: Portal/modules/gpt-realtime.js:30-47 (SEL), :68-71 (state), :1185-1232 (connect), :1606-1612 (reconnect)
- Modify: Portal/modules/gemini-live.js:31-48 (SEL), :64-68 (state), :~820-873 (connect), :1214-1223 (reconnect)

No JS test harness exists for Portal modules — verification is `node --check` syntax gates plus a manual smoke in Step 4. (grok-live.js needs no JS change — the grey-out is inert disabled HTML.)

**Step 1: index.html — pane rows + version bump**

1a. Realtime pane — insert AFTER the `vaRealtimeIdleRow` div (closes line 1880, anchor `id="vaRealtimeIdleTimeoutInput"`):

```html
          <div class="va-row">
            <label for="vaRealtimeTranslateToggle">Translate mode</label>
            <input type="checkbox" id="vaRealtimeTranslateToggle">
          </div>
          <div class="va-row" id="vaRealtimeTranslateLangRow" style="display:none;">
            <label for="vaRealtimeTranslateLang">Target language</label>
            <select id="vaRealtimeTranslateLang"></select>
            <input type="text" id="vaRealtimeTranslateLangOther" placeholder="BCP-47, e.g. sw" maxlength="12" style="display:none;">
          </div>
```

1b. Gemini pane — insert AFTER the `vaGeminiThinkingRow` div (closes line 1926, anchor `id="vaGeminiThinkingSelect"`), same block with ids `vaGeminiTranslateToggle`, `vaGeminiTranslateLangRow`, `vaGeminiTranslateLang`, `vaGeminiTranslateLangOther`.

1c. Grok pane — insert BEFORE its `<div class="va-controls">` (line ~1945, anchor `id="vaGrokConnect"`):

```html
          <div class="va-row" title="Grok has no translation model">
            <label style="opacity:0.5;">Translate mode (not supported)</label>
            <input type="checkbox" disabled>
          </div>
```

1d. Bump the cache-buster on BOTH refs (index.html lines 11 and 21). Do NOT hard-code a number — earlier phases bump it too. Read the current `?v=genuiNN` first and increment whatever is there by one:

```bash
grep -o 'v=genui[0-9]*' Portal/index.html | sort -u   # must print exactly ONE value
CUR=$(grep -om1 'v=genui[0-9]*' Portal/index.html | grep -o '[0-9]*$')
sed -i "s/v=genui${CUR}/v=genui$((CUR+1))/g" Portal/index.html
```

Update the trailing comment to `<!-- v<NN+1>: P6a translation voice mode -->`.

**Step 2: voice-agents-modal.js — language catalog + row behavior + selector ids**

2a. Add above `const REALTIME_SELECTORS` (line 49):

```javascript
// P6a — translation target languages: hardcoded top-20 BCP-47 + free-text
// "Other" (design doc workstream 5 — YAGNI, no backend catalog fetch).
const TRANSLATE_LANGUAGES = [
    ['en', 'English'], ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'],
    ['it', 'Italian'], ['pt-BR', 'Portuguese (Brazil)'], ['ja', 'Japanese'],
    ['ko', 'Korean'], ['zh-CN', 'Chinese (Simplified)'], ['zh-TW', 'Chinese (Traditional)'],
    ['ar', 'Arabic'], ['hi', 'Hindi'], ['ru', 'Russian'], ['nl', 'Dutch'],
    ['pl', 'Polish'], ['tr', 'Turkish'], ['vi', 'Vietnamese'], ['th', 'Thai'],
    ['id', 'Indonesian'], ['uk', 'Ukrainian'],
];

function setupTranslateRow(toggleId, rowId, selectId, otherId) {
    const toggle = document.getElementById(toggleId);
    const row = document.getElementById(rowId);
    const select = document.getElementById(selectId);
    const other = document.getElementById(otherId);
    if (!toggle || !row || !select) return;
    if (!select.options.length) {
        for (const [tag, label] of TRANSLATE_LANGUAGES) {
            const opt = document.createElement('option');
            opt.value = tag;
            opt.textContent = `${label} (${tag})`;
            select.appendChild(opt);
        }
        const opt = document.createElement('option');
        opt.value = '__other__';
        opt.textContent = 'Other (type a BCP-47 tag)';
        select.appendChild(opt);
    }
    toggle.addEventListener('change', () => {
        row.style.display = toggle.checked ? '' : 'none';
    });
    select.addEventListener('change', () => {
        if (other) other.style.display = (select.value === '__other__') ? '' : 'none';
    });
}
```

2b. Add to `REALTIME_SELECTORS` (after `idleRow: 'vaRealtimeIdleRow',`):

```javascript
    translateToggle: 'vaRealtimeTranslateToggle',
    translateLangSelect: 'vaRealtimeTranslateLang',
    translateLangOther: 'vaRealtimeTranslateLangOther',
```

Add to `GEMINI_SELECTORS` (after `thinkingRow: 'vaGeminiThinkingRow',`) the `vaGemini*` equivalents.

2c. In `ensureProvidersInit()` (line 110-117), before `providersInitialized = true;`:

```javascript
    setupTranslateRow('vaRealtimeTranslateToggle', 'vaRealtimeTranslateLangRow',
        'vaRealtimeTranslateLang', 'vaRealtimeTranslateLangOther');
    setupTranslateRow('vaGeminiTranslateToggle', 'vaGeminiTranslateLangRow',
        'vaGeminiTranslateLang', 'vaGeminiTranslateLangOther');
```

**Step 3: gpt-realtime.js + gemini-live.js — read toggle, send fields, replay on reconnect**

3a. gpt-realtime.js SEL defaults (line 30-47) — add, after `noiseSelect: 'realtimeNoiseSelect',` (P3.23):

```javascript
    translateToggle: 'realtimeTranslateToggle',
    translateLangSelect: 'realtimeTranslateLang',
    translateLangOther: 'realtimeTranslateLangOther',
```

(Non-null legacy-prefixed ids — the P3.23 convention, matching `noiseSelect`. The legacy inline UI has no such elements, so `$()` resolves null there and the feature is inert; the modal remaps them to the `vaRealtime*` ids via REALTIME_SELECTORS.)

3b. gpt-realtime.js state (after line 71, anchor `let currentRealtimeIdleTimeoutMs = null;`):

```javascript
let currentRealtimeTranslateMode = null;   // 'translate' | null (P6a)
let currentRealtimeTargetLanguage = null;  // BCP-47 | null
```

3c. gpt-realtime.js `connect()` — after the `idleTimeoutMs` computation (line 1197):

```javascript
    // Translation mode (P6a) — modal-only UI; selectors are null elsewhere.
    const translateToggle = SEL.translateToggle ? $(SEL.translateToggle) : null;
    const translateOn = !!(translateToggle && translateToggle.checked);
    let targetLanguage;
    if (translateOn) {
        const langSel = SEL.translateLangSelect ? $(SEL.translateLangSelect) : null;
        const otherInput = SEL.translateLangOther ? $(SEL.translateLangOther) : null;
        targetLanguage = (langSel && langSel.value === '__other__')
            ? ((otherInput && otherInput.value.trim()) || 'en')
            : ((langSel && langSel.value) || 'en');
    }
```

After the `currentRealtimeIdleTimeoutMs = ...` capture (line 1205):

```javascript
    currentRealtimeTranslateMode = translateOn ? 'translate' : null;
    currentRealtimeTargetLanguage = translateOn ? targetLanguage : null;
```

After `if (idleTimeoutMs !== undefined) connectMsg.idle_timeout_ms = idleTimeoutMs;` (line 1231):

```javascript
        if (translateOn) {
            connectMsg.mode = 'translate';
            connectMsg.target_language = targetLanguage;
        }
```

3d. gpt-realtime.js reconnect — after the `if (currentRealtimeVadType) reconnectMsg.vad_type = ...` block (lines 1610-1612), add:

```javascript
        if (currentRealtimeTranslateMode) {
            reconnectMsg.mode = currentRealtimeTranslateMode;
            reconnectMsg.target_language = currentRealtimeTargetLanguage;
        }
```

3e. gemini-live.js — mirror exactly: SEL defaults get the three legacy-prefixed entries per the P3.23 convention (`translateToggle: 'geminiTranslateToggle', translateLangSelect: 'geminiTranslateLang', translateLangOther: 'geminiTranslateLangOther',` — matching P6.15's `affectiveToggle: 'geminiAffectiveToggle'` style); state vars `currentGeminiTranslateMode` / `currentGeminiTargetLanguage` after `currentGeminiThinkingLevel` (line ~68); the same toggle-read block in `connect()` after the `thinkingLevel` computation; capture after `currentGeminiThinkingLevel = thinkingLevel || null;`; `connectMsg.mode`/`connectMsg.target_language` after `if (thinkingLevel) connectMsg.thinking_level = thinkingLevel;` (line 872); reconnect replay after `if (currentGeminiThinkingLevel) reconnectMsg.thinking_level = ...` (line 1222).

**Step 4: Verify**

Run:
```bash
node --check "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/modules/gpt-realtime.js" \
  && node --check "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/modules/gemini-live.js" \
  && node --check "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/modules/voice-agents-modal.js" \
  && grep -c "TranslateToggle" "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/index.html"
```
Expected: three silent (exit 0) checks; grep prints `4` (2 realtime + 2 gemini id references; the grok checkbox has no id).

Manual smoke (browser, hard refresh): open Voice Agents modal → GPT Realtime tab shows "Translate mode" checkbox; checking it reveals the 21-option language select; picking "Other" reveals the free-text input; Grok tab shows the greyed disabled checkbox.

**Step 5: Commit**

```bash
git add Portal/index.html Portal/modules/voice-agents-modal.js Portal/modules/gpt-realtime.js Portal/modules/gemini-live.js
git commit -m "feat(voice-translate): Portal translate toggle + top-20 BCP-47 picker on OpenAI/Gemini panes, Grok greyed out (P6.7)"
```

---

### Task P6.8: Android — Translate toggle + language picker in VoiceScreen settings pane

Android project root (note the space in the path — always quote):
`/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`

**Files:**
- Modify: app/src/main/java/com/aiblackbox/portal/util/Constants.kt:212 (end of object)
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt:16-24
- Modify: app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt:124 (URL query builder)
- Modify: app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt — ViewModel: P3.13 persisted-settings StateFlow block, setter block, init-block restore, `buildSessionConfig()`; composable: P3.13 `collectAsState` block + settings pane after the P3.13 "Agent preset" dropdown
- Test: app/src/test/java/com/aiblackbox/portal/util/TranslateLanguagesTest.kt (new)

> **Depends on P3.13's ViewModel hoist.** P3.13 DELETED the composable-local `var ... by remember` config vars and moved all voice-agent settings + `buildSessionConfig()` into `VoiceViewModel` with DataStore write-through (`persist()` / `store.getString` one-shot restore). Do NOT reintroduce composable-local remember state. The translate settings below are ViewModel StateFlows persisted exactly like every sibling setting, and every `buildSessionConfig()` edit is an ADDITIVE field insertion into the P3.13 version — never a branch rewrite.

**Step 1: Write the failing test**

Create `app/src/test/java/com/aiblackbox/portal/util/TranslateLanguagesTest.kt`:

```kotlin
package com.aiblackbox.portal.util

import com.aiblackbox.portal.data.voice.VoiceSessionConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** P6a — translation voice mode: language catalog + config-field invariants. */
class TranslateLanguagesTest {

    private val bcp47 = Regex("^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

    @Test
    fun catalogHasTwentyEntries() {
        assertEquals(20, Constants.TRANSLATE_LANGUAGES.size)
    }

    @Test
    fun allIdsAreWellFormedBcp47() {
        Constants.TRANSLATE_LANGUAGES.forEach { (id, label) ->
            assertTrue("bad tag: $id", bcp47.matches(id))
            assertTrue("empty label for $id", label.isNotBlank())
        }
    }

    @Test
    fun sessionConfigTranslateFieldsDefaultOff() {
        val cfg = VoiceSessionConfig()
        assertNull(cfg.mode)
        assertNull(cfg.targetLanguage)
    }
}
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline
```
Expected: FAIL — compilation error `Unresolved reference: TRANSLATE_LANGUAGES` (and `mode`/`targetLanguage`).

**Step 3: Write minimal implementation**

3a. `Constants.kt` — append inside the `Constants` object, after `GEMINI_LIVE_THINKING_CAPABLE_MODELS` (line 212):

```kotlin
    /** P6a — translation target languages: top-20 BCP-47 + free-text "Other"
     *  handled in VoiceScreen. Mirrors Portal/modules/voice-agents-modal.js. */
    val TRANSLATE_LANGUAGES: List<Pair<String, String>> = listOf(
        "en" to "English", "es" to "Spanish", "fr" to "French", "de" to "German",
        "it" to "Italian", "pt-BR" to "Portuguese (Brazil)", "ja" to "Japanese",
        "ko" to "Korean", "zh-CN" to "Chinese (Simplified)", "zh-TW" to "Chinese (Traditional)",
        "ar" to "Arabic", "hi" to "Hindi", "ru" to "Russian", "nl" to "Dutch",
        "pl" to "Polish", "tr" to "Turkish", "vi" to "Vietnamese", "th" to "Thai",
        "id" to "Indonesian", "uk" to "Ukrainian",
    )
```

3b. `VoiceSessionConfig.kt` — add two fields at the end of the data class (after `val thinkingLevel: String? = null,`) and extend the KDoc:

```kotlin
    val mode: String? = null,            // P6a: "translate" | null (normal session)
    val targetLanguage: String? = null,  // P6a: BCP-47 target when mode == "translate"
```

3c. `VoiceClient.kt` — in the WS URL builder, after `cfg.thinkingLevel?.let { append("&thinking_level=").append(it) }` (line 124):

```kotlin
                    cfg.mode?.let { append("&mode=").append(it) }
                    cfg.targetLanguage?.let { append("&target_language=").append(it) }
```

3d. `VoiceScreen.kt` — ViewModel state (P3.13 pattern: StateFlow + DataStore write-through; NOT composable-local remember). Append to the P3.13 persisted-settings block (anchor: after `val selectedPresetId: StateFlow<String> = _selectedPresetId.asStateFlow()`):

```kotlin
    // ── Translation mode (P6a) — OpenAI + Gemini only; Grok has no translate model.
    // DataStore-persisted like every sibling setting (P3.13 pattern; keys "va_").
    private val _translateEnabled = MutableStateFlow(false)
    val translateEnabled: StateFlow<Boolean> = _translateEnabled.asStateFlow()
    private val _translateLang = MutableStateFlow("es")
    val translateLang: StateFlow<String> = _translateLang.asStateFlow()
    private val _translateLangOther = MutableStateFlow("")
    val translateLangOther: StateFlow<String> = _translateLangOther.asStateFlow()
```

Append to the P3.13 setter block (anchor: after `fun setPreset(id: String) { _selectedPresetId.value = id; persist("va_preset", id) }`):

```kotlin
    fun setTranslateEnabled(v: Boolean) { _translateEnabled.value = v; persist("va_translate_on", if (v) "true" else "false") }
    fun setTranslateLang(v: String) { _translateLang.value = v; persist("va_translate_lang", v) }
    fun setTranslateLangOther(v: String) { _translateLangOther.value = v; persist("va_translate_lang_other", v) }

    private fun resolvedTranslateLang(): String =
        if (_translateLang.value == "__other__") _translateLangOther.value.trim().ifBlank { "en" }
        else _translateLang.value
```

Append inside the P3.13 init-block one-shot restore coroutine (anchor: after the `store.getString("va_preset")...` line):

```kotlin
            _translateEnabled.value = store.getString("va_translate_on").first() == "true"
            store.getString("va_translate_lang").first().takeIf { it.isNotBlank() }?.let { _translateLang.value = it }
            store.getString("va_translate_lang_other").first().takeIf { it.isNotBlank() }?.let { _translateLangOther.value = it }
```

3e. `VoiceScreen.kt` — `buildSessionConfig()` (the ViewModel function P3.13 established): ADDITIVE insertions only — do not rewrite either branch. In the `VoiceBackend.GPT_REALTIME -> VoiceSessionConfig(...)` construction, insert directly after the `agentId = preset,` line (the last field P3.13 establishes there):

```kotlin
                mode = if (_translateEnabled.value) "translate" else null,
                targetLanguage = if (_translateEnabled.value) resolvedTranslateLang() else null,
```

In the `VoiceBackend.GEMINI_LIVE -> { ... VoiceSessionConfig(...) }` construction, insert the SAME two lines directly after its `agentId = preset,` line, leaving `model`/`vadStart`/`vadEnd`/`thinkingLevel`/`agentId` untouched. The `VoiceBackend.GROK_LIVE` arm is untouched (no translate model).

3f. `VoiceScreen.kt` — composable. Add to the P3.13 hoisted-state `collectAsState` block (anchor: after `val selectedPresetId by viewModel.selectedPresetId.collectAsState()`):

```kotlin
    val translateEnabled by viewModel.translateEnabled.collectAsState()
    val translateLang by viewModel.translateLang.collectAsState()
    val translateLangOther by viewModel.translateLangOther.collectAsState()
```

Settings pane UI: insert AFTER the P3.13 "Agent preset" dropdown block (anchor: the `if (presetOpts.isNotEmpty()) { ... }` block P3.13 inserted after the Voice `LabeledDropdown`) and BEFORE the `// ── Per-provider live-models config` comment:

```kotlin
                    // ── Translation mode (P6a) — greyed out for Grok (no translate model) ──
                    val translateSupported = backend != VoiceBackend.GROK_LIVE
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            if (translateSupported) "Translate mode" else "Translate mode (not supported)",
                            style = MaterialTheme.typography.labelLarge,
                            color = if (translateSupported) BbxDim else Neutral500,
                            modifier = Modifier.weight(1f),
                        )
                        Switch(
                            checked = translateEnabled && translateSupported,
                            onCheckedChange = viewModel::setTranslateEnabled,
                            enabled = translateSupported && !isConnected,
                        )
                    }
                    if (translateEnabled && translateSupported) {
                        val langOpts = Constants.TRANSLATE_LANGUAGES
                            .map { (id, label) -> id to "$label ($id)" } +
                            ("__other__" to "Other (type below)")
                        LabeledDropdown(
                            label = "Target language",
                            options = langOpts,
                            selectedId = translateLang,
                            enabled = !isConnected,
                            onSelect = viewModel::setTranslateLang,
                        )
                        if (translateLang == "__other__") {
                            OutlinedTextField(
                                value = translateLangOther,
                                onValueChange = { new ->
                                    viewModel.setTranslateLangOther(
                                        new.filter { it.isLetterOrDigit() || it == '-' }.take(12))
                                },
                                placeholder = { Text("BCP-47, e.g. sw", color = Neutral500) },
                                singleLine = true,
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedTextColor = BbxWhite,
                                    unfocusedTextColor = BbxWhite,
                                ),
                                modifier = Modifier.fillMaxWidth().widthIn(max = 200.dp),
                            )
                            Spacer(Modifier.height(10.dp))
                        }
                    }
```

Check the import block: add `androidx.compose.material3.Switch` if not already imported (the file already imports `OutlinedTextField`/`OutlinedTextFieldDefaults` — follow the same style; the compile in Step 4 catches a miss).

**Step 4: Run test to verify it passes**

Run:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline
```
Expected: BUILD SUCCESSFUL (~35s), `TranslateLanguagesTest` 3/3 passed, zero pre-existing test regressions.

**Step 5: Commit**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && git add \
  "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/util/Constants.kt" \
  "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt" \
  "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" \
  "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" \
  "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/util/TranslateLanguagesTest.kt"
git commit -m "feat(voice-translate): Android translate toggle + BCP-47 picker in VoiceScreen, wire mode/target_language through VoiceClient (P6.8)"
```

---

### Task P6.9: Probe-shape adaptation fallback + live bridge smoke

**Files:**
- Create: diagnostics/voice_probes/smoke_translate_bridge.py
- Possibly modify (fallback only): Orchestrator/routes/realtime_routes.py (translate branch from P6.3), Orchestrator/routes/gemini_live_routes.py (translate branch from P6.5), Orchestrator/config.py, Orchestrator/tests/test_voice_translate.py

**Step 1: Fallback check — re-read the P0 probe results against what shipped**

Run:
```bash
cat "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/diagnostics/voice_probes/results/"*translate*
```

If the probes confirmed the assumed shapes (the normal case per P6.1), make NO route changes and go to Step 2. Otherwise adapt EXACTLY these fields — nothing else — and update the matching assertions in `Orchestrator/tests/test_voice_translate.py` in the same change:

- **OpenAI** (`configure_openai_session` translate branch, P6.3): `session.type` value; `instructions` vs a probe-discovered dedicated target-language field (e.g. an `audio.output` translation/language key — use the probe's exact field name); presence/shape of `turn_detection`; `output_modalities`. Keep the branch tool-free regardless.
- **Gemini** (`configure_gemini_session` translate branch, P6.5): `systemInstruction` vs a probe-discovered translation config field; `generationConfig` keys. If the probe showed model-not-found for `gemini-3.5-live-translate-preview` but discovered a different live-translate id, change only the `GEMINI_LIVE_TRANSLATE_MODEL` default in config.py. If NO Gemini translate model exists, P6.5/P6.6 were skipped at the gate — additionally grey out the Gemini translate toggle exactly like Grok's (index.html gemini pane + `VoiceScreen.kt` `translateSupported` becomes `backend == VoiceBackend.GPT_REALTIME`) and note it in the commit message.

After any adaptation:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py Orchestrator/tests/test_live_models.py -q
```
Expected: PASS.

**Step 2: Write the live smoke script**

Create `diagnostics/voice_probes/smoke_translate_bridge.py`:

```python
#!/usr/bin/env python3
"""P6a acceptance smoke — drive OUR backend bridges in translate mode.

Connects to ws://localhost:9091/ws/{realtime,gemini-live}/<id>
?mode=translate&target_language=es, sends the JSON connect message, and
asserts the bridge reaches "connected" without an error event. Exercises the
REAL upstream providers (keys must be configured — this is the dev box).

Usage:
    Orchestrator/venv/bin/python diagnostics/voice_probes/smoke_translate_bridge.py [realtime|gemini-live|all]
Exit 0 = all requested bridges passed.
"""
import asyncio
import json
import sys
import uuid

import websockets

BASE = "ws://localhost:9091"
TIMEOUT_S = 30


async def smoke(path: str) -> bool:
    sid = uuid.uuid4().hex[:12]
    url = f"{BASE}/ws/{path}/{sid}?mode=translate&target_language=es"
    print(f"--- {path}: {url}")
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            # JSON connect carries the same fields — exercises the
            # JSON-wins-over-URL precedence path too.
            await ws.send(json.dumps({
                "type": "connect",
                "operator": "system",
                "mode": "translate",
                "target_language": "es",
            }))
            loop = asyncio.get_event_loop()
            deadline = loop.time() + TIMEOUT_S
            while loop.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_S)
                msg = json.loads(raw)
                print(f"    <- {msg.get('type')}: {str(msg.get('data'))[:120]}")
                if msg.get("type") == "connected":
                    print(f"    PASS {path}")
                    return True
                if msg.get("type") == "error":
                    print(f"    FAIL {path}: {msg.get('data')}")
                    return False
    except Exception as e:
        print(f"    FAIL {path}: {type(e).__name__}: {e}")
        return False
    print(f"    FAIL {path}: no connected/error within {TIMEOUT_S}s")
    return False


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    paths = ["realtime", "gemini-live"] if target == "all" else [target]
    results = [await smoke(p) for p in paths]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 3: Restart the live service and run the smoke**

(Restart is pre-authorized; 60-90s warm-up for the snapshot index rebuild.)

Run:
```bash
sudo systemctl restart blackbox.service && sleep 90
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python diagnostics/voice_probes/smoke_translate_bridge.py all
```
Expected: `PASS realtime` and `PASS gemini-live` (or `realtime` only if the Gemini half was gated out at P6.1), exit 0. Then confirm the minimal-session log lines fired:

```bash
journalctl -u blackbox.service --since "5 minutes ago" | grep "TRANSLATE session configured"
```
Expected: one `[REALTIME] TRANSLATE session configured (target=es, ...)` line per bridge smoked (plus `[GEMINI-LIVE] ...` if applicable). Also verify no-degradation: run the existing default-path smoke of your choice (e.g. open a normal Portal voice session) — tools still declared, persona present.

**Step 4: Run the full backend test suite one last time**

Run:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_voice_translate.py Orchestrator/tests/test_live_models.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add diagnostics/voice_probes/smoke_translate_bridge.py
# plus any files actually touched by the Step 1 adaptation:
# git add Orchestrator/routes/realtime_routes.py Orchestrator/routes/gemini_live_routes.py Orchestrator/config.py Orchestrator/tests/test_voice_translate.py
git commit -m "feat(voice-translate): live bridge smoke + probe-shape adaptation pass — P6a translation mode verified end-to-end (P6.9)"
```

---

### Task P6.10: Config — version-parameterized Gemini Live URL + affective-capable model allowlist

**Files:**
- Modify: Orchestrator/config.py:538-542 (GEMINI_LIVE_URL block), Orchestrator/config.py:650-656 (after GEMINI_LIVE_THINKING_CAPABLE_MODELS)
- Test: Orchestrator/tests/test_gemini_affective.py (create)

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_gemini_affective.py`:

```python
"""P6 workstream 5 — Gemini affective dialog + proactive audio (2.5-native-audio family, v1alpha only).

Pins:
1. gemini_live_url() derives v1beta/v1alpha endpoints; GEMINI_LIVE_URL back-compat exact.
2. GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS = the 2.5-native-audio family EXACTLY (3.1 rejects the fields).
Later tasks (P6.11-P6.13) append session-persistence, flag-resolution, and setup-emission tests here.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.config import (
    GEMINI_LIVE_URL,
    GEMINI_LIVE_URL_TEMPLATE,
    gemini_live_url,
    GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
)


def test_gemini_live_url_versions():
    assert gemini_live_url() == GEMINI_LIVE_URL
    assert ".v1beta." in gemini_live_url()
    assert gemini_live_url("v1alpha") == GEMINI_LIVE_URL.replace("v1beta", "v1alpha")
    with pytest.raises(ValueError):
        gemini_live_url("v2wrong")


def test_gemini_live_url_backcompat_exact():
    # Byte-exact guard: routes + phone bridge import this constant today.
    assert GEMINI_LIVE_URL == (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )


def test_affective_capable_models_exact():
    # 2.5-native-audio family ONLY — 3.1 rejects enableAffectiveDialog/proactivity in setup.
    assert GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS == frozenset({
        "gemini-2.5-flash-native-audio-latest",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    })
    assert "gemini-3.1-flash-live-preview" not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'GEMINI_LIVE_URL_TEMPLATE'`

**Step 3: Write minimal implementation**

In `Orchestrator/config.py`, replace lines 538-539 (the comment + `GEMINI_LIVE_URL = "wss://..."` assignment — leave `GEMINI_LIVE_MODEL` line 540 untouched) with:

```python
# Google Gemini Live API (Gemini 2.5/3.1 voice conversations).
# The BidiGenerateContent endpoint exists on two API versions:
#   v1beta  — default; all standard Live sessions
#   v1alpha — required for enableAffectiveDialog + proactivity.proactiveAudio
#             (2.5-native-audio family only; 3.1 rejects these setup fields)
GEMINI_LIVE_URL_TEMPLATE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.{version}.GenerativeService.BidiGenerateContent"
)


def gemini_live_url(version: str = "v1beta") -> str:
    """Gemini Live WS endpoint for an API version. Allowlist: v1beta | v1alpha."""
    if version not in ("v1beta", "v1alpha"):
        raise ValueError(f"Unsupported Gemini Live API version: {version!r}")
    return GEMINI_LIVE_URL_TEMPLATE.format(version=version)


# Back-compat constant — existing imports (gemini_live_routes, phone bridge) keep working.
GEMINI_LIVE_URL = gemini_live_url()
```

Then after the `GEMINI_LIVE_THINKING_CAPABLE_MODELS` frozenset (closes at line 656 pre-edit) add:

```python
# Model ids that support setup.enableAffectiveDialog + setup.proactivity.proactiveAudio.
# v1alpha-ONLY features on the 2.5-native-audio family (DEPRECATED line, no shutdown
# date announced). gemini-3.1-flash-live-preview REJECTS these setup fields — never
# emit them for non-members. Per ai.google.dev/gemini-api/docs/live-guide (2026-07-11).
GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS: frozenset = frozenset({
    "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-12-2025",
})
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py Orchestrator/tests/test_live_models.py -q`
Expected: PASS (all; test_live_models.py confirms no config regression)

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/tests/test_gemini_affective.py
git commit -m "feat(gemini-live): version-parameterized Live URL (v1beta/v1alpha) + affective-capable model allowlist"
```

---

### Task P6.11: Session fields + affective/proactive flag resolver with 3.1 rejection

**Files:**
- Modify: Orchestrator/models.py:131-138 (GeminiLiveSession reconnection-state block), Orchestrator/routes/gemini_live_routes.py:47-56 (config import block), Orchestrator/routes/gemini_live_routes.py:214 (insert helper between `connect_to_gemini` end at :213 and `configure_gemini_session` at :215)
- Test: Orchestrator/tests/test_gemini_affective.py

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_gemini_affective.py`:

```python
from Orchestrator.models import GeminiLiveSession
from Orchestrator.routes.gemini_live_routes import resolve_affective_flags


def test_session_persists_affective_flags_default_false():
    s = GeminiLiveSession(session_id="t1")
    assert s.affective_dialog is False
    assert s.proactive_audio is False


def test_resolve_flags_accepted_on_25():
    # JSON-bool and query-string forms both accepted
    a, p, err = resolve_affective_flags(
        "gemini-2.5-flash-native-audio-latest", "true", True)
    assert (a, p, err) == (True, True, None)


def test_resolve_flags_off_by_default():
    a, p, err = resolve_affective_flags(
        "gemini-2.5-flash-native-audio-latest", None, None)
    assert (a, p, err) == (False, False, None)


def test_resolve_flags_rejected_on_31():
    a, p, err = resolve_affective_flags(
        "gemini-3.1-flash-live-preview", "true", "false")
    assert (a, p) == (False, False)
    assert err is not None
    assert "gemini-3.1-flash-live-preview" in err
    assert "2.5" in err


def test_resolve_flags_garbage_treated_false():
    a, p, err = resolve_affective_flags(
        "gemini-2.5-flash-native-audio-latest", "DROP TABLE", {"x": 1})
    assert (a, p, err) == (False, False, None)
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py -x -q`
Expected: FAIL with `ImportError: cannot import name 'resolve_affective_flags'`

**Step 3: Write minimal implementation**

1. `Orchestrator/models.py` — inside `GeminiLiveSession`, after `intentional_disconnect` (line 137) and before `provenance` (line 138), add:

```python
    # Affective dialog + proactive audio (P6 — 2.5-native-audio family, v1alpha only).
    # Persisted on the session so the reconnect path (P1a) picks the same URL
    # version and reconfigures identically. Defaults False = v1beta everywhere.
    affective_dialog: bool = False
    proactive_audio: bool = False
```

2. `Orchestrator/routes/gemini_live_routes.py` — in the config import block, after `GEMINI_LIVE_THINKING_CAPABLE_MODELS,` (line 56) add:

```python
    GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
```

3. Same file — insert between `connect_to_gemini` (ends line 213) and `configure_gemini_session` (line 215):

```python
def _parse_ws_bool(value) -> bool:
    """Allowlist-parse a boolean that arrives as JSON bool or URL-query string.

    Accepts bool, "true"/"1"/"yes", "false"/"0"/"no"/""/None. Anything else
    (attacker-shaped strings, dicts) logs a warning and resolves False —
    mirrors the warn-and-fallback convention of the other Gemini allowlists.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no", ""):
            return False
    print(f"[GEMINI-LIVE] WARNING: unrecognized boolean flag {value!r}; treating as False")
    return False


def resolve_affective_flags(model, affective_raw, proactive_raw):
    """Validate affective-dialog / proactive-audio request flags against the model.

    Returns (affective: bool, proactive: bool, error: Optional[str]).
    `error` is a client-facing message when either flag is requested on a model
    outside GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS (3.1 rejects these v1alpha-only
    setup fields); both flags resolve False in that case. Model resolution
    mirrors configure_gemini_session: unknown/None -> GEMINI_LIVE_MODEL default.
    """
    affective = _parse_ws_bool(affective_raw)
    proactive = _parse_ws_bool(proactive_raw)
    if not (affective or proactive):
        return False, False, None
    _allowed_model_ids = {m["id"] for m in GEMINI_LIVE_MODELS}
    resolved_model = model if model in _allowed_model_ids else GEMINI_LIVE_MODEL
    if resolved_model not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS:
        return False, False, (
            f"Affective dialog / proactive audio are not supported by {resolved_model}. "
            f"They require a Gemini 2.5 native-audio model "
            f"({', '.join(sorted(GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS))} — deprecated line, "
            "v1alpha endpoint only). Turn the toggles off or switch model."
        )
    return affective, proactive, None
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py Orchestrator/tests/test_live_models.py -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/models.py Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_affective.py
git commit -m "feat(gemini-live): affective/proactive session flags + allowlist resolver with 3.1 rejection"
```

---

### Task P6.12: connect_to_gemini selects v1alpha per-session from the persisted flags

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py:49 (import entry `GEMINI_LIVE_URL,`), Orchestrator/routes/gemini_live_routes.py:192-194 (URL construction in `connect_to_gemini`)
- Test: Orchestrator/tests/test_gemini_affective.py

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_gemini_affective.py`:

```python
@pytest.mark.asyncio
async def test_connect_url_version_selection(monkeypatch):
    import Orchestrator.routes.gemini_live_routes as glr

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return MagicMock()

    monkeypatch.setattr(glr, "websockets", SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(glr, "WEBSOCKETS_AVAILABLE", True)
    monkeypatch.setattr(glr, "GOOGLE_API_KEY", "test-key")

    # Default flags (False/False) -> v1beta
    s = GeminiLiveSession(session_id="t-beta")
    assert await glr.connect_to_gemini(s) is True
    assert ".v1beta." in captured["url"]
    assert "key=test-key" in captured["url"]

    # Either flag set -> v1alpha (real dataclass: proves P1a reconnect re-derives
    # the same URL from persisted session state, not from request plumbing)
    s2 = GeminiLiveSession(session_id="t-alpha")
    s2.affective_dialog = True
    assert await glr.connect_to_gemini(s2) is True
    assert ".v1alpha." in captured["url"]

    s3 = GeminiLiveSession(session_id="t-alpha-2")
    s3.proactive_audio = True
    assert await glr.connect_to_gemini(s3) is True
    assert ".v1alpha." in captured["url"]
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py::test_connect_url_version_selection -x -q`
Expected: FAIL with `AssertionError` (v1beta URL returned for the affective session — `.v1alpha.` not in URL)

**Step 3: Write minimal implementation**

1. In the config import block (line 49), replace `    GEMINI_LIVE_URL,` with `    gemini_live_url,` (line 194 was the constant's only use in this module; the phone bridge imports it from config directly and is unaffected).

2. Replace lines 192-194 (`try:` block opening of `connect_to_gemini`):

```python
    try:
        # Gemini Live uses API key in URL query parameter.
        # v1alpha endpoint is REQUIRED when affective dialog / proactive audio were
        # requested for this session (flags validated + persisted by the WS handler
        # via resolve_affective_flags BEFORE this call); all other sessions stay on
        # v1beta. Reading session state (not request params) keeps the P1a
        # reconnect path on the identical endpoint.
        api_version = "v1alpha" if (session.affective_dialog or session.proactive_audio) else "v1beta"
        url = f"{gemini_live_url(api_version)}?key={GOOGLE_API_KEY}"
```

(The existing `print(f"[GEMINI-LIVE] Connecting to Gemini Live API...")` at line 196 stays; optionally extend it to `...API ({api_version})...`.)

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py Orchestrator/tests/test_live_models.py -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_affective.py
git commit -m "feat(gemini-live): connect on v1alpha when affective dialog / proactive audio requested"
```

---

### Task P6.13: configure_gemini_session emits enableAffectiveDialog + proactivity (capability-gated)

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py:481-496 (insert new block between the thinkingLevel block ending at :494 and `setup_message = ...` at :496), Orchestrator/tests/test_live_models.py:68-77 (`_make_gemini_session` fixture)
- Test: Orchestrator/tests/test_gemini_affective.py

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_gemini_affective.py`:

```python
@pytest.fixture
def stub_fossil_context(monkeypatch):
    """Stub snapshot retrieval (same pattern as test_live_models.py)."""
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub)


def _make_gemini_session(affective=False, proactive=False):
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.context_injected = False
    session.voice = ""
    # Explicit bools — a bare MagicMock attribute is truthy and would fake-enable
    session.affective_dialog = affective
    session.proactive_audio = proactive
    return session


def _sent_setup(session):
    raw = session.gemini_ws.send.await_args.args[0]
    return json.loads(raw)["setup"]


@pytest.mark.asyncio
async def test_configure_emits_affective_fields_on_25(stub_fossil_context):
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session(affective=True, proactive=True)
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-2.5-flash-native-audio-latest")
    setup = _sent_setup(session)
    assert setup["enableAffectiveDialog"] is True
    assert setup["proactivity"] == {"proactiveAudio": True}


@pytest.mark.asyncio
async def test_configure_emits_affective_only(stub_fossil_context):
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session(affective=True, proactive=False)
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-2.5-flash-native-audio-latest")
    setup = _sent_setup(session)
    assert setup["enableAffectiveDialog"] is True
    assert "proactivity" not in setup


@pytest.mark.asyncio
async def test_configure_suppresses_affective_on_31(stub_fossil_context, capsys):
    # Defense in depth: even if flags land on the session, NEVER emit at 3.1
    # (Google closes the WS on unknown setup fields — the June silent-failure class).
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session(affective=True, proactive=True)
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-3.1-flash-live-preview")
    setup = _sent_setup(session)
    assert "enableAffectiveDialog" not in setup
    assert "proactivity" not in setup
    assert "ignored" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_configure_default_session_emits_nothing(stub_fossil_context):
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    session = _make_gemini_session()
    await configure_gemini_session(
        session, "test_operator", "Charon",
        model="gemini-2.5-flash-native-audio-latest")
    setup = _sent_setup(session)
    assert "enableAffectiveDialog" not in setup
    assert "proactivity" not in setup
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py -x -q`
Expected: FAIL with `KeyError: 'enableAffectiveDialog'` in test_configure_emits_affective_fields_on_25

**Step 3: Write minimal implementation**

1. In `Orchestrator/routes/gemini_live_routes.py`, insert after the thinkingLevel `elif` block (line 494) and before `setup_message = {"setup": setup_config}` (line 496):

```python
    # Affective dialog + proactive audio (v1alpha, 2.5-native-audio family ONLY).
    # Flags were validated + persisted onto the session by the WS connect handler
    # (resolve_affective_flags) BEFORE connect_to_gemini chose the v1alpha URL; a
    # reconnect reconfigure re-reads them here so the rebuilt session is identical.
    # The capability re-check is defense in depth — 3.1 rejects these setup fields
    # and would close the WS, so never emit them for non-capable models.
    if session.affective_dialog or session.proactive_audio:
        if resolved_model in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS:
            if session.affective_dialog:
                setup_config["enableAffectiveDialog"] = True
            if session.proactive_audio:
                setup_config["proactivity"] = {"proactiveAudio": True}
            print(f"[GEMINI-LIVE] affective_dialog={session.affective_dialog} "
                  f"proactive_audio={session.proactive_audio} enabled for {resolved_model} (v1alpha)")
        else:
            print(f"[GEMINI-LIVE] affective/proactive flags ignored — model "
                  f"{resolved_model!r} not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS")
```

2. In `Orchestrator/tests/test_live_models.py` `_make_gemini_session()` (lines 68-77), add before `return session`:

```python
    session.affective_dialog = False   # explicit: bare MagicMock attrs are truthy
    session.proactive_audio = False
```

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py Orchestrator/tests/test_live_models.py -q`
Expected: PASS

**Step 5: Commit**
```bash
git add Orchestrator/routes/gemini_live_routes.py Orchestrator/tests/test_gemini_affective.py Orchestrator/tests/test_live_models.py
git commit -m "feat(gemini-live): emit enableAffectiveDialog + proactivity.proactiveAudio, capability-gated to the 2.5 family"
```

---

### Task P6.14: WS endpoint wiring — `affective`/`proactive` connect params, reject-on-3.1, connected-payload echo

**Files:**
- Modify: Orchestrator/routes/gemini_live_routes.py:1547 (after `url_thinking_level`), :1617-1623 (connect-branch field parsing, before the "Connecting..." status send), :1668-1676 (connected payload)

This is glue around the TDD'd `resolve_affective_flags` (P6.11) — verification is by exact commands, not a new test.

**Step 1: Add URL-query fallbacks**

After line 1547 (`url_thinking_level = websocket.query_params.get("thinking_level")`) add:

```python
    url_affective = websocket.query_params.get("affective")
    url_proactive = websocket.query_params.get("proactive")
```

**Step 2: Parse + validate + persist in the connect branch**

After line 1617 (`thinking_level = data.get("thinking_level", url_thinking_level)`) and line 1618 (`session.operator = operator`), insert (i.e. immediately before the `_safe_ws_send(... "Connecting to Gemini Live...")` at line 1620):

```python
                # P6 — affective dialog + proactive audio flags. JSON connect wins,
                # URL query fills missing (same precedence as model/vad/thinking).
                affective_raw = data.get("affective", url_affective)
                proactive_raw = data.get("proactive", url_proactive)
                affective, proactive, affective_error = resolve_affective_flags(
                    model, affective_raw, proactive_raw
                )
                if affective_error:
                    # Clear client error: requested on a model that rejects the
                    # fields (e.g. 3.1). Do NOT proceed to connect.
                    await _safe_ws_send(websocket, {"type": "error", "data": affective_error})
                    continue
                # Persist BEFORE connect_to_gemini — the flags select the v1alpha
                # endpoint there, and the P1a reconnect path re-reads them so a
                # reconnected session reconfigures identically.
                session.affective_dialog = affective
                session.proactive_audio = proactive
```

**Step 3: Echo resolved flags in the `connected` payload**

In the `"connected"` send (lines 1668-1676), after `"voice": voice` add:

```python
                            "affective_dialog": session.affective_dialog,
                            "proactive_audio": session.proactive_audio
```

(remember the trailing comma on the `"voice": voice,` line).

**Step 4: Verify (tree stays importable + green, wiring present)**

Run:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && \
Orchestrator/venv/bin/python -c "import Orchestrator.routes.gemini_live_routes as m; print('import OK')" && \
grep -n "resolve_affective_flags(" Orchestrator/routes/gemini_live_routes.py && \
grep -n "url_affective\|session.affective_dialog = affective" Orchestrator/routes/gemini_live_routes.py && \
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_gemini_affective.py Orchestrator/tests/test_live_models.py -q
```
Expected: `import OK`; grep shows the resolver defined (~line 224) AND called in the handler (~line 1640); the persist line present; pytest PASS.

**Step 5: Commit**
```bash
git add Orchestrator/routes/gemini_live_routes.py
git commit -m "feat(gemini-live): affective/proactive connect params on /ws/gemini-live (JSON + URL query, reject on 3.1)"
```

---

### Task P6.15: Portal — affective + proactive toggles, 2.5-only gating, reconnect replay

**Files:**
- Modify: Portal/index.html:1916-1925 (Gemini modal pane, after the thinking row) + cache-buster bump (read the current `?v=genuiNN`, increment by one)
- Modify: Portal/modules/voice-agents-modal.js:71-80 (GEMINI_SELECTORS)
- Modify: Portal/modules/gemini-live.js:31-47 (SEL), :68-71 (state), :821-873 (connect), :1214-1223 (reconnect replay), :1395-1403 (updateUI), :1510-1553 (visibility helpers + init wiring)

No JS test harness exists in this repo — verification is `node --check` + grep + live smoke.

**Step 1: index.html — two toggle rows**

After the `vaGeminiThinkingRow` div (closes at line 1925) insert:

```html
          <div class="va-row" id="vaGeminiAffectiveRow">
            <label for="vaGeminiAffectiveToggle">Affective dialog — 2.5 only (deprecated line)</label>
            <input type="checkbox" id="vaGeminiAffectiveToggle" disabled>
          </div>
          <div class="va-row" id="vaGeminiProactiveRow">
            <label for="vaGeminiProactiveToggle">Proactive audio — 2.5 only (deprecated line)</label>
            <input type="checkbox" id="vaGeminiProactiveToggle" disabled>
          </div>
```

Bump the cache-buster on BOTH refs. Do NOT hard-code a number — earlier phases bump it too, and a sed against a stale number is a silent no-op. Read the current `?v=genuiNN` first and increment whatever is there by one (mirrors P6.26):

```bash
grep -o 'v=genui[0-9]*' Portal/index.html | sort -u   # must print exactly ONE value
CUR=$(grep -om1 'v=genui[0-9]*' Portal/index.html | grep -o '[0-9]*$')
sed -i "s/v=genui${CUR}/v=genui$((CUR+1))/g" Portal/index.html
```

Update the trailing comment to mention the affective/proactive toggles.

**Step 2: voice-agents-modal.js — selector mapping**

In `GEMINI_SELECTORS` (lines 71-80), after `thinkingRow: 'vaGeminiThinkingRow',` add:

```javascript
    affectiveToggle: 'vaGeminiAffectiveToggle',
    proactiveToggle: 'vaGeminiProactiveToggle',
```

**Step 3: gemini-live.js — SEL, state, gate helper, connect, reconnect, updateUI, init**

1. SEL table (after `thinkingRow: null,` line 37):
```javascript
    affectiveToggle: 'geminiAffectiveToggle',
    proactiveToggle: 'geminiProactiveToggle',
```

2. Module state (after line 71 `let currentGeminiThinkingLevel = null;`):
```javascript
let currentGeminiAffective = false;
let currentGeminiProactive = false;
```

3. Next to `updateGeminiThinkingVisibility()` (line 1513-1521) add:
```javascript
/** 2.5-native-audio family gate for affective dialog + proactive audio (v1alpha only). */
function isAffectiveCapableModel(model) {
    return typeof model === 'string' && model.startsWith('gemini-2.5-flash-native-audio');
}

/**
 * Enable the affective/proactive toggles only when a 2.5-native-audio model is
 * selected (backend allowlist: GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS). A disabled
 * toggle is also unchecked so switching to 3.1 can't smuggle stale flags.
 */
function updateGeminiAffectiveAvailability() {
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    if (!modelSelect) return;
    const capable = isAffectiveCapableModel(modelSelect.value);
    [SEL.affectiveToggle, SEL.proactiveToggle].forEach(id => {
        const t = id ? $(id) : null;
        if (!t) return;
        t.disabled = !capable;
        if (!capable) t.checked = false;
    });
}
```

4. In `connect()` after the thinkingLevel read (lines 830-832) add:
```javascript
    // affective/proactive only valid on the 2.5-native-audio family (v1alpha)
    const affectiveToggle = SEL.affectiveToggle ? $(SEL.affectiveToggle) : null;
    const proactiveToggle = SEL.proactiveToggle ? $(SEL.proactiveToggle) : null;
    const affective = !!(isAffectiveCapableModel(selectedModel) && affectiveToggle && affectiveToggle.checked);
    const proactive = !!(isAffectiveCapableModel(selectedModel) && proactiveToggle && proactiveToggle.checked);
```
After the state-capture block (line 840 `currentGeminiThinkingLevel = ...`):
```javascript
    currentGeminiAffective = affective;
    currentGeminiProactive = proactive;
```
In `connectMsg` construction (after line 872 `if (thinkingLevel) ...`):
```javascript
        if (affective) connectMsg.affective = true;
        if (proactive) connectMsg.proactive = true;
```

5. In `reconnectToExistingSession()` (after line 1222):
```javascript
        if (currentGeminiAffective) reconnectMsg.affective = true;
        if (currentGeminiProactive) reconnectMsg.proactive = true;
```

6. In `updateUI()` (after line 1403 `if (vadEndSelect) vadEndSelect.disabled = isConnected;`):
```javascript
    // Affective/proactive are setup-binding (audit I4) AND URL-version-binding:
    // locked while connected; re-derive 2.5-family availability when idle.
    if (isConnected) {
        [SEL.affectiveToggle, SEL.proactiveToggle].forEach(id => {
            const t = id ? $(id) : null;
            if (t) t.disabled = true;
        });
    } else {
        updateGeminiAffectiveAvailability();
    }
```

7. In `initGeminiLiveUI()`: inside the catalog `.then` (line 1546, after `updateGeminiThinkingVisibility();`) add `updateGeminiAffectiveAvailability();`; and next to the model-change listener (line 1552) add:
```javascript
        modelSelect.addEventListener('change', updateGeminiAffectiveAvailability);
```

**Step 4: Verify**

Run:
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && \
node --check Portal/modules/gemini-live.js && node --check Portal/modules/voice-agents-modal.js && \
grep -c "vaGeminiAffectiveToggle\|vaGeminiProactiveToggle" Portal/index.html Portal/modules/voice-agents-modal.js && \
grep -n "connectMsg.affective\|reconnectMsg.affective\|updateGeminiAffectiveAvailability" Portal/modules/gemini-live.js && \
grep -o 'v=genui[0-9]*' Portal/index.html | sort -u
```
Expected: both `node --check` silent (exit 0); index.html count 4, modal count 2; gemini-live.js shows connect-send, reconnect-replay, and ≥3 availability-helper call sites; the version grep prints exactly ONE value, one higher than the pre-edit number (compare against the value you recorded in Step 1 — do not assume a specific number).

Live smoke (service runs from working tree): open the Portal Voice Agents modal → Gemini tab; with a 2.5 model selected toggles are enabled; switching to 3.1 disables AND unchecks them; connecting with both on against a 2.5 model logs `enableAffectiveDialog` in `journalctl -u blackbox.service` (`[GEMINI-LIVE] affective_dialog=True ... (v1alpha)`).

**Step 5: Commit**
```bash
git add Portal/index.html Portal/modules/voice-agents-modal.js Portal/modules/gemini-live.js
git commit -m "feat(portal): Gemini affective dialog + proactive audio toggles — 2.5 only (deprecated line)"
```

---

### Task P6.16: Android — affective/proactive switches in VoiceScreen Gemini section + wire params

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/util/Constants.kt:212-213`
- Modify: `.../app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt:16-24`
- Modify: `.../app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt:117-126` (URL buildString)
- Modify: `.../app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt` — ViewModel: P3.13 persisted-settings StateFlow block, setter block, init-block restore, `buildSessionConfig()` GEMINI_LIVE branch (additive); composable: P3.13 `collectAsState` block, `GeminiConfigBlock` call site, `GeminiConfigBlock` body
- Test: `.../app/src/test/java/com/aiblackbox/portal/voice/GeminiAffectiveTest.kt` (create)

> **Depends on P3.13's ViewModel hoist.** P3.13 DELETED the composable-local `var ... by remember` config vars (including `geminiThinkingLevel`) and moved all voice-agent settings + `buildSessionConfig()` into `VoiceViewModel` with DataStore write-through. Do NOT reintroduce composable-local remember state. The affective/proactive settings below are ViewModel StateFlows persisted like every sibling setting, and the `buildSessionConfig()` edit is an ADDITIVE insertion into the P3.13 GEMINI_LIVE branch — NEVER a branch replacement (the branch already carries `agentId` from P3.13 and, when Phase 6a landed, `mode`/`targetLanguage` from P6.8; replacing it would regress both).

**Step 1: Write the failing test**

Create `app/src/test/java/com/aiblackbox/portal/voice/GeminiAffectiveTest.kt` (under the AI_BlackBox_Portal module above):

```kotlin
package com.aiblackbox.portal.voice

import com.aiblackbox.portal.data.voice.VoiceSessionConfig
import com.aiblackbox.portal.util.Constants
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** P6 — affective dialog + proactive audio gate parity with the backend
 *  GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS frozenset (2.5-native-audio family only). */
class GeminiAffectiveTest {

    @Test
    fun affectiveCapableSetMatchesBackendAllowlist() {
        assertEquals(
            setOf(
                "gemini-2.5-flash-native-audio-latest",
                "gemini-2.5-flash-native-audio-preview-12-2025",
            ),
            Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
        )
    }

    @Test
    fun threeOneIsNotAffectiveCapable() {
        assertFalse(
            "gemini-3.1-flash-live-preview" in Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
        )
    }

    @Test
    fun sessionConfigDefaultsOmitAffectiveFlags() {
        val cfg = VoiceSessionConfig()
        assertNull(cfg.affective)
        assertNull(cfg.proactive)
        assertTrue(VoiceSessionConfig(affective = true, proactive = true).affective == true)
    }
}
```

**Step 2: Run test to verify it fails**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline`
Expected: FAIL — compilation error `Unresolved reference: GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS` (and `affective` on VoiceSessionConfig)

**Step 3: Write minimal implementation**

1. `Constants.kt` — after `GEMINI_LIVE_THINKING_CAPABLE_MODELS` (line 212), before the closing brace:

```kotlin
    /** Model ids supporting affective dialog + proactive audio (2.5 native-audio family,
     *  v1alpha, DEPRECATED line). Mirrors backend GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS. */
    val GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS: Set<String> = setOf(
        "gemini-2.5-flash-native-audio-latest",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    )
```

2. `VoiceSessionConfig.kt` — add to the data class (after `thinkingLevel`):

```kotlin
    /** Gemini affective dialog / proactive audio — 2.5 native-audio only; null = omit. */
    val affective: Boolean? = null,
    val proactive: Boolean? = null,
```

3. `VoiceClient.kt` — after `cfg.thinkingLevel?.let { ... }` (line 124):

```kotlin
                    cfg.affective?.takeIf { it }?.let { append("&affective=true") }
                    cfg.proactive?.takeIf { it }?.let { append("&proactive=true") }
```

4. `VoiceScreen.kt` — **all edits are additive against the post-P3.13 (ViewModel-hoisted) version; do not reintroduce composable-local `remember` state.**

ViewModel state (P3.13 pattern: StateFlow + DataStore write-through) — append to the P3.13 persisted-settings block (anchor: after the P6.8 `_translateLangOther` pair, or after `val selectedPresetId: StateFlow<String> = ...` if Phase 6a was gated out):

```kotlin
    // ── Gemini affective dialog + proactive audio (P6b) — 2.5 native-audio only.
    // DataStore-persisted like every sibling setting (P3.13 pattern; keys "va_").
    private val _geminiAffective = MutableStateFlow(false)
    val geminiAffective: StateFlow<Boolean> = _geminiAffective.asStateFlow()
    private val _geminiProactive = MutableStateFlow(false)
    val geminiProactive: StateFlow<Boolean> = _geminiProactive.asStateFlow()
```

Setters — append to the P3.13 setter block (anchor: after `fun setPreset(...)`, or after the P6.8 translate setters when present):

```kotlin
    fun setGeminiAffective(v: Boolean) { _geminiAffective.value = v; persist("va_gem_affective", if (v) "true" else "false") }
    fun setGeminiProactive(v: Boolean) { _geminiProactive.value = v; persist("va_gem_proactive", if (v) "true" else "false") }
```

Init-block restore — append inside the P3.13 one-shot restore coroutine (anchor: after the `store.getString("va_preset")...` line, or after the P6.8 translate restores when present):

```kotlin
            _geminiAffective.value = store.getString("va_gem_affective").first() == "true"
            _geminiProactive.value = store.getString("va_gem_proactive").first() == "true"
```

`buildSessionConfig()` GEMINI_LIVE branch (the ViewModel function P3.13 established) — ADDITIVE insertions only; leave `model`/`vadStart`/`vadEnd`/`agentId` and any P6.8 `mode`/`targetLanguage` lines exactly as they are. Insert after the `val thinkingAllowed = _geminiModel.value in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS` line:

```kotlin
                val affectiveAllowed = _geminiModel.value in Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
```

and inside that branch's `VoiceSessionConfig(...)` construction, insert directly after the `thinkingLevel = if (thinkingAllowed) _geminiThinkingLevel.value else null,` line (this anchor exists whether or not Phase 6a landed):

```kotlin
                    affective = if (affectiveAllowed && _geminiAffective.value) true else null,
                    proactive = if (affectiveAllowed && _geminiProactive.value) true else null,
```

Composable — add to the P3.13 hoisted-state `collectAsState` block (anchor: after `val geminiThinkingLevel by viewModel.geminiThinkingLevel.collectAsState()`):

```kotlin
    val geminiAffective by viewModel.geminiAffective.collectAsState()
    val geminiProactive by viewModel.geminiProactive.collectAsState()
```

Call site — add to the `GeminiConfigBlock(` call (P3.13 style: setter references, not local-mutation lambdas):

```kotlin
                            affective = geminiAffective,
                            onAffectiveChange = viewModel::setGeminiAffective,
                            proactive = geminiProactive,
                            onProactiveChange = viewModel::setGeminiProactive,
```

`GeminiConfigBlock` — add parameters after `onThinkingLevelChange`:

```kotlin
    affective: Boolean,
    onAffectiveChange: (Boolean) -> Unit,
    proactive: Boolean,
    onProactiveChange: (Boolean) -> Unit,
```

and after the thinking-level conditional inside the function body (anchor: the `if`/conditional that renders the thinking-level dropdown; line numbers have drifted since P3.12/P3.13 — trust the anchor):

```kotlin
    // Affective dialog + proactive audio — 2.5 native-audio family only (v1alpha,
    // deprecated line). Setup+URL-version binding: locked while connected (audit I4).
    val affectiveCapable = model in Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text(
            "Affective dialog — 2.5 only (deprecated line)",
            style = MaterialTheme.typography.labelLarge,
            color = BbxDim,
            modifier = Modifier.weight(1f),
        )
        Switch(
            checked = affective && affectiveCapable,
            onCheckedChange = onAffectiveChange,
            enabled = !connected && affectiveCapable,
        )
    }
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text(
            "Proactive audio — 2.5 only (deprecated line)",
            style = MaterialTheme.typography.labelLarge,
            color = BbxDim,
            modifier = Modifier.weight(1f),
        )
        Switch(
            checked = proactive && affectiveCapable,
            onCheckedChange = onProactiveChange,
            enabled = !connected && affectiveCapable,
        )
    }
```

Add `import androidx.compose.material3.Switch` to VoiceScreen.kt's material3 import block (lines 46-50; explicit imports, no wildcard — verify `Row`/`Alignment`/`Modifier.weight` already imported, they are used elsewhere in this file).

**Step 4: Run test to verify it passes**
Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline`
Expected: PASS (~35s; GeminiAffectiveTest 3/3 green, no regressions)

**Step 5: Commit**
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/util/Constants.kt" \
        "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt" \
        "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceClient.kt" \
        "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" \
        "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/voice/GeminiAffectiveTest.kt"
git commit -m "feat(android): Gemini affective dialog + proactive audio switches — 2.5 only (deprecated line)"
```

---

### Task P6.20: Live probe — xAI Custom Voices wire shapes

**Files:**
- Create: diagnostics/xai_custom_voices_probe.py

This is a probe task (no pytest). Wire probes beat documentation: the recon (scratchpad `recon/xaiResearch.json`) confirms `/v1/custom-voices` CRUD exists (POST file ≤120s, GET list, DELETE) but NOT the exact multipart field names or the list response envelope. Everything downstream (P6.21+) assumes `file`+`name` fields and a `voice_id` key — this probe confirms or corrects those assumptions WITHOUT ever creating a voice.

**Step 1: Write the probe script**

```python
#!/usr/bin/env python3
"""Live probe: xAI Custom Voices REST wire shapes (voice-upgrade pass, workstream 5).

Confirms — against the REAL API with this box's XAI_API_KEY — the shapes the
provider module (Orchestrator/xai_voices.py, task P6.21) assumes:

  1. GET  /v1/custom-voices              -> auth ok, top-level shape (bare list vs
     {"voices": [...]}), and per-voice key names (voice_id vs id, name, ...).
  2. POST /v1/custom-voices              -> probed with an EMPTY multipart so the
     validation error enumerates the REQUIRED field names (file? audio? name?).
     NO voice is ever created.
  3. DELETE /v1/custom-voices/<fake-id>  -> error shape for an unknown id.

Read-only + non-creating by construction. Run BEFORE implementing P6.21; if any
assumed name differs, adjust P6.21's code to the probed truth.
"""
import json
import os
import sys

import httpx

BASE = "https://api.x.ai/v1/custom-voices"
KEY = os.getenv("XAI_API_KEY", "")
if not KEY:
    sys.exit("XAI_API_KEY not set — export it from the service env before running")
H = {"Authorization": f"Bearer {KEY}"}

print("== 1. GET /v1/custom-voices ==")
r = httpx.get(BASE, headers=H, timeout=30)
print("status:", r.status_code)
try:
    body = r.json()
    print("top-level type:", type(body).__name__)
    print(json.dumps(body, indent=2)[:2000])
except Exception:
    print("non-JSON body:", r.text[:500])

print("\n== 2. POST /v1/custom-voices (empty multipart -> expect 4xx naming required fields) ==")
r = httpx.post(BASE, headers=H, files={"_probe": ("empty.txt", b"")}, timeout=30)
print("status:", r.status_code)
print("body:", r.text[:1000])

print("\n== 3. DELETE /v1/custom-voices/probe-nonexistent-id ==")
r = httpx.delete(f"{BASE}/probe-nonexistent-id", headers=H, timeout=30)
print("status:", r.status_code)
print("body:", r.text[:500])
```

**Step 2: Run the probe**
Run (from repo root `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`, with the key exported from wherever the service env holds `XAI_API_KEY` — same source `Orchestrator/config.py:446` reads):
```
XAI_API_KEY="<key from service env>" Orchestrator/venv/bin/python diagnostics/xai_custom_voices_probe.py
```
Expected: section 1 prints `status: 200` and either a bare JSON list or an object with a `voices`/`data` array; section 2 prints a 4xx whose body names the required multipart fields; section 3 prints a 4xx error shape.

**Step 3: Record the probed truth**
If the list envelope is NOT (bare list | `{"voices": [...]}` | `{"data": [...]}`), or the clone fields are NOT `file`+`name`, or the id key is NOT (`voice_id` | `id`): note the actual names and use them in P6.21 Step 3 (the code below already tolerates all the listed variants).

**Step 4: Commit**
```
git add diagnostics/xai_custom_voices_probe.py
git commit -m "chore(xai-voice): live probe of /v1/custom-voices wire shapes (pre-P6.21 gate)"
```

---

### Task P6.21: Provider module — `Orchestrator/xai_voices.py` (list / clone / delete)

**Files:**
- Create: Orchestrator/xai_voices.py
- Test: Orchestrator/tests/test_xai_voices_module.py

**Step 1: Write the failing test**

```python
"""Hermetic tests for Orchestrator/xai_voices.py — the xAI Custom Voices provider
module. httpx is mocked at the module seam (xai_voices calls httpx.get/post/delete
as module attributes), so no live xAI call ever happens."""
import json

import pytest

from Orchestrator import xai_voices as xv


class FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "xai-fake-key")
    xv._cache["ts"] = 0.0
    xv._cache["ids"] = frozenset()
    yield
    xv._cache["ts"] = 0.0
    xv._cache["ids"] = frozenset()


def test_list_parses_voices_envelope(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, {"voices": [{"voice_id": "cv-1", "name": "Narrator"}]}))
    voices = xv.list_custom_voices()
    assert voices == [{"voice_id": "cv-1", "name": "Narrator"}]


def test_list_parses_bare_list(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, [{"id": "cv-2", "name": "Alt"}]))
    assert xv.list_custom_voices() == [{"id": "cv-2", "name": "Alt"}]


def test_list_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    monkeypatch.setattr(xv.httpx, "get",
                        lambda *a, **k: pytest.fail("network hit despite no key"))
    assert xv.list_custom_voices() is None


def test_list_provider_error_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        401, {"error": "invalid api key"}))
    with pytest.raises(RuntimeError, match="401"):
        xv.list_custom_voices()


def test_clone_posts_multipart_and_returns_body(monkeypatch, tmp_path):
    sample = tmp_path / "sample.mp3"
    sample.write_bytes(b"ID3fakeaudio")
    seen = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        seen.update(url=url, data=data, file_field=list(files.keys()))
        return FakeResp(200, {"voice_id": "cv-new", "name": "My Voice"})

    monkeypatch.setattr(xv.httpx, "post", fake_post)
    result = xv.clone_voice("My Voice", str(sample), description="warm")
    assert result["voice_id"] == "cv-new"
    assert seen["url"] == xv.XAI_VOICES_URL
    assert seen["data"]["name"] == "My Voice"
    assert seen["data"]["description"] == "warm"
    assert seen["file_field"] == ["file"]


def test_clone_no_key_raises(monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"x")
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    with pytest.raises(RuntimeError, match="not configured"):
        xv.clone_voice("X", str(sample))


def test_delete_hits_id_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(xv.httpx, "delete",
                        lambda url, **kw: (seen.update(url=url), FakeResp(200, {"ok": True}))[1])
    xv.delete_voice("cv-1")
    assert seen["url"] == f"{xv.XAI_VOICES_URL}/cv-1"
```

**Step 2: Run test to verify it fails**
Run (from repo root):
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voices_module.py -x -q
```
Expected: FAIL with `ModuleNotFoundError: No module named 'Orchestrator.xai_voices'`

**Step 3: Write minimal implementation**

Create `Orchestrator/xai_voices.py` (adjust field names ONLY if the P6.20 probe contradicted them):

```python
"""xAI Custom Voices provider module (voice-upgrade pass, workstream 5).

Thin sync httpx client for ``https://api.x.ai/v1/custom-voices`` (clone from a
≤120s reference clip; the resulting voice_id is usable as a Grok voice-session
voice — recon: scratchpad recon/xaiResearch.json, wire shapes confirmed by
diagnostics/xai_custom_voices_probe.py).

The REST routes (routes/xai_voice_routes.py), the ToolVault executor
(ToolVault/tools/xai_clone_voice/), and the Grok live-session voice validation
(routes/grok_live_routes.py) ALL consume THIS module — one provider seam,
mirroring Orchestrator/elevenlabs/voices. Key is resolved FRESH per call
(never frozen at import) so wizard-pasted keys work without a restart.

Errors: unconfigured -> ``list_custom_voices`` returns None / mutators raise
RuntimeError("xAI not configured..."); provider 4xx/5xx -> RuntimeError with
the human message (routes map these to HTTP 400, same contract as elevenlabs).
"""
import os
import time

import httpx

XAI_VOICES_URL = "https://api.x.ai/v1/custom-voices"
_TIMEOUT = 30.0
_CLONE_TIMEOUT = 120.0  # uploads a reference clip; give it headroom

# 60s validity cache for Grok-session voice validation (is_custom_voice).
CACHE_TTL_SECS = 60.0
_cache: dict = {"ts": 0.0, "ids": frozenset()}


def resolve_api_key() -> str:
    """Fresh read every call — a key pasted in the wizard works with NO restart."""
    return os.getenv("XAI_API_KEY", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {resolve_api_key()}"}


def _raise_for_error(resp) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error") or resp.text
        except Exception:
            detail = resp.text
        raise RuntimeError(f"xAI error {resp.status_code}: {str(detail)[:300]}")


def _bust_cache() -> None:
    _cache["ts"] = 0.0


def list_custom_voices():
    """All cloned voices on the account, or None when no key is configured.

    Tolerates every probed envelope: bare list, {"voices": [...]}, {"data": [...]}.
    Raises RuntimeError on provider errors.
    """
    if not resolve_api_key():
        return None
    resp = httpx.get(XAI_VOICES_URL, headers=_headers(), timeout=_TIMEOUT)
    _raise_for_error(resp)
    body = resp.json()
    if isinstance(body, list):
        return body
    return body.get("voices") or body.get("data") or []


def clone_voice(name: str, audio_path: str, description: str | None = None) -> dict:
    """Clone a custom voice from ONE local reference clip (xAI enforces <=120s
    server-side). Returns the provider's voice object (voice_id + name)."""
    if not resolve_api_key():
        raise RuntimeError("xAI not configured - set XAI_API_KEY (onboarding wizard)")
    with open(audio_path, "rb") as fh:
        files = {"file": (os.path.basename(audio_path), fh, "application/octet-stream")}
        data = {"name": name}
        if description:
            data["description"] = description
        resp = httpx.post(XAI_VOICES_URL, headers=_headers(), data=data,
                          files=files, timeout=_CLONE_TIMEOUT)
    _raise_for_error(resp)
    _bust_cache()
    return resp.json()


def delete_voice(voice_id: str) -> None:
    if not resolve_api_key():
        raise RuntimeError("xAI not configured - set XAI_API_KEY (onboarding wizard)")
    resp = httpx.delete(f"{XAI_VOICES_URL}/{voice_id}", headers=_headers(), timeout=_TIMEOUT)
    _raise_for_error(resp)
    _bust_cache()


def voice_id_of(voice: dict) -> str:
    """Canonical id extraction — tolerates voice_id | id key naming."""
    return str(voice.get("voice_id") or voice.get("id") or "")


def is_custom_voice(voice_id: str) -> bool:
    """True iff ``voice_id`` is a cloned voice on this account. 60s TTL cache;
    FAIL-OPEN to catalog-only: no key / xAI unreachable -> False (the caller
    falls back to the built-in voice catalog). A failed refresh keeps the stale
    id set (graceful degradation) and does NOT stamp ts, so the next call retries.
    """
    if not voice_id or not resolve_api_key():
        return False
    now = time.time()
    if now - _cache["ts"] > CACHE_TTL_SECS:
        try:
            voices = list_custom_voices() or []
            _cache["ids"] = frozenset(voice_id_of(v) for v in voices)
            _cache["ts"] = now
        except Exception:
            pass  # keep stale ids; retry on next call
    return voice_id in _cache["ids"]
```

**Step 4: Run test to verify it passes**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voices_module.py -x -q
```
Expected: PASS (7 passed)

**Step 5: Commit**
```
git add Orchestrator/xai_voices.py Orchestrator/tests/test_xai_voices_module.py
git commit -m "feat(xai-voice): custom-voices provider module (list/clone/delete, fresh-key reads)"
```

---

### Task P6.22: 60s-cached `is_custom_voice` behavior (cache hit, TTL, fail-open)

**Files:**
- Modify: Orchestrator/xai_voices.py (already implemented in P6.21 — this task PROVES the cache/fail-open contract)
- Test: Orchestrator/tests/test_xai_voices_module.py (append)

**Step 1: Write the failing test**

Append to `Orchestrator/tests/test_xai_voices_module.py`:

```python
# =============================================================================
# is_custom_voice — 60s cache + fail-open (design: workstream 5 / scope item 3)
# =============================================================================

def test_is_custom_voice_hits_and_misses(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, {"voices": [{"voice_id": "cv-1", "name": "N"}]}))
    assert xv.is_custom_voice("cv-1") is True
    assert xv.is_custom_voice("not-a-voice") is False


def test_is_custom_voice_caches_for_ttl(monkeypatch):
    calls = {"n": 0}

    def counting_get(url, **kw):
        calls["n"] += 1
        return FakeResp(200, {"voices": [{"voice_id": "cv-1"}]})

    monkeypatch.setattr(xv.httpx, "get", counting_get)
    assert xv.is_custom_voice("cv-1") is True
    assert xv.is_custom_voice("cv-1") is True
    assert xv.is_custom_voice("cv-2") is False
    assert calls["n"] == 1  # ONE fetch inside the 60s window


def test_is_custom_voice_refetches_after_ttl(monkeypatch):
    calls = {"n": 0}

    def counting_get(url, **kw):
        calls["n"] += 1
        return FakeResp(200, {"voices": [{"voice_id": "cv-1"}]})

    monkeypatch.setattr(xv.httpx, "get", counting_get)
    assert xv.is_custom_voice("cv-1") is True
    xv._cache["ts"] = time_module.time() - 61  # age the cache past TTL
    assert xv.is_custom_voice("cv-1") is True
    assert calls["n"] == 2


def test_is_custom_voice_fail_open_when_unreachable(monkeypatch):
    def boom(url, **kw):
        raise Exception("connection refused")

    monkeypatch.setattr(xv.httpx, "get", boom)
    assert xv.is_custom_voice("cv-1") is False  # empty cache + unreachable -> catalog-only


def test_is_custom_voice_keeps_stale_ids_on_refresh_failure(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, {"voices": [{"voice_id": "cv-1"}]}))
    assert xv.is_custom_voice("cv-1") is True
    xv._cache["ts"] = time_module.time() - 61

    def boom(url, **kw):
        raise Exception("xai down")

    monkeypatch.setattr(xv.httpx, "get", boom)
    assert xv.is_custom_voice("cv-1") is True  # stale set survives the outage


def test_is_custom_voice_no_key_is_false(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    assert xv.is_custom_voice("cv-1") is False


def test_clone_and_delete_bust_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(200, {"voices": []}))
    xv.is_custom_voice("cv-1")
    assert xv._cache["ts"] > 0
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"x")
    monkeypatch.setattr(xv.httpx, "post", lambda *a, **k: FakeResp(200, {"voice_id": "cv-9"}))
    xv.clone_voice("V", str(sample))
    assert xv._cache["ts"] == 0.0
```

Also add `import time as time_module` to the test file's imports.

**Step 2: Run test to verify it fails**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voices_module.py -x -q
```
Expected: PASS if P6.21's implementation is exactly as written; if any cache test FAILS, fix `is_custom_voice` in `Orchestrator/xai_voices.py` until green (the contract in the tests is authoritative: one fetch per 60s, stale-set survival, no-key/no-cache → False).

**Step 3: (only on failure) adjust implementation** — the P6.21 code already implements this contract; no change expected.

**Step 4: Run full module test file**
Run: same command as Step 2.
Expected: PASS (14 passed)

**Step 5: Commit**
```
git add Orchestrator/tests/test_xai_voices_module.py
git commit -m "test(xai-voice): prove 60s cache + fail-open contract of is_custom_voice"
```

---

### Task P6.23: REST routes — GET/POST/DELETE `/xai/voices` with consent gate

**Files:**
- Create: Orchestrator/routes/xai_voice_routes.py
- Modify: Orchestrator/app.py:99 (add import after the elevenlabs_routes line)
- Test: Orchestrator/tests/test_xai_voice_routes.py

**Step 1: Write the failing test**

```python
"""Hermetic tests for the xAI Custom Voices routes (Voice Lab xAI section).

Route layer only (TestClient). Provider calls are monkeypatched on
``Orchestrator.xai_voices`` (imported inside each handler), so no live xAI call
ever happens. Mirrors test_elevenlabs_voice_routes.py.

Contract the Portal/Android UI depends on:
  * GET /xai/voices no key -> {"configured": false, "voices": []} (zone hides)
  * GET /xai/voices        -> {"configured": true, "voices": [...]}
  * POST clone WITHOUT consent="true" -> 422, provider NEVER called (the gate)
  * POST clone WITH consent -> clone_voice called with parsed args -> {voice_id}
  * DELETE /xai/voices/{id} -> {"ok": true}; provider RuntimeError -> 400
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator import xai_voices as xv
from Orchestrator.app import app


@pytest.fixture
def cli():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _present_key(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "xai-fake")


def test_list_no_key_returns_unconfigured(cli, monkeypatch):
    monkeypatch.setattr(xv, "list_custom_voices", lambda: None)
    resp = cli.get("/xai/voices")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "voices": []}


def test_list_returns_voices(cli, monkeypatch):
    monkeypatch.setattr(xv, "list_custom_voices",
                        lambda: [{"voice_id": "cv-1", "name": "Narrator"}])
    resp = cli.get("/xai/voices")
    assert resp.status_code == 200
    assert resp.json() == {"configured": True,
                           "voices": [{"voice_id": "cv-1", "name": "Narrator"}]}


def test_list_provider_error_maps_to_400(cli, monkeypatch):
    def boom():
        raise RuntimeError("xAI error 401: invalid api key")
    monkeypatch.setattr(xv, "list_custom_voices", boom)
    resp = cli.get("/xai/voices")
    assert resp.status_code == 400


def test_clone_without_consent_returns_422_and_never_calls_provider(cli, monkeypatch):
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite missing consent"))
    resp = cli.post(
        "/xai/voices",
        data={"name": "Test", "consent": "false"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 422
    assert "consent" in resp.json()["detail"].lower()


def test_clone_with_consent_calls_provider_and_returns_voice_id(cli, monkeypatch):
    seen = {}

    def fake_clone(name, audio_path, description=None):
        import os
        seen.update(name=name, description=description,
                    path_exists=os.path.exists(audio_path))
        return {"voice_id": "cv-new", "name": name}

    monkeypatch.setattr(xv, "clone_voice", fake_clone)
    resp = cli.post(
        "/xai/voices",
        data={"name": "My Grok Voice", "consent": "true", "description": "warm"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"voice_id": "cv-new", "name": "My Grok Voice"}
    assert seen == {"name": "My Grok Voice", "description": "warm", "path_exists": True}


def test_clone_no_key_returns_400(cli, monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite no key"))
    resp = cli.post(
        "/xai/voices",
        data={"name": "X", "consent": "true"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 400


def test_clone_runtime_error_maps_to_400(cli, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("xAI error 400: audio longer than 120 seconds")
    monkeypatch.setattr(xv, "clone_voice", boom)
    resp = cli.post(
        "/xai/voices",
        data={"name": "X", "consent": "true"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 400
    assert "120 seconds" in resp.json()["detail"]


def test_delete_ok(cli, monkeypatch):
    seen = {}
    monkeypatch.setattr(xv, "delete_voice", lambda vid: seen.update(deleted=vid))
    resp = cli.delete("/xai/voices/cv-1")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert seen["deleted"] == "cv-1"


def test_delete_runtime_error_maps_to_400(cli, monkeypatch):
    def boom(vid):
        raise RuntimeError("xAI error 404: voice not found")
    monkeypatch.setattr(xv, "delete_voice", boom)
    resp = cli.delete("/xai/voices/nope")
    assert resp.status_code == 400
```

**Step 2: Run test to verify it fails**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voice_routes.py -x -q
```
Expected: FAIL — first test gets HTTP 404 (`assert 404 == 200`); the routes don't exist yet.

**Step 3: Write minimal implementation**

Create `Orchestrator/routes/xai_voice_routes.py`:

```python
"""xAI Custom Voices routes — the Voice Lab xAI section + Grok cloned voices.

Thin consumers of ``Orchestrator.xai_voices`` (the provider module) — these
routes own ONLY transport concerns (multipart parsing, temp-file lifecycle,
error->HTTP mapping) and the CONSENT GATE on cloning, mirroring
elevenlabs_routes.py exactly. Cloning is refused with HTTP 422 unless the
caller passes ``consent="true"``. xAI enforces the <=120s reference-clip limit
server-side (surfaced as a 400 with the provider message).

GET /xai/voices doubles as the frontends' gating probe: no XAI_API_KEY ->
{"configured": false, "voices": []} and the Portal/Android xAI zones hide.
"""
import os
import tempfile

from fastapi import File, Form, HTTPException, UploadFile

from Orchestrator.checkpoint import app


@app.get("/xai/voices")
async def xai_voices_list():
    """List cloned voices. No key -> graceful unconfigured payload (zone hides)."""
    from Orchestrator import xai_voices
    try:
        voices = xai_voices.list_custom_voices()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if voices is None:
        return {"configured": False, "voices": []}
    return {"configured": True, "voices": voices}


@app.post("/xai/voices")
async def xai_voices_clone(
    name: str = Form(...),
    file: UploadFile = File(...),
    consent: str = Form(...),
    description: str = Form(None),
):
    """Clone a custom voice (multipart, ONE reference clip <=120s — xAI enforces
    the duration). CONSENT GATE: ``consent`` must be the literal string "true"
    or this 422s WITHOUT touching the provider — the UI must collect an explicit
    "I own / have permission to use this voice" confirmation first.

    The upload is streamed to a temp file, handed to ``clone_voice``, then
    removed in a finally (never leak the audio to disk). No key -> 400;
    provider RuntimeError -> 400 with the human message.
    """
    if consent != "true":
        raise HTTPException(status_code=422, detail="Voice cloning requires consent confirmation")

    from Orchestrator import xai_voices
    if not xai_voices.resolve_api_key():
        raise HTTPException(status_code=400, detail="xAI not configured - set XAI_API_KEY (onboarding wizard)")

    suffix = os.path.splitext(file.filename or "")[1] or ".mp3"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(await file.read())
        try:
            result = xai_voices.clone_voice(name, temp_path, description=description)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return {
        "voice_id": xai_voices.voice_id_of(result),
        "name": result.get("name", name),
    }


@app.delete("/xai/voices/{voice_id}")
async def xai_voices_delete(voice_id: str):
    """Delete a cloned voice. Provider RuntimeError -> 400."""
    from Orchestrator import xai_voices
    try:
        xai_voices.delete_voice(voice_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}
```

Then register in `Orchestrator/app.py` — Edit, anchored on line 99:

```
old_string: from Orchestrator.routes.elevenlabs_routes import *
new_string: from Orchestrator.routes.elevenlabs_routes import *
from Orchestrator.routes.xai_voice_routes import *
```

**Step 4: Run test to verify it passes**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voice_routes.py Orchestrator/tests/test_xai_voices_module.py -q
```
Expected: PASS (23 passed). Then confirm the live tree still imports (service runs from this tree):
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -c "import Orchestrator.app; print('app OK')"
```
Expected: `app OK`

**Step 5: Commit**
```
git add Orchestrator/routes/xai_voice_routes.py Orchestrator/app.py Orchestrator/tests/test_xai_voice_routes.py
git commit -m "feat(xai-voice): /xai/voices REST passthrough (list/clone/delete) with 422 consent gate"
```

---

### Task P6.24: ToolVault tool `xai_clone_voice` (consent-gated, mirrors elevenlabs_clone_voice)

**Files:**
- Create: ToolVault/tools/xai_clone_voice/schema.json
- Create: ToolVault/tools/xai_clone_voice/executor.py
- Test: Orchestrator/tests/test_xai_clone_voice_tool.py

**Step 1: Write the failing test**

```python
"""Consent-gate tests for the xai_clone_voice ToolVault executor.

The gate MUST refuse BEFORE any provider call when confirm_consent is not
explicitly true — mirroring elevenlabs_clone_voice verbatim. Provider calls are
monkeypatched on Orchestrator.xai_voices (imported inside the executor)."""
import asyncio

import pytest

from Orchestrator import xai_voices as xv
from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext


@pytest.fixture
def execute():
    ex = registry.get_executor("xai_clone_voice")
    assert ex is not None, f"executor failed to load: {registry.load_errors()}"
    return ex


def _ctx():
    return ToolContext(operator="TestOp", base_url="http://localhost:9091")


def test_tool_is_in_catalog():
    assert any(t["name"] == "xai_clone_voice" for t in registry.load_canonical())


def test_refuses_without_consent_and_never_calls_provider(execute, monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"ID3fakeaudio")
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite missing consent"))
    r = asyncio.run(execute(
        {"name": "V", "audio_path": str(sample), "confirm_consent": False}, _ctx()))
    assert r.success is False
    assert "confirm" in r.result.lower()


def test_refuses_missing_audio_file(execute, monkeypatch):
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite missing file"))
    r = asyncio.run(execute(
        {"name": "V", "audio_path": "/nope/missing.mp3", "confirm_consent": True}, _ctx()))
    assert r.success is False
    assert "not found" in r.result.lower()


def test_clones_with_consent(execute, monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"ID3fakeaudio")
    seen = {}

    def fake_clone(name, audio_path, description=None):
        seen.update(name=name, audio_path=audio_path, description=description)
        return {"voice_id": "cv-new", "name": name}

    monkeypatch.setattr(xv, "clone_voice", fake_clone)
    r = asyncio.run(execute(
        {"name": "My Grok Voice", "audio_path": str(sample),
         "confirm_consent": True, "description": "warm"}, _ctx()))
    assert r.success is True
    assert "cv-new" in r.result
    assert r.data == {"voice_id": "cv-new"}
    assert seen == {"name": "My Grok Voice", "audio_path": str(sample), "description": "warm"}


def test_provider_error_returns_failure_not_exception(execute, monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"x")

    def boom(*a, **k):
        raise RuntimeError("xAI error 400: audio longer than 120 seconds")

    monkeypatch.setattr(xv, "clone_voice", boom)
    r = asyncio.run(execute(
        {"name": "V", "audio_path": str(sample), "confirm_consent": True}, _ctx()))
    assert r.success is False
    assert "120 seconds" in r.result
```

**Step 2: Run test to verify it fails**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_clone_voice_tool.py -x -q
```
Expected: FAIL with `AssertionError: executor failed to load` (module doesn't exist yet).

**Step 3: Write minimal implementation**

Create `ToolVault/tools/xai_clone_voice/schema.json`:

```json
{
  "name": "xai_clone_voice",
  "description": "Clone a voice from ONE local audio sample file (max 120 seconds) using xAI Custom Voices. The cloned voice_id becomes selectable as a Grok realtime voice-session voice (and on the xAI phone line) — it is NOT a TTS-picker voice. Use when the user wants a custom Grok voice from a recording they provide (e.g. an uploaded audio sample). REQUIRES explicit consent: you must first confirm the user owns or has permission to clone the voice, then pass confirm_consent=true.",
  "category": "audio",
  "groups": ["chat", "chat_cu", "mcp"],
  "tier": 2,
  "parameters": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Display name for the cloned voice (how it will appear in the Grok voice list)."
      },
      "audio_path": {
        "type": "string",
        "description": "Local file path to ONE audio sample to clone from, at most 120 seconds long (e.g. a session-upload path)."
      },
      "confirm_consent": {
        "type": "boolean",
        "description": "Set true ONLY after the user has explicitly confirmed they own or have permission to clone this voice. You MUST ask and get confirmation first — refuse otherwise."
      },
      "description": {
        "type": "string",
        "description": "Optional short description of the voice."
      }
    },
    "required": ["name", "audio_path", "confirm_consent"]
  },
  "returns": "The cloned voice's xAI voice_id and a confirmation it is selectable as a Grok session voice.",
  "example": "xai_clone_voice(name=\"My Narrator\", audio_path=\"/path/to/sample.mp3\", confirm_consent=true)",
  "notes": "Consent gate is enforced in the executor: without confirm_consent=true NO voice is created. xAI accepts ONE reference clip of at most 120 seconds (enforced server-side). The cloned id is a Grok session voice, not an ElevenLabs/TTS voice."
}
```

Create `ToolVault/tools/xai_clone_voice/executor.py` (consent gate mirrored verbatim from elevenlabs_clone_voice):

```python
"""Executor for xai_clone_voice — xAI Custom Voices cloning with a consent gate.

Calls ``Orchestrator.xai_voices.clone_voice`` DIRECTLY (in-process, no HTTP).
The consent gate lives here: without ``confirm_consent=true`` we refuse and no
voice is ever created. ``audio_path`` must exist on disk (ONE clip, <=120s —
xAI enforces the duration server-side).
"""
import os

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    name = (params.get("name") or "").strip()
    audio_path = (params.get("audio_path") or "").strip()
    confirm_consent = bool(params.get("confirm_consent"))
    description = params.get("description")

    if not name:
        return ToolResult(False, "name is required to clone a voice.")
    if not audio_path:
        return ToolResult(False, "audio_path is required (one local audio sample, max 120 seconds).")

    # Consent gate — refuse BEFORE any provider call when not explicitly confirmed.
    if not confirm_consent:
        return ToolResult(
            False,
            "I can't clone a voice without explicit confirmation you have the "
            "right to use it. Please confirm.",
        )

    if not os.path.exists(audio_path):
        return ToolResult(False, f"Audio file not found: {audio_path}")

    import asyncio

    from Orchestrator import xai_voices

    try:
        result = await asyncio.to_thread(
            xai_voices.clone_voice, name, audio_path, description=description
        )
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    voice_id = xai_voices.voice_id_of(result)
    return ToolResult(
        True,
        f"Cloned '{name}' (xAI voice_id {voice_id}) — selectable as a Grok session voice now.",
        data={"voice_id": voice_id},
    )
```

**Step 4: Validate, run tests, make it live**
Run (from repo root):
```
Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate
```
Expected: exit 0, no errors for `xai_clone_voice`.
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_clone_voice_tool.py -x -q
```
Expected: PASS (5 passed).
```
curl -s -X POST http://localhost:9091/toolvault/reload
```
Expected: JSON reply with no errors (tool re-embedded + discoverable — DO NOT hand-edit `ToolVault/embeddings.json`).

**Step 5: Commit**
```
git add ToolVault/tools/xai_clone_voice/schema.json ToolVault/tools/xai_clone_voice/executor.py Orchestrator/tests/test_xai_clone_voice_tool.py
git commit -m "feat(xai-voice): xai_clone_voice ToolVault tool with elevenlabs-parity consent gate"
```

---

### Task P6.25: Grok sessions accept cloned voice_ids (catalog OR verified custom, fail-open)

**Files:**
- Modify: Orchestrator/routes/grok_live_routes.py:296-299 (the `# Validate voice` block inside `configure_grok_session`)
- Test: Orchestrator/tests/test_grok_voice_resolution.py

**Step 1: Write the failing test**

```python
"""Tests for resolve_grok_voice — Grok session voice validation now accepts
catalog voices OR a cloned xAI custom-voice id (verified via the 60s-cached
is_custom_voice; fail-open to catalog default when unverifiable)."""
import asyncio

import pytest

from Orchestrator import xai_voices as xv
from Orchestrator.config import GROK_LIVE_DEFAULT_VOICE, GROK_LIVE_VOICES
from Orchestrator.routes.grok_live_routes import resolve_grok_voice


def test_catalog_voice_passes_without_network(monkeypatch):
    monkeypatch.setattr(
        xv, "is_custom_voice",
        lambda vid: pytest.fail("is_custom_voice called for a catalog voice"))
    voice = GROK_LIVE_VOICES[0]
    assert asyncio.run(resolve_grok_voice(voice)) == voice


def test_verified_custom_voice_is_accepted(monkeypatch):
    monkeypatch.setattr(xv, "is_custom_voice", lambda vid: vid == "cv-cloned-1")
    assert asyncio.run(resolve_grok_voice("cv-cloned-1")) == "cv-cloned-1"


def test_unverified_id_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(xv, "is_custom_voice", lambda vid: False)
    assert asyncio.run(resolve_grok_voice("cv-unknown")) == GROK_LIVE_DEFAULT_VOICE


def test_verifier_exception_falls_back_to_default(monkeypatch):
    def boom(vid):
        raise Exception("unexpected")
    monkeypatch.setattr(xv, "is_custom_voice", boom)
    assert asyncio.run(resolve_grok_voice("cv-unknown")) == GROK_LIVE_DEFAULT_VOICE
```

**Step 2: Run test to verify it fails**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_voice_resolution.py -x -q
```
Expected: FAIL with `ImportError: cannot import name 'resolve_grok_voice'`

**Step 3: Write minimal implementation**

In `Orchestrator/routes/grok_live_routes.py`, add the helper directly ABOVE `async def configure_grok_session` (line 282; `asyncio` is already imported at line 21):

```python
async def resolve_grok_voice(voice: str) -> str:
    """Resolve a requested session voice: a built-in catalog voice passes
    through; any other id is accepted ONLY if it verifies as a cloned xAI
    custom voice (GET /v1/custom-voices via xai_voices.is_custom_voice —
    60s cache, FAIL-OPEN to the catalog default when xAI is unreachable or
    unconfigured). Anything unverifiable falls back to the default voice.
    """
    if voice in GROK_LIVE_VOICES:
        return voice
    from Orchestrator import xai_voices
    try:
        if await asyncio.to_thread(xai_voices.is_custom_voice, voice):
            return voice
    except Exception:
        pass  # fail-open to catalog-only
    return GROK_LIVE_DEFAULT_VOICE
```

Then replace the validation block (Edit — current lines 296-299):

```
old_string:
    # Validate voice
    if voice not in GROK_LIVE_VOICES:
        voice = GROK_LIVE_DEFAULT_VOICE
    session.voice = voice

new_string:
    # Validate voice — built-in catalog OR a verified cloned xAI custom voice id.
    voice = await resolve_grok_voice(voice)
    session.voice = voice
```

**Step 4: Run test to verify it passes**
Run:
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_grok_voice_resolution.py -x -q && /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -c "import Orchestrator.app; print('app OK')"
```
Expected: PASS (4 passed) and `app OK`

**Step 5: Commit**
```
git add Orchestrator/routes/grok_live_routes.py Orchestrator/tests/test_grok_voice_resolution.py
git commit -m "feat(xai-voice): Grok sessions accept cloned custom voice_ids (60s-cached verify, fail-open)"
```

---

### Task P6.26: Portal Voice Lab — xAI (Grok) voices zone

**Files:**
- Modify: Portal/voice-lab.js (zone HTML after line 171; static wiring after line 186; new zone-4 section before the Open/close section at line 638; `loadXaiVoices()` call in `openVoiceLab` after line 661)
- Modify: Portal/index.html:11,21 (version bump `?v=genui309` → `?v=genui310`; re-check the current number first — it may have moved)

No JS unit harness exists for Portal modules; verification is a syntax gate + live endpoint check (steps 3-4).

**Step 1: Add Zone 4 markup**

In `ensureModal()`'s `modal.innerHTML`, insert AFTER the Zone 3 `</section>` (line 171) and BEFORE the `vlab-foot-hint` paragraph:

```html
            <!-- ── Zone 4: Grok (xAI) voices — hidden until GET /xai/voices says configured ── -->
            <section class="vlab-zone" id="vlabXaiZone" hidden>
              <h4 class="vlab-zone-title">Grok (xAI) voices</h4>
              <p class="vlab-zone-hint">Clone a voice for Grok voice sessions — one clip, max 120 seconds.
                Cloned voices are selectable in Grok voice mode (not the TTS picker).</p>
              <input id="vlabXaiFile" class="vlab-file-input" type="file"
                     accept="audio/wav,audio/mpeg,audio/mp3,audio/x-m4a,audio/mp4,audio/webm,.wav,.mp3,.m4a,.webm" />
              <input id="vlabXaiName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
              <input id="vlabXaiDesc" class="vlab-input" type="text" placeholder="Description (optional)" autocomplete="off" />
              <label class="vlab-consent">
                <input id="vlabXaiConsent" type="checkbox" />
                <span>I confirm I own this voice or have permission to clone it.</span>
              </label>
              <div class="vlab-row vlab-row-end">
                <button id="vlabXaiCloneBtn" class="vlab-btn vlab-btn-accent" type="button" disabled>Clone Grok voice</button>
              </div>
              <div id="vlabXaiStatus" class="vlab-status"></div>
              <div id="vlabXaiList" class="vlab-my-list"></div>
            </section>
```

And add static wiring in `ensureModal()` immediately after the design-zone wiring (after line 186 `modal.querySelector('#vlabDesignSaveBtn').addEventListener('click', saveDesign);`):

```js
    // ── xAI zone wiring (static; gate/list is refreshed per-open) ──
    modal.querySelector('#vlabXaiFile').addEventListener('change', refreshXaiCloneButton);
    modal.querySelector('#vlabXaiName').addEventListener('input', refreshXaiCloneButton);
    modal.querySelector('#vlabXaiConsent').addEventListener('change', refreshXaiCloneButton);
    modal.querySelector('#vlabXaiCloneBtn').addEventListener('click', submitXaiClone);
```

**Step 2: Add the Zone 4 functions**

Insert a new section immediately BEFORE the `// Open / close` banner (line 638), mirroring the structure/consent/delete-confirm conventions of Zones 1+3:

```js
// =============================================================================
// Zone 4 — Grok (xAI) voices (clone / list / delete). Gated on GET /xai/voices
// `configured` — no XAI key, or xAI unreachable, hides the whole zone. Cloned
// ids are Grok SESSION voices, so no populateVoiceCatalog() refresh here.
// =============================================================================

/** Clone button enabled only when name AND one file AND consent (mirrors Zone 1). */
function refreshXaiCloneButton() {
    const btn = document.getElementById('vlabXaiCloneBtn');
    if (!btn) return;
    const name = (document.getElementById('vlabXaiName')?.value || '').trim();
    const consent = !!document.getElementById('vlabXaiConsent')?.checked;
    const hasFile = !!(document.getElementById('vlabXaiFile')?.files || []).length;
    btn.disabled = !(name && consent && hasFile);
}

async function submitXaiClone() {
    const btn = document.getElementById('vlabXaiCloneBtn');
    if (!btn || btn.disabled) return;
    const name = (document.getElementById('vlabXaiName').value || '').trim();
    const description = (document.getElementById('vlabXaiDesc').value || '').trim();
    const file = document.getElementById('vlabXaiFile').files[0];

    const fd = new FormData();
    fd.append('name', name);
    fd.append('consent', 'true');
    if (description) fd.append('description', description);
    fd.append('file', file, file.name);

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Cloning…';
    try {
        const res = await fetch('/xai/voices', { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Clone failed: ${msg}`);
            btn.disabled = false;
            btn.textContent = original;
            return;
        }
        toastSuccess(`Cloned "${name}" — selectable as a Grok voice`);
        document.getElementById('vlabXaiName').value = '';
        document.getElementById('vlabXaiDesc').value = '';
        document.getElementById('vlabXaiFile').value = '';
        document.getElementById('vlabXaiConsent').checked = false;
        btn.textContent = original;
        await loadXaiVoices();
    } catch (err) {
        toastError(`Clone failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = original;
    }
}

/** Fetch /xai/voices; show the zone only when the box has a working XAI key. */
async function loadXaiVoices() {
    const zone = document.getElementById('vlabXaiZone');
    const listEl = document.getElementById('vlabXaiList');
    const statusEl = document.getElementById('vlabXaiStatus');
    if (!zone || !listEl) return;
    try {
        const res = await fetch('/xai/voices');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data.configured) { zone.hidden = true; return; }
        zone.hidden = false;
        const voices = data.voices || [];
        if (statusEl) statusEl.textContent = voices.length
            ? `${voices.length} cloned voice${voices.length === 1 ? '' : 's'}` : '';
        listEl.innerHTML = voices.length ? '' : '<div class="vlab-empty">No cloned Grok voices yet.</div>';
        for (const v of voices) renderXaiVoiceRow(v, listEl);
    } catch {
        zone.hidden = true;   // fail quiet — unreachable == unconfigured for the UI
    }
}

function renderXaiVoiceRow(v, listEl) {
    const id = v.voice_id || v.id || '';
    const row = document.createElement('div');
    row.className = 'vlab-voice-row';
    row.innerHTML = `
        <div class="vlab-voice-main">
          <div class="vlab-voice-name">${escapeHtml(v.name || 'Unnamed voice')}</div>
          <div class="vlab-voice-desc">${escapeHtml(id)}</div>
        </div>
        <button class="vlab-btn vlab-delete-btn" type="button">Delete</button>`;
    row.querySelector('.vlab-delete-btn').addEventListener('click',
        (e) => deleteXaiVoice(id, v.name, e.currentTarget));
    listEl.appendChild(row);
}

async function deleteXaiVoice(voiceId, name, btn) {
    if (btn.disabled) return;
    if (!window.confirm(`Delete Grok voice "${name || voiceId}"? This cannot be undone.`)) return;
    btn.disabled = true;
    try {
        const res = await fetch(`/xai/voices/${encodeURIComponent(voiceId)}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Delete failed: ${msg}`);
            btn.disabled = false;
            return;
        }
        toastSuccess(`Deleted "${name || voiceId}"`);
        await loadXaiVoices();
    } catch (err) {
        toastError(`Delete failed: ${err.message}`);
        btn.disabled = false;
    }
}
```

In `openVoiceLab()` add the xAI refresh after the existing `loadMyVoices();` call (line 661):

```js
    // Load the account's voices for the manage zone.
    loadMyVoices();

    // Gate + load the Grok (xAI) zone — hidden when no XAI key.
    loadXaiVoices();
```

Also update the file-header comment block (lines 1-35) with one line documenting zone 4: `*   4. Grok (xAI) voices — clone (≤120s clip + consent) / list / delete via /xai/voices; gated on GET /xai/voices configured.` Then bump `?v=genui309` → `?v=genui310` on Portal/index.html lines 11 and 21 (verify the current number first — bump whatever is there by one, updating the trailing comment to mention the Voice Lab xAI zone).

**Step 3: Syntax gate**
Run:
```
cp "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/voice-lab.js" /tmp/claude-1000/-home-ai-black-box-fc-Desktop-blackbox-poc--blackbox-poc/af84acfb-beb2-44ac-913d-01b50da5176d/scratchpad/voice-lab-check.mjs && node --check /tmp/claude-1000/-home-ai-black-box-fc-Desktop-blackbox-poc--blackbox-poc/af84acfb-beb2-44ac-913d-01b50da5176d/scratchpad/voice-lab-check.mjs && echo SYNTAX_OK
```
Expected: `SYNTAX_OK`

**Step 4: Live endpoint sanity**
Run:
```
curl -s http://localhost:9091/xai/voices
```
Expected: `{"configured": true, "voices": [...]}` on this box (XAI key present) — or `{"configured": false, "voices": []}` on a keyless box; either proves the gate contract the zone consumes.

**Step 5: Commit**
```
git add Portal/voice-lab.js Portal/index.html
git commit -m "feat(xai-voice): Voice Lab Grok (xAI) zone — clone/list/delete, gated on /xai/voices"
```

---

### Task P6.27: Android — VoiceLabRepository xAI endpoints + parse test

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/repository/VoiceLabRepository.kt` (data classes near line 105; methods after `addLibraryVoice` ends at line 303, before the `// Helpers` banner at line 305)
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/repository/XaiVoiceParsingTest.kt`

**Step 1: Write the failing test**

```kotlin
package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * parseXaiVoicesResponse — the GET /xai/voices contract the xAI Voice Lab zone
 * consumes: {configured, voices:[{voice_id|id, name}]}. Tolerant of both id key
 * namings (probe P6.20) and sparse rows.
 */
class XaiVoiceParsingTest {

    @Test
    fun `parses configured with voices`() {
        val raw = """{"configured": true, "voices": [
            {"voice_id": "cv-1", "name": "Narrator"},
            {"id": "cv-2", "name": "Alt"}
        ]}"""
        val res = parseXaiVoicesResponse(raw)
        assertTrue(res.configured)
        assertEquals(2, res.voices.size)
        assertEquals(XaiVoice("cv-1", "Narrator"), res.voices[0])
        assertEquals(XaiVoice("cv-2", "Alt"), res.voices[1])
    }

    @Test
    fun `unconfigured yields empty`() {
        val res = parseXaiVoicesResponse("""{"configured": false, "voices": []}""")
        assertFalse(res.configured)
        assertTrue(res.voices.isEmpty())
    }

    @Test
    fun `row without any id is skipped and missing name falls back to id`() {
        val raw = """{"configured": true, "voices": [
            {"name": "no-id-row"},
            {"voice_id": "cv-3"}
        ]}"""
        val res = parseXaiVoicesResponse(raw)
        assertEquals(1, res.voices.size)
        assertEquals(XaiVoice("cv-3", "cv-3"), res.voices[0])
    }

    @Test
    fun `garbage-free on unknown extra keys`() {
        val raw = """{"configured": true, "extra": 1, "voices": [
            {"voice_id": "cv-4", "name": "V", "created_at": "2026-07-11"}
        ]}"""
        assertEquals(1, parseXaiVoicesResponse(raw).voices.size)
    }
}
```

**Step 2: Run test to verify it fails**
Run (from `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`):
```
./gradlew :app:testDebugUnitTest --offline
```
Expected: FAIL — compilation error `Unresolved reference: parseXaiVoicesResponse` / `XaiVoice`

**Step 3: Write minimal implementation**

In `VoiceLabRepository.kt`, add after the `SharedVoice` data class (ends line 105, before `class VoiceLabRepository`):

```kotlin
/** One xAI custom (cloned) Grok voice. Tolerates voice_id|id key naming. */
data class XaiVoice(val voiceId: String, val name: String)

/** GET /xai/voices → gating (configured=false hides the zone) + cloned voices. */
data class XaiVoicesResult(val configured: Boolean, val voices: List<XaiVoice>)

/** Top-level so the offline unit test can exercise the wire contract directly. */
internal fun parseXaiVoicesResponse(raw: String): XaiVoicesResult {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    val o = j.parseToJsonElement(raw).jsonObject
    val configured = o["configured"]?.jsonPrimitive?.content?.toBoolean() ?: false
    val voices = (o["voices"]?.jsonArray ?: JsonArray(emptyList())).mapNotNull { el ->
        val vo = el.jsonObject
        val id = vo["voice_id"]?.jsonPrimitive?.contentOrNull
            ?: vo["id"]?.jsonPrimitive?.contentOrNull
            ?: return@mapNotNull null
        XaiVoice(
            voiceId = id,
            name = vo["name"]?.jsonPrimitive?.contentOrNull ?: id,
        )
    }
    return XaiVoicesResult(configured, voices)
}
```

Then add the three endpoint methods inside `class VoiceLabRepository`, after `addLibraryVoice` (line 303) and before the `// Helpers` banner (line 305):

```kotlin
    // -------------------------------------------------------------------------
    // xAI (Grok) custom voices — GET/POST/DELETE /xai/voices
    //   Cloned ids are Grok SESSION voices (not TTS-picker voices). The clone
    //   path mirrors cloneVoice()'s manual multipart; the backend consent gate
    //   422s unless consent == "true".
    // -------------------------------------------------------------------------
    suspend fun fetchXaiVoices(): XaiVoicesResult =
        parseXaiVoicesResponse(api.get("/xai/voices"))

    suspend fun cloneXaiVoice(
        name: String,
        file: File,
        consent: Boolean,
        description: String = "",
    ): String {
        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("name", name)
            .addFormDataPart("consent", if (consent) "true" else "false")
        if (description.isNotBlank()) builder.addFormDataPart("description", description)
        builder.addFormDataPart("file", file.name, file.asRequestBody(mediaTypeFor(file.name)))
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/xai/voices")
            .header("X-BlackBox-Client", "native-android/1.0")
            .post(builder.build())
            .build()
        api.getClient().newCall(request).execute().use { response ->
            val body = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                throw VoiceLabException(response.code, extractError(body, response.code))
            }
            val o = json.parseToJsonElement(body).jsonObject
            return o["voice_id"]?.jsonPrimitive?.contentOrNull ?: ""
        }
    }

    suspend fun deleteXaiVoice(voiceId: String): Boolean {
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/xai/voices/$voiceId")
            .header("X-BlackBox-Client", "native-android/1.0")
            .delete()
            .build()
        api.getClient().newCall(request).execute().use { response ->
            val body = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                throw VoiceLabException(response.code, extractError(body, response.code))
            }
            val o = json.parseToJsonElement(body).jsonObject
            return o["ok"]?.jsonPrimitive?.content?.toBoolean() ?: false
        }
    }
```

**Step 4: Run test to verify it passes**
Run (same Android dir):
```
./gradlew :app:testDebugUnitTest --offline
```
Expected: PASS (BUILD SUCCESSFUL; XaiVoiceParsingTest 4/4 green, zero pre-existing regressions)

**Step 5: Commit**
```
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/repository/VoiceLabRepository.kt" "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/repository/XaiVoiceParsingTest.kt"
git commit -m "feat(android/xai-voice): VoiceLabRepository /xai/voices endpoints + parse test"
```

---

### Task P6.28: Android — VoiceLabViewModel xAI state + actions

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voicelab/VoiceLabViewModel.kt` (state block after line 114; `initialize` at 120-126; actions after `addLibraryVoice` ends line 402; `onCleared` at 404-411)

Pure ViewModel plumbing over the tested repository — verification is the offline unit-test gate (compile + zero regressions).

**Step 1: Add xAI state flows**

After the Browse Library state block (line 114, before `companion object`):

```kotlin
    // ── xAI (Grok) custom voices zone ────────────────────────────────────────
    private val _xaiConfigured = MutableStateFlow(false)
    val xaiConfigured: StateFlow<Boolean> = _xaiConfigured.asStateFlow()
    private val _xaiVoices = MutableStateFlow<List<XaiVoice>>(emptyList())
    val xaiVoices: StateFlow<List<XaiVoice>> = _xaiVoices.asStateFlow()
    private val _xaiCloneState = MutableStateFlow(CloneState.IDLE)
    val xaiCloneState: StateFlow<CloneState> = _xaiCloneState.asStateFlow()
    private val _xaiCloneError = MutableStateFlow<String?>(null)
    val xaiCloneError: StateFlow<String?> = _xaiCloneError.asStateFlow()
    private val _xaiClonePart = MutableStateFlow<ClonePart?>(null)
    val xaiClonePart: StateFlow<ClonePart?> = _xaiClonePart.asStateFlow()
```

Add the import alongside the other repository imports (after line 14 `import com.aiblackbox.portal.data.repository.VoiceLabRepository`):

```kotlin
import com.aiblackbox.portal.data.repository.XaiVoice
```

**Step 2: Wire the load into initialize + add actions**

In `initialize` (line 120-126), after `refreshStatus()` add `loadXaiVoices()` (xAI gating is independent of the ElevenLabs key):

```kotlin
    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        originBase = origin
        api = BlackBoxApi(origin)
        repo = VoiceLabRepository(api!!)
        refreshStatus()
        loadXaiVoices()   // xAI zone gates on its own key, independent of ElevenLabs
    }
```

Append the actions after `addLibraryVoice` (line 402), before `onCleared`:

```kotlin
    // ── xAI (Grok) custom voices ───────────────────────────────────────────────
    fun loadXaiVoices() {
        val repo = repo ?: return
        viewModelScope.launch {
            try {
                val res = repo.fetchXaiVoices()
                _xaiConfigured.value = res.configured
                _xaiVoices.value = res.voices
            } catch (_: Exception) {
                _xaiConfigured.value = false   // unreachable == unconfigured (zone hides)
            }
        }
    }

    /** Queue ONE picked clip (xAI clones from a single ≤120s reference). */
    fun addXaiPickedFile(uri: Uri) {
        viewModelScope.launch {
            try {
                val part = withContext(Dispatchers.IO) { copyUriToCache(uri) }
                _xaiClonePart.value?.let { runCatching { it.file.delete() } }
                _xaiClonePart.value = part
                _xaiCloneError.value = null
            } catch (e: Exception) {
                _message.value = "Couldn't read file: ${e.message}"
            }
        }
    }

    fun clearXaiPart() {
        _xaiClonePart.value?.let { runCatching { it.file.delete() } }
        _xaiClonePart.value = null
    }

    fun submitXaiClone(name: String, description: String, consent: Boolean) {
        val repo = repo ?: return
        val part = _xaiClonePart.value
        if (name.isBlank() || part == null || !consent) {
            _xaiCloneError.value = "Name, one clip (max 120s), and consent are required."
            return
        }
        _xaiCloneState.value = CloneState.SUBMITTING
        _xaiCloneError.value = null
        viewModelScope.launch {
            try {
                repo.cloneXaiVoice(name.trim(), part.file, consent, description.trim())
                _xaiCloneState.value = CloneState.IDLE
                _message.value = "Grok voice \"${name.trim()}\" cloned."
                clearXaiPart()
                loadXaiVoices()
            } catch (e: VoiceLabException) {
                _xaiCloneState.value = CloneState.IDLE
                _xaiCloneError.value = when (e.status) {
                    422 -> "Consent is required to clone a voice."
                    400 -> "Clone rejected: ${e.message}"
                    else -> e.message
                }
            } catch (e: Exception) {
                _xaiCloneState.value = CloneState.IDLE
                _xaiCloneError.value = e.message ?: "Clone failed"
            }
        }
    }

    fun deleteXaiVoice(voiceId: String) {
        val repo = repo ?: return
        viewModelScope.launch {
            try {
                repo.deleteXaiVoice(voiceId)
                _message.value = "Grok voice deleted."
                loadXaiVoices()
            } catch (e: Exception) {
                _message.value = "Delete failed: ${e.message}"
            }
        }
    }
```

In `onCleared` (lines 404-411), add cleanup of the queued xAI clip after the existing clone-parts cleanup line:

```kotlin
        _xaiClonePart.value?.let { runCatching { it.file.delete() } }
```

**Step 3: Compile + regression gate**
Run (from `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`):
```
./gradlew :app:testDebugUnitTest --offline
```
Expected: BUILD SUCCESSFUL, zero test regressions

**Step 4: Commit**
```
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voicelab/VoiceLabViewModel.kt"
git commit -m "feat(android/xai-voice): VoiceLabViewModel xAI zone state + clone/list/delete actions"
```

---

### Task P6.29: Android — VoiceLabScreen xAI zone UI

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voicelab/VoiceLabScreen.kt` (zone call after the `when` block ends at line 174; new composable after `ManageZone`)

**Step 1: Render the zone (independent of ElevenLabs gating)**

In `VoiceLabScreen`, the xAI zone must render even when ElevenLabs is NOT configured (its own `configured` gate lives inside the composable). Edit the `when` block's closing (lines 158-174):

```kotlin
            when {
                !statusLoaded -> {
                    LoadingBlock("Checking ElevenLabs…")
                }
                status?.configured != true -> {
                    NotConfiguredCard()
                }
                else -> {
                    CloneZone(viewModel, context, view)
                    Spacer(Modifier.height(16.dp))
                    DesignZone(viewModel, view)
                    Spacer(Modifier.height(16.dp))
                    BrowseLibraryZone(viewModel, view)
                    Spacer(Modifier.height(16.dp))
                    ManageZone(viewModel, view)
                }
            }

            // Grok (xAI) voices — own gate (GET /xai/voices configured), independent
            // of the ElevenLabs key. Renders nothing when unconfigured.
            XaiZone(viewModel, view)
```

**Step 2: Add the XaiZone composable**

Insert after the `ManageZone` composable (before the `SectionCard` helper at line 847), reusing the existing building blocks (`SectionCard`, `FieldLabel`, `InputBox`, `PrimaryButton`, `PillAction`, `ConsentRow`, `ErrorText` — signatures at lines 848-981):

```kotlin
// =============================================================================
// Zone: Grok (xAI) voices — clone (one ≤120s clip + consent) / list / delete.
// Gated on GET /xai/voices `configured`; cloned ids are Grok SESSION voices.
// =============================================================================

@Composable
private fun XaiZone(viewModel: VoiceLabViewModel, view: android.view.View) {
    val configured by viewModel.xaiConfigured.collectAsState()
    if (!configured) return

    val voices by viewModel.xaiVoices.collectAsState()
    val cloneState by viewModel.xaiCloneState.collectAsState()
    val cloneError by viewModel.xaiCloneError.collectAsState()
    val part by viewModel.xaiClonePart.collectAsState()

    var name by remember { mutableStateOf("") }
    var description by remember { mutableStateOf("") }
    var consent by remember { mutableStateOf(false) }

    val filePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri -> uri?.let { viewModel.addXaiPickedFile(it) } }

    Spacer(Modifier.height(16.dp))
    SectionCard(title = "🤖  Grok (xAI) voices") {
        Text(
            "Clone a voice for Grok voice sessions — one clip, max 120 seconds.",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500,
        )
        Spacer(Modifier.height(10.dp))

        // ── One reference clip ──
        PillAction(
            label = if (part == null) "Pick audio" else "Replace audio",
            enabled = cloneState != CloneState.SUBMITTING,
            onClick = { filePicker.launch("audio/*") },
        )
        part?.let { p ->
            Spacer(Modifier.height(8.dp))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(RadiusSm))
                    .background(Neutral150OrSurface())
                    .padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    p.displayName,
                    color = BbxWhite,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.weight(1f),
                )
                Box(
                    modifier = Modifier
                        .size(28.dp)
                        .clip(CircleShape)
                        .clickFeedback(enabled = cloneState != CloneState.SUBMITTING) {
                            viewModel.clearXaiPart()
                        },
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(Icons.Filled.Close, contentDescription = "Remove", tint = Neutral700, modifier = Modifier.size(18.dp))
                }
            }
        }

        Spacer(Modifier.height(12.dp))
        FieldLabel("Voice name")
        InputBox(
            value = name,
            onValueChange = { name = it },
            placeholder = "e.g. My Grok Voice",
            enabled = true,
            singleLine = true,
        )

        Spacer(Modifier.height(12.dp))
        FieldLabel("Description (optional)")
        InputBox(
            value = description,
            onValueChange = { description = it },
            placeholder = "Tone, accent, intended use…",
            enabled = true,
            minHeight = 56.dp,
        )

        Spacer(Modifier.height(12.dp))
        ConsentRow(checked = consent, enabled = true, onToggle = { consent = !consent })

        Spacer(Modifier.height(16.dp))
        val canSubmit = name.isNotBlank() && part != null && consent &&
            cloneState != CloneState.SUBMITTING
        PrimaryButton(
            label = if (cloneState == CloneState.SUBMITTING) "Cloning…" else "Clone Grok voice",
            enabled = canSubmit,
            loading = cloneState == CloneState.SUBMITTING,
            onClick = { viewModel.submitXaiClone(name, description, consent) },
        )
        AnimatedVisibility(visible = cloneError != null) {
            cloneError?.let { ErrorText(it) }
        }

        // ── Cloned voices list + delete ──
        Spacer(Modifier.height(16.dp))
        FieldLabel("My Grok voices")
        if (voices.isEmpty()) {
            Text(
                "No cloned Grok voices yet.",
                color = Neutral500,
                style = MaterialTheme.typography.bodySmall,
            )
        } else {
            voices.forEach { v ->
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 3.dp)
                        .clip(RoundedCornerShape(RadiusSm))
                        .background(Neutral150OrSurface())
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(v.name, color = BbxWhite, style = MaterialTheme.typography.bodyMedium)
                        Text(v.voiceId, color = Neutral500, style = MaterialTheme.typography.bodySmall)
                    }
                    Box(
                        modifier = Modifier
                            .size(28.dp)
                            .clip(CircleShape)
                            .clickFeedback(enabled = true) {
                                view.performPressFeedback()
                                viewModel.deleteXaiVoice(v.voiceId)
                            },
                        contentAlignment = Alignment.Center,
                    ) {
                        Icon(Icons.Filled.Delete, contentDescription = "Delete", tint = BbxRed, modifier = Modifier.size(18.dp))
                    }
                }
            }
        }
    }
}
```

Also update the file-header comment (lines 92-105) with one line: `//   4) Grok (xAI) — clone one ≤120s clip + consent → cloned voice_ids usable in Grok voice sessions (own /xai/voices gate).`

**Step 3: Compile + regression gate**
Run (from `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`):
```
./gradlew :app:testDebugUnitTest --offline
```
Expected: BUILD SUCCESSFUL, zero test regressions

**Step 4: Full-phase backend regression sweep**
Run (from repo root):
```
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_xai_voices_module.py Orchestrator/tests/test_xai_voice_routes.py Orchestrator/tests/test_xai_clone_voice_tool.py Orchestrator/tests/test_grok_voice_resolution.py Orchestrator/tests/test_elevenlabs_voice_routes.py -q
```
Expected: PASS (all green — the elevenlabs suite proves no route collision/regression)

**Step 5: Commit**
```
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/voicelab/VoiceLabScreen.kt"
git commit -m "feat(android/xai-voice): Voice Lab Grok (xAI) zone — clone/list/delete UI"
```

---

**Phase 6c notes for the executor:**
- P6.20's probe is a GATE: if it contradicts the assumed field names (`file`, `name`, `voice_id`, list envelope), fix P6.21's provider code (and only it — every other layer goes through `Orchestrator/xai_voices.py`).
- The service runs LIVE from this tree — after P6.23 the routes are live without restart only if the running process re-imports; a `sudo systemctl restart blackbox.service` (pre-authorized) after P6.25 makes the whole workstream live at once. End-to-end check afterwards: `curl -s http://localhost:9091/xai/voices` and a Grok voice session opened with a cloned `voice` id.
- Cloned xAI voice_ids are Grok SESSION voices (grok_live `voice` param + the future xAI phone line), NOT `/tts/catalog` entries — no TTS-picker integration anywhere in this phase.

---

