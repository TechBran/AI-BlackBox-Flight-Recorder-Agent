# TG200 Inbound Resilience + SMS Follow-ups — Plan

> **For Claude:** after Brandon approves, execute via `superpowers:subagent-driven-development` on `main`. Workstream A's software piece (A3) only builds if the cheap gateway settings (A1) don't fully resolve it — confirm root cause first (A0). B and C are independent and can land regardless.

**Goal:** Eliminate the TG200 inbound-SMS dormancy (gateway settings + diagnosis first; a software health-check/auto-recovery only if needed), and land the two deferred SMS items — M4 thread-memory continuity verification/hardening and a `call_anthropic` consecutive-same-role normalizer — in one branch.

**Status:** PLAN (awaiting Brandon's review). Research complete (multi-agent web sweep, 2026-06-26).

---

## Research summary (TG200 inbound-stops-until-reboot)

**Root cause — CONFIRMED 2026-06-26: full gateway SMS inbox (accumulating, never auto-cleared).** Brandon observed the gateway inbox sitting at **39+ accumulated messages**, and the API permitted-IP (`192.168.1.164` = the BlackBox host) is **correct and working** — so access is ruled out; this is storage accumulation, not a block. Symptom-fit confirmed: outbound keeps working (send doesn't need free receive-storage), the SIM stays registered (radio healthy, only the message store wedged), inbound "works then stops after a while" (the store fills), and a reboot is a temporary band-aid that doesn't drain the pile. The gateway stores every received SMS in its inbox and never deletes them, so it creeps to the storage cap and new inbound has nowhere to land. **Fix = clear the inbox + enable daily auto-clear (A1.1).** (Yeastar doesn't *document* "full store blocks inbound," but the 39+ count + symptom triangulation make this the confirmed cause.)

**Decisive confirmation test (free, do at next hang):** check the gateway SMS Inbox count (`SMS > SMS > Inbox`) / `+CPMS` while inbound is dead. Full/near-full → storage-full confirmed (settings fix it). Empty yet still dead → module/firmware receive-path hang (need the software fallback). 

**Sources:** official *TG Series VoIP 4G Gateway User Guide* PDF (help.yeastar.com), Yeastar FAQ, EOL policy, firmware-download page, community project `github.com/PPFilip/smsgw-tg-amqp`. (support.yeastar.com KBs returned HTTP 403 to automated fetch — the "SMS Failure" KB at `support.yeastar.com/hc/en-us/articles/360007696054-SMS-Failure` should be read manually from a browser.)

---

## Workstream A — TG200 inbound dormancy

### A0 — Confirm root cause (next hang) — Brandon, manual
At the next inbound hang, before restarting: open `SMS > SMS > Inbox` and note the message count / storage state. Record it. This single observation decides whether A1 settings suffice or A3 software is needed. (If `SMS to Email` is enabled per A1.4, also check whether email still delivers during the hang — isolates reception-died vs API-reporting-died.)

### A1 — Immediate gateway settings — Brandon, manual (no code), highest leverage
All menu paths verbatim from the official user guide:
1. **SMS auto-clear (top lever):** `SMS > SMS > SMS Clear Settings` → enable **SMS Clear Enabled**, Status = inbox, a **frequent** clear period (daily or more often) → Save → Apply. Prevents the on-box store filling. **Safe because** the BlackBox persists every `ReceivedSMS` to `Manifest/sms_messages.db` on receipt (`router.py` store-on-receive), so clearing the gateway inbox afterward loses nothing.
2. **Scheduled auto-reboot (watchdog):** `System > System Preferences > Reset and Reboot` → **Enable Auto Reboot** → cadence shorter than the observed time-to-hang → Save → Apply.
3. **Verify SMS API:** `SMS > SMS > API Settings` → **Enable API**, confirm port 5038, and that the **permitted-IP list includes the BlackBox host** (or is empty/unrestricted). Rules out a reporting-only fault (onboarding hardening may have locked the IP list — Brandon's "we disturbed it" hunch).
4. **(Optional insurance) SMS to Email:** `SMS > SMS Settings > Email Settings` → **Enable SMS To Email** → POP3/SMTP + per-trunk email. Independent inbound path + a diagnostic signal of reception-vs-reporting.
5. **Recovery preference:** use **per-module reboot** `Gateway > Mobile List > [module] > Reboot` (guide: "will not affect other mobile modules") or **Power off/Power on** instead of a full-box reboot.

### A2 — Firmware check — Brandon, manual (caution)
Confirm the unit's **SN prefix** (`331…` ⇒ TGv3) and **current firmware** (Status/System page). Latest published is **91.3.0.37 (2026-06-02)**. **DANGER:** applying the 91.x track to a non-TGv3 unit causes a system crash — confirm generation first. Product is EOL (TG200W EOL 2027, "last supported 91.3.0.21" per EOL page — an unresolved discrepancy with the 91.3.0.37 download; verify per SN). No release note names this bug, so treat a flash as "cheap to try, unproven," not a guaranteed fix.

### A3 — Software inbound-dormancy health-check + tiered auto-recovery — BUILD (only if A1/A2 don't resolve it)
New module under `Orchestrator/sms/` (e.g. `inbound_watchdog.py`), wired into the SMS manager lifecycle. TDD.
- **A3.1 Dormancy detection.** Track time-since-last-`ReceivedSMS`. Because real inbound is bursty, add an **active heartbeat**: on a schedule (e.g. hourly) send a loopback SMS to a known endpoint and assert the corresponding `ReceivedSMS` arrives within a timeout. Absence = the hang.
- **A3.2 Disambiguate.** Confirm the AMI socket/login is alive (keepalive) and outbound still succeeds while inbound is silent — that combination is the receive-hang signature. Cross-check `SMS to Email` if enabled.
- **A3.3 Tiered recovery (least-disruptive first):** Tier 1 = per-module reboot/power-cycle via web UI or AMI; Tier 2 = lighter `smscore`-only restart **if** validated reachable on the unit (community-sourced — `PPFilip/smsgw-tg-amqp`; validate before relying); Tier 3 = full-box reboot. Re-establish AMI + re-probe with a heartbeat after recovery.
- **A3.4 Prevention drain-loop.** Independently, periodically poll + drain the gateway inbox (persist each to the ledger, then delete on-box) — a software-controlled equivalent of A1.1 that guarantees capture before deletion.
- **A3.5 Telemetry.** Log every dormancy detection, recovery tier, and time-to-recover, to measure true time-to-hang and tune cadences. (Per [[feedback_telemetry_before_fixes]]: instrument before more guessing.)

**Staging:** A0 → A1 → observe for a few days. If dormancy persists, A2, then A3. Don't build A3 blind — YAGNI until the cheap levers are proven insufficient.

---

## Workstream B — M4 thread-memory continuity (verify + harden)
- **B1 Live verification.** Text the BlackBox twice in succession; confirm the second reply demonstrates continuity with the first (the M4 thread injection is exercised — only unit-tested so far). Capture the log + the prepended `messages` shape.
- **B2 Harden if needed.** Address any edge surfaced by B1 (e.g., very long threads vs the 45s deadline, the window size, multi-part reassembly interplay). Likely small or no-op; gated on B1's result.

## Workstream C — `call_anthropic` alternation normalizer (defense-in-depth)
The M4 fix prevents the SMS path from producing consecutive same-role turns, but **any** caller that does would still hit an Anthropic 400 (`messages: roles must alternate`). Add a shared normalizer.
- **C1** A `_normalize_alternation(messages)` helper that merges consecutive same-role turns (concatenating content) applied to `msg_list` just before `call_anthropic` builds its `amsgs` (`chat_routes.py`). **Must handle string-vs-list (multimodal) content** — merge text parts, preserve content blocks. TDD: consecutive user/user and assistant/assistant collapse correctly; multimodal blocks survive; a normal alternating list is unchanged. Consider applying to the other providers' callers too (or a single shared pre-provider pass), but Anthropic is the one that 400s.
- This touches the **interactive chat hot path**, so it gets its own adversarial review (a normalizer bug would affect all chats).

---

## Out of scope / notes
- A peer-path live test still needs a second non-operator phone number (separate, whenever available).
- Do not flash firmware without confirming TGv3/SN first (crash risk).
