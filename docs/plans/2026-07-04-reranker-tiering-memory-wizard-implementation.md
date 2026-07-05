# Hardware-Tiered Reranker + Memory-Wizard Production Uplift — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task.
> Design source of truth: `.claude/plans/graceful-twirling-church.md` (verified seams, Brandon's locked decisions). When this plan and the design conflict, re-verify against current code and flag it.

**Goal:** Every BlackBox box gets good reranking matched to its hardware — local GPU (best), opt-in local CPU (32 GB+), or cloud (dedicated cross-encoders + LLM-as-reranker fallback) — selectable from the wizard with honest per-tier guidance, plus embeddings CPU-slowness warnings so no-GPU users don't unknowingly kick off hours-long re-embeds.

**Architecture:** `rerank.score(query, passages) -> list[float] | None` becomes a provider dispatcher; every provider honors that one contract so `retrieval.py:_apply_rerank` is untouched. Hardware gains a LOW/MID/HIGH tier. Selection persists to a `rerank.json` sidecar (mirror of `placement.json`); cloud keys reuse the existing `.env` secrets-writer + Vertex service-account upload (both live-mirror to `os.environ`, no restart). Every provider is inert without its dep/key — fresh LOW boxes stay `provider=null`.

**Tech Stack:** Python 3.12 (Orchestrator/venv), FastAPI, `requests` (no new cloud SDKs — raw REST), `sentence-transformers`+CPU `torch` (installer-added for MID tier only, lazy-imported), vanilla-JS wizard/Portal, Kotlin/Compose Android, pytest.

**North star for every review:** the reranker only *reorders*; a failed/absent/slow provider returns `None` → the un-reranked ranking stands → memory is never made empty or unscorable. No provider may break that.

**Standing rituals (every task):** TDD; guarded suite `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -k "embedding or retrieval or fossil or context or rerank or hardware or onboarding"` green before commit; explicit-path staging (never `git add -A`); secret-grep staged files; commit trailers:
```
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013KcxhdCJJoPCAw5X3ymyFk
```
Service restart is pre-authorized (`sudo systemctl restart blackbox.service`, ~90 s). Fakes for `torch`/cloud transports in CI — **no heavy deps in `requirements.txt`**. Adversarial review per milestone before push.

**This box is a LOW/MID test bed** (no GPU) — the ideal place to validate the cloud + CPU tiers the Ultra (GPU) cannot exercise.

---

## M1 — Hardware tier derivation

### Task 1.1: `derive_tier` + additive `tier`/`ram_mb` in `probe()`

**Files:**
- Modify: `Orchestrator/hardware.py` (add `derive_tier`; `probe()` return dict at `hardware.py:102-121`)
- Test: `Orchestrator/tests/test_hardware.py`

**Step 1 — failing tests** (point `MEMINFO_PATH` / monkeypatch the GPU ladder as existing tests do):
```python
def test_tier_low_no_gpu_under_32gb():   # gpu=False, ram_mb=31_900 -> "LOW"
def test_tier_mid_no_gpu_32gb():         # gpu=False, ram_mb=32_768 -> "MID"
def test_tier_high_gpu_8gb():            # gpu=True,  vram_mb=8192  -> "HIGH"
def test_tier_high_lspci_unknown_vram(): # gpu=True,  vram_mb=None (lspci) -> "HIGH"
def test_tier_gpu_under_8gb_not_high():  # gpu=True,  vram_mb=6144  -> not "HIGH" (installer refuses vLLM <8GB)
def test_probe_still_returns_legacy_keys(): # gpu/gpu_name/vram_mb/ram_mb/source all present (additive contract)
```
**Step 2:** run → FAIL (`derive_tier` undefined; `probe()` has no `tier`).
**Step 3 — implement.** `derive_tier(gpu, vram_mb, ram_mb)`: `HIGH` if `gpu and (vram_mb is None or vram_mb >= 8192)`; `MID` if `not gpu and ram_mb >= 32768`; else `LOW`. Keep the nvidia-smi→lspci→meminfo ladder (`hardware.py:74-99`) untouched — **no `torch` in the probe** (Brandon's sketch used `torch.cuda`; we don't). Add `"tier"` to the `probe()` return dict; `ram_mb` already there.
**Step 4:** run → PASS.
**Step 5:** Commit `feat(hardware): LOW/MID/HIGH tier derivation (reranker tiering M1)`.

**Note:** the 8 GB HIGH threshold reconciles Brandon's sketch (6 GB) with the installer's actual vLLM gate (`installer/templates/blackbox-install-reranker.sh` ≥8000 MB) — a 6-8 GB card can't co-host the Ollama embedder + vLLM. Additive field; nothing consumes `tier` yet → zero behavior change.

---

## M2 — Provider-dispatch skeleton + RERANK_MODELS schema

### Task 2.1: Extend the RERANK_MODELS schema

**Files:** Modify `Orchestrator/rerank.py:93-119`; Test: `Orchestrator/tests/test_rerank.py` (extend the existing table-shape guard).

Each entry gains: `auth_kind` ∈ `{none, bearer_env, gcp_service_account, frontier_key}`, `key_env` (str|None), `cost_note` (str), `privacy` (`local`|`cloud`), `tiers` (subset of `["LOW","MID","HIGH"]`), `preflight_ceiling_ms` (int), `preflight_passage_n` (int). `query_instruction` stays **only** on the Qwen `vllm`/`cpu` entries (cloud/LLM providers must NOT get the Qwen prefix). The two existing entries get `auth_kind:"none"`, `key_env:None`, `privacy:"local"`, `tiers:["HIGH"]`, `preflight_ceiling_ms:500`, `preflight_passage_n:1`.

**Step 1 — failing test:** `test_every_rerank_model_declares_new_schema_keys` asserts each entry has all new keys with valid types/enums (mirror `test_embeddings_registry.py`'s guard).
**Step 2-4:** run FAIL → add keys to the two entries → PASS.
**Step 5:** Commit `feat(rerank): RERANK_MODELS schema — auth_kind/tiers/per-provider preflight (M2)`.

### Task 2.2: Split `score()` into a dispatcher + `_score_vllm`

**Files:** Modify `Orchestrator/rerank.py:194-231`; Test: `test_rerank.py`.

**Step 1 — failing test:** `test_score_dispatches_by_provider` (monkeypatch `_score_vllm`/a fake, set provider, assert the right helper is called); `test_unknown_provider_returns_none_never_raises`.
**Step 2:** run → FAIL.
**Step 3 — implement.** Move the current `score()` body (`rerank.py:207-231`, the vLLM POST `/score`) verbatim into `_score_vllm(query, passages, settings) -> list[float] | None`. New `score()`:
```python
def score(query, passages):
    if not passages: return None
    s = get_settings()
    p = s["provider"]
    if p == "null": return None
    fn = {"vllm": _score_vllm, "cpu": _score_cpu, "voyage": _score_voyage,
          "cohere": _score_cohere, "vertex": _score_vertex, "llm": _score_llm}.get(p)
    if fn is None: return None
    try:
        return fn(query, passages, s)
    except Exception:  # never-raise contract
        return None
```
Add `KNOWN_PROVIDERS = {"null","vllm","cpu","voyage","cohere","vertex","llm"}`. Generalize `_configured()` (`rerank.py:194-196`) to `provider in KNOWN_PROVIDERS - {"null"} and _provider_ready(s)` where `_provider_ready` checks provider-appropriate config (vllm/cpu → `base_url`/importable; bearer → key_env present; vertex → creds present; llm → frontier key present). Stub the not-yet-implemented `_score_*` to `return None` (real impls in M5-M7).
**Step 4:** run → PASS. **The existing vLLM tests (`test_rerank.py:134-205`) must pass UNCHANGED** — proof `_score_vllm` is byte-identical.
**Step 5:** Commit `feat(rerank): score() provider dispatcher, vLLM body extracted verbatim (M2)`.

---

## M3 — Per-provider preflight/reachability + `status()` rework

### Task 3.1: Per-provider ceilings + realistic passage-count probe

**Files:** Modify `Orchestrator/rerank.py` (`get_settings` `164-191`, preflight `234-278`); Test: `test_rerank.py`.

`get_settings()` resolves `preflight_ceiling_ms` and `preflight_passage_n` **from the model's RERANK_MODELS entry** (config override still wins), plus `auth_kind`/`key_env`. Preflight scores `preflight_passage_n` dummy passages (not 1) and, for `cpu`, extrapolates `measured_ms * (rerank_candidate_n / preflight_passage_n)` before comparing to the ceiling.

**Step 1 — failing tests:** `test_cloud_ceiling_from_registry` (a 600 ms fake voyage passes its 1200 ceiling, fails old 500); `test_cpu_probe_extrapolates_to_candidate_n` (8-passage probe × 5 ≈ 40-passage estimate vs 2000 ceiling); `test_config_ceiling_override_wins`.
**Step 2-4:** FAIL → implement → PASS.
**Step 5:** Commit `fix(rerank): per-provider preflight ceilings + realistic passage-count probe (M3)`.

### Task 3.2: TTL-recoverable cloud preflight + `reachable(settings)` generalization

**Files:** Modify `Orchestrator/rerank.py` (`service_reachable`→add `reachable`, preflight cache); Test: `test_rerank.py`.

Cloud providers: failed preflight cached with a TTL (reuse the `_REACH_TTL_S` pattern, e.g. 60 s) not process-lifetime (a transient cloud blip must not disable until restart — a documented deviation from audit A9's local-only assumption). `reachable(settings)`: vllm/cpu → the existing localhost `/v1/models` probe (or "CrossEncoder importable + model cached" for cpu); bearer/vertex/llm → **key/creds present** (config read, never a paid poll).

**Step 1 — failing tests:** `test_cloud_failed_preflight_recovers_after_ttl`; `test_reachable_bearer_is_key_present_no_network` (fake transport asserts zero HTTP calls); `test_vllm_reachable_still_probes_localhost`.
**Step 2-4:** FAIL → implement → PASS. Keep `service_reachable()` as a thin back-compat wrapper.
**Step 5:** Commit `fix(rerank): TTL-recoverable cloud preflight + provider-aware reachability (M3)`.

### Task 3.3: `status()` additive fields

**Files:** Modify `Orchestrator/rerank.py:303-340` (`status`); `Orchestrator/routes/rerank_routes.py` unchanged; Test: `test_rerank.py`.

`status()` gains `tier` + `ram_mb` (from `hardware.probe()` — it already calls it, just drops them today), per-provider `reachable`, `auth_kind`, `key_present` (bool), `preflight_ceiling_ms`, and keeps **every** existing key (`gpu`/`service_reachable`/`enabled`/`available`/... — additive so the wizard's current bind still works and old frontends ignore new keys).

**Step 1 — failing test:** `test_status_carries_tier_ram_and_key_present`; `test_status_keeps_all_legacy_keys`; `test_status_never_500s`.
**Step 2-4:** FAIL → implement → PASS.
**Step 5:** Commit `feat(rerank): status() exposes tier/ram/key_present (M3)`.

---

## M4 — Key/auth plumbing + `rerank.json` sidecar

### Task 4.1: `rerank.json` sidecar accessors

**Files:** Modify `Orchestrator/embeddings/store.py` (add beside `get_placement`/`set_placement` `864-905`); Test: `Orchestrator/tests/test_embeddings_store.py` or a new `test_rerank_sidecar.py` (tmp_path).

`get_rerank_selection(base_dir=None) -> dict | None` and `set_rerank_selection({enabled, provider, model}, base_dir=None)` — direct copy of the `set_placement` shape: `_atomic_write_json`, fail-open (`FileNotFoundError`/`JSONDecodeError` → None), `RERANK_FILE = "rerank.json"`.

**Step 1 — failing tests:** round-trip write/read; missing file → None; corrupt JSON → None (fail-open); atomic write (no partial file). **Step 2-4:** FAIL → implement → PASS. **Step 5:** Commit `feat(store): rerank.json selection sidecar (mirror of placement.json) (M4)`.

### Task 4.2: `get_settings()` resolution order + live key reads + `is_enabled()`

**Files:** Modify `Orchestrator/rerank.py` (`get_settings` `164-191`, new `is_enabled`), `Orchestrator/config.py` (add `VOYAGE_API_KEY`/`COHERE_API_KEY`/`VERTEX_PROJECT_ID` constants near `386-392` — **but resolve keys via fresh `os.getenv` inside get_settings**, not the frozen constants, so a mirrored write is live); Test: `test_rerank.py`.

Resolution order: **`rerank.json` sidecar → config.ini `[rerank]` → code fallback**. Keys read fresh from `os.getenv(key_env)`. `is_enabled()` = sidecar `enabled` if the sidecar exists, else `CFG.getboolean("retrieval","rerank_enabled", fallback=False)`.

**Step 1 — failing tests:** `test_get_settings_prefers_sidecar_over_config`; `test_bearer_key_resolved_from_env_fresh`; `test_missing_key_is_configured_but_key_present_false`; `test_is_enabled_sidecar_then_config`.
**Step 2-4:** FAIL → implement → PASS.
**Step 5:** Commit `feat(rerank): sidecar>config>default resolution + live os.getenv keys + is_enabled (M4)`.

---

## M5 — CPU reranker provider (in-process CrossEncoder) + installer

### Task 5.1: `_score_cpu`

**Files:** Modify `Orchestrator/rerank.py` (`_score_cpu`, module-level model cache); add `qwen3-reranker-0.6b-cpu` to RERANK_MODELS (`provider:"cpu"`, `auth_kind:"none"`, `tiers:["MID"]`, `preflight_ceiling_ms:2000`, `preflight_passage_n:8`, `query_instruction` = the Qwen prefix, `vram_gb` omitted / `ram_gb`≈2); Test: `test_rerank.py` (monkeypatch a fake `CrossEncoder` — no torch in CI).

`_score_cpu` lazy-imports `sentence_transformers.CrossEncoder`, caches the loaded model module-side (like `_preflight_result`), prepends `query_instruction`, calls `model.predict([(query, p) for p in passages])`, returns a `list[float]` aligned to `passages`. Import/load failure → `None` (contract). Runs in the FastAPI threadpool (where `retrieve()` already executes) — no second service.

**Step 1 — failing tests:** aligned floats with a fake CrossEncoder; import failure → None never raises; query_instruction prepended (parity with the vLLM test at `test_rerank.py:174`); model cached (loaded once across two calls).
**Step 2-4:** FAIL → implement → PASS.
**Step 5:** Commit `feat(rerank): in-process CPU CrossEncoder provider (M5)`.

### Task 5.2: MID-tier CPU installer step

**Files:** Create `installer/templates/blackbox-install-reranker-cpu.sh`; Modify `Scripts/install.sh` (tier-gated non-fatal call); no test (shell — `bash -n` + `set -e` like the sibling script).

Script: gate on tier MID (`ram_mb ≥ 32768 && no GPU` — or just install unconditionally-non-fatally and let `_score_cpu` be the runtime gate; prefer explicit tier gate to avoid the 2 GB torch download on LOW boxes). `pip install torch --index-url https://download.pytorch.org/whl/cpu sentence-transformers` **into `Orchestrator/venv`**, pre-download `Qwen/Qwen3-Reranker-0.6B`. Idempotent (venv has it → skip/upgrade). Wired into `Scripts/install.sh` as a new step, non-fatal (mirror the GPU reranker step). **`torch`/`sentence-transformers` do NOT go in `requirements.txt`** — installer-added only, lazy-imported, so fresh LOW boxes stay clean.

**Step:** write script, `bash -n` clean, commit `feat(installer): MID-tier CPU reranker deps (torch-cpu + sentence-transformers) (M5)`.

---

## M6 — LLM-as-reranker provider

### Task 6.1: `_score_llm` (listwise, single completion)

**Files:** Modify `Orchestrator/rerank.py` (`_score_llm`); add four RERANK_MODELS entries `llm-rerank-{gemini-flash,gpt-mini,claude-haiku,grok}` (`provider:"llm"`, `auth_kind:"frontier_key"`, `key_env` = the matching frontier key, `tiers:["LOW","MID","HIGH"]`, `preflight_ceiling_ms:4000`, `preflight_passage_n:1`, NO query_instruction, `quality_note`:"General LLM, not a purpose-trained ranker — budget/keyless fallback"); Test: `test_rerank.py` (mock the HTTP completion).

Listwise: build a prompt = query + numbered snippets (each passage truncated ≤512 chars), request structured-JSON output = a permutation of the candidate indices. Parse defensively: not-JSON / missing / duplicate / extra / out-of-range index → `None`. Map the returned order → synthetic descending scores (`1/(1+rank)`), positionally aligned to `passages`. Single non-streaming completion; per-key model + endpoint reuse existing frontier plumbing (`config.py` frontier URLs/keys).

**Step 1 — failing tests:** valid permutation → correct order + aligned scores; malformed/short/duplicate JSON → None; snippet truncation applied; exactly one HTTP call (mocked); no query_instruction sent.
**Step 2-4:** FAIL → implement → PASS.
**Step 5:** Commit `feat(rerank): LLM-as-reranker listwise provider (M6)`.

---

## M7 — Dedicated cloud providers (Voyage / Cohere / Vertex)

### Task 7.1: `_score_voyage` + `_score_cohere` (bearer)

**Files:** Modify `Orchestrator/rerank.py`; add `voyage-rerank-2.5` (default cloud, `key_env:"VOYAGE_API_KEY"`) + `cohere-rerank-4` (`COHERE_API_KEY`) RERANK_MODELS entries (`auth_kind:"bearer_env"`, `privacy:"cloud"`, `tiers:["LOW","MID","HIGH"]`, `preflight_ceiling_ms:1200`, `preflight_passage_n:1`, NO query_instruction); Test: `test_rerank.py`.

Raw `requests` (no SDK deps). Voyage: `POST https://api.voyageai.com/v1/rerank` `{query, documents, model:"rerank-2.5", top_k}` Bearer → parse `{results:[{index, relevance_score}]}` → scatter by index into an aligned vector. Cohere: `POST https://api.cohere.ai/v2/rerank` `{model:"rerank-v4.0-pro", query, documents, top_n}` Bearer → `{results:[{index, relevance_score}]}`. Missing key → None; HTTP error/timeout/malformed/row-count-mismatch → None.

**Step 1 — failing tests (mock transport):** each parses `{index, relevance_score}` → aligned; scatter-by-index correctness (out-of-order indices); error/timeout/malformed → None; missing key → None. **Step 2-4:** FAIL → implement → PASS. **Step 5:** Commit `feat(rerank): Voyage + Cohere dedicated cloud rerankers via REST (M7)`.

### Task 7.2: `_score_vertex` (GCP service-account OAuth)

**Files:** Modify `Orchestrator/rerank.py`; add `vertex-semantic-ranker` entry (`auth_kind:"gcp_service_account"`, `key_env:None`, `preflight_ceiling_ms:1500`); Test: `test_rerank.py`.

`google.auth.default()` (transitively present via `google-auth-oauthlib`) → token; `POST https://discoveryengine.googleapis.com/v1/projects/{project}/locations/global/rankingConfigs/default_ranking_config:rank` `{model:"semantic-ranker-default-004", query, records:[{id,content}]}` with `Authorization: Bearer <token>` + `X-Goog-User-Project`. Project from the SA JSON `project_id` or `VERTEX_PROJECT_ID`. Creds from `GOOGLE_APPLICATION_CREDENTIALS` (already set + live-mirrored by `credentials_routes.py:138-144`). Parse `{records:[{id, score}]}` → align by id order. Any auth/HTTP failure → None.

**Step 1 — failing tests (mock google.auth + transport):** token-refresh path; parse → aligned by record id; missing creds → None; failure → None never raises. **Step 2-4:** FAIL → implement → PASS. **Step 5:** Commit `feat(rerank): Google Vertex semantic-ranker provider (SA OAuth) (M7)`.

**Reality flag (in the wizard copy, M10):** Vertex needs a GCP project + SA + discoveryengine enablement — far heavier than paste-a-key. Voyage is the **default** cloud path; Vertex is labeled **"Advanced"** with an explicit SA-setup panel.

---

## M8 — Rerank selector endpoint (replaces instruct-only)

### Task 8.1: `POST /rerank/select`

**Files:** Modify `Orchestrator/routes/rerank_routes.py` (add the route beside `GET /status` `20-25`), `Orchestrator/retrieval.py:309` (swap the enabled gate); reuse `Orchestrator/onboarding/secrets_writer.py:update_env`; Test: new `Orchestrator/tests/test_rerank_select_route.py` + `test_retrieval_rerank.py`.

Body `RerankSelectRequest {provider, model, enabled, api_key?}` (mirror `PlacementRequest`, `embeddings_routes.py:439-469`). Validate: provider ∈ KNOWN, model ∈ RERANK_MODELS, the model's `tiers` includes the current `hardware` tier (else 400). If `api_key` and the model's `key_env`: `update_env({key_env: api_key})` **and** `os.environ[key_env] = api_key` (live-mirror, exactly `credentials_routes.py:143`). Write the `rerank.json` sidecar (`set_rerank_selection`). Call `rerank.reset_preflight()`. Return fresh `rerank.status()`. Loopback-only (same trust model as `credentials_routes.py`). `retrieval.py:309`: `rerank_enabled = CFG.getboolean(...)` → `_rerank.is_enabled()`.

**Step 1 — failing tests:** select persists the sidecar; enable flips `is_enabled()` **without editing config.ini**; provider change resets preflight; tier-forbidden model → 400; `api_key` writes `.env` + mirrors `os.environ`; unknown provider/model → 404/400.
**Step 2-4:** FAIL → implement → PASS. `_apply_rerank` (`retrieval.py:203-264`) and its call site (`423-430`) stay untouched.
**Step 5:** Commit `feat(rerank): POST /rerank/select — tiered selector, key write+mirror, sidecar (M8)`.

**Milestone gate (M1-M8 backend complete):** restart; this LOW/MID box — `POST /rerank/select` a cloud provider with a real key → `GET /rerank/status` `available:true` → a `/debug/context` query returns reranked results; disable → clean fallback. Adversarial review; push.

---

## M9 — Embeddings CPU-slowness warnings (parallel to M2-M8; needs only M1)

### Task 9.1: Advisory `cpu_warning` + LOW-tier cloud steering

**Files:** Modify `Orchestrator/routes/embeddings_routes.py` (`_model_preflight` `136-181`, `embeddings_status` `184-264`); Test: `Orchestrator/tests/test_embeddings_status.py`.

Per LOCAL embedding model when `not hw.gpu`: add an advisory `cpu_warning` string = re-embed estimate from the snapshot count already in hand (`index_ids`, `embeddings_routes.py:196`) × a per-model CPU-rate constant → e.g. "~7,600 snapshots would re-embed on CPU (~2–3 h); a cloud model switches instantly." For **LOW** tier, add positive cloud steering. **Never** enters `blockers[]` — CPU stays "never a dead end" (`embeddings_routes.py:121,156`). Tie copy to `tier` (M1). `ram_preflight` unchanged.

**Step 1 — failing tests:** advisory present on local models when no GPU, absent with GPU, never in `blockers[]`; estimate scales with snapshot count; LOW-tier steering copy. **Step 2-4:** FAIL → implement → PASS. **Step 5:** Commit `feat(embeddings): hardware-tier CPU-slowness advisory + LOW-tier cloud steering (M9)`.

---

## M10 — Wizard reranker: keys in the API-Keys step + selector in the Memory step (surface 1/3)

**Brandon's UX decision (2026-07-04):** cloud reranker keys (Voyage, Cohere) belong in the **API-Keys step** alongside every other provider key — same paste + reveal + **Validate**-on-the-spot UX — NOT as paste fields buried in the reranker selector. The Memory-step reranker selector then just **selects** a provider from what's already configured/validated. This keeps one home for all keys and reuses the proven per-provider validator pattern.

### Task 10.0: Voyage + Cohere as first-class API-Keys entries + live validators

**Files:** Modify `Orchestrator/onboarding/validators.py` (add `validate_voyage` + `validate_cohere`), `Portal/onboarding/steps/api_keys.js` (add two PROVIDERS entries), the `/onboarding/validate` dispatch (find where it maps provider id → validator), `Orchestrator/onboarding/status_rollup.py` (`_PROVIDER_KEY` map ~59), `Orchestrator/onboarding/secrets_writer.py` (key-format regex `_KEY_RE` — confirm Voyage `pa-…`/Cohere alnum keys pass), `Orchestrator/onboarding/state.py` (validated_at tracking is generic — confirm). Tests: `Orchestrator/tests/test_validators*` / onboarding validate-route test.

- `validate_voyage(api_key)` / `validate_cohere(api_key)` follow the `_measure(fn) -> ValidationResult` pattern (validators.py:30), **raw `requests`, no new SDK**: Cohere uses the zero-cost `POST https://api.cohere.ai/v1/check-api-key` (returns `{valid, organization_name}`); Voyage uses a **1-document** `POST /v1/rerank` (tiny — stays under the free-tier 10K-TPM cap that a full 40-doc rerank exceeds; see the M8 live finding). Both return `ok`/`latency_ms`/`error` so the wizard shows clean feedback; a bad key → `ok:false` with the provider's error.
- `api_keys.js` PROVIDERS entries: `{id:"voyage", envVar:"VOYAGE_API_KEY", label:"Voyage (reranking)", description:"Reranks search results with a dedicated cross-encoder — sharper memory recall. Free tier available."}` and the same for Cohere. They render with the identical paste/reveal/Validate card the other keys use (no bespoke UI).
- Honest descriptions: frame both as **optional reranker upgrades** ("improves search result ordering; embeddings/memory work without it").

**Step:** TDD the two validators (mock the HTTP — valid → ok, 401/bad → not-ok, never raises); wire the dispatch + rollup + regex; DOM-check the two new cards render + Validate posts to `/onboarding/validate`. Commit `feat(onboarding): Voyage + Cohere reranker keys in the API-Keys step with live validators (M10.0)`.

### Task 10.1: Memory-step reranker selector (selects from configured keys)

**Files:** Modify `Portal/onboarding/steps/embeddings.js` (`rerankLineHtml` `379-414` → a tier-driven selector, `computeCardHtml` `289-322`, per-card `cpu_warning` in `renderCard`), `Portal/onboarding/onboarding.css`; manual/DOM test.

Render tier-appropriate options from `status.models` + `status.tier` + each cloud model's `key_present`:
- **Voyage / Cohere:** shown as selectable when their key is validated (`key_present:true`); if not, show "Add your Voyage/Cohere key in the API Keys step" with a deep-link back to that step (do NOT paste the key here — Task 10.0 owns key entry).
- **Vertex:** "Advanced" — deep-link the SA-upload (credentials UI); reachable via `GOOGLE_APPLICATION_CREDENTIALS` (M8 fix).
- **LLM (Gemini/GPT/Claude/Grok):** "uses your existing <provider> key" (no field), honest not-a-purpose-trained-ranker note.
- **CPU (MID) / GPU vLLM (HIGH):** tier-gated local options; CPU carries the "opt-in, may be slow" note; GPU keeps the installer remediation.
Show quality/cost/latency (`preflight.latency_ms`). Selecting POSTs `/rerank/select` **without** `api_key` (the key is already in `.env` from Task 10.0), then `refreshStatus()`. Respect the `status.enabled` field (now correct post-M8) for the on/off toggle.

**Step:** implement; assert tier→options mapping + the key-present gating + POST shape (no api_key in the body); `rerankStatus===null` still hides the block. Commit `feat(onboarding): Memory-step reranker selector over configured providers (M10.1)`.

**Milestone gate:** restart; drive the wizard on this box (LOW) — API-Keys step: paste + Validate the Voyage/Cohere keys live (they're already in `.env`; re-validate proves the button); Memory step: select Cohere (validated) → status `available` → toggle enable → a real query reranks; device-validate. Review; push.

---

## M11 — Portal updates-manager reranker block (surface 2/3)

### Task 11.1: Reranker card in `updates-manager.js`

**Files:** Modify `Portal/modules/updates-manager.js` (add beside `_computeCardHtml` `422-456`; new `fetch('/rerank/status')` — none today; `_onRerankSelect` mirroring `_onPlacementClick` `474-495`), `Portal/portal.css`; manual/DOM test.

Same `/rerank/status` + `/rerank/select` contract and tier→options mapping as M10; markup mirrored (not shared), per the established wizard-vs-portal split. Commit `feat(portal): reranker selector card in updates-manager (M11)`.

---

## M12 — Android reranker card (surface 3/3)

### Task 12.1: `RerankStatus` model + ViewModel flow + card

**Files:** Create `data/model/RerankStatus.kt` (`@Serializable`, `ignoreUnknownKeys` subset: `enabled`, `available`, `tier`, `provider`, `model`, `models`, `preflight`); Modify `ui/updates/UpdatesViewModel.kt` (`rerankStatus` StateFlow + `refreshRerank()`), `data/repository/UpdateRepository.kt` (GET `/rerank/status`, POST `/rerank/select`), `ui/updates/UpdatesScreen.kt` (`RerankCard` composable); Test: serialization round-trip + ViewModel state.

Scope Android to **select-an-already-keyed-provider + enable** (paste-key input is fine; Vertex SA upload deep-links to the Portal — SA JSON upload on mobile is disproportionate). Commit `feat(android): reranker status card + select (M12)`.

**Milestone gate:** build APK (`~/Android/Sdk/platform-tools/adb` — the Debian adb can't pair), install to the Fold, validate the card renders + a select persists. Review; push.

---

## M13 — Consolidation: portability, rollback, parity

### Task 13.1: Fresh-box + rollback regression tests

**Files:** `Orchestrator/tests/test_retrieval_rerank.py`, `test_rerank.py`, `test_hardware.py`, `test_embeddings_status.py`.

- `_apply_rerank` integration behaves identically across all providers (the contract is preserved) — parametrize over fake providers.
- Fresh LOW box: no `[rerank]` section, no `rerank.json`, no torch → `provider=null`, rerank inert, **no import errors** (pin as regression — audit A13 fresh-box rule). `requirements.txt` has no torch/cloud SDK.
- Rollback matrix asserted: delete `rerank.json` → config/null fallback; `[retrieval] rerank_enabled=false` config still force-off; each provider independently inert without its dep/key; `tier` advisory-only (never blocks).

**Step:** write tests → all green → guarded suite green → Commit `test(rerank): fresh-box portability + rollback matrix + provider-agnostic apply (M13)`. Final adversarial review; push; `/snapshot-dev`.

---

## M14 — Body-only ranking (measured discovery, 2026-07-04)

**Context/measured basis:** every snapshot leads with a fixed bookkeeping envelope
(`=== START SNAPSHOT … === CROSS-FILE BEACON … VOLUME TRACKER … GAUGES …`) before the
`SNAPSHOT BODY` → `Raw Session Log` (the user+AI turns — the actual content). Reranking on the
envelope-prefixed passage measurably hurts: on 26 same-corpus labeled queries, **Vertex
semantic-ranker went recall@10 0.654 → 0.846 (+29%), MRR 0.352 → 0.495 (+41%) when passed
body-only text instead of the envelope window** (Cohere is robust to the envelope but also
benefits; it couldn't be cleanly re-measured due to its free-trial per-minute rate limit). The
envelope also dilutes every EMBEDDING chunk (the first chunk of each snapshot is mostly
boilerplate → a near-identical vector across all snapshots). **Brandon's decision (2026-07-04):
strip the envelope everywhere ranking happens — rerank passages AND embedding chunks (accept a
full re-embed); keep Cohere the default reranker regardless of the head-to-head.**

North star: content-only ranking gets "right to the meat" — the reranker and the embedder both
score the user message + AI response, not the bookkeeping.

### Task 14.1: `extract_snapshot_content()` — the one body extractor
**Files:** new helper in `Orchestrator/fossils.py` (beside `window_snapshot_text`); Test: `test_fossils_body.py`.
`extract_snapshot_content(text) -> str`: return from the `Raw Session Log` marker onward (the
user+AI turns + any Release Notes/summary that follows — those are dense, keyword-rich content);
if absent, fall back to the `SNAPSHOT BODY` marker; if neither present, return the full text
(robustness — never returns empty for a non-empty input). Never raises. Tests: real snapshot →
drops BEACON/TRACKER/GAUGES header, keeps the session log; a snapshot missing the markers →
full text; empty → empty. **Measured basis:** the eval that lifted Vertex used exactly this
"from Raw Session Log / SNAPSHOT BODY onward" cut.

### Task 14.2: Rerank passages use body-only (immediate, no re-embed)
**Files:** `Orchestrator/retrieval.py` `_apply_rerank` passage building (~240-255); Test: `test_retrieval_rerank.py`.
Build each rerank passage from `extract_snapshot_content(decoded_text)` truncated to
`passage_chars`, instead of the envelope-inclusive `window_snapshot_text`. (For long bodies the
head of the session log is the most representative; the chunk-ordinal window was designed for
envelope-inclusive chunk space and is inconsistent with body extraction — prefer the clean body
head. Note this in a comment.) Test: the passage passed to `rerank.score` contains the session
log, not the BEACON header. **Then re-run the Cohere-vs-Vertex head-to-head on body-only passages
(clean, Cohere un-rate-limited) and record the numbers — Cohere stays default per Brandon; the
data just confirms the gain and Vertex's rescue.**

### Task 14.3: Embedding chunks body-only (mint path)
**Files:** the snapshot embed seam — `Orchestrator/embeddings/search.py::embed_snapshot_for_index`
(or `chunker.chunk_snapshot`'s caller) so the chunker receives `extract_snapshot_content(text)`,
NOT the full envelope-prefixed text; `Orchestrator/checkpoint.py` mint sites feed the body.
Tests: a minted snapshot's chunks derive from the session log (no BEACON in chunk-0). **Do NOT
strip the envelope inside `chunk_snapshot` itself** (it's a generic chunker — the snapshot-only
body extraction belongs at the snapshot embed seam, mirroring the ToolVault-protection rule from
the retrieval upgrade). New snapshots embed body-only immediately; the existing corpus is handled
by 14.4.

### Task 14.4: Re-embed the corpus body-only (gated cutover)
Reuse the retrieval-upgrade M6f machinery (build-only migrate rebuild → `_build` candidate →
calibrate → gate → explicit stop-swap-restart cutover → retain `.pre-body` rollback dir). The
active `gemini-embedding-2` store is rebuilt body-only (~$2–3 cloud, <1h). **Gate:** the eval
harness (`eval/run_bench.py`) shows semantic recall@10 ≥ the current envelope-inclusive baseline
(expect improvement — cleaner vectors), goldens + lean-profile pass against the candidate, and
the rerank head-to-head re-run on the body-only store confirms the gain. Then cut over; keep the
`.pre-body` store for instant rollback. The Ultra's qwen store rebuilds body-only lazily on its
own switchover. Runbook artifact: `docs/plans/artifacts/2026-07-0X-body-only-reembed-runbook.md`.

**Sequencing:** 14.1→14.2 land + measure first (immediate rerank win, no re-embed). 14.3 (mint
body-only) before 14.4 (so mints during the rebuild are already body-only — the A4 lesson). 14.4's
candidate can build in the background while the UI milestones (M10–M12) proceed; gate+cutover when
green. Cohere stays the live default throughout.

> **OUTCOME (2026-07-05): 14.4 re-embed ABANDONED — gate FAILED, body-only embeddings do not
> help retrieval.** The full 7,648-snapshot body-only candidate built clean (26,225 chunks vs the
> full store's 33,039 — ~21% boilerplate removed) with `content_mode="body"`. But the six-gate
> eval, read apples-to-apples (fresh full store vs fresh body candidate, both from `runs` in
> `eval/results/2026-07-04-chunk-gate.json`), showed body-only is **neutral-to-negative** on the
> production **hybrid** path: overall recall@10 **0.634 → 0.612 (−11 queries)**, worse in <6k
> (−10 q) and 6–10k (−3 q), only +2 q in >10k. Semantic-only (phone lean) was +6 q net. **Caution
> on the gate table's own columns:** its overall/>10k gates compare the candidate against a STALE
> July-2 recency-sweep baseline (0.489) — that inflated a −2pt change into an apparent "+25% win."
> Always read `runs.baseline` (fresh current store) vs `runs.candidate`, not the table's baseline
> column, for a cutover decision. **Why:** a bi-encoder already averages the near-constant envelope
> into a tiny offset that barely moves cosine ranks, while re-chunking the body shifts chunk
> boundaries and drops the envelope's structured terms that the keyword channel uses — so hybrid
> loses more than the boilerplate removal gains. This is the OPPOSITE of 14.2 (rerank), where a
> cross-encoder reads whole passages and boilerplate genuinely distracts it (Vertex 0.654→0.846).
> **Same idea, opposite outcome, because embedding and reranking consume text differently.**
>
> **Decision (Brandon):** do NOT cut over — keep the full-envelope store. Candidate discarded.
> **KEEP the M14.3 `content_mode` infrastructure inert** (store flag + body-mint + windower branch
> + watcher fix, commits `2dd1b94`/`19f89ac`/`33c8c58` watcher) — it's byte-identical on a
> full-mode store, tested, and lets a FUTURE model/box that benefits re-embed body-only. Full is
> the proven default for gemini-embedding-2 on this corpus; the finding is model/corpus-specific,
> not universal. The real body-only wins (14.2 rerank + M15 delivery) are orthogonal to what is
> embedded and remain shipped. `.pre-body` rollback dir was never needed (no cutover occurred).

---

## M15 — Delivery-side snapshot formatting (measured, 2026-07-04)

**Context/measured basis:** the envelope isn't only bad for ranking — it ships to the MODEL. A live
inspection of one query's assembled context (`context_builder.build_fossil_context`) delivered
**250,718 chars (~62,700 tokens) across 19 snapshot blocks, each carrying ~1,000 chars of
bookkeeping** (`=== START SNAPSHOT … === CROSS-FILE BEACON … Tail-first sweep resolved tip …
COUNT=1 | TARGET_ID … UFL: OUTSIDE-JUNK IGNORED | BYTES_AFTER_END=0 … Tail lock confirmed …
VOLUME TRACKER … GAUGES … Kernel Index …`) before the content — **~20,000 chars (~5,000 tokens,
~8% of the turn) of pure internal bookkeeping** the model can't use and that competes with the real
memory. **Brandon's decision (2026-07-04): deliver only the useful sections — a compact snapshot
attribution (SNAP id + date + operator — the model must know which snapshot each memory is from),
Context Provenance (the fossil lineage), and the Raw Session Log (user message + AI full response) +
any Release Notes summary. Drop BEACON / VOLUME TRACKER / GAUGES / Kernel Index.**

Distinct from M14: M14 strips for RANKING (embed + rerank score on Raw Session Log only); M15 strips
for DELIVERY (keeps a clean ID attribution + Context Provenance + content). Same "kill the envelope"
theme, different kept-set.

### Task 15.1: `format_snapshot_for_delivery()`
**Files:** new helper in `Orchestrator/fossils.py` (near `extract_snapshot_content` from M14.1);
Test: `test_fossils_body.py`.
`format_snapshot_for_delivery(text) -> str`: emit a compact attribution header `[SNAP-… · <UTC
date> · operator: <op>]` (parsed from the START line + GAUGES `OPERATOR:`), then the `Context
Provenance` section (if present), then from `Raw Session Log` onward (content + Release Notes).
Drop the START/BEACON/VOLUME-TRACKER/GAUGES/Kernel-Index lines. Robust: markers absent → return the
text unchanged (never empty for non-empty; never raises). Preserve enough of GAUGES to build the
attribution (date/operator) but not the full block. Tests: a real snapshot → output has the
`[SNAP-… · … · operator]` header + Context Provenance + Raw Session Log, and does NOT contain
"CROSS-FILE BEACON"/"Tail lock confirmed"/"BYTES_AFTER_END"/"VOLUME TRACKER"/"GAUGES"/"Kernel
Index"; markers absent → unchanged; measure the char reduction (~1,000 chars/snapshot dropped).

### Task 15.2: Apply at the delivery/context-assembly seam
**Files:** `Orchestrator/context_builder.py` (where the retrieved whole-snapshot texts are assembled
into the fossil-context string — the recent/keyword/semantic/checkpoint sections); confirm whether
to format in `context_builder` or in the `fossils.py` delivery shims (`hybrid_retrieve` /
`semantic_retrieve`). DECIDE: format in the CONTEXT-ASSEMBLY path only (chat/voice/MCP context
injection), NOT in raw single-snapshot tools (`get_snapshot`, the Android timeline `/fossil/snapshot`
— those legitimately show the full snapshot). So the seam is `context_builder` (and the chat-loop
`hybrid_retrieve` consumers if they bypass context_builder — grep). Apply `format_snapshot_for_delivery`
to each delivered block; the `max_fossil_chars` / WI-10 window guards still apply AFTER formatting
(now they cap cleaner text). Tests: assembled context contains the attribution header, not the
beacon noise; per-turn char count drops ~5–8%; the snapshot-id attribution is present so the model
still knows the source; a raw `get_snapshot` call is UNCHANGED (full envelope preserved).

**Measured basis:** the inspection above (250,718 chars, 19 blocks, ~1,000-char envelope each).
**Verify end-to-end:** re-run the inspection after 15.2 — the same query's context should drop
~5,000 tokens and read clean (attribution + provenance + session log, no bookkeeping). Commit each
task; adversarial review; this is context the model sees, so device-validate a real chat turn.

### Task 15.3: The AI-invoked SEARCH tools return body-only results (Brandon addition 2026-07-04)
When an AI model calls the snapshot-search tool, results must be reranked (already true — verified)
AND body-only formatted (not yet). **VERIFIED LIVE:** both search surfaces already run the full
`retrieve()` pipeline incl. rerank — `[RERANK] provider=cohere` fires on both — so the RANKING
requirement is met; only the RESULT FORMATTING carries envelope noise.
- **`ToolVault/tools/search_snapshots/executor.py`** (the chat-model tool + on-device
  `/local/tools/execute`): today it returns each `hybrid_retrieve` result as the WHOLE
  envelope-inclusive `snap_text` (executor.py:59-60). Apply `format_snapshot_for_delivery` to each
  result before the `--- Result i ---` join. On-device (window_budget_chars set): extract body
  FIRST, then respect the budget window — compose body-extraction with the existing
  `LOCAL_RESULT_BUDGET_CHARS` windowing so the phone still gets a size-bounded but body-only result.
- **`/fossil/hybrid`** (`Orchestrator/routes/task_routes.py`, the MCP `search_snapshots` /
  `get_context` path via `blackbox_mcp_server.py`): today it returns a 500-char `snippet` that is
  the HEAD of the whole snapshot = pure BEACON/START envelope (a useless preview). Build the snippet
  from `extract_snapshot_content(text)` (the session-log head), so the MCP client sees a meaningful
  content preview, not bookkeeping. Keep the ranked `snap_id` + `similarity` fields.
- **North star (Brandon):** "we don't want the search to pull back junk and noise — actual results,
  perfect results, no matter what." Both AI-facing search surfaces return reranked, body-only,
  attribution-tagged results.
- Tests: the executor output for a full-envelope snapshot contains the session log + attribution,
  NOT "CROSS-FILE BEACON"; the `/fossil/hybrid` snippet is body content, not the START header;
  on-device path stays within its char budget AND is body-only. **Live-verify:** an MCP/tool
  `search_snapshots` call returns reranked + clean results (re-run the live check above).
Commit `feat(search): search_snapshots + /fossil/hybrid return reranked body-only results (M15.3)`.

**Sequencing:** M15 after M14.1 (shares `fossils.py`; avoid a conflicting concurrent edit). It's
independent of the re-embed (delivery formatting is orthogonal to what's embedded). 15.1→15.2→15.3
in order (15.3 reuses 15.1's `format_snapshot_for_delivery`/`extract_snapshot_content`).

---

## Risks & rollback (top line)

| Risk | Mitigation |
|---|---|
| Cloud 500 ms ceiling false-fails + disables for the process | Per-provider ceilings from RERANK_MODELS (M3.1); cloud preflight TTL-recoverable (M3.2) |
| CPU 0.6B breaches interactive budget on 40 passages | Realistic 8-passage extrapolated preflight (M3.1) auto-disables; CPU tier may lower `rerank_candidate_n`; explicit "may be slow" wizard copy |
| Vertex auth heaviness (GCP project + SA) | Voyage is the default cloud path; Vertex = "Advanced" panel reusing the credentials upload; not a bare key field |
| Newly-pasted key needs a restart | Selector mirrors key into `os.environ` (like `credentials_routes.py:143`); `get_settings` reads `os.getenv` fresh |
| Heavy deps (torch/SDKs) bloat fresh LOW boxes | torch/ST installer-added for MID only, lazy-imported, absent → inert; cloud = raw `requests`, no SDKs |
| Any provider failure empties memory | Every `_score_*` returns `None` on failure → `_apply_rerank` returns None → un-reranked ranking stands (north-star invariant, tested M13) |

## Out of scope

- Activating the reranker on the **Ultra** (GPU/vLLM already works) and switching over its chunked qwen3-8b candidate — separate M6f-style gate, runbook already written.
- WI-6 Phase B rerank-quality eval (rerank on/off recall@k on `eval/labeled_set.jsonl`) — run once a provider is live to pick the default; not a ship blocker.
