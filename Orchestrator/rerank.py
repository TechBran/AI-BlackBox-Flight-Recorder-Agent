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
  null (default) — no reranker; score() always returns None. The CPU /
                   fresh-box state: this module is fully inert.
  vllm           — POST {base_url}/score, vLLM's cross-encoder scoring API:
                     {"model": <model_id>, "text_1": <query>,
                      "text_2": [<passage>, ...]}
                   -> {"data": [{"index": i, "score": s}, ...]} (index maps a
                   score back to its text_2 position; missing index = in-order).
base_url code fallback is http://localhost:8091 — the port the installer's
vllm-reranker.service binds (M13) — so a fresh GPU box needs ZERO url/port
config once the service is up; `provider = vllm` stays the single deliberate
switch. An EXPLICITLY EMPTY base_url in config still disables (escape hatch).

Capability gating (audit A9 gating leg 3) — TWO regimes, because a probe is not
free everywhere:
  * On-box batch providers (_EVIDENCE_PROVIDERS: localstack/vllm) are gated on
    EVIDENCE FROM REAL TRAFFIC, not on a synthetic probe. A probe honest enough
    to catch their real failure mode has to be production-shaped
    (rerank_candidate_n x passage_chars) — MEASURED on MS02 that is a real ~16s
    rerank (21.3s cold) on the first search after every restart, held under
    _preflight_lock while concurrent searches queue behind it, to learn exactly
    what the next real search reports for free. So score() records every real
    outcome and preflight() reports three honest states:
      ok      — a real rerank succeeded within _EVIDENCE_OK_TTL_S;
      failed  — a real rerank failed (the reason + batch shape
                _note_fallthrough recorded); the gate closes for
                _PREFLIGHT_FAIL_TTL_S, then decays to unknown and retries;
      unknown — no recent evidence (cold start / aged out). TRUTHFUL, never
                reported as ok, and the gate stays OPEN: the first real search
                IS the probe. If it fails the user gets un-reranked results
                once, loudly logged, and the state flips to failed.
  * cpu / cloud / llm keep the cheap once-per-process probe of
    `preflight_passage_n` dummy passages under [rerank] preflight_ceiling_ms
    (fallback 500): their failure mode is auth/quota/deps, not our batch size,
    and a 1-passage probe there is not a 16s tax. A failed probe disables
    rerank for the process lifetime (cloud: for _PREFLIGHT_FAIL_TTL_S).
LIVENESS is a SEPARATE concern from capability and stays cheap: reachable() /
scoring_reachable() / service_reachable() answer "can I reach the front door",
never "does reranking work". GET /rerank/status surfaces both, plus the running
un-reranked `fallthrough` record and the base_url scoring ACTUALLY uses.

── Post-GPU activation checklist (RTX 2000 Ada 16GB — for the operator) ──────
1. Serve the reranker. On GPU boxes the installer provisions this (M13:
   installer/templates/blackbox-install-reranker.sh writes ~/start-reranker.sh
   + the vllm-reranker.service unit on port 8091 and starts it). The manual
   incantation, kept for reference — Qwen3-Reranker publishes CausalLM
   weights; vLLM needs the sequence-classification conversion overrides or
   /score will not exist:

     vllm serve Qwen/Qwen3-Reranker-0.6B --port 8091 \
       --gpu-memory-utilization 0.20 --max-model-len 8192 \
       --hf-overrides '{"architectures": ["Qwen3ForSequenceClassification"],
                        "classifier_from_token": ["no", "yes"],
                        "is_original_qwen3_reranker": true}'

   gpu_memory_utilization MUST stay in the 0.15–0.25 band: the Ollama embedder
   (qwen3-embedding on-GPU, WI-9 placement) shares the card, and an
   unconstrained vLLM pre-allocates ~90% of VRAM and evicts it.
2. config.ini (the wizard's Memory & Search step shows this instruction —
   nothing ever writes config.ini for you):
     [rerank]
     provider = vllm
   (base_url and model already default in code to http://localhost:8091 and
   qwen3-reranker-0.6b — set them only to override)
   and flip [retrieval] rerank_enabled = true.
3. Restart; GET /rerank/status shows state "unknown" until the first real
   search (vllm is evidence-gated — nothing is probed synthetically), then "ok"
   with the real scoring latency once a search has actually reranked.
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

import asyncio
import json
import os
import re
import threading
import time

import requests

from Orchestrator import hardware
from Orchestrator.config import (
    ANTHROPIC_URL,
    CFG,
    GEMINI_BASE_URL,
    OPENAI_URL,
    XAI_URL,
)

# Where the installer's vllm-reranker.service listens (M13). Code fallback so
# a fresh GPU box works with zero url/port config edits once the service is
# up; an explicit [rerank] base_url overrides, an explicitly EMPTY one disables.
DEFAULT_BASE_URL = "http://localhost:8091"

# ── on-box (llama-swap) transport deadlines ───────────────────────────────────
# llama-swap HOLDS a /v1/rerank request while it evicts a group and cold-loads
# the reranker member: a slow READ is the stack working, not a hung connection.
# Same value and same reasoning as Orchestrator/embeddings/providers.py's
# LOCALSTACK_READ_TIMEOUT_S (the other on-box client of the same front door —
# kept as its own named constant here so rerank.py's minimal import surface
# stands, and so the two can be compared rather than silently drifting).
# MEASURED on MS02 2026-08-11: a production-sized rerank (rerank_candidate_n 40
# x passage_chars 4096, ~25K tokens at ~0.67ms/token) runs ~16s warm and ~19s
# including an intra-group swap (21.3s cold worst case) — the old flat 15s
# [rerank] timeout_s aborted work llama-swap was still correctly doing.
# BOUNDED at 90s (~4x the cold worst case), not the embeddings client's 600s:
# score() runs inline on the caller's thread — on /chat/stream that is the
# event loop — so a member that accepts the connection and never answers holds
# every caller for the whole cap, and a skipped rerank only costs ranking
# quality (score() falls through to the un-reranked order, it never fails the
# search). Its own knob, [rerank] localstack_timeout_s; [rerank] timeout_s is
# the cloud deadline and no longer leaks into this one. Connect stays short so
# a genuinely dead :9098 front door fails fast instead of hanging for the cap.
LOCALSTACK_READ_TIMEOUT_S = 90.0
LOCALSTACK_CONNECT_TIMEOUT_S = 5.0

# Scoring endpoints, named once so status()' scoring_base_url() can report the
# URL the scorer really posts to instead of a second hand-copied literal.
VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"
COHERE_RERANK_URL = "https://api.cohere.ai/v2/rerank"
VERTEX_RANK_URL = ("https://discoveryengine.googleapis.com/v1/projects/"
                   "{project}/locations/global/rankingConfigs/"
                   "default_ranking_config:rank")

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
    # M2 tiering schema (Task 2.1): auth_kind/key_env/cost_note/privacy/tiers +
    # per-provider preflight_ceiling_ms/preflight_passage_n. query_instruction
    # stays ONLY on these Qwen vllm/cpu entries — cloud/LLM providers added in
    # M6/M7 must NOT inherit the Qwen instruct prefix (it inverts non-Qwen rankers).
    "qwen3-reranker-0.6b": {
        "provider": "vllm",
        "model_id": "Qwen/Qwen3-Reranker-0.6B",
        "label": "Qwen3 Reranker 0.6B (local GPU)",
        "vram_gb": 1.2,  # FP16 resident (M10 Task 10.2 budget arithmetic)
        "max_input_tokens": 32768,
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "Default post-GPU pick; pairs with the qwen3 embedding stores",
        "auth_kind": "none",
        "key_env": None,
        "cost_note": "Local GPU — free, private, unlimited (runs on your box; nothing leaves it)",
        "privacy": "local",
        "tiers": ["HIGH"],
        "preflight_ceiling_ms": 500,
        "preflight_passage_n": 1,
    },
    "qwen3-reranker-4b": {
        "provider": "vllm",
        "model_id": "Qwen/Qwen3-Reranker-4B",
        "label": "Qwen3 Reranker 4B (local GPU)",
        "vram_gb": 2.5,  # Q4 resident; FP16 ≈ 8GB wants the 16GB card mostly free
        "max_input_tokens": 32768,
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "Bigger cross-encoder — only if Phase B shows 0.6B leaves recall on the table",
        "auth_kind": "none",
        "key_env": None,
        "cost_note": "Local GPU — free, private, unlimited (runs on your box; nothing leaves it)",
        "privacy": "local",
        "tiers": ["HIGH"],
        "preflight_ceiling_ms": 500,
        "preflight_passage_n": 1,
    },
    # ── On-box localstack reranker (Milestone 4): Qwen3-Reranker-8B @ Q8_0 (D13 —
    # the retrieval group is sequential, so the reranker gets the whole card and we
    # run the best-that-fits-in-one-shot 8B; the 0.6B stays CPU-tier fallback only)
    # served by llama-server (--reranking --pooling rank) behind the llama-swap
    # front door, exposed as /v1/rerank. Provider "localstack" — the on-box
    # retrieval-group member, NOT the vLLM :8091 seam (retained as the FP16
    # fallback, which may NEVER co-run with this group; see preflight()). model_id
    # is the llama-swap MEMBER name ("rerank-qwen3-8b" in the config template) — llama-swap routes
    # /v1/rerank to that member by the body `model`. The GGUF is SELF-CONVERTED from
    # a pinned post-#16407 llama.cpp build (community GGUFs are broken → ~1e-28
    # scores); G2 (eval/rerank_g2.py) gates score validity + rank order before the
    # wizard flips the sidecar to this model. query_instruction is the SAME
    # mandatory Qwen instruct prefix as the vllm/cpu entries — the ranker inverts
    # without it (measured 2026-07-03). Keyless loopback (auth_kind none).
    "qwen3-reranker-8b-local": {
        "provider": "localstack",
        "model_id": "rerank-qwen3-8b",
        "label": "Qwen3 Reranker 8B Q8_0 (on-box, llama-swap)",
        "vram_gb": 8.1,  # Q8_0 8B resident ALONE in the sequential retrieval group (D13, §5.2 ~10–11GB peak)
        "max_input_tokens": 8192,  # matches the member's -c 8192 (config template)
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "On-box default reranker (Qwen3-Reranker-8B @ Q8_0, top-of-family); sequential with the embedder per D13 (~6–12s model-swap per search). Pairs with the on-box qwen3 embedding store. Self-converted GGUF — G2-gated for score validity.",
        "auth_kind": "none",
        "key_env": None,
        "cost_note": "On-box GPU/CPU — free, private, unlimited (runs on your box via llama-swap; nothing leaves it)",
        "privacy": "local",
        "tiers": ["MID", "HIGH"],
        # INERT for this provider (schema uniformity only): localstack is
        # evidence-gated (_EVIDENCE_PROVIDERS) and never probed synthetically,
        # so no latency ceiling is applied to it. Kept as the historical D13
        # per-scoring-call target (~76-106ms once loaded) — it was never a
        # full-batch budget (MEASURED 2026-08-11: a real 40 x 4096-char rerank
        # is ~16s warm), and gating on it is what let a reranker that 500s every
        # real search still report preflight ok.
        "preflight_ceiling_ms": 500,
        "preflight_passage_n": 1,     # llama.cpp /v1/rerank batches all docs in one call
    },
    # MID-tier opt-in (M5): the SAME Qwen 0.6B weights served IN-PROCESS on CPU
    # via a sentence-transformers CrossEncoder — no second service, no GPU. Slower
    # than the vLLM path (hence the 2000ms preflight ceiling + an 8-passage probe
    # that M3 extrapolates to the real candidate count). Carries ram_gb (a CPU
    # RAM footprint) where the GPU entries carry vram_gb — for the M10 wizard
    # footprint display. query_instruction is the Qwen instruct prefix: it is the
    # same model as the GPU 0.6b, so it needs the SAME prefix (it inverts without).
    "qwen3-reranker-0.6b-cpu": {
        "provider": "cpu",
        "model_id": "Qwen/Qwen3-Reranker-0.6B",
        "label": "Qwen3 Reranker 0.6B (local CPU)",
        "ram_gb": 2.0,  # approx resident RAM footprint (M10 wizard display)
        "max_input_tokens": 32768,
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "MID-tier opt-in; same weights as the GPU 0.6b, in-process on CPU (slower)",
        "auth_kind": "none",
        "key_env": None,
        "cost_note": "Local CPU — free, private, unlimited (on your box); slower than GPU/cloud",
        "privacy": "local",
        "tiers": ["MID"],
        "preflight_ceiling_ms": 2000,
        "preflight_passage_n": 8,
    },
    # ── LLM-as-reranker (M6): the cheap, keyless-beyond-existing-keys cloud
    # fallback tier. ONE entry per frontier chat key the box already holds. These
    # are NOT purpose-trained cross-encoders — a general frontier model is asked,
    # in a single non-streaming completion, to return a relevance-ordered
    # permutation of the candidate indices (listwise), mapped to synthetic
    # descending scores. Framed HONESTLY in quality_note so the wizard never
    # oversells it (Brandon's directive). NO query_instruction: the Qwen instruct
    # prefix INVERTS non-Qwen rankers (RERANK_MODELS discipline above). model_ids
    # are the box's current GA/flash/mini tiers, sourced from the fallback model
    # catalog (routes/admin_routes.py) + config.py — the cheap fast path, never a
    # date-pinned preview where a stable id exists. Cloud → no local footprint
    # (no vram_gb/ram_gb). Key resolved FRESH via os.getenv(key_env) at score
    # time (M4), so a wizard-mirrored paste takes effect with no restart.
    "llm-rerank-gemini-flash": {
        "provider": "llm",
        "model_id": "gemini-2.5-flash",   # GA flash text tier (web_tools/tasks)
        "label": "LLM reranker — Gemini Flash (your Google key)",
        "max_input_tokens": 1000000,
        "quality_note": "General LLM, not a purpose-trained ranker — budget/keyless fallback",
        "auth_kind": "frontier_key",
        "key_env": "GOOGLE_API_KEY",
        "cost_note": "Uses your existing Google/Gemini key — no separate signup or quota; ~cents/query",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 4000,
        "preflight_passage_n": 1,
    },
    "llm-rerank-gpt-mini": {
        "provider": "llm",
        "model_id": "gpt-5-mini-2025-08-07",   # box's GPT-5 Mini (fallback catalog)
        "label": "LLM reranker — GPT-5 Mini (your OpenAI key)",
        "max_input_tokens": 400000,
        "quality_note": "General LLM, not a purpose-trained ranker — budget/keyless fallback",
        "auth_kind": "frontier_key",
        "key_env": "OPENAI_API_KEY",
        "cost_note": "Uses your existing OpenAI key — no separate signup or quota; ~cents/query",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 4000,
        "preflight_passage_n": 1,
    },
    "llm-rerank-claude-haiku": {
        "provider": "llm",
        "model_id": "claude-haiku-4-5-20251001",   # box's Haiku (fallback catalog)
        "label": "LLM reranker — Claude Haiku (your Anthropic key)",
        "max_input_tokens": 200000,
        "quality_note": "General LLM, not a purpose-trained ranker — budget/keyless fallback",
        "auth_kind": "frontier_key",
        "key_env": "ANTHROPIC_API_KEY",
        "cost_note": "Uses your existing Anthropic key — no separate signup or quota; ~cents/query",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 4000,
        "preflight_passage_n": 1,
    },
    "llm-rerank-grok": {
        "provider": "llm",
        "model_id": "grok-4.3",   # box's current xAI default (config XAI_MODEL_DEFAULT)
        "label": "LLM reranker — Grok (your xAI key)",
        "max_input_tokens": 1000000,
        # grok-4.3 is xAI's FLAGSHIP — no cheaper Grok tier (mini/flash) ships,
        # so this is the pricier/slower LLM-fallback option; framed honestly so
        # the wizard never picks it as a "cheap" default (still NOT a ranker).
        "quality_note": "General LLM (xAI flagship — no cheaper Grok tier ships; pricier/slower than the Flash/mini/Haiku options), not a purpose-trained ranker",
        "auth_kind": "frontier_key",
        "key_env": "XAI_API_KEY",
        "cost_note": "Uses your existing xAI/Grok key; xAI flagship pricing — pricier than the Flash/mini/Haiku options",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 4000,
        "preflight_passage_n": 1,
    },
    # ── Dedicated cloud cross-encoders (M7): the PRIMARY quality cloud path.
    # Unlike the LLM fallback above these are PURPOSE-TRAINED rerankers, reached
    # over raw REST (no SDK deps). Bearer key read FRESH via os.getenv(key_env)
    # at score time (M4) so a wizard-mirrored paste needs no restart. NO
    # query_instruction — the Qwen instruct prefix INVERTS non-Qwen rankers
    # (RERANK_MODELS discipline). Cloud → no local vram_gb/ram_gb footprint.
    # Voyage is the recommended cloud DEFAULT; Cohere is the enterprise reference.
    "voyage-rerank-2.5": {
        "provider": "voyage",
        "model_id": "rerank-2.5",
        "label": "Voyage rerank-2.5 (cloud)",
        "max_input_tokens": 32000,
        "quality_note": "Dedicated cross-encoder — best speed/quality; recommended cloud default",
        "auth_kind": "bearer_env",
        "key_env": "VOYAGE_API_KEY",
        "cost_note": "Voyage API — ~$0.05/1M tokens; 200M-token free tier",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 1200,
        "preflight_passage_n": 1,
    },
    "cohere-rerank-4": {
        "provider": "cohere",
        "model_id": "rerank-v4.0-pro",
        "label": "Cohere Rerank 4 (cloud)",
        "max_input_tokens": 4096,
        "quality_note": "Dedicated cross-encoder — enterprise reference",
        "auth_kind": "bearer_env",
        "key_env": "COHERE_API_KEY",
        "cost_note": "Cohere API — free trial ~1,000 searches/mo (fine for personal use); ~$2/1K + billing after",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 1200,
        "preflight_passage_n": 1,
    },
    # Google Vertex AI semantic-ranker (M7.2) — a dedicated cross-encoder via the
    # Discovery Engine Ranking API. auth_kind gcp_service_account (NOT a bare
    # key): creds come from the ambient GCP service account
    # (GOOGLE_APPLICATION_CREDENTIALS, set + live-mirrored by the credentials
    # upload route), so key_env is None. Labeled "Advanced" honestly — it needs a
    # GCP project + SA + discoveryengine enablement, far heavier than paste-a-key
    # (Voyage is the recommended cloud default). No Qwen prefix; no local footprint.
    "vertex-semantic-ranker": {
        "provider": "vertex",
        "model_id": "semantic-ranker-default-004",
        "label": "Google Vertex semantic-ranker (cloud)",
        "max_input_tokens": 1024,  # ~per-record content limit (truncated in-code)
        "quality_note": "Dedicated cross-encoder (Advanced: requires GCP project + service-account setup)",
        "auth_kind": "gcp_service_account",
        "key_env": None,
        "cost_note": "Vertex AI Ranking API — ~$1/1K queries; needs a GCP project + service account",
        "privacy": "cloud",
        "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": 1500,
        "preflight_passage_n": 1,
    },
}

_DEFAULT_MODEL_SLUG = "qwen3-reranker-0.6b"

# Providers score() knows how to dispatch (M2). "null" is the inert default;
# the rest each map to a _score_<provider> helper. vllm/cpu/llm ship now;
# voyage/cohere/vertex (M7) are stubbed to return None until then. localstack
# (Milestone 4) is registered here so its model is selectable/preflightable, but
# its _score_localstack dispatch entry lands in Task 4.2 — until then selecting
# it scores None, which the retriever tolerates (falls through to the
# un-reranked ranking). Deliberate staged stub, not a missing helper.
KNOWN_PROVIDERS = {"null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm",
                   "localstack"}

# Cloud providers whose FAILED preflight is TTL-recoverable (M3.2): a transient
# cloud blip must NOT disable rerank until the next restart. Deliberate,
# documented deviation from audit A9's local-only once-per-process assumption —
# the local vllm/cpu providers keep the process-lifetime failure cache.
CLOUD_PROVIDERS = {"voyage", "cohere", "vertex", "llm"}

# ── capability from REAL traffic vs. a synthetic probe ────────────────────────
# The on-box batch providers are gated on evidence, never probed. History, so
# nobody re-adds the probe: the shipped 1-short-passage probe was structurally
# blind (MS02, 2026-08-11 — the on-box reranker returned HTTP 500 for EVERY real
# search, a llama.cpp batch limit below 40x4096 chars, while /rerank/status
# reported available:true off a 25-character passage that stayed under it). The
# fix that saw it was a PRODUCTION-SHAPED probe — and that probe is a real ~16s
# rerank (21.3s cold) on the first search after every restart, taken under
# _preflight_lock so concurrent searches serialize behind it. It buys exactly
# what the next real search reports for free, so the probe is gone: score()
# records real outcomes (_record_evidence) and preflight() reads them.
# Metered/cpu providers are NOT in here — their probe is one short passage
# (cheap, and their failure mode is auth/quota/deps, not our batch size), and a
# full-size batch every TTL would bill the operator.
_EVIDENCE_PROVIDERS = {"localstack", "vllm"}

# How long a real SUCCESS keeps being reported as "ok". This is an HONESTY
# window, not a gate: `unknown` attempts a rerank exactly like `ok` does, so a
# decayed record costs nothing — it only stops status() claiming a healthy
# reranker off hours-old evidence in a service that runs for weeks.
_EVIDENCE_OK_TTL_S = 600.0
# A real FAILURE closes the gate for _PREFLIGHT_FAIL_TTL_S (the same 60s a
# failed cloud probe closes it), then decays to unknown and the next real search
# retries — a wedged reranker costs un-reranked results once a minute, never a
# restart to recover.

# The short filler the probing providers have always used.
_PROBE_PASSAGE = "preflight probe passage"


def _key_present_for(auth_kind: "str | None", key_env: "str | None") -> bool:
    """Whether the credential a model needs is configured, read FRESH (M4).

    gcp_service_account (Vertex) has NO key_env — its credential is the service-
    account file at GOOGLE_APPLICATION_CREDENTIALS (set + live-mirrored by the
    credentials upload route), so presence resolves from THAT, mirroring
    reachable(). Every other cloud/frontier model resolves from its bearer
    key_env. Fixes the wizard bug where an uploaded Google SA still showed Vertex
    as unselectable (key_present was always False for a None key_env)."""
    if auth_kind == "gcp_service_account":
        return bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    return bool(key_env and os.getenv(key_env))

# One-time-per-process preflight cache (audit A9). Guarded because retrieve()
# runs from FastAPI's threadpool — two first-uses must not double-probe.
# _preflight_expiry is the monotonic deadline for a TTL-bound (cloud-failed)
# cache entry; None means the entry sticks for the process lifetime (audit A9).
_preflight_lock = threading.Lock()
_preflight_result: dict | None = None
_preflight_expiry: float | None = None
_PREFLIGHT_FAIL_TTL_S = 60.0

# Short-TTL reachability cache (M13 wizard): status() is consumed by the
# onboarding rollup + wizard cards, which may poll — the probe itself is
# ~1s-capped, the cache keeps repeat calls free. Distinct from the preflight
# cache on purpose: reachability recovers live (vLLM's cold start can take
# minutes on first boot), the preflight is deliberately once-per-process.
_REACH_TTL_S = 5.0
_reach_lock = threading.Lock()
_reach_cache: "tuple[float, bool] | None" = None


# ── un-reranked fallthrough record (observability) ────────────────────────────
# Falling through to the un-reranked ranking is a real degradation of search
# quality, not a curiosity: on MS02 every search silently lost the rerank stage
# for days because the one log line was emitted ONCE PER PROCESS and the service
# runs for weeks. Every occurrence is logged and counted here, and the latest
# reason rides on GET /rerank/status. The journal cannot flood: a scoring
# failure closes the gate until it clears — as failed capability evidence for
# the on-box providers (one line per _PREFLIGHT_FAIL_TTL_S at worst), and by
# reopening the cached probe (below) for the probing ones.
_fallthrough_lock = threading.Lock()
_fallthrough: dict = {"count": 0, "reason": None, "provider": None,
                      "model": None, "at": None}

# Per-thread scoring bookkeeping: `in_preflight` suppresses recording while
# preflight() runs its own probe (that failure is already reported as the
# preflight state), `noted` lets an inner helper record a more specific reason
# than the dispatcher could.
_score_ctx = threading.local()


def _reopen_preflight_after_fallthrough() -> None:
    """Drop a cached OK preflight after a real scoring failure so the next gate
    check re-probes. Without this, status() keeps reporting available:true off a
    probe that passed once while every live search falls through. Non-blocking:
    if a probe already holds the lock it is about to publish fresh state
    anyway (and score() runs INSIDE that probe — never block on it)."""
    global _preflight_result, _preflight_expiry
    if not _preflight_lock.acquire(blocking=False):
        return
    try:
        if _preflight_result is not None and _preflight_result.get("state") == "ok":
            _preflight_result = None
            _preflight_expiry = None
    finally:
        _preflight_lock.release()


def _note_fallthrough(reason: str, settings: "dict | None" = None,
                      health_signal: bool = True) -> None:
    """Record + log ONE un-reranked fallthrough. `health_signal=False` for a
    degradation that says nothing about reranker health (the D12 voice gate
    deliberately yields the retrieval group) — it is still counted and logged,
    it just must not become FAILED capability evidence (nor cost a cloud
    re-probe) on every voice-heavy search."""
    _score_ctx.noted = True
    if getattr(_score_ctx, "in_preflight", False):
        return
    s = settings or {}
    with _fallthrough_lock:
        _fallthrough["count"] += 1
        _fallthrough["reason"] = str(reason)[:200]
        _fallthrough["provider"] = s.get("provider")
        _fallthrough["model"] = s.get("model")
        _fallthrough["at"] = time.time()
        snapshot = dict(_fallthrough)
    print(f"[RERANK] {snapshot['reason']}; falling through to the un-reranked "
          f"ranking (provider={snapshot['provider']} "
          f"model={snapshot['model']}, {snapshot['count']} this process)")
    if health_signal:
        _record_evidence("failed", s, reason=snapshot["reason"])
        _reopen_preflight_after_fallthrough()


def fallthrough_state() -> dict:
    """The running un-reranked-fallthrough record for GET /rerank/status:
    {count, reason, provider, model, at} (`at` = epoch seconds). Never raises."""
    with _fallthrough_lock:
        return dict(_fallthrough)


# ── capability evidence from real scoring calls ───────────────────────────────
# What the synthetic probe used to assert, recorded from the searches that
# actually ran. Written by score() (ok) and _note_fallthrough (failed); read by
# preflight() for the _EVIDENCE_PROVIDERS and reported verbatim on
# /rerank/status. `state` here is the LAST RECORDED outcome; evidence_state()
# decays it to "unknown" once its TTL passes, so nothing can claim ok off a
# stale record. Probe traffic is excluded (in_preflight) — a probe is not
# evidence about production.
_evidence_lock = threading.Lock()
_evidence: dict = {"state": "unknown", "at": None, "reason": None,
                   "provider": None, "model": None, "passage_n": None,
                   "passage_chars": None, "latency_ms": None}
_evidence_deadline: "float | None" = None    # monotonic; None = no record


def _record_evidence(state: str, settings: "dict | None" = None,
                     reason: "str | None" = None,
                     latency_ms: "float | None" = None) -> None:
    """Record the outcome of a REAL scoring call. The batch shape rides on
    _score_ctx (set by score()) so an inner helper's more specific reason keeps
    its numbers. Never raises."""
    global _evidence_deadline
    if getattr(_score_ctx, "in_preflight", False):
        return
    s = settings or {}
    passage_n, passage_chars = getattr(_score_ctx, "batch", (None, None))
    with _evidence_lock:
        _evidence.update(
            state=state, at=time.time(), reason=reason,
            provider=s.get("provider"), model=s.get("model"),
            passage_n=passage_n, passage_chars=passage_chars,
            latency_ms=(round(latency_ms, 1) if latency_ms is not None
                        else None))
        _evidence_deadline = time.monotonic() + (
            _EVIDENCE_OK_TTL_S if state == "ok" else _PREFLIGHT_FAIL_TTL_S)


def _evidence_ttl_s(state: str) -> "float | None":
    if state == "ok":
        return _EVIDENCE_OK_TTL_S
    if state == "failed":
        return _PREFLIGHT_FAIL_TTL_S
    return None


def evidence_state() -> dict:
    """The three honest capability states, from real traffic only:
      ok      — a real rerank succeeded within _EVIDENCE_OK_TTL_S;
      failed  — a real rerank failed (reason + batch shape carried);
      unknown — no record yet, or the last one aged out (`stale` True, and the
                record's `at`/`reason` are kept for ops).
    `at` is epoch seconds. Never raises, never probes anything."""
    with _evidence_lock:
        ev = dict(_evidence)
        deadline = _evidence_deadline
    fresh = deadline is not None and time.monotonic() < deadline
    ev["ttl_s"] = _evidence_ttl_s(ev["state"])
    ev["stale"] = bool(ev["state"] != "unknown" and not fresh)
    if not fresh:
        ev["state"] = "unknown"
    return ev


_cpu_importable: "bool | None" = None

# M5 in-process CPU CrossEncoder: the loaded model is process-cached (keyed by
# model_id) behind a lock — load once, reuse across retrieve() calls (mirrors
# the preflight cache pattern). Cleared by reset_preflight() so an M8 provider/
# model switch (and every test) re-loads fresh.
_cpu_model_lock = threading.Lock()
_cpu_model_cache: dict = {}


def _probe_localhost(base_url: str, timeout_s: float = 1.0) -> bool:
    """TTL-cached GET {base_url}/v1/models (vLLM's model-list endpoint — up as
    soon as the engine finishes loading), ~1s cap. Never raises. Shared by
    service_reachable() and reachable()'s vllm/null path."""
    global _reach_cache
    now = time.monotonic()
    with _reach_lock:
        if _reach_cache is not None and (now - _reach_cache[0]) < _REACH_TTL_S:
            return _reach_cache[1]
    ok = False
    if base_url:
        try:
            ok = requests.get(
                base_url + "/v1/models", timeout=timeout_s
            ).status_code == 200
        except Exception:  # noqa: BLE001 - never-raise, mirrors score()
            ok = False
    with _reach_lock:
        _reach_cache = (now, ok)
    return ok


def service_reachable(timeout_s: float = 1.0) -> bool:
    """Back-compat wrapper (M13): is something answering on the resolved
    [rerank] base_url? The localhost /v1/models probe, provider-INDEPENDENT —
    it detects the fresh-GPU-box "service up, awaiting the config flip" state
    even under the null provider. reachable() below is the provider-aware
    generalization (M3.2); this stays as the vLLM/GPU-box detector."""
    return _probe_localhost(get_settings()["base_url"], timeout_s)


def _cpu_reachable() -> bool:
    """CPU cross-encoder deps present? sentence-transformers importable
    (find_spec only — no heavy import), cached. The simplest honest
    reachability signal for the in-process cpu provider (there is no service to
    poll); M5 adds the provider itself, M4 the fuller readiness story."""
    global _cpu_importable
    if _cpu_importable is None:
        try:
            import importlib.util
            _cpu_importable = (
                importlib.util.find_spec("sentence_transformers") is not None)
        except Exception:  # noqa: BLE001 - never-raise
            _cpu_importable = False
    return _cpu_importable


def reachable(settings: dict | None = None, timeout_s: float = 1.0) -> bool:
    """Provider-aware reachability (M3.2); never raises:
      vllm / null / unknown → the localhost /v1/models probe;
      cpu                   → sentence-transformers importable (no service);
      cloud (voyage/cohere/vertex/llm) → key/creds PRESENT — a config/env read,
                              NEVER a paid network poll (actual cloud
                              reachability is proven once by preflight()).
    Cloud key resolution is the forward-compatible os.getenv(key_env) floor;
    M4 refines the full sidecar>config>env order but the env read is stable."""
    s = settings or get_settings()
    p = s["provider"]
    if p == "cpu":
        return _cpu_reachable()
    if p == "localstack":
        return _localstack_healthy()
    if p in CLOUD_PROVIDERS:
        # Vertex (gcp_service_account) has NO key_env, so key_present is always
        # False even with a valid GCP SA — its reachability is the ambient SA
        # creds path the credentials upload route sets/live-mirrors. Check that
        # instead (still a pure env read, NEVER a paid network poll).
        if s.get("auth_kind") == "gcp_service_account":
            return bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
        # key/creds present — the fresh os.getenv read get_settings already did
        # (M4); NEVER a paid network poll. (settings dicts always carry it;
        # fall back to a fresh read if an external caller hand-built one.)
        if "key_present" in s:
            return bool(s["key_present"])
        key_env = s.get("key_env")
        return bool(key_env and os.getenv(key_env))
    return _probe_localhost(s["base_url"], timeout_s)


# ── On-box localstack reranker helpers (Milestone 4) ──────────────────────────
# The localstack reranker posts to the llama-swap front door (/v1/rerank),
# resolved from the [local_models] base_url via Orchestrator/local_stack.py — NOT
# the [rerank] base_url (which defaults to the legacy vLLM :8091 seam). local_stack
# is imported LAZILY (cycle-proof, mirroring _load_sidecar's rationale: local_stack
# may import rerank for capability routing, so a top-level import risks a cycle).
# Absent/uninstalled resolver → None/False so the provider stays inert, never raises.

def _localstack_base_url() -> "str | None":
    """The llama-swap front-door base_url ([local_models]) for the on-box
    reranker, or None when the resolver is unavailable (M1 not landed / stack not
    installed). Never raises."""
    try:
        from Orchestrator import local_stack
        return local_stack.base_url()
    except Exception:  # noqa: BLE001 - resolver absent → inert
        return None


def _localstack_healthy() -> bool:
    """Whether the on-box stack is installed + configured + llama-swap is live
    (local_stack.is_healthy(), which keys on install/config/process-liveness, NOT
    live per-member VRAM residency — a group swap never flips this). Never raises."""
    try:
        from Orchestrator import local_stack
        return bool(local_stack.is_healthy())
    except Exception:  # noqa: BLE001 - resolver absent → not healthy
        return False


def _member_gguf_present(model_id: str = "rerank-qwen3-8b") -> bool:
    """Is the on-box reranker's GGUF physically on disk (the reranker BUILT flag,
    A4)? Delegates to local_stack._member_gguf_present — the single on-disk-truth
    presence check — lazily (cycle-proof: local_stack imports rerank for
    capability routing) and never-raises (absent resolver / uninstalled stack →
    False). This is the truth the reranker UI card keys on, replacing the
    gpu-and-service_reachable proxy (which is False for the self-converted GGUF
    that never binds the vLLM :8091 port)."""
    try:
        from Orchestrator import local_stack
        return bool(local_stack._member_gguf_present(model_id))
    except Exception:  # noqa: BLE001 - resolver absent / uninstalled → not built
        return False


def _vllm_reranker_running(timeout_s: float = 1.0) -> bool:
    """Is the legacy vllm-reranker.service answering on its :8091 port? A direct
    ~1s-capped GET of DEFAULT_BASE_URL/v1/models (NOT the shared _probe_localhost
    cache — this is a distinct, safety-critical probe for the §5.2 hard rule).
    Never raises."""
    try:
        return requests.get(
            DEFAULT_BASE_URL + "/v1/models", timeout=timeout_s
        ).status_code == 200
    except Exception:  # noqa: BLE001 - never-raise
        return False


# The §5.2 co-run check is a LIVENESS read (is the legacy service up?), and it
# now runs on the capability path of every search — there is no once-per-process
# probe left to hide behind — so it gets its own short TTL. The answer only
# changes when an operator starts/stops a service, and a 30s-stale read still
# resolves it live (no restart), which is the property the rule needs.
_CORUN_TTL_S = 30.0
_corun_lock = threading.Lock()
_corun_cache: "tuple[float, bool] | None" = None


def _vllm_corun_conflict() -> bool:
    """TTL-cached §5.2 co-run check: is the legacy vLLM reranker holding VRAM
    that the on-box retrieval group would OOM against? Never raises."""
    global _corun_cache
    now = time.monotonic()
    with _corun_lock:
        if _corun_cache is not None and (now - _corun_cache[0]) < _CORUN_TTL_S:
            return _corun_cache[1]
    up = _vllm_reranker_running()
    with _corun_lock:
        _corun_cache = (now, up)
    return up


# Malformed-config resilience (M3.1 fold-in from the M2 review). M2 moved
# get_settings() outside score()'s try, so a non-numeric [rerank] value would
# now propagate out of score()/status() as a ValueError. Fix at the source:
# a bad numeric config value degrades to the default (logged once) so the whole
# module keeps its never-raise contract (audit A9) even on hand-edited typos.
_warned_cfg: set[str] = set()


def _warn_malformed(section: str, option: str) -> None:
    key = f"{section}.{option}"
    if key not in _warned_cfg:
        _warned_cfg.add(key)
        print(f"[RERANK] malformed [{section}] {option} in config — "
              f"using the default")


def _cfg_float(section: str, option: str, fallback: float) -> float:
    """CFG.getfloat that degrades a malformed (non-numeric) value to `fallback`
    (logged once) instead of raising."""
    try:
        return CFG.getfloat(section, option, fallback=fallback)
    except (ValueError, TypeError):
        _warn_malformed(section, option)
        return fallback


def _cfg_int(section: str, option: str, fallback: int) -> int:
    """CFG.getint with the same malformed-config resilience as _cfg_float."""
    try:
        return CFG.getint(section, option, fallback=fallback)
    except (ValueError, TypeError):
        _warn_malformed(section, option)
        return fallback


def _cfg_bool(section: str, option: str, fallback: bool) -> bool:
    """CFG.getboolean with the same malformed-config resilience as _cfg_float
    (an unrecognised truthy string degrades to `fallback`, logged once)."""
    try:
        return CFG.getboolean(section, option, fallback=fallback)
    except (ValueError, TypeError):
        _warn_malformed(section, option)
        return fallback


def _load_sidecar() -> "dict | None":
    """The rerank.json selection {enabled, provider, model}, or None when
    absent/corrupt (store.get_rerank_selection is itself fail-open). Lazily
    imported: keeps rerank.py's top-level import surface minimal (store pulls in
    numpy + the embeddings registry) AND is cycle-proof regardless of future
    refactors. Wrapped never-raise so a bad sidecar can only fall through to
    config, never propagate out of get_settings (audit A9 / A13 fresh-box)."""
    try:
        import Orchestrator.embeddings.store as _store
        return _store.get_rerank_selection()
    except Exception:  # noqa: BLE001 - never-raise; fall back to config
        return None


def get_settings() -> dict:
    """Resolved reranker config, RESOLUTION ORDER: rerank.json sidecar →
    config.ini [rerank] → code fallback (M4). The sidecar is the wizard/Portal
    selection (POST /rerank/select); when present its provider/model win over
    config, so a selection takes effect with no restart or config.ini edit.
    Fresh-box safe: no sidecar AND no [rerank] section → provider "null" = inert.

    Per-provider preflight tuning (preflight_ceiling_ms / preflight_passage_n)
    and the auth descriptor (auth_kind / key_env) are resolved FROM the selected
    model's RERANK_MODELS entry (M3.1); a [rerank] preflight_ceiling_ms in
    config still overrides the registry ceiling. `key_present` reads the key_env
    FRESH via os.getenv at call time (M4) — never config.py's frozen import-time
    constants — so a wizard-mirrored os.environ write is visible immediately.
    Numeric reads are malformed-config-resilient (a bad value falls back, logged
    once) so get_settings() — and thus score()/status() — never raise (A9)."""
    sel = _load_sidecar()
    # provider/model: sidecar wins when it carries a non-blank value, else config,
    # else the code fallback (null / default slug).
    sel_provider = str(sel.get("provider", "")).strip() if sel else ""
    if sel_provider:
        provider = sel_provider.lower()
    else:
        provider = CFG.get("rerank", "provider", fallback="null").strip().lower()
    base_url = CFG.get(
        "rerank", "base_url", fallback=DEFAULT_BASE_URL
    ).strip().rstrip("/")
    sel_model = str(sel.get("model", "")).strip() if sel else ""
    if sel_model:
        model = sel_model
    else:
        model = CFG.get("rerank", "model", fallback=_DEFAULT_MODEL_SLUG).strip()
    entry = RERANK_MODELS.get(model)
    key_env = entry.get("key_env") if entry else None
    # Registry-sourced preflight tuning (config ceiling override still wins).
    entry_ceiling = entry.get("preflight_ceiling_ms") if entry else None
    ceiling_fallback = float(entry_ceiling) if entry_ceiling is not None else 500.0
    passage_n = int(entry.get("preflight_passage_n", 1)) if entry else 1
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
        "timeout_s": _cfg_float("rerank", "timeout_s", 15.0),
        # The on-box (llama-swap) READ deadline: its OWN knob, defaulting to the
        # bounded swap-tolerant cap (see LOCALSTACK_READ_TIMEOUT_S). Deliberately
        # NOT [rerank] timeout_s (the cloud deadline): setting that to 15 used
        # to drop the on-box read back to the 15s swap abort. Connect stays short.
        "localstack_read_timeout_s": _cfg_float(
            "rerank", "localstack_timeout_s", LOCALSTACK_READ_TIMEOUT_S),
        # Config override wins over the model entry's registry ceiling. Gates
        # the PROBING providers only (cpu/cloud/llm) — the evidence providers
        # are not timed against a ceiling at all (_EVIDENCE_PROVIDERS).
        "preflight_ceiling_ms": _cfg_float(
            "rerank", "preflight_ceiling_ms", ceiling_fallback),
        "preflight_passage_n": passage_n,
        # Head-cut retrieval.py applies to every passage.
        "passage_chars": _cfg_int("rerank", "passage_chars", 4096),
        # Candidate count the reranker sees at retrieve()-time; feeds the CPU
        # preflight extrapolation. Lives in [retrieval]; resilient read.
        "rerank_candidate_n": _cfg_int("retrieval", "rerank_candidate_n", 40),
        # M2 auth descriptor surfaced for reachability + status (M3) and key
        # plumbing (M4). "none"/None on a fresh box or unknown model.
        "auth_kind": entry.get("auth_kind", "none") if entry else "none",
        "key_env": key_env,
        # Key resolved FRESH (M4): the selected model's key_env read via
        # os.getenv at THIS call — not config.py's frozen constants. This is the
        # single live-read the reachable()/status() key checks consume, so a
        # newly-pasted key mirrored into os.environ is seen with no restart.
        # Vertex (gcp_service_account) resolves from GOOGLE_APPLICATION_CREDENTIALS.
        "key_present": _key_present_for(
            entry.get("auth_kind") if entry else None, key_env),
    }


def is_enabled() -> bool:
    """Is the reranker turned ON? Resolution mirrors get_settings (M4): the
    rerank.json sidecar's `enabled` wins whenever the sidecar EXISTS (the
    wizard/Portal selection, even over a contradicting config), else the
    resilient [retrieval] rerank_enabled read (default False on a fresh box, and
    a malformed value degrades to False — never raises).

    M4 PROVIDES this; it is NOT yet wired into retrieval.py's rerank gate — that
    swap is M8 (the selector-endpoint milestone), so retrieval still reads its
    own [retrieval] rerank_enabled today."""
    sel = _load_sidecar()
    if sel is not None:
        return bool(sel.get("enabled", False))
    return _cfg_bool("retrieval", "rerank_enabled", False)


def _configured(settings: dict | None = None) -> bool:
    """Is a real (non-null) provider selected AND provider-appropriately ready?

    M2 keeps this minimal — per-provider readiness firms up in M3 (reachability)
    and M4 (key/creds plumbing):
      vllm/cpu → a base_url is resolved (the cpu deps — sentence-transformers/
                 torch — and every provider's keys are gated at
                 preflight()/reachable(), NOT here: a selected provider counts as
                 configured even before its deps/key resolve, so status can
                 report the real not-ready reason instead of falsely gating off);
      cloud (voyage/cohere/vertex) + llm → not blocked here; their key/creds
                 checks live in reachable()/the provider helper, so a selected
                 provider counts as configured (do not falsely gate them off).
    """
    s = settings or get_settings()
    p = s["provider"]
    if p not in (KNOWN_PROVIDERS - {"null"}):
        return False
    if p in ("vllm", "cpu"):
        return bool(s["base_url"])
    return True


def _score_vllm(query: str, passages: list[str],
                settings: dict) -> list[float] | None:
    """vLLM /score cross-encoder scoring — the original score() body, verbatim.

    Does NOT wrap its own try/except: the dispatcher owns the never-raise
    backstop (audit A9). Returns None on unconfigured/non-200/malformed/
    row-count-mismatch exactly as before; `settings` is passed in so score()
    resolves config once per call (no double get_settings)."""
    s = settings
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


# ── provider helpers ──────────────────────────────────────────────────────────
# Same contract as _score_vllm: (query, passages, settings) -> list[float] | None,
# positionally aligned to `passages`, None on ANY failure. cpu (M5) + llm (M6)
# are implemented below; voyage/cohere/vertex (M7) are inert stubs until built.

def _load_cpu_model(model_id: str):
    """Return a process-cached sentence-transformers CrossEncoder for
    `model_id`, or None on ANY import/load failure. The import is LAZY (inside
    this function, NEVER at module top) so a fresh LOW box — which has no
    torch/sentence-transformers — stays import-clean; absent deps make the CPU
    provider inert, never raise. Double-checked-locked so concurrent retrieve()
    calls from the FastAPI threadpool load the ~0.6B model only once."""
    model = _cpu_model_cache.get(model_id)
    if model is not None:
        return model
    with _cpu_model_lock:
        model = _cpu_model_cache.get(model_id)
        if model is not None:
            return model
        try:
            from sentence_transformers import CrossEncoder
            model = CrossEncoder(model_id)
        except Exception:  # noqa: BLE001 - absent deps / load failure → inert
            return None
        _cpu_model_cache[model_id] = model
        return model


def _score_cpu(query: str, passages: list[str],
               settings: dict) -> list[float] | None:
    """In-process CrossEncoder scoring — the MID-tier opt-in path (no second
    service; runs in the FastAPI threadpool where retrieve() already executes).
    Prepends the model's query_instruction to the query (parity with
    _score_vllm — the Qwen reranker inverts without its instruct prefix) and
    returns per-passage scores as python floats aligned to `passages` (the real
    predict() returns an ndarray). None on absent deps / load failure — a clean
    None on import/load failure is better UX than relying on the dispatcher's
    never-raise backstop (which still guards a predict() blow-up)."""
    if not passages:
        return None
    model = _load_cpu_model(settings["model_id"])
    if model is None:
        return None
    instructed = settings.get("query_instruction", "") + query
    scores = model.predict([(instructed, p) for p in passages])
    out = [float(s) for s in scores]
    if len(out) != len(passages):
        return None
    return out


# ── dedicated cloud cross-encoders (M7) ───────────────────────────────────────
# Voyage + Cohere: purpose-trained rerankers over raw REST (no SDK deps). Each
# returns a list of {index, relevance_score} rows scattered back to their
# passage positions so the returned vector is positionally aligned. The array
# KEY differs by provider (live-verified): Voyage nests rows under `data` (its
# envelope is {"object":"list","data":[...],"model":...,"usage":...}), Cohere
# under `results`. Bearer key read FRESH via os.getenv(key_env) at score time
# (M4). None on missing key / non-200 / count-or-index anomaly; transport
# blow-ups are backstopped by the dispatcher's never-raise (audit A9). NO Qwen
# instruct prefix (it inverts non-Qwen rankers) — the raw query goes on the wire.

def _scatter_relevance_scores(payload: "dict | None",
                              n: int) -> "list[float] | None":
    """Scatter a cloud reranker's {index, relevance_score} rows back onto passage
    positions: init None, out[index] = relevance_score, then require no gap. The
    rows array key differs by provider (live-verified): Voyage uses `data`, Cohere
    `results` — accept either, `data` first. None on ANY shape/count/index anomaly
    — a partial result (a duplicate index leaving another position unfilled) can't
    rank on one scale, so the retriever must fall through to its un-reranked
    ranking (never guess). Mirrors _scatter_vertex_records' None-init gap check for
    symmetry — a valid all-unique response is byte-identical to the old 0.0-init."""
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("results")
    if not isinstance(rows, list) or len(rows) != n:
        return None
    out: list[float | None] = [None] * n
    for item in rows:
        try:
            idx = int(item["index"])
            sc = float(item["relevance_score"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= idx < n):          # out of range → None (never IndexError)
            return None
        out[idx] = sc
    if any(v is None for v in out):     # a duplicate/missing index left a gap
        return None
    return out  # type: ignore[return-value]


def _score_voyage(query: str, passages: list[str],
                  settings: dict) -> list[float] | None:
    """Voyage rerank (POST /v1/rerank) — the recommended cloud default. Requests
    ALL passages back (top_k=len) so every passage is scored on one scale (no
    truncation). Missing key / non-200 / malformed / count-mismatch → None."""
    if not passages:
        return None
    key_env = settings.get("key_env")
    if not key_env:
        return None
    api_key = os.getenv(key_env)
    if not api_key:
        return None
    resp = requests.post(
        VOYAGE_RERANK_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "content-type": "application/json"},
        json={"query": query, "documents": list(passages),
              "model": settings["model_id"], "top_k": len(passages)},
        timeout=settings["timeout_s"],
    )
    if resp.status_code != 200:
        return None
    return _scatter_relevance_scores(resp.json(), len(passages))


def _score_cohere(query: str, passages: list[str],
                  settings: dict) -> list[float] | None:
    """Cohere Rerank (POST /v2/rerank) — the enterprise reference. top_n=len so
    all passages come back scored; same scatter-by-index + failure→None contract
    as Voyage."""
    if not passages:
        return None
    key_env = settings.get("key_env")
    if not key_env:
        return None
    api_key = os.getenv(key_env)
    if not api_key:
        return None
    resp = requests.post(
        COHERE_RERANK_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "content-type": "application/json"},
        json={"model": settings["model_id"], "query": query,
              "documents": list(passages), "top_n": len(passages)},
        timeout=settings["timeout_s"],
    )
    if resp.status_code != 200:
        return None
    return _scatter_relevance_scores(resp.json(), len(passages))


# ── On-box localstack reranker (Milestone 4) ──────────────────────────────────
# llama.cpp's /v1/rerank (--reranking --pooling rank) behind llama-swap. Wire shape
# {model, query, documents} → {results:[{index, relevance_score}]} — the SAME row
# shape the cloud rerankers return, so _scatter_relevance_scores parses it verbatim
# (no bespoke parser). The mandatory Qwen3-Reranker instruct prefix is prepended to
# the query (the ranker inverts without it). Base URL is the [local_models] front
# door (loopback, keyless), NOT the [rerank] :8091 vLLM seam. None on absent stack /
# non-200 / malformed / count-or-index anomaly; transport blow-ups are backstopped
# by the dispatcher's never-raise (audit A9).

def _do_localstack_rerank(query: str, passages: list[str],
                          settings: dict) -> list[float] | None:
    """On-box Qwen3-Reranker-8B @ Q8_0 via llama-swap /v1/rerank (the retrieval-group
    member, sequential per D13). Posts {model, query: instruction+query, documents} and scatters
    {index, relevance_score} back onto passage positions. Synchronous HTTP dispatch
    (requests.post) — the real POST, gate-free. Both gated entries wrap THIS behind
    the on-box voice-session gate (D12): the production sync score() path via
    _score_localstack_sync (blocking retrieval_gate_sync), and the async
    _score_localstack (async retrieval_gate) for a future async dispatch."""
    if not passages:
        return None
    base = _localstack_base_url()
    if not base:
        return None
    resp = requests.post(
        base.rstrip("/") + "/rerank",
        json={"model": settings["model_id"],
              "query": settings.get("query_instruction", "") + query,
              "documents": list(passages)},
        # (connect, read): a swap is a long READ, never a long connect.
        timeout=(LOCALSTACK_CONNECT_TIMEOUT_S,
                 settings.get("localstack_read_timeout_s",
                              LOCALSTACK_READ_TIMEOUT_S)),
    )
    if resp.status_code != 200:
        # The batch shape is in the reason on purpose: an oversized batch (the
        # llama.cpp -b/-ub class of failure) 500s while a small one succeeds, so
        # "HTTP 500 for 40 passages / 163840 chars" is the actionable fact.
        _note_fallthrough(
            f"on-box reranker returned HTTP {resp.status_code} for "
            f"{len(passages)} passages / "
            f"{sum(len(p or '') for p in passages)} chars", settings)
        return None
    return _scatter_relevance_scores(resp.json(), len(passages))


async def _score_localstack(query: str, passages: list[str],
                            settings: "dict | None" = None) -> list[float] | None:
    """Async gated entry for the on-box localstack reranker (:9098 /v1/rerank).

    D12: serialize behind any open on-box voice stream; a gate timeout means
    un-reranked retrieval (score None), never an error (a dead reranker costs
    latency, never recall — §6). Wraps the M4 real dispatch (_do_localstack_rerank)
    in local_stack.retrieval_gate(); on the bounded-wait timeout, returns None so
    the retriever falls through to its un-reranked ranking rather than raising.
    """
    from Orchestrator import local_stack  # lazy: avoid import cycle
    try:
        async with local_stack.retrieval_gate(
                timeout=local_stack.RETRIEVAL_GATE_TIMEOUT_S):
            if settings is None:
                settings = get_settings()
            return _do_localstack_rerank(query, passages, settings)
    except asyncio.TimeoutError:
        _note_fallthrough("voice session held the on-box retrieval group",
                          settings, health_signal=False)
        return None
    except Exception:  # noqa: BLE001 - never-raise backstop (audit A9): a
        # transport error from _do_localstack_rerank on an unreachable :9098
        # (requests.ConnectionError) must degrade to un-reranked, mirroring the
        # sync score() dispatcher's swallow-to-None — not propagate to the caller.
        return None


def _score_localstack_sync(query: str, passages: list[str],
                           settings: dict) -> list[float] | None:
    """Production SYNC entry for the on-box localstack reranker — what score()
    dispatches to (rerank runs synchronously in the FastAPI threadpool).

    D12: serialize behind any open on-box voice stream via the BLOCKING gate
    (retrieval_gate_sync — score() cannot await the async gate). On the
    bounded-wait timeout, return None (un-reranked): a voice-held reranker costs
    latency, never recall (§6). Transport/HTTP failures inside the real dispatch
    already return None via _do_localstack_rerank's own guards, and score()'s
    A9 backstop is the final net."""
    from Orchestrator import local_stack  # lazy: avoid import cycle
    try:
        with local_stack.retrieval_gate_sync(
                timeout=local_stack.RETRIEVAL_GATE_TIMEOUT_S):
            return _do_localstack_rerank(query, passages, settings)
    except asyncio.TimeoutError:
        # Counted and logged like any other fallthrough, but health_signal=
        # False: a gate yield is the D12 design working, not a sick reranker —
        # it must never become FAILED capability evidence.
        _note_fallthrough("voice session held the on-box retrieval group",
                          settings, health_signal=False)
        return None


# ── Google Vertex semantic-ranker (M7.2) ──────────────────────────────────────
# The Discovery Engine Ranking API, reached over raw REST (no google-cloud SDK —
# only google.auth, transitively present, for the OAuth token). Auth is a GCP
# SERVICE ACCOUNT, not a bearer key: google.auth.default() picks up the ambient
# GOOGLE_APPLICATION_CREDENTIALS (set + live-mirrored by the credentials upload
# route), and we refresh it to mint an access token. Each passage becomes a
# record {id: str(original_index), content}; scores map back by that id.

# Vertex caps a record's content at ~1024 tokens; truncate to a safe char budget
# (~3 chars/token) before sending so an over-long passage can't 400 the batch.
_VERTEX_CONTENT_CHARS = 1024 * 3


def _vertex_token_and_project(settings: dict) -> "tuple[str, str] | tuple[None, None]":
    """Mint a (bearer token, project id) for the Vertex Ranking API from the
    ambient GCP service-account creds. google.auth.default() → (creds, project);
    refresh the creds to get an access token. Project prefers a VERTEX_PROJECT_ID
    env override, else the SA JSON's project. Returns (None, None) on ANY auth
    failure (no creds, refresh error) or a missing token/project — so the caller
    degrades to None and NEVER raises (audit A9). google.auth is imported lazily
    (it is a transitive dep — keep the module import surface minimal + robust)."""
    try:
        import google.auth
        import google.auth.transport.requests as _ga_transport
        creds, default_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(_ga_transport.Request())
        token = creds.token
    except Exception:  # noqa: BLE001 - absent creds / refresh failure → inert
        return None, None
    if not token:
        return None, None
    project = os.getenv("VERTEX_PROJECT_ID") or default_project
    if not project:
        return None, None
    return token, project


def _scatter_vertex_records(payload: "dict | None",
                            n: int) -> "list[float] | None":
    """Scatter Vertex's {records:[{id, score}]} back onto passage positions by
    the str(original_index) id we sent (init None, verify no gap). None on ANY
    count/id/shape anomaly — a missing, duplicate, or out-of-range id can't rank
    on one scale, so the retriever falls through to its un-reranked ranking."""
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or len(records) != n:
        return None
    out: list[float | None] = [None] * n
    for item in records:
        try:
            idx = int(item["id"])
            sc = float(item["score"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= idx < n):          # out of range → None (never IndexError)
            return None
        out[idx] = sc
    if any(v is None for v in out):     # a duplicate/missing id left a gap
        return None
    return out  # type: ignore[return-value]


def _score_vertex(query: str, passages: list[str],
                  settings: dict) -> list[float] | None:
    """Vertex AI semantic-ranker (dedicated cross-encoder) via the Discovery
    Engine Ranking API. Mints an OAuth token + resolves the project from the
    ambient GCP service account, POSTs the query + all passages as records (each
    content truncated to the ~1024-token limit), and scatters {id, score} back
    onto passage positions. None on any auth/project/HTTP/count/id anomaly
    (never raises — the dispatcher backstops transport blow-ups too)."""
    if not passages:
        return None
    token, project = _vertex_token_and_project(settings)
    if not token or not project:
        return None
    records = [{"id": str(i), "content": (p or "")[:_VERTEX_CONTENT_CHARS]}
               for i, p in enumerate(passages)]
    resp = requests.post(
        VERTEX_RANK_URL.format(project=project),
        headers={"Authorization": f"Bearer {token}",
                 "X-Goog-User-Project": project,
                 "content-type": "application/json"},
        json={"model": settings["model_id"], "query": query,
              "records": records},
        timeout=settings["timeout_s"],
    )
    if resp.status_code != 200:
        return None
    return _scatter_vertex_records(resp.json(), len(passages))


# ── LLM-as-reranker (M6) ──────────────────────────────────────────────────────
# The cheap cloud fallback tier: reuse a frontier CHAT key the box already holds
# to run a LISTWISE rank in ONE non-streaming completion. NOT a purpose-trained
# cross-encoder — an honest budget/keyless fallback (RERANK_MODELS quality_note).
# Each passage is truncated to _LLM_SNIPPET_CHARS before prompting — the single
# biggest latency/cost lever. The 4 provider request-shapes are small per-key
# helpers dispatched by key_env; each makes exactly ONE requests.post and returns
# the completion text or None (never raises), mirroring the box's existing
# frontier call styles (web_tools.py / config.py URLs).

_LLM_SNIPPET_CHARS = 512


def _build_llm_rank_prompt(query: str, passages: list[str]) -> str:
    """Listwise rank prompt: the query + a numbered list of snippet-truncated
    passages, asking for a JSON permutation of the indices most→least relevant.
    Deliberately carries NO Qwen instruct prefix (llm entries declare none — it
    inverts non-Qwen rankers)."""
    numbered = "\n".join(
        f"[{i}] {(p or '')[:_LLM_SNIPPET_CHARS]}"
        for i, p in enumerate(passages))
    return (
        "You are a search-result re-ranker. Given a QUERY and a numbered list of "
        "PASSAGES, order the passage numbers from MOST to LEAST relevant to the "
        "query. Respond with ONLY a JSON object of the form "
        '{"ranking": [<passage numbers>]} — a permutation of every passage number '
        "(0-based), each appearing exactly once. No prose, no markdown.\n\n"
        f"QUERY: {query}\n\nPASSAGES:\n{numbered}"
    )


def _parse_llm_ranking(text: "str | None", n: int) -> "list[int] | None":
    """Defensively extract a length-n permutation of 0..n-1 from a model's reply.
    Any deviation → None (contract): not-JSON / wrong length / duplicate /
    out-of-range / missing / non-int index. Tolerates a bare array, an object
    wrapping the list under a relevance key, and a fenced/prose-wrapped array."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t.startswith("```"):                      # strip a ```json … ``` fence
        t = t.strip("`")
        nl = t.find("\n")
        if nl != -1 and t[:nl].strip().lower() in ("json", ""):
            t = t[nl + 1:]
        t = t.strip()
    obj = None
    try:
        obj = json.loads(t)
    except Exception:  # noqa: BLE001 - fall back to a substring array scan
        m = re.search(r"\[[\s\d,]*\]", t)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(obj, dict):                    # unwrap {"ranking": [...]}
        cand = None
        for k in ("ranking", "order", "indices", "ranked", "result"):
            v = obj.get(k)
            if isinstance(v, list):
                cand = v
                break
        if cand is None:
            for v in obj.values():
                if isinstance(v, list):
                    cand = v
                    break
        obj = cand
    if not isinstance(obj, list) or len(obj) != n:
        return None
    # strict ints only (reject bool/float/str — a permutation is integer indices)
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in obj):
        return None
    if sorted(obj) != list(range(n)):            # duplicate / out-of-range / gap
        return None
    return obj


def _llm_complete_gemini(model_id: str, api_key: str, prompt: str,
                         timeout_s: float) -> "str | None":
    """One Gemini generateContent call, JSON response mode. x-goog-api-key header
    (key never in the URL). Returns the concatenated text parts or None."""
    resp = requests.post(
        f"{GEMINI_BASE_URL}/{model_id}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"response_mime_type": "application/json"}},
        timeout=timeout_s,
    )
    if resp.status_code >= 400:
        return None
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _llm_complete_openai(model_id: str, api_key: str, prompt: str,
                         timeout_s: float) -> "str | None":
    """One OpenAI /v1/chat/completions call, json_object response format."""
    resp = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": model_id,
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_object"}},
        timeout=timeout_s,
    )
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _llm_complete_anthropic(model_id: str, api_key: str, prompt: str,
                            timeout_s: float) -> "str | None":
    """One Anthropic /v1/messages call. No native JSON mode → a system line
    reinforces the JSON-only instruction; parsing stays defensive regardless."""
    resp = requests.post(
        ANTHROPIC_URL,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model_id, "max_tokens": 1024,
              "system": "Respond with ONLY the requested JSON object. "
                        "No prose, no markdown.",
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout_s,
    )
    if resp.status_code >= 400:
        return None
    try:
        blocks = resp.json()["content"]
        return "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _llm_complete_xai(model_id: str, api_key: str, prompt: str,
                      timeout_s: float) -> "str | None":
    """One xAI /v1/chat/completions call (OpenAI-compatible), json_object mode."""
    resp = requests.post(
        XAI_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": model_id,
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_object"}},
        timeout=timeout_s,
    )
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# key_env → the single-completion helper for that frontier family.
_LLM_COMPLETERS = {
    "GOOGLE_API_KEY": _llm_complete_gemini,
    "GEMINI_API_KEY": _llm_complete_gemini,
    "OPENAI_API_KEY": _llm_complete_openai,
    "ANTHROPIC_API_KEY": _llm_complete_anthropic,
    "XAI_API_KEY": _llm_complete_xai,
}


def _score_llm(query: str, passages: list[str],
               settings: dict) -> list[float] | None:
    """Listwise LLM-as-reranker (M6) — the cheap cloud fallback tier.

    Resolves the frontier family from the selected model's key_env, reads the key
    FRESH via os.getenv (M4), builds a snippet-truncated listwise prompt, and runs
    ONE non-streaming completion asking for a JSON permutation of the candidate
    indices. The returned order maps to synthetic descending scores (1/(1+rank))
    positionally aligned to `passages`. This is NOT a purpose-trained
    cross-encoder — but it needs no key beyond a frontier chat key already held.

    None on: empty passages, missing key, unknown provider family, HTTP/transport
    failure, or a reply that is not a clean length-N permutation. Never raises
    (the dispatcher backstops too, but each helper is self-defended)."""
    if not passages:
        return None
    key_env = settings.get("key_env")
    if not key_env:
        return None
    api_key = os.getenv(key_env)
    if not api_key:
        return None
    complete = _LLM_COMPLETERS.get(key_env)
    if complete is None:
        return None
    prompt = _build_llm_rank_prompt(query, passages)
    text = complete(settings["model_id"], api_key, prompt, settings["timeout_s"])
    order = _parse_llm_ranking(text, len(passages))
    if order is None:
        return None
    out = [0.0] * len(passages)
    for rank, passage_idx in enumerate(order):
        out[passage_idx] = 1.0 / (1.0 + rank)
    return out


def score(query: str, passages: list[str]) -> list[float] | None:
    """Cross-encoder scores for (query, passage) pairs — None on ANY failure.

    Provider dispatcher (M2): resolves [rerank] once, routes to the selected
    provider's _score_<provider> helper, and NEVER raises — any helper
    exception is swallowed to None so a dead reranker only costs latency (the
    retriever falls through to its un-reranked ranking). The returned list is
    positionally aligned with `passages`.

    Every real call is EVIDENCE (the capability signal the on-box providers are
    gated on, replacing a synthetic probe): a success records state ok with its
    measured latency and batch shape, and every None from a SELECTED provider is
    recorded and logged as a fallthrough (_note_fallthrough), which records state
    failed with the reason. The degradation is never silent, and a scoring
    failure also reopens a cached OK probe (cpu/cloud) rather than keeping the
    reranker nominally "available".
    """
    if not passages:
        return None
    s = get_settings()
    p = s["provider"]
    if p == "null":
        return None
    # Batch shape for whichever evidence this call produces — an inner helper's
    # more specific reason (llama.cpp's oversized-batch 500) keeps its numbers.
    _score_ctx.batch = (len(passages), sum(len(x or "") for x in passages))
    fn = {"vllm": _score_vllm, "cpu": _score_cpu, "voyage": _score_voyage,
          "cohere": _score_cohere, "vertex": _score_vertex,
          "llm": _score_llm, "localstack": _score_localstack_sync}.get(p)
    if fn is None:
        _note_fallthrough(f"no scorer for provider {p!r}", s)
        return None
    _score_ctx.noted = False
    t0 = time.monotonic()
    try:
        out = fn(query, passages, s)
    except Exception as exc:  # noqa: BLE001 - never-raise contract (audit A9)
        _note_fallthrough(f"{type(exc).__name__} from the {p} scorer", s)
        return None
    if out is None or len(out) != len(passages):
        if not getattr(_score_ctx, "noted", False):
            _note_fallthrough("scorer returned no usable scores", s)
        return None
    _record_evidence("ok", s, latency_ms=(time.monotonic() - t0) * 1000.0)
    return out


def _skipped_state(s: dict) -> dict:
    """The unconfigured result — NOT cached anywhere: config can change."""
    return {"state": "skipped", "latency_ms": None, "measured_ms": None,
            "ceiling_ms": s["preflight_ceiling_ms"],
            "passage_n": s["preflight_passage_n"],
            "passage_chars": s["passage_chars"], "at": None,
            "source": "config", "reason": "no reranker provider configured"}


def _evidence_capability(s: dict) -> dict:
    """Capability for an _EVIDENCE_PROVIDERS provider, in preflight()'s shape,
    read from real traffic — no probe, no I/O, no lock contention (§5.2 aside).

    `unknown` keeps the gate OPEN on purpose: the first real search is the probe,
    and its outcome lands here. ceiling_ms is None because nothing is timed
    against a ceiling on this path; latency_ms is the LAST REAL rerank's wall
    time, dropped once the evidence goes stale so the wizard can't show a
    number for a state it no longer stands behind.

    Evidence earned by a DIFFERENT provider/model reads as unknown: a selector
    change calls reset_preflight(), but a hand-edited config.ini does not, and
    the old selection's outcome says nothing about the new one."""
    ev = evidence_state()
    mismatch = (ev["state"] != "unknown"
                and (ev["provider"] != s.get("provider")
                     or ev["model"] != s.get("model")))
    state = "unknown" if mismatch else ev["state"]
    fresh = state != "unknown"
    if mismatch:
        reason = ("evidence belongs to a different reranker selection — the "
                  "next search re-proves this one")
    elif state == "failed":
        reason = ev["reason"]
    elif state == "ok":
        reason = None
    elif ev["at"] is None:
        reason = "no real scoring evidence yet — the next search is the probe"
    else:
        reason = ("last real scoring evidence aged out — the next search "
                  "re-proves it")
    return {"state": state,
            "latency_ms": ev["latency_ms"] if fresh else None,
            "measured_ms": ev["latency_ms"] if fresh else None,
            "ceiling_ms": None,
            "passage_n": ev["passage_n"] if fresh else None,
            "passage_chars": ev["passage_chars"] if fresh else None,
            "at": None if mismatch else ev["at"],
            "source": "real-traffic", "reason": reason}


def preflight(settings: "dict | None" = None) -> dict:
    """Capability state for the configured provider — TWO regimes (module doc).

    _EVIDENCE_PROVIDERS (localstack/vllm): NO probe. The state is the record of
    real scoring outcomes (_evidence_capability): ok / failed / unknown, where
    unknown is honest and still attempts. Nothing here scores a synthetic batch,
    so a restart no longer costs a ~16s rerank and polling /rerank/status is
    free.

    cpu / cloud / llm: the cheap once-per-process probe, unchanged — scores
    `preflight_passage_n` short dummy passages and requires wall latency under
    the resolved ceiling. For `cpu` the measured latency is EXTRAPOLATED to the
    real candidate count (measured_ms × rerank_candidate_n / preflight_passage_n)
    before the compare — a CPU cross-encoder scales ~linearly with passage
    count. Cloud providers batch, so passage_n stays 1 and no extrapolation
    applies. States: skipped (no provider configured; NOT cached), ok (cached),
    failed (cached: process lifetime, or _PREFLIGHT_FAIL_TTL_S for cloud).

    `settings` lets a caller that already resolved config (available(), status())
    pass it in rather than paying a second sidecar+config read per search.
    """
    global _preflight_result, _preflight_expiry
    s = settings or get_settings()
    provider = s["provider"]
    if provider in _EVIDENCE_PROVIDERS:
        if not _configured(s):
            return _skipped_state(s)
        # §5.2 hard rule (Milestone 4): the legacy vLLM reranker (:8091) and the
        # on-box localstack retrieval group CANNOT co-run — vLLM pre-allocates
        # ~90% of VRAM via gpu_memory_utilization and is invisible to llama-swap's
        # budgeting, so both resident → guaranteed OOM. Refuse to activate while
        # it answers (a LIVENESS read, TTL-cached — stopping the service resolves
        # it live, no restart).
        if provider == "localstack" and _vllm_corun_conflict():
            return {"state": "failed", "latency_ms": None, "measured_ms": None,
                    "ceiling_ms": None, "passage_n": None,
                    "passage_chars": None, "at": None, "source": "co-run-check",
                    "reason": ("vLLM reranker still answering on :8091 — stop "
                               "vllm-reranker.service before enabling the on-box "
                               "retrieval group (both pre-allocate VRAM → OOM)")}
        return _evidence_capability(s)
    res = _preflight_result
    if res is not None and (
            _preflight_expiry is None or time.monotonic() < _preflight_expiry):
        return res
    with _preflight_lock:
        res = _preflight_result
        if res is not None and (
                _preflight_expiry is None
                or time.monotonic() < _preflight_expiry):
            return res
        passage_n = s["preflight_passage_n"]
        passage_chars = len(_PROBE_PASSAGE)
        ceiling = s["preflight_ceiling_ms"]
        if not _configured(s):
            # Not a probe failure — do not burn the process-lifetime cache.
            return _skipped_state(s)
        passages = [_PROBE_PASSAGE] * max(1, passage_n)
        # Probe failures are the preflight's own result, not retrieve()-time
        # degradations — in_preflight keeps them out of the fallthrough record
        # AND out of the capability evidence (a probe is not real traffic).
        _score_ctx.in_preflight = True
        try:
            t0 = time.monotonic()
            got = score("preflight probe", passages)
            measured_ms = (time.monotonic() - t0) * 1000.0
        finally:
            _score_ctx.in_preflight = False
        # CPU scales ~linearly with candidate count; extrapolate the probe to
        # the real rerank_candidate_n. Batched providers compare the raw wall.
        if provider == "cpu" and passage_n > 0:
            estimated_ms = measured_ms * (s["rerank_candidate_n"] / passage_n)
        else:
            estimated_ms = measured_ms
        base = {"latency_ms": round(estimated_ms, 1),
                "measured_ms": round(measured_ms, 1),
                "ceiling_ms": ceiling, "passage_n": passage_n,
                "passage_chars": passage_chars, "at": time.time(),
                "source": "probe"}
        if got is None:
            result = {**base, "state": "failed",
                      "reason": "provider scoring failed"}
        elif estimated_ms > ceiling:
            if provider == "cpu":
                reason = (f"estimated {estimated_ms:.0f}ms (measured "
                          f"{measured_ms:.0f}ms × {s['rerank_candidate_n']}/"
                          f"{passage_n}) over the {ceiling:.0f}ms ceiling")
            else:
                reason = (f"probe latency {estimated_ms:.0f}ms over the "
                          f"{ceiling:.0f}ms ceiling")
            result = {**base, "state": "failed", "reason": reason}
        else:
            result = {**base, "state": "ok", "reason": None}
        # Cache policy (M3.2): a CLOUD provider's FAILED probe recovers after a
        # TTL rather than sticking for the process lifetime (a transient blip
        # must not disable rerank until restart — the documented deviation from
        # audit A9's local-only assumption). The on-box providers used to share
        # this policy; they are evidence-gated now, and their recovery is the
        # evidence TTL instead. The in-process cpu provider keeps the sticky
        # process-lifetime failure (a missing dep does not heal itself).
        if result["state"] == "failed" and provider in CLOUD_PROVIDERS:
            _preflight_expiry = time.monotonic() + _PREFLIGHT_FAIL_TTL_S
            disabled = (f" — rerank disabled for {int(_PREFLIGHT_FAIL_TTL_S)}s"
                        f" (TTL, then re-probes)")
        else:
            _preflight_expiry = None
            disabled = (" — rerank disabled for process lifetime"
                        if result["state"] == "failed" else "")
        _preflight_result = result
        print(f"[RERANK] preflight {result['state']}"
              f" ({result['reason'] or f'{estimated_ms:.0f}ms'});"
              f" provider={provider} model={s['model']}{disabled}")
        return result


def reset_preflight() -> None:
    """Clear every cached judgement about the reranker — the probe cache
    (result + TTL deadline), the CAPABILITY EVIDENCE, the reachability + §5.2
    co-run caches, the in-process CPU CrossEncoder cache (M5), AND the
    un-reranked fallthrough record. The M8 selector calls this on a provider
    change: the previous provider's evidence and failures say NOTHING about the
    new one, so the new selection starts at `unknown` and the next real search
    proves it. Tests use it for isolation."""
    global _preflight_result, _preflight_expiry, _reach_cache
    global _evidence_deadline, _corun_cache
    with _preflight_lock:
        _preflight_result = None
        _preflight_expiry = None
    with _evidence_lock:
        _evidence.update(state="unknown", at=None, reason=None, provider=None,
                         model=None, passage_n=None, passage_chars=None,
                         latency_ms=None)
        _evidence_deadline = None
    with _reach_lock:
        _reach_cache = None
    with _corun_lock:
        _corun_cache = None
    with _cpu_model_lock:
        _cpu_model_cache.clear()
    with _fallthrough_lock:
        _fallthrough.update(count=0, reason=None, provider=None, model=None,
                            at=None)


def available() -> bool:
    """The retrieve()-time gate: is a rerank worth ATTEMPTING right now?

    Provider configured AND the capability state is not `failed`. `unknown` is
    deliberately open — on the evidence path a cold start has nothing to go on
    and the first real search IS the probe (a failure then closes this gate for
    _PREFLIGHT_FAIL_TTL_S, loudly logged, so the user pays it once). For the
    probing providers `unknown` never occurs, so this stays their old
    probe-passed gate. [retrieval] rerank_enabled is checked by the caller
    first, so the null-provider default costs one config read and probes
    nothing.
    """
    s = get_settings()
    if not _configured(s):
        return False
    return preflight(s).get("state") in ("ok", "unknown")


# key_env → the frontier endpoint the LLM-as-reranker path actually posts to.
_LLM_SCORING_URLS = {
    "GOOGLE_API_KEY": GEMINI_BASE_URL,
    "GEMINI_API_KEY": GEMINI_BASE_URL,
    "OPENAI_API_KEY": OPENAI_URL,
    "ANTHROPIC_API_KEY": ANTHROPIC_URL,
    "XAI_API_KEY": XAI_URL,
}


def scoring_base_url(settings: "dict | None" = None) -> "str | None":
    """The URL the SELECTED provider's scorer actually posts to — None when
    there is no URL (cpu is in-process; null/unknown score nothing).

    status()'s `base_url` is the [rerank] seam (default: the legacy vLLM :8091
    port) and is NOT where the on-box provider scores: localstack resolves the
    llama-swap front door via local_stack.base_url(). Reporting only the seam
    made /rerank/status describe a service the scorer never touches. Never
    raises."""
    s = settings or get_settings()
    p = s.get("provider")
    if p == "localstack":
        base = _localstack_base_url()
        return base.rstrip("/") + "/rerank" if base else None
    if p == "vllm":
        base = s.get("base_url")
        return base.rstrip("/") + "/score" if base else None
    if p == "voyage":
        return VOYAGE_RERANK_URL
    if p == "cohere":
        return COHERE_RERANK_URL
    if p == "vertex":
        return VERTEX_RANK_URL
    if p == "llm":
        return _LLM_SCORING_URLS.get(s.get("key_env"))
    return None


def scoring_reachable(settings: "dict | None" = None) -> bool:
    """LIVENESS of the path scoring_base_url() names — "can I reach it", NEVER
    "does reranking work" (that is capability: preflight()/evidence_state()).
    Deliberately kept cheap and separate: provider-aware, never a paid poll —
    the same resolution score() uses, so status cannot report a dead reranker
    that scores fine (or a live one that 500s every search) the way the
    [rerank]-seam `service_reachable` did. False for null/unknown: nothing
    scores."""
    s = settings or get_settings()
    p = s.get("provider")
    if p == "null" or p not in KNOWN_PROVIDERS:
        return False
    return reachable(s)


def model_catalog() -> list[dict]:
    """Per-model selector metadata for the M10 wizard/Portal reranker selector.

    The flat `models` slug list in status() carries no per-model provider/tiers/
    key_present, so a tier-driven, key-gated selector can't be built from it.
    This exposes exactly what the selector needs, additively. `key_present` is
    resolved FRESH via os.getenv(key_env) per model (M4 pattern) so a wizard-
    mirrored paste gates selectability with no restart. Sorted by slug to match
    `models`. Never raises."""
    out = []
    for slug in sorted(RERANK_MODELS):
        e = RERANK_MODELS[slug]
        key_env = e.get("key_env")
        # A4: `built` — the reranker UI card's truth signal, replacing the
        # gpu-and-service_reachable proxy. True ONLY for the localstack on-box
        # reranker when its self-converted GGUF is on disk; False/N-A for cloud/
        # cpu/vllm models (they have no on-box GGUF to build).
        built = (
            e.get("provider") == "localstack"
            and _member_gguf_present(e.get("model_id", "rerank-qwen3-8b"))
        )
        out.append({
            "slug": slug,
            "provider": e.get("provider"),
            "label": e.get("label", slug),
            "tiers": list(e.get("tiers", [])),
            "privacy": e.get("privacy"),
            "built": built,
            "auth_kind": e.get("auth_kind", "none"),
            "key_env": key_env,
            # Cloud bearer models: present iff their key_env resolves. Vertex
            # (gcp_service_account, key_env=None) resolves from the uploaded SA
            # file at GOOGLE_APPLICATION_CREDENTIALS so it's selectable once set.
            "key_present": _key_present_for(e.get("auth_kind", "none"), key_env),
            "cost_note": e.get("cost_note", ""),
            "quality_note": e.get("quality_note", ""),
        })
    return out


def _tier_guidance(tier: "str | None") -> str:
    """A short, honest 'which reranker should I pick?' line for the selector,
    adapted to the box's hardware tier (Brandon 2026-07-05). Free-first framing
    for a personal-use box: on capable hardware the LOCAL reranker is free,
    private, and unlimited — the best pick; LOW boxes lean on cloud, where the
    free tiers cover personal use and a production key is only for heavy use."""
    if tier == "HIGH":
        return ("This box has a GPU — the local reranker is free, private, and "
                "unlimited (nothing leaves your box). That's the recommended "
                "pick; the cloud options work too.")
    if tier == "MID":
        return ("This box can run the local CPU reranker — free, private, and "
                "unlimited (a bit slower than cloud). Or pick a cloud reranker "
                "below.")
    # LOW (or unknown hardware): cloud-only.
    return ("This box uses a cloud reranker. Cohere's free trial (~1,000 "
            "searches/mo) is fine for personal use, or reuse your existing chat "
            "model's key with no separate quota. Add a production key only for "
            "heavy or commercial use.")


def status() -> dict:
    """/rerank/status payload (ADDITIVE ops contract, /embeddings/status
    style). Safe to poll, and CHEAP for the on-box providers: their capability
    is read from real traffic, so a status poll can no longer trigger a ~16s
    synthetic rerank. Only the probing providers (cpu/cloud/llm) still run the
    one-time probe here, and only when a provider is configured.

    `preflight.state` is the honest capability state — ok / failed / unknown /
    skipped — and `evidence` carries the real-traffic record behind it,
    including WHEN it was earned (`at`). `available` is the GATE (will a rerank
    be attempted), which is deliberately open on `unknown`: never read it as
    "proven healthy" — read the state for that.

    M13 additive keys for the wizard's Memory & Search reranker block:
      gpu               — host has a usable NVIDIA GPU (hardware.probe(),
                          60s-cached; the reranker is GPU-only hardware-wise)
      service_reachable — something answers on base_url (TTL-cached probe;
                          distinguishes "run the installer's reranker step"
                          from "flip the config" on a GPU box)

    M3.3 additive keys (tiered selector) — ALL keys stay additive so old
    frontends + the wizard's current bind keep working:
      tier              — hardware tier "LOW"/"MID"/"HIGH" (hardware.probe())
      ram_mb            — system RAM (hardware.probe())
      reachable         — provider-aware reachability (localhost probe for
                          vllm, deps for cpu, key-present for cloud — no poll)
      auth_kind         — the selected model's auth descriptor (M2)
      key_present       — the model's key_env resolves to a non-empty env value
      preflight_ceiling_ms — the resolved (per-provider) latency ceiling
    """
    s = get_settings()
    configured = _configured(s)
    if configured:
        pf = preflight(s)
    else:
        pf = _preflight_result or _skipped_state(s)
    hw = hardware.probe()
    return {
        # Track the ACTUAL retrieve() gate (M8): is_enabled() resolves
        # sidecar > config, so the wizard/status card can't show "disabled"
        # while a sidecar selection has rerank ON. Backward-compatible — no
        # sidecar falls back to [retrieval] rerank_enabled, same as before.
        "enabled": is_enabled(),
        "gpu": bool(hw.get("gpu")),
        "service_reachable": service_reachable(),
        # A4 additive: the on-box reranker's GGUF is physically on disk (the BUILT
        # truth the reranker card keys on, not the gpu-and-service_reachable proxy).
        "member_gguf_present": _member_gguf_present("rerank-qwen3-8b"),
        "provider": s["provider"],
        "model": s["model"],
        "model_id": s["model_id"],
        "base_url": s["base_url"] or None,
        # The URL scoring ACTUALLY posts to + a reachability read off THAT same
        # path, so the payload can no longer contradict what score() does (the
        # legacy `base_url`/`service_reachable` pair still describes the
        # [rerank] :8091 seam and stays for the wizard's GPU-box detection).
        "scoring_base_url": scoring_base_url(s),
        "scoring_reachable": scoring_reachable(s),
        "configured": configured,
        # The honest capability state (ok/failed/unknown/skipped). Key name kept
        # for the wizard/Portal bind; for the on-box providers it is no longer a
        # probe result but the real-traffic record (`source`: real-traffic).
        "preflight": pf,
        # The GATE, not a health claim: `unknown` (no recent evidence) is
        # deliberately attempted, so available:true + state:unknown means "we
        # will try", NOT "proven working".
        "available": configured and pf.get("state") in ("ok", "unknown"),
        # The real-traffic capability record itself: state + when it was earned
        # (`at`) + the batch/latency it was earned on + staleness.
        "evidence": evidence_state(),
        # Running un-reranked degradation record (count/reason/provider/at) —
        # the retrieve()-time truth a once-per-process log line used to hide.
        "fallthrough": fallthrough_state(),
        # candidate_n resolves via get_settings' resilient read.
        "candidate_n": s["rerank_candidate_n"],
        "passage_chars": s["passage_chars"],
        "models": sorted(RERANK_MODELS.keys()),
        # M10.1 additive: per-model selector metadata (provider/tiers/key_present/
        # notes) the wizard/Portal reranker selector needs — `models` stays the
        # flat slug list for backward compatibility.
        "model_catalog": model_catalog(),
        # ── M3.3 additive (tier/ram + per-provider auth & reachability) ──
        "tier": hw.get("tier"),
        # Honest, tier-aware "which should I pick?" line for the selector.
        "tier_guidance": _tier_guidance(hw.get("tier")),
        "ram_mb": hw.get("ram_mb"),
        "reachable": reachable(s),
        "auth_kind": s["auth_kind"],
        # Single fresh os.getenv read, resolved in get_settings (M4).
        "key_present": s["key_present"],
        "preflight_ceiling_ms": s["preflight_ceiling_ms"],
    }
