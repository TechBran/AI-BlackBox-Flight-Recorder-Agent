"""Cross-encoder reranker — provider seam for the canonical retriever (WI-4/M11).

Design (audit A9, docs/plans/2026-07-01-retrieval-upgrade-spec-audit.md §3):
the reranker is an OPTIONAL late stage inside Orchestrator/retrieval.retrieve(),
gated on [retrieval] rerank_enabled (default false) AND available() below.
This module owns HOW passages get scored; retrieval.py owns WHERE the scores
slot into the ranking (rank-space remap + recency re-application + MMR).

Contract: score(query, passages) -> list[float] | None. One float per passage,
higher = more relevant, all on ONE scale for the whole call. Returns None on
ANY failure (provider unconfigured, HTTP error, timeout, malformed response,
wrong-length result) and NEVER raises: the retriever falls through to its
un-reranked ranking, so a dead reranker can only cost latency, never recall.

Providers ([rerank] provider, code fallback "null" — the [rerank] section may
be entirely absent, per the A13 fresh-box rule):
  null (default) — no reranker; score() always returns None. The pre-GPU /
                   fresh-box state: this module is fully inert.
  vllm           — POST {base_url}/score, vLLM's cross-encoder scoring API:
                     {"model": <model_id>, "text_1": <query>,
                      "text_2": [<passage>, ...]}
                   -> {"data": [{"index": i, "score": s}, ...]} (index maps a
                   score back to its text_2 position; missing index = in-order).

Latency preflight (audit A9 gating leg 3): retrieve()-time probing is too hot,
so preflight() runs ONCE per process on first use — it scores a 1-passage dummy
and requires wall latency under [rerank] preflight_ceiling_ms (fallback 500).
A failed probe (error OR over-ceiling) disables rerank for the PROCESS LIFETIME
(cached; logged once). GET /rerank/status surfaces the result.

── Post-GPU activation checklist (RTX 2000 Ada 16GB — for the operator) ──────
1. Serve the reranker. Qwen3-Reranker publishes CausalLM weights; vLLM needs
   the sequence-classification conversion overrides or /score will not exist:

     vllm serve Qwen/Qwen3-Reranker-0.6B --port 8091 \
       --gpu-memory-utilization 0.20 \
       --hf-overrides '{"architectures": ["Qwen3ForSequenceClassification"],
                        "classifier_from_token": ["no", "yes"],
                        "is_original_qwen3_reranker": true}'

   gpu_memory_utilization MUST stay in the 0.15–0.25 band: the Ollama embedder
   (qwen3-embedding on-GPU, WI-9 placement) shares the card, and an
   unconstrained vLLM pre-allocates ~90% of VRAM and evicts it.
2. config.ini:
     [rerank]
     provider = vllm
     base_url = http://localhost:8091
     model = qwen3-reranker-0.6b
   and flip [retrieval] rerank_enabled = true.
3. Restart; GET /rerank/status must show preflight state "ok" with latency
   comfortably under the 500 ms ceiling.
4. Validate scores once against the in-process transformers reference (the
   audit's vLLM-fidelity check), then run the WI-6 Phase B eval
   ({quant-8B, FP16-8B} x {rerank on/off} — audit A10) BEFORE shipping
   rerank_enabled as a default.

── RERANK_MODELS literal discipline (Task-16-style) ──────────────────────────
Mirrors Orchestrator/embeddings/registry.py: reranker model literals (slugs +
provider model ids) live ONLY in the RERANK_MODELS table below. These are NOT
embedding models — they must never be added to EMBEDDING_MODELS (different
task head, different serving stack, no vector store keyed on them).
[rerank] model accepts a RERANK_MODELS slug (resolved to its model_id for the
wire call) or, as an escape hatch, a verbatim served-model name.
"""
from __future__ import annotations

import threading
import time

import requests

from Orchestrator.config import CFG

# Reranker model registry — the ONLY home for reranker model literals.
# Same conventions as EMBEDDING_MODELS (slug-keyed, kebab-case, provider +
# model_id + ops metadata); vram_gb feeds the WI-9/M10 placement arithmetic
# when reranker placement activates post-GPU.
RERANK_MODELS = {
    # query_instruction (REQUIRED for Qwen3-Reranker correctness): the query is
    # prepended with this instruct prefix before scoring, exactly as the embedding
    # registry does. Measured live on the RTX 2000 Ada (2026-07-03): WITHOUT the
    # prefix the reranker scores "is this a well-formed passage" not "is this
    # relevant to THIS query" and INVERTS — a relevant passage scored 0.726 vs an
    # off-topic cake recipe at 0.790; WITH the prefix it ranks correctly with a
    # wide margin (0.822 vs 0.530). Shipping the bare query would degrade recall.
    "qwen3-reranker-0.6b": {
        "provider": "vllm",
        "model_id": "Qwen/Qwen3-Reranker-0.6B",
        "label": "Qwen3 Reranker 0.6B (local GPU)",
        "vram_gb": 1.2,  # FP16 resident (M10 Task 10.2 budget arithmetic)
        "max_input_tokens": 32768,
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "Default post-GPU pick; pairs with the qwen3 embedding stores",
    },
    "qwen3-reranker-4b": {
        "provider": "vllm",
        "model_id": "Qwen/Qwen3-Reranker-4B",
        "label": "Qwen3 Reranker 4B (local GPU)",
        "vram_gb": 2.5,  # Q4 resident; FP16 ≈ 8GB wants the 16GB card mostly free
        "max_input_tokens": 32768,
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "Bigger cross-encoder — only if Phase B shows 0.6B leaves recall on the table",
    },
}

_DEFAULT_MODEL_SLUG = "qwen3-reranker-0.6b"

# One-time-per-process preflight cache (audit A9). Guarded because retrieve()
# runs from FastAPI's threadpool — two first-uses must not double-probe.
_preflight_lock = threading.Lock()
_preflight_result: dict | None = None


def get_settings() -> dict:
    """Resolved [rerank] config with code fallbacks (fresh-box safe: the
    section may be absent — provider then resolves to "null" = inert)."""
    provider = CFG.get("rerank", "provider", fallback="null").strip().lower()
    base_url = CFG.get("rerank", "base_url", fallback="").strip().rstrip("/")
    model = CFG.get("rerank", "model", fallback=_DEFAULT_MODEL_SLUG).strip()
    entry = RERANK_MODELS.get(model)
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        # Registry slug -> wire model id; unknown value passes verbatim
        # (escape hatch for a custom served-model name).
        "model_id": entry["model_id"] if entry else model,
        # Instruct prefix prepended to the query (Qwen3-Reranker requires it —
        # see RERANK_MODELS comment). Config override wins; else the model's
        # registry value; else empty (a reranker that needs no instruction).
        "query_instruction": CFG.get(
            "rerank", "query_instruction",
            fallback=(entry.get("query_instruction", "") if entry else ""),
        ),
        "timeout_s": CFG.getfloat("rerank", "timeout_s", fallback=15.0),
        "preflight_ceiling_ms": CFG.getfloat(
            "rerank", "preflight_ceiling_ms", fallback=500.0
        ),
    }


def _configured(settings: dict | None = None) -> bool:
    s = settings or get_settings()
    return s["provider"] == "vllm" and bool(s["base_url"])


def score(query: str, passages: list[str]) -> list[float] | None:
    """Cross-encoder scores for (query, passage) pairs — None on ANY failure.

    Never raises. The returned list is positionally aligned with `passages`
    (vLLM's /score `index` field is honored when present). A response whose
    row count doesn't match the request is treated as failure — a partial
    score set cannot rank the pool on one scale.
    """
    try:
        s = get_settings()
        if not _configured(s) or not passages:
            return None
        resp = requests.post(
            s["base_url"] + "/score",
            json={"model": s["model_id"],
                  "text_1": s["query_instruction"] + query,
                  "text_2": list(passages)},
            timeout=s["timeout_s"],
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data")
        if not isinstance(data, list) or len(data) != len(passages):
            return None
        out: list[float | None] = [None] * len(passages)
        for i, item in enumerate(data):
            idx = int(item.get("index", i))
            out[idx] = float(item["score"])
        if any(v is None for v in out):
            return None
        return out  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - never-raise contract (audit A9)
        return None


def preflight() -> dict:
    """One-time-per-process latency probe; result cached for process lifetime.

    Scores a 1-passage dummy against the configured provider and requires
    wall latency under [rerank] preflight_ceiling_ms. States:
      skipped — no provider configured (NOT cached: config can change);
      ok      — probe scored under the ceiling (cached);
      failed  — provider error or over-ceiling (cached: rerank disabled for
                the process lifetime; a restart re-probes).
    """
    global _preflight_result
    if _preflight_result is not None:
        return _preflight_result
    with _preflight_lock:
        if _preflight_result is not None:
            return _preflight_result
        s = get_settings()
        ceiling = s["preflight_ceiling_ms"]
        if not _configured(s):
            # Not a probe failure — do not burn the process-lifetime cache.
            return {"state": "skipped", "latency_ms": None,
                    "ceiling_ms": ceiling,
                    "reason": "no reranker provider configured"}
        t0 = time.monotonic()
        got = score("preflight probe", ["preflight probe passage"])
        ms = (time.monotonic() - t0) * 1000.0
        if got is None:
            result = {"state": "failed", "latency_ms": round(ms, 1),
                      "ceiling_ms": ceiling,
                      "reason": "provider scoring failed"}
        elif ms > ceiling:
            result = {"state": "failed", "latency_ms": round(ms, 1),
                      "ceiling_ms": ceiling,
                      "reason": f"probe latency {ms:.0f}ms over the "
                                f"{ceiling:.0f}ms ceiling"}
        else:
            result = {"state": "ok", "latency_ms": round(ms, 1),
                      "ceiling_ms": ceiling, "reason": None}
        _preflight_result = result
        print(f"[RERANK] preflight {result['state']}"
              f" ({result['reason'] or f'{ms:.0f}ms'});"
              f" provider={s['provider']} model={s['model']}"
              + (" — rerank disabled for process lifetime"
                 if result["state"] == "failed" else ""))
        return result


def reset_preflight() -> None:
    """Clear the one-time probe cache (tests + explicit ops re-check)."""
    global _preflight_result
    with _preflight_lock:
        _preflight_result = None


def available() -> bool:
    """Provider configured AND the one-time latency preflight passed.

    This is the retrieve()-time gate (with [retrieval] rerank_enabled checked
    by the caller first, so the null-provider default costs one config read
    and never probes anything).
    """
    if not _configured():
        return False
    return preflight().get("state") == "ok"


def status() -> dict:
    """/rerank/status payload (ADDITIVE ops contract, /embeddings/status
    style). Triggers the one-time preflight only when a provider is
    configured — with the null provider this is pure config reads, safe to
    poll."""
    s = get_settings()
    configured = _configured(s)
    if configured:
        pf = preflight()
    else:
        pf = _preflight_result or {
            "state": "skipped", "latency_ms": None,
            "ceiling_ms": s["preflight_ceiling_ms"],
            "reason": "no reranker provider configured",
        }
    return {
        "enabled": CFG.getboolean("retrieval", "rerank_enabled", fallback=False),
        "provider": s["provider"],
        "model": s["model"],
        "model_id": s["model_id"],
        "base_url": s["base_url"] or None,
        "configured": configured,
        "preflight": pf,
        "available": configured and pf.get("state") == "ok",
        "candidate_n": CFG.getint("retrieval", "rerank_candidate_n", fallback=40),
        "passage_chars": CFG.getint("rerank", "passage_chars", fallback=4096),
        "models": sorted(RERANK_MODELS.keys()),
    }
