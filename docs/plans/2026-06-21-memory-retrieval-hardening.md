# Memory Retrieval Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make BlackBox snapshot-memory retrieval production-grade and *model-agnostic* — one canonical, recency-aware, diversity-aware retriever used by every surface, with per-model similarity thresholds calibrated so recall never silently degrades when you switch embedding models.

**Architecture:** Today three divergent rankers sit over one active vector store (chat/voice use a weighted keyword+semantic `hybrid_retrieve`; MCP uses pure semantic via `/fossil/hybrid`; per-turn context fuses 4 channels). We collapse the *ranking core* into a single `Orchestrator/retrieval.py::retrieve()` using **Reciprocal Rank Fusion (RRF)** of keyword+semantic candidates, a **mild recency tie-break** (semantic relevance dominates; recency only reorders near-ties), and **MMR diversity** (kills near-duplicate session clusters). We replace the hard similarity threshold with top-k + a low junk floor, calibrate a per-model threshold for the active model, and add a recent-gap guard so auto-migration can never cut over to a stale store.

**Tech Stack:** Python 3.12, FastAPI, NumPy (existing `VectorStore` binary stores), pytest. No new heavy deps (no cross-encoder/LLM rerank — explicitly out of scope per design decision).

**Design decisions (locked with the operator):**
- **Recency = mild tie-break**, not aggressive decay. Semantic relevance is the primary sort; recency only flips candidates whose relevance is within a small band. The best-matching fact still wins even if old; the *latest* version wins only when relevance is ~equal.
- **No rerank.** Fully offline-capable everywhere, including the on-device phone profile. (A rerank stage is deliberately not designed in; revisit only if measured precision demands it.)

---

## Background: what the audit established (evidence-based, not theory)

Three parallel read-only audits + live probing confirmed the following. **None of the semantic search surfaces bypass the active store** — the "active model is what's used" invariant holds. The problems are in *ranking*, *thresholds*, and *switch safety*:

| # | Finding | Severity | Evidence |
|---|---------|----------|----------|
| F1 | **Active model `gemini-embedding-2` has no calibrated threshold** — inherits the global `0.60` tuned for `gemini-embedding-001`. Its cosine scores run 0.05–0.10 lower; worst real top-10 hit = **0.6024** (0.0024 from being dropped). | HIGH | `registry.py:22-27` (comment: "until measured post-migration"); live score sweep |
| F2 | **`hybrid_retrieve` decodes ALL 7,176 snapshots (37 MB) into a dict on every call** to reverse-map keyword text→snap_id. ~52 ms + 37 MB transient per search, from chat + 3 voice routes + CU. Feeds the OOM leak. | HIGH (perf/OOM) | `fossils.py:77-91`; measured |
| F3 | **No recency prior on the semantic channel anywhere.** Recurring topics surface old foundational snapshots over the latest state. | MEDIUM | reproduced live: `"embeddings model switch reembed"` → June work buried at ranks 3/4/10 |
| F4 | **Auto-migration can cut over to a stale store** — `_pick_migration_target` has no recent-gap guard; a broken Gemini key could activate `qwen3-0.6b` (missing the 181 newest snapshots). | HIGH (latent) | `watcher.py:225-246`; the `missing` signal already exists at `embeddings_routes.py:85-94` |
| F5 | **Near-duplicate cluster crowding** — adjacent same-session snapshots fill most of top-k (4/10, 5/8 measured). No diversity step. | MEDIUM | `fossils.py:104,119`; `task_routes.py:236` |
| F6 | **Hard `>=0.60` threshold** is the wrong design vs top-k: drops a relevant 0.58 recent snap while keeping an old 0.61. Currently near-inert for the active model but fragile. | LOW→MED | `fossils.py:152`; `context_builder.py:109-111` |
| F7 | **Brittle keyword text→snap_id map** via `if snap_text == text` (O(k·N) 5 KB string compares; silently mis-maps on any duplicate text). | LOW→MED | `fossils.py:97-101` |
| F8 | **Provider-down = silent empty search** with no UI signal until the daily watcher pass (≤24 h). | MEDIUM | `search.py:126-140`; `watcher.py:96` |
| F9 | **Same-dims cutover race** writes wrong-space vectors (`raced`), logged but never auto-corrected. | MEDIUM | `migrate.py:14-20,371-377` |
| F10 | **`/fossil/hybrid` is misnamed** — semantic-only despite "hybrid"; MCP `search_snapshots`/`get_context` users get no keyword matching. | LOW | `task_routes.py:228-236` |

**Verified healthy (do not "fix"):** per-provider query-purpose handling (Gemini `retrieval_query` vs `retrieval_document`; Qwen instruct-prefix on queries) — live-proven correct; query vectors are unit-normalized and the store double-normalizes, so the threshold is applied to true cosine; dims-mismatch raises `ValueError` and is caught → `[]`; deliberate model-switch is fill-then-cutover and never serves a half-filled store.

**Out of scope (separate track):** the orchestrator heap leak / hourly OOM recycle (`MEMORY: project_blackbox_unreachable_memory_leak`). F2's fix *reduces* the pressure but does not fix the root leak. Cross-reference `diagnostics/leak-hunt/`.

### Surface map (all route to the active store; ranking differs)
- **Weighted hybrid** (`hybrid_retrieve`): chat tool-loop (`chat_routes.py:708,1293,2048,2779,4665,5319`), voice (`gemini_live_routes.py:155`, `realtime_routes.py:220`, `grok_live_routes.py:218`), CU driver (`browser/driver_anthropic.py:420`), admin recall (`admin_routes.py:873`).
- **Pure semantic** (`/fossil/hybrid` → `fossil_hybrid_search`): MCP `search_snapshots` + `get_context` (`MCP/blackbox_mcp_server.py:483,526`).
- **4-channel per-turn context** (`context_builder.build_fossil_context`): cloud chat/voice/tasks.
- **On-device lean per-turn** (`/local/turn/prepare`): `semantic_k=3` + 1 checkpoint, **no recency/keyword channel** — the most fragile profile and the true bar for "flawless regardless of model."

---

## Phase ordering & rationale

1. **Phase 1 — Threshold calibration + model-agnostic guard** (fixes F1; small, immediate recall win; makes "flawless regardless of model" enforceable).
2. **Phase 2 — Kill the 37 MB-per-call decode** (fixes F2 + F7; pure perf/OOM, ranking-preserving; ships independently).
3. **Phase 3 — Canonical recency-aware retriever** (fixes F3, F5, F6, F10; the core of the project).
4. **Phase 4 — Model-switch bulletproofing** (fixes F4, F8, F9).
5. **Phase 5 — Observability + golden-set validation** (locks quality; proves no regressions; validates the on-device profile).

Each phase is independently shippable and device-validatable (this box is staging-as-prod; build off `main`, no test branches, push = ship after local validation).

---

## Phase 1: Per-model threshold calibration + invariant guard

**Why:** F1. The active model silently runs on another model's threshold. We measure a correct floor for `gemini-embedding-2`, set it, and add a test that *forbids* any registered model from depending on the global fallback — so the next model switch can't reintroduce this.

### Task 1.1: Measurement script for per-model threshold

**Files:**
- Create: `scripts/calibrate_threshold.py`

**Step 1: Write the script** (it is a measurement tool, validated by running, not by unit test)

```python
#!/usr/bin/env python3
"""Measure a sensible semantic_threshold for the ACTIVE embedding model.

Embeds a fixed query set, runs store.search, and reports the score
distribution of the top-k so an operator can pick a floor that keeps strong
matches well clear of the cut. Read-only; no store writes.
"""
import sys, statistics
sys.path.insert(0, ".")
from Orchestrator.embeddings import search as S
from Orchestrator.embeddings.store import get_active_slug

QUERIES = [
    "embeddings model switch reembed snapshot volume",
    "nav2 costmap inflation tuning robot",
    "voice agent streaming speech to text",
    "control phone on-device gemma delegate",
    "google workspace docs sheets integration",
    "memory leak restart oom recycle",
    "android audiorecord release race crash",
    "tts voice catalog openai gemini elevenlabs",
    "tailscale security perimeter operator auth",
    "checkpoint mint auto embedding searchable",
]

def main():
    slug = get_active_slug()
    store = S.get_active_store()
    print(f"active model: {slug}  store.count={store.count}")
    all_top = []
    worst_top10 = []
    for q in QUERIES:
        qv = S.generate_embedding_sync(q, purpose="query")
        if not qv:
            print(f"  EMBED FAILED: {q!r}")
            continue
        hits = store.search(qv, 10, None)
        scores = [s for _, s in hits]
        all_top += scores
        if scores:
            worst_top10.append(min(scores))
        print(f"  {q[:40]:40} top1={scores[0]:.4f} top10={scores[-1]:.4f}")
    if all_top:
        print(f"\nAll top-10 scores: min={min(all_top):.4f} "
              f"p10={statistics.quantiles(all_top, n=10)[0]:.4f} "
              f"median={statistics.median(all_top):.4f} max={max(all_top):.4f}")
        print(f"Worst per-query top-10 floor: min={min(worst_top10):.4f}")
        print(f"SUGGESTED threshold = worst_top10_min - 0.05 = {min(worst_top10) - 0.05:.4f} "
              f"(keep strong matches clear of the cut; never above p10)")

if __name__ == "__main__":
    main()
```

**Step 2: Run it against the live active model**

Run: `Orchestrator/venv/bin/python scripts/calibrate_threshold.py`
Expected: prints per-query top1/top10 and a suggested threshold. For `gemini-embedding-2` the audit observed worst top-10 ≈ 0.6024, so expect a suggestion in the **0.52–0.55** range.

**Step 3: Commit**

```bash
git add scripts/calibrate_threshold.py
git commit -m "feat(embeddings): add per-model threshold calibration script"
```

### Task 1.2: Set the calibrated threshold for `gemini-embedding-2`

**Files:**
- Modify: `Orchestrator/embeddings/registry.py:22-28` (the `gemini-embedding-2` entry)
- Test: `Orchestrator/tests/test_embeddings_registry.py`

**Step 1: Write the failing test** (every model must declare an explicit threshold — no silent global fallback)

```python
# Orchestrator/tests/test_embeddings_registry.py  (add)
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

def test_every_model_declares_explicit_semantic_threshold():
    """A model-agnostic retriever requires each model to own its similarity
    floor. Inheriting the global 0.60 silently mis-cuts a model whose score
    distribution differs (regression guard for gemini-embedding-2 / F1)."""
    missing = [slug for slug, e in EMBEDDING_MODELS.items()
               if e.get("semantic_threshold") is None]
    assert missing == [], f"models without an explicit semantic_threshold: {missing}"
```

**Step 2: Run it to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_registry.py::test_every_model_declares_explicit_semantic_threshold -v`
Expected: FAIL — lists `gemini-embedding-2`, `openai-text-embedding-3-large`, `qwen3-embedding-8b`.

**Step 3: Set thresholds** using the measured value from Task 1.1. Replace the omission comment and add explicit values:

```python
# gemini-embedding-2 entry — use the value Task 1.1 measured (example: 0.54):
"query_instruction": None, "keep_alive": None, "semantic_threshold": 0.54,
# openai-text-embedding-3-large — 3-large cosine runs high; 0.55 is a safe floor:
"semantic_threshold": 0.55,
# qwen3-embedding-8b — local Qwen scores run low (sibling 0.6b uses 0.54): 0.50
"semantic_threshold": 0.50,
```

> Use the **measured** number from Task 1.1 for `gemini-embedding-2`, not the literal `0.54`, if the sweep differs. Document the measured floor in the commit body.

**Step 4: Run the test to verify it passes**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_registry.py -v`
Expected: PASS.

**Step 5: Reload + live-verify** (no restart needed for registry-derived threshold; it is read live via `active_threshold`)

Run: `curl -s -X POST http://localhost:9091/toolvault/reload` then re-run `scripts/calibrate_threshold.py` and confirm strong matches sit ≥0.05 above the new floor.

**Step 6: Commit**

```bash
git add Orchestrator/embeddings/registry.py Orchestrator/tests/test_embeddings_registry.py
git commit -m "fix(embeddings): calibrate per-model semantic_threshold (gemini-embedding-2 was inheriting gemini-001's 0.60)"
```

---

## Phase 2: Kill the 37 MB-per-call decode (perf/OOM, ranking-preserving)

**Why:** F2 + F7. `hybrid_retrieve` rebuilds a `{snap_id: full_text}` dict for all 7,176 snapshots on every call, purely to reverse-map keyword *results* (texts) back to snap_ids. The keyword retriever already knows the snap_ids from the index — we make it return them, deleting both the 37 MB allocation and the brittle string-equality map. **Ranking output is unchanged**; only the plumbing changes.

### Task 2.1: Add a snap_id-returning keyword retriever

**Files:**
- Modify: `Orchestrator/fossils.py` (add `keyword_retrieve_ids` / `keyword_retrieve_ids_for_operator` near `:818`/`:856`)
- Test: `Orchestrator/tests/test_retrieval_keyword_ids.py` (new)

**Step 1: Write the failing test**

```python
# Orchestrator/tests/test_retrieval_keyword_ids.py
from Orchestrator.fossils import keyword_retrieve_ids, load_snapshot_index

def test_keyword_retrieve_ids_returns_valid_snap_ids():
    idx = load_snapshot_index()
    ids = keyword_retrieve_ids("embeddings model switch reembed", k=5)
    assert isinstance(ids, list) and len(ids) <= 5
    assert all(sid in idx for sid in ids), "every returned id must exist in the index"
```

**Step 2: Run to verify it fails** (`keyword_retrieve_ids` not defined).

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_retrieval_keyword_ids.py -v`
Expected: FAIL — ImportError.

**Step 3: Implement** — refactor the existing TF-IDF scorer so it ranks snap_ids and returns ids; keep the existing text-returning functions as thin wrappers (`[snap_to_text[sid] for sid in ids]`) so current callers are untouched. The scoring logic (including the keyword recency bonus at `fossils.py:914-919`) is preserved verbatim — only the return type is added.

**Step 4: Run to verify it passes.**

**Step 5: Commit**

```bash
git add Orchestrator/fossils.py Orchestrator/tests/test_retrieval_keyword_ids.py
git commit -m "perf(retrieval): keyword retriever can return snap_ids (kills text reverse-map)"
```

### Task 2.2: Rewrite `hybrid_retrieve` internals to drop the full-volume decode

**Files:**
- Modify: `Orchestrator/fossils.py:53-127` (`hybrid_retrieve`)
- Test: `Orchestrator/tests/test_hybrid_retrieve_parity.py` (new)

**Step 1: Write the parity + cost test** (output unchanged; only decode result snap_ids)

```python
# Orchestrator/tests/test_hybrid_retrieve_parity.py
import tracemalloc
from Orchestrator.fossils import hybrid_retrieve, load_snapshot_index, read_volume_bytes
from Orchestrator.config import VOL_PATH

def _ids(texts):
    from Orchestrator.fossils import extract_snap_ids
    return extract_snap_ids(texts)

def test_hybrid_retrieve_decodes_only_result_snapshots():
    vol = read_volume_bytes(VOL_PATH).decode("utf-8", "replace")
    tracemalloc.start()
    hybrid_retrieve(vol, "embeddings model switch reembed", k=5, operator="system")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # Old path peaked ~37MB rebuilding snap_to_text. New path must stay well under.
    assert peak < 5 * 1024 * 1024, f"peak {peak} bytes — should not rebuild full volume"
```

**Step 2: Run to verify it fails** (current impl peaks ~37 MB).

**Step 3: Implement** — `hybrid_retrieve` now: get `keyword_retrieve_ids_for_operator` (ids+rank) + `semantic_search` (ids+score), fuse with the *existing* 0.4/0.6 weighting **by snap_id** (no text comparison), then decode **only** the final ≤k snap_ids' bytes from `vol_bytes` for the return value. Delete the `snap_to_text` build (`:77-91`) and the `if snap_text == text` loop (`:97-101`).

> Keep the 0.4/0.6 weighting here for now — Phase 3 replaces the fusion math globally. This task is a **pure perf fix** so it can ship and be validated in isolation.

**Step 4: Run to verify it passes** (peak < 5 MB; spot-check results match the pre-change top-k for 3 queries).

**Step 5: Commit**

```bash
git add Orchestrator/fossils.py Orchestrator/tests/test_hybrid_retrieve_parity.py
git commit -m "perf(retrieval): hybrid_retrieve decodes only result snapshots (was 37MB/call) [F2]"
```

**Step 6: Restart + live-validate** (this path is hot in chat/voice):

Run: `sudo systemctl restart blackbox.service` (pre-authorized), wait ~90 s, then exercise a chat search and confirm results + check `journalctl -u blackbox.service` for the `[HYBRID]` log line.

---

## Phase 3: Canonical recency-aware retriever

**Why:** F3, F5, F6, F10. One ranking core for every surface: RRF fusion (scale-free — fixes the TF-IDF-vs-cosine magnitude mismatch the 0.4/0.6 weighted-sum has), a **mild recency tie-break**, **MMR diversity**, and **top-k instead of a hard threshold** (low junk floor only).

### Task 3.1: The `retrieve()` core

**Files:**
- Create: `Orchestrator/retrieval.py`
- Test: `Orchestrator/tests/test_retrieval_core.py`
- Modify: `Orchestrator/embeddings/store.py` (add `search_with_vectors()` returning `(snap_id, score, vector)` so MMR reuses cached vectors — no re-embed)
- Config: `config.ini` new `[retrieval]` section

**Step 1: Write failing unit tests** against pure functions (deterministic, no network):

```python
# Orchestrator/tests/test_retrieval_core.py
from Orchestrator.retrieval import rrf_fuse, apply_recency_tiebreak, mmr_select

def test_rrf_is_scale_free():
    # keyword ranks [A,B], semantic ranks [B,C]; RRF rewards agreement (B)
    fused = rrf_fuse({"kw": ["A", "B"], "sem": ["B", "C"]}, c=60)
    assert fused[0][0] == "B"           # appears high in both → top
    assert {sid for sid, _ in fused} == {"A", "B", "C"}

def test_recency_only_breaks_near_ties():
    # Two candidates within the band -> newer wins; a far-better old one stays #1
    rel = {"old_strong": 1.00, "old_a": 0.50, "new_b": 0.49}
    ages = {"old_strong": 400, "old_a": 400, "new_b": 5}
    ranked = apply_recency_tiebreak(rel, ages, weight=0.05, half_life_days=90)
    assert ranked[0][0] == "old_strong"          # dominant relevance preserved
    assert ranked.index(("new_b", ranked[1][1])) < ranked.index(("old_a", ranked[2][1])) \
        if False else [s for s, _ in ranked][1] == "new_b"  # near-tie flips to recent

def test_mmr_drops_near_duplicates():
    import numpy as np
    v = lambda x: np.array(x, dtype="float32")
    cands = [("A", 1.0, v([1, 0])), ("A2", 0.98, v([0.99, 0.01])), ("B", 0.9, v([0, 1]))]
    picked = mmr_select(cands, k=2, lam=0.7)
    assert picked == ["A", "B"]          # A2 (near-dup of A) is skipped for diversity
```

**Step 2: Run to verify they fail** (module not defined).

**Step 3: Implement `Orchestrator/retrieval.py`:**

```python
"""Canonical snapshot retrieval — the single ranking core for every surface.

Pipeline: candidate generation (keyword ids + semantic ids/vectors from the
ACTIVE store) -> RRF fusion (scale-free; ranks, not raw scores) -> mild recency
tie-break (relevance dominates; recency flips only near-ties) -> MMR diversity
(drops near-duplicate session clusters) -> top-k. A low junk floor replaces the
old hard 0.60 threshold so a genuinely-relevant recent snap is never cut.

No rerank stage (design decision): fully offline-capable, incl. the on-device
phone profile. All semantic candidates come from get_active_store(), so the
retriever is automatically correct for whatever embedding model is active.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
import numpy as np

from Orchestrator import config
from Orchestrator.embeddings import search as _emb
from Orchestrator.fossils import keyword_retrieve_ids_for_operator, load_snapshot_index


def rrf_fuse(rankings: dict[str, list[str]], c: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion. score(d) = Σ_channels 1/(c + rank). Scale-free:
    uses rank position only, so TF-IDF (unbounded) and cosine (0.6-0.75) fuse
    without one channel's magnitude dominating — the bug in the old 0.4/0.6 sum."""
    scores: dict[str, float] = {}
    for ids in rankings.values():
        for rank, sid in enumerate(ids):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (c + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _age_days(ts_iso: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (now - t).total_seconds() / 86400.0)
    except Exception:
        return 3650.0  # unknown timestamp = treat as very old (no boost)


def apply_recency_tiebreak(relevance: dict[str, float], age_days: dict[str, float],
                           weight: float, half_life_days: float) -> list[tuple[str, float]]:
    """final = relevance + weight * 2^(-age/half_life). `weight` is small (default
    0.05) so recency can only reorder candidates whose relevance is within ~`weight`
    of each other — a *tie-break*, not a decay. A far-better old snap keeps its lead."""
    out = {}
    for sid, rel in relevance.items():
        boost = weight * math.pow(2.0, -age_days.get(sid, 3650.0) / half_life_days)
        out[sid] = rel + boost
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


def mmr_select(cands: list[tuple[str, float, np.ndarray]], k: int, lam: float) -> list[str]:
    """Maximal Marginal Relevance over (snap_id, relevance, unit_vector). Greedily
    picks the candidate maximizing lam*relevance - (1-lam)*max_sim_to_picked, so
    near-duplicate session clusters don't crowd out diverse facts."""
    picked: list[str] = []
    picked_vecs: list[np.ndarray] = []
    pool = list(cands)
    while pool and len(picked) < k:
        best_i, best_val = 0, -1e9
        for i, (_sid, rel, vec) in enumerate(pool):
            sim = max((float(vec @ pv) for pv in picked_vecs), default=0.0)
            val = lam * rel - (1.0 - lam) * sim
            if val > best_val:
                best_val, best_i = val, i
        sid, _rel, vec = pool.pop(best_i)
        picked.append(sid)
        picked_vecs.append(vec)
    return picked


def retrieve(query: str, operator: str = "", k: int = 10, *,
             include_keyword: bool = True) -> list[tuple[str, float]]:
    """Canonical ranked retrieval → [(snap_id, score), ...] top-k. `operator`
    "" / "system" sees all. `include_keyword=False` for lean profiles (on-device)."""
    if not query:
        return []
    N = config.CFG.getint("retrieval", "candidate_n", fallback=40)
    rrf_c = config.CFG.getint("retrieval", "rrf_c", fallback=60)
    w = config.CFG.getfloat("retrieval", "recency_weight", fallback=0.05)
    hl = config.CFG.getfloat("retrieval", "recency_half_life_days", fallback=90.0)
    lam = config.CFG.getfloat("retrieval", "mmr_lambda", fallback=0.7)
    floor = config.CFG.getfloat("retrieval", "junk_floor", fallback=0.40)

    store = _emb.get_active_store()
    qv = _emb.generate_embedding_sync(query, purpose="query")
    if not qv or store.count == 0:
        return []
    allowed = None
    if operator and operator != "system":
        idx0 = load_snapshot_index()
        allowed = {s for s, e in idx0.items() if e.get("operator") == operator}

    sem = store.search_with_vectors(qv, N, allowed)          # [(sid, cos, vec)]
    sem = [(sid, sc, vec) for sid, sc, vec in sem if sc >= floor]  # junk floor only
    sem_ids = [sid for sid, _, _ in sem]
    vec_by_id = {sid: vec for sid, _, vec in sem}
    rel_by_id = {sid: sc for sid, sc, _ in sem}

    rankings = {"semantic": sem_ids}
    if include_keyword:
        kw_ids = keyword_retrieve_ids_for_operator(query, N, operator or "system")
        rankings["keyword"] = kw_ids
        for sid in kw_ids:                                   # keyword-only hits get a floor rel
            rel_by_id.setdefault(sid, floor)

    fused = rrf_fuse(rankings, c=rrf_c)                       # [(sid, rrf)]
    rel = {sid: score for sid, score in fused}               # RRF as the relevance signal

    idx = load_snapshot_index()
    now = datetime.now(timezone.utc)
    ages = {sid: _age_days(idx.get(sid, {}).get("timestamp", ""), now) for sid in rel}
    ranked = apply_recency_tiebreak(rel, ages, weight=w, half_life_days=hl)

    # MMR over the top window using cached semantic vectors (keyword-only ids lack a
    # vector → given a zero vector, so they never collide and stay eligible).
    window = ranked[: max(k * 2, 20)]
    dim = len(qv)
    mmr_in = [(sid, sc, vec_by_id.get(sid, np.zeros(dim, dtype="float32"))) for sid, sc in window]
    order = mmr_select(mmr_in, k=k, lam=lam)
    score_of = dict(ranked)
    return [(sid, score_of.get(sid, 0.0)) for sid in order]
```

Add to `Orchestrator/embeddings/store.py` a `search_with_vectors(query_vec, k, allowed_ids)` that mirrors `search()` (`store.py:240-261`) but also returns the matched row vector (already in the in-memory matrix — no extra I/O).

Add to `config.ini`:
```ini
[retrieval]
candidate_n = 40
rrf_c = 60
recency_weight = 0.05
recency_half_life_days = 90
mmr_lambda = 0.7
junk_floor = 0.40
final_k = 10
```

**Step 4: Run unit tests to green.**

**Step 5: Live validation harness** — extend `scripts/calibrate_threshold.py` style: print `retrieve()` top-10 for the recurring-topic queries and assert the latest-state snapshots rank ahead of foundational ones *without evicting them* (the audit's 3/4/10 → 1/2/3 result).

**Step 6: Commit**

```bash
git add Orchestrator/retrieval.py Orchestrator/embeddings/store.py Orchestrator/tests/test_retrieval_core.py config.ini
git commit -m "feat(retrieval): canonical RRF + recency-tiebreak + MMR retriever [F3,F5,F6]"
```

### Task 3.2: Route every surface through `retrieve()`

**Files (modify, one commit each, live-validate between):**
- `Orchestrator/fossils.py:53` — `hybrid_retrieve` becomes a thin shim: `ids = retrieve(query, operator, k, include_keyword=True)` then decode those ids' text. (Replaces Phase-2's interim 0.4/0.6 fusion with the canonical core.)
- `Orchestrator/routes/task_routes.py:228` — `/fossil/hybrid` calls `retrieve()` (now genuinely hybrid + recency-aware → fixes the user-visible `search_snapshots` recency gap **and** F10's misnomer). Update the docstring.
- `Orchestrator/context_builder.py:132` — replace the `semantic_retrieve(...)` sub-channel with `retrieve(user_text, operator, k=SF, include_keyword=False)` (keyword is already a separate channel here; keep recent + checkpoint + cross-channel dedup untouched). The hard 0.60 threshold is gone — top-k + junk floor instead.
- `Orchestrator/routes/local_routes.py:266` (`/local/turn/prepare`) inherits the change via `context_builder` automatically; **add an explicit on-device test** (Task 5.3) because its budget is `semantic_k=3`.

**Per file:** add a test asserting the surface returns recency-aware results for a recurring-topic query, run it, then `sudo systemctl restart blackbox.service` and live-exercise that surface (MCP `search_snapshots`, a chat search, a voice search) before moving on. Commit per file with `refactor(retrieval): route <surface> through canonical retrieve()`.

---

## Phase 4: Model-switch bulletproofing

**Why:** F4, F8, F9 — make switching/auto-migration safe so "flawless regardless of model" holds even under provider failure + the hourly OOM.

### Task 4.1: Recent-gap guard on auto-migration target (F4)

**Files:**
- Modify: `Orchestrator/embeddings/watcher.py:225-246` (`_pick_migration_target`)
- Test: `Orchestrator/tests/test_embeddings_watcher.py`

**Step 1: Failing test** — a candidate store missing any of the newest-N snapshots is rejected:

```python
def test_pick_migration_target_rejects_stale_recent_gap(monkeypatch):
    # store missing the 100 newest snap_ids must NOT be chosen even if local + high count
    ...
    target, reason = _pick_migration_target(active="gemini-embedding-2", successor_slug=None)
    assert target != "qwen3-embedding-0.6b"
    assert "recent" in reason.lower() or "gap" in reason.lower()
```

**Step 2: Run → fail.**

**Step 3: Implement** — compute each candidate's `missing(index)` (reuse the logic at `embeddings_routes.py:85-94`); reject any whose newest-N gap exceeds `RECENT_GAP_MAX` (config, default 25) or that is missing any of the last `RECENT_GAP_TAIL` (default 50) snap_ids. Prefer staying `broken` with a loud banner over activating stale memory. (Auto-migration already diff-fills before cutover via `start_migration`, but the guard prevents cutover when the fill can't complete — e.g. the broken provider owns the gaps.)

**Step 4: Run → pass. Step 5: Commit** `fix(embeddings): auto-migration rejects stores with a recent-end gap [F4]`.

### Task 4.2: Fast provider-down health signal (F8)

**Files:**
- Modify: `Orchestrator/embeddings/search.py:126-140` (record consecutive query-embed failures); `Orchestrator/embeddings/watcher.py` (`health.json` writer); the Portal/onboarding embeddings card.
- Test: `Orchestrator/tests/test_embeddings_search.py`

**Step 1–4 (TDD):** after `FAIL_THRESHOLD` (default 3) consecutive query-embed failures, write `health.json` `state="degraded", detail="embedding provider unreachable"` immediately (don't wait for the ≤24 h watcher), and surface "search temporarily unavailable" in the UI instead of silently returning `[]`. Test simulates a failing provider and asserts the flag flips fast. **Commit** `feat(embeddings): fast provider-down health signal [F8]`.

### Task 4.3: Auto-correct same-dims `raced` vectors (F9)

**Files:**
- Modify: `Orchestrator/embeddings/store.py` (add `upsert(snap_id, vec)` overwrite-by-id); `Orchestrator/embeddings/migrate.py:371-377` (after cutover, re-embed the `raced` set under the new model and upsert).
- Test: `Orchestrator/tests/test_embeddings_migrate.py`

**TDD + commit** `fix(embeddings): re-embed raced same-dims vectors after cutover [F9]`.

### Task 4.4 (optional, ties to OOM): OOM-survivable large re-embed

**Files:** `Orchestrator/embeddings/migrate.py:278-291` — persist a per-pass cursor (last embedded snap_id) so a resumed job after an OOM kill skips already-embedded ids cheaply instead of re-slicing the full 35 MB volume each pass. **TDD + commit.** (Note in commit: full fix depends on the separate OOM-leak track.)

---

## Phase 5: Observability + golden-set validation

**Why:** lock quality, prove no regressions, and validate the fragile on-device profile. Follows the `MEMORY: telemetry-before-fixes` principle — make retrieval debuggable so the next "old results" report is answerable from logs.

### Task 5.1: Provenance logging on `retrieve()`

Add a structured debug log per call: query, per-channel candidate counts, top-k with `(snap_id, rrf, recency_boost, age_days, final)` and which channel each came from. Gated behind a `[retrieval] debug_log` flag. Test asserts the log line shape. **Commit.**

### Task 5.2: Golden-set regression test

**Files:** `Orchestrator/tests/golden/retrieval_golden.jsonl` (curated), `Orchestrator/tests/test_retrieval_golden.py`.

Two query classes: **recurring topics** (latest state must rank in top-3 without evicting foundational) and **single-event topics** (the *old* snapshot IS the answer — must NOT be demoted by the tie-break; this is the safety check on `recency_weight`). Test asserts expected snap_ids appear in top-k. **Commit.** Run a parameter sweep on `recency_weight` / `half_life` against this set and record the chosen defaults in the commit body.

### Task 5.3: On-device lean-profile validation

**Files:** `Orchestrator/tests/test_local_turn_prepare_recall.py`.

Assert `/local/turn/prepare` (semantic_k=3, no keyword/recent) returns **non-empty** semantic provenance for representative queries under the *calibrated* threshold + junk-floor (guards against the F1/F6 "phone gets zero memories" failure). Validate against the active model AND, by temporarily pointing a test at the qwen store, a second model — proving model-agnostic recall. **Commit.**

---

## Testing & rollout strategy

- **Run the full embeddings suite after each phase:** `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -k "embedding or retrieval or fossil or context" -v`.
- **Reload vs restart:** registry/threshold changes go live with `POST /toolvault/reload`; code changes need `sudo systemctl restart blackbox.service` (pre-authorized; ~90 s warm-up).
- **Device-validate each phase** on the real surfaces (MCP `search_snapshots`, in-app chat search, a voice session, the phone `/local/turn/prepare`) — this box is staging-as-prod; push to `main` = ship after local validation.
- **Ship order:** Phase 1 → 2 (independently valuable, low risk) before the larger Phase 3.

## Risks & rollback

| Risk | Mitigation |
|------|------------|
| Recency tie-break demotes a single-event answer | `recency_weight=0.05` is additive + tiny by design; Task 5.2 golden set explicitly guards single-event topics; all params are config-tunable live. |
| RRF changes ranking vs current weighted-sum in a surprising way | Phase 2 ships the perf fix with **unchanged** ranking first; Phase 3's RRF is validated against the golden set before wiring all surfaces; each surface wired + validated independently. |
| `search_with_vectors` memory | Returns references into the existing in-memory matrix; no copy of the full store. |
| MMR latency | O(k·window) cosines on cached vectors (~hundreds of ops); negligible vs the embed RTT. |
| Threshold too low admits noise | Junk floor (0.40) + top-k cap; golden set checks precision. |
| OOM still recycling underneath | Phase 2 reduces pressure; Phase 4.4 makes re-embeds survivable; **root leak is a separate track** (`diagnostics/leak-hunt/`). |

## Out of scope (tracked separately)
- The orchestrator heap leak / hourly OOM recycle (root cause). This plan *reduces* its retrieval-path contribution (F2) and makes re-embeds survive it (4.4), but does not fix the leak.
- LLM/cross-encoder rerank (design decision: not now).
