# Multi-Provider Image Generation — Design

**Date:** 2026-06-20
**Status:** Validated (brainstorm complete, approved by Brandon). Next: implementation plan.
**Author:** Claude (Opus 4.8) + Brandon
**Precedent:** mirrors `docs/plans/2026-06-20-multi-provider-web-search-design.md` (same per-provider pattern), adapted for the ASYNC image pipeline + a provider-aware generation UI on two surfaces.

## Goal

Replace the single `generate_image` tool with per-provider image tools — `gemini_image`,
`openai_image`, `grok_image` — auto-injected when (key present AND provider enabled), with an
onboarding-selected default (a prompt hint), so a model can fire several providers in parallel
for the same prompt (cross-provider image comparison). PLUS a provider-aware generation UI on
BOTH the Portal and Android, driven by a backend catalog so the two surfaces stay aligned.

## Feasibility — spike (2026-06-20, `diagnostics/imagegen_spike.py`, existing keys)

| Provider | Result | Output shape |
|---|---|---|
| OpenAI `gpt-image-1` (`POST /v1/images/generations`) | OK ~11s | `data[].b64_json` (~1.25MB base64) |
| xAI `grok-imagine-image-quality` (`POST /v1/images/generations`) | OK ~4.7s | `data[].url` — TEMPORARY (`imgen.x.ai/xai-tmp-imgen`), must fetch+persist immediately |
| Gemini Nano Banana (current `/generate/image` → worker) | OK ~18s | writes to `/ui/uploads` (`artifact`/`all_urls`) |

**Key finding:** OpenAI + xAI share the `/v1/images/generations` shape (base-URL + model swap) but
DIFFER in output (b64 vs temp URL) — one adapter family with an output-normalization step
(decode-b64 OR download-url → raw bytes). Gemini is its own path (existing `call_imagen`,
inline base64). All three feasible with the existing OPENAI/XAI/GOOGLE keys.

## Scope

**In (v1):** Gemini (Nano Banana), OpenAI (gpt-image, quality-first tier), xAI (Grok image).
**Out:** Google Imagen as a separate 4th provider (Nano Banana covers Google); video/music (same
pattern, later — the feature-aware gate below is built to extend to them). **No keyless floor** —
unlike web search's DuckDuckGo, there's no free image provider, so "no image key" = no image tools.

## Architecture

### 1. The three tools (replace `generate_image`)
`gemini_image`, `openai_image`, `grok_image` — provider-first naming, ASYNC (return `task_id`),
Tier-2, same 7 groups as `generate_image`. Each creates an `IMAGE_GENERATION` task tagged with its
`provider`. Keep `prompt` + provider-applicable params (Gemini: `reference_images`/`aspectRatio`/
`resolution`/`numberOfImages`; OpenAI: `size`/`quality`/`background`/`n`; xAI: `n`/aspect). Delete
`ToolVault/tools/generate_image/`.

### 2. Worker routing (the core new logic)
`TaskType.IMAGE_GENERATION` tasks gain a `provider` field (in `result_data`/options). The image
worker routes to a per-provider adapter that returns **bytes**, then writes them to the predicted
`/ui/uploads/{slug}_{task_id}_{i}.png` path so the placeholder/predicted-URL mechanism is uniform:
- OpenAI adapter: `POST {openai}/v1/images/generations` → `b64_json` → `base64.b64decode` → bytes.
- xAI adapter: `POST {xai}/v1/images/generations` → `url` → download immediately (temp URL) → bytes.
  (One OpenAI-images family serves both — base-URL + model + output-normalizer.)
- Gemini adapter: existing `call_imagen` path (unchanged).
The worker default-routes legacy/untagged tasks to the configured default provider (back-compat
for any in-flight `/generate/image` callers during migration).

### 3. Availability gate — generalized to be feature-aware
Extend the Task-2 web-search gate (`Orchestrator/toolvault/availability.py`) so `x-availability`
carries a `feature`: `{"feature": "image", "provider": "openai", "requires_env": ["OPENAI_API_KEY"]}`.
`availability.py` resolves the enabled set + default PER FEATURE (`IMAGE_ENABLED`/`IMAGE_DEFAULT`
vs `WEB_SEARCH_ENABLED`/`WEB_SEARCH_DEFAULT`), via a small feature registry
(`{feature: {pref_enabled, pref_default, PROVIDER_ENV, PROVIDER_TOOL}}`). Existing web-search entries
get `feature: "web_search"` (or default to it for back-compat). STILL stdlib-only / lean-venv-safe.
The injector/get_tools_by_group/get_mcp_tools filters are unchanged (they call `is_available`).
This future-proofs the same gate for video/music.

### 4. Image-provider param catalog (the SoT) — NEW endpoint
`GET /image/catalog` returns, for each ENABLED image provider, its label + param schema:
```
[{ "provider": "openai", "label": "OpenAI (gpt-image)", "default": true,
   "params": [{"name":"size","type":"enum","options":["1024x1024","1536x1024","1024x1536","auto"],"default":"1024x1024"},
              {"name":"quality","type":"enum","options":["low","medium","high"],"default":"high"}, ...] },
 { "provider": "gemini", "label": "Gemini Nano Banana", "params": [{"name":"aspectRatio",...},
   {"name":"resolution",...},{"name":"numberOfImages",...},{"name":"reference_images",...}] },
 { "provider": "grok", "label": "Grok image", "params": [{"name":"n",...},{"name":"aspectRatio",...}] }]
```
Enabled set + default come from `availability` (DRY). Param schemas live in one backend module
(the per-provider param spec) so adding a provider/param is a backend-only change. Quality-first
defaults per `feedback-quality-first-defaults`.

### 5. Provider-aware generation UI — Portal + Android (both hydrate the catalog)
- **Portal** (`Portal/modules/generation-modals.js` image modal): replace the hardcoded single-
  provider params with a **provider dropdown** (enabled providers from `/image/catalog`, default
  preselected) + param controls rendered DYNAMICALLY from the selected provider's schema; changing
  the dropdown re-renders the params. Submit posts the selected provider + its params.
- **Android** (Kotlin image-gen screen): same — dropdown from `/image/catalog` + dynamic param
  controls per provider. (3-surfaces rule: WebView wrappers inherit the Portal.)
Both consume ONE catalog → aligned by construction (Brandon's requirement: "any settings/parameter
changes have to change when we change the dropdown, on both"). The generation request carries the
chosen `provider`, which flows to the right per-provider tool / `/generate/image` task.

### 6. Onboarding image step
New wizard step `image` in `ALL_STEPS` (mirror the web-search step): provider checkboxes (those
with keys) + a preferred-default radio; writes `IMAGE_ENABLED` + `IMAGE_DEFAULT` (live-read by
`availability`, no restart). No keyless option (no free provider). Portal wizard only.

### 7. Default-provider hint
When ≥1 image tool is injected, `build_tool_instructions` appends a hint built from the image
enabled-set + `IMAGE_DEFAULT` (reuse the Task-7 `default_web_search_hint` mechanism, generalized
to features): "For image generation, prefer `gemini_image`; other providers available to compare."

### 8. Dispatch
The ~6 hand-written `image_task` sites (chat_routes + voice routes) currently key on
`tool_name == "generate_image"`. Update them to recognize the three new tool names and pass the
`provider` into task creation. The placeholder/predicted-URL/`image_task` event payload is
provider-agnostic (task_id + predicted URL), so the Portal/Android animations need little/no change.

### 9. MCP
The three image tools ride the existing async task system (`get_task_status`); no special MCP
handler needed beyond listing them (they're `mcp`-group, availability-filtered). Confirm a model
can poll the task to completion via MCP.

## Testing
- Spike done (`diagnostics/imagegen_spike.py`).
- Unit: worker per-provider routing (mock the adapter HTTP; assert bytes written to the predicted
  path); adapter output-normalization (b64-decode vs url-download); feature-aware gate (image ×
  key×enabled matrix; web-search entries unaffected); `/image/catalog` shape + enabled-filtering;
  onboarding save/current-config; default hint; dispatch routes new tool names with provider.
- Live per-provider smoke (generate 1 image each through the real pipeline; confirm files land +
  `image_task` events fire). Full suite incl. `Orchestrator/tools/`. Final holistic review + device
  validation (Brandon) GATES the push.

## Decisions locked
1. Per-provider tools REPLACE `generate_image` (consistent with web search).
2. Worker routes by a `provider` field; per-provider adapters → bytes → predicted `/ui/uploads` path.
3. Availability gate GENERALIZED to feature-aware (image + web_search; extensible to video/music).
4. Image-provider param CATALOG (`GET /image/catalog`) is the SoT; Portal + Android both hydrate it
   (provider dropdown + dynamic params) — alignment by construction.
5. Explicit enable + default (`IMAGE_ENABLED`/`IMAGE_DEFAULT`); no keyless floor.
6. Provider-first names; quality-first model tiers.

## Follow-ups / risks
- xAI temp-URL must be fetched+persisted in the worker (don't surface the ephemeral URL).
- Verify the current Gemini image model isn't on a deprecation path (memory: gemini-3-pro-*preview*
  text model was retired; the *image* variant `gemini-3-pro-image-preview` works per spike — confirm
  in plan, prefer a GA/stable image model id where one exists per `feedback-brandon-ga-vs-preview`).
- OpenAI gpt-image org-verification (worked on our key in the spike) — note for other deployments.
- Reference-image (image-to-image) support varies by provider (Gemini yes; OpenAI edits endpoint;
  xAI tbd) — catalog should reflect per-provider capability; v1 may gate image-to-image to providers
  that support it.
