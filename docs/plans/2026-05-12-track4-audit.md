# Track 4 (install.sh + factory image) — Pre-Execution Audit

**Date:** 2026-05-12
**Author:** Controller (audit-then-execute, post-Track-3-completion)
**Plan reviewed:** `docs/plans/2026-05-10-onboarding-flow.md` Phase 4.1 + Phase 4.2 (lines 3438–3583)
**Status:** Audit complete — open questions for Brandon before plan-edit phase.

---

## Why this audit exists

The audit-then-execute pattern was validated twice:

- **Phase 2.10** (~30 min implementation) — audit caught 2 real bugs.
- **Track 3** (1–2 week implementation) — audit caught 3 critical + 5 major + 3 empirical corrections folded back during execution.

Track 4 is also a 1–2 week build, and it touches **system-level state on a customer's machine** (apt, systemd, /usr/bin, ~/.config/autostart) — a class of bug that is much harder to recover from than a Rust compile error. The audit ROI is therefore at least as high as Track 3, and likely higher.

---

## Environment probe — reality check 2026-05-12

| Probe | Result | Notes |
|---|---|---|
| Distro | Ubuntu 24.04.4 LTS (Noble Numbat) ✓ | Plan target matches |
| Python | 3.12.3 at `/usr/bin/python3` ✓ | Plan uses `python3.12` explicitly |
| `Orchestrator/utils/paths.py` (Track 0) | EXISTS ✓ | `BLACKBOX_ROOT` resolution working |
| `Scripts/onboarding/system-packages.txt` | EXISTS, well-bucketed ✓ | MUST_HAVE / SHOULD_HAVE / FEATURE_OPTIONAL / HARDWARE_OPTIONAL / DEV_ONLY |
| `requirements.txt` | EXISTS, 83 lines ✓ | Wizard already validated |
| Tauri `.deb` artifact | EXISTS at `installer/src-tauri/target/release/bundle/deb/BlackBox Setup_0.1.0_amd64.deb` (5.1 MB) ✓ | **Path differs from plan; literal SPACE in filename** |
| Tauri `.AppImage` | EXISTS, same dir, 79 MB ✓ | Same path/space concern |
| `installer/dist/blackbox-setup-autostart.desktop` | EXISTS ✓ | Plan refers to this path correctly |
| `installer/dist/blackbox-setup.desktop` | EXISTS ✓ | Plan refers to this path correctly |
| Existing `/etc/systemd/system/blackbox.service` | RUNNING (`active`) | Hardcoded dev path + 24G memory cap (dev-box assumption) |
| Legacy `setup.sh` (root) | EXISTS (Nov 16 2025, 4.2 KB) | Plan says `git rm` |
| Legacy `install_enhanced_service.sh` (root) | EXISTS (Nov 22 2025) | **NOT referenced in plan — gets orphaned** |
| Legacy `blackbox-enhanced.service` template | EXISTS | Used by `install_enhanced_service.sh` |
| Legacy `cleanup_crash.sh` | EXISTS | Used by enhanced unit's `ExecStopPost` |
| `/opt/blackbox` | DOES NOT EXIST | Customer install location undecided |
| `.env.template` | EXISTS but only 3 keys (Open AI / Anthropic / Google) | Wizard configures many more — see M4 |
| `.gitignore` coverage of `.onboarding_*` + `.env.backup.*` | NONE FOUND | Currently shows as untracked in repo — N3 |

---

## CRITICAL findings (block customer install on day 1)

### C1: `apt install` pipeline is structurally broken

**Plan lines 3468–3471:**

```bash
xargs -a "$BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt" -d '\n' \
    grep -vE '^#|^$' \
    | grep '# MUST_HAVE' | awk '{print $1}' \
    | sudo xargs apt install -y
```

**Problem:** `xargs -a FILE COMMAND` reads lines from FILE and passes each as **arguments** to COMMAND, not stdin. So this invokes `grep -vE '^#|^$' <every-non-blank-line-from-file>` — `grep` then treats every package name as a regex pattern to search for in the next arg. The result is garbage. The `sudo xargs apt install -y` at the tail never receives clean package names.

**Fix:**

```bash
grep -E '^[a-zA-Z0-9._+-]+\s+#\s+MUST_HAVE' \
    "$BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt" \
  | awk '{print $1}' \
  | xargs sudo apt install -y
```

**Severity:** CRITICAL — install fails at Step 1 with confusing errors. No customer reaches Step 2.

---

### C2: `.deb` source path/filename wrong AND the `.deb` isn't in the repo

**Plan lines 3510–3513:**

```bash
if [[ -f "$BLACKBOX_ROOT/installer/dist/blackbox-setup.deb" ]]; then
    echo "[install] Installing BlackBox Setup app..."
    sudo dpkg -i "$BLACKBOX_ROOT/installer/dist/blackbox-setup.deb" || sudo apt install -fy
fi
```

**Problem (compound):**

1. **Wrong path.** Reality is `installer/src-tauri/target/release/bundle/deb/BlackBox Setup_0.1.0_amd64.deb` (note literal SPACE in filename, version-pinned, under `target/release/`).
2. **Wrong filename.** Tauri-slugified `productName: "BlackBox Setup"` produced `BlackBox Setup_0.1.0_amd64.deb`, NOT `blackbox-setup.deb`.
3. **Wrong dpkg name assumption.** Once installed, dpkg lists it as `black-box-setup` (per Track 3 audit closeout). Plan never uses dpkg name, but documentation downstream will.
4. **Bundle is gitignored.** `target/` is gitignored by Cargo convention; Track 3 explicitly noted "bundle artifacts (gitignored, but documented for Track 4)." So `git clone && ./Scripts/install.sh` produces a customer machine with NO .deb available. The `if [[ -f ... ]]` guard would silently skip the entire Tauri app install — customer reaches a working Orchestrator with no setup wizard.

**Fix (open question — see Q1 below):** Three valid models, each requires different install.sh shape.

- **Option A — Build from source on customer machine:** install.sh apt-installs Tauri build deps (`libwebkit2gtk-4.1-dev`, `librsvg2-dev`, `libsoup-3.0-dev`, `cargo`, Rust toolchain), then runs `cargo tauri build` inside `installer/`. Adds ~15–30 min install time + ~2 GB disk, but no GitHub asset hosting needed.
- **Option B — Download from GitHub release asset:** install.sh `curl`s the .deb from `https://github.com/TechBran/blackbox-poc/releases/download/v0.1.0/...`. Requires (a) GitHub release workflow producing the asset, (b) repo public OR token-auth in install.sh.
- **Option C — Factory image (Track 4.2):** `.deb` pre-baked into the OS image; install.sh becomes a no-op for that step. Doesn't work for "git clone install" path; only for hardware-product path.

**Severity:** CRITICAL — without resolution, Tauri setup wizard never reaches the customer's screen.

---

### C3: Final user-facing message points at wrong binary path

**Plan line 3527:**

```
echo "[install] Done. Reboot to launch BlackBox Setup, or run /usr/local/bin/blackbox-setup now."
```

**Problem:** Tauri `.deb` installs binary at `/usr/bin/blackbox-setup` (verified in Track 3), not `/usr/local/bin/`. Customer follows this instruction and gets `command not found`.

**Fix:** `/usr/local/bin/blackbox-setup` → `/usr/bin/blackbox-setup`

**Severity:** CRITICAL for UX. Trivial typo-class fix but customer-visible.

---

## MAJOR findings (architectural correctness)

### M1: Customer install location strategy undefined

**Plan lines 3461–3463:**

```bash
BLACKBOX_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
```

**Problem:** Wherever the customer ran `./Scripts/install.sh` becomes the permanent BLACKBOX_ROOT. Realistic customer scenarios:

- `cd ~/Downloads/blackbox-poc && ./Scripts/install.sh` → BlackBox lives in `~/Downloads/` (gets cleaned up on next "free disk space" sweep)
- `cd /tmp/blackbox-poc && ./Scripts/install.sh` → BlackBox lives in `/tmp/` (purged on reboot)
- `cd ~/Desktop/blackbox-poc && ./Scripts/install.sh` → BlackBox lives on Desktop (visual clutter)

Snapshot SNAP-20260512-6605 explicitly noted: "Track 4 must move to /opt/blackbox/ or similar for customer install."

**Fix (open question — see Q2):** Pick a canonical install location strategy.

- **Option A — `~/blackbox-poc`** (current dev-box pattern): simplest, no sudo for code dirs, `User=$USER` in systemd. But fragile if user moves dirs.
- **Option B — `/opt/blackbox`** (FHS-standard): respects Linux conventions, survives user account changes, but needs `sudo cp -r` of repo + a system service user.
- **Option C — Detect-and-offer:** If install.sh detects BLACKBOX_ROOT under a "transient" path (Downloads/Desktop/tmp/...), prompt to relocate to `~/blackbox` or `/opt/blackbox`.

**Severity:** MAJOR — the wrong choice creates support tickets six months later.

---

### M2: New systemd unit drops resource limits + security hardening

**Existing production unit (`/etc/systemd/system/blackbox.service`):**

- `MemoryMax=24G` (assumes 30 GB dev box — wrong for customer mini-PCs likely 8–16 GB)
- `CPUWeight=200` + `Nice=-5` (favorable scheduling)
- `NoNewPrivileges=true` + `PrivateTmp=true` (security)
- `--timeout-keep-alive 120 --limit-max-requests 10000 --loop uvloop` (uvicorn tuning)

**Plan's new unit (lines 3488–3505):**

- No memory cap
- No CPU prioritization
- No security hardening
- No uvicorn flags — falls back to defaults (no uvloop, no keep-alive bump)

**Fix:** Carry forward security hardening + uvicorn flags. Drop the 24G memory cap (or compute dynamically: `MemoryMax=$(awk '/MemTotal/{printf "%dG\n", $2*0.7/1048576}' /proc/meminfo)`). See Q4 for memory-cap decision.

Recommended inserted block:

```ini
# Security hardening
NoNewPrivileges=true
PrivateTmp=true

# Resource limits — ~70% of system RAM (computed at install time)
MemoryMax=__MEMORY_MAX__

# Uvicorn tuning
ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app \
    --host 0.0.0.0 --port 9091 \
    --timeout-keep-alive 120 --limit-max-requests 10000 --loop uvloop
```

**Severity:** MAJOR — security regression on customer machines vs current dev box.

---

### M3: Existing `install_enhanced_service.sh` infrastructure ignored

The repo currently has a parallel install path with **production-grade self-healing features that the new plan drops:**

- **Watchdog timer:** `blackbox-watchdog.timer` polls `/watchdog` every 2 min, restarts on failure (uses `Type=notify` + `WatchdogSec=120` — service must heartbeat).
- **Log rotation:** `/etc/logrotate.d/blackbox` — daily rotation, 7-day retention, compress.
- **Crash cleanup:** `cleanup_crash.sh` runs via `ExecStopPost` if exit code ≠ success.
- **Override.conf scaffolding:** `/etc/systemd/system/blackbox.service.d/override.conf` — customer-customizable port, memory, CPU without modifying main unit.
- **Helper scripts:** `blackbox-status.sh` (formatted status + dashboard) and `blackbox-cleanup.sh` (manual archive sweep).

Plan's Track 4 rewrites install.sh from scratch with NONE of this. Either (a) we knowingly deprecate these features, or (b) Track 4 carries them forward.

**Fix (open question — see Q3):** Decide which features to preserve.

**Recommendation:** Preserve `NoNewPrivileges` + `PrivateTmp` + `MemoryMax` + `EnvironmentFile=$BLACKBOX_ROOT/.env` + `Type=notify` + `WatchdogSec` if `/watchdog` endpoint exists (verify first). Drop the IO bandwidth limits (`IOReadBandwidthMax=/dev/sda 100M` — assumes `/dev/sda` device naming, breaks on NVMe). Drop the watchdog .timer (systemd's own `WatchdogSec` is sufficient).

**Severity:** MAJOR — losing watchdog + log rotation degrades operational posture for customer.

---

### M4: `.env.template` is grossly incomplete

**Reality:** template has 3 keys (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY`) plus a recently-DELETED `BLACKBOX_ROOT` block (uncommitted change in `git diff`).

**Wizard configures more:**

- `BLACKBOX_TAILNET_HOSTNAME` (Tailscale step)
- `DEFAULT_OPERATOR` (operator step)
- `XAI_API_KEY`, `PERPLEXITY_API_KEY` (optional providers per Tier-2, deferred to v1.1 but env shape should exist)
- `GMAIL_*` OAuth credentials (Gmail integration)
- `BLACKBOX_ROOT` (Track 0 paths utility — was added then removed?!)
- TG200 cellular provisioning vars
- Drachtio / FreeSWITCH passwords (deferred per master plan but env keys exist)

**Fix:**
1. **Restore `BLACKBOX_ROOT=` commented block** that's currently being deleted in pending uncommitted change.
2. **Audit Track 4 must require `.env.template` to be a canonical inventory** of all wizard-managed env vars (commented placeholders + descriptions + provider URLs).
3. New install.sh Step 3 (`cp .env.template .env`) only works correctly if template is complete.

**Severity:** MAJOR — install completes but customer's .env is missing keys the wizard expects to exist.

---

### M5: Re-running install.sh doesn't restart the service

**Plan Step 4 (lines 3506–3507):**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now blackbox.service
```

**Problem:** `--now` is a no-op if the service is already running. So a customer who re-runs install.sh after upgrading the repo gets the new systemd unit content on disk but the **service keeps running with the old in-memory config**. Confusing failure mode: changes don't take effect until manual restart.

**Fix:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable blackbox.service
sudo systemctl restart blackbox.service  # idempotent — works whether running or stopped
```

**Severity:** MAJOR — silent upgrade failure mode. Easy fix.

---

### M6: install.sh doesn't handle being run via sudo

**Plan Step 6a/6b (lines 3515–3525):**

```bash
mkdir -p "$HOME/.config/autostart"
cp ... "$HOME/.config/autostart/blackbox-setup.desktop"
```

**Problem:** Customer who runs `sudo ./Scripts/install.sh` gets:
- `$USER` = `root`
- `$HOME` = `/root`
- Autostart .desktop lands in `/root/.config/autostart/` — NEVER triggers user's GNOME session
- Persistent .desktop lands in `/root/.local/share/applications/` — invisible to customer
- systemd unit gets `User=root` (line 3496 `User=$USER`) — runs the FastAPI app as root (security regression vs current setup which uses `User=ai-black-box-fc`)

**Fix:** Detect sudo invocation and re-anchor:

```bash
# Detect sudo and resolve real user/home
if [[ $EUID -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" ]]; then
        echo "[install] ERROR: do not run as direct root. Run as your user (sudo will be invoked as needed)."
        exit 1
    fi
    REAL_USER="$SUDO_USER"
    REAL_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    REAL_USER="$USER"
    REAL_HOME="$HOME"
fi
```

Then use `$REAL_USER` and `$REAL_HOME` everywhere `$USER` and `$HOME` appear.

**Severity:** MAJOR — wrong invocation pattern silently breaks autostart.

---

## MINOR findings

### N1: Phase 4.1 dependency list incomplete

**Plan line 3443:** `Dependencies: Track 0 (paths), Track 3 (Tauri binary to install)`

**Reality:** Also depends on:
- `requirements.txt` being current (it is, May 10)
- `Scripts/onboarding/system-packages.txt` being current (it is, May 10)
- `installer/dist/*.desktop` files (added by Track 3 T3.4 — present ✓)

Documentation nit only.

---

### N2: Test step assumes public repo access

**Plan lines 3535–3540:**

```bash
git clone https://github.com/TechBran/blackbox-poc.git
cd blackbox-poc
./Scripts/install.sh
```

**Problem:** `github.com/TechBran/blackbox-poc` is a private repo. Customer has no auth.

**Fix:** Document the expected install path:
- Private clone via SSH key (sophisticated customer)
- HTTPS with personal access token
- GitHub release tarball download (no clone needed)
- Pre-installed factory image (Track 4.2)
- Or repo goes public for v1 launch

Tied to the C2 .deb-distribution decision (Q1).

---

### N3: `.gitignore` doesn't cover onboarding state files

**Reality:** repo root currently has untracked `.onboarding_complete` + `.onboarding_state.json` + `.env.backup.*` — none of these are in `.gitignore`. A customer who runs Track 4's install AND completes onboarding will see these as "untracked" in `git status`. Risk: accidental commit pushes their state to the repo.

**Fix:** Add to `.gitignore`:

```
# Onboarding state (per-install, never commit)
.onboarding_state.json
.onboarding_complete
.env.backup.*
```

This belongs in install.sh's prep step OR as a separate `chore(gitignore)` commit before Track 4 ships.

**Severity:** MINOR but worth fixing now while we remember.

---

### N4: install.sh runs `apt update` unconditionally

Every re-run of install.sh hits apt mirrors. On a flaky network this can stall. Could check `find /var/lib/apt/periodic/update-success-stamp -mmin -60` and skip if updated within last hour.

**Severity:** MINOR — UX nice-to-have, not blocker.

---

### N5: Plan's Step 2 (test in Ubuntu VM) is verification, not automation

Test step (3531–3540) belongs in the verification section, not as part of Task 4.1.1's implementer steps. (Implementer can't ship a VM test in the same task.) Either:
- Keep as "verification recipe" labeled clearly OUT-OF-SCOPE for implementer
- Move to a separate Task 4.1.2 ("VM smoke test") owned by Brandon, not implementer

---

### N6: Legacy `setup.sh` + `install_enhanced_service.sh` cleanup is unspecified

Plan only deletes `setup.sh` (line 3546). It does NOT touch `install_enhanced_service.sh`, `blackbox-enhanced.service`, or `cleanup_crash.sh`. After Track 4 ships, these become orphaned legacy files. Either:
- Delete them too (`git rm install_enhanced_service.sh blackbox-enhanced.service cleanup_crash.sh`)
- Or carry forward the watchdog/cleanup features into Track 4's unit (M3 decision)

---

### N7: `dpkg -i` retry logic is wrong for missing deps

**Plan line 3512:** `sudo dpkg -i ... || sudo apt install -fy`

The `apt install -fy` does fix broken deps after a failed `dpkg -i`, but the message ordering is confusing. Better pattern:

```bash
sudo apt install -y "$DEB_PATH"  # apt handles deps automatically since apt 1.1+
```

(Modern apt accepts a local .deb file path directly and resolves deps in one step. No `dpkg -i` + `apt -f` two-step needed.)

---

### N8: Phase 4.2 factory image task is correctly stubbed

Plan line 3553–3582 says `TODO: fill in once hardware spec locked.` That is the right call. Audit recommends NO change — Track 4.2.1 ships as a stub with `exit 1` until hardware spec lands.

---

## NEW tasks recommended

### T4.0.1 (NEW): System pre-flight check

**Why:** Mirror Track 3's T3.0.1 sysprep pattern — separate, low-risk task that validates customer environment before main install.

**Steps:**
1. Verify Ubuntu version (24.04 only; warn on 22.04 / 23.x)
2. Verify minimum disk free (10 GB on `/`)
3. Verify minimum RAM (4 GB)
4. Verify network reachability (`curl -fsS https://api.github.com/zen` for connectivity)
5. Verify sudo access (`sudo -n true` — fail early if no sudo)
6. Detect existing BlackBox install (running blackbox.service — offer upgrade vs fresh path)

This becomes Phase **4.0** (sysprep), and current Phase 4.1 becomes the actual install.

---

### T4.1.0 (NEW, conditional on Q1): `.deb` distribution wiring

Whichever option Brandon chooses for C2 (build from source / GitHub release / factory pre-baked), it deserves its own task with explicit step list. Don't bury this in Task 4.1.1.

---

### T4.1.3 (NEW): `.gitignore` hygiene

One-liner task: add `.onboarding_state.json`, `.onboarding_complete`, `.env.backup.*` to `.gitignore`. Could fold into T4.1.1 Step 0 ("prep") if we don't want a standalone task.

---

### T4.1.4 (NEW): Update `.env.template` to canonical key inventory

Per M4: rewrite `.env.template` to include all wizard-managed env vars (commented placeholders + descriptions + provider URLs). This becomes the single source of truth for "what does the wizard configure."

---

## OPEN QUESTIONS for Brandon

These are the decisions that drive plan-edit shape. Audit cannot prescribe — Brandon decides, then audit recommendations follow.

### Q1: `.deb` distribution method (drives C2 fix)

Three viable models:

| Option | Pros | Cons |
|---|---|---|
| **A. Build from source on customer machine** | No release asset infra needed; repo self-contained | +15–30 min install time; +2 GB disk; needs Tauri build deps |
| **B. Download from GitHub release asset** | Fast install (5 min); customer never compiles Rust | Need release workflow; repo must be public OR install.sh needs token-auth |
| **C. Factory image (Track 4.2 path)** | Fastest customer experience; flash-and-go | Doesn't work for software-only install path; tied to hardware spec |

(Hybrid is also possible: install.sh tries A then falls back to B, or vice versa.)

### Q2: Customer install location

| Option | Pros | Cons |
|---|---|---|
| **A. `~/blackbox-poc`** (current dev-box pattern) | Simplest; no sudo for code; matches dev box; `User=$USER` straightforward | Fragile if user moves dirs; per-user not per-machine |
| **B. `/opt/blackbox`** (FHS-standard) | Survives user account changes; respects Linux conventions; clear "this is system software" | Needs `sudo cp -r` of repo; needs system service user |
| **C. Detect-and-offer** | Best UX; refuses transient locations (Downloads/Desktop/tmp) | More install.sh complexity; one more prompt |

### Q3: Self-healing infrastructure (drives M3 fix)

Which features from the existing `install_enhanced_service.sh` + `blackbox-enhanced.service` should Track 4 carry forward?

| Feature | Recommendation | Reason |
|---|---|---|
| `Type=notify` + `WatchdogSec=120` (HTTP /watchdog heartbeat) | KEEP if `/watchdog` endpoint exists (verify) | Free crash detection |
| Log rotation (`/etc/logrotate.d/blackbox`) | KEEP | Customer disks fill up otherwise |
| `cleanup_crash.sh` ExecStopPost | DROP | Logs + journalctl are sufficient; one less moving part |
| Watchdog .timer (separate `blackbox-watchdog.timer`) | DROP | systemd's own `WatchdogSec` is enough |
| Override.conf scaffolding | KEEP (empty file with commented examples) | Customer customization without touching unit |
| IO bandwidth caps (`IOReadBandwidthMax=/dev/sda`) | DROP | `/dev/sda` assumption breaks on NVMe (`/dev/nvme0n1`) |
| Helper scripts (`blackbox-status.sh`, `blackbox-cleanup.sh`) | KEEP | Convenient `./blackbox-status.sh` is operator-friendly |

Brandon: confirm or override.

### Q4: Memory cap on customer machines

Current dev unit: `MemoryMax=24G` (assumes 30 GB box). Customer mini-PC likely 8–16 GB.

| Option | Behavior |
|---|---|
| **A. Drop entirely** | OS handles overcommit; oom-killer fires when needed. Simplest. |
| **B. Hardcode `MemoryMax=4G`** | Conservative default. Loud failure mode if BlackBox grows. |
| **C. Compute at install time: `~70% of system RAM`** | Adapts to hardware. Brittle if customer adds RAM later (unit doesn't auto-update). |
| **D. Compute via `MemoryMax=infinity` + `MemoryHigh=70%`** | Soft pressure (memory.high) instead of hard cap (memory.max). Lets kernel reclaim cleanly. |

Recommendation: **D** (or A if you want zero magic). B is least good for a product where workload size varies with operator activity.

### Q5: Public repo for v1?

Tied to Q1 Option B (GitHub release download): this only works if the repo is public OR install.sh ships with a fine-grained PAT that scopes only to release assets. Public repo is simpler operationally but exposes all code (including any embedded model prompts, internal architecture, etc.) Brandon's call.

---

## Recommended audit fixes (after Q1–Q5 answered)

Once Brandon picks Q1–Q5, plan-edit phase applies these (mirroring Track 3 audit-fix style):

1. **Fix C1** — replace broken xargs pipeline with grep | awk | xargs.
2. **Fix C2** — implement chosen .deb distribution model in new T4.1.0; update path/filename references throughout; update dpkg name to `black-box-setup`.
3. **Fix C3** — `/usr/local/bin/blackbox-setup` → `/usr/bin/blackbox-setup`.
4. **Fix M1** — implement chosen install-location strategy (Q2 answer).
5. **Fix M2** — restore security hardening + uvicorn flags + memory cap per Q4.
6. **Fix M3** — carry forward Q3-approved features; drop the rest.
7. **Fix M4** — rewrite `.env.template` per T4.1.4; restore `BLACKBOX_ROOT=` commented block.
8. **Fix M5** — add `sudo systemctl restart blackbox.service` after `enable`.
9. **Fix M6** — sudo-detection block at top of install.sh; use `$REAL_USER` / `$REAL_HOME` throughout.
10. **Fix N3** — add gitignore entries (T4.1.3).
11. **Fix N6** — `git rm install_enhanced_service.sh blackbox-enhanced.service cleanup_crash.sh` in same Track 4 cleanup commit.
12. **Fix N7** — replace `dpkg -i` + `apt -f` with `apt install -y` of local .deb path.

Optional (non-blocking):
- N4: skip `apt update` if recent.
- N5: split VM smoke test into Brandon-owned task.

---

## Pre-execution audit — final note

Audit complete. Recommend executing in this order:

1. **Brandon answers Q1–Q5** (this document).
2. **Plan-edit phase** (controller-direct): apply audit fixes to master plan Phase 4.1 + 4.2.
3. **Implementer dispatch** (subagent-driven-development): T4.0.1 (sysprep) → T4.1.0 (.deb distribution) → T4.1.1 (modernized install.sh) → T4.1.2 (verification recipe) → T4.1.3 (.gitignore) → T4.1.4 (.env.template) → T4.2.1 (factory-image stub) → T4.3 (milestone tag `track4-installer-complete`).

Estimated implementation effort post-audit: **~1 week** (matches plan estimate). Unknown factor: depending on Q1 answer, .deb distribution wiring may add 1–2 days.

---

## Post-execution empirical corrections (2026-05-12)

After the pre-execution audit findings landed (Q1–Q4 decisions locked), the subagent pipeline executed and surfaced four additional corrections during the code-quality review of T4.1.1. These were folded back into the master plan and into `Scripts/install.sh` via fix-loop commit `6b3197e`. Documenting here so the audit doc stays the canonical "what we caught and why" record.

| Correction | Severity | Where caught | Fix landed |
|---|---|---|---|
| **C1-empirical:** `desktop-file-utils` package missing from `Scripts/onboarding/system-packages.txt` MUST_HAVE bucket | CRITICAL | T4.1.1 code-quality review (Step 6b's `update-desktop-database` call would silently no-op without the package, leaving the persistent menu launcher invisible until next refresh) | system-packages.txt MUST_HAVE addition + plan reference |
| **I1-empirical:** Step 2 venv + pip ran as root when `install.sh` invoked via sudo, leaving root-owned files in user tree | IMPORTANT | T4.1.1 code-quality review (audit M6 sudo-detection captured `$REAL_USER` but Steps 2/3 didn't use it; Step 6 already used `sudo -u "$REAL_USER"` correctly — inconsistency caught) | All 3 venv+pip commands now prefixed `sudo -u "$REAL_USER"` |
| **I2-empirical:** Step 3 `.env` created as root; secrets file ended up root-owned 0644 instead of user-owned 0600 | IMPORTANT | T4.1.1 code-quality review | `cp` + `bash -c "echo ... >> ..."` both prefixed `sudo -u "$REAL_USER"`; trailing `chmod 0600` added |
| **I4-empirical:** `ls "..."*.deb \| head -1` resolves alphabetically, picking stale older `.deb` after version bumps | IMPORTANT | T4.1.1 code-quality review | `rm -f "$BUNDLE_DIR"/*.deb 2>/dev/null \|\| true` added before `cargo tauri build` |

**Pre-T4.1.5 doc cleanup correction (commit `af033fb`):** the implementer's safety check for T4.1.5 found 3 doc-file references to `setup.sh` (CUSTOMER_SETUP.md, Claude.md, AUDIT_REPORT.md). Brandon's call: surgical update CUSTOMER_SETUP.md to point at `Scripts/install.sh`, delete Claude.md (legacy duplicate of canonical CLAUDE.md), leave AUDIT_REPORT.md alone (historical Nov 2025 audit, all past-tense). T4.1.5 then proceeded cleanly at commit `2f37536`.

**Type=notify decision actually taken:** implementer ran `grep -rn "sd_notify\|systemd.daemon" Orchestrator/app.py Orchestrator/startup.py` — no matches found. Per Step 2 BLOCKING audit instructions, downgraded to `Type=simple` and removed the 5-line `WatchdogSec=120` block. Documented in commit `6855738` body. If `sd_notify` gets wired in a future `Orchestrator/app.py` change, revisit and restore Type=notify + WatchdogSec.

**Deferred to post-Track-4 follow-up commit:**
- I3 (build_tauri_setup `cd` outside subshell + dead `.deb` existence guard after `set -e`)
- M1–M5 (npm error swallowing, brittle cargo-version grep, apt update no-error-handling, getent failure handling, systemctl restart non-atomic)

These are non-blocking quality nits. Defensible as-is for v1; revisit if any cause real issues during T4.1.2 VM smoke test or post-launch.

---

*End of audit + post-execution corrections. Same shape as Track 3 audit at `docs/plans/2026-05-12-track3-audit.md` for reference.*
