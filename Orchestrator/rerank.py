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

Latency preflight (audit A9 gating leg 3): retrieve()-time probing is too hot,
so preflight() runs ONCE per process on first use — it scores a 1-passage dummy
and requires wall latency under [rerank] preflight_ceiling_ms (fallback 500).
A failed probe (error OR over-ceiling) disables rerank for the PROCESS LIFETIME
(cached; logged once). GET /rerank/status surfaces the result.

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
        "cost_note": "Local GPU — no API cost",
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
        "cost_note": "Local GPU — no API cost",
        "privacy": "local",
        "tiers": ["HIGH"],
        "preflight_ceiling_ms": 500,
        "preflight_passage_n": 1,
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
        "cost_note": "Local CPU — no API cost; slower than GPU",
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
        "cost_note": "Uses your existing Google/Gemini key; ~cents/query",
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
        "cost_note": "Uses your existing OpenAI key; ~cents/query",
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
        "cost_note": "Uses your existing Anthropic key; ~cents/query",
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
        "cost_note": "Cohere API — ~$2/1K searches",
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
# voyage/cohere/vertex (M7) are stubbed to return None until then.
KNOWN_PROVIDERS = {"null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm"}

# Cloud providers whose FAILED preflight is TTL-recoverable (M3.2): a transient
# cloud blip must NOT disable rerank until the next restart. Deliberate,
# documented deviation from audit A9's local-only once-per-process assumption —
# the local vllm/cpu providers keep the process-lifetime failure cache.
CLOUD_PROVIDERS = {"voyage", "cohere", "vertex", "llm"}


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
        # Config override wins over the model entry's registry ceiling.
        "preflight_ceiling_ms": _cfg_float(
            "rerank", "preflight_ceiling_ms", ceiling_fallback),
        "preflight_passage_n": passage_n,
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
        "https://api.voyageai.com/v1/rerank",
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
        "https://api.cohere.ai/v2/rerank",
        headers={"Authorization": f"Bearer {api_key}",
                 "content-type": "application/json"},
        json={"model": settings["model_id"], "query": query,
              "documents": list(passages), "top_n": len(passages)},
        timeout=settings["timeout_s"],
    )
    if resp.status_code != 200:
        return None
    return _scatter_relevance_scores(resp.json(), len(passages))


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
        f"https://discoveryengine.googleapis.com/v1/projects/{project}"
        f"/locations/global/rankingConfigs/default_ranking_config:rank",
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
# frontier call styles (web_tools.py / sms_processor.py / config.py URLs).

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
    """
    if not passages:
        return None
    s = get_settings()
    p = s["provider"]
    if p == "null":
        return None
    fn = {"vllm": _score_vllm, "cpu": _score_cpu, "voyage": _score_voyage,
          "cohere": _score_cohere, "vertex": _score_vertex,
          "llm": _score_llm}.get(p)
    if fn is None:
        return None
    try:
        return fn(query, passages, s)
    except Exception:  # noqa: BLE001 - never-raise contract (audit A9)
        return None


def preflight() -> dict:
    """One-time-per-process latency probe; result cached for process lifetime.

    Scores `preflight_passage_n` dummy passages (M3.1: a realistic batch, not
    1) against the configured provider and requires wall latency under the
    resolved ceiling. For the `cpu` provider the measured latency is
    EXTRAPOLATED to the real candidate count
    (measured_ms × rerank_candidate_n / preflight_passage_n) before the
    compare — a CPU cross-encoder scales ~linearly with passage count, so an
    8-passage probe under-represents a 40-candidate rerank. vLLM/cloud batch,
    so their passage_n stays 1 and no extrapolation applies. States:
      skipped — no provider configured (NOT cached: config can change);
      ok      — probe scored under the ceiling (cached);
      failed  — provider error or over-ceiling (cached: rerank disabled for
                the process lifetime; a restart re-probes).
    """
    global _preflight_result, _preflight_expiry
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
        s = get_settings()
        ceiling = s["preflight_ceiling_ms"]
        provider = s["provider"]
        passage_n = s["preflight_passage_n"]
        if not _configured(s):
            # Not a probe failure — do not burn the process-lifetime cache.
            return {"state": "skipped", "latency_ms": None,
                    "measured_ms": None, "ceiling_ms": ceiling,
                    "passage_n": passage_n,
                    "reason": "no reranker provider configured"}
        passages = ["preflight probe passage"] * max(1, passage_n)
        t0 = time.monotonic()
        got = score("preflight probe", passages)
        measured_ms = (time.monotonic() - t0) * 1000.0
        # CPU scales ~linearly with candidate count; extrapolate the probe to
        # the real rerank_candidate_n. Batched providers compare the raw wall.
        if provider == "cpu" and passage_n > 0:
            estimated_ms = measured_ms * (s["rerank_candidate_n"] / passage_n)
        else:
            estimated_ms = measured_ms
        base = {"latency_ms": round(estimated_ms, 1),
                "measured_ms": round(measured_ms, 1),
                "ceiling_ms": ceiling, "passage_n": passage_n}
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
        # Cache policy (M3.2): a cloud FAILED preflight recovers after a TTL (a
        # transient blip must not disable rerank until restart — the documented
        # deviation from audit A9's local-only assumption). Local (vllm/cpu)
        # failures and every OK stick for the process lifetime.
        if result["state"] == "failed" and provider in CLOUD_PROVIDERS:
            _preflight_expiry = time.monotonic() + _PREFLIGHT_FAIL_TTL_S
            disabled = (f" — rerank disabled for {int(_PREFLIGHT_FAIL_TTL_S)}s"
                        f" (cloud TTL, then re-probes)")
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
    """Clear the probe caches — preflight (result + TTL deadline), reachability,
    AND the in-process CPU CrossEncoder cache (M5). The M8 selector calls this on
    a provider change so the next preflight() re-probes the new provider (and a
    model switch re-loads the CPU model); tests use it for isolation."""
    global _preflight_result, _preflight_expiry, _reach_cache
    with _preflight_lock:
        _preflight_result = None
        _preflight_expiry = None
    with _reach_lock:
        _reach_cache = None
    with _cpu_model_lock:
        _cpu_model_cache.clear()


def available() -> bool:
    """Provider configured AND the one-time latency preflight passed.

    This is the retrieve()-time gate (with [retrieval] rerank_enabled checked
    by the caller first, so the null-provider default costs one config read
    and never probes anything).
    """
    if not _configured():
        return False
    return preflight().get("state") == "ok"


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
        out.append({
            "slug": slug,
            "provider": e.get("provider"),
            "label": e.get("label", slug),
            "tiers": list(e.get("tiers", [])),
            "privacy": e.get("privacy"),
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


def status() -> dict:
    """/rerank/status payload (ADDITIVE ops contract, /embeddings/status
    style). Triggers the one-time preflight only when a provider is
    configured — with the null provider the rest is config reads plus the
    TTL-cached ~1s-capped reachability probe; safe to poll.

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
        pf = preflight()
    else:
        pf = _preflight_result or {
            "state": "skipped", "latency_ms": None, "measured_ms": None,
            "ceiling_ms": s["preflight_ceiling_ms"],
            "passage_n": s["preflight_passage_n"],
            "reason": "no reranker provider configured",
        }
    hw = hardware.probe()
    return {
        # Track the ACTUAL retrieve() gate (M8): is_enabled() resolves
        # sidecar > config, so the wizard/status card can't show "disabled"
        # while a sidecar selection has rerank ON. Backward-compatible — no
        # sidecar falls back to [retrieval] rerank_enabled, same as before.
        "enabled": is_enabled(),
        "gpu": bool(hw.get("gpu")),
        "service_reachable": service_reachable(),
        "provider": s["provider"],
        "model": s["model"],
        "model_id": s["model_id"],
        "base_url": s["base_url"] or None,
        "configured": configured,
        "preflight": pf,
        "available": configured and pf.get("state") == "ok",
        # candidate_n resolves via get_settings' resilient read.
        "candidate_n": s["rerank_candidate_n"],
        "passage_chars": _cfg_int("rerank", "passage_chars", 4096),
        "models": sorted(RERANK_MODELS.keys()),
        # M10.1 additive: per-model selector metadata (provider/tiers/key_present/
        # notes) the wizard/Portal reranker selector needs — `models` stays the
        # flat slug list for backward compatibility.
        "model_catalog": model_catalog(),
        # ── M3.3 additive (tier/ram + per-provider auth & reachability) ──
        "tier": hw.get("tier"),
        "ram_mb": hw.get("ram_mb"),
        "reachable": reachable(s),
        "auth_kind": s["auth_kind"],
        # Single fresh os.getenv read, resolved in get_settings (M4).
        "key_present": s["key_present"],
        "preflight_ceiling_ms": s["preflight_ceiling_ms"],
    }
