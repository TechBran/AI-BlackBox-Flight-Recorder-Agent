# Tailscale Wizard Actuator — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Audit pass must run BEFORE Phase 2 (implementation) per Brandon's audit-then-execute pattern (validated 4x consecutively across Track 3 + Track 4).

**Goal:** Upgrade `Portal/onboarding/steps/tailscale.js` Branches B + C from copy-paste-command UI to one-click actuator buttons that install Tailscale, authenticate via browser flow, and auto-configure MagicDNS + HTTPS cert — all from inside the wizard with no terminal context switch.

**Architecture:**
- 5 new FastAPI endpoints under `/onboarding/tailscale/*` (install-stream, up, poll, cert, accept-dns) — backend handles all sudo via NOPASSWD sudoers entry written by `install.sh`.
- Backend opens the Tailscale auth URL in default browser via `xdg-open` subprocess (NOT Tauri shell plugin) — this keeps the wizard renderer-agnostic (works identically in Tauri + Chrome dev + Firefox).
- Per-step server-side mutex prevents double-click race conditions during long-running operations (install: 30-90s, auth: up to 3 min).
- Validator extended to detect MagicDNS state from `tailscale status --json` (`MagicDNSSuffix` field).
- Tailnet-admin-console settings (MagicDNS toggle, HTTPS toggle) cannot be changed from device — when detected as off, surface deep-link to right admin panel.

**Tech Stack:** FastAPI + StreamingResponse for SSE • Python `subprocess.Popen` with line-buffered stdout for install streaming • `xdg-open` for browser launch • bash + sudoers.d (mode 0440) • vanilla JS in `tailscale.js` (no framework — matches existing pattern).

---

## Audit pass — bugs caught at plan-time, before code is written

Per Brandon's audit-then-execute pattern (4x validated: Phase 2.10 → Track 3 → Track 4 audit → Track 4 hardware test). Numbered the same way Track 3/4 audits did: C=critical, M=major, I=important, N=nice-to-have, Q=question.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| C1 | CRITICAL | `tauri-plugin-opener` JS API requires `withGlobalTauri: true` in `tauri.conf.json` PLUS `@tauri-apps/plugin-opener` npm dep PLUS importing it in the page. Current Portal wizard is served by FastAPI as an external URL, NOT bundled into Tauri — so even with all that wiring, the JS imports would couple Portal code to Tauri runtime. **Resolution:** Backend uses `xdg-open <url>` subprocess instead. Works identically in any renderer. Fallback display: always render `<a href target="_blank">` as visible-and-clickable secondary path if backend xdg-open fails (e.g., headless Linux). |
| C2 | CRITICAL | `sudo tailscale up` blocks until user authenticates in browser — process holds stdout open until BackendState transitions to Running OR user aborts. Backend can't synchronously wait inside a request handler (would hold the HTTP worker thread for 3+ min). **Resolution:** `up` endpoint returns 200 immediately after capturing the `Login URL:` line from stdout, launches xdg-open, leaves `tailscale up` running in background. Separate `/onboarding/tailscale/poll` endpoint polls `tailscale status --json` every 2s; UI calls it on a 2s timer until success or timeout. The original `tailscale up` subprocess is owned by an asyncio background task that gets cleaned up on poll-success OR `cancel` endpoint. |
| C3 | CRITICAL | If user double-clicks "Install Tailscale Now" or returns to wizard mid-install, a second `curl ... | sudo sh` would race. **Resolution:** Per-step module-level asyncio.Lock guarding the operation; second concurrent call returns 409 Conflict with current operation state. UI shows "Already in progress" + re-attaches to existing stream. |
| C4 | CRITICAL | Sudoers entry for `tailscale up *` with wildcard could allow `tailscale up; rm -rf /` via shell injection if the running service ever interpolates user input into command args. **Resolution:** (a) NEVER use `shell=True` in the subprocess.Popen calls — always pass argv list, (b) sudoers entries use EXACT command strings, not wildcards where possible (e.g., `/usr/bin/tailscale up --accept-dns=true --hostname=*` allows hostname variance but not arbitrary flag injection), (c) hostname value is sanitized server-side to `[a-zA-Z0-9-]+` only before being passed. |
| M1 | MAJOR | `validate_tailscale` returns "binary not found" if `shutil.which("tailscale")` fails. After `tailscale` is installed to `/usr/bin/tailscale`, the calling Python process may have already cached PATH — `shutil.which` re-scans every call so this is fine, BUT FastAPI's working dir + env may not include `/usr/bin` if the systemd service has hardened PATH. **Resolution:** Verify systemd unit doesn't set restrictive `Environment=PATH=`. Current install.sh sets none, default = `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` which DOES include /usr/bin. Document as confirmed-OK in plan. |
| M2 | MAJOR | `tailscale cert <hostname>` requires the tailnet's admin-console HTTPS toggle to be ON. If off, command errors with `failed to fetch certificate: HTTPS is disabled`. **Resolution:** Wizard runs cert command, catches the specific error, surfaces banner: "Enable HTTPS in your Tailscale admin console — [Open admin console]" with deep-link `https://login.tailscale.com/admin/dns` (the HTTPS toggle lives on the same page as MagicDNS). After user enables, "Re-check" button re-runs cert command. |
| M3 | MAJOR | MagicDNS is a TAILNET setting (not device); the device `--accept-dns` flag only tells the device to USE MagicDNS if the tailnet has it on. Setting `--accept-dns=true` when MagicDNS is off has no effect. **Resolution:** Wizard tries `tailscale set --accept-dns=true` (device-side, idempotent) and ALSO checks `MagicDNSSuffix` in status. If suffix present → MagicDNS working. If absent → surface deep-link with explanation. |
| M4 | MAJOR | `tailscale up` re-auth scenario (180-day key expiry): backend can't tell from `BackendState=NeedsLogin` alone whether the user JUST installed OR whether keys expired on a previously-configured device. **Resolution:** OK as-is — both cases use the same recovery (`tailscale up` → browser auth → poll). The status badge in UI can say "Tailscale needs re-authentication" (generic phrasing covers both first-auth and re-auth without misleading). |
| M5 | MAJOR | `install.sh` is re-runnable (Brandon often re-runs after pulling repo updates). Sudoers.d file write must be idempotent — overwrite cleanly each time. **Resolution:** `install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/blackbox-tailscale <<EOF ... EOF` — `install` command atomic-replaces existing file with correct mode/owner in one operation. visudo-check the file after write to abort if syntax broken. |
| M6 | MAJOR | If `tailscale up` fails BEFORE printing the Login URL (e.g., tailscaled daemon not running), backend would wait forever for the URL line. **Resolution:** 15-second timeout waiting for `Login URL:` regex match in stdout. On timeout, kill subprocess + return 500 with stderr captured. Pre-check: backend verifies `tailscale status` returns something parseable BEFORE invoking up — if daemon entirely dead, fail fast with clear message + suggest `sudo systemctl restart tailscaled`. |
| I1 | IMPORTANT | The `xdg-open` subprocess to launch a browser inherits the FastAPI service env. systemd services don't always have `DISPLAY` set — Tauri runs as a user-session app and DOES have DISPLAY, but if the wizard is accessed from a remote browser (Brandon's phone), the device-side xdg-open would either fail or open on the device screen (wrong place). **Resolution:** UI always renders the auth URL as a visible-and-clickable `<a href target="_blank">` link in addition to triggering xdg-open. Customer on remote device clicks the link in THEIR browser. Customer at device benefits from auto-open. Both paths work. |
| I2 | IMPORTANT | After `tailscale cert <hostname>` succeeds, the cert lives at `/var/lib/tailscale/certs/<hostname>.crt` + `<hostname>.key`. To actually USE these in Portal, uvicorn would need to launch with `--ssl-certfile` + `--ssl-keyfile` AND restart whenever the cert renews (Tailscale auto-renews ~14 days before expiry). **Resolution: OUT OF SCOPE for this plan.** The cert request is captured and the file paths are logged for the customer's record (so v1.1 polish can pick it up), but uvicorn stays HTTP-only for now. Wizard surfaces an info banner: "Cert obtained — HTTPS Portal support arriving in v1.1." Customer can still access via http://<hostname> on the tailnet. |
| I3 | IMPORTANT | Re-auth flow needs entry point that isn't "complete the entire onboarding wizard again." **Resolution:** Wizard's manage mode (`/usr/bin/blackbox-setup` without `--first-run`) already opens to a step picker; tailscale step accessible from there. No new code needed for entry-point — manage mode already covers it. Audit confirms by reading installer/src-tauri/src/main.rs mode-detection. |
| N1 | NICE | Live install stream could show fancy progress (% installed, current package). **Resolution:** SKIPPED for v1. Raw line-by-line stdout from `tailscale install.sh | sudo sh` is informative enough. Pre-render a friendly "Installing Tailscale… (30-90 seconds)" header above the stream so customer knows what to expect. |
| N2 | NICE | Could verify install integrity with PGP signature check on the install.sh script before piping to shell. **Resolution:** SKIPPED — Tailscale's official curl-to-shell idiom is industry-standard and the script itself is GPG-signed apt repo setup. Adding pre-validation adds complexity for marginal gain. Document the trust assumption in the plan. |
| N3 | NICE | Telemetry / metrics for each step's success rate. **Resolution:** SKIPPED — minting a snapshot at session end captures empirical findings; per-event telemetry is overkill for v1. |
| Q1 | QUESTION | Should the wizard auto-advance to next step after successful auth, OR require Continue click? **Resolution:** Require Continue click. Customer needs a beat to see the success state + understand the Portal URL they're about to use. Match Branch A current behavior. |
| Q2 | QUESTION | What hostname does `tailscale up --hostname=<X>` use? **Resolution:** Use `gethostname()` Python result (already what the device uses for tailnet name by default). Sanitize to `[a-z0-9-]+` lowercase. If user wants a custom hostname, that's a manage-mode feature (out of scope for first-run wizard). |
| Q3 | QUESTION | Should we offer Tailscale account creation BEFORE running `tailscale up`? Currently Branch D disclosure recommends signup link. **Resolution:** Leave Branch D as-is. After [Authenticate Now] click, `tailscale up` opens the browser to Tailscale's login page which itself offers signup. No additional logic needed. |
| I4 | IMPORTANT | **MagicDNS gates the Android app pairing flow.** The Android app reaches the Portal at `https://<device>.<tailnet>.ts.net` — that hostname resolution requires MagicDNS enabled tailnet-wide (admin console toggle). Without it, the phone cannot resolve the URL even though both phone + BlackBox are on the tailnet. **Resolution:** MagicDNS banner can NOT be a generic "enable it at admin console" — it must (a) name the consequence: Android app won't work, (b) give step-by-step instructions exact enough that a non-technical customer can follow without searching, (c) emphasize it takes ~10 seconds. Same banner shape for HTTPS toggle (lives on same admin page). Banner content rewritten in Task 8. |
| M7 | MAJOR | `tailscale cert <hostname>` triggers Let's Encrypt ACME flow on first call — round-trip can take 30+ seconds depending on Let's Encrypt response time. Without a loading state in the banner, the customer may think it failed silently. **Resolution:** Cert banner shows "Requesting HTTPS certificate..." spinner state while the fetch promise is pending. Replace with success/error state on resolution. Add a 60-second client-side timeout to bail with a "took too long — try Re-check" CTA. |
| I5 | IMPORTANT | The 4-minute auth poll timeout may not accommodate slow logins (2FA, password recovery, SSO redirects through multiple IdPs). **Resolution:** Bump to 5 min. At 3 min insert a "Still waiting? Make sure you completed the login in your browser. [Re-open login URL]" hint so customer knows they're not abandoned. |
| N4 | NICE | If user navigates away from Branch A and comes back, the cert + accept-dns calls fire again unnecessarily (idempotent so safe, but wasteful). **Resolution:** Module-level boolean `_certAttemptedThisSession` guards against re-firing. Cleared on full page reload or on `/onboarding/reset`. |

**Total: 4 critical, 7 major, 5 important, 4 nice-to-have, 3 questions resolved.** Same audit shape as Track 4 (which caught 8 more bugs in hardware test); expect similar pattern here — plan-time + implementation + MSO5 hardware test = three filters for empirical bugs.

---

## Empirical findings (caught during execution — plan amendments)

These were not visible at plan time. Captured here to keep the plan as a complete historical record + warn future-readers.

| # | Discovered | Finding | Resolution |
|---|---|---|---|
| E1 | T2 hardware test on MSO5 | **sudoers parser treats `\|` as a directive separator** — the planned line `bbx ALL=(root) NOPASSWD: /bin/sh -c curl -fsSL https://tailscale.com/install.sh \| sh` failed visudo with "syntax error" at column 85 (the pipe). Sudoers has its own grammar, not bash. Pipes/ampersands/semicolons cannot appear in command paths. If shipped as-is, every customer install would `exit 1` at Step 4e. | **Pre-install Tailscale via MUST_HAVE.** Brandon's decision after AskUserQuestion: simplest path is to add `tailscale` to install.sh's apt MUST_HAVE list (alongside curl/jq/etc). Tailscale is always installed on every BlackBox (~50MB binary). The curl\|sh sudoers entry is **deleted entirely** from the template. Downstream consequences: **Task T3 (install SSE endpoint) and Task T6 (Branch B install button UI) are SKIPPED** — Branch B's "binary not found" path becomes effectively unreachable on a fresh BlackBox. `validate_tailscale`'s `shutil.which("tailscale")` check stays as defensive code; if a customer manually uninstalls, the wizard falls back to a static "please re-run install.sh" message (one-line UI change, NOT a full Branch B build). Branch B fall-back UI handled in T7 dispatch brief (single-paragraph guidance instead of full button-stream-recheck flow). |
| E2 | T4 hardware test on MSO5 | **`NoNewPrivileges=true` in blackbox.service blocks sudo escalation entirely.** Track 3's systemd-hardening defaults included `NoNewPrivileges=true` for sandboxing. With it set, `sudo -n /usr/bin/tailscale up` from the wizard actuator failed with "sudo: The 'no new privileges' flag is set, which prevents sudo from running as root" — process exited rc=1, readline loop hit EOF, start_up raised "Tailscale did not emit a login URL" → 500. **This affects ALL wizard actuator sudo calls** (T4 today, T5 cert/dns tomorrow), not just one endpoint. Would have killed the entire feature on first customer click. | **Flip `NoNewPrivileges=true` → `false` in install.sh Step 4 unit file.** Bounded NOPASSWD sudoers entries (T2) remain the security boundary — only specific tailscale subcommands with literal arg matching are permitted, which is meaningfully tighter than the full-shell access the previous NoNewPrivileges flag was guarding against. Fix applied in T4's commit (3ee359d) since deferring would have left T4 in a non-working state. Live-patched the running unit on MSO5 + daemon-reload + restart to confirm. **Downstream T5 (cert + accept-dns) inherits this fix automatically — no per-endpoint work needed.** |

---

## Acceptance criteria

A customer with a fresh MSO2 Ultra running Ubuntu 24.04 Desktop minimal, with Tailscale never installed, can complete the entire Tailscale wizard step without ever opening a terminal:

1. Lands on Tailscale step → sees [Install Tailscale Now] button
2. Clicks button → live install stream shows progress (30-90s)
3. Install completes → auto-transitions to [Authenticate Now] button
4. Clicks button → default browser opens to Tailscale login URL
5. Completes login in browser → wizard auto-detects (poll every 2s), shows success badge with hostname + IP
6. Wizard auto-runs `tailscale set --accept-dns=true` (silent — informational only)
7. Wizard auto-runs `tailscale cert <hostname>` — either:
   - Success → banner: "HTTPS cert obtained — ready for v1.1+"
   - "HTTPS disabled" error → banner with step-by-step instructions + admin console deep-link + Re-check button
8. If MagicDNS not detected → banner explicitly framed around Android-app-pairing dependency, with numbered step-by-step instructions for the admin console toggle + Re-check button
9. Continue button advances to next step

**The Android-app criterion (load-bearing):** After completing the wizard with MagicDNS enabled, the customer's Android phone (on the tailnet) can scan/enter the Portal URL `https://<device>.<tailnet>.ts.net` and successfully pair. This is the I4 acceptance bar — the whole point of MagicDNS surfacing this prominently.

A customer whose Tailscale key has expired (180-day re-auth) can re-authenticate from manage mode:

1. Launches BlackBox Setup → manage mode (not first-run)
2. Picks Tailscale step
3. Sees [Authenticate Now] button (same Branch C UI as new install)
4. Completes auth → success, Continue back to portal

---

## Test plan

### Manual tests on MSO5 (after implementation, before merge)

**Test M1 — Fresh install path (Branch B → Branch A):**
1. SSH to MSO5, `sudo apt-get remove --purge tailscale && sudo systemctl daemon-reload`
2. Verify Tailscale gone: `which tailscale` returns nothing
3. In Tauri wizard, navigate to Tailscale step
4. Expect Branch B with [Install Tailscale Now] button
5. Click → stream should show apt repo add + apt install progress
6. After ~60s, Branch C [Authenticate Now] should appear
7. Click → default browser opens to login URL
8. Complete login → within 4s, Branch A success badge appears
9. Verify hostname + IP populated correctly
10. Cert + magicdns banners render per tailnet state

**Test M2 — Re-auth path (Branch C only):**
1. SSH to MSO5, `sudo tailscale logout`
2. Verify status: `tailscale status --json | jq .BackendState` → "NeedsLogin"
3. In wizard, navigate to Tailscale step
4. Expect Branch C with [Authenticate Now] button (NOT Branch B — already installed)
5. Click → browser opens, login, polling completes, Branch A renders
6. Continue button works

**Test M3 — MagicDNS-off edge case:**
1. In Tailscale admin console, turn OFF MagicDNS for the tailnet
2. SSH to MSO5, `sudo tailscale logout && sudo tailscale up` (re-auth)
3. Verify status: `tailscale status --json | jq .MagicDNSSuffix` → null
4. In wizard, navigate to Tailscale step (Branch A — already authed)
5. Expect MagicDNS banner with admin console deep-link
6. Re-enable MagicDNS in admin console
7. Click banner's "Re-check" → banner disappears

**Test M4 — HTTPS-off edge case:**
1. In Tailscale admin console, turn OFF HTTPS for tailnet
2. SSH to MSO5, `sudo rm -rf /var/lib/tailscale/certs/*.crt`
3. In wizard, navigate to Tailscale step (Branch A)
4. Expect HTTPS cert banner with admin console deep-link
5. Re-enable HTTPS in admin console
6. Click banner's "Re-check" → cert is fetched, banner changes to "v1.1+" notice

**Test M5 — Sudoers idempotency:**
1. SSH to MSO5, `sudo cat /etc/sudoers.d/blackbox-tailscale`
2. Re-run install.sh fully
3. `sudo cat /etc/sudoers.d/blackbox-tailscale` — should be byte-identical
4. `sudo visudo -c -f /etc/sudoers.d/blackbox-tailscale` → "/etc/sudoers.d/blackbox-tailscale: parsed OK"

**Test M6 — Double-click race condition:**
1. In wizard Branch B, click [Install Tailscale Now]
2. While stream is running, click button again rapidly
3. Expect 409 Conflict + UI message "Install already in progress" — no second curl spawn
4. Verify only one `tailscaled.service` got created (not two)

**Test M7 — `tailscale up` daemon-dead edge case:**
1. SSH to MSO5, `sudo systemctl stop tailscaled`
2. In wizard, click [Authenticate Now]
3. Expect 500 error within ~15s + UI message: "Tailscale daemon not running — try `sudo systemctl restart tailscaled`"
4. NOT an infinite spinner

**Test M8 — Remote-device wizard access (I1 edge case):**
1. From Brandon's laptop browser (not Tauri), navigate to `https://<mso5-hostname>.ts.net/onboarding/`
2. Tailscale step renders with auth URL visible-and-clickable
3. Click link → opens in laptop browser (correct)
4. Verify xdg-open on MSO5 didn't ALSO try to open a browser on the headless device

**Test M9 — Android app pairing AFTER MagicDNS toggle (I4 acceptance):**
This validates that the wizard's MagicDNS banner actually unblocks the Android app — the load-bearing reason for I4's banner-content rewrite.
1. Pre-condition: Tailscale admin console MagicDNS toggle OFF.
2. Complete wizard auth flow on MSO5 → land on Branch A.
3. Verify MagicDNS banner renders with: (a) "Android app won't pair" framing, (b) numbered steps 1-4, (c) "Open admin console" primary button + "Re-check" secondary button.
4. Follow the banner's step-by-step exactly (open admin console, find MagicDNS, toggle on, return, click Re-check).
5. Banner should disappear after Re-check.
6. On Brandon's Android phone (also on tailnet), open the BlackBox Android app + pair to `https://<hostname>.<tailnet>.ts.net` (the Portal URL shown in Branch A).
7. Pairing should succeed — phone resolves hostname via MagicDNS, app connects to Portal.
8. If pairing FAILS even after MagicDNS toggle: banner instructions were misleading OR there's a deeper MagicDNS propagation delay we need to surface. Add audit row in close-out commit.

**Test M10 — Cert pending state visible (M7 acceptance):**
1. Pre-condition: HTTPS toggle on in admin console, no cached cert on device (`sudo rm -f /var/lib/tailscale/certs/*`)
2. Complete wizard auth flow.
3. Branch A should show cert PENDING banner ("Requesting HTTPS certificate from Let's Encrypt...") with spinner animation for the duration of the ACME flow.
4. Banner should swap to "HTTPS certificate obtained" success once complete.
5. If cert flow exceeds 60s, expect cert-timeout banner with Re-check button.

**Test M11 — Auth poll "still waiting?" hint (I5 acceptance):**
1. SSH: `sudo tailscale logout`
2. In wizard, click [Authenticate Now] → don't complete the browser login.
3. Wait 3 minutes.
4. Expect amber "Still waiting?" hint to appear under the spinner with re-open-URL link.
5. Wait 2 more minutes (5 total).
6. Expect timeout error + Try Again button (NOT infinite spinner).

---

## Implementation tasks

Tasks designed for 2-5 min steps each. Each task ends in commit. TDD where applicable.

### Task 1: Extend `validate_tailscale` to capture MagicDNSSuffix

**Files:**
- Modify: `Orchestrator/onboarding/validators.py:133-154`

**Step 1: Read current return shape**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && sed -n '133,154p' Orchestrator/onboarding/validators.py`
Expected: function returns `hostname`, `ip`, `online` only — no `magicdns_suffix` field.

**Step 2: Add MagicDNSSuffix to the returned dict**

Modify the `_fn` inner function's return:

```python
def _fn():
    if not shutil.which("tailscale"):
        raise RuntimeError("tailscale binary not found on PATH")
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tailscale status failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    backend = data.get("BackendState", "unknown")
    if backend != "Running":
        raise RuntimeError(f"tailscale not running (BackendState={backend})")
    self_node = data.get("Self", {})
    magicdns_suffix = data.get("MagicDNSSuffix") or ""  # empty string if MagicDNS off for tailnet
    return {
        "hostname": self_node.get("DNSName", "").rstrip("."),
        "ip": (self_node.get("TailscaleIPs") or ["unknown"])[0],
        "online": self_node.get("Online", False),
        "magicdns_suffix": magicdns_suffix,
        "magicdns_enabled": bool(magicdns_suffix),
    }
```

**Step 3: Quick local verification**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -c "from Orchestrator.onboarding.validators import validate_tailscale; import json; print(json.dumps(validate_tailscale(), indent=2, default=str))"`
Expected: dict containing `magicdns_suffix` + `magicdns_enabled` keys.

**Step 4: Commit**

```bash
git add Orchestrator/onboarding/validators.py
git commit -m "feat(onboarding): validate_tailscale captures MagicDNS state

Extend the status-probe to include MagicDNSSuffix from
'tailscale status --json'. UI uses magicdns_enabled boolean to
decide whether to surface the 'enable MagicDNS in admin console'
banner. magicdns_suffix string (e.g. 'tail401fb3.ts.net') is
informational for the success view.

Step 1 of Tailscale Wizard Actuator plan (T1)."
```

---

### Task 2: Sudoers.d template + install.sh wiring

**Files:**
- Create: `installer/templates/sudoers-blackbox-tailscale` (template file with placeholder for $REAL_USER)
- Modify: `Scripts/install.sh` (add Step 4e after Step 4d helper script)

**Step 1: Create sudoers template**

Write `installer/templates/sudoers-blackbox-tailscale`:

```
# /etc/sudoers.d/blackbox-tailscale
# Generated by BlackBox install.sh — DO NOT EDIT BY HAND.
# Grants the BlackBox service user passwordless sudo for the
# specific tailscale operations the onboarding wizard needs.
# Bounded scope: only these commands, no wildcards on dangerous flags.

REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/apt-get install -y tailscale
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/tailscale up --accept-dns=true --hostname=*
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/tailscale set --accept-dns=true
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/tailscale cert *
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/tailscale logout
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl start tailscaled
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl enable tailscaled
REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /bin/sh -c curl -fsSL https://tailscale.com/install.sh | sh
```

**Note on the install command:** Tailscale's official install.sh is `curl | sh`. We allow that exact pipeline via NOPASSWD: `/bin/sh -c curl -fsSL https://tailscale.com/install.sh | sh`. Audit C4 mitigation: subprocess.Popen uses argv list `["sudo", "/bin/sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]` — no user input ever interpolated into this string.

**Step 2: Add Step 4e to install.sh**

Insert after Step 4d (line ~155, before Step 5 Tauri build):

```bash
# ── Step 4e: sudoers grant for runtime tailscale operations ──
# (Tailscale wizard actuator: bounded NOPASSWD for the specific commands
# the onboarding step needs. install -m 0440 atomic-replaces existing
# file; visudo-check aborts if syntax broken.)
sed "s/REAL_USER_PLACEHOLDER/$REAL_USER/g" \
    "$BLACKBOX_ROOT/installer/templates/sudoers-blackbox-tailscale" \
    | sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/blackbox-tailscale
if ! sudo visudo -c -f /etc/sudoers.d/blackbox-tailscale > /dev/null 2>&1; then
    echo "[install] ERROR: sudoers file syntax check failed" >&2
    sudo rm -f /etc/sudoers.d/blackbox-tailscale
    exit 1
fi
echo "[install] Sudoers grant written for $REAL_USER (tailscale operations)"
```

**Step 3: Manual test (apply to MSO5)**

SSH to MSO5, copy these two new artifacts over via scp (or have Brandon git pull + re-run install.sh). Verify:
```bash
sudo cat /etc/sudoers.d/blackbox-tailscale | grep "^bbx"
sudo visudo -c -f /etc/sudoers.d/blackbox-tailscale
```
Expected: lines start with `bbx ALL=(root)...`, visudo parses OK.

**Step 4: Test NOPASSWD actually works**

```bash
sudo -u bbx -- sudo -n tailscale status --json | jq .BackendState
```
Expected: no password prompt, returns "Running" (or "NeedsLogin" if logged out). Note: `tailscale status` doesn't actually need sudo, but this confirms the user can invoke sudo without a TTY.

**Step 5: Test idempotency (M5)**

Re-run install.sh fully. Diff before/after the sudoers file: should be identical.

**Step 6: Commit**

```bash
git add installer/templates/sudoers-blackbox-tailscale Scripts/install.sh
git commit -m "feat(install): sudoers grant for runtime tailscale operations

install.sh Step 4e writes /etc/sudoers.d/blackbox-tailscale via
atomic 'install -m 0440', then visudo-checks. Grants the BlackBox
service user NOPASSWD for the specific commands the wizard's
Tailscale actuator needs: install, up, set, cert, logout, daemon
start/enable. Idempotent (overwrite-safe).

Audit C4 mitigation: bounded scope, no wildcards on dangerous
flags (only --hostname=* and cert <hostname> which are sanitized
server-side to [a-z0-9-]+ before invocation).

Step 2 of Tailscale Wizard Actuator plan (T2)."
```

---

### Task 3: Backend `/onboarding/tailscale/install/stream` SSE endpoint

**Files:**
- Create: `Orchestrator/onboarding/tailscale_actuator.py` (new module — keeps wizard-actuator logic out of routes file)
- Modify: `Orchestrator/routes/onboarding_routes.py` (mount new endpoints)

**Step 1: Create `tailscale_actuator.py` skeleton with the operation lock**

```python
"""Tailscale wizard actuator — long-running operations the onboarding UI
   triggers (install, authenticate, cert). All sudo wrapped via the
   NOPASSWD sudoers entry from install.sh Step 4e.

   Audit refs:
   - C3 (double-click race): per-operation asyncio.Lock
   - C4 (shell injection): subprocess argv lists only, hostname sanitized
   - M5 (sudoers idempotency): handled in install.sh, not here
   - M6 (auth timeout): 15s timeout waiting for Login URL line
"""
import asyncio
import re
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# One lock per operation so install + auth can serialize independently.
_install_lock = asyncio.Lock()
_up_lock = asyncio.Lock()

# Background process handle for active `tailscale up` — needed to poll
# completion + clean up when status transitions to Running OR timeout.
_active_up_process: subprocess.Popen | None = None
_active_up_login_url: str | None = None

@asynccontextmanager
async def operation_lock(lock: asyncio.Lock, name: str):
    """Acquire lock or raise 409-equivalent."""
    if lock.locked():
        raise RuntimeError(f"{name} already in progress")
    async with lock:
        yield

def _safe_hostname() -> str:
    """Return a sanitized hostname suitable for --hostname= flag.
    Audit C4: only [a-z0-9-] allowed."""
    raw = socket.gethostname().lower()
    safe = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
    return safe or "blackbox"
```

**Step 2: Add `stream_install` async generator**

```python
async def stream_install() -> AsyncIterator[bytes]:
    """Yields SSE-formatted progress events while Tailscale install runs.
    Caller must hold _install_lock.
    """
    cmd = ["sudo", "-n", "/bin/sh", "-c",
           "curl -fsSL https://tailscale.com/install.sh | sh"]
    yield b"event: start\ndata: Installing Tailscale...\n\n"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            # SSE format: lines must not contain raw newlines mid-event
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            yield f"data: {text}\n\n".encode("utf-8")
        rc = await proc.wait()
        if rc == 0:
            yield b"event: done\ndata: installed\n\n"
        else:
            yield f"event: error\ndata: install exited rc={rc}\n\n".encode("utf-8")
    except asyncio.CancelledError:
        proc.terminate()
        raise
```

**Step 3: Add FastAPI route in `onboarding_routes.py`**

After existing routes (after line ~310 / `/reset` endpoint), add:

```python
from fastapi.responses import StreamingResponse
from Orchestrator.onboarding import tailscale_actuator as ts_act

@router.post("/tailscale/install/stream")
async def tailscale_install_stream():
    """SSE stream of Tailscale install progress. Acquires install lock;
    returns 409 if another install is already running."""
    try:
        async def gen():
            async with ts_act.operation_lock(ts_act._install_lock, "install"):
                async for chunk in ts_act.stream_install():
                    yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")
    except RuntimeError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=str(e))
```

**Step 4: Manual SSH test on MSO5**

Before any UI work, prove the backend endpoint works:
```bash
ssh bbx@192.168.1.171 'sudo apt-get remove -y --purge tailscale' && \
  curl -N -X POST http://192.168.1.171:9091/onboarding/tailscale/install/stream
```
Expected: SSE stream with `event: start` then progressive `data:` lines from apt, ending in `event: done`.

**Step 5: Commit**

```bash
git add Orchestrator/onboarding/tailscale_actuator.py Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): /onboarding/tailscale/install/stream SSE endpoint

New actuator module Orchestrator/onboarding/tailscale_actuator.py
houses long-running wizard-driven operations. First endpoint:
SSE stream of Tailscale install progress.

Uses sudo NOPASSWD entry from install.sh Step 4e. Per-operation
asyncio.Lock prevents double-click races (audit C3). Subprocess
argv list — never shell=True (audit C4).

Step 3 of Tailscale Wizard Actuator plan (T3)."
```

---

### Task 4: Backend `/onboarding/tailscale/up` + `/poll` + `/cancel` endpoints

**Files:**
- Modify: `Orchestrator/onboarding/tailscale_actuator.py`
- Modify: `Orchestrator/routes/onboarding_routes.py`

**Step 1: Add `start_up` function**

```python
LOGIN_URL_PATTERN = re.compile(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+")

async def start_up() -> str:
    """Start `tailscale up`, capture login URL, leave process running.
    Returns the login URL. Caller must hold _up_lock until poll completes."""
    global _active_up_process, _active_up_login_url

    # Pre-flight: daemon alive? (audit M6)
    try:
        check = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        # rc != 0 with "not running" is OK (NeedsLogin); rc != 0 with
        # other errors means daemon is dead.
        if check.returncode != 0 and "daemon" in check.stderr.lower():
            raise RuntimeError(
                "tailscaled daemon not running — try `sudo systemctl restart tailscaled`"
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError("tailscaled status check timed out")

    hostname = _safe_hostname()
    cmd = ["sudo", "-n", "/usr/bin/tailscale", "up",
           "--accept-dns=true", f"--hostname={hostname}"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # Wait up to 15s for the Login URL line (audit M6)
    login_url = None
    try:
        async with asyncio.timeout(15):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                m = LOGIN_URL_PATTERN.search(line.decode("utf-8", errors="replace"))
                if m:
                    login_url = m.group(0)
                    break
    except TimeoutError:
        proc.terminate()
        raise RuntimeError("Timeout waiting for Tailscale login URL")

    if not login_url:
        proc.terminate()
        raise RuntimeError("Tailscale did not emit a login URL")

    _active_up_process = proc
    _active_up_login_url = login_url

    # Best-effort: open in default browser (audit I1)
    try:
        subprocess.Popen(["xdg-open", login_url],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass  # UI also renders clickable link

    return login_url


async def poll_up() -> dict:
    """Check whether the active `tailscale up` has authenticated.
    Returns {state: 'running'|'pending'|'failed', detail: ...}"""
    global _active_up_process, _active_up_login_url

    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            backend = data.get("BackendState", "unknown")
            if backend == "Running":
                # Clean up the up-process
                if _active_up_process is not None:
                    try:
                        _active_up_process.wait(timeout=2)
                    except Exception:
                        _active_up_process.terminate()
                    _active_up_process = None
                    _active_up_login_url = None
                return {"state": "running", "detail": data.get("Self", {})}
            return {"state": "pending", "backend_state": backend,
                    "login_url": _active_up_login_url}
    except Exception as e:
        return {"state": "failed", "detail": str(e)}

    return {"state": "pending", "login_url": _active_up_login_url}


async def cancel_up() -> None:
    global _active_up_process, _active_up_login_url
    if _active_up_process is not None:
        _active_up_process.terminate()
        try:
            _active_up_process.wait(timeout=2)
        except Exception:
            _active_up_process.kill()
        _active_up_process = None
    _active_up_login_url = None
```

**Step 2: Add FastAPI routes**

```python
class TailscaleUpResponse(BaseModel):
    login_url: str

@router.post("/tailscale/up", response_model=TailscaleUpResponse)
async def tailscale_up():
    """Start `tailscale up` and return login URL for browser launch."""
    from fastapi import HTTPException
    try:
        if ts_act._up_lock.locked():
            # Already in progress — return existing URL
            if ts_act._active_up_login_url:
                return TailscaleUpResponse(login_url=ts_act._active_up_login_url)
            raise HTTPException(status_code=409, detail="up already in progress")
        await ts_act._up_lock.acquire()
        try:
            url = await ts_act.start_up()
            return TailscaleUpResponse(login_url=url)
        except Exception:
            ts_act._up_lock.release()
            raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tailscale/poll")
async def tailscale_poll():
    """Check authentication progress."""
    result = await ts_act.poll_up()
    if result.get("state") == "running" and ts_act._up_lock.locked():
        ts_act._up_lock.release()
    return result


@router.post("/tailscale/cancel")
async def tailscale_cancel():
    """User aborted auth flow."""
    await ts_act.cancel_up()
    if ts_act._up_lock.locked():
        ts_act._up_lock.release()
    return {"ok": True}
```

**Step 3: Manual SSH test**

Before UI work:
```bash
ssh bbx@192.168.1.171 'sudo tailscale logout'
curl -X POST http://192.168.1.171:9091/onboarding/tailscale/up
# Should return {"login_url": "https://login.tailscale.com/a/..."}
# In another terminal, complete login via that URL
curl http://192.168.1.171:9091/onboarding/tailscale/poll
# Should return state: running after auth completes
```

**Step 4: Commit**

```bash
git add Orchestrator/onboarding/tailscale_actuator.py Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): /onboarding/tailscale/up + /poll + /cancel

Three endpoints implementing the async authentication flow:
- up: starts tailscale up, captures Login URL (15s timeout per M6),
       opens in default browser via xdg-open (best-effort per I1),
       returns URL to UI for visible fallback link.
- poll: checks tailscale status; releases up_lock when Running.
- cancel: terminates active up subprocess + releases lock.

Daemon pre-flight check (M6): fails fast if tailscaled dead.

Step 4 of Tailscale Wizard Actuator plan (T4)."
```

---

### Task 5: Backend `/onboarding/tailscale/cert` + `/accept-dns` endpoints

**Files:**
- Modify: `Orchestrator/onboarding/tailscale_actuator.py`
- Modify: `Orchestrator/routes/onboarding_routes.py`

**Step 1: Add `request_cert` and `set_accept_dns` functions**

```python
HTTPS_DISABLED_PATTERN = re.compile(r"HTTPS is disabled|HTTPS.*not enabled", re.IGNORECASE)

async def request_cert() -> dict:
    """Run `tailscale cert <hostname>`. Returns:
       - {ok: true, cert_path, key_path} on success
       - {ok: false, https_disabled: true} if tailnet HTTPS toggle off
       - {ok: false, error: <msg>} otherwise"""
    # Get current hostname from status (must match what tailscale assigned)
    try:
        status = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        import json as _json
        data = _json.loads(status.stdout)
        hostname = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
        if not hostname:
            return {"ok": False, "error": "no hostname assigned by Tailscale"}
    except Exception as e:
        return {"ok": False, "error": f"status probe failed: {e}"}

    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/tailscale", "cert", hostname,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = (stdout + stderr).decode("utf-8", errors="replace")

    if proc.returncode == 0:
        return {
            "ok": True,
            "cert_path": f"/var/lib/tailscale/certs/{hostname}.crt",
            "key_path": f"/var/lib/tailscale/certs/{hostname}.key",
            "hostname": hostname,
        }
    if HTTPS_DISABLED_PATTERN.search(out):
        return {"ok": False, "https_disabled": True,
                "admin_url": "https://login.tailscale.com/admin/dns"}
    return {"ok": False, "error": out.strip()[:500]}


async def set_accept_dns() -> dict:
    """Run `tailscale set --accept-dns=true`. Idempotent."""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/tailscale", "set", "--accept-dns=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        return {"ok": True}
    return {"ok": False, "error": stderr.decode("utf-8", errors="replace").strip()}
```

**Step 2: Add FastAPI routes**

```python
@router.post("/tailscale/cert")
async def tailscale_cert():
    return await ts_act.request_cert()

@router.post("/tailscale/accept-dns")
async def tailscale_accept_dns():
    return await ts_act.set_accept_dns()
```

**Step 3: Manual SSH test**

```bash
curl -X POST http://192.168.1.171:9091/onboarding/tailscale/accept-dns
# Expect: {"ok": true}
curl -X POST http://192.168.1.171:9091/onboarding/tailscale/cert
# Expect either {"ok": true, "cert_path": ..., "hostname": ...}
# OR if HTTPS off: {"ok": false, "https_disabled": true, "admin_url": ...}
```

**Step 4: Commit**

```bash
git add Orchestrator/onboarding/tailscale_actuator.py Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): /onboarding/tailscale/cert + /accept-dns

cert: requests HTTPS cert from Tailscale. Detects HTTPS-disabled
state (M2) — returns https_disabled:true + admin_url deep-link
instead of raw error. Cert path returned but not yet used by
uvicorn (I2 — deferred to v1.1).

accept-dns: idempotently sets --accept-dns=true on device.
Tailnet-level MagicDNS toggle is separate (cannot be set from
device — M3 deep-link surfaces if MagicDNSSuffix absent).

Step 5 of Tailscale Wizard Actuator plan (T5)."
```

---

### Task 6: UI rewrite — Branch B (install) actuator

**Files:**
- Modify: `Portal/onboarding/steps/tailscale.js` (renderBranchB function ~line 149-180)

**Step 1: Replace copy-paste card with action button + stream container**

```javascript
function renderBranchB(statusEl, result, { back, skip, recheck }) {
    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-error" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">Tailscale not installed</span>
        </div>
        <div class="ob-action-card">
            <p class="ob-action-card-prose">
                Click below to install Tailscale on this device.
                Installation takes about 30 to 90 seconds.
            </p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-install-btn">
                    Install Tailscale Now <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
            </div>
            <div id="ob-install-stream" class="ob-install-stream" hidden>
                <div class="ob-install-stream-header">Installing Tailscale...</div>
                <pre id="ob-install-stream-log" class="ob-install-stream-log"></pre>
            </div>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    document.getElementById("ob-install-btn").addEventListener("click", () => {
        startInstall(statusEl, { back, skip, recheck });
    });
    wireStepNav(statusEl, { back, skip });
}

async function startInstall(statusEl, { back, skip, recheck }) {
    const btn = document.getElementById("ob-install-btn");
    const streamBox = document.getElementById("ob-install-stream");
    const log = document.getElementById("ob-install-stream-log");
    btn.disabled = true;
    btn.textContent = "Installing...";
    streamBox.hidden = false;

    try {
        const resp = await fetch("/onboarding/tailscale/install/stream", { method: "POST" });
        if (resp.status === 409) {
            log.textContent = "Install already in progress — please wait...";
            return;
        }
        if (!resp.ok) {
            log.textContent = `Install failed to start (HTTP ${resp.status})`;
            btn.disabled = false;
            btn.textContent = "Try again";
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let done = false;

        while (!done) {
            const { value, done: isDone } = await reader.read();
            done = isDone;
            if (value) {
                buffer += decoder.decode(value, { stream: true });
                // SSE parse: split on \n\n
                const events = buffer.split("\n\n");
                buffer = events.pop() || "";
                for (const evt of events) {
                    const lines = evt.split("\n");
                    let event = "message", data = "";
                    for (const ln of lines) {
                        if (ln.startsWith("event:")) event = ln.slice(6).trim();
                        else if (ln.startsWith("data:")) data += ln.slice(5).trim() + "\n";
                    }
                    if (event === "done") {
                        log.textContent += "\nInstall complete.\n";
                        // Auto re-check — should transition to Branch C
                        setTimeout(recheck, 1000);
                        return;
                    }
                    if (event === "error") {
                        log.textContent += "\nError: " + data;
                        btn.disabled = false;
                        btn.textContent = "Try again";
                        return;
                    }
                    log.textContent += data;
                    log.scrollTop = log.scrollHeight;
                }
            }
        }
    } catch (e) {
        log.textContent += "\nNetwork error: " + e.message;
        btn.disabled = false;
        btn.textContent = "Try again";
    }
}
```

**Step 2: Add minimal CSS for stream box**

Append to `Portal/onboarding/onboarding.css`:

```css
.ob-install-stream {
    margin-top: 1rem;
    border: 1px solid var(--ob-border-subtle, rgba(255,255,255,0.1));
    border-radius: var(--radius-md, 8px);
    background: rgba(0,0,0,0.4);
    overflow: hidden;
}
.ob-install-stream-header {
    padding: 0.5rem 0.75rem;
    font-size: 0.85em;
    background: rgba(255,255,255,0.04);
    border-bottom: 1px solid rgba(255,255,255,0.06);
}
.ob-install-stream-log {
    margin: 0;
    padding: 0.75rem;
    max-height: 240px;
    overflow-y: auto;
    font-family: ui-monospace, monospace;
    font-size: 0.78em;
    line-height: 1.4;
    white-space: pre-wrap;
    color: rgba(255,255,255,0.85);
}
.ob-action-card-prose {
    margin: 0 0 1rem;
    color: rgba(255,255,255,0.75);
}
```

**Step 3: Manual UI test on MSO5**

After deploying:
1. SSH: `sudo apt-get remove -y --purge tailscale`
2. Hard refresh Tauri wizard (Ctrl+Shift+R)
3. Navigate to Tailscale step
4. Expect Branch B with "Install Tailscale Now" button
5. Click → live stream should fill in over 30-90s
6. After "Install complete" → auto re-check → Branch C appears

**Step 4: Commit**

```bash
git add Portal/onboarding/steps/tailscale.js Portal/onboarding/onboarding.css
git commit -m "feat(onboarding): Branch B install actuator (one-click + live stream)

Replaces copy-paste install command with [Install Tailscale Now]
button that POSTs to /onboarding/tailscale/install/stream and
renders the SSE stream live in a console-style log box. On
'done' event, auto re-checks status to advance to Branch C.

Handles 409 (already in progress) gracefully — UI just shows
'install already in progress'.

Step 6 of Tailscale Wizard Actuator plan (T6)."
```

---

### Task 7: UI rewrite — Branch C (authenticate) actuator

**Files:**
- Modify: `Portal/onboarding/steps/tailscale.js` (renderBranchC function ~line 183-218)

**Step 1: Replace copy-paste with auth flow**

```javascript
function renderBranchC(statusEl, result, { back, skip, recheck }) {
    const errMsg = (result.error || "needs authentication").replace(/^RuntimeError:\s*/, "");
    statusEl.innerHTML = `
        <div class="ob-status-badge" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">
                Tailscale needs authentication
            </span>
            <span class="ob-status-badge-version">${escapeHtml(errMsg)}</span>
        </div>
        <div class="ob-action-card">
            <p class="ob-action-card-prose">
                Click below — we will open your browser to the Tailscale login page.
                After you sign in, this screen will auto-detect within a few seconds.
            </p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-auth-btn">
                    Authenticate Now <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
            </div>
            <div id="ob-auth-status" hidden></div>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    document.getElementById("ob-auth-btn").addEventListener("click", () => {
        startAuth(statusEl, { back, skip, recheck });
    });
    wireStepNav(statusEl, { back, skip });
}

async function startAuth(statusEl, { back, skip, recheck }) {
    const btn = document.getElementById("ob-auth-btn");
    const statusBox = document.getElementById("ob-auth-status");
    btn.disabled = true;
    btn.textContent = "Starting...";
    statusBox.hidden = false;

    let loginUrl;
    try {
        const resp = await fetch("/onboarding/tailscale/up", { method: "POST" });
        const j = await resp.json();
        if (!resp.ok) {
            statusBox.innerHTML = `<p class="ob-auth-err">${escapeHtml(j.detail || "failed")}</p>`;
            btn.disabled = false;
            btn.textContent = "Try again";
            return;
        }
        loginUrl = j.login_url;
    } catch (e) {
        statusBox.innerHTML = `<p class="ob-auth-err">Network error: ${escapeHtml(e.message)}</p>`;
        btn.disabled = false;
        btn.textContent = "Try again";
        return;
    }

    // I1: always render clickable fallback in case xdg-open didn't work
    // (e.g., user accessing wizard from a remote browser, not on device)
    statusBox.innerHTML = `
        <p class="ob-auth-prompt">
            Your browser should open automatically. If not:
        </p>
        <p class="ob-auth-link-row">
            <a href="${escapeHtml(loginUrl)}" target="_blank" rel="noopener" class="ob-auth-link">
                Open Tailscale login &rarr;
            </a>
        </p>
        <p class="ob-auth-waiting">Waiting for authentication...</p>
    `;
    btn.textContent = "Waiting...";

    // Poll every 2s. Audit I5: 5 min total, "still waiting?" hint at 3 min.
    const startedAt = Date.now();
    const TIMEOUT_MS = 5 * 60 * 1000;
    const HINT_MS = 3 * 60 * 1000;
    let hintShown = false;
    const pollOnce = async () => {
        const elapsed = Date.now() - startedAt;
        if (elapsed > TIMEOUT_MS) {
            statusBox.innerHTML += `<p class="ob-auth-err">Timed out waiting for authentication.</p>`;
            await fetch("/onboarding/tailscale/cancel", { method: "POST" });
            btn.disabled = false;
            btn.textContent = "Try again";
            return;
        }
        if (elapsed > HINT_MS && !hintShown) {
            hintShown = true;
            statusBox.insertAdjacentHTML("beforeend", `
                <p class="ob-auth-hint">
                    Still waiting? Make sure you completed the login in your browser.
                    <a href="${escapeHtml(loginUrl)}" target="_blank" rel="noopener">
                        Re-open login URL
                    </a>
                </p>
            `);
        }
        try {
            const r = await fetch("/onboarding/tailscale/poll");
            const j = await r.json();
            if (j.state === "running") {
                statusBox.innerHTML = `<p class="ob-auth-ok">Authenticated. Loading...</p>`;
                setTimeout(recheck, 800);
                return;
            }
        } catch (_) { /* transient */ }
        setTimeout(pollOnce, 2000);
    };
    setTimeout(pollOnce, 2000);
}
```

**Step 2: Add CSS for auth-status sub-elements**

Append to `Portal/onboarding/onboarding.css`:

```css
.ob-auth-prompt { margin: 1rem 0 0.5rem; color: rgba(255,255,255,0.85); }
.ob-auth-link-row { margin: 0.5rem 0 1rem; }
.ob-auth-link {
    display: inline-block;
    padding: 0.5rem 1rem;
    background: var(--accent, #4a9eff);
    color: white;
    border-radius: var(--radius-md, 8px);
    text-decoration: none;
    font-weight: 500;
}
.ob-auth-waiting { color: rgba(255,255,255,0.6); font-style: italic; }
.ob-auth-err { color: #ff7878; }
.ob-auth-ok { color: #78ff9b; }
```

**Step 3: Manual UI test on MSO5**

After deploying:
1. SSH: `sudo tailscale logout`
2. Hard refresh Tauri wizard
3. Navigate to Tailscale step → expect Branch C with [Authenticate Now]
4. Click → default browser should open to login URL
5. Status box shows fallback link + "Waiting..."
6. Complete login in browser
7. Within 4s, status updates to "Authenticated" → Branch A renders

**Step 4: Commit**

```bash
git add Portal/onboarding/steps/tailscale.js Portal/onboarding/onboarding.css
git commit -m "feat(onboarding): Branch C auth actuator (button + xdg-open + poll)

Replaces copy-paste 'sudo tailscale up' with [Authenticate Now]
button. Backend launches xdg-open to default browser AND returns
login URL — UI always renders clickable fallback link so remote-
browser access works too (audit I1).

Polls /onboarding/tailscale/poll every 2s for up to 4 minutes.
On success, transitions to Branch A. On timeout, cancel + re-arm
button.

Step 7 of Tailscale Wizard Actuator plan (T7)."
```

---

### Task 8: Branch A post-auth automation — cert + MagicDNS detection

**Files:**
- Modify: `Portal/onboarding/steps/tailscale.js` (renderBranchA function ~line 95-146)

**Step 1: Extend renderBranchA to call cert + accept-dns**

After the existing `await fetch("/onboarding/save", ...)` block, before rendering the success view, add:

```javascript
async function renderBranchA(statusEl, result, { next, back, skip }) {
    const hostname = (result.detail && result.detail.hostname) || "unknown";
    const ip = (result.detail && result.detail.ip) || "unknown";
    const magicdnsEnabled = !!(result.detail && result.detail.magicdns_enabled);

    // Persist hostname to .env (unchanged)
    try {
        await fetch("/onboarding/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ secrets: { BLACKBOX_TAILNET_HOSTNAME: hostname } }),
        });
    } catch (e) {
        console.warn("Couldn't persist BLACKBOX_TAILNET_HOSTNAME:", e);
    }

    // N4: Guard against re-firing on Branch A re-render (page nav back-forth)
    if (!window.__ob_tailscale_cert_attempted) {
        window.__ob_tailscale_cert_attempted = true;
        // Fire-and-forget: set --accept-dns=true (device-side, idempotent)
        fetch("/onboarding/tailscale/accept-dns", { method: "POST" }).catch(() => {});
    }

    // M7: Cert flow — render PENDING banner first, swap on resolve.
    // Promise + 60s client-side timeout (cert can be slow on first ACME run).
    let certPromise = null;
    if (!window.__ob_tailscale_cert_done) {
        const certTimeout = new Promise(r => setTimeout(() => r({
            ok: false, error: "timeout", timed_out: true,
        }), 60_000));
        const certFetch = fetch("/onboarding/tailscale/cert", { method: "POST" })
            .then(r => r.json())
            .catch(() => ({ ok: false, error: "network" }));
        certPromise = Promise.race([certFetch, certTimeout]).then(result => {
            window.__ob_tailscale_cert_done = true;
            return result;
        });
    }

    const portalUrl = `https://${hostname}`;
    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-success" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">&check;</span>
            <span class="ob-status-badge-label">
                Tailscale online &mdash; <strong>${escapeHtml(hostname)}</strong>
            </span>
            <span class="ob-status-badge-version">${escapeHtml(ip)}</span>
        </div>
        <div class="ob-tailnet-url-card">
            <div class="ob-tailnet-url-label">Your Portal URL</div>
            <div class="ob-tailnet-url-row">
                <code class="ob-tailnet-url-code">${escapeHtml(portalUrl)}</code>
                <button type="button" class="ob-copy-btn" data-copy="${escapeHtml(portalUrl)}">
                    <span aria-hidden="true">&#9112;</span> Copy
                </button>
            </div>
            <p class="ob-tailnet-url-hint">
                Open this in any browser on a phone, laptop, or other device on
                your tailnet &mdash; pair the Android app, or just use desktop.
            </p>
        </div>
        <div id="ob-tailscale-banners"></div>
        <div class="ob-cta-row">
            <button type="button" class="ob-cta" id="ob-tailscale-continue">
                Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
            </button>
        </div>
        ${renderDisclosure(false)}
        ${renderStepNav({ showSkip: false })}
    `;
    wireCopyBtn(statusEl);
    document.getElementById("ob-tailscale-continue").addEventListener("click", next);
    wireStepNav(statusEl, { back, skip });

    // Render banners as cert + magicdns results land
    const banners = document.getElementById("ob-tailscale-banners");
    if (!magicdnsEnabled) {
        banners.insertAdjacentHTML("beforeend", magicdnsBanner());
        wireRecheckBtn(banners);
    }

    // Cert pending banner while ACME flow runs (M7)
    if (certPromise) {
        const pendingId = "ob-cert-pending";
        banners.insertAdjacentHTML("beforeend", certPendingBanner(pendingId));
        const result = await certPromise;
        const pendingEl = document.getElementById(pendingId);
        if (pendingEl) pendingEl.remove();
        if (result.ok) {
            banners.insertAdjacentHTML("beforeend", certInfoBanner());
        } else if (result.https_disabled) {
            banners.insertAdjacentHTML("beforeend",
                httpsDisabledBanner(result.admin_url));
            wireRecheckBtn(banners);
        } else if (result.timed_out) {
            banners.insertAdjacentHTML("beforeend",
                certTimeoutBanner());
            wireRecheckBtn(banners);
        }
        // Other errors are silent — cert is non-fatal for v1
    }
}

// I4: MagicDNS banner — Android-app-gate framing + step-by-step.
// Customers see "10 seconds" and "Android app won't pair without this" and ACT.
function magicdnsBanner() {
    return `
        <div class="ob-banner ob-banner-warn">
            <strong>Enable MagicDNS to use the Android app</strong>
            <p>
                Your Android app reaches the BlackBox at a friendly hostname like
                <code>blackbox.<em>your-tailnet</em>.ts.net</code> &mdash; that name
                only resolves when MagicDNS is on for your tailnet. Without it,
                pairing won't work.
            </p>
            <p><strong>Takes about 10 seconds:</strong></p>
            <ol class="ob-banner-steps">
                <li>Click the <strong>Open admin console</strong> button below
                    (opens in your browser).</li>
                <li>Find the <strong>MagicDNS</strong> section near the top of
                    the page.</li>
                <li>Click the toggle to turn it <strong>On</strong>.</li>
                <li>Come back here and click <strong>Re-check</strong>.</li>
            </ol>
            <div class="ob-banner-actions">
                <a href="https://login.tailscale.com/admin/dns" target="_blank" rel="noopener"
                   class="ob-banner-link ob-banner-link-primary">Open admin console &rarr;</a>
                <button type="button" class="ob-banner-link ob-banner-recheck"
                        data-recheck="magicdns">Re-check &#x21bb;</button>
            </div>
        </div>
    `;
}

// I4: Same treatment for HTTPS toggle. Less urgent for v1 (Portal is HTTP-only
// until v1.1) but worth flipping since cert obtain works automatically once on.
function httpsDisabledBanner(adminUrl) {
    return `
        <div class="ob-banner ob-banner-warn">
            <strong>Enable HTTPS certs for your tailnet</strong>
            <p>
                Tailscale can issue an HTTPS certificate for your BlackBox &mdash;
                useful once your Portal supports it (v1.1+). The toggle is on
                the same admin page as MagicDNS.
            </p>
            <p><strong>Takes about 10 seconds:</strong></p>
            <ol class="ob-banner-steps">
                <li>Click the <strong>Open admin console</strong> button.</li>
                <li>Find the <strong>HTTPS Certificates</strong> section.</li>
                <li>Click <strong>Enable HTTPS</strong> and confirm.</li>
                <li>Come back here and click <strong>Re-check</strong>.</li>
            </ol>
            <div class="ob-banner-actions">
                <a href="${escapeHtml(adminUrl)}" target="_blank" rel="noopener"
                   class="ob-banner-link ob-banner-link-primary">Open admin console &rarr;</a>
                <button type="button" class="ob-banner-link ob-banner-recheck"
                        data-recheck="cert">Re-check &#x21bb;</button>
            </div>
        </div>
    `;
}

function certPendingBanner(id) {
    return `
        <div class="ob-banner ob-banner-info" id="${escapeHtml(id)}">
            <span class="ob-banner-spinner" aria-hidden="true">&#9696;</span>
            Requesting HTTPS certificate from Let's Encrypt&hellip;
        </div>
    `;
}

function certInfoBanner() {
    return `
        <div class="ob-banner ob-banner-info">
            HTTPS certificate obtained &mdash; ready for full HTTPS Portal in v1.1.
        </div>
    `;
}

function certTimeoutBanner() {
    return `
        <div class="ob-banner ob-banner-warn">
            <strong>HTTPS cert request took too long.</strong>
            <p>This usually clears up on its own. Click Re-check to try again.</p>
            <div class="ob-banner-actions">
                <button type="button" class="ob-banner-link ob-banner-recheck"
                        data-recheck="cert">Re-check &#x21bb;</button>
            </div>
        </div>
    `;
}

// Re-check wiring: each "Re-check" button re-runs the appropriate probe and
// re-renders Branch A so banners refresh based on new state.
function wireRecheckBtn(scope) {
    scope.querySelectorAll(".ob-banner-recheck").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.preventDefault();
            const which = btn.dataset.recheck;
            btn.disabled = true;
            btn.textContent = "Checking...";
            // Reset cert-attempted guard so it can re-fire
            if (which === "cert") window.__ob_tailscale_cert_done = false;
            // Force full step re-render via top-level render
            const container = scope.closest(".ob-step")?.parentElement || document.body;
            await render(container, {
                next: () => {}, back: () => {}, skip: () => {},
            });
        });
    });
}
```

**Step 2: Add banner CSS**

```css
.ob-banner {
    margin-top: 0.75rem;
    padding: 0.75rem 1rem;
    border-radius: var(--radius-md, 8px);
    font-size: 0.9em;
}
.ob-banner-warn {
    background: rgba(255, 180, 50, 0.08);
    border-left: 3px solid rgba(255, 180, 50, 0.6);
}
.ob-banner-info {
    background: rgba(120, 200, 255, 0.06);
    border-left: 3px solid rgba(120, 200, 255, 0.4);
    color: rgba(255,255,255,0.75);
}
.ob-banner strong { display: block; margin-bottom: 0.25rem; }
.ob-banner p { margin: 0.25rem 0; color: rgba(255,255,255,0.7); }
.ob-banner-link {
    display: inline-block;
    margin-top: 0.5rem;
    color: var(--accent, #4a9eff);
    text-decoration: none;
    font-weight: 500;
}
.ob-banner-link:hover { text-decoration: underline; }

/* I4: rich-instruction banner additions (numbered steps, action row, code chips) */
.ob-banner-steps {
    margin: 0.25rem 0 0.75rem 1.25rem;
    padding: 0;
    color: rgba(255,255,255,0.8);
}
.ob-banner-steps li {
    margin-bottom: 0.35rem;
    line-height: 1.4;
}
.ob-banner-actions {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    margin-top: 0.5rem;
    flex-wrap: wrap;
}
.ob-banner-link-primary {
    background: var(--accent, #4a9eff);
    color: white;
    padding: 0.4rem 0.85rem;
    border-radius: var(--radius-sm, 6px);
    margin-top: 0;
}
.ob-banner-link-primary:hover { text-decoration: none; opacity: 0.92; }
.ob-banner-recheck {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.2);
    color: rgba(255,255,255,0.85);
    padding: 0.4rem 0.85rem;
    border-radius: var(--radius-sm, 6px);
    cursor: pointer;
    font-size: inherit;
    font-family: inherit;
}
.ob-banner-recheck:hover { background: rgba(255,255,255,0.06); }
.ob-banner-recheck:disabled { opacity: 0.5; cursor: default; }
.ob-banner code {
    background: rgba(0,0,0,0.3);
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.92em;
}
.ob-banner-spinner {
    display: inline-block;
    margin-right: 0.4rem;
    animation: ob-spin 1.2s linear infinite;
}
@keyframes ob-spin { to { transform: rotate(360deg); } }

/* I5: auth-hint shown after 3 min of waiting */
.ob-auth-hint {
    margin-top: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: rgba(255, 180, 50, 0.06);
    border-left: 3px solid rgba(255, 180, 50, 0.5);
    border-radius: var(--radius-sm, 6px);
    color: rgba(255,255,255,0.8);
    font-size: 0.9em;
}
.ob-auth-hint a {
    color: var(--accent, #4a9eff);
    margin-left: 0.25rem;
}
```

**Step 3: Manual UI test on MSO5 (covers M3 + M4)**

Pre-condition: Tailscale authenticated.
1. In admin console, turn OFF MagicDNS
2. SSH: `sudo tailscale logout && sudo tailscale up`
3. Hard refresh wizard
4. Expect Branch A success WITH MagicDNS warn banner + admin link
5. Re-enable MagicDNS in admin console, refresh wizard → banner gone

Same drill for HTTPS toggle.

**Step 4: Commit**

```bash
git add Portal/onboarding/steps/tailscale.js Portal/onboarding/onboarding.css
git commit -m "feat(onboarding): Branch A post-auth cert + MagicDNS automation

After authenticating, Branch A automatically:
- Sets --accept-dns=true (fire-and-forget, idempotent)
- Requests HTTPS cert via /onboarding/tailscale/cert
- Detects MagicDNS state from validator response

Banner content rewritten per audit I4: MagicDNS gates the Android
app pairing flow (phone needs hostname resolution via tailnet
DNS). Banners include explicit step-by-step admin-console
instructions framed around consequences (\"Android app won't
pair\") + 10-second timing expectation + Re-check button for
post-toggle verification.

Hardenings:
- M7: cert pending banner with spinner while Let's Encrypt round-
      trips; 60s client-side timeout with re-check fallback
- I5: auth poll bumped 4→5 min + 'still waiting?' hint at 3 min
- N4: window-scope guard prevents re-firing cert on Branch A re-render

Step 8 of Tailscale Wizard Actuator plan (T8)."
```

---

### Task 9: End-to-end validation on MSO5

Run all manual tests M1-M8 from the Test plan section above. Document each result in a session notebook (or in the commit message of the final close-out commit). If any test reveals a bug not caught in audit, add it to a new audit table row with severity, fold a fix into a small follow-up commit, then re-run that specific test.

**Step 1: Run test matrix sequentially**

For each of M1-M8:
- Document pre-condition
- Document expected result
- Document actual result
- Pass / Fail / Workaround

**Step 2: Mint closeout snapshot**

If all pass (or all pass with minor noted workarounds): invoke `/snapshot-dev` for operator `Brandon`. Snapshot should capture:
- The wizard actuator feature is live
- Audit table from this plan + any empirical additions from M1-M8
- Acceptance criteria status
- v1.1 polish items pending (uvicorn HTTPS adoption, custom select component)

---

## Risks + open questions for Brandon's review

1. **xdg-open on systemd-launched FastAPI may fail to reach the user session DBUS**. Mitigation via fallback clickable link (I1). Realistic worst-case: customer at MSO5 console clicks Authenticate, no browser opens automatically, but clear fallback link works. Acceptable degraded path.

2. **Tailscale's install.sh script could change format / output significantly in a future update**, breaking our SSE parsing. Mitigation: we don't parse the install output — just pipe stdout to UI. Only failure mode is exit code, which we capture cleanly.

3. **Sudoers grant for `/bin/sh -c "curl ... | sh"` is the highest-trust entry in the file** — it's a literal "fetch and execute remote code as root." Brandon's accepted this since the alternative is asking the customer to do exactly the same thing manually. Documented in the sudoers file comment.

4. **Polling interval (2s) + 4-minute timeout** may need tuning. Some users take longer to log in (2FA flows, password recovery). If real testing shows 4 min is tight, bump to 6 min — change is one constant in startAuth.

5. **State persistence across wizard sessions** — if user closes wizard mid-auth, the next session calls `/poll` which returns "no active up". Should `up` be resumable? Probably not — easier to just re-click [Authenticate Now] which idempotently starts a new flow (Tailscale will issue a new URL or return the existing pending one).

---

## Execution choice (per writing-plans skill protocol)

Plan complete and saved to `docs/plans/2026-05-13-tailscale-wizard-actuator.md`.

**Two execution options:**

1. **Subagent-Driven (this session)** — Dispatch fresh subagent per task (T1 through T9), code-review between tasks, fast iteration. Best for tight loop with current context still warm.

2. **Parallel Session (separate)** — Open new session with executing-plans skill, batch execution with checkpoint commits. Best if Brandon wants to fresh-reinstall test the existing Track 4 stack FIRST (today's planned next action) before adding more wizard surface area.

**Recommendation:** Option 2. Brandon stated he's about to do a fresh MSO5 reinstall to validate the 7 hardware-test fixes from this morning. That validation is the v1 acceptance bar for Track 4. Adding this Tailscale actuator on top would mix two test surfaces and make regression diagnosis harder. Better sequence:
- (a) Brandon completes fresh-reinstall test → confirms Track 4 is stable end-to-end
- (b) New session picks up this plan via executing-plans
- (c) Implement T1-T8, hardware test T9, mint snapshot

Brandon's call.
