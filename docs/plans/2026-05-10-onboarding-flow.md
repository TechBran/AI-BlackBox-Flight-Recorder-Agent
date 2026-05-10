# AI BlackBox Onboarding Flow Implementation Plan

> **For Claude:** This plan is large (6 tracks, ~7-9 weeks including manage-mode). REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute (fresh subagent per task with two-stage review). `superpowers:executing-plans` is the alternative for parallel-session execution.

**Goal:** Build a customer-facing first-run onboarding experience for AI BlackBox — a Tauri standalone app that wraps Portal `/onboarding` routes, walks the customer through Tailscale install, BYOK API keys (incl. Twilio for phone), optional integrations, QR phone pairing, operator setup, and completion handoff. **Ships with a paired maintenance UI** (same wizard re-entered via `?mode=manage`) accessible from Portal's System Menu and a persistent desktop launcher — customers can review/update their configuration any time. Ships as the first-boot experience on pre-installed mini-PC hardware.

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
| 6 | Tier-1 integrations for v1 wizard | **OpenAI, Anthropic, Google, Twilio, Tailscale, Gmail (6 providers).** ElevenLabs / Asterisk / xAI / Perplexity → v1.1. Cellular + UGV → never in v1 wizard. | Phone is a flagship BlackBox capability — Twilio belongs in v1 so the customer's first call works after onboarding without a return trip to System Menu. Validator: `client.api.accounts(sid).fetch()`. |
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

> **Track 4 sequencing correction (audit 2026-05-10):** Earlier text called Track 4 "parallelizable with Tracks 1+2." That is wrong. Track 4's `install.sh` Step 5 installs `installer/dist/blackbox-setup.deb` which is built in Track 3 (T3.6.1), and Step 4 (systemd unit) is the deferred Bucket B1 work from T0.3.7. Track 4 is the LAST execution track, not parallel.

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


def validate_twilio(account_sid: str, auth_token: str) -> ValidationResult:
    """Validate Twilio credentials by fetching account metadata (no charge)."""
    def _fn():
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        acct = client.api.accounts(account_sid).fetch()
        return {
            "friendly_name": acct.friendly_name,
            "status": acct.status,
            "type": acct.type,
        }
    return _measure(_fn)
```

**Step 2: Verify with the actual current keys (sanity check)**

```bash
Orchestrator/venv/bin/python -c "
from Orchestrator.onboarding.validators import (
    validate_openai, validate_anthropic, validate_google, validate_tailscale, validate_twilio
)
from Orchestrator.config import (
    OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
)
print('OpenAI:', validate_openai(OPENAI_API_KEY))
print('Anthropic:', validate_anthropic(ANTHROPIC_API_KEY))
print('Google:', validate_google(GOOGLE_API_KEY))
print('Tailscale:', validate_tailscale())
print('Twilio:', validate_twilio(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
"
```

Expected: all five print `ValidationResult(ok=True, latency_ms=…, detail={…})`.

**Step 3: Commit**

```bash
git add Orchestrator/onboarding/validators.py
git commit -m "feat(onboarding): tier-1 per-provider validators (OpenAI/Anthropic/Google/Twilio/Tailscale/Gmail)

Each does a cheap 1-token (or metadata-only) call to confirm the credential works.
Returns ValidationResult{ok, latency_ms, error?, detail?} for clean wizard UX.
Twilio promoted to Tier-1 per audit decision (phone is a flagship capability)."
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
        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER,
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
        "twilio": {
            "present": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
            "sid_last4": _redact(TWILIO_ACCOUNT_SID),
            "phone_number": TWILIO_PHONE_NUMBER or None,  # not secret, full reveal OK
            "validated_at": val_at.get("twilio"),
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
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
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

## Phase 2.1: Routing + base shell

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

**Step 2:** Twilio (phone) is collected in its own dedicated step (Phase 2.4b below) since it requires three fields (SID, Auth Token, Phone Number) and walks through external account setup.

**Step 3: Commit**

```bash
git add Portal/onboarding/steps/api_keys.js
git commit -m "feat(portal-ob): API keys step — BYOK paste/validate/save for OpenAI, Anthropic, Google"
```

### Phase 2.4b — Phone (Twilio) step — NEW per audit decision

#### Task 2.4b.1: Twilio phone setup step component

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/phone.js`
- Modify: `Portal/onboarding/onboarding.js` — add `"phone"` between `"api_keys"` and `"optional_integrations"` in the `STEPS` array
- Modify: `Orchestrator/onboarding/state.py` — add `"phone"` to `StepName` Literal + `ALL_STEPS`

**Step 1: Component renders:**
- Lede: "Phone integration — make and receive calls/SMS through your BlackBox. Optional but recommended for full functionality."
- 3 paste fields: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` (E.164 format, e.g. `+15551234567`)
- "Get Twilio credentials →" expandable disclosure with brief walkthrough:
  > 1. Sign up at twilio.com (~3 min, free trial credit)
  > 2. Buy a phone number ($1/mo) — Console → Phone Numbers → Buy
  > 3. Find your Account SID and Auth Token on Console homepage
  > 4. Paste all three values here
- "Validate" button → `POST /onboarding/validate` with `{provider:"twilio", credentials:{account_sid, auth_token}}`
- Show ✓/✗ + account friendly_name on success
- "Save & continue" → `POST /onboarding/save` with all 3 vars (only if validated)
- "Skip for now" → records skip; phone integration off until configured later

**Step 2: Commit**

```bash
git add Portal/onboarding/steps/phone.js Portal/onboarding/onboarding.js Orchestrator/onboarding/state.py
git commit -m "feat(portal-ob): Twilio phone step — Tier-1 promotion per audit decision"
```

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

## Phase 2.10: Manage-mode UI (NEW per audit 2026-05-10)

**Goal:** Re-entrant maintenance UI accessible from Portal System Menu and the Tauri desktop launcher. Same wizard codebase + `?mode=manage` URL parameter — each step component supports rendering its current state with reveal/edit/remove controls.

**Design reference (chose by Brandon during audit):**
- Layout: step-grid landing page with status badges (✓/⚠/⊘) — clicking a step opens its edit UI
- Sensitive values: full reveal with masking toggle (eye icon) — bullet-mask by default, reveal on click
- Entry points: (a) Portal System Menu → "Manage Setup" button, (b) Tauri persistent desktop launcher (separate from autostart)

### Task 2.10.1: Mode parameter handling + manage landing page

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding.js`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/manage_landing.js`

**Step 1: Read `?mode=` from URL in `onboarding.js`:**

```javascript
const params = new URLSearchParams(location.search);
const MODE = params.get("mode") === "manage" ? "manage" : "setup";
```

**Step 2: In manage mode, replace linear flow with step-grid landing.** At the bottom of the IIFE in `onboarding.js`:

```javascript
(async () => {
    if (MODE === "manage") {
        const r = await fetch("/onboarding/current-config");
        const config = await r.json();
        const mod = await import("./manage_landing.js");
        mod.render(document.getElementById("ob-step-container"), {config, openStep: (name) => {
            currentStepIdx = STEPS.indexOf(name);
            renderStep({mode: "manage"});  // step components see mode flag
        }});
        return;
    }
    // setup mode: existing flow
    await fetchState();
    if (state.is_complete) { location.href = "/ui"; return; }
    await renderStep({mode: "setup"});
})();
```

**Step 3: Write `manage_landing.js`:**

```javascript
export function render(container, {config, openStep}) {
    const cards = [
        {step: "tailscale", label: "Tailscale", state: config.tailscale.configured ? "✓" : "⊘",
         summary: config.tailscale.detail.hostname || "Skipped"},
        {step: "api_keys", label: "API Keys", state: countConfigured(config.providers, ["openai","anthropic","google"]) > 0 ? "✓" : "⊘",
         summary: `${countConfigured(config.providers, ["openai","anthropic","google"])} of 3 providers`},
        {step: "phone", label: "Phone (Twilio)", state: config.providers.twilio.present ? "✓" : "⊘",
         summary: config.providers.twilio.phone_number || "Not configured"},
        {step: "optional_integrations", label: "Gmail", state: config.providers.gmail.present ? "✓" : "⊘",
         summary: config.providers.gmail.client_id ? "OAuth client configured" : "Skipped"},
        {step: "pair_phone", label: "Paired Devices", state: config.paired_devices.length > 0 ? "✓" : "⊘",
         summary: config.paired_devices.length ? config.paired_devices.map(d=>d.name).join(", ") : "None"},
        {step: "operator", label: "Operators", state: config.operators.length > 0 ? "✓" : "⊘",
         summary: config.operators.length ? config.operators.join(", ") : "None"},
    ];
    container.innerHTML = `
        <section class="ob-step ob-manage-landing">
            <h1 class="ob-step-title">Setup Status</h1>
            <p class="ob-step-lede">Click any step to review or update.</p>
            <div class="ob-manage-grid">
                ${cards.map(c => `
                    <button class="ob-manage-card ob-card-${c.state === '✓' ? 'ok' : c.state === '⚠' ? 'warn' : 'empty'}"
                            data-step="${c.step}">
                        <div class="ob-card-state">${c.state} ${c.label}</div>
                        <div class="ob-card-summary">${c.summary}</div>
                    </button>
                `).join("")}
            </div>
            <button class="ob-btn ob-btn-secondary" id="ob-manage-close">Close</button>
        </section>
    `;
    container.querySelectorAll(".ob-manage-card").forEach(btn => {
        btn.addEventListener("click", e => openStep(e.currentTarget.dataset.step));
    });
    container.querySelector("#ob-manage-close").addEventListener("click", () => {
        location.href = "/ui";
    });
}

function countConfigured(providers, keys) {
    return keys.filter(k => providers[k]?.present).length;
}
```

**Step 4: Hide footer (back/next/skip) in manage mode** — `onboarding.js`:

```javascript
if (MODE === "manage") {
    document.querySelector(".ob-footer").style.display = "none";
    document.querySelector(".ob-progress").style.display = "none";
}
```

**Step 5: Test**

```bash
# Visit /onboarding/?mode=manage in browser; confirm step grid renders with status badges
# Click "API Keys" card; confirm individual step UI loads (edit mode)
```

**Step 6: Commit**

```bash
git add Portal/onboarding/onboarding.js Portal/onboarding/manage_landing.js
git commit -m "feat(portal-ob): manage-mode landing page — step grid with status badges"
```

### Task 2.10.2: Per-step manage-mode rendering with current state header

**Files:**
- Modify: ALL of `Portal/onboarding/steps/*.js` (api_keys.js, tailscale.js, phone.js, optional_integrations.js, pair_phone.js, operator.js)

**Step 1: Each step component's `render(container, {state, next, back, skip, mode, config})` checks `mode` and prepends a current-state header in manage mode.** Example for `api_keys.js`:

```javascript
export async function render(container, {next, mode = "setup", config}) {
    const isManage = mode === "manage";
    const header = isManage ? `
        <div class="ob-manage-header">
            <h2>API Keys</h2>
            ${["openai","anthropic","google"].map(p => `
                <div class="ob-current-row">
                    <span class="ob-provider-name">${p.charAt(0).toUpperCase() + p.slice(1)}:</span>
                    ${config.providers[p].present ? `
                        <span class="ob-status">✓</span>
                        <span class="ob-key-mask" data-provider="${p}" data-key-name="${p.toUpperCase()}_API_KEY">
                            ${config.providers[p].last4 || "••••••••"}
                        </span>
                        <button class="ob-btn-icon ob-reveal" data-provider="${p}" data-key-name="${p.toUpperCase()}_API_KEY">👁</button>
                        <span class="ob-validated">${formatValidated(config.providers[p].validated_at)}</span>
                        <button class="ob-btn-text ob-revalidate" data-provider="${p}">Re-validate</button>
                        <button class="ob-btn-text ob-replace" data-provider="${p}">Replace</button>
                        <button class="ob-btn-text ob-remove" data-provider="${p}" data-key-name="${p.toUpperCase()}_API_KEY">Remove</button>
                    ` : `
                        <span class="ob-status">⊘ Not configured</span>
                        <button class="ob-btn-text ob-add" data-provider="${p}">Add key</button>
                    `}
                </div>
            `).join("")}
        </div>
    ` : "";
    container.innerHTML = header + /* original setup-mode form */;
    if (isManage) wireManageHandlers(container, config, /* refresh callback */);
    // ... existing form-wiring
}
```

**Step 2: Repeat the pattern for each step component.** Each step knows its own provider keys and current state. The "Replace" button shows the same paste field but pre-filled empty (just collects new value).

**Step 3: Commit (one commit per step component to keep diffs small)**

```bash
git add Portal/onboarding/steps/api_keys.js
git commit -m "feat(portal-ob): api_keys step manage-mode rendering with current state header"
# repeat for tailscale.js, phone.js, etc.
```

### Task 2.10.3: Reveal/mask toggle component

**Files:**
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/components/reveal_toggle.js`

**Step 1: Write the toggle helper:**

```javascript
/** Wire eye-icon toggles to fetch full secret on demand and swap mask ↔ cleartext.
 * Buttons must have data-key-name (env var name) and a sibling .ob-key-mask element.
 */
export function wireRevealToggles(container) {
    container.querySelectorAll(".ob-reveal").forEach(btn => {
        btn.addEventListener("click", async () => {
            const keyName = btn.dataset.keyName;
            const maskEl = btn.parentElement.querySelector(".ob-key-mask");
            const isRevealed = btn.dataset.revealed === "true";
            if (isRevealed) {
                // Re-mask
                maskEl.textContent = maskEl.dataset.masked;
                btn.textContent = "👁";
                btn.dataset.revealed = "false";
            } else {
                // Reveal — fetch full value
                const r = await fetch(`/onboarding/config/${keyName}?reveal=1`);
                if (!r.ok) {
                    console.error("reveal failed", await r.text());
                    return;
                }
                const data = await r.json();
                maskEl.dataset.masked = maskEl.textContent;  // remember mask for re-mask
                maskEl.textContent = data.value;
                btn.textContent = "🙈";
                btn.dataset.revealed = "true";
            }
        });
    });
}
```

**Step 2: Import + call from each step's wireManageHandlers.**

**Step 3: Test**

```bash
# In browser at /onboarding/?mode=manage → API Keys
# Click eye next to OpenAI key — full key shows
# Click again — mask returns
```

**Step 4: Commit**

```bash
git add Portal/onboarding/components/reveal_toggle.js
git commit -m "feat(portal-ob): reveal toggle — eye icon swaps masked ↔ cleartext on demand"
```

### Task 2.10.4: Re-validate / Replace / Remove handlers

**Files:**
- Modify: each step component to wire the per-provider buttons

**Step 1: Re-validate handler:**

```javascript
async function onRevalidate(provider) {
    const r = await fetch("/onboarding/validate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({provider, credentials: {}}),  // server reads from env
    });
    const result = await r.json();
    showInlineToast(result.ok ? `${provider} ✓ ${result.latency_ms}ms` : `${provider} ✗ ${result.error}`);
}
```

**Note:** /onboarding/validate currently expects credentials in the body. For manage-mode re-validation with existing-on-disk keys, either: (a) pre-fetch the cleartext via reveal endpoint and pass it; or (b) extend /validate to accept `{provider, use_existing: true}` and read from env on the server side. Option (b) is cleaner — add it as part of this task.

**Step 2: Replace handler** — opens a small modal with the same paste/validate UI as setup mode, then on save POSTs `/onboarding/save` with just that one key.

**Step 3: Remove handler:**

```javascript
async function onRemove(keyName) {
    if (!confirm(`Remove ${keyName}? This won't delete the .env backup.`)) return;
    const r = await fetch(`/onboarding/config/${keyName}`, {method: "DELETE"});
    const result = await r.json();
    if (result.ok) {
        showInlineToast(`Removed ${keyName}`);
        location.reload();  // refresh manage-landing card states
    }
}
```

**Step 4: Test all three button paths in browser.**

**Step 5: Commit**

```bash
git add Portal/onboarding/steps/*.js Orchestrator/routes/onboarding_routes.py
git commit -m "feat(portal-ob): manage-mode re-validate / replace / remove handlers"
```

### Task 2.10.5: Portal Web — Add "Manage Setup" button to System Menu

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/index.html` (settings modal)
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/modules/ui-setup.js` (settings menu wire-up)

**Step 1: Add a button in the System section of the settings modal:**

```html
<button id="settings-manage-setup" class="settings-btn">Manage Setup</button>
```

**Step 2: Wire it in `ui-setup.js`:**

```javascript
document.getElementById("settings-manage-setup").addEventListener("click", () => {
    location.href = "/onboarding/?mode=manage";
});
```

**Step 3: Test in browser** — open Portal, hamburger → System → Manage Setup → confirms wizard opens in manage mode.

**Step 4: Commit**

```bash
git add Portal/index.html Portal/modules/ui-setup.js
git commit -m "feat(portal): System Menu 'Manage Setup' button opens onboarding ?mode=manage"
```

### Task 2.10.6: Portal Android — Add "Manage Setup" button to System Menu

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt`

**Step 1: Add a `MenuButton` to the System section:**

```kotlin
MenuButton("Manage Setup") {
    val origin = SettingsViewModel.currentOrigin.value ?: "http://localhost:9091"
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse("$origin/onboarding/?mode=manage"))
    context.startActivity(intent)
    onDismiss()
}
```

(Open in default browser since the wizard isn't yet a native Android view. Future enhancement: render in WebView inside the Portal app.)

**Step 2: Test on Android** — System → Manage Setup → opens browser to wizard at maintenance landing page.

**Step 3: Commit**

```bash
git add "AI_BlackBox_Portal_Android_MVP (2)/.../SettingsSheet.kt"
git commit -m "feat(android): System Menu 'Manage Setup' button opens onboarding ?mode=manage"
```

### Task 2.10.7: Phase 2.10 acceptance test

**Goal:** End-to-end verification of manage mode after onboarding has been completed (Scenarios 1+ from main verification suite).

```bash
# Pre-conditions: complete onboarding fully (Scenario 1 from verification)
# Then:

# (a) Manage UI accessible from /onboarding/?mode=manage
curl -sI http://localhost:9091/onboarding/?mode=manage | grep "200" || { echo "FAIL: manage URL 404s"; exit 1; }

# (b) /current-config returns expected JSON shape
curl -s http://localhost:9091/onboarding/current-config | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'providers' in d
assert 'operators' in d
assert d['providers']['openai']['present']
assert 'last4' in d['providers']['openai']
print('OK: /current-config shape valid')
"

# (c) Reveal endpoint returns full key (loopback OK)
curl -s "http://localhost:9091/onboarding/config/OPENAI_API_KEY?reveal=1" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert len(d['value']) > 20, 'reveal returned masked value'
print('OK: reveal works on loopback')
"

# (d) Manual: Portal Web System → Manage Setup → opens manage landing
# (e) Manual: Android System → Manage Setup → opens browser to manage landing
# (f) Manual: Click each card; confirm step UI shows current state header + reveal/edit/remove buttons
# (g) Manual: Click eye icon; full key reveals. Click again; mask returns.
# (h) Manual: Click Remove on a non-essential provider (e.g. Perplexity if configured); confirm DELETE works + .env.backup created
```

### Task 2.10.8: Track 2 final checkpoint

```bash
git tag -a track2-portal-ui-complete -m "Portal onboarding wizard complete — all 7 steps + branding + manage mode"
git push origin track2-portal-ui-complete
```

---

# TRACK 3 — TAURI SHELL APP

**Goal:** Standalone Rust app wrapping the Portal `/onboarding` webview. Provides full-screen branded chrome, taskbar icon, no browser address bar. ~10MB binary. Bundled as `.deb` and `.AppImage` for Linux.

**Estimated effort:** 1-2 weeks
**Dependencies:** Track 2 (or can mock against placeholder)
**Outcome:** A standalone app `blackbox-setup` that launches the wizard in a polished native window.

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

**Step 1: Install Tauri prerequisites** (system + cargo):

```bash
# System deps for Tauri on Ubuntu
sudo apt install -y libwebkit2gtk-4.1-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev

# Rust toolchain (if not already)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
cargo install create-tauri-app --locked
```

**Step 2: Scaffold the project**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
cargo create-tauri-app installer --template vanilla --identifier com.blackbox.setup --name "BlackBox Setup"
cd installer
npm install   # Tauri scaffolding requires this even for vanilla
```

**Step 3: Update `installer/src-tauri/tauri.conf.json`** — set the dev URL to point at our running Orchestrator:

```json
{
  "build": {
    "devUrl": "http://localhost:9091/onboarding/",
    "frontendDist": "../dist",
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
      "transparent": false,
      "url": "http://localhost:9091/onboarding/"
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

**Step 4: Smoke test build**

```bash
cd installer
cargo build --manifest-path src-tauri/Cargo.toml --release  # ~5-10 min first time
```

**Step 5: Commit**

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
    if !wait_for_server("http://localhost:9091/health", 90) {
        eprintln!("Orchestrator failed to come up within 90s");
        std::process::exit(1);
    }
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

**Step 3: Commit**

```bash
git add installer/src-tauri/tauri.conf.json installer/src-tauri/src/main.rs
git commit -m "feat(installer): wait-for-orchestrator + fullscreen no-decoration window"
```

## Phase 3.3: Branding (icons + splash)

### Task 3.3.1: Generate icon set + splash

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
Exec=/usr/local/bin/blackbox-setup --first-run
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
Exec=/usr/local/bin/blackbox-setup
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
            let window = tauri::WindowBuilder::new(
                app,
                "main",
                tauri::WindowUrl::External(url.parse().unwrap()),
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
# Simulate autostart (first-run)
/usr/local/bin/blackbox-setup --first-run
# Expect: fullscreen window, no decorations, opens at /onboarding/?mode=setup

# Simulate manual launch from desktop applications menu
/usr/local/bin/blackbox-setup
# Expect: windowed (1280x800), decorations on, opens at /onboarding/?mode=manage
# (assuming onboarding has been completed; otherwise opens setup)
```

**Step 3: Commit**

```bash
git add installer/src-tauri/src/main.rs installer/src-tauri/tauri.conf.json
git commit -m "feat(installer): mode-aware launch — --first-run flag routes setup vs manage

Autostart invocation passes --first-run (fullscreen setup mode).
Persistent launcher invocation reads /onboarding/state and opens
in setup or manage mode automatically (windowed with decorations)."
```

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

# TRACK 4 — INSTALL SCRIPTS

**Goal:** Modernized `Scripts/install.sh` that template-substitutes paths, installs systemd unit, and is idempotent on re-run. Plus a factory-image build script (skeletal — fills in once hardware spec is locked).

**Estimated effort:** 1 week
**Dependencies:** Track 0 (paths), Track 3 (Tauri binary to install)
**Outcome:** A single command bootstraps a fresh Ubuntu 24.04 mini-PC into a working BlackBox.

## Phase 4.1: Modern install.sh

### Task 4.1.1: Modernized installer

**Files:**
- Modify: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/setup.sh`
- Create: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Scripts/install.sh` (replaces)

**Step 1: Write the new install.sh:**

```bash
#!/usr/bin/env bash
# AI BlackBox installer — Ubuntu 24.04
set -euo pipefail

# Determine BLACKBOX_ROOT: parent of the directory holding this script
BLACKBOX_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "[install] BLACKBOX_ROOT=$BLACKBOX_ROOT"

# 1. apt deps
echo "[install] Installing system packages..."
sudo apt update
xargs -a "$BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt" -d '\n' \
    grep -vE '^#|^$' \
    | grep '# MUST_HAVE' | awk '{print $1}' \
    | sudo xargs apt install -y

# 2. venv
echo "[install] Creating Python venv..."
python3.12 -m venv "$BLACKBOX_ROOT/Orchestrator/venv"
"$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install --upgrade pip
"$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install -r "$BLACKBOX_ROOT/requirements.txt"

# 3. .env from template
if [[ ! -f "$BLACKBOX_ROOT/.env" ]]; then
    cp "$BLACKBOX_ROOT/.env.template" "$BLACKBOX_ROOT/.env"
    echo "BLACKBOX_ROOT=$BLACKBOX_ROOT" >> "$BLACKBOX_ROOT/.env"
    echo "[install] Created .env from template"
fi

# 4. systemd unit (template-substituted)
echo "[install] Installing blackbox.service..."
sudo tee /etc/systemd/system/blackbox.service > /dev/null <<EOF
[Unit]
Description=AI BlackBox Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BLACKBOX_ROOT
EnvironmentFile=$BLACKBOX_ROOT/.env
ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app --host 0.0.0.0 --port 9091
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now blackbox.service

# 5. Tauri setup app (if .deb is bundled here)
if [[ -f "$BLACKBOX_ROOT/installer/dist/blackbox-setup.deb" ]]; then
    echo "[install] Installing BlackBox Setup app..."
    sudo dpkg -i "$BLACKBOX_ROOT/installer/dist/blackbox-setup.deb" || sudo apt install -fy
fi

# 6a. Autostart .desktop (first-boot launch — removed when onboarding completes)
mkdir -p "$HOME/.config/autostart"
cp "$BLACKBOX_ROOT/installer/dist/blackbox-setup-autostart.desktop" \
   "$HOME/.config/autostart/blackbox-setup.desktop"

# 6b. Persistent desktop launcher (stays forever for re-launch into manage mode)
mkdir -p "$HOME/.local/share/applications"
cp "$BLACKBOX_ROOT/installer/dist/blackbox-setup.desktop" \
   "$HOME/.local/share/applications/blackbox-setup.desktop"
# Refresh the desktop database so the entry appears in the application menu immediately
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "[install] Done. Reboot to launch BlackBox Setup, or run /usr/local/bin/blackbox-setup now."
echo "[install] After first-run, find 'BlackBox Setup' in your applications menu to re-open for maintenance."
```

**Step 2: Test in a clean Ubuntu 24.04 VM** (via vagrant or virt-manager):

```bash
# In a fresh Ubuntu 24.04 box:
git clone https://github.com/TechBran/blackbox-poc.git
cd blackbox-poc
./Scripts/install.sh
# Reboot
# Confirm Tauri setup app autostarts and shows the wizard
```

**Step 3: Commit**

```bash
git add Scripts/install.sh
git rm setup.sh   # delete old, stale installer
git commit -m "feat(install): modernized installer — apt deps, venv, .env, systemd, Tauri app, autostart

Single command bootstraps a fresh Ubuntu 24.04 mini-PC into a working BlackBox.
Replaces the stale setup.sh from Oct 2025."
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
6. **Walk through wizard:** welcome → tailscale → API keys (3 providers) → phone (Twilio) → optional integrations (Gmail) → phone pairing (QR) → operator (enter 2 operator names) → done
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
   - `.onboarding_state.json` exists with `current_step: "phone"` (or wherever you stopped)
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
- **Tier-2 integrations** (ElevenLabs, Asterisk, xAI, Perplexity) — v1.1. (Twilio promoted to Tier-1 per audit decision; was previously deferred.)
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
