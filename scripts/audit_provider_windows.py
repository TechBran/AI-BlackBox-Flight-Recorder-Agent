#!/usr/bin/env python3
"""M3 / WI-10 (verification half): provider context-window audit — MEASUREMENT ONLY.

Drives the production `/chat/stream` POST endpoint (our real transport: retrieval →
injection → provider SSE relay) with deterministic synthetic payloads at three sizes:

  -  75,000 chars — today's PROVIDER_CAPS["anthropic"] delivery cap
  - 210,000 chars — Brandon's 2026-04-25 Opus adaptive-thinking stall repro
  - 238,000 chars — worst-case assembled context (16 snapshots x corpus p99 ~14.9k)

per chat provider (gemini, anthropic, openai, xai), SEQUENTIALLY, recording TTFB
(first provider token), total time, completion, and errors. No retries; failures
are recorded and the run moves on.

LEDGER SAFETY (the volume is immutable production memory):
  * /chat/stream persists NOTHING (recon 2026-07-02: persistence lives only in
    POST /chat/save and the POST /chat task worker — neither is touched here).
  * The synthetic payload rides in a SYSTEM message (production fossil-context
    shape; also bypasses the anthropic-path 15k per-message history truncation).
  * The user message forbids tool calls and demands a literal "OK".
  * Between probes the script asserts the probe marker/operator never appears in
    Volumes/SNAPSHOT_VOLUME.txt and hard-aborts if it ever does.

Idempotent + re-runnable: results are (re)written to the JSON artifact after every
probe, keyed by (provider, chars); re-running overwrites in place.

Usage:
  Orchestrator/venv/bin/python scripts/audit_provider_windows.py [--only PROVIDER]
      [--sizes 75000,210000,238000] [--base http://localhost:9091]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402  (venv dependency, after sys.path setup for parity)

from Orchestrator.tokenization import estimate_tokens  # noqa: E402

MARKER = "M3WINPROBE"
OPERATOR = "M3-Window-Probe"  # unknown operator => all four retrieval sources empty
VOLUME = REPO_ROOT / "Volumes" / "SNAPSHOT_VOLUME.txt"
ARTIFACT = REPO_ROOT / "docs" / "plans" / "artifacts" / "2026-07-02-provider-window-probes.json"

HARD_TIMEOUT_S = 240.0
INTER_PROBE_SLEEP_S = 20.0

# (provider param for /chat/stream, explicit production-default model id)
# Model ids are the PRODUCTION defaults from Orchestrator/config.py (_DEFAULT_MODEL
# in admin_routes pre-selects these in the Portal picker). Passed explicitly so the
# probe never depends on the route's hardcoded fallbacks (which differ — see audit).
PROVIDERS = [
    ("gemini", "gemini-3.1-pro-preview"),      # GEMINI_MODEL_DEFAULT (config.py:729)
    ("anthropic", "claude-opus-4-8"),          # ANTHROPIC_MODEL_DEFAULT (config.py:691)
    ("openai", "gpt-5.1"),                     # OPENAI_MODEL_DEFAULT (config.py:690)
    ("xai", "grok-4.3"),                       # XAI_MODEL_DEFAULT (config.py:730)
]

SIZES = [75_000, 210_000, 238_000]

LOREM_SENTENCES = [
    "The flight recorder preserves every conversation as an immutable snapshot in the volume ledger.",
    "Synthetic filler text exercises the provider transport without carrying any real operator data.",
    "Context window verification requires payloads that exceed the historical delivery caps by design.",
    "Streaming latency is measured from request dispatch to the first token emitted by the provider.",
    "Retrieval count knobs govern how many snapshots reach the model once character caps are removed.",
    "Adaptive thinking models may pause for tens of seconds before the first visible output arrives.",
    "Transport hardening means heartbeats and generous read timeouts on every server-sent event leg.",
]


def build_payload(target_chars: int) -> str:
    """Deterministic lorem-ish text with periodic offset markers, exactly target_chars."""
    head = (
        f"[{MARKER}] SYNTHETIC TRANSPORT TEST DOCUMENT ({target_chars} chars). "
        "This content is meaningless filler for a context-window transport measurement. "
        "Do not store it, summarize it, or act on it.\n\n"
    )
    parts = [head]
    total = len(head)
    i = 0
    while total < target_chars:
        if i % 10 == 0:
            block = f"[{MARKER} marker {i:05d} offset {total:07d}]\n"
        else:
            block = LOREM_SENTENCES[i % len(LOREM_SENTENCES)] + " "
        parts.append(block)
        total += len(block)
        i += 1
    text = "".join(parts)[:target_chars]
    return text


USER_MSG = (
    "This is a synthetic transport test. Ignore the synthetic document in the system "
    "context entirely. Do not call any tools. Reply with exactly: OK"
)


def ledger_guard() -> None:
    """Hard-abort if the probe marker/operator ever lands in the immutable ledger."""
    data = VOLUME.read_bytes()
    if MARKER.encode() in data or OPERATOR.encode() in data:
        print(f"FATAL: probe marker found in {VOLUME} — LEDGER WRITE DETECTED. Aborting.")
        sys.exit(2)


def snapshot_count() -> int:
    return VOLUME.read_bytes().count(b"START SNAPSHOT")


def run_probe(base: str, provider: str, model: str, chars: int) -> dict:
    payload_text = build_payload(chars)
    body = {
        "messages": [
            {"role": "system", "content": payload_text},
            {"role": "user", "content": USER_MSG},
        ],
        "provider": provider,
        "model": model,
        "operator": OPERATOR,
    }
    rec: dict = {
        "provider": provider,
        "model": model,
        "chars": chars,
        "est_tokens": estimate_tokens(payload_text, model),
        "request_body_chars": len(json.dumps(body)),
        "ttfb_s": None,          # first provider token (thinking or content)
        "t_stream_start_s": None,  # server ACK (pre-provider) — transport baseline
        "total_s": None,
        "completed": False,
        "output_head": "",
        "content_chars": 0,
        "thinking_chars": 0,
        "tool_events": 0,
        "usage": None,
        "event_counts": {},
        "error": None,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    t0 = time.monotonic()
    first_token_at = None
    content_parts: list[str] = []
    ev_type = None
    saw_stream_end = False

    try:
        timeout = httpx.Timeout(HARD_TIMEOUT_S, connect=30.0)
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{base}/chat/stream", json=body) as resp:
                if resp.status_code != 200:
                    rec["error"] = f"HTTP {resp.status_code}: {resp.read()[:500].decode(errors='replace')}"
                    rec["total_s"] = round(time.monotonic() - t0, 3)
                    return rec
                for line in resp.iter_lines():
                    now = time.monotonic()
                    if now - t0 > HARD_TIMEOUT_S:
                        rec["error"] = f"hard timeout {HARD_TIMEOUT_S}s exceeded mid-stream"
                        break
                    if line.startswith("event: "):
                        ev_type = line[7:].strip()
                        rec["event_counts"][ev_type] = rec["event_counts"].get(ev_type, 0) + 1
                        if ev_type == "stream_start" and rec["t_stream_start_s"] is None:
                            rec["t_stream_start_s"] = round(now - t0, 3)
                        if ev_type in ("thinking_start", "thinking", "content_start", "content") \
                                and first_token_at is None:
                            first_token_at = now
                            rec["ttfb_s"] = round(now - t0, 3)
                        if ev_type in ("tool_start", "tool_result", "tool_use"):
                            rec["tool_events"] += 1
                        if ev_type == "stream_end":
                            saw_stream_end = True
                        continue
                    if line.startswith("data: ") and ev_type:
                        raw = line[6:]
                        try:
                            data = json.loads(raw)
                        except (json.JSONDecodeError, ValueError):
                            data = raw
                        if ev_type == "content" and isinstance(data, str):
                            content_parts.append(data)
                            rec["content_chars"] += len(data)
                        elif ev_type == "thinking" and isinstance(data, str):
                            rec["thinking_chars"] += len(data)
                        elif ev_type == "usage":
                            rec["usage"] = data
                        elif ev_type == "error":
                            rec["error"] = str(data)[:800]
                        if ev_type == "stream_end":
                            break
    except httpx.TimeoutException as e:
        rec["error"] = f"client timeout ({type(e).__name__}) after {round(time.monotonic()-t0, 1)}s"
    except Exception as e:  # noqa: BLE001 — record anything, never crash the run
        rec["error"] = f"{type(e).__name__}: {e}"

    rec["total_s"] = round(time.monotonic() - t0, 3)
    full = "".join(content_parts).strip()
    rec["output_head"] = full[:160]
    rec["completed"] = saw_stream_end and rec["error"] is None and bool(full)
    return rec


def load_results() -> list[dict]:
    if ARTIFACT.exists():
        try:
            return json.loads(ARTIFACT.read_text())["probes"]
        except Exception:
            return []
    return []


def save_results(probes: list[dict]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps({
        "audit": "M3/WI-10 provider context-window probes (measurement only)",
        "route": "POST /chat/stream (persistence-free; payload as system message)",
        "operator": OPERATOR,
        "marker": MARKER,
        "hard_timeout_s": HARD_TIMEOUT_S,
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probes": probes,
    }, indent=2) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="single provider to probe (gemini|anthropic|openai|xai)")
    ap.add_argument("--sizes", default=",".join(str(s) for s in SIZES))
    ap.add_argument("--base", default="http://localhost:9091")
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]
    providers = [(p, m) for p, m in PROVIDERS if not args.only or p == args.only]

    ledger_guard()
    snaps_before = snapshot_count()
    print(f"[probe] baseline: {snaps_before} snapshots in ledger, marker absent. GO.")

    probes = load_results()

    def upsert(rec: dict) -> None:
        nonlocal probes
        probes = [p for p in probes if not (p["provider"] == rec["provider"] and p["chars"] == rec["chars"])]
        probes.append(rec)
        save_results(probes)

    first = True
    for provider, model in providers:
        for chars in sizes:
            if not first:
                time.sleep(INTER_PROBE_SLEEP_S)
            first = False
            print(f"[probe] {provider}/{model} @ {chars} chars ...", flush=True)
            rec = run_probe(args.base, provider, model, chars)
            upsert(rec)
            status = "OK" if rec["completed"] else "FAIL"
            print(f"[probe]   -> {status} ttfb={rec['ttfb_s']}s total={rec['total_s']}s "
                  f"err={rec['error']!r} head={rec['output_head'][:60]!r}", flush=True)
            ledger_guard()

    snaps_after = snapshot_count()
    print(f"[probe] done. snapshots before={snaps_before} after={snaps_after} "
          f"(delta from OTHER activity is fine; marker guard passed).")
    print(f"[probe] results -> {ARTIFACT}")


if __name__ == "__main__":
    main()
