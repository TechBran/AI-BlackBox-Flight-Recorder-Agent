# Android Device-Control Wire Contract (v1)

**Milestone M0** of the frontier-driven Android/tablet/XR device-control rewire
(plan: `docs/plans/2026-06-30-android-control-frontier-driven-plan.md`; research:
`docs/plans/2026-06-30-android-control-layer-research.md`).

These JSON Schema (2020-12) documents are the **single wire contract** both sides
build against:

- the **device** (phone / tablet / foldable / XR headset) is the *hands* — it
  emits `observation`s and executes `action`s through the already-built Android
  actuation layer (`overlay/{UiTreeReader,Actuators,IntentActuator,AndroidPhoneController}.kt`);
- the **cloud frontier ReAct loop** is the *brain* — it consumes `observation`s
  and emits `action`s over the existing Tailscale **8765** channel.

M0 is **contract only**: these schemas + one accessibility-config edit. No cloud
loop (M2), no phone endpoint wiring (M1), no Kotlin data classes.

## Files

| Schema | Direction | Purpose |
|---|---|---|
| `ui_node.json` | — | One actionable accessibility node. Mirrors the on-device `@Serializable UiNode` **exactly** (JSON keys = `@SerialName`s). |
| `device_capability.json` | device→brain (inside observation) | `{formFactor, hasScreenshot, supportsCoordinateGesture, displayId}` — what the device can do; the loop degrades gracefully. |
| `observation.json` | device→brain | `{msg:"observation", ui_tree[], device_capability, screenshot?, timestamp}`. Screen snapshot. |
| `action.json` | brain→device | `{msg:"action", …}` — discriminated union (`type`) of the **nine** actuations. |
| `action_result.json` | device→brain | `{msg:"action_result", success, detail?, error?, observation?}`. |

## Two discriminators: `msg` (message kind) vs `type` (action variant)

Each wire message carries a **required** `msg` const identifying the MESSAGE KIND —
`"observation"` / `"action"` / `"action_result"`. This is a **separate key** from the
action-variant discriminator `type` (`element_click` / `element_set_text` / … inside an
`action`), so the two never collide: an action frame is `{msg:"action", type:"element_click",
…}`. The Kotlin `WireMessageType` constants + `ActionEnvelope.msg` / `ActionResultEnvelope.msg`
mirror the `msg` const; the envelope structures conform to `action.json` / `action_result.json`.

## The loop

```
device ──observation──▶ frontier loop ──action──▶ device ──action_result(+observation?)──▶ …
```

Tree-first cadence: `ui_tree` is always sent; `screenshot` rides along only when
`device_capability.hasScreenshot` is true **and** the step needs vision (tree-blind
surface or model-requested). XR (`hasScreenshot=false`) runs tree+intent only.

## The password-redaction INVARIANT (load-bearing)

The single security guarantee this contract enforces on the wire:

> An `is_password: true` node's `text` **MUST** be the redaction placeholder
> `·····` (five `U+00B7` MIDDLE DOT), **never** the raw credential.

On-device this is enforced by `UiTreeReader.nodeText` (the raw text is dropped on
the floor and never materialized into a `UiNode`, log line, or exception). On the
wire it is enforced structurally: `observation.json` applies an `if/then` to every
`ui_tree` element — `is_password==true ⇒ text == "·····"` — so a password node
carrying real text is a hard **schema violation**. The redemption of a password
value only ever happens via the **credential handoff** on `element_set_text` (the
user types it; the model stays blind in both directions).

## Action → actuator-method mapping

Every `action` variant routes to a **real** entry point in the existing actuation
layer. Dispatch enters via `AndroidPhoneController.dispatch(name, args)`.

| `action.type` | Key fields | Dispatch name | Actuator method (file:line) | Mechanism |
|---|---|---|---|---|
| `element_click` | `resource_id` \| `node_id` | `tap` | `Actuators.tap(NodeRef)` — `Actuators.kt:98` (idx overload `:145`) | `ACTION_CLICK` on node / nearest clickable ancestor (`:120`), else center gesture |
| `element_set_text` | (`resource_id`\|`node_id`) + `text` | `type` | `Actuators.type(NodeRef, text)` — `Actuators.kt:194` (idx overload `:242`) | `ACTION_SET_TEXT` (`:227`); **credential-handoff** on password fields |
| `coordinate_tap` | `x`, `y` | `coordinate_tap` **(new in M1)** | `Actuators.dispatchTap` — `Actuators.kt:382` **(private today)** | `dispatchGesture` single tap |
| `coordinate_swipe` | `x`, `y`, `x2`, `y2`, `duration_ms?` | `swipe`/`coordinate_swipe` **(new coord branch in M1)** | `Actuators.swipe(startX,startY,endX,endY)` — `Actuators.kt:278` (public) → `dispatchSwipe` `:390` | `dispatchGesture` swipe stroke |
| `global_action` | `action` ∈ back/home/**recents** | `back` / `home` / `recents` **(new in M1)** | `Actuators.back()` `:333` / `home()` `:336` → `globalActionFor` `:561` | `performGlobalAction(GLOBAL_ACTION_*)` |
| `intent` | `name` ∈ `INTENT_ACTIONS`, `params` | *(the intent name)* | `IntentActuator.perform(name, args)` — `IntentActuator.kt:103` | Stock OS `Intent` via Application context (`send_*` autonomy-gated) |
| `open_app` | `package` (required) | `open_app` | `Actuators.openApp(packageName)` — `Actuators.kt:317` | Launch `Intent` (`getLaunchIntentForPackage` + `FLAG_ACTIVITY_NEW_TASK`); uninstalled pkg → `success=false`. **Wired today** (dispatch reads `package`/`package_name`). A `PHONE_ACTUATOR`, NOT an intent → deliberately absent from `intent.name`. |
| `scroll` | `direction` ∈ up/down/left/right | `scroll` | `Actuators.scroll(direction)` — `Actuators.kt:297` → `swipeCoords` `:535` | Centered-swipe `dispatchGesture`. **Direction-based (not coordinate) → XR-portable.** Unknown direction → `success=false`. **Wired today.** |
| `press_key` | `key` ∈ enter/back/home/recents/tab/delete **(new in M2 / v1.2)** | `press_key` | `Actuators.pressKey(key)` → `pressKeyPlan` | `enter` → `ACTION_IME_ENTER` on the focused editable node (submit; API 30+, graceful otherwise); `back`/`home`/`recents` → `performGlobalAction` (reuses `globalActionFor`); `tab`/`delete` → best-effort `success=false` (M2). **Coordinate-free → XR-portable (never coordinate-gated).** Parser validates `key` against the enum → `invalid_argument` on anything else. |

`AndroidPhoneController.dispatch` (`AndroidPhoneController.kt:66`) routes: the eight
gesture names in `ResidentTools.PHONE_ACTUATORS` (`read_screen`, `tap`, `type`,
`swipe`, `scroll`, `open_app`, `back`, `home`) to `Actuators`; and any `name in
ResidentTools.INTENT_ACTIONS` to `IntentActuator`.

### `intent.name` — the 26 `INTENT_ACTIONS` and their `params`

From `ResidentTools.INTENT_ACTIONS` (`ResidentTools.kt:123`) / `IntentActuator.perform`:

| `name` | Required `params` | Optional `params` | Gate |
|---|---|---|---|
| `flashlight_on` / `flashlight_off` | — | — | — (drives CameraManager torch, not an intent) |
| `create_contact` | — | `first_name`, `last_name`, `phone_number`, `email` | — |
| `send_email` | `to` | `subject`, `body` | **high-consequence** |
| `show_map` | `query` | — | — |
| `open_wifi_settings` | — | — | — |
| `create_calendar_event` | `datetime` | `title` | — |
| `open_url` | `uri` (http/https OR app deep-link scheme; `file`/`content`/`intent`/`javascript`/`data` rejected) | `url` (legacy alias) | — |
| `dial` | `number` | — | — |
| `send_sms` | `number` | `body` | **high-consequence** |
| `set_alarm` | `hour`, `minutes` | `label` | — |
| `set_timer` | `seconds` | `label` | — |
| `share_text` | `text` | — | — |
| `open_settings_panel` | — | `which` (any `Settings.ACTION_*` panel) | — (LEGACY — kept for back-compat; superseded by `open_settings`) |
| `take_photo` | — | — | — |
| `capture_video` | — | — | — |
| `show_alarms` | — | — | — |
| `view_calendar` | — | `datetime` | — |
| `pick_contact` | — | — | — |
| `view_contacts` | — | — | — |
| `pick_file` | — | `mime` | — |
| `create_document` | — | `mime`, `filename` | — |
| `navigate` | `destination` | — | — |
| `play_media` | `query` | — | — |
| `open_settings` | `panel` (ANY `Settings.ACTION_*` panel by key; unknown → valid list) | — | — (SUPERSEDES `open_settings_panel`) |
| `send_intent` | `action` (intent action string) | `uri`, `mime`, `package`, `extras` | **high-consequence** (guarded generic escape-hatch: dangerous-action denylist + unsafe-URI reject + confirm-gate) |

> **Scope note (M1.5 / decision 9 — LANDED):** `open_app(package)` is its own `open_app`
> action variant (a `PHONE_ACTUATOR` → `Actuators.openApp`, NOT an intent — correctly absent
> from `intent.name`). Decision-9's intent-layer expansion has now landed **additively**: the
> full common-intents catalog, `open_settings(panel)` (which supersedes the legacy
> `open_settings_panel`, kept for back-compat), and the guarded generic `send_intent(...)`.
> This grew `intent.name` from 15 to **26** — an additive minor bump (`schema_version` 1.0 →
> 1.1; see Versioning). **Wire-safety (I1):** `RemoteActionChannel.parseAction` rejects any
> `intent.name` not in `INTENT_ACTIONS` (`unknown_action`), so a gesture / global / coordinate
> dispatch name can never be smuggled through the intent branch.

## The on-wire transport envelope (M2)

`action.json` / `observation.json` / `action_result.json` describe the **message
payloads**. On the wire the M2 loop wraps an `action` payload in a tiny **transport
envelope**: `POST /action`'s JSON body is a full `action.json` frame **PLUS two sibling
framing keys** —

```jsonc
{
  "msg": "action", "type": "element_set_text", "resource_id": "…", "text": "…",  // action.json payload
  "task_id": "3f9c…",   // transport: the correlation id the loop minted (both channels share it)
  "operator": "Brandon" // transport: the operator scope (fail-closed in authorize())
}
```

- `task_id` correlates the `/action`↓ and `/stream`↑ halves of one session; `operator`
  is re-checked in `authorize()` (a foreign operator's hub can't actuate the device).
- **Why it isn't a schema violation:** each `action.json` variant is
  `additionalProperties: false`, so `task_id`/`operator` are **not** valid *inside* the
  action payload. They are the **framing around it**. A strict validator MUST therefore
  validate the action variant **after stripping** `task_id` + `operator` (the device side
  does exactly this — `ACTION_JSON` is `ignoreUnknownKeys=true` and `ActionEnvelope` reads
  only `msg`/`task_id`/`operator`, handing the untouched body to `parseAction`, which keys
  on the variant `type`). This split is intentional: the payload stays a clean, portable
  contract while the transport carries only correlation + scope.
- The response (`action_result.json`) carries **no** envelope — it is the bare payload
  (the loop already knows the `task_id` it POSTed).

## Gaps flagged for M1 (where this contract runs ahead of the current code)

The schema defines the target contract; three variants have **no wired entry point
yet** and must be added when M1 lands the action channel:

1. **`coordinate_tap`** — `Actuators.dispatchTap(svc,x,y)` is **private** (only the
   internal fallback inside `tap(NodeRef)`); there is no public coordinate-tap
   method and no `coordinate_tap` dispatch branch. M1 must expose one.
2. **`coordinate_swipe`** — the public `Actuators.swipe(x,y,x2,y2)` **exists**, but
   `AndroidPhoneController.dispatch("swipe")` today reads only a `direction` string,
   not coordinates. M1 must add a coordinate branch.
3. **`global_action: recents`** — `globalActionFor` maps only `back`/`home`
   (`Actuators.kt:561`); `recents` needs a `GLOBAL_ACTION_RECENTS` mapping + an
   `AndroidPhoneController` branch.

## Still-open M1/M2 decisions (flagged in the M0 review)

Two design questions the M0 scaffold deliberately leaves for M1/M2 to resolve:

1. **`/action` acks `not_wired` even with no handler.** Today `POST /action` always
   returns a well-formed `action_result` with `error:"not_wired"` — it does NOT consult
   the `RemoteTaskHandlerHolder`. When M1 wires real actuator dispatch, `/action` should
   **gate on a real (non-no-op) handler** being present, so a model-less / handler-less
   device reports an honest "no handler" state rather than a blanket scaffold ack.
2. **Two coexisting observation-delivery paths.** An observation can arrive either
   **embedded** in `action_result.observation` (piggy-backed on a result) OR **streamed**
   over `GET /stream/{id}` (SSE). M1/M2 must pick **ONE** as the canonical path to avoid a
   double-observe race (the loop acting twice on the same screen state). The other becomes
   an explicit opt-in (e.g. embed only when `/stream` is not open).

## Versioning

- **Path-encoded major:** every `$id` carries `/v1/`. A breaking change bumps to
  `/v2/` (a parallel directory) and the `observation`/`action` `schema_version`
  major.
- **`schema_version`** (optional string on `observation`, `const "1.2"`) lets the
  device and loop negotiate; additive minor changes (new `intent.name`, a new
  action variant, a new optional field, a new `error` enum value) bump the minor and
  stay compatible. **1.1** grew `intent.name` from 15 → 26 (decision-9 comprehensive
  intents + `open_settings` + the guarded `send_intent`). **1.2** added the `press_key`
  action variant (enter/back/home/recents/tab/delete) — `enter` submits the focused
  field via `ACTION_IME_ENTER`, enabling a 'type → submit' flow with no coordinate.
  Both are additive + back-compatible.
- **Additive by default:** new fields land as optional; enums grow, never shrink,
  within a major. A field rename or a required-field addition is breaking → new
  major.
- A future **`provider`** tag on `action` (M7, per-backend coordinate adapter) is
  an example of a planned additive field — it slots in without a major bump.
