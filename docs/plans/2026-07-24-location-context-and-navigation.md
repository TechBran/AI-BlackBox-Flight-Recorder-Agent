# Location Context + Navigation Push Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task.

**Goal:** Ship location as a ride-along on the user prompt (used live by the model,
recorded once per snapshot), and give the box the ability to push a navigation
destination to a phone — including from a calendar event on a schedule.

**Architecture:** Location is **metadata on the turn**, never its own subsystem. The
phone attaches coordinates to the prompt it was already sending; the box enriches them
with a place name, appends one line to the user text, and that line reaches both the
model and the snapshot for free (`turns_threshold = 1`, so one turn *is* one snapshot).
Navigation reuses the shipped, device-proven `navigate` intent; the only new server-side
piece is a one-shot tool that POSTs an action frame to the device.

**Tech Stack:** Kotlin/Compose (Android, minSdk 26, targetSdk 36), FastAPI + httpx
(Orchestrator), ToolVault v2 modules, APScheduler (cron).

**Brandon's locked decisions (2026-07-24):**
- Location **rides along with the user prompt**. One location per snapshot. **No
  location-minted snapshots, no polling, no background service, no separate store.**
- Store **both** coordinates and reverse-geocoded place text (city + state).
- **No home suppression** — always store real coordinates.
- CLI agents in a terminal, Portal, and cron have no device location: the field is simply
  absent and behaviour is unchanged.

---

## Context — what the audit established

### The design fits an existing seam almost exactly

`StreamRequest` already carries `origin_device_id`, added in M3 for precisely this class
of reason — *"a device-control tool triggered by THIS chat defaults to targeting THIS
phone"* (`ChatMessage.kt:37-41`). A `location` field sits beside it, same file, same idea.

`turns_threshold = 1` (`config.ini:10`) is verified — one turn is one snapshot. So a line
appended to the user text is recorded by the ledger automatically; no mint-path surgery
is needed to capture it, only an optional structured gauge line.

### Three findings that shape the build

**1. Android forbids what the cron use case naively wants.** The app is `targetSdk 36`, so
all Background Activity Launch restrictions apply: **a remote command cannot launch Google
Maps while the app is in the background or the phone is locked.** A direct `/action`
intent push works only when the app is already foreground. The supported path is a
**notification with a "Navigate" action** the user taps — which is also the safer product
(a phone that hijacks itself into turn-by-turn while you are already driving is a hazard,
not a feature). Tier 1 of the nav work is therefore notification-delivered, with direct
launch as an opportunistic fast path when the app happens to be foreground.

**2. No geocoder is needed for navigation.** `google.navigation:q=<text>` accepts a
free-text address and lets Google Maps resolve it. A calendar event's `location` string
can be passed straight through. Geocoding is only needed for the *reverse* direction
(coords → "Hamilton, Ontario") on the location-context half.

**3. Two real gaps found in shipped code:**
- **`origin_device_id` is structurally dead on the non-stream `POST /chat` path** —
  `_set_origin_device_id` is called only at `chat_routes.py:7312` (GET /chat/stream) and
  `:7443` (POST /chat/stream). **Cron runs through `POST /chat`**, so a cron-initiated
  device tool has no origin and silently falls back to the operator's primary device.
  ~4 lines to fix, but it changes device-routing semantics on a shipped path.
- **`navigate` is completely ungated** (`ConfirmGate.kt:405,422-423`). A remote push can
  seize the screen mid-drive with no consent. It must be gated **by origin** — remote
  pushes confirm, the shipped on-device Gemma path (Brandon holding the phone) is
  unchanged, or the existing device-proven behaviour regresses.

### What already exists (reuse, do not rebuild)

| Capability | Where | State |
|---|---|---|
| `navigate` intent → real turn-by-turn `google.navigation:q=` | `IntentActuator.kt:347`, builder `IntentActions.kt:294` | Shipped, host-JVM tested, device-proven on XR |
| `show_map` intent → `geo:0,0?q=` preview | `IntentActuator.kt:162` | Shipped |
| 26-intent catalog | `ResidentTools.kt:123` | Shipped |
| Device action wire path `POST /action {type:"intent"}` | `RemoteActionChannel.kt:144` | Fully wired on-device |
| Tailnet auth + per-path operator scoping | `RemoteControlServer.kt:466,515` | Shipped; new paths default UNSCOPED — must be added to `authorize()` |
| Calendar `location` field | `calendar.py:87-94` returns raw Google events | Present but **undocumented to the model** |
| Cron → full `/chat` turn with the whole ToolVault surface | `scheduler/executor.py:77,130-135` | Shipped |
| Single cross-transport context assembler | `context_builder.py:195` `build_fossil_context` | Used by chat, all 3 voice routes, CLI/CU agents |
| Never-dropped top-of-context block precedent | `context_builder.py:350` `context_parts`, `_assemble()` :424-441 | RECENT MEDIA + LIVE SYSTEM HEALTH both live here |
| Ledger is gitignored | `Volumes/`, `Archive/`, `Manifest/` all IGNORED + untracked | **Verified** — real coordinates cannot reach the public repo |

---

## Milestones

### M1 — Location rides with the prompt (the core feature)

**Files:**
- Modify: `.../data/model/ChatMessage.kt:29-42` (`StreamRequest`)
- Create: `.../data/location/LocationProvider.kt`
- Modify: `.../app/src/main/AndroidManifest.xml` (COARSE + FINE, **no background**)
- Modify: `Orchestrator/routes/chat_routes.py` (request model + user-text append)
- Create: `Orchestrator/location.py` (normalize + reverse-geocode + cache)
- Modify: `Orchestrator/monitoring.py:300-304` (`LOCATION:` gauge line)
- Test: `Orchestrator/tests/test_location_context.py`

**Step 1: Write the failing backend test.**

```python
def test_location_line_appended_to_user_text():
    from Orchestrator.location import format_location_line, normalize
    loc = normalize({"lat": 43.2557, "lon": -79.8711, "accuracy_m": 12})
    line = format_location_line(loc, place="Hamilton, Ontario")
    assert line == "[location: 43.2557,-79.8711 · Hamilton, Ontario]"

def test_absent_location_is_a_noop():
    from Orchestrator.location import format_location_line
    assert format_location_line(None, place=None) == ""

def test_garbage_coordinates_rejected():
    from Orchestrator.location import normalize
    assert normalize({"lat": 91.0, "lon": 0.0}) is None      # out of range
    assert normalize({"lat": "NaN", "lon": 0.0}) is None
    assert normalize(None) is None
```

**Step 2: Run — expect failure** (`Orchestrator/location.py` does not exist).

**Step 3: Implement `Orchestrator/location.py`.** Responsibilities, in order:
`normalize()` (validate ranges, coerce floats, reject junk — this is untrusted client
input), `reverse_geocode()` (city + region; **cached on coordinates rounded to ~3
decimals** so repeat prompts from the same block cost nothing; failure returns `None` and
is non-fatal), `format_location_line()`.

**Step 4: Run — expect PASS.**

**Step 5: Wire the backend request path.** Add the optional `location` object to the
chat request model, and in the streaming context path append the formatted line to the
**user text** (not only to the fossil block) so it reaches the model *and* the
`conversation_log` item that the mint renders. Guard the whole thing in try/except: a
geocoder outage must never fail a chat turn.

**Step 6: Add the snapshot gauge line.** In `render_snapshot_body_v71`
(`monitoring.py:300-304`), append `LOCATION: <lat>,<lon> · <place>` to `gauges_lines`
when present, next to `OPERATOR:` and `MODEL:`. Absent location → no line at all.

**Step 7: Android capture.** `LocationProvider.kt` — `getLastLocation()` first (free,
instant), `getCurrentLocation()` only if null/stale, with a **hard ~1s budget**: if no fix
is ready, send the prompt **without** location. Sending must never block on GPS. Attach
to `StreamRequest`. Permissions: `ACCESS_COARSE_LOCATION` + `ACCESS_FINE_LOCATION`,
**while-in-use only — `ACCESS_BACKGROUND_LOCATION` is explicitly out of scope.**

**Step 8: Permission UX.** Ask on first use with a plain rationale ("so the assistant can
answer questions about where you are"), and degrade silently forever if denied. Add a
settings toggle to stop attaching location without revoking the OS permission.

**Step 9: Verify end-to-end**, then commit. Send a prompt from the Fold; assert the model
sees the location, the snapshot body contains both the appended line and the `LOCATION:`
gauge, and a Portal prompt in the same session produces neither.

**Guardrail:** no real coordinates in test fixtures — use the documented Google example
`37.4224,-122.0841` or obvious fakes. The ledger is gitignored; test files are not.

---

### M2 — `navigate_device` tool (foreground fast path)

**Files:** create `ToolVault/tools/navigate_device/{schema.json,executor.py}`; modify
`IntentActions.kt:294`, `IntentActuator.kt:347`, `ResidentTools.kt:572`.

1. **Harden the intent builder first** (all host-JVM testable, no device):
   `navigationUri(destination, mode=null, avoid=null)` with **strict whitelists** (`mode`
   ∈ `{d,b,l,w}`, `avoid` ⊆ `{t,h,f}`), and a lat/lng branch that emits the comma
   literally when the destination matches `^-?\d+(\.\d+)?,-?\d+(\.\d+)?$` (currently
   `URLEncoder` turns it into `%2C`, which is not the documented form).
2. **Add `setPackage("com.google.android.apps.maps")` with a `resolveActivity` preflight**
   and an overridable/omittable package param, so a device without Maps fails with a
   clear error instead of an opaque one — and a Waze user is not locked out.
3. **New ToolVault module** `navigate_device`: `{destination (required), device?, mode?,
   avoid?}`. Resolves the target via the existing `mesh.resolve_device` precedence and
   POSTs one `{"type":"intent","name":"navigate","params":{...}}` action frame. ~80 lines
   reusing existing helpers. Follow `ToolVault/tools/ADDING_A_TOOL.md`, then
   `python -m Orchestrator.toolvault.validate` and `POST /toolvault/reload`.
4. **Register in every provider's tool catch-all** — the documented reusable gotcha from
   `control_phone`: a directly-callable ToolVault tool needs a catch-all in **every**
   `stream_<provider>` loop or it silently does nothing on that provider.

**Consent (must land in the same milestone, not after):** gate `navigate` **by origin**.
A remote/cloud-originated push requires confirmation; the shipped on-device Gemma path
stays ungated so the device-proven behaviour does not regress. Thread an origin flag
`PhoneActionDispatcher → AndroidPhoneController → IntentActuator` without disturbing
YOLO/PERMISSION semantics or the existing `IntentGateTest` assertions.

---

### M3 — Notification-delivered navigation (the locked-phone path)

**This is the milestone that makes the cron use case actually work.** Direct launch is
impossible from the background on `targetSdk 36`.

- Extend the `/notify` payload and `BlackBoxNotificationManager` (`:196`) with a
  navigation notification carrying a **"Navigate" action button** whose `PendingIntent`
  fires the same `navigate` intent — a user tap is the documented BAL exemption.
- Respect the existing cross-operator rule in `notifications/bus.py:108-141`
  (`_payload_for_device` strips the body cross-operator; a nav payload must stay
  metadata-only under the same rule).
- Add a `dedup_key` so a retrying cron job cannot stack duplicate navigation prompts.
- `navigate_device` gains `delivery: auto|notify|direct`, defaulting to **auto**: direct
  when the app is foreground, notification otherwise.
- **Device-validate on a genuinely LOCKED Fold at a real scheduled time.** Do not ship
  this on reasoning alone — BAL behaviour must be observed, not assumed.

---

### M4 — Calendar → navigation, on a schedule

1. **Advertise `location`** in `ToolVault/tools/list_events/schema.json` (description,
   `returns`, `notes`). One-line-ish change; today the model is never told the field
   exists, so "find the job address" depends on it noticing an undocumented key.
   Optionally add a slim field projection to cut token weight.
2. **Fix `origin_device_id` on `POST /chat`** — add the `_set_origin_device_id` call the
   streaming paths already have (`chat_routes.py:7312`/`:7443`). This is what lets a cron
   job target a device at all. Ship with a test pinning the fallback behaviour, since it
   changes routing semantics on a shipped path.
3. **Give cron jobs a device target** — a `target_device_id` column on `cron_jobs`
   (`manager.py:304-325`), the matching `CronJobCreate`/`CronJobUpdate` fields, and the
   plumbing to set the origin contextvar for that run. Must land on all surfaces
   (backend + REST + both UIs) per the 3-surfaces rule.
4. **End-to-end flow:** a cron job at 07:30 runs a normal `/chat` turn whose prompt is
   "check my calendar for today's first job and send its address to my phone" → the model
   calls `list_events`, reads `location`, calls `navigate_device` → notification lands on
   the phone → Brandon taps **Navigate** → Maps opens with turn-by-turn.

---

## Verification

1. `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -q` green.
2. `./gradlew :app:testDebugUnitTest --offline` green (includes `IntentActionsTest`,
   `IntentGateTest`).
3. **Location end-to-end:** prompt from the Fold shows the location line in the model's
   context and both the appended line and the `LOCATION:` gauge in the snapshot; the same
   prompt from the Portal produces neither.
4. **Permission-denied path:** deny location on the phone; prompts still send normally
   with no location and no error.
5. **Navigation foreground:** `navigate_device` opens Maps with real turn-by-turn on the
   Fold.
6. **Navigation locked (M3):** phone locked and screen off at a scheduled time → the
   notification arrives, the tap opens navigation. Observe it; do not infer it.
7. **Consent:** a remote nav push prompts for confirmation; the on-device Gemma path is
   unchanged (existing gate tests still pass).
8. **No real coordinates in any committed file** — grep the diff before every commit.

## Non-goals / guardrails

- **No background location permission**, no location foreground service, no polling, no
  geofencing. Location is read only when the user sends a prompt.
- **No location-minted snapshots** and no separate location store — location is metadata
  on a turn that was happening anyway.
- The ledger stays gitignored; never commit `Volumes/`, `Archive/`, `Manifest/`.
- Location must be **per-operator** and must never appear in another operator's context.
- A geocoder outage, a denied permission, or a missing fix must all be silent no-ops —
  none of them may ever fail a chat turn.
- Do not add `navigate` to `HIGH_CONSEQUENCE_INTENTS` wholesale; gate by origin, or the
  device-proven on-device path regresses.
