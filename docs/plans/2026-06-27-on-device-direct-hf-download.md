# On-Device Direct-from-Hugging-Face Download Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the Android MVP download on-device Gemma bundles *directly from the Hugging Face CDN* into app-private storage (deleting the hub mirror proxy that causes the stuck-at-0%/1% bug), auto-populate the model catalog from the HF Hub API, and make the phone download durable via a foreground Service.

**Architecture:** The hub stops moving bytes. `Orchestrator/local_provider/mirror.py` is renamed to `catalog.py` and becomes a *discovery + curation* module: it queries the HF Hub API for `litert-community` Gemma `*-it-litert-lm` repos, merges real `size_bytes`/`sha256` (from `lfs.oid`)/`gated` onto a curated config floor, caches with a TTL, and serves `GET /local/models/catalog` with new `download_url` + `gated` fields. The phone downloads bytes from `download_url` (HF CDN) — keeping the existing `.part`+`Range` resume — inside a foreground `ModelDownloadService` that publishes progress to a pure-Kotlin `DownloadProgressBus` the ViewModel observes. The hub byte-proxy endpoint, `mirror_store/`, and the fetch machinery are deleted.

**Tech Stack:** Python/FastAPI/Starlette + `requests` (backend, hermetic-mocked in tests); Kotlin/Compose, OkHttp 5.3.2, kotlinx-serialization, foreground Service + `ServiceCompat`, MockWebServer3 + coroutines-test (Android).

**Conventions:**
- Build on `main`, no worktree (staging-as-prod box).
- Production-quality + fresh-box safe; slugs stay stable (`gemma-4-e2b`/`gemma-4-e4b`) so existing sidecars/attestations keep mapping.
- Backend tests: `Orchestrator/venv/bin/python -m pytest <path> -v` from repo root.
- Android tests: from `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/`, run `./gradlew :app:testDebugUnitTest --tests "<FQN>"`.
- Stage explicit paths only — **never `git add -A`** (sweeps untracked local files).
- Design doc: `docs/plans/2026-06-27-on-device-direct-hf-download-design.md`.

**Paths (constants used throughout):**
- `REPO = /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`
- `AND = $REPO/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`
- `KT = $AND/app/src/main/java/com/aiblackbox/portal`
- `KTEST = $AND/app/src/test/java/com/aiblackbox/portal`

---

## Phase A — Backend: HF discovery + curation (replaces the mirror byte-mover)

### Task A1: Rename `mirror.py` → `catalog.py`, repoint imports (no behaviour change yet)

**Files:**
- Rename: `Orchestrator/local_provider/mirror.py` → `Orchestrator/local_provider/catalog.py` (`git mv`)
- Modify: `Orchestrator/routes/local_routes.py:31` (import), `:132`, `:160`, `:166`
- Rename test: `Orchestrator/tests/test_local_mirror.py` → `Orchestrator/tests/test_local_discovery.py` (`git mv`; rewritten in A5/A6)

**Step 1:** `git mv Orchestrator/local_provider/mirror.py Orchestrator/local_provider/catalog.py`

**Step 2:** In `local_routes.py` change `from Orchestrator.local_provider import mirror` → `from Orchestrator.local_provider import catalog`. Update the three call sites: `mirror.list_bundles()` → `catalog.list_bundles()`, `mirror.get_bundle(slug)` → `catalog.get_bundle(slug)`, `mirror.ensure_present(slug)` → `catalog.ensure_present(slug)` (the download route is deleted in A7, but keep it compiling until then).

**Step 3:** `git mv Orchestrator/tests/test_local_mirror.py Orchestrator/tests/test_local_discovery.py` and replace `from Orchestrator.local_provider import mirror` → `... import catalog as mirror` (alias keeps the existing tests green this step; they’re rewritten in A6).

**Step 4:** Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_discovery.py Orchestrator/tests/test_local_catalog.py -q`
Expected: PASS (pure rename).

**Step 5:** Commit:
```bash
git add Orchestrator/local_provider/catalog.py Orchestrator/routes/local_routes.py Orchestrator/tests/test_local_discovery.py
git commit -m "refactor(local): rename mirror.py -> catalog.py (no behaviour change)"
```
(Note: `git mv` stages the rename; verify `git status` shows renames, not add+delete of unrelated files.)

---

### Task A2: HF Hub API fetchers (`_fetch_hf_models`, `_fetch_hf_tree`) — the only network code

**Files:**
- Modify: `Orchestrator/local_provider/catalog.py`
- Test: `Orchestrator/tests/test_local_discovery.py`

These two functions are the ONLY network touch — tests monkeypatch them so no real HF call runs (same hermetic pattern the old `_download_bundle` used).

**Step 1: Write failing tests**
```python
# test_local_discovery.py (new section)
from Orchestrator.local_provider import catalog

def test_fetch_hf_models_shape(monkeypatch):
    # _fetch_hf_models returns a list of {"id","gated"} for litert-community gemma repos.
    fake = [
        {"id": "litert-community/gemma-4-E2B-it-litert-lm", "gated": False},
        {"id": "litert-community/gemma-4-E4B-it-litert-lm", "gated": False},
        {"id": "litert-community/not-a-litertlm-repo", "gated": False},
    ]
    monkeypatch.setattr(catalog, "_http_get_json", lambda url, **kw: fake)
    out = catalog._fetch_hf_models()
    ids = {m["id"] for m in out}
    assert "litert-community/gemma-4-E2B-it-litert-lm" in ids

def test_fetch_hf_tree_shape(monkeypatch):
    fake_tree = [
        {"path": "gemma-4-E2B-it-web.litertlm", "size": 2008432640,
         "lfs": {"oid": "a"*64}},
        {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712,
         "lfs": {"oid": "b"*64}},
        {"path": "README.md", "size": 100},
    ]
    monkeypatch.setattr(catalog, "_http_get_json", lambda url, **kw: fake_tree)
    files = catalog._fetch_hf_tree("litert-community/gemma-4-E2B-it-litert-lm")
    paths = {f["path"] for f in files}
    assert "gemma-4-E2B-it.litertlm" in paths
```

**Step 2:** Run: `... -m pytest Orchestrator/tests/test_local_discovery.py -k "fetch_hf" -v` → FAIL (no such attrs).

**Step 3: Implement** (in `catalog.py`, replacing the mirror download section header):
```python
import time
import requests  # already in venv

_HF_BASE = "https://huggingface.co"
_HF_AUTHOR = "litert-community"
_HF_TIMEOUT = 20  # HF Hub API JSON calls are tiny; 20s is generous.

def _http_get_json(url: str, **kw):
    """GET a URL and return parsed JSON. Isolated so tests monkeypatch IT
    (never a real HF request runs under pytest)."""
    token = os.environ.get("HF_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=_HF_TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()

def _fetch_hf_models() -> list[dict]:
    """List litert-community gemma models from the HF Hub API."""
    url = f"{_HF_BASE}/api/models?author={_HF_AUTHOR}&search=gemma"
    data = _http_get_json(url)
    out = []
    for m in data:
        mid = m.get("id", "")
        if mid.endswith("-litert-lm") and "gemma" in mid and "-it-" in mid:
            out.append({"id": mid, "gated": bool(m.get("gated"))})
    return out

def _fetch_hf_tree(repo: str) -> list[dict]:
    """Return the repo's main-branch file tree (path/size/lfs)."""
    url = f"{_HF_BASE}/api/models/{repo}/tree/main"
    data = _http_get_json(url)
    return [f for f in data if f.get("type", "file") == "file"]
```
(Keep the existing `import os` at top.)

**Step 4:** Run the same pytest -k → PASS.

**Step 5:** Commit:
```bash
git add Orchestrator/local_provider/catalog.py Orchestrator/tests/test_local_discovery.py
git commit -m "feat(local): HF Hub API fetchers for catalog discovery (hermetic-testable)"
```

---

### Task A3: File selection + slug derivation + download URL helpers

**Files:** `catalog.py` (+ tests)

**Step 1: Write failing tests**
```python
def test_select_litertlm_prefers_curated_filename():
    tree = [
        {"path": "gemma-4-E2B-it-web.litertlm", "size": 1, "lfs": {"oid": "a"*64}},
        {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712, "lfs": {"oid": "b"*64}},
    ]
    f = catalog._select_litertlm(tree, preferred="gemma-4-E2B-it.litertlm")
    assert f["path"] == "gemma-4-E2B-it.litertlm"
    assert f["sha256"] == "b"*64
    assert f["size_bytes"] == 2588147712

def test_select_litertlm_heuristic_excludes_web():
    tree = [
        {"path": "gemma-4-E4B-it-web.litertlm", "size": 1, "lfs": {"oid": "a"*64}},
        {"path": "gemma-4-E4B-it.litertlm", "size": 5, "lfs": {"oid": "b"*64}},
    ]
    f = catalog._select_litertlm(tree, preferred=None)
    assert f["path"] == "gemma-4-E4B-it.litertlm"  # the non-web -it build

def test_slug_for_repo():
    assert catalog._slug_for_repo("litert-community/gemma-4-E2B-it-litert-lm") == "gemma-4-e2b"
    assert catalog._slug_for_repo("litert-community/gemma-4-12B-it-litert-lm") == "gemma-4-12b"

def test_download_url():
    assert catalog._download_url("litert-community/gemma-4-E2B-it-litert-lm",
                                 "gemma-4-E2B-it.litertlm") == \
        "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm"
```

**Step 2:** Run -k "select_litertlm or slug_for_repo or download_url" → FAIL.

**Step 3: Implement**
```python
def _select_litertlm(tree: list[dict], preferred: str | None) -> dict | None:
    """Pick the canonical .litertlm file from a repo tree.

    A repo ships multiple .litertlm builds (e.g. ``-web`` variants). Selection:
      1. the curated ``preferred`` filename if present;
      2. else the non-``-web`` ``*-it.litertlm`` build;
      3. else the largest .litertlm (last resort).
    Returns a normalised dict {path, size_bytes, sha256} or None.
    """
    litertlms = [f for f in tree if str(f.get("path", "")).endswith(".litertlm")]
    if not litertlms:
        return None

    def _norm(f):
        return {
            "path": f["path"],
            "size_bytes": int(f.get("size") or (f.get("lfs") or {}).get("size") or 0),
            "sha256": (f.get("lfs") or {}).get("oid"),  # LFS oid == content SHA-256
        }

    if preferred:
        for f in litertlms:
            if f.get("path") == preferred:
                return _norm(f)
    non_web = [f for f in litertlms
               if "-web" not in f["path"] and f["path"].endswith("-it.litertlm")]
    if non_web:
        return _norm(non_web[0])
    return _norm(max(litertlms, key=lambda f: f.get("size") or 0))

def _slug_for_repo(repo: str) -> str:
    """litert-community/gemma-4-E2B-it-litert-lm -> gemma-4-e2b."""
    name = repo.split("/")[-1]
    name = name.removesuffix("-litert-lm").removesuffix("-it")
    return name.lower()

def _download_url(repo: str, filename: str) -> str:
    return f"{_HF_BASE}/{repo}/resolve/main/{filename}"
```

**Step 4:** Run -k → PASS.

**Step 5:** Commit `feat(local): catalog file-selection + slug/url helpers`.

---

### Task A4: Curated config floor + `build_catalog()` merge (HF-enriched, TTL-cached)

**Files:** `catalog.py` (+ tests)

This replaces the old hardcoded `BUNDLES` byte-mover dict with a *curated floor* that HF discovery enriches (real size/sha/gated) and extends (new repos appear automatically). If HF is unreachable, the curated floor is served (fresh-box safe) — distinct from the (declined) phone-side baked-in fallback; this is hub-side resilience only.

**Step 1: Write failing tests**
```python
def _patch_hf(monkeypatch, models, trees):
    monkeypatch.setattr(catalog, "_fetch_hf_models", lambda: models)
    monkeypatch.setattr(catalog, "_fetch_hf_tree", lambda repo: trees[repo])
    catalog._invalidate_cache()  # force a rebuild

def test_build_catalog_enriches_curated_with_hf_facts(monkeypatch):
    models = [{"id": "litert-community/gemma-4-E2B-it-litert-lm", "gated": False},
              {"id": "litert-community/gemma-4-E4B-it-litert-lm", "gated": False}]
    trees = {
        "litert-community/gemma-4-E2B-it-litert-lm": [
            {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712, "lfs": {"oid": "b"*64}}],
        "litert-community/gemma-4-E4B-it-litert-lm": [
            {"path": "gemma-4-E4B-it.litertlm", "size": 3659530240, "lfs": {"oid": "c"*64}}],
    }
    _patch_hf(monkeypatch, models, trees)
    bundles = {b["slug"]: b for b in catalog.list_bundles()}
    e4b = bundles["gemma-4-e4b"]
    assert e4b["size_bytes"] == 3659530240
    assert e4b["sha256"] == "c"*64                 # pinned from lfs.oid
    assert e4b["gated"] is False
    assert e4b["download_url"].endswith("/gemma-4-E4B-it.litertlm")
    assert e4b["recommended"] is True              # curated config preserved
    assert e4b["max_tokens"] == 16384
    assert e4b["support_image"] is True

def test_build_catalog_adds_uncurated_repo_with_defaults(monkeypatch):
    models = [{"id": "litert-community/gemma-4-12B-it-litert-lm", "gated": False}]
    trees = {"litert-community/gemma-4-12B-it-litert-lm":
             [{"path": "gemma-4-12B-it.litertlm", "size": 8_000_000_000, "lfs": {"oid": "d"*64}}]}
    _patch_hf(monkeypatch, models, trees)
    bundles = {b["slug"]: b for b in catalog.list_bundles()}
    assert "gemma-4-12b" in bundles
    assert bundles["gemma-4-12b"]["recommended"] is False      # default
    assert bundles["gemma-4-12b"]["min_ram_gb"] > 4.5          # inferred from 8GB size

def test_build_catalog_falls_back_to_curated_when_hf_down(monkeypatch):
    def _boom(): raise RuntimeError("HF unreachable")
    monkeypatch.setattr(catalog, "_fetch_hf_models", _boom)
    catalog._invalidate_cache()
    bundles = {b["slug"]: b for b in catalog.list_bundles()}
    # The curated floor (E2B/E4B) is still served, with constructible download_url.
    assert {"gemma-4-e2b", "gemma-4-e4b"} <= set(bundles)
    assert bundles["gemma-4-e2b"]["download_url"].endswith(".litertlm")

def test_list_bundles_is_cached(monkeypatch):
    calls = {"n": 0}
    def _models():
        calls["n"] += 1
        return []
    monkeypatch.setattr(catalog, "_fetch_hf_models", _models)
    catalog._invalidate_cache()
    catalog.list_bundles(); catalog.list_bundles()
    assert calls["n"] == 1  # second call is a cache hit within TTL
```

Keep these existing tests (update field expectations): `test_catalog_lists_both_bundles`, `test_catalog_per_model_config_e4b_recommended`, `test_catalog_per_model_config_e2b_experimental`, `test_catalog_exactly_one_recommended`, `test_catalog_ram_ordering`, `test_slug_parity`. Update `test_catalog_entries_have_required_fields` to require the new fields: add `"download_url"`, `"gated"`; `hf_repo` stays. These catalog-endpoint tests need the HF fetchers patched in the `client` fixture or per-test (otherwise they hit the network). Add an `autouse` fixture in this file that patches `_fetch_hf_models`/`_fetch_hf_tree` to the E2B/E4B fakes + calls `_invalidate_cache()`.

**Step 2:** Run the file → the new tests FAIL.

**Step 3: Implement**
```python
# --- curated floor (the always-present, blessed config) --------------------
CURATED: dict[str, dict] = {
    "gemma-4-e2b": {
        "display_name": "Gemma 4 E2B (on-device)",
        "hf_repo": "litert-community/gemma-4-E2B-it-litert-lm",
        "filename": "gemma-4-E2B-it.litertlm",
        "size_bytes": 2588147712,          # fallback if HF is down
        "min_ram_gb": 3.0,
        "recommended_for": "Lighter, faster on-device model for phones with less RAM.",
        "recommended": False,
        "context_note": "Experimental — weaker at multi-step agent loops",
        "max_tokens": 16384,
        "support_image": True,
    },
    "gemma-4-e4b": {
        "display_name": "Gemma 4 E4B (on-device)",
        "hf_repo": "litert-community/gemma-4-E4B-it-litert-lm",
        "filename": "gemma-4-E4B-it.litertlm",
        "size_bytes": 3659530240,
        "min_ram_gb": 4.5,
        "recommended_for": "Heavier, higher-quality on-device model for high-RAM phones.",
        "recommended": True,
        "context_note": "Recommended — best on-device agent reliability",
        "max_tokens": 16384,
        "support_image": True,
    },
}

_CACHE_TTL = 3600  # seconds
_cache: dict = {"at": 0.0, "bundles": None}

def _invalidate_cache():
    _cache["at"] = 0.0
    _cache["bundles"] = None

def _infer_min_ram_gb(size_bytes: int) -> float:
    # A model needs roughly its file size in RAM plus headroom; 1.25x, floored at 3.
    return round(max(3.0, (size_bytes / 1_073_741_824) * 1.25), 1)

def _default_entry(slug: str, repo: str, gated: bool, f: dict) -> dict:
    return {
        "slug": slug,
        "display_name": slug.replace("-", " ").title() + " (on-device)",
        "hf_repo": repo,
        "filename": f["path"],
        "size_bytes": f["size_bytes"],
        "sha256": f["sha256"],
        "min_ram_gb": _infer_min_ram_gb(f["size_bytes"]),
        "recommended_for": "Auto-discovered on-device model.",
        "recommended": False,
        "context_note": "Auto-discovered",
        "max_tokens": 16384,
        "support_image": True,
        "download_url": _download_url(repo, f["path"]),
        "gated": gated,
    }

def _curated_floor() -> dict[str, dict]:
    """The curated entries as full bundle dicts (sha None, download_url built)."""
    out = {}
    for slug, c in CURATED.items():
        b = dict(c)
        b["slug"] = slug
        b.setdefault("sha256", None)
        b["download_url"] = _download_url(c["hf_repo"], c["filename"])
        b["gated"] = False
        out[slug] = b
    return out

def build_catalog() -> list[dict]:
    """Discover + curate the downloadable bundles (TTL-cached).

    HF facts (size/sha256/gated, new repos) enrich/extend the curated floor.
    On any HF error the curated floor is served unchanged (fresh-box safe)."""
    now = time.time()
    if _cache["bundles"] is not None and (now - _cache["at"]) < _CACHE_TTL:
        return [dict(b) for b in _cache["bundles"]]

    bundles = _curated_floor()
    try:
        for m in _fetch_hf_models():
            repo, gated = m["id"], m["gated"]
            slug = _slug_for_repo(repo)
            curated = CURATED.get(slug)
            f = _select_litertlm(_fetch_hf_tree(repo), curated["filename"] if curated else None)
            if f is None:
                continue
            if slug in bundles:  # enrich curated entry with live facts
                bundles[slug].update({
                    "filename": f["path"],
                    "size_bytes": f["size_bytes"],
                    "sha256": f["sha256"],
                    "download_url": _download_url(repo, f["path"]),
                    "gated": gated,
                })
            else:                # extend with an auto-discovered entry
                bundles[slug] = _default_entry(slug, repo, gated, f)
    except Exception as e:
        print(f"[LOCAL CATALOG] HF discovery failed, serving curated floor: {e}")
        # bundles already holds the curated floor.

    result = sorted(bundles.values(), key=lambda b: b["slug"])
    _cache["at"], _cache["bundles"] = now, result
    return [dict(b) for b in result]

def list_bundles() -> list[dict]:
    return build_catalog()

def get_bundle(slug: str) -> dict | None:
    return next((dict(b) for b in build_catalog() if b["slug"] == slug), None)
```
Delete the old `BUNDLES` dict and the `_download_bundle`/`ensure_present`/`bundle_sha256`/`MIRROR_DIR`/`_HASH_CHUNK` definitions (their tests are removed in A6). Keep `import os`.

**Step 4:** Run `... pytest Orchestrator/tests/test_local_discovery.py -v` → PASS.

**Step 5:** Commit `feat(local): build_catalog() — HF-enriched curated floor, TTL cached`.

---

### Task A5: Remove the byte-mover tests; keep/repurpose catalog + parity tests

**Files:** `Orchestrator/tests/test_local_discovery.py`

**Step 1:** Delete the now-obsolete tests that exercised the deleted machinery: `test_download_*` (the `/local/models/download/*` HTTP tests), `test_ensure_present_*`, `test_bundle_sha256`, the `fake_mirror`/`FIXTURE_BYTES` fixtures, and `test_bundles_pinned_to_real_litert_community_repos` (BUNDLES no longer exists). Keep `test_slug_parity` but repoint it from `mirror.BUNDLES.keys()` to `{b["slug"] for b in catalog.list_bundles()}` (under the patched-HF autouse fixture) — note it now asserts the curated-floor slugs are a subset of/equal to `LOCAL_MODELS` ids.

**Step 2:** Run the file → PASS (only discovery + catalog tests remain).

**Step 3:** Commit `test(local): drop byte-mover tests, repoint parity to discovery catalog`.

---

### Task A6: Delete the download proxy endpoint + `mirror_store/`

**Files:**
- Modify: `Orchestrator/routes/local_routes.py` — delete `local_models_download` (lines ~138-173) and its block comment; update the `import catalog` usage (remove `ensure_present`/`get_bundle`-for-download references). Keep the `local_models_catalog` route (now serving `catalog.list_bundles()`).
- Delete on disk: `Orchestrator/local_provider/mirror_store/` (untracked — `rm -rf`).
- Modify: `catalog.py` — remove `FileResponse` import if now unused (it's imported in local_routes, not catalog; check local_routes — drop `FileResponse` import there if no other user).

**Step 1: Write a failing test** (download endpoint must now 404 as an unknown route):
```python
# test_local_discovery.py
def test_download_endpoint_removed(client):
    resp = client.get("/local/models/download/gemma-4-e2b")
    assert resp.status_code == 404  # route deleted
```

**Step 2:** Run → currently the route still exists (returns 200/500), so FAIL.

**Step 3:** Delete `local_models_download` from `local_routes.py`. Remove the now-unused `FileResponse` import there (grep first: `grep -n FileResponse Orchestrator/routes/local_routes.py`). `rm -rf Orchestrator/local_provider/mirror_store`.

**Step 4:** Run the file → PASS. Then full local suite: `... pytest Orchestrator/tests/test_local_discovery.py Orchestrator/tests/test_local_catalog.py Orchestrator/tests/test_local_turn_prepare.py Orchestrator/tests/test_local_turn_complete.py -q` → PASS.

**Step 5:** Commit:
```bash
git add Orchestrator/routes/local_routes.py Orchestrator/local_provider/catalog.py Orchestrator/tests/test_local_discovery.py
git commit -m "feat(local): delete hub byte-proxy download endpoint + mirror_store (bytes go phone->HF direct)"
```

---

### Task A7: Live smoke (hub) — confirm auto-discovery serves the new fields

**Step 1:** Restart the service (pre-authorized): `sudo systemctl restart blackbox.service`; wait ~90s (snapshot index rebuild).

**Step 2:** `curl -s http://localhost:9091/local/models/catalog | python3 -m json.tool | head -60`
Expected: each bundle has `download_url` (HF resolve URL), a real `size_bytes`, a 64-hex `sha256`, `gated: false`; `gemma-4-e4b.recommended == true`; the `gemma-4-12b` entry appears (auto-discovered).

**Step 3:** `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9091/local/models/download/gemma-4-e4b` → `404` (endpoint gone).

**Step 4 (no commit — smoke only):** if `sha256` is null, check journalctl for `[LOCAL CATALOG] HF discovery failed` and resolve (network/proxy) before proceeding.

---

## Phase B — Android: data model + direct-HF download

### Task B1: Add `download_url` + `gated` to `LocalBundle`

**Files:**
- Modify: `$KT/data/model/LocalBundle.kt` (insert after line 44, before the closing `)`).
- Test: `$KTEST/data/api/LocalModelApiTest.kt` (the `catalog` deserialization test already exists; extend it).

**Step 1:** Add fields:
```kotlin
    val temperature: Float? = null,
    @SerialName("download_url") val downloadUrl: String = "",
    val gated: Boolean = false,
)
```

**Step 2:** Extend the existing catalog test in `LocalModelApiTest.kt` to assert `downloadUrl`/`gated` deserialize (enqueue a catalog JSON carrying `"download_url"` + `"gated"`; assert the parsed `LocalBundle.downloadUrl` / `.gated`).

**Step 3:** Run: `./gradlew :app:testDebugUnitTest --tests "*LocalModelApiTest*"` → PASS.

**Step 4:** Commit `feat(android): LocalBundle carries download_url + gated`.

---

### Task B2: `LocalModelApi.download()` streams from HF `download_url` (+ read timeout, drop hub header)

**Files:**
- Modify: `$KT/data/api/LocalModelDownloader.kt` (signature), `$KT/data/api/LocalModelApi.kt` (impl), `$KT/data/local/LocalModelManager.kt:283` (call site), `$KT/data/api/BlackBoxApi.kt` (a download client with a read timeout).
- Test: `$KTEST/data/api/LocalModelApiTest.kt`.

**Design:** change `download(slug, destFile, onProgress)` → `download(bundle, destFile, onProgress)` so the API reads `bundle.downloadUrl`/`gated`. Keep the `.part`+`Range` resume mechanics verbatim. Use a 90s-read-timeout client (a stall now surfaces as a retryable failure instead of an eternal 0%). Do NOT send `X-BlackBox-Client` to HF. For `gated`, attach `Authorization: Bearer <token>` only if a token seam returns non-null (stub returns null now — YAGNI).

**Step 1: Update the interface** `LocalModelDownloader.kt`:
```kotlin
suspend fun download(
    bundle: LocalBundle,
    destFile: File,
    onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
): Result<File>
```

**Step 2: Write failing tests** (replace the two existing download tests so they assert the HF URL + headers). The MockWebServer stands in for HF; pass a `LocalBundle` whose `downloadUrl = server.url("/repo/resolve/main/gemma-4-E2B-it.litertlm").toString()`:
```kotlin
@Test
fun `download streams from bundle download_url with no hub header`() = runTest {
    val content = ByteArray(2048) { (it % 251).toByte() }
    server.enqueue(MockResponse.Builder().code(200)
        .headers(headersOf("Content-Type", "application/octet-stream"))
        .body(Buffer().write(content)).build())
    val dest = File(tmpDir, "gemma.litertlm")
    val url = server.url("/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm").toString()
    val bundle = LocalBundle(slug = "gemma-4-e2b", filename = "gemma.litertlm", downloadUrl = url)

    val result = api.download(bundle, dest) { _, _ -> }
    assertTrue(result.isSuccess)
    assertArrayEquals(content, dest.readBytes())
    val rec = server.takeRequest()
    assertEquals("/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm", rec.target)
    assertNull("must NOT send the hub client header to HF", rec.headers["X-BlackBox-Client"])
    assertNull("fresh download sends no Range", rec.headers["Range"])
}

@Test
fun `download resumes from .part via Range against download_url`() = runTest {
    // (same as the existing resume test, but construct the bundle with downloadUrl
    //  and assert rec.headers["Range"] == "bytes=$prefixLen-")
}
```

**Step 3: Implement** in `LocalModelApi.kt` — replace lines 75-151 URL/header construction:
```kotlin
override suspend fun download(
    bundle: LocalBundle,
    destFile: File,
    onProgress: (Long, Long) -> Unit,
): Result<File> = withContext(Dispatchers.IO) {
    val partFile = File(destFile.parentFile, destFile.name + ".part")
    val existing = if (partFile.exists()) partFile.length() else 0L

    val url = bundle.downloadUrl.ifBlank {
        // Defensive fallback: construct the HF resolve URL from coordinates.
        "https://huggingface.co/${bundle.hfRepo}/resolve/main/${bundle.filename}"
    }
    val requestBuilder = Request.Builder().url(url).get()
    if (existing > 0L) requestBuilder.header("Range", "bytes=$existing-")
    if (bundle.gated) hfToken()?.let { requestBuilder.header("Authorization", "Bearer $it") }
    // NOTE: no X-BlackBox-Client header — this goes to HF, not the hub.

    try {
        downloadClient.newCall(requestBuilder.build()).await().use { response ->
            // ... identical body to the current impl (lines 99-147):
            // isSuccessful check, resuming logic, computeTotal, the 64KB
            // write loop calling onProgress, the .part -> dest rename ...
        }
    } catch (e: IOException) {
        Result.failure(e)
    }
}
```
Add the client + token seam to `LocalModelApi`:
```kotlin
class LocalModelApi(
    private val api: BlackBoxApi,
    private val hfToken: () -> String? = { null },  // BYOK seam (future gated repos)
) : LocalModelDownloader, LocalModelCatalogClient, PersonaSource {
    // 90s read timeout: a steady CDN stream lets a real stall surface as a
    // retryable failure instead of an eternal 0% (streamClient had readTimeout 0).
    private val downloadClient = api.streamClient.newBuilder()
        .readTimeout(90, java.util.concurrent.TimeUnit.SECONDS)
        .build()
```
Update `LocalModelManager.install()` call site (`:283`): `api.download(bundle, destFile, onProgress)`.

**Step 4:** Update fakes: in `$KTEST/data/local/LocalModelManagerTest.kt` any fake `LocalModelDownloader`/`LocalModelInstaller` whose `download(slug, ...)` signature changed → `download(bundle, ...)`. Run: `./gradlew :app:testDebugUnitTest --tests "*LocalModelApiTest*" --tests "*LocalModelManagerTest*"` → PASS.

**Step 5:** Commit `feat(android): download direct from HF download_url (read timeout, drop hub header)`.

---

## Phase C — Android: durable foreground download Service

### Task C1: `DownloadProgressBus` (pure Kotlin, unit-tested)

**Files:**
- Create: `$KT/data/local/DownloadProgressBus.kt`
- Test: `$KTEST/data/local/DownloadProgressBusTest.kt`

**Step 1: Write failing test**
```kotlin
class DownloadProgressBusTest {
    @Test fun `update then observe latest by slug`() {
        DownloadProgressBus.clearAll()
        DownloadProgressBus.update(DownloadProgressBus.State("gemma-4-e4b", 0.5f, DownloadProgressBus.Status.RUNNING))
        val s = DownloadProgressBus.flow.value["gemma-4-e4b"]!!
        assertEquals(0.5f, s.fraction, 0.0001f)
        assertEquals(DownloadProgressBus.Status.RUNNING, s.status)
    }
    @Test fun `clear removes a slug`() {
        DownloadProgressBus.update(DownloadProgressBus.State("x", 1f, DownloadProgressBus.Status.SUCCESS))
        DownloadProgressBus.clear("x")
        assertNull(DownloadProgressBus.flow.value["x"])
    }
}
```

**Step 2:** Run → FAIL.

**Step 3: Implement**
```kotlin
package com.aiblackbox.portal.data.local
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update

/** Process-wide bus carrying live download state so the ViewModel observes a
 *  download that runs in the foreground Service (survives screen navigation). */
object DownloadProgressBus {
    enum class Status { RUNNING, SUCCESS, FAILED }
    data class State(val slug: String, val fraction: Float, val status: Status, val error: String? = null)

    private val _flow = MutableStateFlow<Map<String, State>>(emptyMap())
    val flow: StateFlow<Map<String, State>> = _flow

    fun update(s: State) = _flow.update { it + (s.slug to s) }
    fun clear(slug: String) = _flow.update { it - slug }
    fun clearAll() = _flow.update { emptyMap() }
}
```

**Step 4:** Run → PASS. **Step 5:** Commit `feat(android): DownloadProgressBus for cross-component download state`.

---

### Task C2: `ModelDownloadService` (foreground, runs install(), publishes to the bus)

**Files:**
- Create: `$KT/ModelDownloadService.kt` (mirror `$KT/LocalModelService.kt`)
- Modify: `$AND/app/src/main/AndroidManifest.xml` (add `<service>`)

This is thin Android glue (no unit test, per the established pattern — `LocalModelService` has none). Coverage comes from C1 (bus), B2 (download), `LocalModelManagerTest` (install), and C3 (ViewModel observes bus).

**Step 1:** Add the manifest service near the other `dataSync` services (after line 88):
```xml
        <service
            android:name=".ModelDownloadService"
            android:foregroundServiceType="dataSync"
            android:exported="false" />
```
(`FOREGROUND_SERVICE_DATA_SYNC` + `POST_NOTIFICATIONS` perms already present — manifest lines 23, 15.)

**Step 2:** Create `ModelDownloadService.kt`, mirroring `LocalModelService` mechanics (notification channel, `ServiceCompat.startForeground` with `FOREGROUND_SERVICE_TYPE_DATA_SYNC`, started-only, companion `start()` using `startForegroundService`). Core:
```kotlin
class ModelDownloadService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onCreate() { super.onCreate(); createChannel() }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val bundleJson = intent?.getStringExtra(EXTRA_BUNDLE) ?: return START_NOT_STICKY.also { stopSelf() }
        val bundle = Json { ignoreUnknownKeys = true }.decodeFromString(LocalBundle.serializer(), bundleJson)
        val operator = intent.getStringExtra(EXTRA_OPERATOR) ?: "system"
        val delegate = intent.getStringExtra(EXTRA_DELEGATE) ?: "cpu"
        val origin = intent.getStringExtra(EXTRA_ORIGIN) ?: ""
        val deviceId = intent.getStringExtra(EXTRA_DEVICE_ID) ?: ""

        startForegroundWith(buildNotification(bundle.displayName, 0, true))
        DownloadProgressBus.update(State(bundle.slug, 0f, Status.RUNNING))

        scope.launch {
            val manager = LocalModelManager.fromContext(
                applicationContext, LocalModelApi(BlackBoxApi(origin)), deviceId)
            var lastPct = -1
            val result = manager.install(bundle, operator, delegate) { soFar, total ->
                val frac = if (total > 0) (soFar.toFloat() / total).coerceIn(0f, 1f) else -1f
                val pct = if (frac < 0) 0 else (frac * 100).toInt()
                if (pct != lastPct) {
                    lastPct = pct
                    DownloadProgressBus.update(State(bundle.slug, frac, Status.RUNNING))
                    updateNotification(buildNotification(bundle.displayName, pct, frac < 0))
                }
            }
            DownloadProgressBus.update(
                if (result.isSuccess) State(bundle.slug, 1f, Status.SUCCESS)
                else State(bundle.slug, 0f, Status.FAILED,
                           result.exceptionOrNull()?.message ?: "download failed"))
            stopForegroundCompat(); stopSelf()
        }
        return START_NOT_STICKY  // a download is not auto-restarted; the user re-taps (resume via .part)
    }
    // buildNotification(...).setProgress(100, pct, indeterminate).setOngoing(true)...
    companion object {
        fun start(ctx: Context, bundle: LocalBundle, operator: String, delegate: String,
                  origin: String, deviceId: String) { /* build intent + startForegroundService */ }
    }
}
```
Notes: `START_NOT_STICKY` (a half-finished multi-GB download shouldn't silently auto-restart; the `.part` lets the user resume by re-tapping). Serialize the bundle via `kotlinx.serialization` (LocalBundle is `@Serializable`).

**Step 3:** Build to verify it compiles: `./gradlew :app:compileDebugKotlin` → SUCCESS.

**Step 4:** Commit `feat(android): ModelDownloadService — durable foreground download`.

---

### Task C3: ViewModel starts the Service + observes the bus (download survives `dispose()`)

**Files:**
- Modify: `$KT/ui/settings/LocalModelViewModel.kt`
- Test: `$KTEST/ui/settings/LocalModelViewModelTest.kt`

**Design:** replace the in-scope `installer.install(...)` in `download()` with a `startDownload(bundle)` seam (defaulted in `fromContext` to `ModelDownloadService.start(...)`), and add a bus collector that maps `DownloadProgressBus.flow` → `downloadProgress`/`failedSlugs`/`busySlug`, re-querying `installer.installedModels()` on `SUCCESS`. `dispose()` cancels only the ViewModel’s observer scope — the Service keeps running.

**Step 1: Write failing test** (inject a fake `startDownload` that drives the bus):
```kotlin
@Test fun `download starts via seam and bus SUCCESS refreshes installed`() = runTest {
    DownloadProgressBus.clearAll()
    val started = mutableListOf<String>()
    val vm = LocalModelViewModel(
        installer = fakeInstaller,          // installedModels() returns the model after success
        catalog = fakeCatalog,
        /* ...existing params... */
        startDownload = { b -> started += b.slug
            DownloadProgressBus.update(State(b.slug, 1f, Status.SUCCESS)) },
    )
    vm.download(LocalBundle(slug = "gemma-4-e4b", downloadUrl = "http://x/y"))
    advanceUntilIdle()
    assertEquals(listOf("gemma-4-e4b"), started)
    assertTrue(vm.state.value.isInstalled("gemma-4-e4b"))
    assertNull(vm.state.value.busySlug)
}

@Test fun `dispose does not cancel an in-flight service download`() = runTest {
    // download() sets busySlug then dispose(); the bus later emits SUCCESS;
    // assert state still transitions (observer rebinds) — i.e. no scope.cancel of the download.
}
```

**Step 2:** Run → FAIL.

**Step 3: Implement.** Add constructor param `startDownload: (LocalBundle) -> Unit`. In `download(bundle)`: keep the busySlug/seed-progress `_state.update`, then call `startDownload(bundle)` instead of `scope.launch { installer.install(...) }`. Add an `init { scope.launch { DownloadProgressBus.flow.collect { onBus(it) } } }` that, per slug state: RUNNING → set `downloadProgress[slug]=fraction`, busySlug=slug; FAILED → failedSlugs+=slug, busySlug=null, clear progress, set error, `DownloadProgressBus.clear(slug)`; SUCCESS → `installer.installedModels()` refresh, busySlug=null, clear progress + failed, `DownloadProgressBus.clear(slug)`. In `fromContext`, default `startDownload = { b -> ModelDownloadService.start(appContext, b, operatorProvider(), delegate, api.getBaseUrl(), deviceId) }` (capture `appContext`, `origin`, `deviceId` already available in `fromContext`). Keep `installer` for `installedModels`/`delete`/`recommendForDevice`/`updateModelConfig`.

**Step 4:** Run: `./gradlew :app:testDebugUnitTest --tests "*LocalModelViewModelTest*"` → PASS.

**Step 5:** Commit `feat(android): durable downloads via ModelDownloadService + bus (survive nav-away)`.

---

## Phase D — Portal / WebView (catalog read-only)

### Task D1: Portal shows on-device models informationally (no download button)

**Files:**
- Inspect first: `grep -rn "local/models/catalog\|on_device\|on-device\|gemma" Portal/app.js Portal/index.html`
- Modify (only if Portal currently renders a download affordance for local models): `Portal/app.js` / `Portal/style.css`.

**Step 1:** Determine current behaviour. The on-device provider only runs on the phone; the Portal cannot download a `.litertlm`. If the Portal already does not surface a download button for `provider: "local"`, this task is a no-op verification (document it). If it does, replace the button with an informational note ("Download on your paired phone").

**Step 2:** If changed, bump `?v=genuiXX` in `Portal/index.html` per the version-bump convention.

**Step 3:** Commit `feat(portal): on-device models are informational (download is phone-only)` (or skip with a note in the execution log if no change needed).

---

## Phase E — Verification & handoff

### Task E1: Full backend suite
Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_discovery.py Orchestrator/tests/test_local_catalog.py Orchestrator/tests/test_local_turn_prepare.py Orchestrator/tests/test_local_turn_complete.py Orchestrator/tests/test_local_lean_retrieval.py -q` → all PASS. Then a broader sanity run of any suite importing `local_routes`.

### Task E2: Full Android unit suite
Run: `./gradlew :app:testDebugUnitTest` → BUILD SUCCESSFUL. Investigate any red.

### Task E3: Hub live smoke
Already covered by A7 after the backend lands; re-confirm `GET /local/models/catalog` carries `download_url`/`gated`/real `sha256`, and the download endpoint is `404`.

### Task E4: Device acceptance (user-run)
Rebuild + install the APK. On the phone: delete `gemma-4-e4b` via the delete button, then reinstall. Expected: a foreground notification appears, progress advances smoothly to 100% **streaming direct from HF** (independent of the hub), the model installs + attests, and leaving the Settings screen mid-download does **not** cancel it. Repeat for `gemma-4-e2b` (no mirror prerequisite). Confirm the picker auto-lists any newly-discovered bundle (e.g. `gemma-4-12b`).

### Task E5: Snapshot
Invoke `/snapshot-dev` (operator resolved dynamically) to persist the session into BlackBox memory.

---

## Risk notes / gotchas

- **`lfs.oid` == content SHA-256:** verified true for Git-LFS/Xet files on HF. Device verify (`LocalModelManager.verify()`) compares case-insensitively. If a real device test ever shows a mismatch (e.g. a Xet edge case), fall back to `sha256 = null` for that entry (verify no-ops) rather than blocking install — but the expectation is a clean match.
- **Multiple `.litertlm` per repo:** `_select_litertlm` must pick the curated filename (or the non-`-web` `*-it.litertlm`). Getting this wrong downloads a different build — covered by A3 tests.
- **Bundle over Intent:** `ModelDownloadService` receives the `LocalBundle` as a JSON extra (it's `@Serializable`); decode with `ignoreUnknownKeys = true` for forward-compat.
- **`origin`/`deviceId` in the Service:** passed as intent extras from the ViewModel's `fromContext` (which already resolves `api.getBaseUrl()` + `deviceId`) so the Service constructs the same `LocalModelManager` the ViewModel would.
- **Fresh-box safety:** if HF discovery fails at catalog-build time, the curated floor (E2B/E4B) is still served — the picker is never empty on a box with no internet at that instant. This is hub-side resilience, NOT the (declined) phone-side baked-in catalog.
- **No `git add -A`:** stage explicit paths; `mirror_store/` and other untracked local files must not be swept in.
