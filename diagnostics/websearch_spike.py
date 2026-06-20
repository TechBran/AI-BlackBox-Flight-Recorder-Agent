#!/usr/bin/env python3
"""Spike: probe each candidate web-search provider with existing keys.

Read-only feasibility test for the production-grade multi-provider web search
plan. Each provider is isolated in try/except so one failure doesn't block the
others. Prints: ok/fail, latency, a content snippet, citation count, and which
API variant worked. No secrets are printed.
"""
import json
import os
import time
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env = {}
    p = os.path.join(ROOT, ".env")
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
QUERY = "What are the top AI model releases announced in the last two weeks?"
X_QUERY = "What is the latest news being discussed about Anthropic on X today?"


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def snippet(text, n=320):
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def test_perplexity():
    hr("1. PERPLEXITY (sonar) — current default")
    key = ENV.get("PERPLEXITY_API_KEY", "")
    if not key:
        print("SKIP: no key"); return
    t = time.time()
    try:
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "sonar", "messages": [{"role": "user", "content": QUERY}],
                  "search_recency_filter": "week"},
            timeout=40,
        )
        dt = int((time.time() - t) * 1000)
        r.raise_for_status()
        d = r.json()
        ans = d["choices"][0]["message"]["content"]
        cites = d.get("citations", []) or d.get("search_results", [])
        print(f"OK  {dt}ms  citations={len(cites)}")
        print("ANSWER:", snippet(ans))
        if cites:
            print("CITES[0]:", cites[0] if isinstance(cites[0], str) else json.dumps(cites[0])[:160])
    except Exception as e:
        print(f"FAIL ({type(e).__name__}): {str(e)[:300]}")


def test_openai():
    hr("2. OPENAI (Responses API web_search)")
    key = ENV.get("OPENAI_API_KEY", "")
    if not key:
        print("SKIP: no key"); return
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for tool_type in ("web_search", "web_search_preview"):
        for model in ("gpt-4.1", "gpt-4o"):
            t = time.time()
            try:
                r = requests.post(
                    "https://api.openai.com/v1/responses",
                    headers=headers,
                    json={"model": model, "tools": [{"type": tool_type}], "input": QUERY},
                    timeout=60,
                )
                dt = int((time.time() - t) * 1000)
                if r.status_code >= 400:
                    print(f"  try tool={tool_type} model={model}: HTTP {r.status_code} {snippet(r.text,160)}")
                    continue
                d = r.json()
                # Aggregate output_text + count url_citation annotations
                text, n_cites = "", 0
                for item in d.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") in ("output_text", "text"):
                                text += c.get("text", "")
                                n_cites += sum(1 for a in (c.get("annotations") or [])
                                               if a.get("type") == "url_citation")
                print(f"OK  tool={tool_type} model={model}  {dt}ms  url_citations={n_cites}")
                print("ANSWER:", snippet(text))
                return
            except Exception as e:
                print(f"  try tool={tool_type} model={model}: FAIL {type(e).__name__} {str(e)[:160]}")
    print("FAIL: no working (tool_type, model) combination")


def test_gemini():
    hr("3. GOOGLE GEMINI (grounding via google_search)")
    key = ENV.get("GEMINI_API_KEY") or ENV.get("GOOGLE_API_KEY", "")
    if not key:
        print("SKIP: no key"); return
    for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
        t = time.time()
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            r = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": QUERY}]}],
                      "tools": [{"google_search": {}}]},
                timeout=60,
            )
            dt = int((time.time() - t) * 1000)
            if r.status_code >= 400:
                print(f"  try model={model}: HTTP {r.status_code} {snippet(r.text,200)}")
                continue
            d = r.json()
            cand = (d.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            gm = cand.get("groundingMetadata") or {}
            chunks = gm.get("groundingChunks") or []
            print(f"OK  model={model}  {dt}ms  groundingChunks={len(chunks)}")
            print("ANSWER:", snippet(text))
            if chunks:
                w = chunks[0].get("web") or {}
                print("CHUNK[0]:", json.dumps({"title": w.get("title"), "uri": (w.get("uri") or "")[:80]}))
            return
        except Exception as e:
            print(f"  try model={model}: FAIL {type(e).__name__} {str(e)[:160]}")
    print("FAIL: no working model")


def test_xai(source_type, query, label):
    hr(f"4. xAI GROK Live Search — source={source_type} ({label})")
    key = ENV.get("XAI_API_KEY", "")
    if not key:
        print("SKIP: no key"); return
    model = ENV.get("XAI_MODEL", "grok-4.3")
    t = time.time()
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": query}],
                  "search_parameters": {"mode": "on", "return_citations": True,
                                        "sources": [{"type": source_type}]}},
            timeout=60,
        )
        dt = int((time.time() - t) * 1000)
        if r.status_code >= 400:
            print(f"FAIL HTTP {r.status_code}: {snippet(r.text,300)}")
            return
        d = r.json()
        ans = d["choices"][0]["message"]["content"]
        cites = d.get("citations", []) or []
        print(f"OK  model={model}  {dt}ms  citations={len(cites)}")
        print("ANSWER:", snippet(ans))
        if cites:
            print("CITES[0]:", cites[0] if isinstance(cites[0], str) else json.dumps(cites[0])[:160])
    except Exception as e:
        print(f"FAIL ({type(e).__name__}): {str(e)[:300]}")


if __name__ == "__main__":
    test_perplexity()
    test_openai()
    test_gemini()
    test_xai("web", QUERY, "general web")
    test_xai("x", X_QUERY, "X / Twitter live")
    print("\nDONE.")
