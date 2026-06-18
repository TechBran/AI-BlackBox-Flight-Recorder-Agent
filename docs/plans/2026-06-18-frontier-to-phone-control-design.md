# Frontier → On-Device Gemma device-control tool (`control_phone`) — design

> Validated design from the 2026-06-18 brainstorm with Brandon. Follow-on to the
> shipped on-device Gemma + snapshot-ledger ([[project-on-device-gemma]],
> [[project-snapshot-ledger]]). **Built directly on `main`** in this dev sandbox —
> NO worktree/feature branch ([[feedback-staging-box-as-production]]).

## Goal

A ToolVault tool, `control_phone`, that lets a **frontier model** (Gemini / Claude /
etc. in BlackBox chat) **delegate a device-control task to the on-device Gemma model
on the user's phone**. Gemma executes the task with its intent/accessibility powers
and reports back. This is the **inversion** of everything so far: until now the
phone's model called BACK into the BlackBox (phone→server); now the BlackBox reaches
OUT to the phone (server→phone).

**v1:** the task runs on the **originating** device (the operator whose chat made the
request). **Phase 2:** target ANY reachable registered Gemma by its Tailscale name.

## Validated decisions (brainstorm)

1. **Transport = Direct Tailscale HTTP.** The phone runs an inbound HTTP listener on
   its tailnet address; the BlackBox `POST`s the task and polls for the result. The
   phone is addressable by its tailnet name — Phase 2 ("call any reachable node")
   is the same code path.
2. **Registry / control layer = BlackBox enumerates the tailnet.** `tailscale status`
   (liveness + address) ⋈ the attestation registry (which Gemma a device has) = the
   device list. Join key: the phone's **tailnet name**, recorded at attest.
3. **Consent = YOLO within an allowlist.** Remote tasks run **without per-action
   prompts** (so they never stall unattended), bounded to an **allowlist** of safe
   actions; **high-consequence actions are REFUSED when remote** (not confirmed). The
   allowlist IS the blast radius.

## Architecture

```
USER → FRONTIER MODEL (BlackBox chat)
  "...do X on my phone"
   │  (the model first says: "Waking Gemma on your phone — I'll report back…")
   ▼
control_phone(task)  [ToolVault tool, BLOCKING]
   ▼
BLACKBOX executor
   1. resolve device: operator → tailnet node  (tailscale status ⋈ attestation)
   2. POST https://<phone-tailnet-name>/task   { task, operator, allowlist_mode }
   3. poll GET …/status  → waking → working → done|error   (wake-tracking)
   ▼ (over Tailscale)
PHONE — LocalModelService HTTP listener (tailnet-bound)
   • POST /task   → wake/load Gemma → run on-device agent loop (REMOTE ALLOWLIST
                    filter: safe actions run YOLO; high-consequence REFUSED)
   • GET /status  → {phase, progress, result|error}
   • GET /healthz → liveness for the device list
   ▼
result → executor returns to the frontier model → "Done — <result>"
   (unreachable / wake-fail / timeout → structured error → frontier retries or ends)
```

## Components

### Backend (Orchestrator/, Python)
- **ToolVault module** `ToolVault/tools/control_phone/` (`schema.json` + `executor.py`):
  - schema: `task: str` (the natural-language device task). Description STEERS the
    frontier model to pre-announce ("Waking Gemma… I'll report back") and notes it
    blocks + may take a while. (Phase 2: optional `device: str` = tailnet name.)
  - executor (blocking): resolve device → POST /task → poll /status until
    `done`/`error`/timeout → return the result text or a structured error.
- **Device resolution** `Orchestrator/local_provider/` (new, e.g. `mesh.py`):
  - `tailscale status` (JSON) → online nodes + tailnet names/IPs.
  - ⋈ `LocalProviderRegistry` attestations (operator → device → model) on the
    tailnet-name join key.
  - `reachable_devices(operator?)` → the list (online + has-Gemma); `resolve(operator)`
    → the originating device's tailnet address (v1).
- **Attestation extension:** `/local/device/attest` records the phone's **tailnet
  name** (the phone supplies it; it knows its own Tailscale identity) so the join works.
- **Blocking + tracking:** poll `GET /status` with a sensible interval; overall
  timeout (model load ~10-75s + execution → generous, e.g. 4-5 min); map
  unreachable / wake-fail / timeout → a clear error the frontier model can act on.

### Phone (Android, Kotlin)
- **HTTP listener** hosted by the foreground `LocalModelService`, bound to the
  tailnet interface (an embedded server — NanoHTTPD/Ktor): `POST /task`,
  `GET /status`, `GET /healthz`.
- **Remote task runner:** reuses the on-device native agent loop (the snapshot-ledger
  pipeline) but with a **remote-allowlist filter** wrapping tool dispatch:
  - allowlisted action → run (YOLO, no ConfirmGate prompt);
  - high-consequence action → REFUSE with a clear "refused: not allowed for remote
    control" result (the loop continues / wraps up).
  - Each task tracked as `waking → working → done|error` for `GET /status`.
- **Auth/scope:** Tailscale perimeter ([[tailscale-security-perimeter]]) +
  accept tasks only from the paired hub + for the bound operator.

### Allowlist (default — Brandon to refine)
- **Safe (run remote):** `read_screen`, `open_app`, `tap`, `swipe`, `scroll`,
  `home`, `back`, `show_map`, `flashlight_on/off`, `open_url`, `open_wifi_settings`,
  `open_settings_panel`, `take_photo`, `set_timer`, `web_search`.
- **Refused (remote):** `send_sms`, `send_email`, `dial`, `create_contact`,
  `create_calendar_event`, `set_alarm`, `share_text`, and any payment/irreversible/
  outbound action. (These stay available on the LOCAL, user-present path.)

## Interim UX (the "waking up… will report back")
The tool is **blocking**, but the frontier model emits its pre-call text first
("Waking Gemma on your phone — I'll report back…"), then the tool runs (chat shows
the working indicator), then the model continues with the result. The backend
**polls** the phone's `/status` for wake-tracking; on timeout/failure it returns a
structured error and the model decides (retry / give up).

## Out of scope / Phase 2
- **Phase 2:** the `device` param → call any reachable registered Gemma by tailnet
  name (the list + resolution already support it; just expose the param + a
  "list my devices" affordance).
- FCM push-wake for fully-killed apps (v1 assumes the FG service keeps the listener alive).
- Multi-device fan-out / parallel delegation.

## Open questions for the plan
- Embedded HTTP server lib on Android (NanoHTTPD vs Ktor) + binding to the tailnet IP.
- Exact poll interval + total timeout (tune with device timing).
- How the phone authenticates "the paired hub" (pairing token vs Tailscale source identity).
- Whether `/status` streams partial progress or just phase transitions (v1: phases).
- Allowlist final contents (Brandon's safety call).
