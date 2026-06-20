# Multi-Provider Image Generation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Build on `main` (staging-as-prod — NO worktrees). Stage explicit paths only, never `git add -A`. Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` via `-F -` heredocs (no backticks). GitHub push = ship AFTER device/live validation. Edit tool is BLOCKED under this dir (`c./` false-positive) → edit via Python scripts in Bash with verbatim-anchored replacements; CHECK + PRESERVE line endings per file (`grep -c $'\r'`).

**Goal:** Replace `generate_image` with per-provider image tools (`gemini_image`/`openai_image`/`grok_image`), provider-tagged async worker routing, a feature-aware availability gate, a `GET /image/catalog` SoT, and a provider-aware generation UI on Portal + Android.

**Architecture:** Mirrors the shipped multi-provider web search (per-provider tools + x-availability gate + onboarding + default hint + dispatch catch-all + MCP), adapted for the ASYNC image pipeline: per-provider tools create `IMAGE_GENERATION` tasks tagged with a `provider`; the worker (`tasks.py process_task`) routes to a per-provider adapter that yields bytes written to the predicted `/ui/uploads` path; a backend catalog drives provider-aware param UIs on both surfaces.

**Tech Stack:** Python/FastAPI, the task worker (`Orchestrator/tasks.py`), `requests`, ToolVault v2 modules, pytest; Portal vanilla-JS (`generation-modals.js`); Android Kotlin/Compose.

**Design:** `docs/plans/2026-06-20-multi-provider-image-gen-design.md` · **Spike:** `diagnostics/imagegen_spike.py` (all 3 providers pass) · **Precedent plan:** `docs/plans/2026-06-20-multi-provider-web-search.md`.

**Run tests:** `Orchestrator/venv/bin/python -m pytest <path> -v`. **Full suite MUST include** `Orchestrator/tools` (the web-search final review learned this the hard way).

---

## Task 1: Feature-aware availability gate

Generalize the web-search gate so it serves multiple features (image + web_search; extensible to video/music). Keep it stdlib-only / lean-venv-safe.

**Files:** Modify `Orchestrator/toolvault/availability.py`; Test `Orchestrator/toolvault/tests/test_availability_features.py` (create).

**Design:** introduce a feature registry:
```python
FEATURES = {
    "web_search": {"enabled_pref": "WEB_SEARCH_ENABLED", "default_pref": "WEB_SEARCH_DEFAULT",
                   "provider_env": {...current web PROVIDER_ENV...}},
    "image": {"enabled_pref": "IMAGE_ENABLED", "default_pref": "IMAGE_DEFAULT",
              "provider_env": {"gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY", "grok": "XAI_API_KEY"}},
}
```
- `x-availability` gains an optional `feature` (default `"web_search"` for back-compat with the shipped web tools).
- `enabled_providers(feature)` (rename/wrap `enabled_web_search_providers` → keep a back-compat alias) reads `FEATURES[feature]["enabled_pref"]` (unset → every provider with a key present; for `image` there is NO keyless floor, so do NOT auto-add duckduckgo — image has no keyless member).
- `is_available(entry, ...)` reads `entry["x-availability"]["feature"]` (default web_search) and checks `requires_env` present AND provider in `enabled_providers(feature)`.
- Apply the gemini GEMINI_API_KEY→GOOGLE_API_KEY alias (already in `_read_env`) — image's gemini provider benefits from it too.

**Steps (TDD):** test image gate (key×enabled matrix; web_search entries still work unchanged; image has no keyless floor); the existing `test_availability.py` must stay green (back-compat). Verify lean-venv import (only `os`).
**Commit:** `feat(toolvault): feature-aware availability gate (image + web_search)`.

---

## Task 2: Image provider adapters + worker routing

Route `IMAGE_GENERATION` tasks to the tagged provider; per-provider adapters produce bytes written to the predicted path.

**Files:**
- Create: `Orchestrator/image_providers.py` (adapters → bytes).
- Modify: `Orchestrator/tasks.py` (`process_task` image branch ~263-320 — route by `task` provider; currently hardcodes `call_imagen`); `Orchestrator/routes/tts_routes.py` (`/generate/image` GenIn + `generate_image` ~647 — accept + store `provider`).
- Test: `Orchestrator/tests/test_image_providers.py` (create).

**Adapters** (each `(prompt, options) -> list[bytes]`):
- `_openai_images(prompt, options)`: `POST https://api.openai.com/v1/images/generations` `{model, prompt, n, size, quality}` → `data[].b64_json` → `base64.b64decode`. Model: quality-first gpt-image tier (confirm `gpt-image-1` works; prefer newest stable per `feedback-brandon-ga-vs-preview`).
- `_xai_images(prompt, options)`: `POST https://api.x.ai/v1/images/generations` `{model:"grok-imagine-image-quality", prompt, n}` → `data[].url` → **download immediately** (temp URL) → bytes.
- `_gemini_images(prompt, options)`: reuse the existing `call_imagen` (Gemini Nano Banana) — return its bytes/artifacts.
```python
IMAGE_PROVIDERS = {"gemini": _gemini_images, "openai": _openai_images, "grok": _xai_images}
```
In `process_task`'s IMAGE_GENERATION branch: read `provider = (task.result_data.get("options") or {}).get("provider") or <IMAGE_DEFAULT>`; dispatch to `IMAGE_PROVIDERS[provider]`; write each image's bytes to the SAME predicted `/ui/uploads/{slug}_{task_id}_{i}.png` path the worker already uses; keep the existing artifact/all_urls result shape so the UI is unchanged. Untagged/legacy tasks → default provider (back-compat).

**Steps (TDD):** mock each adapter's HTTP; assert b64-decode (openai) and url-download (xai) produce bytes; assert `process_task` writes files to the predicted path for each provider and records `all_urls`. Live smoke deferred to Task 10.
**Commit:** `feat(image-gen): per-provider adapters + worker routing (openai/xai/gemini)`.

---

## Task 3: Three tool modules (+ remove generate_image)

**Files:** Create `ToolVault/tools/{gemini_image,openai_image,grok_image}/{schema.json,executor.py}`; Delete `ToolVault/tools/generate_image/`; Test `Orchestrator/toolvault/tests/test_image_tools.py`.

Each schema: category `media_generation`, the 7 groups, tier 2, `x-availability:{feature:"image",provider:<p>,requires_env:[<key>]}`, params per provider (Gemini: prompt/reference_images/aspectRatio/resolution/numberOfImages; OpenAI: prompt/size/quality/background/n; xAI: prompt/n/aspectRatio). Executor mirrors the old `generate_image` executor but POSTs `/generate/image` with `provider:"<p>"` in the body (so the task is tagged). Use `ToolVault/tools/generate_image/` (pre-deletion) + `roll_dice` as templates.

**Steps (TDD):** assert the 3 tools load, `generate_image` gone, each carries `x-availability.feature=="image"`, `validate` exits 0. **Commit:** `feat(image-gen): three per-provider image tools; remove generate_image`.

---

## Task 4: `GET /image/catalog` endpoint + param-spec SoT

**Files:** Create `Orchestrator/image_catalog.py` (per-provider param spec); Modify a routes file (e.g. `Orchestrator/routes/tts_routes.py` near `/generate/image`, or a new `media_routes.py`) to add `GET /image/catalog`; Test `Orchestrator/tests/test_image_catalog.py`.

`GET /image/catalog` → `[{provider,label,default,params:[{name,type,options|min/max,default}]}]` for each ENABLED image provider (enabled set + default from `availability.enabled_providers("image")` + `IMAGE_DEFAULT` — DRY). Param specs live in `image_catalog.py` (one place). **Steps (TDD):** shape + enabled-filtering (disabled provider absent; default flagged); fresh `.env` read. **Commit:** `feat(image-gen): GET /image/catalog provider+param SoT`.

---

## Task 5: Dispatch migration (image_task sites)

**Files:** Modify `Orchestrator/routes/chat_routes.py` (image_task sites ~535,1158,1894,2599,4471), `gemini_live_routes.py` (~958-975), `grok_live_routes.py` (~661-682), `realtime_routes.py` (~813). CHECK line endings (gemini_live/grok_live are CRLF).

These create the IMAGE_GENERATION task + emit the provider-agnostic `image_task` event. Currently keyed on `tool_name == "generate_image"`. Update to recognize `gemini_image`/`openai_image`/`grok_image`, set the task's `provider` accordingly, and keep the predicted-URL/`image_task` payload identical (so Portal/Android animations are unchanged). Update any system-prompt text naming `generate_image`. **Steps:** source-guard test (no `== "generate_image"` remains; new names route with provider) + import smoke. **Commit:** `feat(image-gen): dispatch per-provider image tools with provider tag`.

---

## Task 6: Onboarding image step (backend)

**Files:** Modify `Orchestrator/onboarding/state.py` (add `"image"` to StepName + ALL_STEPS — after `web_search`, before `pair_phone`), `Orchestrator/routes/onboarding_routes.py` (`current_config` image block reusing `availability.enabled_providers("image")`; `/save` persists `IMAGE_ENABLED`+`IMAGE_DEFAULT` — secrets_writer has no allowlist, just a key regex, so they pass). Test `Orchestrator/tests/test_onboarding_image.py`. Mirror the web-search onboarding task exactly (no keyless option). **Commit:** `feat(onboarding): image provider step (enable list + default)`.

---

## Task 7: Default-provider hint (feature-generalized)

**Files:** Modify `Orchestrator/toolvault/availability.py` (generalize `default_web_search_hint` → `default_provider_hint(tool_names, feature)` or add an image variant), `Orchestrator/toolvault/injector.py` (`build_tool_instructions` also appends the image hint when an image tool is injected). Test `Orchestrator/toolvault/tests/test_image_hint.py`. Hint: "For image generation, prefer `<default>_image`; other image providers are available to compare." **Commit:** `feat(image-gen): default-provider hint`.

---

## Task 8: Portal provider-aware image UI (catalog-driven)

**Files:** Modify `Portal/modules/generation-modals.js` (image modal). READ the current image modal + the music modal first as templates.

Replace the hardcoded single-provider params with: a **provider `<select>`** populated from `GET /image/catalog` (default preselected), + a params container rendered DYNAMICALLY from the selected provider's `params` schema (enum→select, number→input with min/max, boolean→checkbox, array→the existing reference-image picker). On dropdown `change`, re-render the params. Submit posts `{provider, ...params}` to `/generate/image`. Bump the Portal cache version. **Verify:** `node --check`; manual checklist (dropdown lists enabled providers; switching swaps params; defaults applied; submit carries provider). **Commit:** `feat(image-gen): provider-aware Portal image modal (catalog-driven)`.

---

## Task 9: Android provider-aware image UI (catalog-driven)

**Files:** LOCATE the Android image-gen screen first (`grep -rl "generate/image\|aspectRatio\|numberOfImages" <android src>`; it wasn't found by keyword — check the `AI_BlackBox_Portal` Android MVP media/generation screens). Add a DTO for `/image/catalog`, a provider dropdown, and dynamic param controls per the catalog schema (Compose). On provider change, swap the param controls. Mirror the music-gen screen's pattern. **CRLF/Edit caution:** Kotlin files may be CRLF (memory: ChatViewModel/LocalModelService/NavGraph are) — preserve; edit via Python. **Verify:** `assembleDebug` (or the project's build) compiles. **Commit:** `feat(image-gen): provider-aware Android image UI (catalog-driven)`.

---

## Task 10: MCP + integration smoke + final review + push

1. `grep` sweep for remaining `generate_image` literals (intentional: legacy `/generate/image` route name stays; tool name gone). Confirm MCP lists the 3 image tools (availability-filtered) and a model can poll a generated task via `get_task_status`.
2. `sudo systemctl restart blackbox.service` (pre-authorized) → `POST /toolvault/reload` (embed the 3 new tools) → confirm `test_validate_all_real_tree_ok` passes.
3. **Live smoke:** generate 1 image per provider through the real pipeline (the per-provider tools / `/generate/image` with each provider) → confirm files land at predicted paths + `image_task` events fire. Open the Portal image modal + the Android screen → dropdown swaps params.
4. Full suite incl. `Orchestrator/tests Orchestrator/toolvault/tests Orchestrator/tools` → green (note any pre-existing reds).
5. Dispatch the final whole-diff `superpowers:code-reviewer` (holistic — provider-identifier consistency across worker/adapters/tools/catalog/onboarding/UI/MCP; key-leak check on the new adapters' error paths; the xAI temp-URL fetch; Portal/Android catalog-contract alignment).
6. **DEVICE/LIVE VALIDATION GATES THE PUSH** — Brandon validates (chat image gen across providers + the Portal & Android dropdowns) → then push.
7. Memory (`project_multi_provider_image_gen.md` + MEMORY.md) + `/snapshot-dev`.

---

## Post-review follow-ups (non-blocking)
- Image-to-image (reference images) varies by provider — catalog should reflect per-provider capability; v1 may gate it to supporting providers.
- Confirm a GA/stable Gemini image model id (vs `-preview`); OpenAI gpt-image org-verification note for other deployments.
- video/music get the same feature-aware-gate + catalog treatment later.
