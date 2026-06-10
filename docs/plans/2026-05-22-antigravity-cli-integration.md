# Antigravity CLI Integration — 4th Provider Alongside Claude/Gemini/Codex

> **For Claude:** Execute as one focused multi-commit chain across backend + onboarding + Portal + Android. Push to origin/main after each track verified.

**Goal:** Add Google's Antigravity CLI (`agy` binary) as a 4th provider in the CLI Agents stack: backend supported-providers tuple, onboarding install + auth wizard, Portal CLI Agents modal, Android MVP CLI agent picker. Gemini CLI stays for now; will be removed in a separate follow-up after Antigravity validates end-to-end on customer hardware.

**Tech stack:** Python FastAPI backend (cli_agent_routes.py, onboarding_routes.py), vanilla JS Portal (cli-agents-modal.js), Kotlin Compose Android (Constants.kt + cli_agent/*).

---

## Research findings (Track 0, completed)

Brandon shared the official docs URL (https://antigravity.google/docs/cli-overview). Install script downloaded + inspected; binary installed on dev box; `agy --help` and `agy --version` run successfully.

| Fact | Value | Source |
|---|---|---|
| Binary name | `agy` | Install script line 54: `BINARY_PATH="$TARGET_DIR/agy"` |
| Install path | `$HOME/.local/bin/agy` | Install script line 12: `TARGET_DIR="$HOME/.local/bin"` |
| Install command | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | Brandon's docs paste |
| Version | 1.0.1 (`agy --version`) | Dev box install |
| Subcommands | `install` (self-setup), `update`, `plugin`/`plugins`, `changelog`, `help` | `agy --help` |
| Flags | `-c/--continue`, `-p/--print`, `-i/--prompt-interactive`, `--add-dir`, `--dangerously-skip-permissions`, `--sandbox`, `--log-file`, `--conversation`, `--print-timeout` | `agy --help` |
| TUI framework | bubbletea (Go) — requires real TTY | `agy 2>&1` in non-TTY environment errors: `bubbletea: could not open TTY: open /dev/tty` |
| Auth flow | OS keyring (Linux: `secret-service`); implicit on first interactive run; **NO `agy login` subcommand** | Brandon's docs paste + `agy --help` (no auth/login command listed) |
| Logout | Interactive `/logout` command (inside the running TUI session, like Claude Code's `/exit`) | Brandon's docs paste |
| Install side-effects | Appends `export PATH="/home/<user>/.local/bin:$PATH"` to both `~/.bashrc` AND `~/.profile` | Dev box install log |
| Auto-update | Built-in via `agy update` | `agy --help` |
| Enterprise auth | Optional GCP project link (out of v1 scope; not adding GCP plumbing) | Brandon's docs paste |

---

## Architectural differences from Claude/Gemini/Codex

| Concern | Existing 3 providers | Antigravity |
|---|---|---|
| **Distribution** | npm (`@anthropic-ai/claude-code`, `@google/gemini-cli`, `@openai/codex`) | curl-piped shell script (Google Cloud Run signed builds) |
| **Install command** | `npm install -g <pkg>` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` |
| **Auth detection** | File exists check (e.g., `~/.claude/.credentials.json`, `~/.gemini/oauth_creds.json`, `~/.codex/auth.json`) | OS keyring — **no file to check** |
| **Login command** | `claude` / `gemini` / `codex login` | **None** — auth triggers implicitly on first interactive `agy` launch |
| **Binary location** | `~/.npm-global/bin/<name>` (system PATH) | `~/.local/bin/agy` (user PATH; PATH appended at install) |
| **PTY-bridge fit** | Drop-in (bubbletea-style TUI) | Drop-in (also bubbletea-style TUI — identical to Claude Code) |
| **Workspace flag** | Each provider has its own equivalent (Claude `--add-dir`, codex's `--cwd`, etc.) | Has `--add-dir` (same name as Claude's flag) |
| **Continue-conversation** | Claude has `-c`, gemini has `--continue`, codex none | `-c` / `--continue` |

Three of these (install method, auth detection, login command absence) require new code paths. The rest slot in via existing tables.

---

## Design decisions (locked, propose to Brandon for sign-off)

### D1: Install dispatch — new `INSTALL_COMMANDS` table

Current `onboarding_routes.py:725` `INSTALL_PACKAGES` table maps each provider to an npm package name; the install function does `npm install -g <pkg>`. This assumes npm, doesn't fit Antigravity.

Replace with `INSTALL_COMMANDS` table that maps each provider to a full shell command:

```python
INSTALL_COMMANDS = {
    "claude":      ["npm", "install", "-g", "@anthropic-ai/claude-code"],
    "gemini":      ["npm", "install", "-g", "@google/gemini-cli"],
    "codex":       ["npm", "install", "-g", "@openai/codex"],
    "antigravity": ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"],
}
```

Each entry is a list of args passed to `subprocess.run(...)`. Antigravity needs `bash -c "..."` because the curl-piped install is a shell pipeline, not a single binary invocation. The other three become equivalent: `subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"])`. No behavioral change for existing providers — just generalized.

### D2: Auth detection — best-effort, no false negatives

Antigravity's keyring auth can't be file-checked. Two options:

**D2a: Use `agy -p "ping" --print-timeout 5s`** in a non-TTY subprocess and inspect exit code / stderr. If auth-required, expect a non-zero exit with a recognizable error string. If authenticated, expect a real response (success, but slow ~3-5s call) or a "no response yet" timeout (still success — means auth passed, just no immediate response).

Pros: real test. Cons: slow (3-5s every onboarding check); needs API quota; will burn a small amount of Brandon's Antigravity allowance per check.

**D2b: Track only "installed" state for Antigravity. Don't try to detect auth.**

Status response for Antigravity: `{"installed": bool, "authenticated": None, "auth_method": "implicit_on_launch"}`. Wizard renders: "Installed ✓ — click Launch to sign in (browser will open)" instead of the "Login" button.

Pros: simple, no API burn, no false negatives. Cons: user can't see auth state at a glance from the wizard.

**Recommend D2b** for v1. If Brandon wants real auth detection later, add D2a as a follow-up. Match the simpler UX first; the auth flow is genuinely "click Launch, OAuth prompts, done" — that's not worse than seeing "Authenticated ✓" before launch.

### D3: "Login" button behavior

For Claude/Gemini/Codex, the onboarding wizard's Login button runs `<provider> login` in a terminal. For Antigravity, that command doesn't exist.

Two options:
- **D3a**: Reuse the same UI pattern but run `agy` (the binary itself) instead of `agy login`. Auth triggers automatically. After first OAuth completes, subsequent runs use the keyring silently.
- **D3b**: Replace the "Login" button with "Launch & Sign In" for Antigravity, copy explaining that auth happens on first launch.

**Recommend D3b** — small per-provider copy override in the wizard makes the UX accurate. The button still launches a terminal session with `agy`, but the label sets the right expectation.

### D4: Provider tuple ordering

`SUPPORTED_PROVIDERS = ("claude", "gemini", "codex", "antigravity")` — Antigravity appended at the end. Maintains backward compat with any existing data that assumed indices.

In UI radios (Portal modal, Android picker, onboarding wizard), Antigravity shows LAST in the order — least disruptive position. Brandon's stated future intent: "Later we'll remove Gemini after we validate Antigravity" — after that, order becomes Claude / Codex / Antigravity.

### D5: PATH for systemd-spawned PTY processes

The install script appends `~/.local/bin` to PATH in `~/.bashrc` and `~/.profile`. The blackbox.service systemd unit may not source either (depends on its `Type=` and `Environment=` config). If our PTY-bridge tries `subprocess.Popen(["agy"])` from a systemd context, PATH may not include `~/.local/bin` → ENOENT.

Two fixes:
- **D5a**: Add `Environment="PATH=/home/<user>/.local/bin:..."` to the service unit + restart.
- **D5b**: Resolve the absolute binary path at PTY spawn time. Existing `PROVIDER_BIN(name)` function in `cli_agent_routes.py:47-50` already does this for the other 3 (per memory notes from earlier refactoring). Extend to look up `agy` via `shutil.which("agy")` with a fallback to `os.path.expanduser("~/.local/bin/agy")`.

**Recommend D5b** — keeps the fix scoped to the PTY-bridge, doesn't require a systemd unit edit per host.

---

## Source-of-truth file map

| Concern | File | Specific lines (verified) |
|---|---|---|
| Backend supported providers | `Orchestrator/routes/cli_agent_routes.py` | Line 47: `SUPPORTED_PROVIDERS` tuple; line 82: `EXTRA_FLAGS` per-provider; line 143: validation |
| Backend binary lookup | `Orchestrator/routes/cli_agent_routes.py` | Per-call `provider_bin(name)` function (per memory `feedback_pty_bridge_over_headless.md`) |
| Onboarding credential files | `Orchestrator/routes/onboarding_routes.py:715-717` | `CREDENTIAL_FILES` dict |
| Onboarding npm packages | `Orchestrator/routes/onboarding_routes.py:725-727` | `INSTALL_PACKAGES` dict — to be generalized to `INSTALL_COMMANDS` |
| Onboarding login commands | `Orchestrator/routes/onboarding_routes.py:732-736` | `LOGIN_COMMANDS` dict |
| Onboarding status response | `Orchestrator/routes/onboarding_routes.py:779-782` | `/onboarding/status` shape |
| Onboarding install endpoint | `Orchestrator/routes/onboarding_routes.py:809` | `Literal["claude", "gemini", "codex"]` → add `"antigravity"` |
| Onboarding wizard UI | `Portal/onboarding/steps/cli_agents.js` (verify exact path during execution) | Provider grid + per-provider Install/Login buttons |
| Portal CLI Agents modal | `Portal/modules/cli-agents-modal.js` | Provider radio group |
| Portal CLI Agents modal HTML | `Portal/index.html` | `cliAgentsModal` div, provider radio inputs |
| Android CLI agent constants | `AI_BlackBox_Portal_Android_MVP*/.../util/Constants.kt` | CLI provider list (verify exact location during execution) |
| Android CLI agent UI | `AI_BlackBox_Portal_Android_MVP*/.../ui/cli_agent/CliAgentScreen.kt` + `AppFolderPicker.kt` | Provider radio/chip row |

---

## Tracks (commit per track for bisectability)

### Track 1 — Backend: SUPPORTED_PROVIDERS + binary resolution + install dispatch

**Files:**
- `Orchestrator/routes/cli_agent_routes.py` — add `"antigravity"` to `SUPPORTED_PROVIDERS`; extend `provider_bin()` or equivalent to look up `agy` via `shutil.which()` + `~/.local/bin/agy` fallback (D5b)
- `Orchestrator/routes/onboarding_routes.py` — generalize `INSTALL_PACKAGES` → `INSTALL_COMMANDS` (D1); extend `CREDENTIAL_FILES`/`LOGIN_COMMANDS` dicts with Antigravity entries (or null/None for Antigravity per D2b/D3b); update `/onboarding/status` to return `"auth_method": "implicit_on_launch"` for Antigravity; extend `Literal[...]` type union to include `"antigravity"`

**Sub-changes:**
1. `cli_agent_routes.py:47` — `SUPPORTED_PROVIDERS = ("claude", "gemini", "codex", "antigravity")`
2. `cli_agent_routes.py` — find existing `provider_bin()` or equivalent binary-resolution function. Add antigravity branch: `if name == "antigravity": return shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")`. Verify the resolved path exists before returning; else raise the existing "not installed" exception.
3. `cli_agent_routes.py:82` — `EXTRA_FLAGS["antigravity"] = []` (no special PTY flags needed; defaults work — bubbletea + alt-screen handled the same as Claude)
4. `onboarding_routes.py:725-727` — refactor:
   ```python
   INSTALL_COMMANDS = {
       "claude":      ["npm", "install", "-g", "@anthropic-ai/claude-code"],
       "gemini":      ["npm", "install", "-g", "@google/gemini-cli"],
       "codex":       ["npm", "install", "-g", "@openai/codex"],
       "antigravity": ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"],
   }
   ```
   Update the install code that consumes this to do `subprocess.run(INSTALL_COMMANDS[provider])` (it currently builds the npm command inline; needs minor refactor).
5. `onboarding_routes.py:715-717` — `CREDENTIAL_FILES["antigravity"] = []` (empty list — no file-based credentials to check; auth detection lives elsewhere per D2b)
6. `onboarding_routes.py:732-736` — `LOGIN_COMMANDS["antigravity"] = None` (no separate login command; `None` signals "launch the binary itself to trigger auth" per D3b)
7. `onboarding_routes.py:779-782` — `/onboarding/status` response for antigravity: `{"installed": <bool from binary detect>, "authenticated": None, "auth_method": "implicit_on_launch"}`. The frontend renders this differently (D3b).
8. `onboarding_routes.py:809` — extend `Literal[...]` type union: `Literal["claude", "gemini", "codex", "antigravity"]`

**Verification:**
```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
Orchestrator/venv/bin/python -m py_compile Orchestrator/routes/cli_agent_routes.py Orchestrator/routes/onboarding_routes.py
Orchestrator/venv/bin/python -c "from Orchestrator.routes.cli_agent_routes import SUPPORTED_PROVIDERS; assert 'antigravity' in SUPPORTED_PROVIDERS; print('OK')"
# Restart service
echo '<REDACTED-SECRET>' | sudo -S systemctl restart blackbox.service && sleep 20
# /onboarding/status should now include antigravity
curl -s 'http://localhost:9091/onboarding/status?operator=Brandon' | python3 -m json.tool | grep -A 4 antigravity
```

**Commit:** `feat(cli-agent): backend support for Antigravity (agy) as 4th provider`

### Track 2 — Onboarding wizard UI (4th provider card)

**Files:**
- Onboarding step that lists CLI provider cards (likely `Portal/onboarding/steps/cli_agents.js` — verify exact path via `grep -rn 'gemini.*cli\|codex.*login' Portal/onboarding/` during execution)
- CSS for the new card

**Sub-changes:**
1. Add 4th provider card for Antigravity:
   - Logo / icon (use a generic 🚀 or 🪐 emoji unless Antigravity has a brand mark — defer to brand-mark when official assets surface)
   - Description: "Google Antigravity CLI — AI coding agent with GCP integration"
   - Install button: calls existing `/onboarding/install` endpoint with `provider="antigravity"`
   - Status display: matches D2b output (`installed: true` + "Click Launch to sign in")
   - Launch button (NEW for Antigravity, replaces "Login" — D3b): opens the CLI Agents modal with provider preselected to "antigravity"
2. Update wizard's per-provider Status pill rendering to handle `authenticated: null` case (don't render the green check, render the "Click Launch to sign in" copy)

**Verification:**
- Hard-refresh onboarding wizard in browser; navigate to CLI agents step
- Antigravity card appears alongside the other 3
- Click Install → curl-pipe runs to completion → status flips to "Installed"
- Click Launch & Sign In → CLI Agents modal opens with `antigravity` preselected → terminal opens → OAuth prompt triggers in a new browser tab

**Commit:** `feat(onboarding): Antigravity card in CLI agents wizard step`

### Track 3 — Portal CLI Agents modal (4th radio option)

**Files:**
- `Portal/index.html` — add 4th `<input type="radio" name="cliAgentsProvider" value="antigravity">` next to claude/gemini/codex inside the modal's provider radio group
- `Portal/modules/cli-agents-modal.js` — verify `buildSessionName()` matches Python's `session_name()` for the new provider value (probably no code change needed if the format is `cli-agent-{op}__{provider}__{slug}` and the function uses `${provider}` verbatim — but verify)

**Sub-changes:**
1. Add radio: `<label><input type="radio" name="cliAgentsProvider" value="antigravity"> Antigravity</label>`
2. No CSS change (existing `.cli-agents-provider-row` flex layout handles 4 radios fine)
3. Verify the `buildSessionName()` JS function handles `"antigravity"` correctly — it should just plug into the string template; no allowlist client-side

**Verification:**
- Hard-refresh Portal → Tools → CLI Agents
- Antigravity radio appears as 4th option
- Pick app + Antigravity → Launch → terminal opens → `agy` TUI renders cleanly in xterm.js
- If OAuth-required: a new browser tab opens, sign in, return → session continues

**Commit:** `feat(portal): Antigravity provider radio in CLI Agents modal`

### Track 4 — Android MVP CLI agent picker (4th option)

**Files:**
- `AI_BlackBox_Portal_Android_MVP*/.../util/Constants.kt` — find the CLI agent provider list constant. Currently has claude/gemini/codex; add antigravity. (Per memory, may also need to extend the per-provider display-name map.)
- `AI_BlackBox_Portal_Android_MVP*/.../ui/cli_agent/CliAgentScreen.kt` or `AppFolderPicker.kt` — verify the provider chip/radio renders all entries from Constants.kt dynamically; no hardcoded "only 3" assumption
- `AI_BlackBox_Portal_Android_MVP*/.../data/cli_agent/CliAgentSessionRepository.kt` — verify it accepts arbitrary provider strings (probably already does since session-name is constructed client-side; verify)

**Sub-changes:**
1. Add `"antigravity"` to the CLI provider list constant in `Constants.kt`
2. Add display-name mapping if such a map exists (e.g., `"antigravity" → "Antigravity"`)
3. Build APK + verify UI renders all 4 chips/radios

**Verification:**
```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
./gradlew :app:compileDebugKotlin
# Brandon rebuilds + installs APK + smoke tests
```

**Commit:** `feat(android): Antigravity provider in CLI Agent picker`

### Track 5 — End-to-end validation (manual, with Brandon)

**Plan:**
1. Dev box Portal: install Antigravity via onboarding wizard → sign in → launch a CLI Agents modal session → verify TUI renders + sign-in OAuth flow works (do this with a real OAuth — Brandon's call whether to use his Google account or a test account)
2. Dev box Portal CLI Agents modal: directly (skipping wizard) — sign in via the "launch and OAuth" flow, run a few prompts
3. MSO2 Ultra: push update via the existing update pipeline → run onboarding install step on MSO2 → sign in → verify Portal CLI Agents modal works there too
4. Android: APK rebuild + install → CLI Agent screen → Antigravity → sign in flow (Note: OAuth on Android may need different handling — antigravity binary runs on the BACKEND, not Android. So Android picks "antigravity", backend launches `agy` in PTY, OAuth flow happens on whatever browser is reachable from the BACKEND, not Android. Document this UX. Likely fine for trusted-network setups.)

**Commit:** No commit for Track 5 (validation only).

### Track 6 — Polish + push + snapshot

**Files:** `Portal/index.html` cache-buster

**Steps:**
1. Bump cache-buster `?v=genuiN` → `?v=genui<N+1>`
2. Run full verification suite
3. Commit polish + push all tracks to origin/main
4. Mint a snapshot capturing Antigravity integration + the install-dispatch-table generalization pattern

**Commit:** `chore(portal): cache-buster bump + finalize Antigravity integration`

---

## Critical reuse — don't reinvent

| Need | Existing pattern | Source |
|---|---|---|
| PTY-bridge for TUI binaries | `cli_agent_routes.py` WS endpoint at `/cli-agent/ws/{session_id}` | Drop-in for any bubbletea binary |
| Session-name format | `cli-agent-{operator}__{provider}__{slug-or-_root}` | Python `session_name()` + JS `buildSessionName()` |
| xterm.js terminal rendering | Already wired in Portal CLI Agents modal | Tracks 3+4 (CLI Agents + xterm.js swap) yesterday |
| Per-operator session DataStore (Android) | `CliAgentSessionRepository` | Treats provider as opaque string |
| Onboarding install runner | Existing function calling `subprocess.run(["npm", "install", "-g", ...])` | Generalize signature to take a command list (D1) |
| Provider binary lookup | `provider_bin()` per memory `feedback_pty_bridge_over_headless.md` | Extend for antigravity → `shutil.which("agy")` fallback (D5b) |
| Onboarding wizard provider grid | Existing step rendering 3 provider cards | Just add a 4th |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **install.sh script fails on minimal Ubuntu** (curl missing, glibc too old, etc.) | The install.sh has its own error handling; surface stderr to the wizard. If it fails systematically, fall back to documenting the manual install path in a "still doesn't work?" callout. |
| **PATH not set for blackbox.service** so PTY-bridge can't find `agy` | D5b: resolve via `shutil.which("agy")` first, then `~/.local/bin/agy` fallback. Document the systemd Environment fix as a per-host follow-up only if D5b doesn't cover it. |
| **OAuth flow on backend-spawned terminal** opens browser on the BACKEND machine, not the user's machine | For Portal use case: BACKEND is the user's machine, so this works. For Android use case: backend is on MSO2/dev box; OAuth opens a browser there — user has to either be at the machine OR get the auth URL printed to terminal (Antigravity's docs say SSH detection prints URL for paste). Document the Android case clearly in the modal copy. |
| **Auth state invisible in wizard** (D2b — no real auth detection) | Wizard copy explicitly tells user "Click Launch to sign in" — no false promise of authentication-state visibility. Easy upgrade path to D2a later if needed. |
| **`agy update` auto-fires** during a user session, interrupting their work | Document that updates may happen; offer no UI to disable. If it becomes annoying, follow-up adds `--no-update-check` flag if Antigravity has one (not visible in --help today). |
| **Gemini stays alongside Antigravity** during validation period — UI shows 4 providers temporarily | Brandon explicitly OK'd this (his words: "Later we'll remove Gemini after we validate Antigravity"). Document the future-removal item; don't act on it yet. |
| **Existing 3-provider tests break** when INSTALL_PACKAGES is renamed to INSTALL_COMMANDS | Verify all callers update. Hopefully `grep INSTALL_PACKAGES Orchestrator/` returns a manageable list. |
| **Antigravity binary architecture mismatch** (e.g., MSO2 is x86_64, install.sh detects platform correctly?) | The install.sh queries `https://antigravity-cli-auto-updater-...` which presumably serves per-platform builds. The install log on dev box showed `Platform detected: linux_amd64` — matches MSO2 architecture. Should JustWork. |

---

## Out of scope (defer)

- **Removing Gemini CLI** — happens AFTER Antigravity validates on customer hardware. Brandon's explicit plan: "Later we'll remove Gemini after we validate Antigravity."
- **Antigravity enterprise auth** (GCP project linking) — not relevant for personal/individual usage. Add when a customer asks.
- **Plugin management** — Antigravity has `agy plugin` subcommand. Surface in UI later if usage warrants.
- **Real auth-state detection** (D2a) — only if D2b's "click Launch to sign in" UX feels weak in practice.
- **`agy update` UI control** — only if auto-updates become disruptive.
- **Sandbox mode toggle in UI** — `agy --sandbox` is interesting but not v1.
- **`agy --continue` to resume conversations** — could surface as a "Resume last session" button in the modal; not v1.

---

## Verification commands (full suite, run after Track 6)

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc

# Backend: 4 providers in allowlist
Orchestrator/venv/bin/python -c "
from Orchestrator.routes.cli_agent_routes import SUPPORTED_PROVIDERS
assert set(SUPPORTED_PROVIDERS) == {'claude', 'gemini', 'codex', 'antigravity'}, f'got {SUPPORTED_PROVIDERS}'
print('OK')
"

# Backend: install command table
Orchestrator/venv/bin/python -c "
from Orchestrator.routes.onboarding_routes import INSTALL_COMMANDS
assert set(INSTALL_COMMANDS.keys()) == {'claude', 'gemini', 'codex', 'antigravity'}
assert INSTALL_COMMANDS['antigravity'][0:2] == ['bash', '-c']
print('OK')
"

# Onboarding status endpoint includes antigravity
curl -s 'http://localhost:9091/onboarding/status?operator=Brandon' | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'antigravity' in d['cli_agents'], 'antigravity missing from /onboarding/status'
print('antigravity:', d['cli_agents']['antigravity'])
"

# Portal: 4 radios in CLI Agents modal
grep -c 'value="antigravity"' Portal/index.html
# Should be ≥1

# Android: 4 providers in Constants.kt
grep -c '"antigravity"' "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/util/Constants.kt"
# Should be ≥1

# Android compiles
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
./gradlew :app:compileDebugKotlin
# BUILD SUCCESSFUL
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc

# Push
git push origin main
git log origin/main..HEAD  # should be empty
```

---

## Commit map

| # | Track | Commit message | Files | Smoke test |
|---|---|---|---|---|
| 1 | Track 1 | `feat(cli-agent): backend support for Antigravity (agy) as 4th provider` | cli_agent_routes.py, onboarding_routes.py | `/onboarding/status` shows antigravity; SUPPORTED_PROVIDERS includes it |
| 2 | Track 2 | `feat(onboarding): Antigravity card in CLI agents wizard step` | Portal/onboarding/steps/cli_agents.js + CSS | Wizard renders 4th card; Install button runs curl-pipe to completion |
| 3 | Track 3 | `feat(portal): Antigravity provider radio in CLI Agents modal` | Portal/index.html + cli-agents-modal.js | 4th radio renders; selecting it + Launch opens working terminal |
| 4 | Track 4 | `feat(android): Antigravity provider in CLI Agent picker` | Constants.kt + CliAgentScreen.kt or related | APK builds; 4th chip/radio renders; selection works |
| 5 | Track 5 | (validation only — no commit) | n/a | Manual end-to-end on dev box + MSO2 + Android |
| 6 | Track 6 | `chore(portal): cache-buster bump + finalize Antigravity integration` | Portal/index.html | All tracks visible after hard-refresh |

**Push to origin/main after each track verified.** Final snapshot via `/chat/save` after Track 6.
