# TG200 / Asterisk Telephony — Production Pass Design

**Date:** 2026-06-07
**Status:** Design approved (brainstorm complete, all 6 sections validated)
**Author:** Brandon + Claude (Opus 4.8)
**Goal:** Make the Yeastar NeoGate TG telephony stack production-quality and multi-tenant: anyone can plug in their own TG100/TG200/TG400/TG800 with multiple SIMs/numbers, configure it through the Portal UI (no terminal), and have SMS + voice work — with credentials managed safely and the contact-book/PIN security gates preserved.

---

## 1. Audit — what exists today (evidence-based)

The telephony stack is **real and live**, not a stub. Verified on the box 2026-06-07:
Asterisk `active`/`enabled`; AMI :5038, ARI :8088, AudioSocket :9092 all listening on
127.0.0.1; TG200 at `192.168.1.200` reachable; Asterisk configs hand-written 2026-03-25.

### Subsystems
- `Orchestrator/asterisk/` — ARI client (`client.py`), gateway manager (`gateway_manager.py`), `gateways.json`, IVR (`ivr.py`), voice bridge (`voice_bridge.py`), AudioSocket (`audio_subprocess.py`, `audio_ipc.py`), config (`config.py`).
- `Orchestrator/sms/` — AMI SMS client (`ami_client.py`), router (`router.py`), SQLite store (`message_store.py`).
- `Orchestrator/phone/` — session model, `bridge.py` (153 KB), `sip_client.py`, `sms_processor.py`, DTMF, IVR prompts.
- Routes: `asterisk_routes.py`, `phone_routes.py`, `sms_routes.py`, `cellular_routes.py`, `twilio_routes.py`.
- Portal: `modules/telephony-manager.js`, `cellular-manager.js`, `onboarding/steps/optional_integrations.js`.

### Three control planes (the central architectural fact)
| Plane | Transport | Endpoint | Used for | Credential source today |
|---|---|---|---|---|
| **ARI** | HTTP+WS | **your local** Asterisk `127.0.0.1:8088` | Call control, SIP-reg check | `config.py` env (global) — correct |
| **AMI** | TCP | **the TG's own** Asterisk `TG_IP:5038` | SMS send/recv (`gsm send sms`), GSM status (`gsm show spans`) | ⚠️ **hardcoded in `ami_client.py`** |
| **Boa CGI** | HTTP | `TG_IP/cgi/WebCGI?<code>=` | (we wrongly call `/api/v1.0/gsm`) | per-gateway in `gateways.json` |

**Two Asterisks.** The TG itself is an embedded Asterisk (serves `astman.js`), owns the SIM
radios, and is where `gsm send sms`/`gsm show spans` exist — our SMS client connects to the
**TG's** AMI. Our local Asterisk owns the voice path (PJSIP trunk → TG SIP :5060, bridged to
AI via AudioSocket). So per gateway: `http` = TG Boa GUI, `ami` = **TG-side** AMI; ARI is global.

### Device truth (validated against the live unit)
- Yeastar **NeoGate TG200**, `Boa/0.94.14rc21`, firmware ~2024-11.
- **No REST API.** Real HTTP interface is numeric-code CGI: `/cgi/WebCGI?1000=...` (login),
  plus `/cgi/WebBilling`, `/cgi/sPMS`. Our `/api/v1.0/gsm` call can never succeed → it always
  falls into `except: pass` → empty SIM list → UI shows "No SIMs detected."
- SMS path we use (raw AMI `SMSCommand` → `gsm send sms <span> <num> "<text>"`, `ReceivedSMS`
  events inbound) is **correct** for NeoGate AMI (code comments note it was validated via `nc`).

### Security gates (already implemented — must be preserved)
- **Calls:** `asterisk/ivr.py:70` Stage-1 PIN verification (`PHONE_PIN_CODE`, max attempts).
  Unknown callers can reach the IVR but cannot proceed without the code.
- **SMS:** `sms/router.py:98-100` — if the sender matches no contact book, it logs "Ignoring
  SMS from unknown number" and returns **before** any task/`/chat` call. The contact book IS the
  whitelist. SMS has no interactive challenge, so the pre-API whitelist is the only defense.

## 2. Gap analysis (prioritized)

### 🔴 Critical
1. **Hardcoded, committed AMI secret.** `sms/ami_client.py:25-27` defaults host/user/secret
   (`<REDACTED-SECRET>`), in git history (`e5c3cad`). `sms/__init__.py:20` constructs `AMISMSClient()`
   with no args → always uses them. No AMI config var exists (`config.py` has only ARI).
2. **Credentials scattered / not in the gateway model.** `gateways.json` stores only HTTP creds;
   the SMS (AMI) and call (ARI) creds live elsewhere. `get_ami_client()` is a **singleton** bound
   to one host — a second gateway box is impossible.
3. **Span/slot hardwired to TG200's 2-port layout** (`span = port + 1`, default `span=2`).
   `capacity` stored but unused. TG100/400/800 won't map.
4. **No phone-number ↔ SIM/gateway mapping.** `phone_numbers: []` never populated; outbound uses a
   single global `TG200_PHONE_NUMBER` env + `TG200_TRUNK_NAME`. No "send from this number" path.
5. **No automated provisioning.** `pjsip.conf`/`extensions.conf`/`manager.conf` hand-written for
   one TG. `generate_pjsip_trunk_config()` returns a string nothing applies. `ProtectSystem=strict`
   blocks the service from writing `/etc/asterisk/`. TG-side setup is 100% manual.

### 🟠 Important
6. **SMS routing is global, not per-number** (`_find_operator_by_phone` scans all books).
7. **Live UI field-drift bugs:** discovery reads `d.ip_address`/`d.model`/`d.mac_address` but
   backend returns `ip` (no model/mac) → blank discovered cards + broken dedupe; `showEditForm`
   passes `ip_address` but `buildFormHtml` reads `prefill.ip` → edit IP always blank.
8. **Doc/dup mismatch:** `asterisk_routes.py` docstring says "SMS via HTTP API" but uses AMI; two
   send endpoints (`/sms/send`, `/asterisk/sms`) with different default spans.

### 🟡 Polish
9. **Fictional REST status path** (`/api/v1.0/gsm`) → silent "No SIMs detected."
10. **No secrets-at-rest story:** `gateways.json` is git-tracked (currently placeholder only).
11. **`.env.template` telephony is 2 comment lines** — no documented knobs.

## 3. Target design

### Section 1 — Data & credential model (foundation)
`gateways.json` becomes the single source of truth for all per-device credentials + topology:
```jsonc
{
  "id", "name", "model",            // model → port_count + span table
  "ip", "enabled", "sip_port", "codec", "trunk_name",
  "http": { "user", "password" },   // TG Boa GUI (WebCGI)
  "ami":  { "port": 5038, "user", "secret" },  // TG-side AMI (SMS + status) — was hardcoded
  "ports": [                        // replaces phone_numbers[]
    { "span", "slot", "phone_number", "carrier", "enabled", "operator" }
  ]
}
```
ARI stays global (your Asterisk). **Secrets at rest = approach (A):** `gateways.json` gitignored
(+ `gateways.example.json` template), `password`/`secret` encrypted with a key derived from an env
secret. GET never returns secrets (mask to `has_secret: true`); UI sends a secret only on change.
A **recommended default** secret is shipped/pre-filled; customers edit it in the UI.

### Section 2 — Per-gateway AMI runtime (kill the singleton)
`AMIConnectionManager` keyed by `gateway_id`, owning one `AMISMSClient` per enabled gateway built
from that gateway's decrypted `ami` creds. `get_ami_client(gateway_id)` replaces the global. Hot
reload on add/edit/remove (no full restart). **Span math = per-model table** (`TG100→[2]`,
`TG200→[2,3]`, `TG400→[2..5]`, `TG800→[2..9]`). `SMSRouter` registers its callback on every
client; inbound events tagged with `gateway_id`+`span`. Outbound default = first enabled gateway,
with optional explicit `from_number`/`gateway_id`. **SMS is TG-only; Twilio (future) is calls-only.**

### Section 3 — Number/SIM-aware routing
- **Outbound:** `from_number` → `(gateway, span)` via `ports[]`; omitted → first enabled.
- **Inbound:** `gateway_id`+`span` → your **line** (`ports[].phone_number`) → owning **operator**.
- **Storage:** add `line_number` + `gateway_id` columns (idempotent `ALTER`/`pragma table_info`);
  thread by `(operator, line_number, remote)`. Legacy rows backfill `line_number=''`.
- **Reply goes back out the same span.**

#### Security invariants (LOCKED — never weakened by the refactor)
1. **Inbound SMS is always gated by the contact book, pre-API.** No match → silent drop, no task,
   no tokens. The whitelist is absolute.
2. **Inbound calls are gated by the IVR PIN.** Unknown callers reach the IVR but must pass the code.
3. **Line→operator scoping only tightens:** if a line has an owner, the sender must be in *that
   operator's* book; if no owner, search all books; **either way, no match = drop.** A dedicated
   line cannot be reached by someone not whitelisted for it. "No operator" is a deny signal, not an
   open one.

### Section 4 — Status/SIM via AMI (delete the REST fiction)
Replace the `/api/v1.0/gsm` GET with AMI `gsm show spans` parsing → per-span
`{span, carrier, signal, registered, phone_number}`. Reachability stays a Boa-root HEAD; SIP-reg
stays ARI `endpoint detail`. Auto-discovery fingerprint updated to `Server: Boa` +
body contains `NeoGate`/`astman`/`WebCGI`. Gateway cards show real signal bars + carrier per SIM.

### Section 5 — Friction-free provisioning (validate + automate; no terminal for users)
Split by device ownership:
- **Your Asterisk side → fully automated.** One-time `install.sh` changes (run as root at install):
  - append `#include "blackbox.d/*.conf"` to `pjsip.conf` + `extensions.conf`;
  - create `/etc/asterisk/blackbox.d/` writable by the service user;
  - add `ReadWritePaths=/etc/asterisk/blackbox.d` to the unit (narrow hole through
    `ProtectSystem=strict`);
  - add a scoped sudoers entry for `asterisk -rx "pjsip reload"` / `"dialplan reload"`.
  The **runtime never writes sudoers or system config** — consistent with the
  "don't ship orchestrator code that writes /etc/sudoers.d" rule. On gateway add/edit, the
  orchestrator writes the trunk + dialplan into `blackbox.d/` and reloads automatically; prompts
  for a full BlackBox restart only if needed (restart is pre-authorized).
- **TG200 side → guided click-through** in the NeoGate GUI (create AMI user w/ recommended creds,
  point a SIP trunk at your Asterisk, set GSM↔trunk routes), then **live-validate** (AMI auth +
  `gsm show spans` + trunk `online`). Later enhancement: best-effort drive via `WebCGI`.

**Wizard** = stepper modal: Identify → Validate (live green/red) → Configure (auto-apply your
side, copy-through for the TG) → Done.

### Section 6 — UI consolidation + live bug fixes
- Fix `ip`/`ip_address` + `prefill.ip` field drift (discovery + edit form).
- Gateway form/wizard: **Model** dropdown; **AMI creds** (password field, recommended default,
  masked in GET, send-on-change); **per-line table** (span/slot → carrier + signal + editable
  phone number + operator owner + enabled).
- SMS UI line-aware: threads labeled by receiving line; from-number selector on compose.
- API hygiene: converge `/asterisk/sms` + `/sms/send` on the manager; fix the "HTTP API"
  docstring → AMI; mask secrets in all GET responses.

## 4. Out of scope
- Twilio call pipeline (future; calls only — SMS stays TG-only).
- Driving the NeoGate fully via `WebCGI` (v1 is guided click-through + live validation).
- The legacy SIM7600/cellular AT path and `cellular/` internet failover.

## 5. Migration / compatibility
- `gateways.json` schema migration: on load, upgrade old records (add `model` from `capacity`,
  derive `ami` from the env/old defaults once, convert `phone_numbers[]` → `ports[]`).
- `messages` table: additive `ALTER` only; existing threads keep working (`line_number=''`).
- Rotate the leaked AMI secret `<REDACTED-SECRET>` (new NeoGate AMI user + new secret) as part of rollout.
- Prod runs live from the working tree → every committed state must `import Orchestrator.app`.
