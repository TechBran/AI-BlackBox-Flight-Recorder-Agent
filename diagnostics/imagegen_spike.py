#!/usr/bin/env python3
"""Spike: probe each candidate IMAGE provider with existing keys.

Feasibility test for multi-provider image generation. Confirms each provider
generates a real image with our configured key and captures the OUTPUT SHAPE
(url vs base64) — which determines how the task worker writes the file to the
predicted /ui/uploads path. Read-only-ish (generates 1 small image per provider).
No secrets printed.
"""
import base64
import json
import os
import time
import urllib.request

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = "a single red apple on a plain wooden table, soft daylight"


def load_env():
    env = {}
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()


def hr(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def shape_of(data_item):
    """Report which output field a /v1/images/generations data[] item carries."""
    if not isinstance(data_item, dict):
        return "?"
    if data_item.get("b64_json"):
        return f"b64_json ({len(data_item['b64_json'])} chars)"
    if data_item.get("url"):
        return f"url ({data_item['url'][:60]}...)"
    return f"keys={list(data_item.keys())}"


def test_openai_compatible(label, base_url, key, model, extra=None):
    hr(f"{label} — {base_url}/v1/images/generations  model={model}")
    if not key:
        print("SKIP: no key"); return
    body = {"model": model, "prompt": PROMPT, "n": 1}
    if extra:
        body.update(extra)
    t = time.time()
    try:
        r = requests.post(
            f"{base_url}/v1/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body, timeout=180,
        )
        dt = int((time.time() - t) * 1000)
        if r.status_code >= 400:
            print(f"FAIL HTTP {r.status_code}: {r.text[:400]}")
            return
        d = r.json()
        items = d.get("data", [])
        print(f"OK  {dt}ms  images={len(items)}")
        for i, it in enumerate(items):
            print(f"   data[{i}]: {shape_of(it)}")
        print("   top-level keys:", list(d.keys()))
    except Exception as e:
        print(f"FAIL ({type(e).__name__}): {str(e)[:300]}")


def test_gemini_via_pipeline():
    hr("GEMINI Nano Banana — via existing POST /generate/image + poll (the real path)")
    try:
        body = json.dumps({"prompt": PROMPT, "operator": "system",
                           "numberOfImages": 1, "resolution": "1K"}).encode()
        req = urllib.request.Request("http://localhost:9091/generate/image", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode())
        task_id = d.get("task_id")
        print(f"queued task_id={task_id} status={d.get('status')}")
        if not task_id:
            print("no task_id"); return
        t = time.time()
        deadline = t + 180
        while time.time() < deadline:
            with urllib.request.urlopen(f"http://localhost:9091/tasks/{task_id}", timeout=10) as r:
                ts = json.loads(r.read().decode())
            st = ts.get("status")
            if st in ("completed", "done", "failed", "error"):
                dt = int((time.time() - t) * 1000)
                rd = ts.get("result_data") or ts.get("result") or {}
                # surface url(s)/keys without dumping base64
                keys = list(rd.keys()) if isinstance(rd, dict) else type(rd).__name__
                print(f"{st.upper()} after ~{dt}ms; result keys={keys}")
                if isinstance(rd, dict):
                    for k in ("url", "urls", "files", "images"):
                        if rd.get(k):
                            v = rd[k]
                            print(f"   {k}: {str(v)[:120]}")
                return
            time.sleep(3)
        print("TIMEOUT polling task")
    except Exception as e:
        print(f"FAIL ({type(e).__name__}): {str(e)[:300]}")


if __name__ == "__main__":
    # OpenAI gpt-image (b64_json by default; may need org verification)
    test_openai_compatible("OPENAI", "https://api.openai.com", ENV.get("OPENAI_API_KEY"),
                           "gpt-image-1", extra={"size": "1024x1024", "quality": "low"})
    # xAI Grok image (OpenAI-compatible per docs)
    test_openai_compatible("xAI GROK", "https://api.x.ai", ENV.get("XAI_API_KEY"),
                           "grok-imagine-image-quality")
    # Gemini Nano Banana via our real async pipeline
    test_gemini_via_pipeline()
    print("\nDONE.")
