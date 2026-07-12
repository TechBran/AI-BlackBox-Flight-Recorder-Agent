# Local Image Generation (Z-Image Turbo) — Integration Design

**Date:** 2026-07-12
**Status:** Approved — design decisions validated with Brandon (AskUserQuestion, this session)
**Author:** Brandon-DEV session
**Implementation plan:** `docs/plans/2026-07-12-local-image-generation.md`

---

## Goal

Add the local llama-swap image server — **Z-Image Turbo** on the RTX 2000 Ada, served OpenAI-compatible at `http://192.168.1.50:8080/v1` — as a first-class, **FREE** image-generation provider (`"Local (free)"`) in the BlackBox generation surface (Portal + Android) and as a ToolVault tool the models can call, **reusing the credentials already stored** for the `gemma-box` custom chat server.

## Context / discovery (all live-verified this session)

- The image server is the **same box already registered as the `custom` chat provider**: `credentials/custom_models.json` → alias `gemma-box`, `base_url: http://192.168.1.50:8080/v1`, key present, `enabled: true`. Its `last_models` already lists `z-image` alongside `gemma-12b/26b/31b` — which is exactly why it looked "already populated in the provider list."
- Because the endpoint is OpenAI-compatible, `z-image` currently **leaks into the CHAT model dropdown** (`GET /models/custom` → `gemma-box::z-image`). It can't chat — a wart this integration fixes.
- **Live contract (probed):** `POST /v1/images/generations {model:"z-image", prompt, size, n, output_format}` → `{created, data:[{b64_json}], output_format}`. Cold-start **37 s** @ 1024², decoded to a valid 1024×1024 PNG. No `url` field — base64 inline.
- **Eviction is a graceful server-side queue** (Brandon-clarified): an image call completes, then the model evicts itself, then the next model loads. So the BlackBox needs a **long client timeout (180 s)**, *not* a cross-subsystem lock. Idle-unload after 10 min.

## Design decisions (validated)

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| 1 | **Modality classification** — how to tell an image model (`z-image`) from a chat model (`gemma-31b`) on the shared endpoint (they're identical in `/v1/models`) | **Name-pattern allowlist** (`z-image`, `flux`, `qwen-image`, `sdxl`, `sd3`, `stable-diffusion`, …, plus `*-image`) | Zero setup; every "addable" model in the guide matches; editable in one constant |
| 2 | **Naming** | Tool `local_image`, provider id `local`, dropdown label **"Local (free)"**; the model is a *param* (defaults to `z-image`) | Portable (no hardcoded `gemma-box`), fresh-box-safe, future-proof for FLUX/Qwen, and the "free/local" framing steers model selection |
| 3 | **Parameter scope (v1)** | **Parity only**: `size` + `numberOfImages` | Both are already `GenIn` fields with UI labels on both surfaces → **frontends need zero code changes**; passes the coherence test cleanly; uses the server's tuned 8-step/CFG-1.0 defaults |

## Architecture

**The request path is already provider-agnostic.** `provider` is a free-string passthrough: `GenIn.provider` → `/generate/image` builds `image_options` → the task worker does `IMAGE_PROVIDERS.get(provider)`. Both frontends render whatever `GET /image/catalog` advertises. So a new provider is **additive** — concentrated in backend registries, not scattered through the UI or the request path.

**Credentials are reused, not re-entered.** The adapter resolves `base_url` + `api_key` + bare model from the existing `custom_servers` registry (fresh-read). Nothing is hardcoded to `gemma-box` — *any* enabled custom server that hosts a name-matched image model works. This is the fresh-box-portable path.

**Availability is registry-gated, not env-gated.** Cloud providers enable when their API-key env var is present. `local` has no env key; it enables **iff an enabled custom server hosts an image-classified model**. This check is lazy-imported + fail-soft so it can never zero out the lean-venv MCP tool list.

**Eviction needs only a long timeout.** The server queues gracefully, so no lock, no pre-warm orchestration in v1 — just `timeout=180` on the adapter (matches the other adapters; absorbs the ~37 s cold swap).

## Touchpoint map

**Backend (all the real work):**
- `Orchestrator/onboarding/custom_servers.py` — new `is_image_model()` classifier + `resolve_image_server()` + `list_image_models()` helpers.
- `Orchestrator/image_providers.py` — new `_local_images()` adapter; register in `IMAGE_PROVIDERS` + `IMAGE_TOOL_PROVIDERS`.
- `Orchestrator/toolvault/availability.py` — `local` in `FEATURES["image"]`; `_local_image_available()`; wire into `enabled_providers("image")`.
- `Orchestrator/image_catalog.py` — `IMAGE_PROVIDER_SPECS["local"]` + add to display-order list.
- `Orchestrator/tasks.py` — `local` in `_IMAGE_MODELS` provenance + a `local` metadata branch.
- `Orchestrator/routes/admin_routes.py` — filter image-classified models **out** of the chat catalog (`_fetch_custom_models`).
- `ToolVault/tools/local_image/{schema.json,executor.py}` — new tool; description **leads with FREE/local/private** (the model-selection signal).
- Dispatch recognition sites — add `local_image` wherever the image tools are enumerated for the `image_task` placeholder animation (grep `IMAGE_TOOL_PROVIDERS` / `openai_image`).
- Tests — coherence probe for `local`; hermetic fixes to the catalog tests; classifier/resolver/gating unit tests.

**Frontend:** **no core change** (catalog-driven; `size`/`numberOfImages` labels already exist). Optional warm-status dot + cold-start hint = deferred polish (Phase 9).

**Request path / `GenIn` / onboarding:** no change (fields + passthrough already present; `IMAGE_ENABLED` unset today, so `local` auto-enables via the registry).

## Non-goals / deferred

- Advanced controls (`steps` 4–8, `seed`, `model` picker) via `<sd_cpp_extra_args>` — deferred (Brandon chose parity-only).
- UI warm-status dot + "first image ~35 s" hint — optional Phase 9.
- Multiple local image models (FLUX/Qwen) surfaced as a picker — resolver already selects the first image model; a picker is a fast-follow.
- Local image-to-image / reference images — deferred (mirrors the gemini-only v1 catalog omission).
- Making `local` the *default* image provider — no; it's a free alternative the model chooses via the description. `IMAGE_DEFAULT` unchanged.

## Risks & mitigations

- **Name-pattern misclassification** → accepted (low risk on a personal box); the allowlist is one editable constant, and Hybrid/wizard-tag remains an easy upgrade path.
- **MCP lean-venv zero-tools disaster** ([[feedback-mcp-lean-venv]]) → the registry check is a lazy, `try/except`-wrapped import; a failure returns `False` (local off), never propagates to break `enabled_providers` for other tools.
- **Coherence + catalog tests read the real registry** → make them hermetic (patch `_local_image_available`/`resolve_image_server`); add a `local` adapter probe so `test_every_catalog_param_reaches_its_adapter` passes.
- **Cold-swap latency** → 180 s timeout; the tool description sets latency expectations so the model picks cloud when speed matters.
