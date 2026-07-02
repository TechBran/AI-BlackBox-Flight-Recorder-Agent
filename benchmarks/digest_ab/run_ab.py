#!/usr/bin/env python3
"""Controlled, FAIR A/B recall harness: DIGEST body vs PERSPECTIVE body.

THE QUESTION: does the new deterministic DIGEST snapshot body (HTML/URL-stripped
answer + a server-side deterministic "Keywords:" line) retrieve a target as well
as the old model-authored PERSPECTIVE body (an LLM reasoning summary + a
model-authored "Keywords:" line), for the SAME source answer, under realistic
user queries, with the LIVE embedding model + the production embedding API?

ISOLATED VARIABLE: body composition ONLY. Same 50 source answers, same queries,
same embedding model (gemini-embedding-2), same cosine ranking, same k.

FAIRNESS CONTROLS
  * Both bodies are derived from the SAME source answer per target.
  * Queries are generated from the ANSWER ONLY — never from a keyword line or a
    perspective — so neither arm is advantaged by query phrasing.
  * The DIGEST body imports the REAL production helpers (_strip_html,
    _extract_keywords) from Orchestrator.tasks, so we test shipped behavior.
  * Both bodies clamped by the production token-aware clamp (per-model
    max_input_tokens budget via tokenization.py) like production.
  * Embeddings via the production generate_embedding_sync(text, purpose):
    purpose="document" for bodies, purpose="query" for queries.
  * NON-minting LLM calls only (direct Anthropic API, claude-haiku-4-5) for
    query + perspective generation. We never POST /chat (which mints + pollutes
    the corpus).
  * Deterministic: fixed answer order, fixed LLM caching to disk (so a re-run
    reuses the exact same generated queries/perspectives unless --regen), no
    random/Date without a fixed seed.

OUTPUT: hit@1 / hit@3 / hit@5 / MRR for each arm, threshold-clear rate at 0.55
for the true target, clamp hit counts, N. NO verdict — method + numbers.

Run:  Orchestrator/venv/bin/python benchmarks/digest_ab/run_ab.py
      (add --regen to re-generate the LLM artifacts from scratch)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

# ---- repo + production imports -------------------------------------------------
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent

from benchmarks.digest_ab.answers import ANSWERS  # noqa: E402
from Orchestrator.tasks import _strip_html, _extract_keywords  # REAL shipped helpers  # noqa: E402
from Orchestrator.embeddings.search import (  # noqa: E402
    generate_embedding_sync,
    get_active_slug,
)
from Orchestrator.config import ANTHROPIC_API_KEY  # noqa: E402

# Production embed clamp — the provider layer token-aware clamps each text to
# 90% of the registry's per-model max_input_tokens before embedding
# (Orchestrator/embeddings/providers.py + Orchestrator/tokenization.py), so we
# only count whether a body would be clamped; we let the live provider do the
# actual clamping.
from Orchestrator import tokenization  # noqa: E402
from Orchestrator.embeddings.providers import EMBED_CLAMP_MARGIN  # noqa: E402
from Orchestrator.embeddings.registry import EMBEDDING_MODELS  # noqa: E402

ARTIFACTS = HERE / "artifacts.json"   # cached LLM-generated queries + perspectives
RESULTS = HERE / "results.json"
LLM_MODEL = "claude-haiku-4-5"        # cheap, non-minting, direct API


# ---- LLM (direct Anthropic, NON-minting) --------------------------------------
def _anthropic_client():
    import anthropic
    key = ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("No ANTHROPIC_API_KEY available for non-minting LLM calls.")
    return anthropic.Anthropic(api_key=key)


def _llm_text(client, system: str, user: str, max_tokens: int) -> str:
    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


QUERY_SYS = (
    "You write ONE realistic search query a user would type to recall a past "
    "work note from memory. Output ONLY the query text — no quotes, no preamble, "
    "no label. One line. Phrase it as a natural question or keyword search a "
    "developer would actually use weeks later when they half-remember the work."
)
QUERY_USER_TMPL = (
    "Here is the note's ANSWER text. Write one query a user would type to find "
    "THIS note later. Do NOT copy distinctive phrases verbatim; ask the way "
    "someone who vaguely remembers it would.\n\nANSWER:\n{answer}"
)

PERSP_SYS = (
    "You are reconstructing an OLD-STYLE snapshot 'perspective' body: a first-"
    "person reasoning summary of what was figured out, written as the assistant "
    "reflecting on its own work, followed by a model-authored keyword line. "
    "Write 3-6 sentences of reflective reasoning summary (NOT a copy of the "
    "answer — summarize the thinking and the takeaway), then a final line "
    "EXACTLY of the form 'Keywords: a, b, c, d, e, f, g' with 5-9 lowercase "
    "comma-separated keywords you choose. Output only the body."
)
PERSP_USER_TMPL = (
    "Reconstruct the old-style perspective body for a session whose user-facing "
    "ANSWER was:\n\n{answer}"
)


def generate_artifacts(regen: bool) -> dict:
    if ARTIFACTS.exists() and not regen:
        data = json.loads(ARTIFACTS.read_text())
        if len(data.get("items", [])) == len(ANSWERS):
            print(f"[artifacts] reusing cached {ARTIFACTS} ({len(data['items'])} items)")
            return data
    print("[artifacts] generating queries + perspectives via direct LLM (non-minting)...")
    client = _anthropic_client()
    items = []
    for i, ans in enumerate(ANSWERS):
        q = _llm_text(client, QUERY_SYS, QUERY_USER_TMPL.format(answer=ans), 120)
        p = _llm_text(client, PERSP_SYS, PERSP_USER_TMPL.format(answer=ans), 600)
        items.append({"idx": i, "answer": ans, "query": q, "perspective": p})
        print(f"  [{i+1:02d}/{len(ANSWERS)}] q={q[:70]!r}")
        time.sleep(0.05)
    data = {
        "model": LLM_MODEL,
        "n": len(items),
        "items": items,
    }
    ARTIFACTS.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[artifacts] wrote {ARTIFACTS}")
    return data


# ---- body composition (the ONLY variable) -------------------------------------
def digest_body(answer: str) -> str:
    """EXACT production composition (Orchestrator/tasks.py): stripped answer +
    deterministic server-side keyword line. (Reasoning is appended LAST in prod
    and is the first thing the 10K truncation eats; the source answers here have
    no reasoning, matching the answer+keywords portion that production embeds.)"""
    clean = _strip_html(answer)
    kw = _extract_keywords(clean)
    if kw:
        return clean + "\n\nKeywords: " + ", ".join(kw)
    return clean


def perspective_body(perspective: str) -> str:
    """OLD-style body: the LLM-reconstructed reasoning summary + its own
    model-authored 'Keywords:' line (already embedded in the perspective text)."""
    return perspective


def clamp_budget_tokens(slug: str) -> int:
    """Mirror of the production clamp budget for the active model."""
    return int(EMBEDDING_MODELS[slug]["max_input_tokens"] * EMBED_CLAMP_MARGIN)


def will_clamp(text: str, slug: str) -> bool:
    """Production clamps inside the provider layer (token-aware, head-keeping).
    We pass the FULL body to embed() and let the live provider clamp exactly as
    it does in production; this only reports whether clamping would occur."""
    return tokenization.estimate_tokens(text, slug) > clamp_budget_tokens(slug)


# ---- cosine -------------------------------------------------------------------
def cosine(a: list[float], b: list[float]) -> float:
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return s / (math.sqrt(na) * math.sqrt(nb))


# ---- embedding with simple retry ----------------------------------------------
def embed(text: str, purpose: str) -> list[float]:
    for attempt in range(4):
        v = generate_embedding_sync(text, purpose)
        if v:
            return v
        time.sleep(1.0 * (attempt + 1))
    raise SystemExit(f"embedding failed (purpose={purpose}) after retries")


# ---- main eval ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regen", action="store_true", help="regenerate LLM artifacts")
    ap.add_argument("--threshold", type=float, default=0.55)
    args = ap.parse_args()

    slug = get_active_slug()
    budget = clamp_budget_tokens(slug)
    print(f"[model] active embedding model = {slug}")
    print(f"[const] clamp budget = {budget} tokens "
          f"(max_input_tokens x {EMBED_CLAMP_MARGIN}), threshold = {args.threshold}")

    data = generate_artifacts(args.regen)
    items = data["items"]
    N = len(items)

    # Compose both corpora (the isolated variable).
    digest_bodies, persp_bodies = [], []
    trunc_digest = trunc_persp = 0
    for it in items:
        d = digest_body(it["answer"])
        p = perspective_body(it["perspective"])
        digest_bodies.append(d)
        persp_bodies.append(p)
        trunc_digest += int(will_clamp(d, slug))
        trunc_persp += int(will_clamp(p, slug))

    # Embed both corpora as DOCUMENTS, queries as QUERIES (production purposes).
    print(f"[embed] {N} digest docs...")
    dig_vecs = [embed(t, "document") for t in digest_bodies]
    print(f"[embed] {N} perspective docs...")
    per_vecs = [embed(t, "document") for t in persp_bodies]
    print(f"[embed] {N} queries...")
    q_vecs = [embed(it["query"], "query") for it in items]

    # Score: for each query, rank all N targets within EACH corpus.
    def eval_arm(doc_vecs):
        ranks, true_sims, clears = [], [], 0
        for qi, qv in enumerate(q_vecs):
            sims = [(j, cosine(qv, doc_vecs[j])) for j in range(N)]
            sims.sort(key=lambda t: t[1], reverse=True)
            order = [j for j, _ in sims]
            rank = order.index(qi) + 1  # 1-based rank of the true target
            ranks.append(rank)
            tsim = next(s for j, s in sims if j == qi)
            true_sims.append(tsim)
            if tsim >= args.threshold:
                clears += 1
        hit1 = sum(r == 1 for r in ranks) / N
        hit3 = sum(r <= 3 for r in ranks) / N
        hit5 = sum(r <= 5 for r in ranks) / N
        mrr = sum(1.0 / r for r in ranks) / N
        return {
            "hit@1": round(hit1, 4),
            "hit@3": round(hit3, 4),
            "hit@5": round(hit5, 4),
            "mrr": round(mrr, 4),
            "threshold_clear_rate": round(clears / N, 4),
            "threshold_clears": clears,
            "mean_true_sim": round(sum(true_sims) / N, 4),
            "ranks": ranks,
            "true_sims": [round(s, 4) for s in true_sims],
        }

    digest = eval_arm(dig_vecs)
    persp = eval_arm(per_vecs)

    out = {
        "embedding_model": slug,
        "llm_model": data["model"],
        "N": N,
        "clamp_budget_tokens": budget,
        "threshold": args.threshold,
        "truncation_hits": {"digest": trunc_digest, "perspective": trunc_persp},
        "arms": {"digest": digest, "perspective": persp},
    }
    RESULTS.write_text(json.dumps(out, indent=2))
    print(f"[done] wrote {RESULTS}\n")

    def row(name, a):
        print(f"  {name:12s} hit@1={a['hit@1']:.3f} hit@3={a['hit@3']:.3f} "
              f"hit@5={a['hit@5']:.3f} MRR={a['mrr']:.3f} "
              f"thr_clear={a['threshold_clear_rate']:.3f} ({a['threshold_clears']}/{N}) "
              f"mean_true_sim={a['mean_true_sim']:.3f}")

    print(f"=== A/B RECALL  (N={N}, model={slug}, threshold={args.threshold}) ===")
    row("PERSPECTIVE", persp)
    row("DIGEST", digest)
    print(f"  clamp@{budget}tok: digest={trunc_digest}/{N} "
          f"perspective={trunc_persp}/{N}")


if __name__ == "__main__":
    main()
