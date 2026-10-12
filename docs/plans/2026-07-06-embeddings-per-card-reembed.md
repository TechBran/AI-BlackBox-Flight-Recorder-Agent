# Per-Card "Re-embed" Button — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task.

**Goal:** Add a per-embedding-model "Re-embed" button that fully re-embeds that model's store under the current chunk strategy (schema 2) in the background with live progress, then makes the rebuilt store live — every model's store preserved and switchable.

**Architecture:** Reuse the existing build-only rebuild engine (`migrate._run_rebuild_engine`, which re-embeds the whole corpus chunked into `{stores}/_build/{slug}`) and add the missing **in-service activation seam** (`_activate_candidate`): atomic dir-swap of the candidate into the live store dir, `get_store` cache eviction, conditional `search.swap_active` (only when the re-embedded slug is the active model) + catch-up-fill of snapshots minted during the build, and a retained `{slug}.pre-rebuild.<ts>` rollback. A new `POST /embeddings/reembed` route drives it via the same singleton `_JOB` + `status.job` progress the wizard already renders. Rerankers are untouched (they own no store).

**Tech Stack:** Python 3 / FastAPI / pytest (backend `Orchestrator/`), vanilla JS (Portal `Portal/onboarding/steps/embeddings.js`), NumPy binary vector stores.

**Context docs:** design rationale in `docs/plans/2026-07-06-embeddings-per-card-reembed-plan.md` (the "why"); this file is the "how". Locked decisions D1–D5 there: embedding cards only; cloud=confirm-with-cost, local=one-click; wizard first then parity; candidate-swap not destructive; auto-activate on completion.

**Branch:** work on `main` (per the box's build-off-main convention); commit per task.

> **Adversarial review applied (2026-07-06, workflow `w82ey2iv5`, verdict GO-WITH-FIXES).** 10 refutation agents hit the source; 1 blocker + 8 majors + 4 minors folded in below. The load-bearing fixes are baked into the task bodies: harness closure (Task 0.1 note), three store-hardening tasks (0.1b list_stores shadowing, 0.1c matrix/search race, 0.1d retire-old-instance), claim-before-clear ordering (0.6), 0-snapshot no-op + stale-`.incoming` cleanup (0.5), the corrected `_catch_up_fill` test (0.4), the 409-sentinel test (0.9), and `MODEL_KEYS` (1.1). Do not "optimize away" these — each maps to a specific defect the pass proved against current code.

---

## Ground-truth already verified (do not re-litigate)

- `VectorStore` loads `vectors.f32` via `np.fromfile(...).reshape(...)` (`store.py:587`) into `self._matrix` (RAM copy, not memmap) — moving a store dir cannot corrupt an in-flight search on a loaded matrix; a re-read miss during the swap window is caught by `semantic_search` (returns `[]`).
- `get_store` caches ONE instance per `(os.path.realpath(base_dir), slug)` in module-global `store._STORES` under `store._STORES_LOCK` (`store.py:711-754`). `search.swap_active(slug)` calls `get_store(slug)` and returns the **cached** instance — so the cache MUST be evicted before `swap_active` or it re-serves the pre-swap dir.
- The rebuild engine `_run_rebuild_engine(target_slug, content_mode)` (`migrate.py:709`) builds schema-2 into `_build_base_dir()/{slug}` and is BUILD-ONLY (never `set_active_slug`/`swap_active`). It re-diffs `store.missing()` each pass; `_build/{slug}` empty ⇒ every snapshot missing ⇒ full re-embed.
- Job singleton: `_begin_job` (`migrate.py:157`) raises `RuntimeError` if a job is running (→ route 409). `status.job` = `get_job_status()` (`migrate.py:101`): `{target,state,done,total,started_at,finished_at,error,skipped[],raced[],cancel_requested}`; rebuild jobs add `{kind:"rebuild",activate:false}`.
- `open_migration_target` keys fresh-vs-existing on `store_exists()` (files on disk); an existing v1 store keeps schema 1 (so plain `/migrate` can NOT upgrade a stale store — hence this feature).
- Test harness (mirror `Orchestrator/tests/test_embeddings_migrate_v2.py`): fixtures `env` (tmp index/stores/volume, resets `migrate._JOB/_JOB_TASK/_CANCEL`, `search._active_store`, `BATCH_SLEEP_S=0`), `fake_provider` (monkeypatch `providers._instances[TARGET]`), `cutover_spies` (spies `migrate.set_active_slug` + `search.swap_active`), helpers `_build_volume`, `_long_body`, `_fixed_chunker`, `_read_group_layout`, `_assert_contiguous_groups`, `app` + `TestClient` + `_wait_for_state`. `TARGET="qwen3-embedding-0.6b"` (1024d), `OLD_SLUG="gemini-embedding-001"`.
- Run tests: `python -m pytest Orchestrator/tests/<file> -v` from repo root.

---

# MILESTONE 0 — Backend engine + activation seam

The risky core. All tasks TDD. New test file: `Orchestrator/tests/test_embeddings_reembed.py`.

**Harness (M8 fix — do this at the top of the new test file).** The fixtures/helpers are module-local in `test_embeddings_migrate_v2.py`, NOT in `conftest.py` — a partial copy `NameError`s at collection. Use the **import** form (lower risk; imported pytest fixtures keep their origin-module globals, so `FakeProvider`/`_long_body` resolve without re-declaring them):
```python
from Orchestrator.tests.test_embeddings_migrate_v2 import (   # noqa: F401 — pytest fixtures
    env, fake_provider, cutover_spies, app, _build_volume, _wait_for_state,
    TARGET, TARGET_DIMS, OLD_SLUG,
)
```
plus per-test imports: `asyncio, json, threading, numpy as np, pytest`, `from fastapi.testclient import TestClient`, and `from Orchestrator.embeddings import migrate, search`, `from Orchestrator.embeddings.store import get_store, set_active_slug, get_active_slug`. (`env` resets `migrate._JOB/_JOB_TASK/_CANCEL`, `search._active_store`, `BATCH_SLEEP_S=0` and monkeypatches `config.EMBEDDINGS_STORES_DIR`/`VOL_PATH` to tmp — so every no-`base_dir` call in `_activate_candidate` (`get_active_slug`, `store_dir`, `_build_base_dir`) is hermetic.)

---

### Task 0.1: `store.evict_store` + `store.store_dir` helpers

**Files:**
- Modify: `Orchestrator/embeddings/store.py` (add near `get_store`, ~line 755)
- Test: `Orchestrator/tests/test_embeddings_store.py` (append)

**Step 1 — failing test:**
```python
def test_evict_store_drops_cached_instance(tmp_path, monkeypatch):
    from Orchestrator.embeddings import store as st
    monkeypatch.setattr(st.config, "EMBEDDINGS_STORES_DIR", str(tmp_path))
    a = st.get_store("qwen3-embedding-0.6b")
    assert st.get_store("qwen3-embedding-0.6b") is a          # cached
    assert st.evict_store("qwen3-embedding-0.6b") is True
    assert st.get_store("qwen3-embedding-0.6b") is not a      # fresh instance
    assert st.evict_store("qwen3-embedding-0.6b") is True     # re-cached by prev line
    assert st.evict_store("nonexistent-never-cached") is False

def test_store_dir_resolves_under_base(tmp_path):
    from Orchestrator.embeddings import store as st
    assert st.store_dir("s", base_dir=tmp_path) == tmp_path / "s"
```

**Step 2 — run, expect FAIL** (`AttributeError: module ... has no attribute 'evict_store'`):
`python -m pytest Orchestrator/tests/test_embeddings_store.py -k "evict_store or store_dir_resolves" -v`

**Step 3 — implement** (in `store.py`, after `get_store`):
```python
def store_dir(slug: str, base_dir=None) -> Path:
    """Filesystem dir for a model's store: {base_dir or EMBEDDINGS_STORES_DIR}/{slug}."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    return base / slug


def evict_store(slug: str, base_dir=None) -> bool:
    """Drop the cached VectorStore for (base_dir, slug); True if one was cached.

    The re-embed activation seam renames a store dir out from under its cached
    instance; the next get_store must re-open the NEW dir, so the stale
    (realpath(base), slug) entry has to go. Same realpath keying as get_store.
    """
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    key = (os.path.realpath(base), slug)
    with _STORES_LOCK:
        return _STORES.pop(key, None) is not None
```

**Step 4 — run, expect PASS.** **Step 5 — commit** `feat(embeddings): store.evict_store + store_dir helpers for re-embed activation`.

---

### Task 0.1b: `list_stores` must ignore backup/incoming dirs (M3 — status-shadowing)

**Why:** the retained rollback `{slug}.pre-rebuild.<ts>` is a direct child of the stores base and carries a `meta.json` with `meta["slug"]==slug`. `list_stores` emits it as a SECOND entry with the same slug; `embeddings_routes.py` dedups `{s["slug"]: s}` later-wins and `sorted()` puts the backup after the live dir → the OLD schema/rows overwrite the live entry in `GET /embeddings/status`. Result: a just-upgraded model reports schema 1 forever (until the next re-embed), so the button "looks like it did nothing." The `_build`-invisibility trick does NOT cover this (backups have meta; `_build` root does not).

**Files:** Modify `Orchestrator/embeddings/store.py` `list_stores` (~788); Test `Orchestrator/tests/test_embeddings_store.py`.

**Step 1 — failing test:**
```python
def test_list_stores_ignores_backup_and_incoming_dirs(tmp_path):
    from Orchestrator.embeddings import store as st
    live = st.get_store("qwen3-embedding-0.6b", base_dir=tmp_path)
    import numpy as np
    live.append("SNAP-0", np.zeros(st.EMBEDDING_MODELS["qwen3-embedding-0.6b"]["dims"]))
    # simulate a retained rollback + an interrupted swap sibling, both with meta
    import shutil
    shutil.copytree(tmp_path / "qwen3-embedding-0.6b",
                    tmp_path / "qwen3-embedding-0.6b.pre-rebuild.20260101T000000Z")
    shutil.copytree(tmp_path / "qwen3-embedding-0.6b",
                    tmp_path / "qwen3-embedding-0.6b.incoming")
    slugs = [s["slug"] for s in st.list_stores(tmp_path)]
    assert slugs == ["qwen3-embedding-0.6b"]              # exactly one, the live dir
```

**Step 2 — FAIL** (returns 3 entries). **Step 3 — implement:** in `list_stores`, after `meta = json.loads(...)`, add:
```python
            # A store's live dir is always named EXACTLY its slug; retained
            # rollbacks ({slug}.pre-rebuild.<ts>) and interrupted-swap staging
            # ({slug}.incoming) carry a copied meta.json but a different dir
            # name — they must never shadow the live store in status/list.
            # Safe for dotted slugs (e.g. qwen3-embedding-0.6b): the guard
            # compares the whole dir name, not a prefix.
            if meta["slug"] != child.name:
                continue
```
(place immediately before the `stores.append({...})`).

**Step 4 — PASS. Step 5 — commit** `fix(embeddings): list_stores ignores {slug}.pre-rebuild/.incoming dirs`.

---

### Task 0.1c: harden matrix load + search against a moved store dir (M6 — mid-swap read race)

**Why:** the plan's swap ordering assumes a mid-swap search "returns []". False as written: `_get_matrix_locked` does an unguarded `np.fromfile` after a `.exists()` check (TOCTOU), and `store.search`/`store.count` run OUTSIDE `semantic_search`'s try/except. A query racing the rename can raise `FileNotFoundError` (path gone) or `IndexError` (`ids[i]` vs a larger re-read matrix) and 500 a live query. Make the degradation real.

**Files:** Modify `store.py` `_get_matrix_locked` (~585) + `search.py` `semantic_search` (~334); Test `test_embeddings_store.py`.

**Step 1 — failing test:**
```python
def test_get_matrix_survives_vectors_file_vanishing(tmp_path, monkeypatch):
    from Orchestrator.embeddings import store as st
    import numpy as np
    s = st.get_store("qwen3-embedding-0.6b", base_dir=tmp_path)
    s.append("SNAP-0", np.zeros(st.EMBEDDING_MODELS["qwen3-embedding-0.6b"]["dims"]))
    s._matrix = None                                     # force a re-read
    (tmp_path / "qwen3-embedding-0.6b" / "vectors.f32").unlink()  # dir moved out
    assert s.search([0.0] * st.EMBEDDING_MODELS["qwen3-embedding-0.6b"]["dims"], k=1) == []
```

**Step 2 — FAIL** (`FileNotFoundError`). **Step 3 — implement:** in `_get_matrix_locked`, wrap the load:
```python
    def _get_matrix_locked(self):
        if self._matrix is None and self.vectors_path.exists():
            try:
                self._matrix = np.fromfile(self.vectors_path, dtype="<f4").reshape(
                    -1, self.dims
                )
            except (OSError, ValueError) as e:
                # The dir can be renamed out mid-read during a re-embed swap
                # (TOCTOU after .exists()), or a torn/short file yields a bad
                # reshape — degrade to "no matrix" so search returns [] instead
                # of 500ing a live query.
                print(f"[VECSTORE] {self.slug}: matrix re-read failed ({e}); returning empty")
                self._matrix = None
        return self._matrix
```
Belt-and-braces in `semantic_search` (search.py) — wrap the final `return store.search(...)`:
```python
    try:
        return store.search(query_embedding, k, allowed_ids)
    except Exception as e:  # noqa: BLE001 — a store mutated under us must not 500 search
        print(f"[SEMANTIC] search failed ({e}); returning no results")
        return []
```

**Step 4 — PASS. Step 5 — commit** `fix(embeddings): matrix/search degrade to [] when store dir moves mid-swap`.

---

### Task 0.1d: `VectorStore.close()` — retire the pre-swap instance (M2 — same-slug corruption)

**Why:** re-embed promotes the candidate into the SAME slug/dir. Unlike a model-switch cutover (old/new are different dirs, so a racing mint's append lands harmlessly in the OLD slug's dir), here a mint that captured the OLD instance before the swap would `append` schema-1 state into the PROMOTED schema-2 dir (`self.dir` is fixed at construction), corrupting `ids.json`/`meta.json`. Fail-safe: retire the old instance so a racing append RAISES — `fossils.update_snapshot_index` already catches append failures ("minted without vector"), and the catch-up fill (Task 0.4) re-embeds it correctly into the new store.

**Files:** Modify `store.py` `VectorStore` (add `_closed` init + `close()` + guard the append methods); Test `test_embeddings_store.py`.

**Step 1 — failing test:**
```python
def test_closed_store_rejects_appends(tmp_path):
    from Orchestrator.embeddings import store as st
    import numpy as np
    dims = st.EMBEDDING_MODELS["qwen3-embedding-0.6b"]["dims"]
    s = st.get_store("qwen3-embedding-0.6b", base_dir=tmp_path)
    s.append("SNAP-0", np.zeros(dims))
    s.close()
    import pytest
    with pytest.raises(RuntimeError, match="retired"):
        s.append("SNAP-1", np.zeros(dims))
    with pytest.raises(RuntimeError, match="retired"):
        s.append_many([("SNAP-2", np.zeros(dims))])
```

**Step 2 — FAIL. Step 3 — implement:** in `VectorStore.__init__` add `self._closed = False`; add:
```python
    def close(self) -> None:
        """Retire this instance (re-embed activation): further appends RAISE so a
        mint that still holds this handle cannot write stale state into the dir
        that a same-slug promotion just replaced. Reads degrade via _get_matrix."""
        with self._lock:
            self._closed = True
            self._matrix = None
```
At the top of each mutating method (`append`, `append_many`, `append_group`), under `self._lock`, first line:
```python
            if self._closed:
                raise RuntimeError(f"{self.slug}: store instance retired (re-embed activated)")
```
*(Executor: confirm each append method already takes `self._lock`; put the guard just inside it.)*

**Step 4 — PASS** (and re-run `test_embeddings_store.py` + `_v2` — no regression). **Step 5 — commit** `feat(embeddings): VectorStore.close() retires the pre-swap instance`.

---

### Task 0.2: `_clear_build_candidate` — guarantee a fresh full rebuild

**Files:** Modify `Orchestrator/embeddings/migrate.py`; Test `test_embeddings_reembed.py`.

**Step 1 — failing test:**
```python
@pytest.mark.asyncio
async def test_clear_build_candidate_removes_dir_and_cache(env, fake_provider):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)
    await migrate.run_rebuild(TARGET)                 # populate _build/{slug}
    bdir = stores_dir / migrate.BUILD_DIR_NAME / TARGET
    assert bdir.exists()
    migrate._clear_build_candidate(TARGET)
    assert not bdir.exists()
    # cache evicted: a fresh get_store re-creates empty (0 rows)
    from Orchestrator.embeddings.store import get_store
    fresh = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME, schema=2)
    assert fresh.missing(["SNAP-0", "SNAP-1"]) == ["SNAP-0", "SNAP-1"]
```

**Step 2 — run, expect FAIL.**

**Step 3 — implement** (in `migrate.py`, import `shutil`, `store.evict_store`, `store.store_dir`):
```python
def _clear_build_candidate(target_slug: str) -> None:
    """Delete {stores}/_build/{slug} + evict its cache so a re-embed is a TRUE
    full rebuild, never a top-up of a stale candidate (a prior candidate may
    predate a chunk-config change). Called only on the INITIAL reembed entry —
    resume tops up the in-progress candidate instead."""
    from Orchestrator.embeddings.store import evict_store, store_dir
    evict_store(target_slug, base_dir=_build_base_dir())
    d = store_dir(target_slug, base_dir=_build_base_dir())
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
```

**Step 4 — PASS. Step 5 — commit** `feat(embeddings): _clear_build_candidate for fresh re-embed`.

---

### Task 0.3: `_prune_old_rollbacks` — keep newest N `.pre-rebuild` dirs

**Files:** Modify `migrate.py`; Test `test_embeddings_reembed.py`.

**Step 1 — failing test:**
```python
def test_prune_old_rollbacks_keeps_newest(env):
    _, stores_dir, _ = env
    stores_dir.mkdir(parents=True, exist_ok=True)
    for ts in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (stores_dir / f"{TARGET}.pre-rebuild.{ts}").mkdir()
    migrate._prune_old_rollbacks(TARGET, keep=1)
    remaining = sorted(p.name for p in stores_dir.glob(f"{TARGET}.pre-rebuild.*"))
    assert remaining == [f"{TARGET}.pre-rebuild.20260103T000000Z"]
```

**Step 2 — FAIL. Step 3 — implement:**
```python
def _prune_old_rollbacks(target_slug: str, keep: int = 1) -> None:
    """Retain the newest `keep` {slug}.pre-rebuild.<ts> dirs; delete older ones.
    Lexical sort works because the suffix is a UTC compact-ISO timestamp."""
    base = Path(config.EMBEDDINGS_STORES_DIR)
    backups = sorted(base.glob(f"{target_slug}.pre-rebuild.*"))
    for old in backups[:-keep] if keep else backups:
        shutil.rmtree(old, ignore_errors=True)
```

**Step 4 — PASS. Step 5 — commit** `feat(embeddings): _prune_old_rollbacks retention`.

---

### Task 0.4: `_catch_up_fill` — embed gate-window mints into the promoted store

**Files:** Modify `migrate.py`; Test `test_embeddings_reembed.py`.

**Rationale:** during a ~minutes rebuild, live mints append to the OLD active store. After promotion the new store is missing them; one v2 diff-fill closes the gap. Non-fatal: a provider failure here is healed later by the watcher.

**Step 1 — failing test** (M7 fix — seed exactly 2 of 3 so the test actually exercises the DELTA, not "fill an empty store"; assertion and narrative must agree):
```python
@pytest.mark.asyncio
async def test_catch_up_fill_embeds_only_the_gate_window_mint(env, fake_provider):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    import Orchestrator.embeddings.search as search
    import numpy as np
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    store = get_store(TARGET, base_dir=stores_dir, schema=2)   # active v2 store
    # seed SNAP-0 and SNAP-1 (append_group requires schema 2); SNAP-2 is the
    # snapshot minted during the rebuild window and is the ONLY thing to fill.
    store.append_group("SNAP-0", [np.zeros(TARGET_DIMS)])
    store.append_group("SNAP-1", [np.zeros(TARGET_DIMS)])
    assert store.missing(sorted(bodies)) == ["SNAP-2"]
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = store

    filled = await migrate._catch_up_fill(TARGET)

    assert filled == 1                        # ONLY the gate-window mint
    assert store.missing(sorted(bodies)) == []
    assert "SNAP-2" in store.ids()
    # proof SNAP-0/1 were not re-embedded: the provider only saw SNAP-2's body
    assert all("snap02" in t for texts, _ in fake_provider.calls for t in texts) \
        or any("snap02" in t for t in fake_provider.embedded_texts)
```
*(The same-cached-instance guarantee holds: `_catch_up_fill`'s `get_store(TARGET)` (no base_dir) keys on `realpath(config.EMBEDDINGS_STORES_DIR)` == the monkeypatched `stores_dir`, so it sees the seeded rows.)*

**Step 2 — FAIL. Step 3 — implement:**
```python
async def _catch_up_fill(target_slug: str) -> int:
    """Diff-and-fill any snap_ids missing from the now-active schema-2 store
    (mints that landed during the rebuild). Returns snapshots filled. Best-
    effort: provider failure logs + returns partial; the watcher heals the rest."""
    from Orchestrator.fossils import load_snapshot_index
    from Orchestrator.embeddings.store import get_store
    store = get_store(target_slug)                     # live (post-evict) instance
    index = await asyncio.to_thread(load_snapshot_index)
    missing = store.missing(sorted(index.keys()))
    if not missing:
        return 0
    vol_bytes = await asyncio.to_thread(read_volume_bytes, Path(config.VOL_PATH))
    ids_texts = []
    for sid in missing:
        text = slice_snapshot_text(sid, index, vol_bytes)
        if text is not None:
            ids_texts.append((sid, text))
    if not ids_texts:
        return 0
    provider = get_provider(target_slug)
    appended, _quarantined = await _fill_v2_batch(
        store, provider, ids_texts, target_slug
    )
    print(f"[MIGRATE] re-embed catch-up filled {len(appended)} gate-window mint(s)")
    return len(appended)
```

**Step 4 — PASS. Step 5 — commit** `feat(embeddings): _catch_up_fill for re-embed activation`.

---

### Task 0.5: `_activate_candidate` — atomic promote of the rebuilt candidate

**Files:** Modify `migrate.py` (import `os`); Test `test_embeddings_reembed.py`.

**Step 1 — failing tests** (two: non-active promote, active promote):
```python
@pytest.mark.asyncio
async def test_activate_candidate_non_active_swaps_dir_no_search_repoint(
    env, fake_provider, cutover_spies
):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    set_active_slug(OLD_SLUG, base_dir=stores_dir)        # a DIFFERENT active model
    await migrate.run_rebuild(TARGET)                     # candidate under _build
    cand = stores_dir / migrate.BUILD_DIR_NAME / TARGET
    assert cand.exists()

    await migrate._activate_candidate(TARGET)

    # candidate promoted to live dir; _build/{slug} gone
    live = stores_dir / TARGET
    assert (live / "meta.json").exists() and not cand.exists()
    promoted = get_store(TARGET, base_dir=stores_dir)
    assert promoted.schema == 2 and promoted.missing(sorted(bodies)) == []
    # NON-active: live search NOT repointed, active pointer unchanged
    assert cutover_spies["swap_active"] == []
    assert get_store  # sanity
    from Orchestrator.embeddings.store import get_active_slug
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG

@pytest.mark.asyncio
async def test_activate_candidate_active_repoints_search_and_catches_up(
    env, fake_provider, monkeypatch
):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    import Orchestrator.embeddings.search as search
    import numpy as np
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    # materialize a live v1 store so a rollback dir is actually created on swap
    seed = get_store(TARGET, base_dir=stores_dir)
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)          # TARGET IS active
    await migrate.run_rebuild(TARGET)
    caught = {"n": 0}
    async def _spy_catch(slug):
        caught["n"] += 1; return 0
    monkeypatch.setattr(migrate, "_catch_up_fill", _spy_catch)

    await migrate._activate_candidate(TARGET)

    assert search._active_store is not None
    assert search._active_store.slug == TARGET
    assert search._active_store.schema == 2              # live handle is the new store
    assert caught["n"] == 1                              # catch-up ran for active model
    # rollback retained
    assert list((stores_dir).glob(f"{TARGET}.pre-rebuild.*"))
```

**Step 2 — FAIL. Step 3 — implement** (hardened per M2/M4/M5):
```python
async def _activate_candidate(target_slug: str) -> None:
    """Promote {stores}/_build/{slug} into the live store dir, in-service.

    The half M6f made manual. Renames are same-filesystem (both under
    EMBEDDINGS_STORES_DIR). Reads are protected by Task 0.1c (matrix re-read
    degrades to []); a same-slug write race is protected by retiring the old
    instance below (Task 0.1d) so a mint holding it fails safe.
    """
    from Orchestrator.embeddings.store import (
        evict_store, get_active_slug, store_dir,
    )
    is_active = get_active_slug() == target_slug
    live = store_dir(target_slug)
    cand = store_dir(target_slug, base_dir=_build_base_dir())
    # M4: an empty corpus (fresh box, or a model whose index has 0 embeddable
    # snapshots) builds nothing, so the candidate dir never materialized. That
    # is a successful NO-OP, not a failure — a dead provider already stalled at
    # the zero-progress guard before reaching here, so a missing candidate here
    # can only mean "nothing to embed". Do NOT raise.
    if not cand.exists():
        print(f"[MIGRATE] re-embed {target_slug}: empty corpus — nothing to activate")
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = live.parent / f"{target_slug}.pre-rebuild.{ts}"
    incoming = live.parent / f"{target_slug}.incoming"

    # M2: capture the pre-swap active instance so we can retire it AFTER the
    # repoint — a mint that still holds it will then fail its append safely
    # (fossils catches it → minted vector-less → the catch-up fill re-embeds it).
    old_active = search.get_active_store() if is_active else None

    def _swap() -> None:
        live.parent.mkdir(parents=True, exist_ok=True)
        # M5: a prior activation may have crashed mid-swap leaving a stale
        # non-empty {slug}.incoming; os.replace onto a non-empty dir raises
        # ENOTEMPTY, so clear it first.
        shutil.rmtree(incoming, ignore_errors=True)
        os.replace(cand, incoming)          # _build/{slug} → stores/{slug}.incoming
        if live.exists():
            os.replace(live, backup)        # stores/{slug} → stores/{slug}.pre-rebuild.<ts>
        os.replace(incoming, live)          # stores/{slug}.incoming → stores/{slug}
    await asyncio.to_thread(_swap)

    # Evict BOTH cached instances so the next open reads the promoted dir.
    evict_store(target_slug)                             # live base
    evict_store(target_slug, base_dir=_build_base_dir()) # _build base

    if is_active:
        search.swap_active(target_slug)                 # reopen fresh live handle
        if old_active is not None:
            old_active.close()                          # M2: retire → racing appends fail safe
        try:
            await _catch_up_fill(target_slug)
        except Exception as e:  # noqa: BLE001 — activation must not fail on catch-up
            print(f"[MIGRATE] re-embed catch-up failed (non-fatal, watcher heals): {e}")
    _prune_old_rollbacks(target_slug, keep=1)
    print(f"[MIGRATE] re-embed activated: {target_slug} store is now chunked/live "
          f"(active={is_active})")
```

**Step 4 — PASS. Step 5 — commit** `feat(embeddings): _activate_candidate in-service candidate promote (retire-old + empty-corpus no-op + stale-incoming cleanup)`.

**Add these hardening tests to the same file (M2/M4/M5 regressions):**
```python
@pytest.mark.asyncio
async def test_reembed_empty_corpus_is_done_noop(env, fake_provider):
    from Orchestrator.embeddings.store import set_active_slug
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=0)          # 0 snapshots
    set_active_slug(TARGET, base_dir=stores_dir)
    result = await migrate.run_reembed(TARGET)
    assert result["state"] == "done"                     # NOT stalled
    assert not list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))

@pytest.mark.asyncio
async def test_activation_survives_stale_incoming(env, fake_provider):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    seed = get_store(TARGET, base_dir=stores_dir)
    import numpy as np
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    stores_dir.mkdir(parents=True, exist_ok=True)
    stale = stores_dir / f"{TARGET}.incoming"; stale.mkdir()
    (stale / "junk").write_text("x")                     # non-empty stale staging
    result = await migrate.run_reembed(TARGET)
    assert result["state"] == "done"
    assert get_store(TARGET, base_dir=stores_dir).schema == 2

@pytest.mark.asyncio
async def test_racing_mint_cannot_corrupt_promoted_store(env, fake_provider, monkeypatch):
    """M2: a mint holding the pre-swap active instance appends AFTER the swap;
    the retired instance rejects it (fossils would drop it) so the promoted
    schema-2 store stays intact."""
    from Orchestrator.embeddings.store import get_store, set_active_slug
    import Orchestrator.embeddings.search as search
    import numpy as np
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    seed = get_store(TARGET, base_dir=stores_dir)
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = seed
    captured = search.get_active_store()                 # what a mid-flight mint holds
    await migrate.run_reembed(TARGET)                    # promotes + retires `seed`
    import pytest
    with pytest.raises(RuntimeError, match="retired"):
        captured.append("SNAP-LATE", np.zeros(TARGET_DIMS))
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2 and live.missing(sorted(bodies)) == []
```

---

### Task 0.6: wire `activate` through `_run_rebuild_engine` / `_begin_job` / `_launch` + `start_reembed`/`run_reembed`

**Files:** Modify `migrate.py`; Test `test_embeddings_reembed.py`.

**Step 1 — failing test (end-to-end engine, run to completion):**
```python
@pytest.mark.asyncio
async def test_run_reembed_active_model_full_flow(env, fake_provider):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    import Orchestrator.embeddings.search as search
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=4)
    # stale schema-1 store IS active (the upgrade case)
    seed = get_store(TARGET, base_dir=stores_dir)          # v1 (autodetect fresh→v1)
    import numpy as np
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = get_store(TARGET, base_dir=stores_dir)
    assert search._active_store.schema == 1               # starts stale/whole-doc

    result = await migrate.run_reembed(TARGET)

    assert result["state"] == "done"
    assert result["kind"] == "reembed" and result["activate"] is True
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2                                # upgraded in place
    assert live.rows > live.snapshots                      # chunk groups
    assert live.missing(sorted(bodies)) == []
    assert search._active_store.schema == 2                # live search on new store
    assert list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))  # rollback kept
```

**Step 2 — FAIL. Step 3 — implement the wiring:**

3a. `_begin_job` — extend the kind block (`migrate.py:188`):
```python
        if kind in ("rebuild", "reembed"):
            _JOB["kind"] = kind
            _JOB["activate"] = (kind == "reembed")
            if kind == "reembed":
                _JOB["phase"] = "building"
            if content_mode != "full":
                _JOB["content_mode"] = content_mode
```

3b. `_launch` — add `activate` param (`migrate.py:210`):
```python
def _launch(target_slug, rebuild=False, content_mode="full", activate=False):
    ...
    if rebuild:
        coro = _run_rebuild_engine(target_slug, content_mode, activate=activate)
    else:
        coro = _run_engine(target_slug)
    ...
```

3c. `_run_rebuild_engine` — add `activate=False`. Replace the existing build-only done-finish (migrate.py:858) AND guard the trailing completion print (migrate.py:859-863) which currently reads `target.dir` (stale after the swap):
```python
        cand_rows, cand_snaps = target.rows, target.snapshots
        if activate:
            _update_job(persist=True, phase="activating")   # UI: "activating…" + hide cancel
            await _activate_candidate(target_slug)
            # m1: clear the phase on the terminal state so a DONE reembed does
            # not still read phase=='activating' (the UI keys the sub-label on it).
            _finish_job("done", phase="done", rows=cand_rows, snapshots=cand_snaps)
        else:
            _finish_job("done", rows=cand_rows, snapshots=cand_snaps)
            # m2: the "candidate at {target.dir}; activation is a separate step"
            # print is only TRUE for a build-only run — keep it here, inside the
            # else (after activation target.dir no longer exists and the message
            # is false). Move migrate.py:859-863's print into this else-branch.
```
*(Capture `cand_rows/cand_snaps` BEFORE `_activate_candidate` moves the dir. `phase` is set ONLY on the `activate` branch, so a plain rebuild's persisted dict stays exactly `{kind,activate}+base` — this is true by construction; note the previously-cited `REBUILD_JOB_KEYS` assertion does NOT actually exist in `test_embeddings_migrate_v2.py`, so do not rely on it as a guard.)*

3d. New public entries (near `start_rebuild`). **M1: claim the singleton BEFORE the destructive `_clear_build_candidate`** — otherwise a re-embed POST for a slug that already has a job building `_build/{slug}` would `rmtree` that in-flight candidate out from under the running job and THEN return 409 (a 409 must be side-effect-free). Mirror `start_rebuild`, which claims first and never touches disk before the claim:
```python
async def start_reembed(target_slug: str, content_mode: str = "full") -> dict:
    """Route entry: full re-embed of target_slug's store under the current chunk
    strategy, THEN activate it in-service (candidate-swap). Same singleton/404/409
    semantics as start_rebuild; the claim is synchronous before create_task."""
    if target_slug not in EMBEDDING_MODELS:
        raise ValueError(f"unknown embedding model slug {target_slug!r}; "
                         f"known: {sorted(EMBEDDING_MODELS)}")
    _begin_job(target_slug, kind="reembed", content_mode=content_mode)  # claim FIRST (→409)
    _clear_build_candidate(target_slug)                 # now safe: we own the singleton
    _launch(target_slug, rebuild=True, content_mode=content_mode, activate=True)
    return get_job_status()


async def run_reembed(target_slug: str, content_mode: str = "full") -> dict:
    """Run a re-embed to completion in this coroutine (resume + tests).

    NOTE: not wired to backfill_embeddings.py — there is no `--reembed` CLI flag
    (out of scope for M0; add a mode mirroring `_run_rebuild_cli` if ops parity
    is wanted later). Do not advertise CLI use."""
    if target_slug not in EMBEDDING_MODELS:
        raise ValueError(f"unknown embedding model slug {target_slug!r}; "
                         f"known: {sorted(EMBEDDING_MODELS)}")
    _begin_job(target_slug, kind="reembed", content_mode=content_mode)  # claim FIRST
    _clear_build_candidate(target_slug)
    return await _run_rebuild_engine(target_slug, content_mode, activate=True)
```

**Step 4 — PASS** (plus re-run `test_embeddings_migrate_v2.py` to prove the rebuild path is unregressed: `activate` defaults False everywhere). **Step 5 — commit** `feat(embeddings): start_reembed/run_reembed — rebuild + in-service activate`.

---

### Task 0.7: resume a `reembed` job across restart (activation-aware)

**Files:** Modify `migrate.resume_if_interrupted` (`migrate.py:340`); Test `test_embeddings_reembed.py`.

**Step 1 — failing test** (mirror `test_boot_resume_of_rebuild_stays_build_only`, but assert it DOES activate):
```python
@pytest.mark.asyncio
async def test_boot_resume_of_reembed_activates(env, fake_provider):
    from Orchestrator.embeddings.store import get_store, set_active_slug, get_active_slug
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    set_active_slug(TARGET, base_dir=stores_dir)
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / migrate.STATE_FILE).write_text(json.dumps({
        "target": TARGET, "state": "running", "kind": "reembed", "activate": True,
        "phase": "building", "done": 0, "total": 3,
        "started_at": "2026-07-01T00:00:00+00:00", "finished_at": None,
        "error": None, "skipped": [], "raced": [],
    }), encoding="utf-8")

    task = migrate.resume_if_interrupted()
    assert task is migrate._JOB_TASK
    await task

    status = migrate.get_job_status()
    assert status["state"] == "done" and status["kind"] == "reembed"
    assert get_store(TARGET, base_dir=stores_dir).schema == 2   # activated on resume
```

**Step 2 — FAIL. Step 3 — implement** (extend the rebuild branch to cover reembed; note resume does NOT `_clear_build_candidate` — it tops up the in-progress candidate):
```python
    if persisted.get("kind") in ("rebuild", "reembed"):
        kind = persisted["kind"]
        content_mode = persisted.get("content_mode", "full")
        activate = (kind == "reembed")
        print(f"[MIGRATE] resuming interrupted {kind} of {target} "
              f"(activate={activate}, content_mode={content_mode})")
        _begin_job(target, kind=kind, content_mode=content_mode)
        return _launch(target, rebuild=True, content_mode=content_mode,
                       activate=activate)
```

**Step 4 — PASS** (and confirm `test_boot_resume_of_rebuild_stays_build_only` still passes — rebuild stays build-only). **Step 5 — commit** `feat(embeddings): resume re-embed jobs activation-aware`.

---

### Task 0.8: cancel during build leaves the live store intact

**Files:** Test only (`test_embeddings_reembed.py`) — behavior already emerges from `_CANCEL` checks in the build loop; this pins it.

**Step 1 — test:**
```python
@pytest.mark.asyncio
async def test_reembed_cancel_during_build_leaves_active_store_untouched(
    env, fake_provider, cutover_spies
):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=4)
    seed = get_store(TARGET, base_dir=stores_dir)
    import numpy as np
    seed.append("SNAP-0", np.zeros(TARGET_DIMS))     # a real v1 active store
    set_active_slug(TARGET, base_dir=stores_dir)
    fake_provider.hook = lambda _texts: migrate.request_cancel()

    result = await migrate.run_reembed(TARGET)

    assert result["state"] == "cancelled"
    assert cutover_spies["swap_active"] == []          # never activated
    assert get_store(TARGET, base_dir=stores_dir).schema == 1   # live store untouched
    assert not list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))  # no promote happened
```
**Step 2 — run, expect PASS** (the build loop's `_CANCEL` check returns before activation). If it fails, fix `_run_rebuild_engine` so `activate` is only honored on the `state=="done"` path (cancel/stall return earlier — verify they do). **Step 3 — commit** `test(embeddings): re-embed cancel is non-destructive`.

---

### Task 0.9: `POST /embeddings/reembed` route

**Files:** Modify `Orchestrator/routes/embeddings_routes.py` (add `ReembedRequest` + route near `/migrate`, import `start_reembed`); Test `test_embeddings_reembed.py` (route tests via `app`/`TestClient`).

**Step 1 — failing tests:**
```python
def test_route_reembed_starts_and_activates(env, fake_provider, app):
    from Orchestrator.embeddings.store import get_store, set_active_slug
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    set_active_slug(TARGET, base_dir=stores_dir)
    with TestClient(app) as client:
        resp = client.post("/embeddings/reembed", json={"target": TARGET})
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "running"
        assert body["kind"] == "reembed" and body["activate"] is True
        _wait_for_state("done")
    assert get_store(TARGET, base_dir=stores_dir).schema == 2

def test_route_reembed_unknown_slug_404(env, app):
    with TestClient(app) as client:
        assert client.post("/embeddings/reembed",
                           json={"target": "no-such"}).status_code == 404

def test_route_reembed_409_is_side_effect_free(env, fake_provider, app):
    """M1: a 409 must NOT run _clear_build_candidate — an in-flight candidate
    for the same slug must survive the rejected request."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=1)
    # a sentinel standing in for another job's in-progress _build/{TARGET}
    cand = stores_dir / migrate.BUILD_DIR_NAME / TARGET
    cand.mkdir(parents=True, exist_ok=True)
    (cand / "in_progress.sentinel").write_text("x")
    migrate._begin_job(TARGET, kind="rebuild")           # occupy the singleton
    with TestClient(app) as client:
        assert client.post("/embeddings/reembed",
                           json={"target": TARGET}).status_code == 409
    assert (cand / "in_progress.sentinel").exists()      # NOT wiped by the 409
    migrate._finish_job("cancelled")
```

**Step 2 — FAIL. Step 3 — implement** (in `embeddings_routes.py`, import `start_reembed` alongside the migrate imports):
```python
class ReembedRequest(BaseModel):
    target: str


@router.post("/reembed")
async def embeddings_reembed(req: ReembedRequest):
    """Full re-embed of target's store under the current chunk strategy, then
    activate it in-service (candidate build → atomic dir-swap → live). 404
    unknown slug, 409 when a job is already running; else the running job dict."""
    if req.target not in EMBEDDING_MODELS:
        raise HTTPException(status_code=404,
                            detail=f"Unknown embedding model slug: {req.target!r}")
    try:
        return await start_reembed(req.target)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
```

**Step 4 — PASS. Step 5 — commit** `feat(embeddings): POST /embeddings/reembed route`.

---

### Task 0.10: full backend suite green (regression gate)

Run and confirm ALL pass (no regressions to migrate/rebuild/store/routes):
```
python -m pytest Orchestrator/tests/test_embeddings_reembed.py \
  Orchestrator/tests/test_embeddings_migrate.py \
  Orchestrator/tests/test_embeddings_migrate_v2.py \
  Orchestrator/tests/test_embeddings_store.py \
  Orchestrator/tests/test_embeddings_store_v2.py \
  Orchestrator/tests/test_embeddings_routes.py -v
```
Commit nothing new unless a regression fix is needed. **Milestone gate:** all green before touching UI.

---

# MILESTONE 1 — Backend: per-model strategy surfacing

The status already emits `models[].schema` + `missing`. Confirm, and add a derived label the UI can render without recomputing.

### Task 1.1: `models[].strategy` in `GET /embeddings/status`
**Files:** Modify the model-assembly in `embeddings_routes.py`; Test `test_embeddings_routes.py`.
- Add per model: `"strategy": "chunked" if schema == 2 else ("whole_document" if schema == 1 else "none")` (derive from the same `schema` already computed; `"none"` when no store).
- **BLOCKER (B1) — same commit:** `test_embeddings_routes.py::test_status_shape` asserts `set(model.keys()) == MODEL_KEYS` against a FROZEN set at `test_embeddings_routes.py:34-39`. Adding `strategy` makes that assertion fail deterministically. Add `"strategy"` to `MODEL_KEYS` in the SAME commit (additive contract change → the exact-key gate must reflect it).
- **Test:** a model with a schema-2 store reports `strategy=="chunked"`; a v1 store reports `"whole_document"`; a model with no store reports `"none"`. Re-run the FULL `test_embeddings_routes.py` and confirm `test_status_shape` passes with the extended `MODEL_KEYS`.
- **Commit** `feat(embeddings): status models[].strategy (+ MODEL_KEYS) for the re-embed UI`.

---

# MILESTONE 2 — Onboarding wizard UI (`Portal/onboarding/steps/embeddings.js`)

Wizard first (D3). Reuse `renderJobPanel`/`updateJobPanel`/`etaText`/`cancelMigration` unchanged — the panel header already reads "Re-embedding to <target>". JS parity/behavior tests follow the pattern in `Orchestrator/tests/test_portal_embeddings_card_parity.py` (source-level assertions) — the executor reads that file first to match the harness.

### Task 2.1: strategy line on every embedding card
- In `renderCard()` (`embeddings.js:566`), render `m.strategy` → "Strategy: chunked (current)" / "Whole-document (older) — re-embed to upgrade" (with the existing superseded/freshness accent class) / "Not built yet."
- Test (parity-style): the rendered card HTML for a `schema:1` model contains the "Whole-document (older)" string + the accent class.
- Commit `feat(portal-onboarding): per-card embedding strategy line`.

### Task 2.2: Re-embed button in `cardActionsHtml` + `wireCardActions`
- `cardActionsHtml()` (`embeddings.js:684`): for the ACTIVE card (currently only the static "Currently active" blurb) AND any built non-active card, emit a `#ob-emb-reembed` button "Re-embed all snapshots" — disabled with a hint when `status.job && status.job.state==='running'`.
- `wireCardActions()` (`embeddings.js:750`): wire `#ob-emb-reembed` → new `startReembed(slug)`.
- `startReembed(slug)`: for cloud models (`m.privacy!=='local'`) show a confirm with snapshot count + est. cost (Task 2.3) first; then `POST /embeddings/reembed {target:slug}`; on `200` seed `status.job` from the body + `routeRender()`; on `409` `refreshStatus()`+`routeRender()` to attach — mirror `startMigrate` (`embeddings.js:910-926`) exactly.
- Test: card HTML for a built model contains `#ob-emb-reembed`; `startReembed` issues `POST /embeddings/reembed`; a `409` re-renders instead of erroring.
- Commit `feat(portal-onboarding): per-card Re-embed button wired to /embeddings/reembed`.

### Task 2.3: cloud confirm-with-cost dialog (D2)
- Before POST for a cloud model, render a small inline confirm: "Re-embed N snapshots with <model>? Est. ~$X on your <provider> key." `N` from the model's snapshot `count`; `X ≈ (corpus_chars/4/1e6) * cost_per_1m_tokens` (approx; label it an estimate). Local models: skip confirm, surface the existing `cpu_warning`.
- Test: cloud card path renders the confirm + does NOT POST until confirmed; local card path POSTs immediately.
- Commit `feat(portal-onboarding): cloud re-embed cost confirm`.

### Task 2.4: progress panel `phase` sub-label + hide Cancel during activate
- In `updateJobPanel()`/`renderJobPanel()`: when `job.kind==='reembed'` show the header "Re-embedding <target>"; when **`job.state==='running' && job.phase==='activating'`** show a small "activating…" sub-label and hide the Cancel button. **Gate on `state==='running'` (m1)** — the backend sets `phase:'done'` on the terminal state (Task 0.6 3c), but gating defensively means a stray `phase` never renders a finished job as "still activating".
- Completion copy in `renderJobDone`: "<model> re-embedded (N snapshots, chunked) — now live" (active) / "…— ready to switch to" (non-active).
- Test: a `job` with `state:'running', phase:'activating'` renders no cancel control; a `state:'done'` reembed renders the completion copy (and NO "activating…").
- Commit `feat(portal-onboarding): re-embed phase label + activation cancel-hide`.

### Task 2.5: wizard behavior suite green
Run the onboarding/portal embeddings JS parity tests + any `test_embeddings_onboarding_step.py`; all green. Commit only if a fix is needed.

---

# MILESTONE 3 — Live + device validation, snapshot

### Task 3.1: live verify on this box (service running)
- Restart service. `curl -s localhost:9091/embeddings/status | jq '.models[]|{slug,schema,strategy,missing}'`.
- Re-embed a NON-active model via `curl -X POST localhost:9091/embeddings/reembed -d '{"target":"<non-active-slug>"}'`; poll `/embeddings/status .job`; confirm on done: that model's `schema==2`, `active` unchanged, a `<slug>.pre-rebuild.*` dir exists under the stores dir, `_build/<slug>` gone.
- Re-embed the ACTIVE model; confirm `schema==2`, `missing==0`, live search still answers (`/debug/context` or a search), journal shows `[MIGRATE] re-embed activated`.
- Tail journal for `[EMBEDDING]`/`[MIGRATE]` lines; no stalls.

### Task 3.2: Fold device-validate the wizard
Open the onboarding Memory step on the Fold; run a re-embed on a small model; watch the progress panel + completion; confirm switchability afterward.

### Task 3.3: snapshot
`/snapshot-dev` (operator resolved) documenting the shipped backend + wizard, `POST /embeddings/reembed`, the activation seam, and the rollback dir contract. Commit + push per Brandon's flow after acceptance.

---

# MILESTONE 4 — Portal updates-manager parity (follow-on)
Add the Re-embed affordance to `Portal/modules/updates-manager.js` embeddings card (5s poll, one-line `_embedProgressLine`, 409-attach). Mirror Task 2.2–2.4 at that surface's cadence. Parity test: `test_portal_embeddings_card_parity.py`.

# MILESTONE 5 — Android parity (follow-on)
`EmbeddingsCard`/`UpdatesViewModel`/`UpdateRepository`/`EmbeddingsStatus.kt`: Re-embed action + `POST /embeddings/reembed` + `kind/phase` fields + strategy line + cloud confirm. Build (`./gradlew :app:testDebugUnitTest --offline`) + device-validate. Parity test: `test_android_embeddings_card_parity.py`.

---

## Risks (post-adversarial; each has an owning task + test)
- **Mid-swap READ race (M6 → Task 0.1c):** matrix is RAM-resident (`np.fromfile`) but can be re-read from a moved path (TOCTOU) → `_get_matrix_locked` + `semantic_search` are hardened to degrade to `[]`, not 500. Do NOT convert the store to memmap.
- **Same-slug WRITE race (M2 → Task 0.1d + 0.5):** a mint holding the pre-swap instance would corrupt the promoted dir; the old instance is `close()`d after the swap so its append fails safe (fossils drops → catch-up re-embeds). Unlike a model-switch cutover (different dirs), re-embed reuses the slug — this is the one genuinely new race.
- **Cache eviction is load-bearing:** `evict_store` for BOTH the live and `_build` keys must precede `swap_active`, or the stale cached instance re-serves the old dir.
- **Status shadowing (M3 → Task 0.1b):** the retained `.pre-rebuild.<ts>` dir must not appear in `list_stores` or it overwrites the live model's schema/rows in `/embeddings/status`.
- **Stale `.incoming` (M5 → Task 0.5):** cleared at the top of `_swap` so a resumed activation can't `ENOTEMPTY`-stall.
- **Empty corpus (M4 → Task 0.5):** 0 embeddable snapshots is a `done` no-op, never a false `stalled`.
- **409 side-effects (M1 → Task 0.6):** claim the singleton BEFORE `_clear_build_candidate`, so a rejected request never wipes another job's in-flight candidate.
- **Singleton:** one re-embed at a time (409-attach on every surface). All cards' buttons disable while a job runs.
- **Cloud billing:** confirm-with-cost gate (Task 2.3).

## Execution Handoff
Plan saved to `docs/plans/2026-07-06-embeddings-per-card-reembed.md`. After the adversarial verification pass, proceed via **superpowers:subagent-driven-development** (this session): fresh subagent per task + code review between tasks, starting at Task 0.1.
