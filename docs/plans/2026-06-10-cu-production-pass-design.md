# Computer Use Production Pass — Design

**Date:** 2026-06-10
**Status:** Approved (brainstorming validated section-by-section with Brandon)
**Branch:** `feat/cu-production-pass`

## Goal

Bring the Computer Use (CU) agent to production quality so it works on any
customer's BlackBox appliance, with model selectors in both frontends
(Portal web + Android Kotlin MVP) auto-populated from the live Anthropic,
Google, and OpenAI catalogs, and CU UI surfaces that are screen-aware and
theme-aligned on both frontends.

**Primary target:** the appliance's own Linux desktop (Wayland or X11, any
resolution, any distro packaging). Remote VNC computers and ADB Android
devices remain supported but secondary.

## Audit Findings (2026-06-10)

### Architecture today

| Stack | Entry points | State |
|---|---|---|
| Anthropic "Sovereign" | chat provider `computer-use` → `stream_computer_use()` (`chat_routes.py:3886`) | Production-grade: persistent `CUSession`, prompt queue, reconnect, E-stop, bash + text-editor + ToolVault tools, fossil context, device switching |
| Anthropic legacy | `/browser/run` → `BrowserSession` (`browser/agent_loop.py`) | Older duplicate loop; used by `use_computer` ToolVault tool + scheduler |
| Gemini CU | `/gemini-cu/run`, `/gemini-cu/stream`, chat path `stream_gemini_computer_use()` | Works but second-class (see defects) |
| OpenAI CUA | `openai_cu/` | Placeholder stub |

Execution layer (`Orchestrator/browser/`): `ActionExecutor` (Wayland-aware,
routes ydotool vs xdotool, scale-corrected), screenshot chain (XDG Portal →
scrot → CDP), VNC via `vncdotool`, Android targets via `adb/`.

Frontends: Portal `cu-drawer.js` (session/E-stop/device picker) +
`cu-interact.js` (interactive viewer); Android `CuScreen.kt` (live viewer +
provider/model row) + `ChatViewModel` streaming with provider `computer-use`.

### Defects driving this pass

1. **CU model lists hardcoded in 3 drifted places**: `Portal/modules/state-management.js:434`
   (4 entries), Android `Constants.kt MODEL_CONFIG["computer-use"]` (5 entries,
   different), backend defaults. The dynamic `/models/{provider}` infra
   (admin_routes.py, T2 2026-05-18) exists but excludes CU.
2. **Model rot**: `gemini_cu/config.py` references retired `gemini-3-pro-preview`
   (404s). Backend CU default `claude-opus-4-6` (`browser/config.py:254` + two
   `chat_routes.py` sites) disagrees with Portal's `claude-opus-4-7 (Sovereign)`
   default.
3. **Gemini CU bypasses the portability layer**: calls `_run_xdotool` directly
   (X11-only; silent no-op on native Wayland windows), uses stale import-time
   `NATIVE_WIDTH/NATIVE_HEIGHT` (E18 dynamic-resolution fix never propagated),
   system prompt hardcodes "display :0, 1920x1080".
4. **Sync-in-async**: Gemini loop calls `client.models.generate_content`
   (blocking) inside the event loop.
5. `NATIVE_MODE = True` and the Chrome path are code constants, not config.
6. **No dependency preflight**: xdotool/ydotool daemon/scrot/Chrome assumed
   present; failures on a customer box are silent.
7. `_snapshot_cu_result` posts to `/chat` instead of `/chat/save` (~400× cost).
8. Default system prompts demand "call `get_current_time` FIRST" but the
   standalone loops don't expose that tool.
9. `operator: str = "Brandon"` hardcoded defaults in stream signatures.
10. **Zero CU tests.**
11. **UI not screen-aware / theme-drifted**: `CuScreen.kt` defines a private
    color palette (lines 117–127), locks the live view to 16:9, uses magic
    paddings (160dp/140dp), no WindowSizeClass/orientation handling. Portal CU
    CSS is tokenized but the drawer/interactive modal lack a responsive pass.

## Design

### 1. CU model catalog

New `GET /models/computer-use` in `admin_routes.py`, reusing the existing
fetcher/cache layer (`models_cache.py`). Calls the three live fetchers and
applies CU-capability filter rules defined **as data in `Orchestrator/config.py`**:

- Anthropic: id matches `claude-(opus|sonnet)-4.*` and newer 4+-series
  families (models accepting `computer_20251124` / beta `computer-use-2025-11-24`).
- Google: id contains `computer-use`.
- OpenAI: id matches `computer-use-preview*` (Responses API CUA).

Response keeps the locked `/models/{provider}` envelope plus a per-model
`backend` field (`anthropic|google|openai`) so frontends group the dropdown
and the router dispatches without `"gemini" in model` string-sniffing.
`default_id` comes from new `CU_MODEL_DEFAULT` config, replacing all four
scattered defaults. Retired Gemini constants deleted. Server-side static
fallback list for offline/no-key boxes, same as chat providers.

### 2. Unified CU engine

One session model, three backend drivers. `CUSession`
(`browser/session_manager.py`) — already owning event queue, prompt queue,
E-stop, reconnect, history — becomes the single session object. Driver
contract: `run(session, prompt, model)` yields events into
`session.event_queue`.

- **AnthropicDriver**: today's `_cu_agent_loop`, extracted from
  `chat_routes.py` into the `browser/` package.
- **GeminiDriver**: rebuilt to parity — actions routed through
  `ActionExecutor` with `coord_space="gemini-999"` (normalized 0–999 →
  live `get_scale_factors()`; inherits Wayland/ydotool + dynamic resolution),
  async client (`client.aio.models.generate_content`), snapshots via
  `/chat/save`.
- **OpenAIDriver**: new — Responses API `computer_use_preview` tool,
  `previous_response_id` continuity, reasoning items passed back, actions
  mapped onto the same `ActionExecutor`.

`stream_computer_use()` becomes a thin backend-agnostic shell: resolve driver
from catalog `backend`, shared queue/reconnect/E-stop, launch driver task.

Callers refit: `/browser/run`, the `use_computer` ToolVault tool, and the
scheduler create a session headlessly and drain its queue.
`browser/agent_loop.py` (`BrowserSession`) is deleted. The unsatisfiable
`get_current_time` instruction is replaced by injecting the current timestamp
into the system prompt.

### 3. Portability, preflight, config

`GET /cu/preflight` runs a readiness checklist, each check returning
`ok|warn|fail` + a remediation string:

- Display server (Wayland vs X11 socket probe), display number,
  XAUTHORITY/DBUS resolvable.
- Input backend: xdotool present; on Wayland, ydotool binary **and daemon
  socket** alive.
- Screenshot path verified by a real tiny capture (Portal → scrot → CDP).
- Live resolution + computed scale factors.
- Per-backend API keys present; Chrome binary found.
- Secondary: vncdotool, adb availability (reported, never blocking).

Portal CU drawer and Android `CuScreen` call it when CU is selected and
render a banner with remediation on `fail`. `install.sh` gains the missing
packages.

Config over constants (`config.ini` + env overrides): `CU_NATIVE_MODE`,
`CU_CHROME_PATH` (with auto-probe fallback), `CU_MODEL_DEFAULT`,
`CU_MAX_ITERATIONS`, `CU_SESSION_TIMEOUT`. Hardcoded `operator="Brandon"`
defaults become required parameters resolved by callers.

### 4. Frontends — catalog + behavior

**Portal:** `computer-use` joins the hydratable providers in
`state-management.js` (sessionStorage 5-min cache); dropdown grouped by
backend via `<optgroup>`; hardcoded list shrinks to a 3-entry offline
fallback. `cu-drawer.js` replaces `_isGeminiModel()` with a model→backend
catalog lookup; device filtering becomes capability-driven (ADB only for
backends supporting the Android environment). Preflight banner.
`cu-interact.js` keeps its 1280×720 canonical interactive space but reads
resolution from `/browser/status`.

**Android:** `Constants.MODEL_CONFIG["computer-use"]` becomes offline
fallback; `ChatViewModel.fetchLiveModels()` learns the `computer-use`
provider (same pattern as the existing xAI hydration). `CuScreen.kt`
`cuModelsForBackend()` partitions by the API `backend` field; preflight
banner above the live view for the appliance device.

Both frontends render from the same server catalog — drift becomes
structurally impossible.

### 5. CU UI/UX — screen awareness + theme alignment

**Android (`CuScreen.kt`):**
- Theme alignment: retire the private `CuPurple`/`CuGreen`/`CuRed` palette;
  consume the Portal-matching theme tokens the rest of the app uses. CU keeps
  its purple accent identity, defined once in the app theme, not privately in
  the file.
- Screen awareness: adopt `WindowSizeClass` — compact-portrait keeps the
  stacked layout; landscape/expanded (tablets, foldables, XR) gets a
  side-by-side layout: live view left, chat/controls right. The live view
  drops the hardcoded 16:9 ratio and sizes from the actual screenshot
  dimensions reported by `/browser/status` (non-16:9 appliance displays
  currently letterbox wrong). Magic paddings (160dp/140dp) replaced with
  `WindowInsets`-derived values so the typing bar clears the IME and
  navigation bars on any device.

**Portal web:**
- Responsive pass on the CU drawer and `cu-interact` modal: on narrow
  viewports the interactive viewer goes true fullscreen with the typing bar
  pinned above the soft keyboard (`visualViewport`-aware); drawer rows wrap
  instead of overflowing the operator bubble.
- The interactive viewer canvas scales from `/browser/status` resolution
  (ties into Section 4) so non-16:9 customer displays render without
  distortion.

Both follow the existing design-token system (`Portal/styles/_variables.css`
/ app theme) — no new colors invented, per the locked design-system rules.

### 6. Testing & rollout

**Tests (first CU coverage in the repo):**
- Unit: catalog filter rules vs mocked vendor responses including
  future-shaped IDs; coordinate round-trips at 1080p/4K/ultrawide for both
  1280×720 and 0–999 spaces; `_map_gemini_keys`; per-driver action mapping
  with subprocess mocked (assert exact argv).
- Contract: `/models/computer-use` envelope, `/cu/preflight` shape, session
  semantics with mocked drivers (queue-while-running, reconnect replay,
  E-stop).
- Golden parity: capture `/browser/run` + `use_computer` behavior before
  consolidation, assert equivalence after (ToolVault-v2 migration lesson).

**Rollout:** prod runs live from the working tree, so each phase lands with
the tree restartable and tests green:

1. Catalog endpoint + config migration (additive)
2. Preflight endpoint + installer deps (additive)
3. Gemini driver parity rebuild
4. Loop consolidation; delete `browser/agent_loop.py`
5. OpenAI CUA driver
6. Portal frontend (catalog hydration + responsive/UI pass)
7. Android frontend (catalog hydration + WindowSizeClass/theme pass)

Live verification on the appliance (real Wayland run) before merge to main.

## Out of scope

- Windows/Mac CU hosts, customer-installed non-appliance machines.
- Remote-device (VNC/ADB) feature investment beyond keeping current behavior
  working and reporting availability in preflight.
