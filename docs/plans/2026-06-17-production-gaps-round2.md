# On-device Gemma — production gaps (round 2, from Brandon's device feedback 2026-06-17)

> Follow-up to the production-hardening plan (W0–W7 done, SNAP-20260617-7109).
> Brandon device-tested + found 4 gaps. Branch `feat/local-gemma-impl`. Subagent-driven.
> NOTE: Brandon is away (at work) — execute via code + unit tests + build; DEVICE
> VALIDATION of all of these is PENDING a watched session. Some items also need the
> backend `/local/*` routes DEPLOYED to the backend the app pairs with (see R2-2).

## What Brandon observed (Fold 6, 2026-06-17)
1. **Cloud tools not used** — asked the on-device model to "roll a six-sided dice using your tools"; it tried to **search the web** / offered to simulate, never calling `roll_dice`. He suspects "the ToolVault isn't available to the model — it's not seeing the tools."
2. **Model manager UI not working** — settings shows **"No on-device models available"** (the installed E4B isn't listed though the file IS on disk) + **"Couldn't load model catalog: HTTP 404"**. He wants a REAL manager: download-if-missing + progress + UPDATE the model (esp. to what the manufacturer/HF provides) + manage completely. "The model picker isn't actually there and working."
3. **Accessibility icon still prominent** — "I still see the accessibility icon, which we said we're not using anymore." (Intents are app-context now; a11y is only for the gesture layer.)
4. **Pin the model to a foreground/background service** — "the model still takes a really long time to actually start up." W1 warm-on-open isn't enough; pin it to a foreground service so it loads once + stays resident (also for the model-as-a-tool path).

## Diagnoses (from the code)
- **#1 cloud:** `toolBridgeOrBuild()` returns non-null whenever `api != null`, and `cloudNativeTools` is gated on `bridge != null` — so on the worktree backend the cloud tools WERE advertised. The miss is the **E4B choosing the `web_search` INTENT** (opens a browser) instead of the `search_cloud_tools` meta-tool — a steering/naming confusion + small-model selection limiter. NOT a missing-tools infra bug.
- **#2 catalog 404:** `/local/models/catalog` + `/local/tools/*` are **worktree-only** (NOT on main `:9091`). On Brandon's production backend they 404. The catalog/download manager therefore CANNOT work until the backend has `/local/*` (merge/deploy). **#2 installed-empty:** `LocalModelSection` renders `state.rows`; `rows = modelRowsFrom(catalog, installed, …)`. On a catalog 404 `refresh()`'s catch loads `installed = runCatching{ installedModels() }.getOrDefault(emptyList())`. The screenshot's "No on-device models available" ⇒ `installedModels()` returned/threw-to EMPTY at runtime despite the file+sidecar being present — almost certainly a per-sidecar parse throw emptying the whole list. Needs defensive per-sidecar parsing + logging.
- **#3 a11y CTA:** `LocalModelSection` renders a prominent "Enable Accessibility — Required for phone control" CTA (Phase 4.1). Should be de-emphasized: intents/chat need NO a11y; a11y is ONLY for the gesture layer (read_screen/tap). Reframe as optional/secondary.
- **#4 startup:** the `LiteRtEngine` singleton is ChatViewModel-owned + closed in `onCleared`; W1 warms on app-open but a cold load is ~10-75s and dies when the VM/process is reclaimed.

## Tasks
### R2-1 — Pin the model to a foreground service (Brandon's #4, highest value)
Hold the warm `LiteRtEngine` in a PROCESS-level holder kept alive by a foreground service (reuse the existing FG-service pattern — `KeepAliveService`/`OverlayService`/`BackgroundTaskService`). On start (when a local model is active/installed) the service triggers the warm `load()` once and keeps the process resident (FG notification) so the engine survives app backgrounding + is instantly ready (incl. model-as-a-tool). ADDITIVE + graceful: `ChatViewModel` uses the holder's engine if present, else falls back to its own (no destructive ownership rip-out). Unit-test the holder/lifecycle decision; DEVICE-VALIDATE startup latency later.
### R2-2 — Model manager: show installed models + robust catalog + UPDATE
(a) Fix installed-empty: make `installedModels()` parse each sidecar DEFENSIVELY (one bad sidecar must not empty the list; log the skip reason) so a present model always lists. (b) Catalog graceful: when `/local/models/catalog` 404s/unreachable, still render installed models + a clear "catalog unavailable (backend not deployed)" note — NOT "No on-device models available." (c) UPDATE: surface a model-update affordance (re-download/replace when the catalog advertises a newer version/sha). (d) Confirm the download→progress→install→select flow renders. NOTE: catalog/download REQUIRE the backend `/local/*` routes — they 404 on production `:9091`; full manager function needs the branch DEPLOYED. Flag this to Brandon.
### R2-3 — De-emphasize the accessibility CTA (Brandon's #3)
Reframe the `LocalModelSection` a11y CTA: not a prominent "required" element. Show it only as an OPTIONAL "Enable advanced screen control (tap/read screen)" affordance, clearly secondary to the (a11y-free) intent/chat capability. Don't remove the gesture capability — just stop implying a11y is required for the on-device model.
### R2-4 — Cloud-tool steering (Brandon's #1)
Reduce the `web_search`-vs-`search_cloud_tools` confusion: tighten the system-prompt addendum + the tool descriptions so the model uses `search_cloud_tools`/`call_cloud_tool` for BlackBox capabilities and `web_search` only for actual web queries. Consider renaming `search_cloud_tools`→ clearer (e.g. `find_blackbox_tool`) and/or constrained decoding nudge. Model-reliability-limited; improve steering, measure. DEVICE-VALIDATE later.

## Cross-cutting / dependencies
- DEVICE VALIDATION of all 4 is pending a watched session (Brandon at work).
- The model manager's catalog/download/update + the cloud tools require the backend `/local/*` routes on the backend the app pairs with — they 404 on production `:9091`. The clean path: **deploy/merge** the branch so `:9091` serves `/local/*`. Until then these only work against the worktree `:9099`.
