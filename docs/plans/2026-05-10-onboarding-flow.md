# AI BlackBox Onboarding Flow Implementation Plan

> **For Claude:** This plan is large (6 tracks, ~7-9 weeks including manage-mode). REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute (fresh subagent per task with two-stage review). `superpowers:executing-plans` is the alternative for parallel-session execution.

**Goal:** Build a customer-facing first-run onboarding experience for AI BlackBox — a Tauri standalone app that wraps Portal `/onboarding` routes, walks the customer through Tailscale install, BYOK API keys, optional integrations, QR phone pairing, operator setup, and completion handoff. **Ships with a paired maintenance UI** (same wizard re-entered via `?mode=manage`) accessible from Portal's System Menu and a persistent desktop launcher — customers can review/update their configuration any time. Ships as the first-boot experience on pre-installed mini-PC hardware.

**Architecture:** Tauri (Rust) shell wraps a webview pointing at `http://localhost:9091/onboarding`. Portal's existing FastAPI server hosts the wizard UI as HTML/CSS/JS. Tauri shell provides full-screen branded chrome, taskbar icon, no browser address bar. After completion, app self-disables and Portal becomes the regular kiosk view. Six implementation tracks executed in dependency order: **Foundation Cleanup → Onboarding Backend → Portal Wizard UI → Tauri Shell → Install Scripts → Customer Docs.** Track 5 (docs) drafts can begin earlier but finalize after Track 2.

> **⚠ Critical-resource serialization rule:** `Orchestrator/app.py` is touched by **at least 5 tasks across Tracks 0/1/2** (router includes, middleware, static mount). NEVER dispatch two `app.py`-touching tasks in parallel. Land each as a small sequential commit with a single owner. This is the most likely merge-conflict surface in the entire plan.

**Tech Stack:** FastAPI (existing), Python 3.12 (existing), HTML/CSS/JS (Portal modules pattern, existing), Tauri 2.x (Rust, new), Cargo (Rust toolchain, new), `qrcode` Python lib (new), `python-dotenv` (existing), systemd (existing), `.desktop` autostart files (new).

---

## Context

After the mono-repo migration shipped to GitHub on 2026-05-09 (commit `e5c3cad`, captured in `SNAP-20260509-6533`) and the Android Portal MVP UX fixes on 2026-05-10 (commit `6d9c3ff`, captured in `SNAP-20260510-6552`), the next major build is the customer-facing onboarding flow. The discovery sweep on 2026-05-10 (`SNAP-20260510-6554`, doc at `docs/onboarding/discovery-notes.md` committed in `cedb9aa`) confirmed there is no existing first-run flow anywhere in the codebase. Greenfield UX with three blocker classes:

1. **Secrets sprawl** — 17 secret env-vars across 16 files; 9 Python files re-read via `os.getenv` instead of importing from central config.
2. **Path portability** — 194 hardcoded `/home/ai-black-box-fc/...` and Tailscale FQDN references in checked-in code (systemd units, `.mcp.json`, app HTML media URLs, docs).
3. **Dependency reproducibility** — `requirements.txt` declares 11 packages; live venv has ~120 (transitive). Fresh `pip install -r requirements.txt` does NOT reproduce the working environment.

These three classes must be addressed in Track 0 before the onboarding UI can be reliably built on top.

---

## Architectural Decisions Recap (LOCKED 2026-05-10)

| Decision | Value |
|---|---|
| Target persona | Eventual customers / strangers buying a BlackBox |
| Distribution | Hardware product first (pre-installed mini-PC); software-only as v2 |
| UI architecture | Tauri shell wrapping Portal `/onboarding` routes |
| Validation strictness | Each integration has "Skip for now" |
| Secrets sweep scope | Lite — collapse 9 Python stragglers; leave Asterisk/FreeSWITCH alone |
| API key model | Bring-your-own-keys (BYOK) |

---

## Defaults Selected for the 8 Deferred Open Questions

These are reasonable defaults baked into this plan. **The audit session may override any of them**, in which case the affected tasks need editing.

| # | Question | Default chosen here | Why |
|---|---|---|---|
| 1 | Platform support v1 | **Ubuntu 24.04 LTS only** | Matches shipped hardware; keeps Track 0 scope tight. v1.1 can add Debian-likes (Pop!_OS, Mint). |
| 2 | Idempotency on re-run | **Detect-and-skip with override toggle** | If a step is already configured, show "Already configured ✓ — reconfigure?" toggle. Best UX. |
| 3 | Migration vs fresh | **v1 = fresh install only** | Migration from existing install is a v1.5 feature. Most customers never migrate. |
| 4 | Single vs multi-operator setup | **Wizard collects 1+ operator name(s) from the customer; registers them via `/operator/add` so they exist in the system before Portal opens. "Brandon" stays as a code-level technical seed only — never shown as a default to customers.** | Pre-registers real operators so the customer doesn't have to set them up post-onboarding. Multi-name input via "Add another operator" button. The internal Brandon seed is analogous to the "system" operator used for apps — unchanged by wizard. |
| 5 | Hardening at install time | **DROPPED — defer to v1.1.** Document existing weak defaults (Drachtio "cymru", FreeSWITCH "ClueCon", TG200 "password") in `docs/TROUBLESHOOTING.md` with manual-rotation instructions. | Auto-rotation requires cross-component coordination (Drachtio↔FreeSWITCH share a secret; rotating one without the other silently breaks SIP). Out of scope for v1 — implementation surface is large and the failure mode (broken phone in field) is the worst kind of regression. |
| 6 | Tier-1 integrations for v1 wizard | **OpenAI, Anthropic, Google, Tailscale, Gmail (5 providers).** Twilio / ElevenLabs / Asterisk / xAI / Perplexity → v1.1. Cellular + UGV → never in v1 wizard. | Phone calls + SMS in v1 are handled by the **TG200 cellular modem** (already auto-detected via `Orchestrator/cellular/hotplug.py` — no customer credentials to collect). Twilio is reserved for v1.1 customers who want a second phone path. |
| 7 | Hardware spec for shipped mini-PC | **TBD — Track 5 factory-image build is plan-skeletal** | Plan stays hardware-spec-agnostic for v1. When spec lands, fill in Track 5 image-build details. |
| 8 | Tailscale customer flow | **Each customer owns their tailnet (own auth) AND wizard handles "no account yet" path.** | Simpler architecture — no shared subdomain. New-to-Tailscale customers get an explicit "Don't have an account? → tailscale.com/start (~5 min)" branch with brief explainer. Skip → LAN-only remains as escape hatch. |

---

## How to Read This Plan

- **Six tracks**, executed in dependency order. Real critical path: **T0 → T1 → T2 → T3 → T4**. Track 5 (docs) drafts can begin during T2 but finalize after T2.8.1. Track 6 is post-v1 (deferred).
- **Each track has phases**, each phase has **numbered tasks** (Task X.Y.Z format).
- **Each task is bite-sized** (2-5 minutes of focused work) per the writing-plans skill convention.
- **Every task includes:** Files (Create/Modify with absolute paths), Steps (with code where applicable), Test command (with expected output), Commit step.
- **Commit cadence:** every 3-7 tasks typically; explicit Commit tasks are numbered.
- **Tasks reference existing files to reuse** rather than inventing new patterns.
- **Symbols:** `[skippable]` = task can be deferred without breaking later tasks; `[blocking]` = next task depends on this; `[parallel-safe]` = can be done concurrently with siblings.

> **Track 4 sequencing correction (audit 2026-05-10, refined 2026-05-12):** Earlier text called Track 4 "parallelizable with Tracks 1+2." That is wrong. Track 4's `install.sh` builds the Tauri setup `.deb` from sources in `installer/src-tauri/` (Track 3 deliverable; bundles are gitignored under `target/`), and the systemd unit work is the deferred Bucket B1 from T0.3.7. Track 4 is the LAST execution track, not parallel. **Audit Q1=A:** customer machine builds .deb from source (no GitHub release asset path in v1). See `docs/plans/2026-05-12-track4-audit.md` for the full Track 4 audit.

---

# TRACK 0 — FOUNDATION CLEANUP

**Goal:** Make the codebase ready for the onboarding UI to build on top. Three deliverables: (a) reproducible Python environment, (b) collapsed secrets re-reads, (c) portable file paths via `BLACKBOX_ROOT` + `paths.py` resolver utility, (d) cleanly-routed pairing endpoints.

**Estimated effort:** 1-2 weeks
**Dependencies:** None — must come first
**Outcome:** Fresh `git clone + setup` reproduces a working BlackBox; one secret-config import pattern; no hardcoded `/home/ai-black-box-fc/` strings; pairing endpoints in proper file with claim flow.

## Phase 0.1: Generate transitive lockfile + system-package manifest

**Background:** `requirements.txt` declares 9 direct packages (6 already pinned with `==`, 3 loose with `>=`). The live venv has ~103 installed packages — meaning ~94 transitive deps with no pinned source of truth. **The real gap is not "sparse pinning of direct deps" but "no transitive lockfile."** Solution: use `pip-compile` (or `uv lock`) to generate a fully-pinned `requirements.lock.txt` that reproduces the exact venv on a fresh install. Then keep `requirements.txt` for direct deps + maintain `system-packages.txt` for apt.

### Task 0.1.1: Snapshot current venv state

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Scripts/onboarding/freeze-venv.sh`

**Step 1: Write the freeze script**

```bash
#!/usr/bin/env bash
# Capture current working venv state for reproducibility
set -euo pipefail

VENV_DIR="${1:-Orchestrator/venv}"
OUT="${2:-requirements.lock.txt}"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "ERROR: venv not found at $VENV_DIR" >&2
    exit 1
fi

"$VENV_DIR/bin/pip" freeze --all > "$OUT"
echo "Wrote $(wc -l < "$OUT") packages to $OUT"
```

**Step 2: Run it**

```bash
chmod +x Scripts/onboarding/freeze-venv.sh
./Scripts/onboarding/freeze-venv.sh
```

Expected: `Wrote 120ish packages to requirements.lock.txt`

**Step 3: Commit**

```bash
git add Scripts/onboarding/freeze-venv.sh requirements.lock.txt
git commit -m "chore(deps): freeze working venv to requirements.lock.txt"
```

### Task 0.1.2: Categorize lock file into Direct vs Transitive

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/requirements.txt`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/requirements.dev.txt`

**Step 1: Read current requirements.txt** to see the 11 declared packages.

**Step 2: Read requirements.lock.txt.** For each lock entry, decide:
- **Direct dependency** (top-level import in our code) → keep in `requirements.txt` with version pin
- **Transitive** (pulled in by a direct dep) → omit from `requirements.txt`; lock file is authoritative
- **Dev-only** (`pytest`, `ruff`, `black`, `mypy`, etc.) → move to `requirements.dev.txt`

**Step 3: Add the missing direct deps the discovery agent identified:**

```
anthropic>=0.96
openai>=2.9
google-genai>=1.64
google-generativeai>=0.8.6
google-api-python-client
apscheduler
numpy
scipy
cryptography
lxml
pillow
miniaudio
webrtcvad-wheels
pyserial
mss
reportlab
python-docx
beautifulsoup4
html2text
duckduckgo_search
psutil
pytz
tzlocal
qrcode[pil]   # NEW — needed by pairing routes (Phase 0.5)
```

(Pin minimum versions from `requirements.lock.txt`; allow patch-level updates.)

**Step 4: Verify install reproducibility in a throwaway venv**

```bash
python3.12 -m venv /tmp/onboarding-test-venv
/tmp/onboarding-test-venv/bin/pip install -r requirements.txt
/tmp/onboarding-test-venv/bin/python -c "from Orchestrator.app import app; print('OK')"
```

Expected: `OK` printed; no ImportError.

**Step 5: Commit**

```bash
git add requirements.txt requirements.dev.txt
git commit -m "chore(deps): pin direct dependencies; split dev-only into requirements.dev.txt"
```

### Task 0.1.3: Document system-package requirements

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Scripts/onboarding/system-packages.txt`

**Step 1: Write the manifest**

```
# AI BlackBox System Package Requirements (Ubuntu 24.04)
# Format: <package> # <bucket> # <reason>

# === MUST_HAVE — system won't start without ===
python3.12               # MUST_HAVE # Python runtime
python3.12-venv          # MUST_HAVE # virtualenv creation
python3-pip              # MUST_HAVE # pip
build-essential          # MUST_HAVE # cffi/cryptography source builds
git                      # MUST_HAVE # repo updates
curl                     # MUST_HAVE # health checks, tailscale install
ca-certificates          # MUST_HAVE # TLS
tmux                     # MUST_HAVE # CLI Agent session manager hardcodes
sudo                     # MUST_HAVE # privileged operations
python3-dbus             # MUST_HAVE # XDG portal screenshot subprocess
python3-gi               # MUST_HAVE # GObject introspection for portal

# === SHOULD_HAVE — degraded but functional ===
chromium-browser         # SHOULD_HAVE # Tauri webview backend, kiosk fallback
xdotool                  # SHOULD_HAVE # Computer Use input injection
scrot                    # SHOULD_HAVE # X11 screenshot
openbox                  # SHOULD_HAVE # CU window management
x11vnc                   # SHOULD_HAVE # remote desktop CU
mpg123                   # SHOULD_HAVE # audio playback
alsa-utils               # SHOULD_HAVE # audio devices
pulseaudio-utils         # SHOULD_HAVE # PulseAudio control

# === FEATURE_OPTIONAL ===
docker.io                # FEATURE_OPTIONAL # phone integration (FreeSwitch/Drachtio)
docker-compose           # FEATURE_OPTIONAL # phone integration
adb                      # FEATURE_OPTIONAL # Android device pairing routes

# === HARDWARE_OPTIONAL — only if hardware present ===
modemmanager             # HARDWARE_OPTIONAL # cellular modem
network-manager          # HARDWARE_OPTIONAL # cellular APN
# (UGV-stack packages live in docs/ugv-beast/, not installed on mini-PC)

# === DEV_ONLY — for development, not customer install ===
# (See requirements.dev.txt for Python dev deps)
```

**Step 2: Commit**

```bash
git add Scripts/onboarding/system-packages.txt
git commit -m "chore(install): document system package requirements with bucket classification"
```

## Phase 0.2: Lite secrets sweep — collapse 9 Python stragglers

**Background:** 9 Python files re-read secrets via `os.getenv()` instead of importing from `Orchestrator.config`. We refactor them to the canonical pattern. Asterisk/FreeSWITCH config files are LEFT ALONE per the lite-sweep decision (consumed by external daemons).

### Task 0.2.1: Add `GEMINI_API_KEY` to central config (the one outlier)

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/config.py`

**Step 1: Add after line 324** (`GOOGLE_API_KEY` definition):

```python
# Gemini API key — historically read directly via os.getenv in gemini_agent_routes.
# Falls back to GOOGLE_API_KEY (they are interchangeable for the Gemini SDK).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", GOOGLE_API_KEY)
```

**Step 2: Verify Python syntax**

```bash
Orchestrator/venv/bin/python -c "from Orchestrator.config import GEMINI_API_KEY; print('OK', bool(GEMINI_API_KEY))"
```

Expected: `OK True` (since `GOOGLE_API_KEY` is set)

**Step 3: Don't commit yet** — bundle with Task 0.2.2.

### Task 0.2.2: Refactor 9 stragglers to import from central config

**Files (Modify):**
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/tts_routes.py` (lines 119, 210, 324, 356)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/admin_routes.py` (lines 320, 321, 322)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/asterisk/ivr_audio.py` (line 74)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/backfill_embeddings.py` (line 26)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/asterisk/config.py` (lines 19, 51)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/gemini_agent_routes.py` (lines 160, 705)

**Step 1: For each file, replace pattern:**

```python
# OLD
key = os.getenv("OPENAI_API_KEY")
```

```python
# NEW (at top of file, with other imports)
from Orchestrator.config import OPENAI_API_KEY
# ...later in code...
key = OPENAI_API_KEY
```

For `gemini_agent_routes.py:160,705`:

```python
# OLD
api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
```

```python
# NEW
from Orchestrator.config import GEMINI_API_KEY
# ...later...
api_key = GEMINI_API_KEY  # already falls back to GOOGLE_API_KEY in config.py
```

**Step 2: Run smoke test**

```bash
Orchestrator/venv/bin/python -c "
from Orchestrator.routes import tts_routes, admin_routes, gemini_agent_routes
from Orchestrator.asterisk import ivr_audio, config as ast_config
print('OK — all imports succeed')
"
```

Expected: `OK — all imports succeed`

**Step 3: Restart blackbox.service and verify it starts cleanly**

```bash
sudo systemctl restart blackbox.service
sleep 70  # wait for snapshot index rebuild
sudo systemctl status blackbox.service --no-pager | head -15
curl -s http://localhost:9091/health | python3 -m json.tool | head -20
```

Expected: service active; `/health` returns JSON with `"status":"ok"`.

**Step 4: Commit**

```bash
git add Orchestrator/config.py \
        Orchestrator/routes/tts_routes.py \
        Orchestrator/routes/admin_routes.py \
        Orchestrator/asterisk/ivr_audio.py \
        Orchestrator/backfill_embeddings.py \
        Orchestrator/asterisk/config.py \
        Orchestrator/routes/gemini_agent_routes.py
git commit -m "refactor(config): collapse 9 os.getenv stragglers to import from central config

- Add GEMINI_API_KEY to Orchestrator/config.py with GOOGLE_API_KEY fallback
- Refactor 9 Python files re-reading secrets via os.getenv to import from config
- Closes the 'shadow read' channels identified in onboarding discovery sweep
- Asterisk/FreeSWITCH config files intentionally left alone (lite-sweep scope)"
```

### Task 0.2.3: Audit and delete orphan Secrets/.env files

**Files (potentially Delete):**
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Secrets/.env`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/Secrets/.env`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/credentials/gmail_oauth_client.json`

**Step 1: Confirm nothing reads them**

```bash
grep -rn "Secrets/" --include="*.py" --include="*.sh" --include="*.service" \
    Orchestrator/ Scripts/ deployments/ 2>/dev/null
grep -rn "gmail_oauth_client.json" --include="*.py" Orchestrator/ 2>/dev/null
```

Expected: no Python references to either path.

**Step 2: Move (don't rm) to a quarantine area first** in case some script you missed needs them:

```bash
mkdir -p Archive/orphan-secrets-quarantine-2026-05-10
mv Secrets/.env Archive/orphan-secrets-quarantine-2026-05-10/Secrets-env
mv Orchestrator/Secrets/.env Archive/orphan-secrets-quarantine-2026-05-10/Orchestrator-Secrets-env
mv credentials/gmail_oauth_client.json Archive/orphan-secrets-quarantine-2026-05-10/
rmdir Secrets Orchestrator/Secrets 2>/dev/null || true
```

(`Archive/` is gitignored per `.gitignore`, so quarantine doesn't get committed.)

**Step 3: Restart and verify**

```bash
sudo systemctl restart blackbox.service && sleep 70
curl -s http://localhost:9091/health | python3 -m json.tool | head -10
```

Expected: service still healthy; nothing broke.

**Step 4: Commit the directory removal**

```bash
git add -u  # picks up the directory deletions
git status --short  # verify expected files staged
git commit -m "chore(secrets): quarantine 3 orphan secret files

- Secrets/.env (duplicate OPENAI_API_KEY, no consumers)
- Orchestrator/Secrets/.env (duplicate OPENAI_API_KEY, no consumers)
- credentials/gmail_oauth_client.json (Gmail OAuth uses env vars, file unread)

All moved to Archive/ (gitignored) for one-week safety hold before permanent rm.
Service restart smoke test passes."
```

## Phase 0.3: BLACKBOX_ROOT + path resolver utility

**Background (re-baselined 2026-05-10 audit):** Earlier framing said "194 hardcoded paths" — that number was a glob across all extensions. Independent grep gives **328 total hits**, but they break down very unevenly:
- **B3 (Python code): ~13 hits** — small. Real refactor is 1-2 hours. `MCP/blackbox_mcp_server.py` already uses `os.getenv("BLACKBOX_ROOT", ...)` fallback (good template).
- **B4 (App HTML/JS Tailscale URLs): ~31 hits** — real user impact, breaks on any other machine.
- **B5 (Markdown docs / examples in prose): ~207 hits** — bulk. Mostly placeholder substitution. **68 of these are inside this plan itself** (descriptive prose, not code requiring change — leave alone).
- **B1 (systemd units): ~3 files** — handled at Track 4 install-time template substitution.
- **B2 (config files like `.mcp.json`): ~5 files** — same install-time substitution.
- **B6 (intentional examples in CLAUDE.md): ~10 hits** — leave alone.

Solution stays the same: one canonical `BLACKBOX_ROOT` env var + a `paths.py` utility every code path uses. Just right-size the work — B3 is small, B5 is the bulk but boring, B1+B2 are deferred to Track 4.

### Task 0.3.1: Create the paths utility module

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/utils/__init__.py` (if missing)
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/utils/paths.py`

**Step 1: Write `paths.py`:**

```python
"""Central path-resolver for AI BlackBox.

All code that constructs a filesystem path INSIDE the BlackBox project tree
should use these helpers rather than hardcoding paths or assuming CWD.

The root is determined in this priority:
  1. BLACKBOX_ROOT env var (set by systemd unit, .env, or installer)
  2. Walk up from this file until we find a sentinel (CLAUDE.md + Orchestrator/)
  3. Fall back to /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
     (the legacy default; warns to log if reached)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from functools import lru_cache

LEGACY_DEFAULT = "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"


@lru_cache(maxsize=1)
def blackbox_root() -> Path:
    """Return the absolute Path to the BlackBox project root."""
    env = os.getenv("BLACKBOX_ROOT")
    if env:
        return Path(env).resolve()

    # Walk up from this file looking for sentinels
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "CLAUDE.md").exists() and (parent / "Orchestrator").is_dir():
            return parent

    # Fallback — warn loudly so we can grep for any caller hitting this
    print(
        f"[paths.py] WARNING: BLACKBOX_ROOT not set and sentinels not found; "
        f"falling back to legacy default {LEGACY_DEFAULT}",
        file=sys.stderr,
    )
    return Path(LEGACY_DEFAULT)


def resolve(*parts: str) -> Path:
    """Resolve a path under the BlackBox root.

    >>> resolve("Portal", "uploads")
    PosixPath('.../blackbox_poc/Portal/uploads')
    """
    return blackbox_root().joinpath(*parts)


def portal_dir() -> Path:
    return resolve("Portal")


def uploads_dir() -> Path:
    return resolve("Portal", "uploads")


def credentials_dir() -> Path:
    return resolve("credentials")


def manifest_dir() -> Path:
    return resolve("Manifest")


def volume_dir() -> Path:
    return resolve("Volume")


def fossils_dir() -> Path:
    return resolve("Fossils")


def apps_dir() -> Path:
    return resolve("Apps")
```

**Step 2: Verify**

```bash
Orchestrator/venv/bin/python -c "
from Orchestrator.utils.paths import blackbox_root, resolve, uploads_dir
print('root:', blackbox_root())
print('uploads:', uploads_dir())
print('resolve test:', resolve('docs', 'plans'))
"
```

Expected: 3 absolute paths printed, all under the project root.

**Step 3: Commit**

```bash
git add Orchestrator/utils/__init__.py Orchestrator/utils/paths.py
git commit -m "feat(paths): add BLACKBOX_ROOT-aware path resolver utility

Single canonical place to compute paths under the BlackBox project tree.
Resolution priority: BLACKBOX_ROOT env var > sentinel walk-up > legacy default.
Helpers: blackbox_root(), resolve(*parts), portal_dir(), uploads_dir(),
credentials_dir(), manifest_dir(), volume_dir(), fossils_dir(), apps_dir()."
```

### Task 0.3.2: Add BLACKBOX_ROOT to .env.template

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.env.template`

**Step 1: Prepend (above existing keys):**

```bash
# === Project Root ===
# Absolute path to the BlackBox project root.
# Auto-detected if unset (walks up from Orchestrator/ looking for CLAUDE.md sentinel).
# Set explicitly in production for reliability.
# BLACKBOX_ROOT=/path/to/your/blackbox_poc
```

**Step 2: Commit**

```bash
git add .env.template
git commit -m "docs(env): add BLACKBOX_ROOT example to .env.template"
```

### Task 0.3.3: Audit the 194 hardcoded paths into buckets

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/docs/onboarding/path-sweep-audit.md`

**Step 1: Generate the full hit list**

```bash
grep -rnE "ai-black-box-fc|/home/ai-black-box-fc" \
  --include="*.py" --include="*.kt" --include="*.js" --include="*.html" \
  --include="*.sh" --include="*.service" --include="*.json" --include="*.md" \
  --include="*.toml" --include="*.yaml" --include="*.yml" --include="*.conf" \
  2>/dev/null | grep -v -E "venv/|node_modules/|/build/|\.gradle/|/Volume|/Manifest|/Fossils|/Archive|tasks\.db" \
  > /tmp/path-hits-raw.txt

wc -l /tmp/path-hits-raw.txt   # ~328 total expected (re-baselined 2026-05-10 audit)
# Group by extension to confirm bucket sizes:
awk -F: '{print $1}' /tmp/path-hits-raw.txt | awk -F. '{print $NF}' | sort | uniq -c | sort -rn
# Expected approx: md=207, json=70, html=31, py=13, sh=4, others
```

**Step 2: Categorize each hit into one of these buckets:**

- **B1 — systemd unit files** (`*.service`): rewrite via template substitution at install time
- **B2 — Config files** (`.mcp.json`, `*.toml`): `${BLACKBOX_ROOT}` substitution at install time
- **B3 — Python/JS code**: replace with `from Orchestrator.utils.paths import …` (Python) or `window.BBX_ROOT` (JS)
- **B4 — App media URLs in HTML** (`Apps/*/index.html`): replace absolute Tailscale URL with relative `/ui/uploads/...` or origin-aware `<base href>`
- **B5 — Documentation** (`*.md`): replace with `<your-tailnet-hostname>` / `<BLACKBOX_ROOT>` placeholders + brief note on substitution
- **B6 — Already-correct** (intentional examples in CLAUDE.md, comments referencing actual hostname): leave alone, document why

Write the audit doc as a table:

```markdown
# Path Sweep Audit (194 hits)

| File | Line | Pattern | Bucket | Action |
|---|---|---|---|---|
| Apps/system-monitor/blackbox-monitor.service | 7 | User=ai-black-box-fc | B1 | Rewrite via install-time template |
| Apps/system-monitor/blackbox-monitor.service | 8 | WorkingDirectory= | B1 | Rewrite via install-time template |
| Apps/system-monitor/blackbox-monitor.service | 9 | ExecStart= | B1 | Rewrite via install-time template |
| .mcp.json | 6 | full path to MCP server | B2 | ${BLACKBOX_ROOT} substitution |
| .mcp.json | 10 | BLACKBOX_ROOT value | B2 | ${BLACKBOX_ROOT} substitution |
| Apps/echoes-of-titan/index.html | 227 | Tailscale FQDN audio src | B4 | Use relative /ui/uploads/ |
| ... etc ... |
```

(Full table populated by inspection — Task 0.3.4 batches the actual edits per bucket.)

**Step 3: Commit the audit doc**

```bash
git add docs/onboarding/path-sweep-audit.md
git commit -m "docs(onboarding): full audit of 194 hardcoded path references

Buckets: B1 systemd, B2 config, B3 code, B4 app HTML, B5 docs, B6 intentional.
Each bucket gets its own subsequent task to apply the fix consistently."
```

### Task 0.3.4: Apply Bucket B3 fixes — Python code path-resolver migration

**Files:** Every Python file in B3 (likely 5-15 files based on the audit).

**Step 1: For each B3 file, replace patterns like:**

```python
# OLD
SNAPSHOT_DIR = "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest"
```

```python
# NEW
from Orchestrator.utils.paths import manifest_dir
SNAPSHOT_DIR = str(manifest_dir())
```

**Step 2: Run full test smoke**

```bash
sudo systemctl restart blackbox.service && sleep 70
curl -s http://localhost:9091/health | python3 -m json.tool | head -10
# Spot-check 2-3 endpoints that touched modified files
```

**Step 3: Commit**

```bash
git add <files>
git commit -m "refactor(paths): migrate B3 Python code to use BLACKBOX_ROOT path resolver

<list of files modified>
Eliminates hardcoded /home/ai-black-box-fc/ references in Python."
```

### Task 0.3.5: Apply Bucket B4 fixes — App HTML media URLs

**Files:** Every `Apps/*/index.html` from B4 (likely 5-10 files).

**Step 1: For each app HTML, replace absolute Tailscale URLs with relative paths:**

```html
<!-- OLD -->
<source src="https://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net/ui/uploads/foo.wav">

<!-- NEW -->
<source src="/ui/uploads/foo.wav">
```

When the app runs on a different port (8060-8099), make the URLs origin-aware via `<base href>` injected at app-load time:

```html
<head>
    <!-- Origin pinned to BlackBox Orchestrator at request time -->
    <base href="http://localhost:9091/">
    ...
</head>
```

OR (cleaner): a small JS shim at the top of each app:

```html
<script>
  // Apps run on ports 8060-8099; their /ui/uploads/ paths must resolve to the
  // Orchestrator on :9091. Inject a <base> tag matching the parent origin.
  (function() {
    const url = new URL(document.location.href);
    const orchOrigin = url.protocol + '//' + url.hostname + ':9091';
    const base = document.createElement('base');
    base.href = orchOrigin + '/';
    document.head.prepend(base);
  })();
</script>
```

**Step 2: Test each app loads its media**

```bash
# For each modified app, verify it serves and media plays
ss -tlnp | grep -E "8060|8061|8062|8063|8064|8065|8066|8067|8068|8069|8070"
# Visit each in browser and play a media element
```

**Step 3: Commit**

```bash
git add Apps/*/index.html
git commit -m "fix(apps): replace hardcoded Tailscale FQDN media URLs with origin-aware paths

Apps now resolve /ui/uploads/ via parent-origin <base> tag so they work on any
machine without depending on the specific Tailscale hostname."
```

### Task 0.3.6: Apply Bucket B5 fixes — Documentation cleanup

**Files:** Every `*.md` from B5 audit (likely `Claude.md`, `AUDIT_REPORT.md`, others).

**Step 1: Replace specific FQDN/path references with placeholders + an explanatory note at top:**

```markdown
> **Note:** Examples in this document use placeholder strings for portability:
> - `<BLACKBOX_ROOT>` — your project root (default `/home/<user>/blackbox_poc`)
> - `<TAILSCALE_HOSTNAME>` — your Tailscale machine FQDN (e.g. `mybox.tailXXXX.ts.net`)
> Substitute your actual values when copy-pasting commands.
```

Then s/`/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`/`<BLACKBOX_ROOT>`/g
And s/`ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net`/`<TAILSCALE_HOSTNAME>`/g

**Step 2: Commit**

```bash
git add *.md
git commit -m "docs: replace hardcoded paths/hostnames with portable placeholders

Adds explanatory note at top of each affected doc explaining placeholder
substitution. No content lost — examples remain functional after substitution."
```

### Task 0.3.7: Apply Bucket B1 + B2 fixes — systemd + config templating

This bucket involves install-time substitution rather than in-place edits, so it's deferred to Track 4 (Install scripts) where the install script writes the systemd unit + .mcp.json with substituted paths. Mark in audit doc as "deferred to Track 4."

## Phase 0.4: Pairing endpoint relocation + claim flow

**Background:** `/pair/start` is orphan-placed in `tts_routes.py:308-321`. There's no `/pair/claim` to validate token redemption. Move it and add the claim.

### Task 0.4.1: Create the pairing routes module

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/pairing_routes.py`

**Step 1: Write the new module — extract the existing logic + add claim flow:**

```python
"""Pairing routes — QR-based device pairing for AI BlackBox.

POST /pair/start   — Mint a one-time pairing token (TTL 5min).
POST /pair/claim   — Redeem a token (called by the claiming device).
GET  /pair/status  — Check if a token has been claimed (for poll-style UX).
GET  /pair/qr/{token} — Render PNG QR code for the token (server-side).
"""
from __future__ import annotations

import io
import secrets
import time
from typing import Optional

import qrcode
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/pair", tags=["pairing"])

PAIR_TOKEN_TTL_SECS = 300

# Token store: token -> {created_at, claimed_at, claimed_by}
# In-memory; tokens are short-lived. Restart-tolerant via TTL.
_pair_tokens: dict[str, dict] = {}


class PairStartResponse(BaseModel):
    type: str = "pair"
    token: str
    exp: int


class PairClaimRequest(BaseModel):
    token: str
    device_name: str
    device_kind: str  # "android", "desktop", "ios", etc.


class PairClaimResponse(BaseModel):
    success: bool
    operator: Optional[str] = None
    origin: Optional[str] = None


class PairStatusResponse(BaseModel):
    exists: bool
    claimed: bool
    claimed_by: Optional[str] = None
    expires_in: int


def _purge_expired() -> None:
    now = time.time()
    expired = [t for t, m in _pair_tokens.items() if now - m["created_at"] > PAIR_TOKEN_TTL_SECS]
    for t in expired:
        del _pair_tokens[t]


@router.post("/start", response_model=PairStartResponse)
def pair_start() -> PairStartResponse:
    _purge_expired()
    token = secrets.token_urlsafe(16)
    now = time.time()
    _pair_tokens[token] = {
        "created_at": now,
        "claimed_at": None,
        "claimed_by": None,
    }
    return PairStartResponse(token=token, exp=int(now + PAIR_TOKEN_TTL_SECS))


@router.post("/claim", response_model=PairClaimResponse)
def pair_claim(req: PairClaimRequest) -> PairClaimResponse:
    _purge_expired()
    meta = _pair_tokens.get(req.token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    if meta["claimed_at"]:
        raise HTTPException(status_code=409, detail="token already claimed")
    meta["claimed_at"] = time.time()
    meta["claimed_by"] = req.device_name
    # Pull operator + origin from /health-style config (kept minimal here)
    from Orchestrator.config import DEFAULT_OPERATOR, DEFAULT_ORIGIN  # added in 0.4.2
    return PairClaimResponse(success=True, operator=DEFAULT_OPERATOR, origin=DEFAULT_ORIGIN)


@router.get("/status", response_model=PairStatusResponse)
def pair_status(token: str) -> PairStatusResponse:
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        return PairStatusResponse(exists=False, claimed=False, expires_in=0)
    expires_in = max(0, int(PAIR_TOKEN_TTL_SECS - (time.time() - meta["created_at"])))
    return PairStatusResponse(
        exists=True,
        claimed=meta["claimed_at"] is not None,
        claimed_by=meta["claimed_by"],
        expires_in=expires_in,
    )


@router.get("/qr/{token}")
def pair_qr(token: str):
    """Render PNG QR for a pairing token. Replaces external api.qrserver.com."""
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    from Orchestrator.config import DEFAULT_OPERATOR, DEFAULT_ORIGIN
    payload = (
        '{"type":"pair","token":"' + token + '","exp":' + str(int(meta["created_at"] + PAIR_TOKEN_TTL_SECS))
        + ',"origin":"' + DEFAULT_ORIGIN + '","operator":"' + DEFAULT_OPERATOR + '"}'
    )
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
```

**Step 2: Wire it into `Orchestrator/app.py`** — find the section where other routers are included, add:

```python
from Orchestrator.routes import pairing_routes
app.include_router(pairing_routes.router)
```

**Step 3: Add `DEFAULT_OPERATOR` + `DEFAULT_ORIGIN` to config.py** if not already exported (read from `config.ini` or env).

**Step 4: Remove old `/pair/start` from `tts_routes.py:308-321`**

**Step 5: Restart + test**

```bash
sudo systemctl restart blackbox.service && sleep 70
curl -s -X POST http://localhost:9091/pair/start | python3 -m json.tool
# Capture the token, then:
TOKEN=$(curl -s -X POST http://localhost:9091/pair/start | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s "http://localhost:9091/pair/status?token=$TOKEN" | python3 -m json.tool
curl -s "http://localhost:9091/pair/qr/$TOKEN" -o /tmp/test-pair-qr.png
file /tmp/test-pair-qr.png  # should report PNG image
```

**Step 6: Commit**

```bash
git add Orchestrator/routes/pairing_routes.py Orchestrator/routes/tts_routes.py Orchestrator/app.py Orchestrator/config.py
git commit -m "feat(pairing): proper pairing routes with claim flow + server-side QR

- New Orchestrator/routes/pairing_routes.py owns /pair/{start,claim,status,qr/{token}}
- Token TTL 5min, single-use claim, in-memory store (restart-tolerant via TTL)
- Server-side QR generation via 'qrcode' lib — replaces external api.qrserver.com dependency
- Removed orphan /pair/start from tts_routes.py
- DEFAULT_OPERATOR + DEFAULT_ORIGIN added to config.py for claim/qr response payload"
```

### Task 0.4.2: Update Portal pairing UI to use server-side QR

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/modules/ui-setup.js` (lines ~788-820)
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/index.html` (line 759-771 #pairModal)

**Step 1: Replace the api.qrserver.com URL** with the new server-side endpoint:

```javascript
// OLD
const qrUrl = `https://api.qrserver.com/v1/create-qr-code/?data=${encodeURIComponent(payload)}&size=240x240`;

// NEW
const qrUrl = `/pair/qr/${token}`;  // token from /pair/start response
```

**Step 2: Test in browser** — open Portal, hit "Pair Device", confirm QR renders from local endpoint (no external network call).

**Step 3: Commit**

```bash
git add Portal/modules/ui-setup.js Portal/index.html
git commit -m "fix(portal): use local /pair/qr/{token} instead of api.qrserver.com

Portal pairing flow no longer depends on external QR-rendering service.
Works fully offline / on-prem."
```

---

## Track 0 Checkpoint Commit

After Phase 0.1 + 0.2 + 0.3 + 0.4 are done, the foundation is stable. Tag this checkpoint:

```bash
git tag -a track0-foundation-complete -m "Foundation cleanup complete — onboarding UI can build on top.

Phase 0.1: requirements.txt pinned, system-packages.txt manifest
Phase 0.2: 9 stragglers refactored, GEMINI_API_KEY in central config, 3 orphans quarantined
Phase 0.3: BLACKBOX_ROOT + paths.py utility, 194 hardcoded paths bucketed, B3+B4+B5 fixed
Phase 0.4: pairing_routes.py with claim flow + server-side QR, Portal updated

Remaining for later tracks: B1 systemd + B2 config templating happens in Track 4."
git push origin track0-foundation-complete
```

---

# TRACK 1 — ONBOARDING BACKEND

**Goal:** FastAPI routes the Portal wizard talks to. State endpoint (is onboarding done?), per-provider validators, save endpoint (write secrets to .env atomically), complete/skip endpoints.

**Estimated effort:** 1 week
**Dependencies:** Track 0
**Outcome:** All wizard steps have a backend to call. Each provider validator does a 1-token cost call to confirm a key works.

## Phase 1.1: First-run state detection

### Task 1.1.1: Create the onboarding package

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/onboarding/__init__.py`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/onboarding/state.py`

**Step 1: Write `state.py`:**

```python
"""Onboarding state — first-run detection and step-completion tracking."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from Orchestrator.utils.paths import resolve

STATE_FILE = resolve(".onboarding_state.json")
COMPLETE_SENTINEL = resolve(".onboarding_complete")

StepName = Literal[
    "welcome",
    "tailscale",
    "api_keys",
    "optional_integrations",
    "pair_phone",
    "operator",
    "done",
]

ALL_STEPS: list[StepName] = [
    "welcome", "tailscale", "api_keys",
    "optional_integrations", "pair_phone", "operator", "done",
]


class OnboardingState:
    """Persistent onboarding progress state.

    Stored as JSON in {BLACKBOX_ROOT}/.onboarding_state.json.
    Marker file {BLACKBOX_ROOT}/.onboarding_complete signals 'done' to other code.
    """

    def __init__(self) -> None:
        self._data: dict = self._load()

    def _load(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {
            "started_at": time.time(),
            "completed_steps": [],
            "skipped_steps": [],
            "current_step": "welcome",
        }

    def _save(self) -> None:
        STATE_FILE.write_text(json.dumps(self._data, indent=2))

    def is_complete(self) -> bool:
        return COMPLETE_SENTINEL.exists()

    def mark_step_complete(self, step: StepName) -> None:
        if step not in self._data["completed_steps"]:
            self._data["completed_steps"].append(step)
        if step in self._data["skipped_steps"]:
            self._data["skipped_steps"].remove(step)
        self._save()

    def mark_step_skipped(self, step: StepName) -> None:
        if step not in self._data["skipped_steps"]:
            self._data["skipped_steps"].append(step)
        if step in self._data["completed_steps"]:
            self._data["completed_steps"].remove(step)
        self._save()

    def set_current(self, step: StepName) -> None:
        self._data["current_step"] = step
        self._save()

    def mark_complete(self) -> None:
        """Final marker — wizard is done."""
        COMPLETE_SENTINEL.write_text(f"completed_at={int(time.time())}\n")
        self._data["completed_at"] = time.time()
        self._save()

    def reset(self) -> None:
        """Clear onboarding state (for re-runs)."""
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        if COMPLETE_SENTINEL.exists():
            COMPLETE_SENTINEL.unlink()
        self._data = self._load()

    def snapshot(self) -> dict:
        """Return state dict for /onboarding/state response."""
        return {
            "is_complete": self.is_complete(),
            "completed_steps": self._data["completed_steps"],
            "skipped_steps": self._data["skipped_steps"],
            "current_step": self._data["current_step"],
            "all_steps": ALL_STEPS,
        }
```

**Step 2: Verify**

```bash
Orchestrator/venv/bin/python -c "
from Orchestrator.onboarding.state import OnboardingState, ALL_STEPS
s = OnboardingState()
print('is_complete:', s.is_complete())
print('snapshot:', s.snapshot())
print('all_steps:', ALL_STEPS)
"
```

**Step 3: Commit**

```bash
git add Orchestrator/onboarding/__init__.py Orchestrator/onboarding/state.py
git commit -m "feat(onboarding): state tracking module — step completion/skip + done sentinel"
```

## Phase 1.2: Per-provider validators

### Task 1.2.1: Create validators module skeleton

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/onboarding/validators.py`

**Step 1: Write the skeleton with one validator per Tier-1 integration:**

```python
"""Per-provider key validators.

Each validator does a CHEAP call (1 token cost or one cheap metadata API)
to confirm the supplied credential works. Returns ValidationResult with
ok/error/latency_ms so the wizard can show clean per-provider feedback.

Tier-1 (v1 wizard): OpenAI, Anthropic, Google, Tailscale, Gmail.
Tier-2 (v1.1): Twilio, ElevenLabs, Asterisk, xAI, Perplexity.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ValidationResult:
    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict[str, Any] | None = None


def _measure(fn) -> ValidationResult:
    """Wrap a sync validator with latency measurement + error capture."""
    start = time.perf_counter()
    try:
        detail = fn()
        return ValidationResult(
            ok=True,
            latency_ms=int((time.perf_counter() - start) * 1000),
            detail=detail,
        )
    except Exception as e:
        return ValidationResult(
            ok=False,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


# ──────────────────────────── Tier-1 ────────────────────────────

def validate_openai(api_key: str) -> ValidationResult:
    """Validate OpenAI API key via models.list (no token cost)."""
    def _fn():
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        models = client.models.list()
        return {"model_count": len(list(models.data))}
    return _measure(_fn)


def validate_anthropic(api_key: str) -> ValidationResult:
    """Validate Anthropic key via cheapest-possible message (1-token completion)."""
    def _fn():
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return {"model": resp.model, "id": resp.id}
    return _measure(_fn)


def validate_google(api_key: str) -> ValidationResult:
    """Validate Google AI key via list_models."""
    def _fn():
        from google import genai
        client = genai.Client(api_key=api_key)
        models = list(client.models.list())
        return {"model_count": len(models)}
    return _measure(_fn)


def validate_tailscale() -> ValidationResult:
    """Validate Tailscale install + auth via 'tailscale status --json'."""
    def _fn():
        if not shutil.which("tailscale"):
            raise RuntimeError("tailscale binary not found on PATH")
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tailscale status failed: {result.stderr.strip()}")
        import json as _json
        data = _json.loads(result.stdout)
        backend = data.get("BackendState", "unknown")
        if backend != "Running":
            raise RuntimeError(f"tailscale not running (BackendState={backend})")
        self_node = data.get("Self", {})
        return {
            "hostname": self_node.get("DNSName", "").rstrip("."),
            "ip": (self_node.get("TailscaleIPs") or ["unknown"])[0],
            "online": self_node.get("Online", False),
        }
    return _measure(_fn)


def validate_gmail_oauth(client_id: str, client_secret: str) -> ValidationResult:
    """Validate Gmail OAuth client by attempting to construct an OAuth flow object.
    Does NOT trigger interactive auth — that happens in the wizard browser frame.
    """
    def _fn():
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            {"web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:9091/auth/gmail/callback"],
            }},
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        # Just verify the URL can be constructed
        url, _ = flow.authorization_url()
        return {"auth_url_prefix": url.split("?")[0]}
    return _measure(_fn)


```

> **Note (2026-05-11 audit reversal):** Twilio is intentionally NOT a Tier-1 validator in v1. Phone calls + SMS are handled by the **TG200 cellular modem** (auto-detected via `Orchestrator/cellular/hotplug.py` — no creds to collect). A `validate_twilio` function may be added in v1.1 once the wizard offers a Twilio second-path. Do not import the `twilio` SDK here.

**Step 2: Verify with the actual current keys (sanity check)**

```bash
Orchestrator/venv/bin/python -c "
from Orchestrator.onboarding.validators import (
    validate_openai, validate_anthropic, validate_google, validate_tailscale, validate_gmail_oauth
)
from Orchestrator.config import (
    OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
)
print('OpenAI:', validate_openai(OPENAI_API_KEY))
print('Anthropic:', validate_anthropic(ANTHROPIC_API_KEY))
print('Google:', validate_google(GOOGLE_API_KEY))
print('Tailscale:', validate_tailscale())
# Gmail OAuth requires client_id + client_secret — load from .env if present, else skip
import os
gid, gsec = os.getenv('GMAIL_CLIENT_ID'), os.getenv('GMAIL_CLIENT_SECRET')
if gid and gsec:
    print('Gmail OAuth:', validate_gmail_oauth(gid, gsec))
else:
    print('Gmail OAuth: SKIPPED (no GMAIL_CLIENT_ID/SECRET in .env yet)')
"
```

Expected: 4-5 print `ValidationResult(ok=True, latency_ms=…, detail={…})` (Gmail row only if creds exist).

**Step 3: Commit**

```bash
git add Orchestrator/onboarding/validators.py
git commit -m "feat(onboarding): tier-1 per-provider validators (OpenAI/Anthropic/Google/Tailscale/Gmail)

Each does a cheap 1-token (or metadata-only) call to confirm the credential works.
Returns ValidationResult{ok, latency_ms, error?, detail?} for clean wizard UX.
Twilio explicitly deferred to Tier-2 (v1.1) — TG200 cellular modem handles
phone+SMS in v1, no Twilio webhooks needed."
```

## Phase 1.3: Onboarding routes

### Task 1.3.1: Build the onboarding routes module

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/onboarding/secrets_writer.py`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/onboarding_routes.py`

**Step 1: Write `secrets_writer.py` — atomic .env update with backup:**

```python
"""Atomic .env file writer with backup."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from Orchestrator.utils.paths import resolve

ENV_FILE = resolve(".env")


def update_env(updates: dict[str, str]) -> dict:
    """Atomically update key=value pairs in .env, preserving structure.

    - Existing keys: replaced in-place (preserves comment ordering)
    - New keys: appended
    - Backup created at .env.backup.<timestamp> before writing
    """
    if not ENV_FILE.exists():
        ENV_FILE.touch()

    # Backup
    ts = int(time.time())
    backup = ENV_FILE.with_suffix(f".backup.{ts}")
    shutil.copy2(ENV_FILE, backup)

    # Read + update
    lines = ENV_FILE.read_text().splitlines(keepends=True)
    seen_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                seen_keys.add(k)
                continue
        new_lines.append(line)

    # Append new keys
    new_keys = [k for k in updates if k not in seen_keys]
    if new_keys:
        new_lines.append("\n# Added by onboarding wizard\n")
        for k in new_keys:
            new_lines.append(f"{k}={updates[k]}\n")

    # Atomic write via tmp + rename
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("".join(new_lines))
    os.replace(tmp, ENV_FILE)

    return {"backup": str(backup), "updated_keys": list(updates.keys())}
```

**Step 2: Write `onboarding_routes.py`:**

```python
"""Onboarding wizard backend routes.

Mounted at /onboarding/* by Orchestrator/app.py.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator.onboarding import validators
from Orchestrator.onboarding.secrets_writer import update_env
from Orchestrator.onboarding.state import (
    OnboardingState,
    StepName,
    ALL_STEPS,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

_state = OnboardingState()


class StateResponse(BaseModel):
    is_complete: bool
    completed_steps: list[str]
    skipped_steps: list[str]
    current_step: str
    all_steps: list[str]


class ValidateRequest(BaseModel):
    provider: Literal["openai", "anthropic", "google", "tailscale", "gmail"]
    credentials: dict[str, str]  # provider-specific shape


class ValidateResponse(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict | None = None


class SaveRequest(BaseModel):
    secrets: dict[str, str]  # env-var name -> value


class StepActionRequest(BaseModel):
    step: StepName


@router.get("/state", response_model=StateResponse)
def get_state() -> StateResponse:
    return StateResponse(**_state.snapshot())


@router.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest) -> ValidateResponse:
    creds = req.credentials
    if req.provider == "openai":
        result = validators.validate_openai(creds["api_key"])
    elif req.provider == "anthropic":
        result = validators.validate_anthropic(creds["api_key"])
    elif req.provider == "google":
        result = validators.validate_google(creds["api_key"])
    elif req.provider == "tailscale":
        result = validators.validate_tailscale()
    elif req.provider == "gmail":
        result = validators.validate_gmail_oauth(creds["client_id"], creds["client_secret"])
    else:
        raise HTTPException(status_code=400, detail=f"unknown provider {req.provider}")
    return ValidateResponse(**vars(result))


@router.post("/save")
def save_secrets(req: SaveRequest) -> dict:
    return update_env(req.secrets)


@router.post("/step/complete")
def step_complete(req: StepActionRequest) -> dict:
    _state.mark_step_complete(req.step)
    return _state.snapshot()


@router.post("/step/skip")
def step_skip(req: StepActionRequest) -> dict:
    _state.mark_step_skipped(req.step)
    return _state.snapshot()


@router.post("/complete")
def complete() -> dict:
    """Mark onboarding fully complete — sentinel file written."""
    _state.mark_complete()
    return {"ok": True, "is_complete": True}


@router.post("/reset")
def reset() -> dict:
    """Reset onboarding (for testing or re-onboarding)."""
    _state.reset()
    return _state.snapshot()
```

**Step 3: Wire it into `Orchestrator/app.py`:**

```python
from Orchestrator.routes import onboarding_routes
app.include_router(onboarding_routes.router)
```

**Step 4: Restart and probe**

```bash
sudo systemctl restart blackbox.service && sleep 70
curl -s http://localhost:9091/onboarding/state | python3 -m json.tool
curl -s -X POST http://localhost:9091/onboarding/validate \
  -H "Content-Type: application/json" \
  -d "{\"provider\":\"openai\",\"credentials\":{\"api_key\":\"$OPENAI_API_KEY\"}}" \
  | python3 -m json.tool
```

Expected: state JSON with empty completed_steps + current_step="welcome"; openai validation returns `ok=true, latency_ms<2000`.

**Step 5: Commit**

```bash
git add Orchestrator/onboarding/secrets_writer.py Orchestrator/routes/onboarding_routes.py Orchestrator/app.py
git commit -m "feat(onboarding): backend routes — state, validate, save, step actions, complete

POST /onboarding/state — wizard reads completion state
POST /onboarding/validate — per-provider key validation (1-token cost)
POST /onboarding/save — atomic .env update with backup
POST /onboarding/step/{complete,skip} — track per-step progress
POST /onboarding/complete — write done sentinel
POST /onboarding/reset — for testing"
```

## Phase 1.4: Maintenance backend (NEW per audit 2026-05-10)

**Background:** The wizard supports two modes: `?mode=setup` (first-run, default) and `?mode=manage` (post-onboarding maintenance). Manage mode needs three backend additions: a redacted config snapshot, a per-key reveal endpoint (for the eye-icon toggle), and a per-key remove endpoint. Plus validation timestamps in `OnboardingState` so the UI can render "validated 3 days ago".

### Task 1.4.1: Track validation timestamps in OnboardingState

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/onboarding/state.py`

**Step 1: Add `validated_at` map to state JSON.** Extend `_load()` default to include:

```python
return {
    "started_at": time.time(),
    "completed_steps": [],
    "skipped_steps": [],
    "current_step": "welcome",
    "validated_at": {},  # NEW: provider name -> unix timestamp of last successful validation
}
```

**Step 2: Add a helper:**

```python
def record_validation(self, provider: str) -> None:
    """Stamp this provider as freshly validated. Called from /onboarding/validate when ok=true."""
    self._data.setdefault("validated_at", {})[provider] = time.time()
    self._save()

def validated_at(self) -> dict[str, float]:
    return dict(self._data.get("validated_at", {}))
```

**Step 3: Wire into existing /onboarding/validate endpoint** (modify `Orchestrator/routes/onboarding_routes.py`):

```python
@router.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest) -> ValidateResponse:
    # ... existing validator dispatch ...
    if result.ok:
        _state.record_validation(req.provider)
    return ValidateResponse(**vars(result))
```

**Step 4: Test**

```bash
sudo systemctl restart blackbox.service && sleep 70
curl -X POST http://localhost:9091/onboarding/validate \
  -H "Content-Type: application/json" \
  -d "{\"provider\":\"openai\",\"credentials\":{\"api_key\":\"$OPENAI_API_KEY\"}}"
cat .onboarding_state.json | python3 -m json.tool | grep validated_at
```

Expected: `validated_at: {"openai": 1747...}` (unix timestamp).

**Step 5: Commit**

```bash
git add Orchestrator/onboarding/state.py Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): track per-provider validation timestamps for manage-mode UI"
```

### Task 1.4.2: GET /onboarding/current-config endpoint

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/onboarding_routes.py`

**Step 1: Add the endpoint:**

```python
class CurrentConfigResponse(BaseModel):
    """Redacted snapshot of what's configured. Sensitive values shown as last-4 only.
    Use /onboarding/config/{key}?reveal=1 to fetch full value of a single key.
    """
    providers: dict[str, dict]  # provider name -> {present, last4, validated_at, ...}
    operators: list[str]
    paired_devices: list[dict]
    tailscale: dict
    onboarding_state: dict


def _redact(value: str | None, keep: int = 4) -> str | None:
    """Show last N chars only; full mask if shorter than 2*keep."""
    if not value:
        return None
    if len(value) < 2 * keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


@router.get("/current-config", response_model=CurrentConfigResponse)
def current_config() -> CurrentConfigResponse:
    from Orchestrator.config import (
        OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
        GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
    )
    val_at = _state.validated_at()
    providers = {
        "openai": {
            "present": bool(OPENAI_API_KEY),
            "last4": _redact(OPENAI_API_KEY),
            "validated_at": val_at.get("openai"),
        },
        "anthropic": {
            "present": bool(ANTHROPIC_API_KEY),
            "last4": _redact(ANTHROPIC_API_KEY),
            "validated_at": val_at.get("anthropic"),
        },
        "google": {
            "present": bool(GOOGLE_API_KEY),
            "last4": _redact(GOOGLE_API_KEY),
            "validated_at": val_at.get("google"),
        },
        "gmail": {
            "present": bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET),
            "client_id": GOOGLE_OAUTH_CLIENT_ID or None,  # not secret per Google docs
            "secret_last4": _redact(GOOGLE_OAUTH_CLIENT_SECRET),
            "validated_at": val_at.get("gmail"),
        },
    }
    # Tailscale (hostname is not secret)
    try:
        from Orchestrator.onboarding.validators import validate_tailscale
        ts_result = validate_tailscale()
        tailscale = {"configured": ts_result.ok, "detail": ts_result.detail or {}}
    except Exception:
        tailscale = {"configured": False, "detail": {}}
    # Operators — read from operator registry (location TBD; use the same source ChatScreen reads from)
    try:
        from Orchestrator.routes.admin_routes import _list_operators  # or wherever
        operators = _list_operators()
    except Exception:
        operators = []
    # Paired devices
    paired_devices: list[dict] = []  # TODO: implement when pairing claim store persists devices
    return CurrentConfigResponse(
        providers=providers,
        operators=operators,
        paired_devices=paired_devices,
        tailscale=tailscale,
        onboarding_state=_state.snapshot(),
    )
```

**Step 2: Test**

```bash
sudo systemctl restart blackbox.service && sleep 70
curl -s http://localhost:9091/onboarding/current-config | python3 -m json.tool
```

Expected: JSON with all providers, redacted values (e.g., `"last4": "••••••••sk-...XYZW"`), validated_at timestamps where present.

**Step 3: Commit**

```bash
git add Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): GET /current-config — redacted snapshot for manage-mode UI"
```

### Task 1.4.3: GET /onboarding/config/{key}?reveal=1 single-key reveal

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/onboarding_routes.py`

**Step 1: Add the endpoint** (loopback-only by design — only callable from the Tauri shell or browser on the same host):

```python
ALLOWED_REVEAL_KEYS = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
}


@router.get("/config/{key}")
def get_config_value(key: str, request: Request, reveal: int = 0) -> dict:
    """Return a single config value. With ?reveal=1, returns full cleartext.
    Loopback-only when revealing — refuses if request not from 127.0.0.1 / ::1.
    """
    if key not in ALLOWED_REVEAL_KEYS:
        raise HTTPException(status_code=403, detail=f"key {key} not in reveal allowlist")
    if reveal:
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="reveal only permitted from loopback")
    import os
    value = os.getenv(key, "")
    if reveal:
        return {"key": key, "value": value, "present": bool(value)}
    return {"key": key, "value": _redact(value), "present": bool(value)}
```

**Step 2: Test**

```bash
sudo systemctl restart blackbox.service && sleep 70
# Redacted by default
curl -s http://localhost:9091/onboarding/config/OPENAI_API_KEY | python3 -m json.tool
# Revealed (loopback only — works from same machine)
curl -s "http://localhost:9091/onboarding/config/OPENAI_API_KEY?reveal=1" | python3 -m json.tool
```

Expected: redacted variant shows `••••••••XYZW`; reveal variant shows full key.

**Step 3: Commit**

```bash
git add Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): GET /config/{key}?reveal=1 — single-key reveal for masking toggle

Loopback-restricted by design. Refuses reveal from non-127.0.0.1 origins."
```

### Task 1.4.4: DELETE /onboarding/config/{key} remove endpoint

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/onboarding/secrets_writer.py`
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/routes/onboarding_routes.py`

**Step 1: Add `remove_env_keys()` to secrets_writer.py:**

```python
def remove_env_keys(keys: list[str]) -> dict:
    """Atomically blank specified keys in .env. Backup created first."""
    if not ENV_FILE.exists():
        return {"backup": None, "removed_keys": []}
    ts = int(time.time())
    backup = ENV_FILE.with_suffix(f".backup.{ts}")
    shutil.copy2(ENV_FILE, backup)
    lines = ENV_FILE.read_text().splitlines(keepends=True)
    out: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in keys:
                removed.append(k)
                continue  # drop the line entirely
        out.append(line)
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("".join(out))
    os.replace(tmp, ENV_FILE)
    return {"backup": str(backup), "removed_keys": removed}
```

**Step 2: Add the DELETE endpoint:**

```python
@router.delete("/config/{key}")
def delete_config_value(key: str) -> dict:
    if key not in ALLOWED_REVEAL_KEYS:
        raise HTTPException(status_code=403, detail=f"key {key} not in allowlist")
    from Orchestrator.onboarding.secrets_writer import remove_env_keys
    result = remove_env_keys([key])
    return {"ok": True, **result}
```

**Step 3: Test**

```bash
sudo systemctl restart blackbox.service && sleep 70
# Add a fake key
echo "TEST_REMOVABLE=should_be_gone" >> .env
# Skip the allowlist check by adding TEST_REMOVABLE to ALLOWED_REVEAL_KEYS first, OR just verify via existing key
# (Recommended: test against a key we don't mind clearing temporarily, e.g. PERPLEXITY_API_KEY if unused)
curl -s -X DELETE http://localhost:9091/onboarding/config/PERPLEXITY_API_KEY | python3 -m json.tool
grep PERPLEXITY .env  # should be gone
ls .env.backup.*  # backup created
```

Expected: `removed_keys: ["PERPLEXITY_API_KEY"]`, line absent from .env, backup file present.

**Step 4: Commit**

```bash
git add Orchestrator/onboarding/secrets_writer.py Orchestrator/routes/onboarding_routes.py
git commit -m "feat(onboarding): DELETE /config/{key} — remove a secret with .env backup"
```

---

### Task 1.3.2: Add first-run middleware/redirect

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/app.py`

**Step 1: Add middleware that redirects `/ui` to `/onboarding` if not complete:**

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse
from Orchestrator.onboarding.state import OnboardingState

_onboarding_state = OnboardingState()

class FirstRunMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Only redirect on /ui or /ui/index.html requests
        path = request.url.path
        if path in ("/ui", "/ui/", "/ui/index.html") and not _onboarding_state.is_complete():
            return RedirectResponse(url="/onboarding/", status_code=307)
        return await call_next(request)

app.add_middleware(FirstRunMiddleware)
```

**Step 2: Test**

```bash
sudo systemctl restart blackbox.service && sleep 70
# Reset onboarding to test redirect
curl -X POST http://localhost:9091/onboarding/reset
# This should now 307 → /onboarding/
curl -sI http://localhost:9091/ui | head -3
# Mark complete
curl -X POST http://localhost:9091/onboarding/complete
# Now /ui should serve normally
curl -sI http://localhost:9091/ui | head -3
```

**Step 3: Commit**

```bash
git add Orchestrator/app.py
git commit -m "feat(onboarding): first-run middleware redirects /ui to /onboarding when incomplete

Customer's mini-PC autostart will hit /ui; gets transparently redirected to
the wizard until /onboarding/complete is called."
```

---

# TRACK 2 — ONBOARDING PORTAL UI

**Goal:** The actual wizard UI customers see. Modernized, branded, step-by-step, resumable. Lives at `/onboarding/` — Tauri shell points its webview here.

**Estimated effort:** 2-3 weeks
**Dependencies:** Track 1
**Outcome:** A polished customer-facing wizard with welcome, Tailscale, API keys, optional integrations, phone pairing, operator setup, and done steps.

**Aesthetic direction (locked 2026-05-11):** Portal-adjacent, elevated. Inherit Portal's pure-black + red-accent palette but push hard on distinctive display typography (Fraunces variable serif), monospace body (JetBrains Mono), asymmetric layouts, and choreographed motion for the welcome moment. Customer feels continuity with Portal but the onboarding has its own gravitas. Mood: editorial gravitas + engineer's precision — "your AI infrastructure, treated with the gravity it deserves."

## Phase 2.0: Design system prep (NEW per audit 2026-05-11)

**Goal:** Lock the visual design system BEFORE wiring up the wizard, so each step component inherits a consistent identity rather than getting a polish pass at the end. Produces (a) self-hosted font files (no internet required during customer install — critical, since the wizard IS where the customer sets up Tailscale + API keys), (b) wizard-specific CSS tokens extending Portal's `_variables.css`, and (c) two static reference mocks (welcome + tailscale) Brandon previews in-browser before T2.1.1 dispatches.

### Task 2.0.1: Self-host fonts + tokens + reference mocks

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/fonts/Fraunces-VariableFont_SOFT,WONK,opsz,wght.woff2` (download from Google Fonts variable axes)
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/fonts/JetBrainsMono-VariableFont_wght.woff2` (download from Google Fonts)
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/fonts/LICENSES.md` (font license text from Google Fonts — both SIL OFL 1.1)
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding-tokens.css`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/_mocks/welcome.html`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/_mocks/tailscale.html`

**Step 1: Download Fraunces and JetBrains Mono variable WOFF2 files.**

```bash
mkdir -p Portal/onboarding/fonts
cd Portal/onboarding/fonts

# Fraunces variable (display) — full axes: SOFT, WONK, opsz, wght
curl -fsSL -o Fraunces.woff2 \
  "https://github.com/google/fonts/raw/main/ofl/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf"

# JetBrains Mono variable (body) — wght axis
curl -fsSL -o JetBrainsMono.woff2 \
  "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/variable/JetBrainsMono%5Bwght%5D.ttf"
```

(If the .ttf-to-.woff2 conversion isn't trivial, accept .ttf for v1 and convert later. Browsers handle .ttf fine; .woff2 is just smaller.)

Write `LICENSES.md` with both fonts' SIL OFL 1.1 license text + attribution.

**Step 2: Write `Portal/onboarding/onboarding-tokens.css`** — extends Portal's `_variables.css` with wizard-specific tokens:

```css
/* onboarding-tokens.css
 * Wizard-specific design tokens. Extends Portal's _variables.css with
 * typography, spacing, motion, and accent treatments tailored to the
 * customer-facing first-run + manage-mode experience.
 *
 * Aesthetic: Portal-adjacent (pure black + red-accent inherited) but
 * elevated with editorial-grade display typography and engineer-precision
 * monospace body. Mood: New Yorker meets terminal.
 */

/* ==== Self-hosted fonts (offline-safe for customer install) ==== */

@font-face {
    font-family: "Fraunces";
    src: url("./fonts/Fraunces.woff2") format("woff2-variations"),
         url("./fonts/Fraunces.ttf") format("truetype-variations");
    font-weight: 100 900;
    font-style: normal;
    font-display: swap;
}

@font-face {
    font-family: "JetBrains Mono";
    src: url("./fonts/JetBrainsMono.woff2") format("woff2-variations"),
         url("./fonts/JetBrainsMono.ttf") format("truetype-variations");
    font-weight: 100 800;
    font-style: normal;
    font-display: swap;
}

/* ==== Wizard-specific tokens (extending Portal _variables.css) ==== */

:root {
    /* Typography stack */
    --ob-font-display: "Fraunces", Georgia, serif;
    --ob-font-body: "JetBrains Mono", ui-monospace, monospace;

    /* Type scale (modular, 1.333 ratio anchored at body 16px) */
    --ob-text-xs: 0.75rem;     /* 12px - meta/labels */
    --ob-text-sm: 0.875rem;    /* 14px - secondary body */
    --ob-text-base: 1rem;      /* 16px - body */
    --ob-text-lg: 1.333rem;    /* 21.3px - emphasis */
    --ob-text-xl: 1.777rem;    /* 28.4px - small heading */
    --ob-text-2xl: 2.369rem;   /* 37.9px - step heading */
    --ob-text-3xl: 3.157rem;   /* 50.5px - welcome display */
    --ob-text-4xl: 4.209rem;   /* 67.3px - hero display */

    /* Display weight (Fraunces variable axis) */
    --ob-display-weight-light: 300;
    --ob-display-weight-regular: 400;
    --ob-display-weight-bold: 700;
    --ob-display-soft: 30;     /* Fraunces SOFT axis (0-100) — slight softness */

    /* Spacing scale (more generous than Portal's chat density) */
    --ob-space-1: 0.25rem;
    --ob-space-2: 0.5rem;
    --ob-space-3: 0.75rem;
    --ob-space-4: 1rem;
    --ob-space-6: 1.5rem;
    --ob-space-8: 2rem;
    --ob-space-12: 3rem;
    --ob-space-16: 4rem;
    --ob-space-24: 6rem;
    --ob-space-32: 8rem;

    /* Motion timings (longer for "moments", faster for confirmations) */
    --ob-motion-instant: 80ms;
    --ob-motion-fast: 180ms;
    --ob-motion-base: 320ms;
    --ob-motion-slow: 560ms;
    --ob-motion-moment: 900ms;     /* welcome reveal, step transitions */
    --ob-ease-out: cubic-bezier(0.16, 1, 0.3, 1);
    --ob-ease-in-out: cubic-bezier(0.45, 0, 0.55, 1);

    /* Accent treatments */
    --ob-accent: var(--accent, #ff4a4a);
    --ob-accent-glow: 0 0 32px rgba(255, 74, 74, 0.18);
    --ob-success: #27d980;        /* validation success — complement to red */
    --ob-success-glow: 0 0 24px rgba(39, 217, 128, 0.20);
    --ob-warning: #ffb84a;
    --ob-error: #ff4a4a;          /* same as accent — error is the dominant signal */

    /* Surface treatments */
    --ob-surface-bg: #000000;
    --ob-surface-elevated: #0a0a0a;
    --ob-surface-card: #141414;
    --ob-surface-border: #222222;
    --ob-surface-border-emphasis: #333333;

    /* Layout constraints */
    --ob-content-max-width: 56rem;       /* ~896px — wider than Portal's chat */
    --ob-content-narrow: 36rem;          /* ~576px — for forms */
    --ob-step-min-height: 60vh;
}
```

**Step 3: Write `Portal/onboarding/_mocks/welcome.html`** — STATIC mock of the welcome step in final visual form. Goal: Brandon can open this in a browser and SEE the design before any production code is written.

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI BlackBox · Welcome (Mock)</title>
    <link rel="stylesheet" href="../onboarding-tokens.css">
    <style>
        /* Mock-specific reset + welcome-step rendering */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: var(--ob-surface-bg);
            color: var(--text, #ffffff);
            font-family: var(--ob-font-body);
            font-size: var(--ob-text-base);
            line-height: 1.6;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        /* Header with progress bar */
        .ob-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: var(--ob-space-6) var(--ob-space-8);
            border-bottom: 1px solid var(--ob-surface-border);
        }
        .ob-brand {
            font-family: var(--ob-font-display);
            font-weight: var(--ob-display-weight-bold);
            font-size: var(--ob-text-lg);
            font-variation-settings: "SOFT" var(--ob-display-soft);
            letter-spacing: -0.02em;
        }
        .ob-progress {
            display: flex;
            flex-direction: column;
            gap: var(--ob-space-2);
            min-width: 240px;
        }
        .ob-progress-bar-track {
            height: 2px;
            background: var(--ob-surface-border);
            border-radius: 0;
            overflow: hidden;
        }
        .ob-progress-bar {
            height: 100%;
            width: 14.3%;  /* 1 of 7 steps */
            background: var(--ob-accent);
            box-shadow: var(--ob-accent-glow);
            transition: width var(--ob-motion-base) var(--ob-ease-out);
        }
        .ob-progress-text {
            font-size: var(--ob-text-xs);
            color: var(--muted, #c9c9c9);
            text-align: right;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }
        /* Welcome step content */
        .ob-step {
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: center;
            max-width: var(--ob-content-max-width);
            margin: 0 auto;
            padding: var(--ob-space-16) var(--ob-space-8);
            min-height: var(--ob-step-min-height);
        }
        .ob-step-eyebrow {
            font-family: var(--ob-font-body);
            font-size: var(--ob-text-xs);
            text-transform: uppercase;
            letter-spacing: 0.15em;
            color: var(--ob-accent);
            margin-bottom: var(--ob-space-4);
            animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) 100ms backwards;
        }
        .ob-step-title {
            font-family: var(--ob-font-display);
            font-weight: var(--ob-display-weight-light);
            font-size: var(--ob-text-4xl);
            line-height: 1.05;
            letter-spacing: -0.025em;
            font-variation-settings: "opsz" 144, "SOFT" var(--ob-display-soft);
            margin-bottom: var(--ob-space-6);
            animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) 200ms backwards;
        }
        .ob-step-title em {
            font-style: italic;
            color: var(--ob-accent);
        }
        .ob-step-lede {
            font-family: var(--ob-font-body);
            font-size: var(--ob-text-lg);
            line-height: 1.5;
            color: var(--neutral-900, #cccccc);
            max-width: var(--ob-content-narrow);
            margin-bottom: var(--ob-space-12);
            animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) 350ms backwards;
        }
        .ob-features {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: var(--ob-space-6);
            margin-bottom: var(--ob-space-12);
        }
        .ob-feature {
            display: flex;
            gap: var(--ob-space-4);
            align-items: flex-start;
            padding: var(--ob-space-4) 0;
            border-top: 1px solid var(--ob-surface-border);
            animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) backwards;
        }
        .ob-feature:nth-child(1) { animation-delay: 500ms; }
        .ob-feature:nth-child(2) { animation-delay: 600ms; }
        .ob-feature:nth-child(3) { animation-delay: 700ms; }
        .ob-feature:nth-child(4) { animation-delay: 800ms; }
        .ob-feature-num {
            font-family: var(--ob-font-display);
            font-size: var(--ob-text-xl);
            font-weight: var(--ob-display-weight-light);
            color: var(--ob-accent);
            font-variation-settings: "opsz" 60;
            line-height: 1;
            min-width: 2ch;
        }
        .ob-feature-body {
            font-size: var(--ob-text-sm);
            line-height: 1.5;
        }
        .ob-feature-label {
            color: var(--text);
            font-weight: 600;
            display: block;
            margin-bottom: var(--ob-space-1);
        }
        .ob-feature-desc {
            color: var(--neutral-700, #888888);
        }
        /* Primary CTA */
        .ob-cta {
            display: inline-flex;
            align-items: center;
            gap: var(--ob-space-3);
            padding: var(--ob-space-4) var(--ob-space-8);
            background: var(--ob-accent);
            color: var(--neutral-0);
            font-family: var(--ob-font-body);
            font-size: var(--ob-text-base);
            font-weight: 600;
            letter-spacing: 0.02em;
            border: none;
            cursor: pointer;
            transition: all var(--ob-motion-fast) var(--ob-ease-out);
            box-shadow: var(--ob-accent-glow);
            animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) 950ms backwards;
            align-self: flex-start;
        }
        .ob-cta:hover {
            transform: translate(-2px, -2px);
            box-shadow: 4px 4px 0 var(--neutral-0), 0 0 48px rgba(255, 74, 74, 0.32);
        }
        .ob-cta-arrow {
            font-family: var(--ob-font-display);
            font-style: italic;
            transition: transform var(--ob-motion-fast) var(--ob-ease-out);
        }
        .ob-cta:hover .ob-cta-arrow {
            transform: translateX(4px);
        }
        /* Reveal animation */
        @keyframes ob-fade-up {
            from { opacity: 0; transform: translateY(16px); }
            to   { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body>
    <header class="ob-header">
        <div class="ob-brand">AI BlackBox</div>
        <div class="ob-progress">
            <div class="ob-progress-bar-track">
                <div class="ob-progress-bar"></div>
            </div>
            <div class="ob-progress-text">Step 1 of 7</div>
        </div>
    </header>
    <main class="ob-step">
        <div class="ob-step-eyebrow">First-run setup</div>
        <h1 class="ob-step-title">Welcome to your <em>private</em> AI infrastructure.</h1>
        <p class="ob-step-lede">
            Voice, vision, memory, and tools — all running on hardware you own.
            We'll walk through setup in a few minutes; skip anything you're not
            ready for and return later.
        </p>
        <div class="ob-features">
            <div class="ob-feature">
                <div class="ob-feature-num">01</div>
                <div class="ob-feature-body">
                    <span class="ob-feature-label">Your data, your hardware</span>
                    <span class="ob-feature-desc">Conversations, memory, and tools live on your BlackBox — not in someone else's cloud.</span>
                </div>
            </div>
            <div class="ob-feature">
                <div class="ob-feature-num">02</div>
                <div class="ob-feature-body">
                    <span class="ob-feature-label">Reach it from anywhere</span>
                    <span class="ob-feature-desc">Tailscale gives you a private mesh: phone, laptop, BlackBox — secure access without exposing the public internet.</span>
                </div>
            </div>
            <div class="ob-feature">
                <div class="ob-feature-num">03</div>
                <div class="ob-feature-body">
                    <span class="ob-feature-label">Bring your own keys</span>
                    <span class="ob-feature-desc">OpenAI, Anthropic, Google — paste in your keys, pay providers directly. No middle-man billing.</span>
                </div>
            </div>
            <div class="ob-feature">
                <div class="ob-feature-num">04</div>
                <div class="ob-feature-body">
                    <span class="ob-feature-label">Your phone is the remote</span>
                    <span class="ob-feature-desc">Pair your phone with a QR scan. On-the-go voice, vision, and tool access — same memory.</span>
                </div>
            </div>
        </div>
        <button class="ob-cta">
            Let's get started <span class="ob-cta-arrow">→</span>
        </button>
    </main>
</body>
</html>
```

**Step 4: Write `Portal/onboarding/_mocks/tailscale.html`** — second mock, more complex form layout. Validates the design system handles multi-state forms (validator-ok / not-installed / not-authenticated / no-account-yet branches per Phase 2.3 spec). Use the same tokens + structure as welcome.html but render the "installed but not authenticated" branch as the primary state shown, with the "no account yet" disclosure expanded.

(Implementer: write this with the same visual language as welcome.html — Fraunces display, JetBrains Mono body, accent for primary CTA, success-green for the ✓ when "already configured", asymmetric grid where appropriate. ~200 lines of HTML.)

**Step 5: Test — preview both mocks in browser**

Since the wizard's StaticFiles mount doesn't exist yet (T2.1.1 territory), serve mocks via Python's built-in HTTP server for visual review:

```bash
cd Portal/onboarding
python3 -m http.server 8095 &
sleep 1
echo "Preview welcome mock at:  http://localhost:8095/_mocks/welcome.html"
echo "Preview tailscale mock at: http://localhost:8095/_mocks/tailscale.html"
```

Visit both URLs in a browser. Confirm:
- Fraunces serif loads (display headings should look distinctly editorial — wedge serifs visible at 64px)
- JetBrains Mono loads (body text should be monospace with crisp glyphs)
- Pure-black background, red accent on CTA + eyebrow + feature numbers
- Welcome step's title fade-up animation plays on page load (staggered reveals)
- CTA hover lifts 2px and shows offset shadow
- Layout breathes — generous spacing matches the "editorial gravitas" mood

Brandon signs off on the visual direction by saying "approved" or "tweak X". Iterate within T2.0.1 until sign-off, then commit.

**Step 6: Commit**

```bash
git add Portal/onboarding/fonts/ Portal/onboarding/onboarding-tokens.css Portal/onboarding/_mocks/
git commit -m "feat(portal-ob): design system prep — Fraunces + JetBrains Mono fonts, tokens, mocks

Self-hosts variable Fraunces (display) + JetBrains Mono (body) so the
wizard works offline during customer install (the wizard IS where Tailscale
gets configured, so internet may not be reliable yet).

onboarding-tokens.css extends Portal's _variables.css with wizard-specific
typography, spacing, motion, and accent treatments. Aesthetic: Portal-
adjacent + elevated — editorial gravitas + engineer's precision.

Static reference mocks at Portal/onboarding/_mocks/{welcome,tailscale}.html
for sign-off before T2.1.1 dispatches. Underscore prefix marks them as
design artifacts, not production routes."
```

DO NOT push until Brandon previews + signs off. Reference mocks are sign-off artifacts.



### Task 2.1.1: Set up the onboarding directory + index route

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/index.html`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding.css`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding.js`
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/app.py` (mount static)

**Step 1: Write `index.html` — base wizard shell with progress bar and step container:**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
    <title>AI BlackBox · Setup</title>
    <link rel="stylesheet" href="onboarding.css" />
</head>
<body>
    <div id="onboarding-app">
        <header class="ob-header">
            <div class="ob-brand">AI BlackBox</div>
            <div class="ob-progress">
                <div id="ob-progress-bar" class="ob-progress-bar"></div>
                <div id="ob-progress-text" class="ob-progress-text"></div>
            </div>
        </header>
        <main id="ob-step-container" class="ob-step-container">
            <!-- Step content injected here by onboarding.js -->
            <div class="ob-loading">Loading…</div>
        </main>
        <footer class="ob-footer">
            <button id="ob-back" class="ob-btn ob-btn-secondary" hidden>Back</button>
            <button id="ob-skip" class="ob-btn ob-btn-text" hidden>Skip for now</button>
            <button id="ob-next" class="ob-btn ob-btn-primary" hidden>Next →</button>
        </footer>
    </div>
    <script type="module" src="onboarding.js"></script>
</body>
</html>
```

**Step 2: Write minimal `onboarding.css`** matching Portal's design tokens (dark theme, accent color, etc.). Place in same file for now; can refactor later.

**Step 3: Write `onboarding.js`** — the orchestrator that:
- Fetches `/onboarding/state`
- Routes to current step component
- Wires next/back/skip buttons
- Handles step transitions

```javascript
// Top-level orchestrator
const STEPS = [
    "welcome", "tailscale", "api_keys",
    "optional_integrations", "pair_phone", "operator", "done",
];

let state = null;
let currentStepIdx = 0;

async function fetchState() {
    const r = await fetch("/onboarding/state");
    state = await r.json();
    currentStepIdx = STEPS.indexOf(state.current_step);
}

async function renderStep() {
    const stepName = STEPS[currentStepIdx];
    const container = document.getElementById("ob-step-container");
    const mod = await import(`./steps/${stepName}.js`);
    await mod.render(container, { state, next, back, skip });
    updateProgress();
}

function updateProgress() {
    const pct = ((currentStepIdx) / (STEPS.length - 1)) * 100;
    document.getElementById("ob-progress-bar").style.width = pct + "%";
    document.getElementById("ob-progress-text").textContent =
        `Step ${currentStepIdx + 1} of ${STEPS.length}`;
}

async function next() {
    if (currentStepIdx < STEPS.length - 1) {
        await fetch("/onboarding/step/complete", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({step: STEPS[currentStepIdx]}),
        });
        currentStepIdx++;
        await renderStep();
    }
}

async function back() {
    if (currentStepIdx > 0) {
        currentStepIdx--;
        await renderStep();
    }
}

async function skip() {
    await fetch("/onboarding/step/skip", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({step: STEPS[currentStepIdx]}),
    });
    currentStepIdx++;
    await renderStep();
}

(async () => {
    await fetchState();
    if (state.is_complete) {
        location.href = "/ui";
        return;
    }
    await renderStep();
})();
```

**Step 4: Mount static directory in `Orchestrator/app.py`:**

```python
from fastapi.staticfiles import StaticFiles
from Orchestrator.utils.paths import resolve
app.mount("/onboarding", StaticFiles(directory=str(resolve("Portal", "onboarding")), html=True), name="onboarding")
```

(Note: this mount must come AFTER all `/onboarding/*` API routes are registered, otherwise the static handler swallows them. Adjust ordering carefully — the API router with prefix `/onboarding` registers individual routes; the static mount handles the index. Verify with curl.)

**Step 5: Test — HARD-FAIL on mount-ordering bug**

```bash
sudo systemctl restart blackbox.service && sleep 70

# (a) Static index serves
curl -sI http://localhost:9091/onboarding/ | head -3 | grep -q "200" || { echo "FAIL: index.html not served"; exit 1; }

# (b) CRITICAL: API route MUST still resolve to JSON, not be shadowed by static mount.
# If StaticFiles is mounted BEFORE the APIRouter, the GET /onboarding/state below
# will return the index.html instead of JSON. This test must fail loudly if so.
RESP=$(curl -s http://localhost:9091/onboarding/state)
echo "$RESP" | python3 -m json.tool >/dev/null 2>&1 || {
    echo "FAIL: /onboarding/state did not return JSON — static mount is shadowing API routes."
    echo "Fix: in app.py, register onboarding_routes router BEFORE app.mount('/onboarding', StaticFiles, ...)."
    echo "Got: $RESP" | head -5
    exit 1
}

# (c) POST endpoint also still resolves
curl -s -X POST http://localhost:9091/onboarding/step/complete \
  -H "Content-Type: application/json" -d '{"step":"welcome"}' \
  | python3 -m json.tool >/dev/null || { echo "FAIL: POST /onboarding/step/complete shadowed"; exit 1; }

echo "PASS: index served + API routes still resolve"
```

**Step 6: Commit**

```bash
git add Portal/onboarding/ Orchestrator/app.py
git commit -m "feat(portal-ob): wizard base shell — index/css/js, progress bar, step container

Steps live as ES module files in Portal/onboarding/steps/{step}.js
each exposing async render(container, {state, next, back, skip}).
Static mount in app.py serves the wizard at /onboarding/."
```

## Phase 2.2 through 2.7: Per-step UI tasks

Each step is its own ES module under `Portal/onboarding/steps/`. The pattern is identical, so I'll detail Phase 2.2 (Welcome) fully and skeletalize 2.3-2.7.

### Phase 2.2 — Welcome + License

#### Task 2.2.1: Welcome step component

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/welcome.js`

**Step 1: Write the component:**

```javascript
export async function render(container, {next}) {
    container.innerHTML = `
        <section class="ob-step ob-welcome">
            <h1 class="ob-step-title">Welcome to AI BlackBox</h1>
            <p class="ob-step-lede">
                Your personal AI infrastructure — voice, vision, memory, and tools,
                all running on hardware you own.
            </p>
            <div class="ob-welcome-features">
                <div class="ob-feature"><span class="ob-feature-icon">🔒</span> Your data stays on your hardware</div>
                <div class="ob-feature"><span class="ob-feature-icon">🌐</span> Access from any device, anywhere via Tailscale</div>
                <div class="ob-feature"><span class="ob-feature-icon">🤖</span> Works with OpenAI, Anthropic, Google, and more</div>
                <div class="ob-feature"><span class="ob-feature-icon">📱</span> Pair your phone for on-the-go voice and vision</div>
            </div>
            <p class="ob-step-helper">
                We'll walk through everything in a few minutes. You can skip any step
                you're not ready for and return later.
            </p>
            <button class="ob-btn ob-btn-primary ob-btn-large" id="ob-welcome-start">
                Let's get started →
            </button>
        </section>
    `;
    document.getElementById("ob-welcome-start").addEventListener("click", next);
}
```

**Step 2: Test in browser** — visit `http://localhost:9091/onboarding/`, confirm welcome step renders + "Let's get started" advances to step 2.

**Step 3: Commit**

```bash
git add Portal/onboarding/steps/welcome.js
git commit -m "feat(portal-ob): welcome step component"
```

### Phase 2.3 — Tailscale step

#### Task 2.3.1: Tailscale install detection + auth flow

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/tailscale.js`

**Step 1: Component renders four branches based on validator result:**

- **A. Already configured** — `ok=true`: show "Tailscale already configured ✓ — your hostname: X.tail-net.ts.net" + Continue button. Persist `BLACKBOX_TAILNET_HOSTNAME=<hostname>` via `/onboarding/save`.

- **B. Not installed** — error contains "binary not found": show install instructions + copy-button for `curl -fsSL https://tailscale.com/install.sh | sudo sh`. After they run it, "Re-check" button re-validates.

- **C. Installed but not authenticated** — error contains "BackendState": show "Tailscale installed but not authenticated — run `sudo tailscale up`" with copy-button. "Re-check" button.

- **D. New to Tailscale (no account yet)** — collapsible disclosure under the install instructions: **"Don't have a Tailscale account? It's free for personal use → tailscale.com/start (~5 min)"** with brief explainer:
  > Tailscale is a private network that lets you reach your BlackBox from any device, anywhere. You'll create an account, install the client on your phone/laptop, and your devices form a secure mesh. Your BlackBox automatically gets a hostname like `mybox.tail-XXXX.ts.net` you can access from anywhere.

  External link to `https://tailscale.com/start`. Returning to the wizard, they re-run install + auth.

- **Always available:** "Skip for now → LAN-only mode" CTA. Skipping is non-destructive — Portal still works on `localhost:9091`; phone pairing requires same-LAN. Wizard records skip via `/onboarding/step/skip`.

**Step 2: Commit**

```bash
git add Portal/onboarding/steps/tailscale.js
git commit -m "feat(portal-ob): tailscale step — detect, install instructions, auth, hostname capture"
```

### Phase 2.4 — API keys step (BYOK, Tier-1 = 4 LLM/AI providers)

#### Task 2.4.1: API keys step component

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/api_keys.js`

**Step 1: Component renders 3 provider cards** (OpenAI, Anthropic, Google):
- Each with paste field, "Get a key →" link, "Validate" button
- On validate: `POST /onboarding/validate` with provider+key
- Show ✓/✗ with latency
- "Save & continue" button (active when ≥1 provider validated)
- On save: `POST /onboarding/save` with `{OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY}` (only the validated ones)

**Step 2:** Phone calls + SMS in v1 are handled by the **TG200 cellular modem** (auto-detected via `Orchestrator/cellular/hotplug.py` — no creds to collect). No phone step in v1 wizard. Twilio is reserved for v1.1 customers wanting a second phone path.

**Step 3: Commit**

```bash
git add Portal/onboarding/steps/api_keys.js
git commit -m "feat(portal-ob): API keys step — BYOK paste/validate/save for OpenAI, Anthropic, Google"
```

### Phase 2.4b — DEFERRED (Twilio phone step → v1.1)

Phase 2.4b was originally planned to collect Twilio credentials (SID/Auth Token/Phone Number) for inbound webhook-based phone calls. **Reverted in 2026-05-11 audit-fix:** the TG200 cellular modem already provides phone+SMS in v1 via `Orchestrator/cellular/hotplug.py` (zero customer config). When v1.1 adds an opt-in Twilio path, recreate this phase with `phone.js` step + `validate_twilio` validator + StepName/ALL_STEPS amendment.

### Phase 2.5 — Optional integrations

#### Task 2.5.1: Optional integrations step component

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/optional_integrations.js`

**Step 1: Component renders cards for each optional integration** (Gmail, Twilio v1.1 placeholder, ElevenLabs v1.1 placeholder):
- For Gmail: client_id + client_secret paste fields + "Walk me through Google Cloud" expandable instructions
- "Validate" button per integration
- "Save & continue" — saves what was provided, skips the rest

**Step 2: Commit**

```bash
git add Portal/onboarding/steps/optional_integrations.js
git commit -m "feat(portal-ob): optional integrations step — Gmail OAuth client setup"
```

### Phase 2.6 — Pair phone

#### Task 2.6.1: Phone pairing step component

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/pair_phone.js`

**Step 1: Component:**
- On mount, `POST /pair/start` to mint token
- Render `<img src="/pair/qr/${token}" />`
- Poll `GET /pair/status?token=…` every 2 seconds
- When `claimed=true`: show "✓ Paired with <device_name>"
- "Continue" button (after pair OR skip-for-now)

**Step 2: Commit**

```bash
git add Portal/onboarding/steps/pair_phone.js
git commit -m "feat(portal-ob): phone pairing step — QR display + claim polling"
```

### Phase 2.7 — Operator setup (multi-name registration)

#### Task 2.7.1: Operator step component — REWRITTEN per audit decision

**Background:** Original draft pre-filled "Brandon" as the operator name, which is wrong for a stranger-customer product. Audit decision: the wizard collects the customer's actual operator name(s) and registers them via `/operator/add` so they exist in the system before Portal opens. The internal "Brandon" code-level seed (analogous to the "system" operator used by apps) stays untouched — customers never see it.

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/operator.js`

**Step 1: Component renders:**
- Lede: "Who's using this BlackBox? You can add one or more operators — each gets their own conversation history, preferences, and operator-scoped memory. You can add more later from the System Menu."
- Form starts with one empty `<input>` for primary operator name + placeholder `e.g. Sarah` (validation: non-empty, alphanumeric + underscore + dash, max 32 chars)
- "+ Add another operator" button appends another input
- "Remove" button on each row (except the first)
- "Save & continue" button (disabled until at least one valid name)

**Step 2: On submit:**
- For each non-empty name field, `POST /operator/add` (existing endpoint, see `Orchestrator/routes/admin_routes.py` or wherever operator-add lives — verify path before implementing)
- If any POST fails, show inline error per-row, allow user to fix and retry
- On all-success: `POST /onboarding/save` with `{DEFAULT_OPERATOR: <first-name-entered>}` so Portal opens with that operator selected
- `POST /onboarding/step/complete` and advance

**Step 3: JS sketch:**

```javascript
export async function render(container, {next}) {
    let operatorRows = [{id: 0, name: ""}];
    let nextId = 1;

    function repaint() {
        container.innerHTML = `
            <section class="ob-step ob-operator">
                <h1 class="ob-step-title">Who's using this BlackBox?</h1>
                <p class="ob-step-lede">
                    Add one or more operators. Each gets their own conversation history,
                    preferences, and memory. You can add more later from System Menu.
                </p>
                <div class="ob-operator-rows">
                    ${operatorRows.map((row, idx) => `
                        <div class="ob-operator-row" data-id="${row.id}">
                            <input type="text" class="ob-input ob-operator-name"
                                   placeholder="e.g. Sarah" value="${row.name}"
                                   data-id="${row.id}" maxlength="32"
                                   pattern="[A-Za-z0-9_-]+" />
                            ${idx > 0 ? `<button class="ob-btn-icon ob-row-remove" data-id="${row.id}">×</button>` : ""}
                        </div>
                    `).join("")}
                </div>
                <button class="ob-btn ob-btn-text ob-add-op">+ Add another operator</button>
                <button class="ob-btn ob-btn-primary" id="ob-operator-save">Save & continue →</button>
                <div id="ob-operator-error" class="ob-error" hidden></div>
            </section>
        `;
        wireHandlers();
    }

    function wireHandlers() {
        container.querySelectorAll(".ob-operator-name").forEach(inp => {
            inp.addEventListener("input", e => {
                const row = operatorRows.find(r => r.id === Number(e.target.dataset.id));
                if (row) row.name = e.target.value.trim();
            });
        });
        container.querySelectorAll(".ob-row-remove").forEach(btn => {
            btn.addEventListener("click", e => {
                const id = Number(e.target.dataset.id);
                operatorRows = operatorRows.filter(r => r.id !== id);
                repaint();
            });
        });
        container.querySelector(".ob-add-op").addEventListener("click", () => {
            operatorRows.push({id: nextId++, name: ""});
            repaint();
        });
        container.querySelector("#ob-operator-save").addEventListener("click", onSave);
    }

    async function onSave() {
        const valid = operatorRows
            .map(r => r.name.trim())
            .filter(n => n && /^[A-Za-z0-9_-]+$/.test(n));
        if (valid.length === 0) {
            const err = container.querySelector("#ob-operator-error");
            err.textContent = "Enter at least one operator name (letters, numbers, _ or -).";
            err.hidden = false;
            return;
        }
        for (const name of valid) {
            const r = await fetch("/operator/add", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({operator: name}),
            });
            if (!r.ok) {
                const err = container.querySelector("#ob-operator-error");
                err.textContent = `Failed to add operator '${name}'. Try again.`;
                err.hidden = false;
                return;
            }
        }
        await fetch("/onboarding/save", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({secrets: {DEFAULT_OPERATOR: valid[0]}}),
        });
        next();
    }

    repaint();
}
```

**Step 4: Verify the `/operator/add` endpoint**
Before merging, confirm the endpoint actually exists. If not, add it to whichever operator-management routes file is canonical (likely `Orchestrator/routes/admin_routes.py` or `agent_routes.py` — check first). The body is `{"operator": "<name>"}` and it should be idempotent (re-add returns 200 with no change).

**Step 5: Commit**

```bash
git add Portal/onboarding/steps/operator.js
git commit -m "feat(portal-ob): operator step — multi-name registration, no Brandon prefill

Customer enters one or more operator names; each is POSTed to /operator/add
so they exist in the system before Portal opens. First name becomes the
default operator. Internal 'Brandon' code-level seed is unchanged."
```

### Phase 2.8 — Done / handoff

#### Task 2.8.1: Done step + handoff to Portal

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/done.js`

**Step 1: Component:**
- "🎉 Setup complete!"
- Summary card (what was configured, what was skipped)
- "Open Portal →" button
- On click: `POST /onboarding/complete`, then `location.href = "/ui"`

**Step 2: Commit**

```bash
git add Portal/onboarding/steps/done.js
git commit -m "feat(portal-ob): done step — summary card, complete-and-handoff to Portal"
```

## Phase 2.9: UI Polish + branding

### Task 2.9.1: Visual design pass

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding.css`

**Step 1: Apply BlackBox visual brand:**
- Use existing Portal design tokens (`--bbx-accent`, `--bbx-dim`, `--neutral-*`)
- Dark background, accent gradient for progress bar, glass-surface cards
- Typography matching Portal (system-ui, weight progression)
- Smooth transitions between steps (fade-out/fade-in 200ms)
- Animated success checkmarks on validation
- Mobile-friendly: viewport meta, touch-friendly tap targets ≥44px

**Step 2: Walk-through in browser** — full wizard, all 7 steps, on a 1920×1080 display + a phone-emulator viewport.

**Step 3: Commit**

```bash
git add Portal/onboarding/onboarding.css
git commit -m "style(portal-ob): apply BlackBox brand — dark glass, accent gradient, smooth transitions"
```

## Phase 2.10: Manage-mode entry points (REVISED 2026-05-12)

**REVISION NOTE — REPLACES the original 8-task Phase 2.10 from the 2026-05-10 audit.**

The original Phase 2.10 designed a separate manage-mode UI: step-grid landing page (`manage_landing.js`), per-step "current state" headers, dedicated reveal-toggle component, separate re-validate / replace / remove handlers. That entire surface area is **dropped** in favor of a thin-client architecture.

**Why dropped:** The Track 2 wizard plus the 2026-05-12 foundation refinements already do everything the original Phase 2.10 designed:

- Each step rehydrates from `/onboarding/current-config` showing `Already configured ••••XXXX [Replace]` (T2.4.1 + T2.4.2)
- Operator step has per-row [×] (T2.7.2)
- Service-account JSON has Replace + Remove on its card (T2.5.2)
- All 5 LLM keys (T2.4.2) + Gmail OAuth (T2.5.1) rehydrate
- Backend endpoints `/onboarding/current-config`, `/onboarding/config/{key}?reveal=true`, `DELETE /onboarding/config/{key}`, `DELETE /operator/{name}` are all live
- `?mode=manage` URL param parsing (`onboarding.js:26`) and the auto-redirect skip when `MODE === "manage"` (`onboarding.js:198`) are **already in place** — zero changes needed to the wizard itself

**The wizard IS the manage UI.** It just needs entry points.

**Architectural decision LOCKED 2026-05-12 (Brandon):**

> "We don't want to end up having to manage both portions. If we only manage and update and maintain the onboarding, that's it. We don't want the Android app to not get updated for a new onboarding step and then if there's friction there."

**The DRY-across-surfaces principle:** Both Portal Web and Portal Android become *thin clients* that just open `/onboarding/?mode=manage`. They never re-implement credential management. Any future onboarding step (new LLM provider, Twilio in v1.1, anything else) automatically appears in manage-mode on both platforms — there is only one UI to update.

**Goal:** Add "Manage Setup" entry points to Portal Web System Controls and Portal Android System Controls. The wizard's existing `?mode=manage` behavior handles the rest.

**Estimated effort:** 30-60 minutes total.

### Task 2.10.1: Portal Web — "Manage Setup" button in Advanced Settings

**Placement decision (Brandon, 2026-05-12):** mirror the Android position — directly after the existing pair-device button. On Android that button is in the top-level System Controls FlowRow; on Portal Web the equivalent (`ctlPair` "Pair Device") lives inside the Advanced Settings collapsible drawer at `Portal/index.html:732`. Place Manage Setup right after `ctlPair` in that drawer. Customers reach it via hamburger → System Menu → 🔧 Advanced Settings (expand) → ⚙️ Manage Setup.

**Files:**

- Modify: `Portal/index.html` (Advanced Settings grid — insert between `ctlPair` line 732 and `ctlMint` line 733)
- Modify: `Portal/index-modular.html` (mirror Advanced Settings grid — insert between `ctlPair` line 452 and `ctlMint` line 453)
- Modify: `Portal/modules/ui-setup.js` (existing System Menu click-handler module — uses `safeSetOnClick(id, fn)` helper; see existing wiring of `ctlClear` at line 824 for the pattern)

**Step 1: Add the button in `Portal/index.html` Advanced Settings drawer.** Insert after `ctlPair` (line 732) and before `ctlMint` (line 733):

```html
<button id="btnManageSetup" class="btn" title="Manage API keys, operators, and integrations">⚙️ Manage Setup</button>
```

Mirror the same insertion in `Portal/index-modular.html` between `ctlPair` (line 452) and `ctlMint` (line 453).

**Step 2: Wire the click handler in `Portal/modules/ui-setup.js`.** Find the existing `safeSetOnClick("ctlClear", ...)` line (around line 824) and add a sibling call:

```javascript
safeSetOnClick("btnManageSetup", () => {
    // Wizard is the single source of truth for credential management.
    // ?mode=manage suppresses the auto-redirect-to-/ui that fires when sentinel exists
    // (see onboarding.js:198).
    location.href = "/onboarding/?mode=manage";
});
```

**Step 3: Verify in browser** — open Portal at `http://localhost:9091/ui`, hamburger → System Menu → 🔧 Advanced Settings (expand) → "⚙️ Manage Setup". The wizard opens at `/onboarding/?mode=manage`. Confirm:

- URL has `?mode=manage` query param
- Wizard does NOT auto-redirect back to /ui (this is the manage-mode behavior already in `onboarding.js:198`)
- Step components render with rehydrated current state ("Already configured ••••XXXX [Replace]" on configured providers)

**Step 4: Commit**

```bash
git add Portal/index.html Portal/index-modular.html Portal/modules/ui-setup.js
git commit -m "feat(portal): System Menu 'Manage Setup' button → /onboarding/?mode=manage"
```

### Task 2.10.2: Portal Android — "Manage Setup" button in System Controls

**Files:**

- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt`

**Context:** System Controls FlowRow lives at lines 680-720. `MenuButton` helper at line 807. Existing buttons in this FlowRow: Checkpoint (686), Clear History (692), Cancel Tasks (698), Re-pair Device (704), Restart Service (710), Pair New Device (717). The `origin` String parameter is in scope (used by "Paired Server" Box at line 666).

**Step 1: Add a `MenuButton` to the System Controls FlowRow** — append at the end (after "Pair New Device" / `showPairQr` block at line 717-720):

```kotlin
// Manage Setup — opens the onboarding wizard in manage mode for credential edits.
// Single source of truth: same UI used at first-run setup, no Android-side reimplementation.
MenuButton("⚙️ Manage Setup") {
    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
    val target = if (origin.isNotBlank()) "$origin/onboarding/?mode=manage"
                 else "http://localhost:9091/onboarding/?mode=manage"
    val intent = android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(target))
    intent.flags = android.content.Intent.FLAG_ACTIVITY_NEW_TASK
    context.startActivity(intent)
    onDismiss()
}
```

The `⚙️` is the gear emoji (⚙️) — matches Portal Web button label/iconography for cross-surface consistency.

**Step 2: Verify on Android** — install debug APK on Brandon's phone, open Portal app → hamburger → System Controls → "⚙️ Manage Setup". Default browser opens at `{paired_origin}/onboarding/?mode=manage`. Confirm:

- Browser launches with correct URL (paired Tailscale origin, NOT localhost) when device is paired
- Wizard renders, step components rehydrate
- Customer can edit/replace/remove any credential through the wizard

**Step 3: Commit**

```bash
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt"
git commit -m "feat(android): System Controls 'Manage Setup' button → onboarding ?mode=manage"
```

### Task 2.10.3: End-to-end verification

**Pre-conditions:** Onboarding fully complete (sentinel `Volume/.onboarding_complete` exists), at least one API key + one operator + Tailscale configured. (Brandon's box already satisfies all of these.)

**Browser path (Portal Web):**

1. Open `http://localhost:9091/ui` (or paired Tailscale URL)
2. Hamburger menu → System Menu → 🔧 Advanced Settings (expand) → "⚙️ Manage Setup"
3. Confirm wizard loads at `/onboarding/?mode=manage`
4. Confirm NO redirect back to /ui (this is the manage-mode behavior — already in `onboarding.js:198`)
5. Click into the API Keys step → verify all 5 LLM cards show "Already configured ••••XXXX [Replace]"
6. Click [Replace] on a provider → form swaps to paste UI → cancel → returns to configured state
7. Click into the Operator step → verify all existing operators shown as read-only rows with [×]
8. Click into the Extras step → verify Gmail and service-account JSON cards rehydrated
9. Close wizard via standard wizard nav (or browser back) → return to Portal

**Android path:**

1. Open Portal app on phone, ensure paired to BlackBox
2. Hamburger → System Controls → "⚙️ Manage Setup"
3. Default browser opens at `{paired_origin}/onboarding/?mode=manage`
4. Repeat steps 4-9 above (the browser is now the wizard host)
5. Returning to the Portal app via the back button → Portal state preserved

**Acceptance criteria:**

- [ ] Both buttons visible in System Menu / System Controls
- [ ] Both open `/onboarding/?mode=manage` correctly (Web stays in tab; Android opens default browser)
- [ ] Wizard does NOT auto-redirect to /ui in manage mode
- [ ] All step components rehydrate with current credential state
- [ ] Customer can edit / replace / remove any credential through the same UI used for setup
- [ ] No new step components, no new endpoints required (verifies thin-client architecture)

### Task 2.10.4: Track 2 final checkpoint

```bash
git tag -a track2-portal-ui-complete -m "Portal onboarding wizard complete — 7 steps + foundation refinements + manage-mode entry points (Web + Android)"
git push origin track2-portal-ui-complete
```

### Deferred to v1.1 — "Control Panel" layout option

Brandon raised the idea of eventually collapsing the wizard's linear flow into a single-page "control panel" view for manage-mode (since 99% of returning users just want to tweak one setting, not walk through a 7-step flow).

This is **NOT a v1 task** — the current wizard works fine for both setup and manage. When v1.1 picks it up:

- Same step components, just reorganized into a single-page grid
- Could be a third URL mode (`?mode=panel`) sibling of `setup` and `manage`
- Preserves the DRY-across-surfaces principle (Web + Android both point at the panel URL)

Tracked here so future-you doesn't reinvent the original step-grid landing page idea.

---

# TRACK 3 — TAURI SHELL APP

**Goal:** Standalone Rust app wrapping the Portal `/onboarding` webview. Provides full-screen branded chrome, taskbar icon, no browser address bar. ~10MB binary. Bundled as `.deb` and `.AppImage` for Linux.

**Estimated effort:** 1-2 weeks
**Dependencies:** Track 2 (or can mock against placeholder)
**Outcome:** A standalone app `blackbox-setup` that launches the wizard in a polished native window.

## Phase 3.0: System prerequisites (NEW per audit 2026-05-12)

**Goal:** Install Rust toolchain and all Tauri 2.x system dependencies on the build machine. Standalone task so the implementer can verify environment readiness before any Cargo/Tauri work.

**Estimated effort:** 5-15 minutes (apt ~2-5 min; Rust install ~3-8 min on first install).

**See also:** `docs/plans/2026-05-12-track3-audit.md` for the audit findings that motivated this task.

### Task 3.0.1: Install + verify Rust toolchain and Tauri build deps

**Files:** none (system-state operations only).

**Step 1: Install Rust toolchain (if not already present):**

```bash
# Skip if `cargo --version` already prints rustc 1.x
which cargo && cargo --version || {
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source $HOME/.cargo/env
}
```

**Step 2: Install Tauri 2.x system dependencies for Ubuntu 24.04:**

```bash
sudo apt update
sudo apt install -y \
    libwebkit2gtk-4.1-dev \
    libsoup-3.0-dev \
    build-essential \
    curl \
    wget \
    file \
    libxdo-dev \
    libssl-dev \
    libayatana-appindicator3-dev \
    librsvg2-dev
```

**Why `libsoup-3.0-dev` is critical:** Tauri 2.x on Ubuntu 24.04 builds against `webkit2gtk-4.1`, which transitively requires `libsoup-3.0`. Without the `-dev` headers, `pkg-config` fails on the webkit-gtk discovery step and `cargo build` errors with "package soup-3.0 not found".

**Step 3: Install pinned `create-tauri-app` scaffolding tool:**

```bash
cargo install create-tauri-app --version "^4" --locked
```

The `^4` constraint ensures we get a v4.x scaffolder, which generates Tauri 2.x apps. Future v5+ may change scaffolding shapes.

**Step 4: Verify everything is in place:**

```bash
# Should print rustc 1.x
cargo --version

# Should print create-tauri-app 4.x
cargo create-tauri-app --version

# All 7 -dev packages should print "ii" status
for p in libwebkit2gtk-4.1-dev libsoup-3.0-dev build-essential libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev; do
    dpkg -l "$p" 2>/dev/null | grep -q "^ii" && echo "  ok $p" || echo "  MISSING $p (re-run Step 2)"
done
```

All 7 should print "ok". If any prints "MISSING", the apt install in Step 2 didn't complete — check apt logs and retry.

**Step 5:** No commit (system-state operations don't produce file changes).

## Phase 3.1: Cargo project skeleton

### Task 3.1.1: Initialize the Tauri project

**Files (Create):**
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/Cargo.toml`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/Cargo.toml`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/tauri.conf.json`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/src/main.rs`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/build.rs`
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/icons/` (set of icon PNGs)
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.gitignore` (add `installer/src-tauri/target/`)

**Step 1: Verify Phase 3.0 prerequisites are installed.**

If Phase 3.0 (sysprep) is complete, this step is a no-op. If you are in a fresh shell or skipped Phase 3.0, re-run Task 3.0.1 Step 4 — all 7 system `-dev` packages and the Rust toolchain must report "ok" before proceeding. **Do not** start Step 2 (scaffold) without Phase 3.0 verification — `cargo create-tauri-app` will hard-fail without the `-dev` packages.

**Step 2: Scaffold the project**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
cargo create-tauri-app installer --template vanilla --identifier com.blackbox.setup --name "BlackBox Setup"
cd installer
npm install   # Tauri scaffolding requires this even for vanilla
```

**Step 3: Update `installer/src-tauri/tauri.conf.json`** — Tauri 2.x schema, no static `url` (we build the webview programmatically in Phase 3.5.2), no `frontendDist` since we point at an external Orchestrator URL (no bundled frontend assets):

```json
{
  "build": {
    "devUrl": "http://localhost:9091/onboarding/",
    "frontendDist": "",
    "beforeDevCommand": "",
    "beforeBuildCommand": ""
  },
  "app": {
    "windows": [{
      "title": "AI BlackBox Setup",
      "width": 1280,
      "height": 800,
      "fullscreen": false,
      "resizable": true,
      "decorations": true,
      "transparent": false
    }],
    "security": {"csp": null}
  },
  "bundle": {
    "active": true,
    "targets": ["deb", "appimage"],
    "icon": ["icons/icon.png"],
    "identifier": "com.blackbox.setup",
    "category": "Utility",
    "shortDescription": "First-run setup for AI BlackBox",
    "longDescription": "Walks you through setting up your AI BlackBox device — Tailscale, API keys, phone pairing, and more."
  },
  "productName": "BlackBox Setup",
  "version": "0.1.0",
  "identifier": "com.blackbox.setup"
}
```

**Why no static `url` in `windows[]`:** Tauri 2.x's static window URL is intended for built-in frontend assets bundled via `frontendDist`. For external HTTP URLs we use the programmatic `WebviewWindowBuilder` (Phase 3.5.2) which gives us mode-aware URL construction (`?mode=setup` vs `?mode=manage`).

**Why `frontendDist: ""`:** We don't bundle any frontend — the wizard lives at the Orchestrator URL. An empty string suppresses Tauri's "frontendDist not found" warnings during build.

**Step 4: Add Cargo dependencies that downstream tasks need** (NEW per audit 2026-05-12).

The scaffolded `installer/src-tauri/Cargo.toml` from `create-tauri-app` includes only `tauri` itself. Downstream tasks (T3.2.1's `wait_for_server`, T3.5.1's autostart-removal, T3.5.2's mode detection) require three extra crates. Add them upfront so the build doesn't fail mid-Phase-3.5:

```toml
# Append to installer/src-tauri/Cargo.toml [dependencies]:
reqwest = { version = "0.12", features = ["blocking", "json"] }
dirs    = "5"
serde_json = "1"
```

**Why these specifically:**
- `reqwest` blocking client → `wait_for_server` HTTP probe + `/onboarding/state` JSON fetch (T3.2.1, T3.5.1, T3.5.2)
- `dirs` → cross-platform `~/.config/autostart/` resolution (T3.5.1)
- `serde_json` → parse `/onboarding/state` response (T3.5.1, T3.5.2). Tauri 2.x bundles serde_json transitively, but declaring it explicitly avoids version-skew surprises.

**Binary-size note:** `reqwest` with `blocking` feature pulls tokio's blocking runtime (~2-3 MB). Acceptable for a setup-shell app. If the bundled binary size is a concern later, consider switching to `ureq` (much smaller, blocking-only).

**Step 5: Smoke test build**

```bash
cd installer
cargo build --manifest-path src-tauri/Cargo.toml --release  # ~15-25 min first time (cold cargo cache + 200+ transitive crates), ~3-5 min on subsequent builds
```

**Step 6: Commit**

```bash
git add installer/ .gitignore
git commit -m "feat(installer): Tauri app skeleton — wraps Portal /onboarding webview

Standalone Rust binary that launches a native window pointing at the BlackBox
onboarding wizard. ~10MB build artifact, bundled as .deb + .AppImage."
```

## Phase 3.2: Webview wrapper customization

### Task 3.2.1: Customize window styling

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/tauri.conf.json`
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/src/main.rs`

**Step 1: Make the window full-screen on launch + no decorations:**

```json
"windows": [{
  "title": "AI BlackBox Setup",
  "fullscreen": true,
  "decorations": false,
  "transparent": false,
  "alwaysOnTop": true,
  "skipTaskbar": false,
  "url": "http://localhost:9091/onboarding/"
}]
```

**Step 2: In `main.rs`, add a wait-for-server health check** before opening the webview (so we don't show a blank page during boot):

```rust
fn wait_for_server(url: &str, timeout_secs: u64) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed().as_secs() < timeout_secs {
        if reqwest::blocking::get(url).is_ok() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    false
}

fn main() {
    if !wait_for_server("http://localhost:9091/health", 180) {
        eprintln!("Orchestrator failed to come up within 180s");
        std::process::exit(1);
    }
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

**Why 180s and not 90s:** BlackBox warmup (snapshot index rebuild on startup) takes 60-90s on warm cache, but cold-boot-after-install can exceed 90s. 180s gives margin without UX impact (user sees splash/wait UI either way; no harm in extra headroom).

**Step 3: Commit**

```bash
git add installer/src-tauri/tauri.conf.json installer/src-tauri/src/main.rs
git commit -m "feat(installer): wait-for-orchestrator + fullscreen no-decoration window"
```

## Phase 3.3: Branding (icons)

### Task 3.3.1: Generate icon set

**Files (Create):** `installer/src-tauri/icons/` set: `32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.icns` (mac, optional), `icon.ico` (win, optional), `icon.png` (Linux primary).

**Step 1: Generate icons from a master 512×512 PNG using Tauri CLI:**

```bash
cargo install tauri-cli --locked
cd installer
tauri icon path/to/master-icon-512.png
```

(Master icon: simple BlackBox logo on dark background. Can be generated by `mcp__blackbox__generate_image` with prompt "minimalist black box logo on dark background, white outline, modern tech aesthetic, 512x512".)

**Step 2: Commit**

```bash
git add installer/src-tauri/icons/
git commit -m "chore(installer): icon set — 32, 128, 128@2x for Linux + Mac + Windows"
```

## Phase 3.4: Auto-launch wiring (autostart + persistent launcher)

**Background (refined per audit 2026-05-10):** Two `.desktop` files serve different purposes:
1. **Autostart .desktop** — placed in `~/.config/autostart/` so the Tauri shell launches automatically on first boot. **Removed when onboarding completes** (T3.5.1) so it doesn't relaunch every boot.
2. **Persistent launcher .desktop** — placed in `~/.local/share/applications/` (or `/usr/share/applications/` for system-wide). **Stays forever** so user can re-launch the Tauri setup app at any time for maintenance (manage mode).

### Task 3.4.1: Autostart .desktop file (first-boot)

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/dist/blackbox-setup-autostart.desktop`

**Step 1: Write the autostart .desktop:**

```ini
[Desktop Entry]
Type=Application
Name=AI BlackBox Setup
Exec=/usr/bin/blackbox-setup --first-run
Icon=blackbox-setup
Comment=First-run setup wizard for AI BlackBox
Categories=Utility;
X-GNOME-Autostart-enabled=true
Terminal=false
StartupNotify=true
```

**Step 2:** Track 4 install script copies this to `~/.config/autostart/blackbox-setup.desktop`. T3.5.1 removes it when onboarding completes.

**Step 3: Commit**

```bash
git add installer/dist/blackbox-setup-autostart.desktop
git commit -m "chore(installer): autostart .desktop for first-boot launch (removed on completion)"
```

### Task 3.4.2: Persistent desktop launcher (NEW per audit decision)

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/dist/blackbox-setup.desktop`

**Step 1: Write the persistent launcher .desktop:**

```ini
[Desktop Entry]
Type=Application
Name=BlackBox Setup
GenericName=AI BlackBox Configuration
Exec=/usr/bin/blackbox-setup
Icon=blackbox-setup
Comment=Configure or update your AI BlackBox setup
Categories=Utility;Settings;
Keywords=blackbox;setup;configure;manage;
Terminal=false
StartupNotify=true
```

**Step 2:** Track 4 install script copies this to `~/.local/share/applications/blackbox-setup.desktop`. **Never removed** — this is the user's permanent desktop entry to re-launch the wizard for maintenance.

**Step 3: When this entry is invoked** (no `--first-run` flag), the Tauri binary checks `/onboarding/state.is_complete`:
- If complete: open Tauri window pointing at `/onboarding/?mode=manage` (maintenance mode)
- If incomplete: open at `/onboarding/?mode=setup` (legitimate first-run, autostart was somehow missed)

This logic lives in `installer/src-tauri/src/main.rs` (see Task 3.5.2).

**Step 4: Commit**

```bash
git add installer/dist/blackbox-setup.desktop
git commit -m "chore(installer): persistent desktop launcher — re-launch wizard for maintenance"
```

## Phase 3.5: Self-disable autostart + manage-mode entry

### Task 3.5.1: Tauri app self-disables AUTOSTART after onboarding completes

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/src/main.rs`

**Important:** This removes the AUTOSTART entry only (`~/.config/autostart/blackbox-setup.desktop`). The PERSISTENT launcher (`~/.local/share/applications/blackbox-setup.desktop`) is left intact so the user can re-launch the wizard for maintenance.

**Step 1: After window closes (i.e., user clicked "Open Portal"), check `/onboarding/state` for `is_complete`:**

```rust
// In a window-close-event handler:
.on_window_event(|window, event| {
    if let tauri::WindowEvent::CloseRequested { .. } = event {
        let state: serde_json::Value = reqwest::blocking::get("http://localhost:9091/onboarding/state")
            .and_then(|r| r.json())
            .unwrap_or(serde_json::json!({"is_complete": false}));
        if state["is_complete"].as_bool().unwrap_or(false) {
            // Remove the AUTOSTART .desktop only — persistent launcher stays
            let autostart = dirs::config_dir()
                .map(|d| d.join("autostart").join("blackbox-setup.desktop"));
            if let Some(p) = autostart {
                let _ = std::fs::remove_file(p);
                eprintln!("[blackbox-setup] removed autostart entry; persistent launcher in ~/.local/share/applications/ remains for re-launch");
            }
        }
    }
})
```

**Step 2: Also kill our own process explicitly** so the Tauri runtime exits cleanly.

**Step 3: Commit**

```bash
git add installer/src-tauri/src/main.rs
git commit -m "feat(installer): self-disable autostart on completion (persistent launcher stays)

When user clicks 'Open Portal' (window-close), check /onboarding/state.
If complete: remove ~/.config/autostart/blackbox-setup.desktop only.
The ~/.local/share/applications/blackbox-setup.desktop persistent launcher
remains so user can re-launch wizard from desktop applications menu for
maintenance (opens manage mode automatically)."
```

### Task 3.5.2: Mode-aware Tauri shell — first-run vs maintenance

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/src/main.rs`
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/installer/src-tauri/tauri.conf.json`

**Step 1: Detect mode at launch time.** When invoked from autostart, the `--first-run` flag is passed (per the autostart .desktop). When invoked from the persistent launcher (no flag), check `/onboarding/state.is_complete` to decide which mode to open in.

```rust
fn main() {
    if !wait_for_server("http://localhost:9091/health", 90) {
        eprintln!("Orchestrator failed to come up within 90s");
        std::process::exit(1);
    }

    // Determine which URL to open based on flag and onboarding state
    let args: Vec<String> = std::env::args().collect();
    let first_run = args.iter().any(|a| a == "--first-run");
    let mode = if first_run {
        "setup"
    } else {
        // No --first-run flag: this is a manual launch from the persistent .desktop entry.
        // Check /onboarding/state to decide mode.
        let state: serde_json::Value = reqwest::blocking::get("http://localhost:9091/onboarding/state")
            .and_then(|r| r.json())
            .unwrap_or(serde_json::json!({"is_complete": false}));
        if state["is_complete"].as_bool().unwrap_or(false) { "manage" } else { "setup" }
    };
    let url = format!("http://localhost:9091/onboarding/?mode={}", mode);
    eprintln!("[blackbox-setup] opening {url}");

    tauri::Builder::default()
        .setup(move |app| {
            // Tauri 2.x: WebviewWindowBuilder + WebviewUrl::External
            // (NOT v1's WindowBuilder + WindowUrl — those are removed in v2)
            let window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External(url.parse().unwrap()),
            )
            .title("AI BlackBox Setup")
            .inner_size(1280.0, 800.0)
            .fullscreen(mode == "setup")  // fullscreen for first-run, windowed for manage
            .decorations(mode == "manage")  // decorations for manage, none for first-run
            .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

**Step 2: Test both invocations:**

```bash
# Simulate autostart (first-run) — post-install path
/usr/bin/blackbox-setup --first-run
# Expect: fullscreen window, no decorations, opens at /onboarding/?mode=setup

# Simulate manual launch from desktop applications menu — post-install path
/usr/bin/blackbox-setup
# Expect: windowed (1280x800), decorations on, opens at /onboarding/?mode=manage
# (assuming onboarding has been completed; otherwise opens setup)

# Dev test (no install needed) — see Task 3.5.3
cd installer && cargo tauri dev -- -- --first-run
```

**Step 3: Commit**

```bash
git add installer/src-tauri/src/main.rs installer/src-tauri/tauri.conf.json
git commit -m "feat(installer): mode-aware launch — --first-run flag routes setup vs manage

Autostart invocation passes --first-run (fullscreen setup mode).
Persistent launcher invocation reads /onboarding/state and opens
in setup or manage mode automatically (windowed with decorations)."
```

### Task 3.5.3: End-to-end dev test (NEW per audit 2026-05-12)

**Goal:** Validate the full Phase 3.1-3.5 wiring (wait-for-server + mode detection + WebviewWindowBuilder + autostart-removal) using `cargo tauri dev` BEFORE spending the time/cost of building the .deb in Phase 3.6.

**Why before Phase 3.6:** A `.deb` build takes 5-10 minutes and the .deb can only be tested by installing it (system-state mutation). Catching wiring bugs in dev mode (instant rebuild loop) is much cheaper. If Phase 3.5 logic has a defect, you want to know now, not after .deb installation.

**Files:** none (test-only).

**Step 1: Confirm BlackBox is running and onboarding sentinel exists** (so `?mode=manage` is the default mode):

```bash
curl -sS http://localhost:9091/health  # expect 200
curl -sS http://localhost:9091/onboarding/state | python3 -c "import json,sys; print(json.load(sys.stdin).get('is_complete'))"
# expect: True (means manage-mode will be selected by main.rs)
```

If `is_complete` is False (sentinel absent), the dev-test still works but routes to setup mode — both modes are worth exercising.

**Step 2: Run dev mode without `--first-run` flag (manual launch path):**

```bash
cd installer
cargo tauri dev
```

Tauri dev loop opens a window. Verify:
- Window opens at `http://localhost:9091/onboarding/?mode=manage` (visible in window title bar / log output)
- Decorations are present (windowed, not fullscreen)
- Wizard renders with rehydrated current state
- Click into 1-2 steps to confirm the manage-mode UX behavior we built in Phase 2.10

Close the window with the X button (or Cmd+W). Verify in stderr that the autostart-removal logic logs the "removed autostart entry" message **only if** `is_complete` was true. Verify the autostart .desktop in `~/.config/autostart/` is NOT present (because we never installed it via Track 4 yet).

**Step 3: Run dev mode with `--first-run` flag (autostart simulation):**

```bash
cd installer
cargo tauri dev -- -- --first-run
```

Verify:
- Window opens at `http://localhost:9091/onboarding/?mode=setup` (note `mode=setup` in URL)
- Window is fullscreen, no decorations
- `wait_for_server` log message appears (proves the health check ran)

Close the window. Confirm autostart-removal triggers as before.

**Step 4: Test wait-for-server failure path** (optional but valuable):

Stop BlackBox: `sudo systemctl stop blackbox.service`. Run `cargo tauri dev` again. Expect:
- stderr prints "Orchestrator failed to come up within 180s" after 180 seconds
- Tauri exits with non-zero status (no window opens)

Restart BlackBox: `sudo systemctl restart blackbox.service`.

**Step 5: Commit (no file changes — record completion in commit message only)**

```bash
git commit --allow-empty -m "test(installer): T3.5.3 — dev-test all Phase 3.5 wiring verified

cargo tauri dev confirmed:
- mode=manage default when sentinel exists, mode=setup when --first-run flag passed
- WebviewWindowBuilder + WebviewUrl correct API for Tauri 2.x
- wait_for_server health check fires + 180s timeout enforced
- autostart-removal logic conditional on is_complete from /onboarding/state

Pre-flight clear for Phase 3.6 .deb build."
```

The empty commit serves as a verification checkpoint in the git log — useful for auditors.

## Phase 3.6: Build + package

### Task 3.6.1: Build .deb and .AppImage

**Steps:**

```bash
cd installer
cargo tauri build --bundles deb,appimage
ls -la src-tauri/target/release/bundle/
```

Expected: `bundle/deb/blackbox-setup_0.1.0_amd64.deb` and `bundle/appimage/blackbox-setup_0.1.0_amd64.AppImage`.

### Task 3.6.2: Track 3 checkpoint

```bash
git tag -a track3-tauri-shell-complete -m "Tauri standalone setup app — bundles to .deb + .AppImage"
git push origin track3-tauri-shell-complete
```

---

# TRACK 4 — INSTALL SCRIPTS + FACTORY IMAGE

> **Track 4 audit applied 2026-05-12.** See `docs/plans/2026-05-12-track4-audit.md` for the 3 critical + 6 major + 8 minor findings + 4 architectural decisions Brandon locked. Plan structure below reflects the post-audit shape.

**Goal:** Modernized `Scripts/install.sh` that template-substitutes paths, builds the Tauri setup `.deb` from source on the customer machine, installs a hardened systemd unit (Type=notify + WatchdogSec + MemoryHigh=70% + security hardening), wires logrotate + override.conf scaffold + autostart launchers, and is idempotent on re-run. Plus a factory-image build script (skeletal — fills in once hardware spec is locked).

**Estimated effort:** 1 week (post-audit)

**Dependencies:**
- Track 0 — `Orchestrator/utils/paths.py` resolver (✓ landed)
- Track 3 — Tauri sources at `installer/src-tauri/` (✓ landed; `.deb` is gitignored under `target/release/`, install.sh builds from source per audit Q1=A)
- `Scripts/onboarding/system-packages.txt` (✓ landed) — bucketed apt manifest
- `requirements.txt` (✓ landed) — Python dep manifest

**Outcome:** A single command bootstraps a fresh Ubuntu 24.04 mini-PC into a working BlackBox with the Tauri setup wizard auto-launching on next boot.

**Audit decisions locked 2026-05-12:**
- **Q1:** Build `.deb` from source on customer machine (no GitHub release asset path for v1)
- **Q2:** Install location is `~/blackbox-poc` (matches dev box; per-user state model)
- **Q3:** Carry forward audit-recommended self-healing set — Type=notify + WatchdogSec=120 + logrotate + override.conf scaffold + helper scripts; drop ExecStopPost crash-cleanup + separate watchdog .timer + IO bandwidth caps
- **Q4:** `MemoryHigh=70%` soft pressure (no hard `MemoryMax` cap)

## Phase 4.0: System pre-flight

### Task 4.0.1: Pre-install validation script

**Files:**
- Create: `<BLACKBOX_ROOT>/Scripts/install-preflight.sh`
- Sourced by: `Scripts/install.sh` Step 0

**Goal:** Catch unsupported environments early with clear errors, before any apt/sudo work begins.

**Step 1: Write `Scripts/install-preflight.sh`:**

```bash
#!/usr/bin/env bash
# Pre-install validation — run before main install.sh begins
set -euo pipefail

fail() { echo "[preflight] FAIL: $*" >&2; exit 1; }
warn() { echo "[preflight] WARN: $*" >&2; }
ok()   { echo "[preflight] OK: $*"; }

# 1. Ubuntu 24.04 only
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null; then
    VERSION="$(grep '^PRETTY_NAME' /etc/os-release | cut -d= -f2 | tr -d '"')"
    fail "Ubuntu 24.04 LTS required. Detected: ${VERSION:-unknown}"
fi
ok "Ubuntu 24.04 detected"

# 2. Disk free — 10 GB minimum on /
AVAIL_GB=$(df -BG / | awk 'NR==2 {print $4}' | tr -d 'G')
if (( AVAIL_GB < 10 )); then
    fail "Need 10 GB free on /, only ${AVAIL_GB} GB available"
fi
ok "${AVAIL_GB} GB free on /"

# 3. Memory — 4 GB minimum
MEM_GB=$(awk '/MemTotal/ {printf "%d\n", $2/1048576}' /proc/meminfo)
if (( MEM_GB < 4 )); then
    fail "Need 4 GB RAM minimum, only ${MEM_GB} GB detected"
fi
ok "${MEM_GB} GB RAM"

# 4. Network — github.com reachable
if ! curl -fsS --max-time 10 https://api.github.com/zen > /dev/null; then
    fail "Cannot reach github.com — check network"
fi
ok "github.com reachable"

# 5. Sudo access
if ! sudo -n true 2>/dev/null; then
    warn "sudo will prompt for password during install"
else
    ok "sudo passwordless"
fi

# 6. Existing BlackBox install detection
if systemctl is-active --quiet blackbox.service 2>/dev/null; then
    warn "blackbox.service is currently running — install.sh will restart it"
fi

# 7. Refuse direct root invocation (audit M6)
if [[ $EUID -eq 0 ]] && [[ -z "${SUDO_USER:-}" ]]; then
    fail "Do not run as direct root. Run as your user; sudo will be invoked when needed."
fi

echo "[preflight] All checks passed."
```

**Step 2: Make executable + commit:**

```bash
chmod +x Scripts/install-preflight.sh
git add Scripts/install-preflight.sh
git commit -m "feat(install): pre-flight validator (Ubuntu 24.04 + disk + RAM + network + sudo)"
```

---

## Phase 4.1: Modern install.sh

### Task 4.1.0: Build Tauri .deb from source on customer machine

**Why:** Per audit Q1=A, the Tauri `.deb` is gitignored under `installer/src-tauri/target/release/bundle/deb/`. A customer who clones the repo gets no `.deb`. install.sh apt-installs Tauri build deps + cargo + tauri-cli, then runs `cargo tauri build`.

**Files:**
- The function below is folded into `Scripts/install.sh` Step 5; not standalone.

**Implementation (called from T4.1.1 Step 5):**

```bash
build_tauri_setup() {
    echo "[install] Building BlackBox Setup (Tauri .deb)..."

    # Tauri build dependencies for Ubuntu 24.04
    sudo apt install -y \
        libwebkit2gtk-4.1-dev libsoup-3.0-dev librsvg2-dev libxdo-dev \
        libssl-dev libayatana-appindicator3-dev pkg-config build-essential

    # Rust toolchain (only if cargo is missing)
    if ! command -v cargo > /dev/null; then
        echo "[install] Installing Rust toolchain via rustup..."
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --default-toolchain stable
        # shellcheck disable=SC1091
        source "$HOME/.cargo/env"
    fi

    # tauri-cli v2.x (~5 min if not cached)
    if ! cargo tauri --version 2>/dev/null | grep -q "^tauri-cli"; then
        cargo install tauri-cli --locked --version "^2.0"
    fi

    # Build .deb only (skip .AppImage to save 79 MB / ~5 min)
    cd "$BLACKBOX_ROOT/installer"
    npm install --no-audit --no-fund > /dev/null 2>&1 || true   # scaffold front-end deps; harmless if absent
    cargo tauri build --bundles deb

    # Resolve produced .deb (version-independent glob)
    DEB_FILE=$(ls "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb | head -1)
    if [[ ! -f "$DEB_FILE" ]]; then
        echo "[install] ERROR: cargo tauri build did not produce a .deb" >&2
        exit 1
    fi
    echo "[install] Built: $DEB_FILE"
    cd "$BLACKBOX_ROOT"
}
```

**Notes:**
- Builds `--bundles deb` only. Skipping `appimage` saves ~5 min build + 79 MB output. install.sh consumers don't need AppImage.
- Tauri build deps are NOT added to `system-packages.txt` MUST_HAVE bucket (would pollute non-Tauri customers). Hardcoded inline here.
- `npm install` runs because Tauri scaffold has `package.json`; the scaffold front-end is never loaded at runtime (we point `WebviewUrl::External` at `/onboarding/`).
- Post-install: dpkg package name is `black-box-setup`; binary lives at `/usr/bin/blackbox-setup` (per Track 3 audit closeout).

---

### Task 4.1.1: Modernized Scripts/install.sh

**Files:**
- Create: `<BLACKBOX_ROOT>/Scripts/install.sh` (replaces legacy `setup.sh`; T4.1.5 cleans up)

**Audit fixes baked in:** C1 (apt pipeline), C2 (.deb build path), C3 (binary path), M1 (install location), M2 (security hardening + uvicorn flags), M3 (self-healing carry-forward), M5 (restart on upgrade), M6 (sudo detection), N7 (`apt install` of local .deb).

**Step 1: Write `Scripts/install.sh`:**

```bash
#!/usr/bin/env bash
# AI BlackBox installer — Ubuntu 24.04
set -euo pipefail

# ── Step 0: detect sudo, resolve real user/home (audit M6) ──
if [[ $EUID -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" ]]; then
        echo "[install] ERROR: do not run as direct root. Run as your user (sudo invoked as needed)."
        exit 1
    fi
    REAL_USER="$SUDO_USER"
    REAL_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    REAL_USER="$USER"
    REAL_HOME="$HOME"
fi

# Determine BLACKBOX_ROOT: parent of the directory holding this script
BLACKBOX_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "[install] BLACKBOX_ROOT=$BLACKBOX_ROOT"
echo "[install] REAL_USER=$REAL_USER  REAL_HOME=$REAL_HOME"

# ── Pre-flight (Phase 4.0) ──
"$BLACKBOX_ROOT/Scripts/install-preflight.sh"

# ── Step 1: apt deps (audit C1 — corrected pipeline) ──
echo "[install] Installing system packages..."
sudo apt update
grep -E '^[a-zA-Z0-9._+-]+\s+#\s+MUST_HAVE' \
    "$BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt" \
  | awk '{print $1}' \
  | xargs sudo apt install -y

# ── Step 2: Python venv (audit I1 — run as $REAL_USER so files are user-owned, not root-owned) ──
echo "[install] Creating Python venv..."
sudo -u "$REAL_USER" python3.12 -m venv "$BLACKBOX_ROOT/Orchestrator/venv"
sudo -u "$REAL_USER" "$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install --upgrade pip
sudo -u "$REAL_USER" "$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install -r "$BLACKBOX_ROOT/requirements.txt"

# ── Step 3: .env from template (audit I2 — created as $REAL_USER, mode 0600 since it holds API keys) ──
if [[ ! -f "$BLACKBOX_ROOT/.env" ]]; then
    sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/.env.template" "$BLACKBOX_ROOT/.env"
    sudo -u "$REAL_USER" bash -c "echo 'BLACKBOX_ROOT=$BLACKBOX_ROOT' >> '$BLACKBOX_ROOT/.env'"
    chmod 0600 "$BLACKBOX_ROOT/.env"
    echo "[install] Created .env from template (mode 0600)"
fi

# ── Step 4: systemd unit (audit M2 + M3 + Q3 + Q4) ──
echo "[install] Installing blackbox.service..."
sudo tee /etc/systemd/system/blackbox.service > /dev/null <<EOF
[Unit]
Description=AI BlackBox Orchestrator
Documentation=https://github.com/TechBran/blackbox-poc
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=$REAL_USER
WorkingDirectory=$BLACKBOX_ROOT
EnvironmentFile=$BLACKBOX_ROOT/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app \\
    --host 0.0.0.0 --port 9091 \\
    --timeout-keep-alive 120 --limit-max-requests 10000 --loop uvloop
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitIntervalSec=600

# Watchdog (audit M3) — service must heartbeat /watchdog within 120 s
WatchdogSec=120
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30

# Memory pressure (audit Q4) — soft cap at 70 % of system RAM
MemoryHigh=70%

# Security hardening (audit M2 — preserved from existing unit)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$BLACKBOX_ROOT
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=blackbox

[Install]
WantedBy=multi-user.target
EOF

# ── Step 4b: override.conf scaffold (audit M3 carry-forward) ──
sudo mkdir -p /etc/systemd/system/blackbox.service.d
sudo tee /etc/systemd/system/blackbox.service.d/override.conf > /dev/null <<EOF
# BlackBox service override — customize without modifying the main unit.
# Uncomment + edit, then run:
#   sudo systemctl daemon-reload && sudo systemctl restart blackbox

[Service]
# Change port (default 9091):
# ExecStart=
# ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app --host 0.0.0.0 --port 8000

# Override memory pressure (default 70 %):
# MemoryHigh=50%

# Override CPU priority:
# Nice=-5
EOF

# ── Step 4c: log rotation (audit M3 carry-forward) ──
sudo tee /etc/logrotate.d/blackbox > /dev/null <<EOF
/var/log/blackbox/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $REAL_USER $REAL_USER
    sharedscripts
}
EOF

# ── Step 4d: helper script (audit M3 carry-forward) ──
cat > "$BLACKBOX_ROOT/blackbox-status.sh" <<'STATUSEOF'
#!/usr/bin/env bash
echo "=== BlackBox Service ==="
systemctl status blackbox.service --no-pager | head -15
echo
echo "=== Recent Logs ==="
journalctl -u blackbox.service -n 20 --no-pager
echo
echo "=== Health ==="
curl -fsS http://localhost:9091/health 2>&1 | head -5
STATUSEOF
chmod +x "$BLACKBOX_ROOT/blackbox-status.sh"

# ── Step 5: Build + install Tauri setup app (audit C2 / Q1=A) ──
build_tauri_setup() {
    echo "[install] Building BlackBox Setup (Tauri .deb)..."
    sudo apt install -y \
        libwebkit2gtk-4.1-dev libsoup-3.0-dev librsvg2-dev libxdo-dev \
        libssl-dev libayatana-appindicator3-dev pkg-config build-essential
    if ! command -v cargo > /dev/null; then
        echo "[install] Installing Rust toolchain via rustup..."
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --default-toolchain stable
        # shellcheck disable=SC1091
        source "$HOME/.cargo/env"
    fi
    if ! cargo tauri --version 2>/dev/null | grep -q "^tauri-cli"; then
        cargo install tauri-cli --locked --version "^2.0"
    fi
    # Pre-clean bundle dir so we always install the freshly-built .deb (audit I4)
    rm -f "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb 2>/dev/null || true
    cd "$BLACKBOX_ROOT/installer"
    npm install --no-audit --no-fund > /dev/null 2>&1 || true
    cargo tauri build --bundles deb
    DEB_FILE=$(ls "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb | head -1)
    if [[ ! -f "$DEB_FILE" ]]; then
        echo "[install] ERROR: cargo tauri build did not produce a .deb" >&2
        exit 1
    fi
    echo "[install] Built: $DEB_FILE"
    cd "$BLACKBOX_ROOT"
}
build_tauri_setup
DEB_FILE=$(ls "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb | head -1)
echo "[install] Installing $DEB_FILE..."
sudo apt install -y "$DEB_FILE"   # apt 1.1+ resolves deps + installs in one step (audit N7)

# ── Step 6a: autostart .desktop — first-boot wizard launch (audit M6) ──
sudo -u "$REAL_USER" mkdir -p "$REAL_HOME/.config/autostart"
sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/installer/dist/blackbox-setup-autostart.desktop" \
    "$REAL_HOME/.config/autostart/blackbox-setup.desktop"

# ── Step 6b: persistent .desktop — manage-mode launcher (audit M6) ──
sudo -u "$REAL_USER" mkdir -p "$REAL_HOME/.local/share/applications"
sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/installer/dist/blackbox-setup.desktop" \
    "$REAL_HOME/.local/share/applications/blackbox-setup.desktop"
sudo -u "$REAL_USER" update-desktop-database "$REAL_HOME/.local/share/applications" 2>/dev/null || true

# ── Step 7: enable + restart (audit M5 — restart works whether running or stopped) ──
sudo systemctl daemon-reload
sudo systemctl enable blackbox.service
sudo systemctl restart blackbox.service

# ── Step 8: Final user message (audit C3 — /usr/bin not /usr/local/bin) ──
echo
echo "[install] Done. Reboot to launch BlackBox Setup, or run /usr/bin/blackbox-setup --first-run now."
echo "[install] Find 'BlackBox Setup' in your applications menu later for maintenance/manage mode."
```

**Step 2: Audit `Type=notify` compatibility (BLOCKING — verify before commit):**

`Type=notify` requires `Orchestrator/app.py` to call `sd_notify("READY=1")` after startup AND periodic heartbeat for `WatchdogSec=120`.

```bash
grep -n "sd_notify\|systemd.daemon\|WATCHDOG=1\|READY=1" \
    Orchestrator/app.py Orchestrator/startup.py 2>/dev/null
```

**If `sd_notify` is NOT wired** (likely the case as of 2026-05-12), implementer must downgrade in the systemd unit:

- `Type=notify` → `Type=simple`
- DROP the `WatchdogSec=120` block (silently ignored without notify support, but cleaner to remove)

Document the chosen path in the commit message ("Type=notify wired" vs "Type=simple — sd_notify not present").

**Step 3: Make executable + commit:**

```bash
chmod +x Scripts/install.sh
git add Scripts/install.sh
git commit -m "feat(install): modernized installer — apt deps, venv, .env, systemd, Tauri build, autostart

Builds Tauri setup .deb from source on customer machine (audit Q1=A).
Hardened systemd unit with watchdog/notify (or simple — see Step 2 audit),
MemoryHigh=70%, security hardening, logrotate + override.conf scaffold,
blackbox-status helper.

Audit-fix bundle: C1 apt pipeline / C2 .deb build path / C3 binary path /
M1 install location / M2 hardening + uvicorn flags / M3 self-healing
carry-forward / M5 restart on upgrade / M6 sudo detection / N7 apt-install
local .deb. Replaces stale setup.sh from Oct 2025 (T4.1.5 removes it)."
```

---

### Task 4.1.2: Verification recipe (Brandon-owned manual smoke test)

**Why:** Per audit N5 — VM smoke test is verification, not implementer work. Document the recipe; Brandon runs it after T4.1.5 lands.

**Recipe:**

```bash
# In a fresh Ubuntu 24.04 VM (virt-manager, vagrant, or LXD container with X session):
git clone <repo-url-or-tarball-extract> blackbox-poc
cd blackbox-poc
./Scripts/install.sh
# Reboot
# Confirm Tauri setup app autostarts and shows the wizard
# Walk through onboarding: tailscale → keys → optional integrations → operator
# Verify Portal opens; autostart .desktop self-removed (per Track 3 T3.5.1)
# Find "BlackBox Setup" in applications menu, click → opens /onboarding/?mode=manage
```

**Pass criteria:**

- Total time fresh-OS → working chat: **< 30 minutes** (including ~15 min Tauri build)
- No terminal needed after `./Scripts/install.sh`
- Setup wizard auto-launches on first reboot
- Setup wizard does NOT relaunch on second reboot
- "BlackBox Setup" persistent launcher available in apps menu
- `systemctl status blackbox.service` shows `active (running)`
- `systemctl show blackbox.service | grep MemoryHigh` returns `MemoryHigh=70%` (or post-deflate equivalent)

---

### Task 4.1.3: .gitignore hygiene (audit N3)

**Files:**
- Modify: `<BLACKBOX_ROOT>/.gitignore`

**Step 1: Append:**

```gitignore
# Onboarding state (per-install, never commit)
.onboarding_state.json
.onboarding_complete
.env.backup.*
```

**Step 2: Commit:**

```bash
git add .gitignore
git commit -m "chore(gitignore): exclude per-install onboarding state + .env backups"
```

---

### Task 4.1.4: Canonical .env.template (audit M4)

**Files:**
- Modify: `<BLACKBOX_ROOT>/.env.template`

**Goal:** Single source of truth for all wizard-managed env vars. Restores the `BLACKBOX_ROOT=` commented block deleted in pending uncommitted change. Documents Tier-1 + Tier-2 providers + integrations even when commented out.

**Step 1: Rewrite `.env.template`:**

```bash
# AI BlackBox Flight Recorder — environment template
# Copy this file to .env and fill in your keys, OR run the onboarding wizard.

# === Project Root ===
# Absolute path to the BlackBox project root.
# Auto-detected if unset (sentinel walk-up via Orchestrator/utils/paths.py).
# Set explicitly in production for reliability across install layouts.
# BLACKBOX_ROOT=/home/your-user/blackbox-poc

# === Operator Identity ===
# Default operator displayed in the Portal on first load.
# Set by onboarding wizard step "operator".
# DEFAULT_OPERATOR=Your Name

# === Tailscale ===
# Hostname assigned by Tailscale (https://your-host.your-tailnet.ts.net).
# Optional — leave blank for LAN-only mode.
# Set by onboarding wizard step "tailscale".
# BLACKBOX_TAILNET_HOSTNAME=

# === LLM Providers (Tier 1 — validated by wizard) ===
# OpenAI — GPT, DALL-E, Whisper, TTS. https://platform.openai.com/api-keys
OPENAI_API_KEY=

# Anthropic — Claude. https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=

# Google — Gemini, Imagen, Veo, TTS. https://aistudio.google.com/app/apikey
GOOGLE_API_KEY=

# === Optional LLM Providers (Tier 2 — not in v1 wizard) ===
# xAI — Grok. https://console.x.ai
# XAI_API_KEY=

# Perplexity. https://www.perplexity.ai/settings/api
# PERPLEXITY_API_KEY=

# === Integrations (Tier 1 — set by onboarding wizard "integrations" step) ===
# Gmail OAuth client (https://console.cloud.google.com/apis/credentials).
# GMAIL_CLIENT_ID=
# GMAIL_CLIENT_SECRET=

# === Phone / Telephony (deferred to v1.1) ===
# TG200 cellular gateway, Drachtio + FreeSWITCH passwords use weak defaults
# in v1; rotation deferred per audit decision. See docs/TROUBLESHOOTING.md.

# === Add new wizard-managed vars above this line ===
```

**Step 2: Commit:**

```bash
git add .env.template
git commit -m "docs(env): canonical wizard-managed env inventory + restore BLACKBOX_ROOT block

Single source of truth for what the wizard configures. Includes Tier-1
providers (validated), Tier-2 providers (commented), Tier-1 integrations
(commented), and the BLACKBOX_ROOT block that was deleted in a pending
change. Per Track 4 audit M4."
```

---

### Task 4.1.5: Cleanup legacy install files (audit N6)

**Files:**
- Delete: `setup.sh`, `install_enhanced_service.sh`, `blackbox-enhanced.service`, `cleanup_crash.sh`

**Step 1: Verify no remaining references (besides this audit doc):**

```bash
grep -rn "setup\.sh\|install_enhanced_service\|blackbox-enhanced\.service\|cleanup_crash\.sh" \
    --include="*.sh" --include="*.md" --include="*.py" .
```

If anything points at these (besides `docs/plans/2026-05-12-track4-audit.md`), update those references first.

**Step 2: Remove + commit:**

```bash
git rm setup.sh install_enhanced_service.sh blackbox-enhanced.service cleanup_crash.sh
git commit -m "chore(install): drop legacy install scripts — superseded by Scripts/install.sh

setup.sh:                       superseded by Scripts/install.sh (T4.1.1)
install_enhanced_service.sh:    self-healing features carried forward into T4.1.1 inline systemd unit
blackbox-enhanced.service:      template superseded by inline unit in install.sh
cleanup_crash.sh:               dropped per audit M3 (logs + journalctl sufficient)"
```

## Phase 4.2: Factory image build (skeletal)

### Task 4.2.1: Factory image script (placeholder until hardware spec)

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Scripts/build-factory-image.sh`

**Step 1: Skeleton with TODOs:**

```bash
#!/usr/bin/env bash
# Build a factory-ready Ubuntu 24.04 image with BlackBox pre-installed
# TODO: fill in once hardware spec is locked.
#
# Steps when ready:
#   1. Start from Ubuntu 24.04 base ISO
#   2. Mount + chroot
#   3. Pre-install: git clone blackbox-poc, run ./Scripts/install.sh
#   4. Pre-bake: Tauri setup app, autostart .desktop
#   5. Resize partition for target SSD size
#   6. Output: .img file ready to flash
echo "TODO: factory image build deferred until hardware spec locked."
exit 1
```

**Step 2: Commit**

```bash
git add Scripts/build-factory-image.sh
git commit -m "chore(install): factory image build skeleton — fills in when hardware spec lands"
```

---

## Phase 4.3: Milestone tag

### Task 4.3.1: Tag track4-installer-complete

After Brandon completes T4.1.2 (VM smoke test passes), tag the milestone.

**Step 1:** Verify all Track 4 sub-tasks are committed and pushed (`git log --oneline | head -10`).

**Step 2:** Annotated tag + push:

```bash
git tag -a track4-installer-complete -m "Track 4 — installer + factory image complete

Modernized Scripts/install.sh with audit-Q1=A build-from-source path,
hardened systemd unit (Type=notify or simple per Step 2 audit; WatchdogSec
when notify; MemoryHigh=70%; security hardening), logrotate +
override.conf scaffold + blackbox-status helper, autostart + persistent
.desktop launchers, gitignore hygiene, canonical .env.template, legacy
file cleanup. Factory image build script stubbed (T4.2.1) until hardware
spec lands.

Audit doc: docs/plans/2026-05-12-track4-audit.md (3 critical + 6 major + 8 minor)."
git push origin main --tags
```

This becomes the **7th milestone tag** after track0 / track1 / track2-linear / track2-portal / wizard-foundation / track3.

---

# TRACK 5 — CUSTOMER-FACING DOCS

**Goal:** README.md, TROUBLESHOOTING.md, per-integration setup guides. Written AFTER onboarding flow lands so they accurately describe the actual UX.

**Estimated effort:** 1 week
**Dependencies:** Track 2 (so README can describe real flow)
**Outcome:** A customer (or future contractor) can land on the GitHub repo and immediately understand what BlackBox is and how to install it.

## Phase 5.1: README.md

### Task 5.1.1: Repo landing page

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/README.md`
- Delete: `README.txt` (legacy, stale)

**Step 1: Write the README** with:
- Logo / hero
- 1-paragraph what-is-it
- "How to install" — link to Quick Start
- "How to use" — link to Portal docs
- "Architecture overview" — diagram of orchestrator + portal + apps + Tauri
- "Hardware product or DIY" — explain both paths
- License + contact

**Step 2: Commit**

```bash
git add README.md
git rm README.txt
git commit -m "docs: replace legacy README.txt with proper README.md landing page"
```

## Phase 5.2: TROUBLESHOOTING.md

### Task 5.2.1: Common failure modes + recovery

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/docs/TROUBLESHOOTING.md`

**Step 1: Cover:**
- "Tauri app doesn't launch on boot" → check `~/.config/autostart/blackbox-setup.desktop`
- "Wizard says Tailscale not found" → install link + manual command
- "API key validation fails" → common error patterns per provider
- "Phone QR scan does nothing" → check LAN reachability, check token expiry
- "Onboarding stuck — how to reset" → `curl -X POST http://localhost:9091/onboarding/reset`
- "Re-run setup after fresh install" → `rm ~/.config/autostart/blackbox-setup.desktop && rm $BLACKBOX_ROOT/.onboarding_complete`

**Step 2: Commit**

```bash
git add docs/TROUBLESHOOTING.md
git commit -m "docs(install): troubleshooting guide for common onboarding failures"
```

## Phase 5.3: Per-integration setup guides

### Task 5.3.1: Gmail OAuth setup walkthrough

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/docs/integrations/gmail.md`

**Step 1: Walkthrough with screenshots:**
- Open Google Cloud Console
- Create project
- Enable Gmail API
- Create OAuth 2.0 client (Web application)
- Add authorized redirect URIs
- Download client_secret JSON (or copy client_id + client_secret)
- Paste into BlackBox wizard

**Step 2: Repeat for each Tier-1 integration** (each as own task):
- `docs/integrations/openai.md`
- `docs/integrations/anthropic.md`
- `docs/integrations/google.md`
- `docs/integrations/tailscale.md`

**Step 3: Commit (after each)**

```bash
git add docs/integrations/<provider>.md
git commit -m "docs(integrations): <provider> setup walkthrough for onboarding"
```

---

# TRACK 6 — (v2 DEFERRED) Software-only distribution

**Status:** DEFERRED until v1 hardware-product onboarding ships.

**Scope when picked up:** Build a SECOND Tauri app — `blackbox-installer` — that:
1. Is downloaded by customer to their own Linux machine
2. Verifies system meets requirements (Ubuntu 24.04, etc.)
3. Downloads BlackBox source from GitHub release artifact
4. Runs `Scripts/install.sh`
5. Launches `blackbox-setup` (the v1 Tauri shell from Track 3)
6. Reuses 100% of Track 3 codebase

**Estimated effort when scheduled:** ~2 weeks (mostly Cargo + reusing Track 3 patterns)

---

# Verification — End-to-End Smoke Test

After Tracks 0-5 ship, run all four scenarios — happy path is necessary but not sufficient:

## Scenario 1 — Happy path (full configuration)

1. **Provision a fresh Ubuntu 24.04 VM** (virt-manager or vagrant)
2. **Clone the repo:** `git clone https://github.com/TechBran/blackbox-poc.git`
3. **Run install:** `cd blackbox-poc && ./Scripts/install.sh` (~5-10 min)
4. **Reboot:** `sudo reboot`
5. **On boot:** Tauri setup app autostarts; wizard appears
6. **Walk through wizard:** welcome → tailscale → API keys (3 providers) → optional integrations (Gmail) → phone pairing (QR) → operator (enter 2 operator names) → done
7. **Click "Open Portal":** wizard closes; Portal launches; autostart `.desktop` removed
8. **Verify:**
   - `curl http://localhost:9091/health` returns `ok`
   - Portal shows chat interface; both customer-entered operators selectable
   - First operator is the default (matches `DEFAULT_OPERATOR` in `.env`)
   - Tauri setup app does NOT relaunch on second reboot

## Scenario 2 — Skip Tailscale (LAN-only mode)

1. Fresh VM, install, reboot
2. Wizard appears — at Tailscale step click "Skip for now → LAN-only mode"
3. Continue through rest of wizard
4. **Verify:**
   - `BLACKBOX_TAILNET_HOSTNAME` is NOT in `.env` (or empty)
   - Phone pairing step still works on same-LAN device
   - `/health` includes `"tailscale_skipped": true` (or equivalent marker)
   - Portal opens normally; no crashes from missing tailnet hostname

## Scenario 3 — Resume after interrupt

1. Fresh VM, install, reboot, wizard appears
2. Walk through welcome + tailscale + API keys
3. **Force-quit Tauri app** (or close laptop lid mid-wizard)
4. Reopen Tauri app (or reboot)
5. **Verify:**
   - `.onboarding_state.json` exists with `current_step: "optional_integrations"` (or wherever you stopped)
   - `.onboarding_complete` does NOT exist
   - Wizard resumes at the step that was current when interrupted
   - Previously-validated providers (OpenAI etc.) show as ✓ already-configured

## Scenario 4 — `/onboarding/reset` for re-run

1. After completing onboarding, run:
   ```bash
   curl -X POST http://localhost:9091/onboarding/reset
   rm ~/.config/autostart/blackbox-setup.desktop
   ```
2. Reboot
3. **Verify:**
   - Tauri setup app launches again on boot
   - Wizard starts at welcome (state cleared)
   - Existing `.env` secrets preserved (reset clears progress, not credentials)

## Scenario 5 — Manage-mode round-trip

After Scenario 1 completes (full onboarding):

1. **From applications menu:** click "BlackBox Setup" (the persistent launcher icon, not autostart). Tauri opens windowed (1280×800, decorations on) at `/onboarding/?mode=manage`.
2. **Step grid renders** with status badges: Tailscale ✓, API Keys ✓, Phone ✓, Gmail ⊘ (or whatever), Pair ✓, Operators ✓.
3. **Click "API Keys" card** → opens API Keys step in manage mode with 3 provider rows showing redacted keys + Re-validate / Replace / Remove / 👁 buttons each.
4. **Click 👁 next to OpenAI** → full key reveals; click again → mask returns.
5. **Click "Re-validate"** → status updates with fresh latency timestamp.
6. **Click "Replace" on Anthropic** → modal opens with paste field; paste new key; Validate; Save. New key persisted to .env, old backed up to .env.backup.<ts>.
7. **Click "Remove" on a non-essential provider** (e.g. PERPLEXITY if configured) → confirms; DELETE /onboarding/config/PERPLEXITY_API_KEY succeeds; key removed from .env; manage landing badge flips to ⊘.
8. **From Portal Web:** open hamburger → System → "Manage Setup" → opens `/onboarding/?mode=manage` in browser tab. Same UI works.
9. **From Portal Android:** System → "Manage Setup" → launches default browser pointed at manage URL.

**Pass:** All buttons work as labeled, secrets never expose without explicit reveal click, .env backups created on Replace and Remove, Portal entry points reach the manage UI.

## Pass criteria (all four scenarios)

- Total time from clean OS to working chat (Scenario 1): **< 30 minutes** including download
- Customer never sees a terminal except for the initial `./Scripts/install.sh` command
- All keys validated successfully OR explicitly skipped (no silent failures)
- Phone pairs successfully via QR (Scenario 1) and same-LAN (Scenario 2)
- Onboarding state persisted across Tauri restart (Scenario 3)
- Reset works without losing already-configured secrets (Scenario 4)
- After completion, `~/.config/autostart/blackbox-setup.desktop` is removed; Tauri does not relaunch

---

# Out of Scope (Explicitly Deferred)

- **Track 6 (v2 software-only distribution)** — picks up after v1 ships
- **Migration flow** — bringing existing snapshots/config/paired devices forward (v1.5)
- **Tier-2 integrations** (Twilio, ElevenLabs, Asterisk, xAI, Perplexity) — v1.1. (Twilio re-deferred to v1.1 per 2026-05-11 audit-fix: TG200 cellular modem handles phone+SMS in v1, no Twilio webhooks needed.)
- **Auto-rotate weak default passwords** (Drachtio "cymru", FreeSWITCH "ClueCon", TG200 "password") — deferred to v1.1 per audit decision. Cross-component coordination (Drachtio↔FreeSWITCH share secrets) is non-trivial. Document existing weak defaults in `docs/TROUBLESHOOTING.md` (Track 5) with manual-rotation instructions.
- **Migrate `@app.on_event` → FastAPI lifespan** — `Orchestrator/startup.py` has 11 deprecated `@app.on_event` decorators. T1.3.2's middleware approach works fine in either pattern, so not blocking. Optional cleanup task.
- **Hardware-spec-dependent tasks in Track 5** — fills in once spec lands
- **GitHub Actions / CI** — independent of onboarding; can ship anytime
- **LICENSE file** — independent
- **Branch protection rules / PR templates** — independent

These are tracked in `SNAP-20260509-6533` and `docs/onboarding/discovery-notes.md` for future sessions.

---

# Execution Handoff

**Plan complete and saved to `docs/plans/2026-05-10-onboarding-flow.md`.**

When ready to implement, two execution options:

1. **Subagent-Driven (recommended for this plan)** — fresh subagent per task with two-stage review (spec compliance, then code quality). Stay in this session. Use `superpowers:subagent-driven-development`.

2. **Parallel Session** — open a new Claude Code session in a worktree. Use `superpowers:executing-plans`. Better when stepping away from active development.

Either way: **read this plan + `docs/onboarding/discovery-notes.md` + the `project_onboarding_architecture.md` memory entry first**. The plan assumes that context.

---

*End of plan. Audit session can override defaults, edit task lists, or split tracks across multiple plan files for finer-grained execution.*
