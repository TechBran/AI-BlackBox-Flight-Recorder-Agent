# Embeddings: Threshold Recalibration + gemini-embedding-2 Registration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore semantic-search recall (snapshots are being filtered out of chat context by a stale `0.7` threshold) and register `gemini-embedding-2` so the existing one-click Upgrade button lights up on every surface — without auto-migrating off `gemini-embedding-001`.

**Architecture:** Two independent backend changes plus cross-surface verification. (1) Make the semantic threshold **per-model** — a new optional `semantic_threshold` field in the embedding-model registry (the single source of truth), resolved at retrieval time with the `config.ini` global as fallback; lower the global default to `0.60`. (2) Add one `gemini-embedding-2` registry entry. Because the watcher resolves `health.successor_slug` from the registry, that single entry makes the **already-built** "Update/Switch now" button appear and function on Portal-onboarding, Portal-updates-manager, Android Updates, and the WebView wrapper — each of which already POSTs `/embeddings/migrate` (background diff-and-fill + atomic cutover). No frontend logic changes; copy polish only.

**Tech Stack:** Python 3.12 / FastAPI (Orchestrator), pytest, `google.generativeai` (Gemini embeddings), vanilla JS (Portal), Kotlin/Compose (Android MVP).

---

## Background — root cause (already investigated, data-backed)

- The pluggable-embeddings refactor changed query embedding from `retrieval_document` → `retrieval_query` (`Orchestrator/embeddings/providers.py:40`, `_GEMINI_TASK_TYPES`). Correct for quality, but it shifts the cosine-score distribution **down ~0.1**.
- The `0.7` threshold (`config.ini:34`, default fallback in `Orchestrator/context_builder.py:103` and `Orchestrator/fossils.py:131`) was calibrated for the OLD symmetric scores and was never updated.
- **Measured live** against the fully-populated `gemini-embedding-001` store (count 6998, missing 0): top scores cluster **0.68–0.80**. E.g. `"voice lab cloning elevenlabs"` tops out at **0.715** with only 4 of 8 results ≥ 0.70; many normal queries return **0** after the 0.7 filter → empty semantic provenance.
- Routing is **correct** — `semantic_search` → `get_active_store()` → `get_active_slug()` (`Orchestrator/embeddings/search.py:108–143`). The "switch did nothing" symptom was the `qwen3-8b` store stalled at 16/6998 (cutover never fired; `migrate.py:351–369` only cuts over after a full backfill), so the active model stayed `gemini-001` while the 0.7 threshold starved it.
- `gemini-embedding-2`: live, **non-preview**, **3072 dims** (== 001), multimodal, **$0.20/1M** text (vs 001 $0.15), MRL like 001, **incompatible embedding space** (migrating = full re-embed ~7000 snapshots ≈ $2–4 one-time). `gemini-embedding-001` is **not deprecated**. ⚠️ March-2026 docs still labeled `gemini-embedding-2` as preview; the live June catalog shows a bare GA id that embeds fine — reconfirm GA on Google's changelog before encouraging a paid migration.

**Cross-provider audit (2026-06-13, evidence-backed — the local-switch failure):**
- **No hardcoded Gemini-only embedding path exists.** Every embed in the tree routes through `provider.embed(...)` (the single chokepoint): `embeddings_routes.py:228,329` (validate/warmup), `search.py:98`, `migrate.py:315`, `watcher.py:288,333`. No raw `genai.embed_content` outside `providers.py`. Dims are registry-driven everywhere (`entry["dims"]`, `EMBEDDING_MODELS[slug]["dims"]`, store meta) — never hardcoded. The only `gemini-embedding-001` literal outside `registry.py` is the config seed default `EMBEDDINGS_ACTIVE_DEFAULT` (`config.py:182`), not a code path.
- **The local-switch "no snapshots" symptom IS the threshold.** Read-only search of the *complete* `qwen3-embedding-0.6b` store (6995 vectors, correct 1024-dim queries) returns top-6 scores **0.724, 0.618, 0.593, 0.567, 0.551, 0.549** — Qwen's cosine geometry sits ~0.15–0.20 below Gemini's, so the `0.7` floor returns **exactly one** result (or zero when the top dips below 0.7). The per-model threshold (this plan) is the direct cure; `qwen3-embedding-0.6b` wants ≈ **0.54**.
- **`qwen3-embedding-8b` "did nothing" for a second, non-bug reason:** its store is at 16/6998 — the backfill never finished (hours of CPU embedding), and the cutover guard (`migrate.py:351`) correctly refuses to activate an empty store, so the active model stayed `gemini-001`. The switch path works; it needs time + visible progress (the card's `job` progress already polls — Task 7 verifies it).

**Decisions (locked with operator):** per-model threshold + lower global to ~0.60; register `gemini-embedding-2` but stay active on `gemini-001`; the superseded banner must offer a one-click Upgrade that re-embeds in the background as usual (already built — registration alone enables it).

**Pre-flight:** Do this work in a dedicated worktree/branch off `main` (the current `feat/elevenlabs-integration` branch has unrelated uncommitted changes). Suggested: `feat/embeddings-threshold-and-embed2`.

---

### Task 1: Per-model semantic threshold resolver + lower global default

**Files:**
- Modify: `Orchestrator/embeddings/registry.py` (add optional `semantic_threshold` to entries)
- Modify: `Orchestrator/embeddings/search.py` (add `active_threshold()` resolver)
- Modify: `Orchestrator/context_builder.py:103,116-117` (use resolver)
- Modify: `Orchestrator/routes/chat_routes.py:~5489` (CU path: same resolver for parity)
- Modify: `config.ini:34` (`semantic_threshold = 0.7` → `0.60`)
- Test: `Orchestrator/tests/test_embeddings_search.py`

**Step 1: Write the failing test** in `test_embeddings_search.py`:

```python
def test_active_threshold_prefers_registry_then_fallback(monkeypatch):
    from Orchestrator.embeddings import search
    from Orchestrator.embeddings.registry import EMBEDDING_MODELS
    # model WITH a per-model floor → registry value wins over fallback
    monkeypatch.setattr(search, "get_active_slug", lambda: "gemini-embedding-001")
    monkeypatch.setitem(EMBEDDING_MODELS["gemini-embedding-001"], "semantic_threshold", 0.60)
    assert search.active_threshold(fallback=0.7) == 0.60
    # model WITHOUT one → caller fallback (the config global) is used
    monkeypatch.delitem(EMBEDDING_MODELS["gemini-embedding-001"], "semantic_threshold")
    assert search.active_threshold(fallback=0.55) == 0.55
```

**Step 2: Run it to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_search.py::test_active_threshold_prefers_registry_then_fallback -v`
Expected: FAIL — `module 'search' has no attribute 'active_threshold'`.

**Step 3: Implement the resolver** in `Orchestrator/embeddings/search.py` (after `get_active_store`):

```python
def active_threshold(fallback: float) -> float:
    """Per-model semantic-similarity floor; `fallback` (the config global) when
    the active model declares none. Registry is the only place model-specific
    values live (Task-16 ratchet), so a model whose score distribution differs
    (Gemini retrieval_query vs Qwen instruct-prefixed) carries its own floor."""
    entry = EMBEDDING_MODELS.get(get_active_slug(), {})
    value = entry.get("semantic_threshold")
    return float(value) if value is not None else float(fallback)
```

(`EMBEDDING_MODELS` and `get_active_slug` are already imported in `search.py`.)

**Step 4: Wire it into the retrieval path.** In `Orchestrator/context_builder.py`, change line ~103:

```python
    ST  = CFG.getfloat("context", "semantic_threshold", fallback=0.60)
    from Orchestrator.embeddings.search import active_threshold  # lazy: avoid startup cycle
    ST = active_threshold(ST)
```

Mirror in `Orchestrator/routes/chat_routes.py` where `CU_ST` is read (~5489): `CU_ST = active_threshold(CU_ST)`.

**Step 5: Lower the global default.** `config.ini:34` → `semantic_threshold = 0.60`. Also bump the in-code fallbacks from `0.7` → `0.60` in `context_builder.py` and `fossils.py:131` (`def semantic_retrieve(..., threshold: float = 0.60)`) so callers without config agree.

**Step 6: Run tests**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_search.py Orchestrator/tests/test_embeddings_guards.py -v`
Expected: PASS (guard test confirms no model literal leaked outside `registry.py` — the resolver reads the dict, no literals).

**Step 7: Commit**

```bash
git add Orchestrator/embeddings/registry.py Orchestrator/embeddings/search.py \
        Orchestrator/context_builder.py Orchestrator/routes/chat_routes.py \
        Orchestrator/fossils.py config.ini Orchestrator/tests/test_embeddings_search.py
git commit -m "fix(embeddings): per-model semantic threshold + lower global to 0.60 (retrieval_query recalibration)"
```

---

### Task 2: Calibrate per-model floors from measured score distributions

**Files:**
- Modify: `Orchestrator/embeddings/registry.py` (set `semantic_threshold` per populated model)

**Step 1: Measure** the active store and each populated store. For each, embed 5–8 representative queries and inspect raw top-k scores (no threshold):

```bash
Orchestrator/venv/bin/python - <<'PY'
from Orchestrator.embeddings import search, store
for slug in ["gemini-embedding-001", "qwen3-embedding-0.6b"]:
    try:
        store.set_active_slug(slug); search.swap_active(slug)
    except Exception as e:
        print(slug, "skip:", e); continue
    for q in ["voice lab cloning", "nav2 tuning", "embeddings switching", "phone bridge asterisk"]:
        res = search.semantic_search(q, operator="", k=8)
        print(slug, q, [round(s,3) for _, s in res])
PY
# restore active
Orchestrator/venv/bin/python -c "from Orchestrator.embeddings import search, store; store.set_active_slug('gemini-embedding-001'); search.swap_active('gemini-embedding-001')"
```

**Step 2: Set floors** in `registry.py` ~0.05 below each model's typical top-relevant score (so the genuine top-k survive, true noise drops). Measured values from this investigation:
- `gemini-embedding-001` → `"semantic_threshold": 0.60` (scores 0.68–0.80).
- `qwen3-embedding-0.6b` → `"semantic_threshold": 0.54` (measured top-6: 0.724, 0.618, 0.593, 0.567, 0.551, 0.549 — Qwen's geometry sits well below Gemini's; do NOT reuse the Gemini number). Re-measure in Step 1 to confirm before committing.
- `qwen3-embedding-8b`, `openai-text-embedding-3-large` → leave unset (sparse/no store) → global `0.60`; measure and set once their stores are populated.

**Step 3: Sanity-check recall** with the real retrieval helper:

```bash
Orchestrator/venv/bin/python -c "from Orchestrator.fossils import semantic_retrieve as r; print(len(r('voice lab cloning', operator='system', k=6)))"
```
Expected: non-zero (was 0 before).

**Step 4: Commit**

```bash
git add Orchestrator/embeddings/registry.py
git commit -m "fix(embeddings): calibrate per-model semantic thresholds from measured score distributions"
```

---

### Task 3: Register gemini-embedding-2 (additive — lights up the Upgrade button)

**Files:**
- Modify: `Orchestrator/embeddings/registry.py`
- Test: `Orchestrator/tests/test_embeddings_watcher.py`

**Step 0: Reconfirm GA** — verify `models/gemini-embedding-2` (bare, non-preview) is GA on Google's official changelog/docs. The live catalog lists it and it embeds at 3072 dims; do not proceed on a paid migration story if it's still preview-only.

**Step 1: Write the failing test** — the watcher must resolve the new vendor id to the new slug:

```python
def test_registry_maps_gemini_embedding_2_vendor_id():
    from Orchestrator.embeddings.watcher import _registry_slug_for
    assert _registry_slug_for("gemini", "models/gemini-embedding-2") == "gemini-embedding-2"
```

**Step 2: Run it to verify it fails**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_watcher.py::test_registry_maps_gemini_embedding_2_vendor_id -v`
Expected: FAIL (returns `None` — no entry yet).

**Step 3: Add the registry entry** in `Orchestrator/embeddings/registry.py` (after `gemini-embedding-001`). **Do NOT** touch `LEGACY_INLINE_SLUG` or the active pointer:

```python
    "gemini-embedding-2": {
        "provider": "gemini", "model_id": "models/gemini-embedding-2", "dims": 3072,
        "label": "Gemini 2 (cloud, multimodal)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.20,
        "privacy": "cloud", "quality_note": "Newest Gemini embedding (multimodal); re-embed required to switch",
        "query_instruction": None, "keep_alive": None,
        # semantic_threshold intentionally omitted → global 0.60 until measured post-migration
    },
```

**Step 4: Run watcher + routes + parity tests**

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_watcher.py Orchestrator/tests/test_embeddings_routes.py Orchestrator/tests/test_embeddings_guards.py Orchestrator/tests/test_portal_embeddings_card_parity.py Orchestrator/tests/test_android_embeddings_card_parity.py -v`
Expected: PASS. (Parity tests assert the `successor_slug` / `/embeddings/migrate` contract — unchanged.)

**Step 5: Commit**

```bash
git add Orchestrator/embeddings/registry.py Orchestrator/tests/test_embeddings_watcher.py
git commit -m "feat(embeddings): register gemini-embedding-2 (enables one-click Upgrade; no auto-migrate)"
```

---

### Task 4: Verify one-click Upgrade across all surfaces (+ optional copy)

No logic changes — registering the model makes `successor_slug` non-null, which the existing gates already consume. This task is **verification**; copy polish is optional.

**Surfaces to verify** (each gated on `successor_slug` / `successorSlug`, each POSTs `/embeddings/migrate`):
- Portal onboarding wizard — `Portal/onboarding/steps/embeddings.js:276` ("Switch now" → `startMigrate(successor_slug)`).
- Portal Updates manager — `Portal/modules/updates-manager.js:56` ("Update" → `_onEmbeddingsUpdateClick(..., successor_slug)`); "Manage" deep-links to the wizard.
- Android Updates — `.../ui/updates/UpdatesScreen.kt` `EmbeddingsCard(onUpdate=slug→viewModel.startEmbeddingsUpdate(slug))` → `UpdatesViewModel.startEmbeddingsUpdate` POSTs migrate.
- WebView wrapper — inherits the Portal web surfaces; no separate code (smoke-check the embeddings card renders + Upgrade fires inside the wrapper).

**Step 1: Live check the contract after restart (Task 6 restarts):**

```bash
curl -s http://localhost:9091/embeddings/status | python3 -m json.tool | grep -A4 '"health"'
```
Expected: `"state": "superseded"`, `"successor_slug": "gemini-embedding-2"` (was `null`).

**Step 2 (optional copy):** If desired, change the Portal labels from "Switch now"/"Update" to "Upgrade to {successor}" — keep the same element ids (`ob-emb-health-switch`, `btnEmbeddingsUpdate`) so parity tests and listeners are untouched. Mirror the Android `EmbeddingsCard` button label. If you change copy, run both `*_card_parity.py` tests again.

**Step 3: Confirm Upgrade is non-destructive & backgrounded** — clicking Upgrade must POST `/embeddings/migrate {target:"gemini-embedding-2"}`, return a running job, diff-and-fill in the background, and only cut over atomically when complete (`migrate.py`). Active stays `gemini-001` until the operator clicks. Verify the job is pollable via `GET /embeddings/status` (`job` field) without blocking the UI.

**Step 4: Commit** (only if copy changed)

```bash
git add Portal/onboarding/steps/embeddings.js Portal/modules/updates-manager.js \
        "AI_BlackBox_Portal_Android_MVP (2)/.../ui/updates/UpdatesScreen.kt"
git commit -m "polish(embeddings): banner Upgrade button copy names the successor model"
```

---

### Task 5: Observability — make threshold-filtering visible

So a future "no semantic snapshots" is diagnosable from logs instead of guesswork.

**Files:**
- Modify: `Orchestrator/fossils.py` (in `semantic_retrieve`, near line ~131 block)

**Step 1:** The "No results above threshold" branch already prints the candidate count. Extend it to also log the **best candidate score** so a near-miss (e.g. top 0.69 vs floor 0.60) is obvious:

```python
    if not filtered_results:
        best = max((s for _, s in semantic_results), default=None)
        print(f"[SEMANTIC] 0 of {len(semantic_results)} candidates cleared threshold "
              f"{threshold} (best={best:.3f})" if best is not None
              else f"[SEMANTIC] no candidates (empty store?) at threshold {threshold}")
        return []
```

**Step 2: Commit**

```bash
git add Orchestrator/fossils.py
git commit -m "chore(embeddings): log best candidate score when threshold filters all semantic results"
```

---

### Task 6: Restart, live-verify end-to-end, snapshot

`registry.py` (`EMBEDDING_MODELS`) and `config.ini` (`CFG`) are read at import — a **restart is required** for Tasks 1–3 to go live. Restart is pre-authorized.

**Step 1: Restart**

```bash
sudo systemctl restart blackbox.service   # 60–90s warm-up (snapshot index rebuild)
```

**Step 2: Verify health + successor**

```bash
curl -s http://localhost:9091/embeddings/status | python3 -m json.tool | grep -E 'active|successor_slug|"state"'
```
Expected: `active: gemini-embedding-001`, `state: superseded`, `successor_slug: gemini-embedding-2`.

**Step 3: Verify recall is restored in a REAL chat turn.** Send a prompt via the Portal whose answer lives in snapshots (e.g. "what did we do for the voice lab cloning"), then tail the service logs:

```bash
journalctl -u blackbox.service --no-pager -n 200 | grep -E "Semantic snapshots|\[SEMANTIC\]"
```
Expected: `Semantic snapshots (N>0, threshold=0.60): [SNAP-...]` and a non-empty semantic section in the context provenance. (If journald is empty, the print lines surface on stdout — check the service's stdout sink.)

**Step 4: (Operator-driven, optional) Exercise Upgrade** — from the Portal/Android banner, click Upgrade and confirm a background migrate job starts and progresses. Do NOT trigger automatically; this re-embeds ~7000 snapshots and is the operator's call (and a cost event).

**Step 5: Snapshot the work** via `/snapshot-dev` (operator resolved dynamically) so this lands in searchable memory.

---

### Task 7: Cross-provider switch verification (the operator's real scenario)

Prove a **local** model works end-to-end after switching, and that the per-model threshold restores recall. This is the regression that started the whole investigation.

**Files:** none (verification + temporary live switch; restore after).

**Step 1: Read-only recall proof on the complete local store** (no active-pointer change):

```bash
Orchestrator/venv/bin/python - <<'PY'
import asyncio
from Orchestrator.embeddings.store import get_store
from Orchestrator.embeddings.providers import get_provider
from Orchestrator.embeddings.search import active_threshold  # uses ACTIVE; here we test the value we'll set
prov = get_provider("qwen3-embedding-0.6b"); store = get_store("qwen3-embedding-0.6b")
for q in ["voice lab cloning", "nav2 tuning", "phone bridge asterisk", "embeddings switching"]:
    v = asyncio.run(prov.embed([q], "query"))[0]
    res = store.search(v, 6, None)
    survivors = [round(s,3) for _, s in res if s >= 0.54]
    print(q, "→ survivors@0.54:", survivors)
PY
```
Expected: each query returns ≥1 (typically 3–6) survivors at 0.54 — recall restored on the local model.

**Step 2: Full live switch test on a controlled box** (only if safe to mutate the active pointer; otherwise skip — Step 1 + the migrate-progress check below already prove the path). Switch active → `qwen3-embedding-0.6b`, run one real Portal chat turn, confirm `Semantic snapshots (N>0, threshold=0.54)` in logs, then **restore** active → `gemini-embedding-001`:

```bash
# switch
Orchestrator/venv/bin/python -c "from Orchestrator.embeddings import store, search; store.set_active_slug('qwen3-embedding-0.6b'); search.swap_active('qwen3-embedding-0.6b'); print('active:', store.get_active_slug())"
# ... send a Portal chat turn, then check logs:
journalctl -u blackbox.service --no-pager -n 100 | grep "Semantic snapshots"
# RESTORE (mandatory)
Orchestrator/venv/bin/python -c "from Orchestrator.embeddings import store, search; store.set_active_slug('gemini-embedding-001'); search.swap_active('gemini-embedding-001'); print('restored:', store.get_active_slug())"
```

**Step 3: Verify the 8B backfill path makes progress** (proves switching to an unpopulated local model works, just slowly). Start a migrate to `qwen3-embedding-8b`, poll status twice, confirm `job.done` increments, then cancel:

```bash
curl -s -X POST http://localhost:9091/embeddings/migrate -H 'Content-Type: application/json' -d '{"target":"qwen3-embedding-8b"}' | python3 -m json.tool
sleep 90; curl -s http://localhost:9091/embeddings/status | python3 -c "import sys,json; j=json.load(sys.stdin)['job']; print('job:', j)"
curl -s -X POST http://localhost:9091/embeddings/migrate/cancel | python3 -m json.tool
```
Expected: `job.state == running` with `done` climbing; active stays `gemini-embedding-001` throughout (cutover guard). Confirms the path is alive — full 8B backfill is ~hours on CPU and is the operator's call to run to completion.

**Step 4: Commit** (none — verification only; record outcome in the Task 6 snapshot).

---

## Verification checklist (superpowers:verification-before-completion)

- [ ] `pytest` green: `test_embeddings_search.py`, `test_embeddings_guards.py`, `test_embeddings_watcher.py`, `test_embeddings_routes.py`, both `*_card_parity.py`.
- [ ] `GET /embeddings/status`: `successor_slug == "gemini-embedding-2"`, `active` still `gemini-embedding-001`.
- [ ] Live chat turn logs `Semantic snapshots (N>0, ...)` and provenance shows semantic IDs.
- [ ] Upgrade button visible + functional on Portal-onboarding, Portal-updates-manager, Android Updates, WebView wrapper (gated on `successor_slug`).
- [ ] No model literal leaked outside `registry.py` (guard test).
- [ ] **Cross-provider proven:** local `qwen3-embedding-0.6b` returns non-zero recall at its 0.54 floor (Task 7 Step 1); 8B migrate `job.done` increments while active stays `gemini-001` (Task 7 Step 3).
- [ ] Active model unchanged after all verification (restored to `gemini-embedding-001`); no migration auto-triggered.
