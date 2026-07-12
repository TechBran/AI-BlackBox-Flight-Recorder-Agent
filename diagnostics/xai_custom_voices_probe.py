#!/usr/bin/env python3
"""Live probe: xAI Custom Voices REST wire shapes (voice-upgrade pass, workstream 5).

READ-ONLY BY DESIGN. This probe confirms — against the REAL API with this box's
XAI_API_KEY — the shapes the provider module (Orchestrator/xai_voices.py, task
P6.21) assumes. It performs ONLY a GET (list); it never creates, mutates, or
deletes a voice, and never spends money. Cloning a voice is BILLABLE and is the
OPERATOR's explicit action (via the /xai/voices route or the xai_clone_voice
tool) — this probe deliberately does NOT clone.

Wire shapes P6.21 depends on (recon: scratchpad recon/xaiResearch.json — the GET
envelope is confirmed live below; the clone/delete shapes are DOCUMENTED request
shapes, not exercised here per the read-only guardrail):

  1. GET    /v1/custom-voices               -> auth ok; TOP-LEVEL shape is one of
     bare list | {"voices": [...]} | {"data": [...]}, and each voice carries an
     id under voice_id (or id) plus name. *** This is the only call this probe
     makes live. ***
  2. POST   /v1/custom-voices   (CLONE)     -> multipart body:
        file        = ONE reference clip, <=120s (xAI enforces the duration)
        name        = display name for the cloned voice
        description = optional short description
     Returns the new voice object (voice_id + name). *** Creating a voice is
     BILLABLE — NOT performed by this probe. A real clone is the operator's
     explicit action. ***
  3. DELETE /v1/custom-voices/<voice_id>     -> removes a cloned voice. *** Not
     performed by this probe. ***

Because P6.21's client tolerates every listed envelope/id-key variant already
(bare list | voices | data; voice_id | id; file+name fields), the GET below is
sufficient to gate P6.21: run it, confirm section 1 returns 200 with one of the
documented envelopes, and proceed. If the GET envelope differs from all three
listed shapes, note the actual key and adjust P6.21 Step 3 accordingly.

Run (from repo root, GET/list-only — the ONLY supported invocation):
  XAI_API_KEY="<key from service env>" \
    Orchestrator/venv/bin/python diagnostics/xai_custom_voices_probe.py

XAI_API_KEY comes from the same source Orchestrator/config.py reads for the
service env.
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

print("== 1. GET /v1/custom-voices (read-only) ==")
r = httpx.get(BASE, headers=H, timeout=30)
print("status:", r.status_code)
try:
    body = r.json()
    print("top-level type:", type(body).__name__)
    if isinstance(body, list):
        print("envelope: bare list")
    elif isinstance(body, dict):
        print("envelope keys:", sorted(body.keys()))
    print(json.dumps(body, indent=2)[:2000])
except Exception:
    print("non-JSON body:", r.text[:500])

print(
    "\n[read-only] Clone (POST) and delete (DELETE) are NOT exercised by this probe.\n"
    "  Documented request shapes P6.21 uses:\n"
    "    POST /v1/custom-voices   multipart {file: <=120s clip, name, description?}\n"
    "    DELETE /v1/custom-voices/<voice_id>\n"
    "  A real clone is BILLABLE and is the operator's explicit action — never done here."
)
