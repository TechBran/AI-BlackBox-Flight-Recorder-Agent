# Hardcoded Path References — Audit & Action Plan

**Generated:** 2026-05-10 by T0.3.3
**Source command:**

```bash
grep -rnE "ai-black-box-fc|/home/ai-black-box-fc" \
  --include="*.py" --include="*.kt" --include="*.js" --include="*.html" \
  --include="*.sh" --include="*.service" --include="*.json" --include="*.md" \
  --include="*.toml" --include="*.yaml" --include="*.yml" --include="*.conf" \
  /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/ 2>/dev/null \
  | grep -v -E "venv/|node_modules/|/build/|\.gradle/|/Volume|/Manifest|/Fossils|/Archive|tasks\.db"
```

**Total hits:** 343 (vs spec estimate ~328 — extra came from later commits and the plan's own self-citations).

> **Important framing:** A "hit" is a line where the regex matches. Most of the 343 hits live in two cohorts that need **no** code surgery:
> 1. The plan + discovery + memory docs that *cite paths as examples*.
> 2. The `.claude/settings.local.json` permission allowlist (Claude Code generated; user-local).
>
> The actionable buckets (B1–B4) total **60 hits** across 14 files. B5 (operator-facing docs) is 12 more. Everything else is intentional and stays put.

## Status (Updated 2026-05-10 after Phase 0.3 sweep)

| Bucket | Status | Commit(s) |
|--------|--------|-----------|
| B1 (systemd, 3 hits) | **Deferred to Track 4** — install-time template substitution | T4.1.1 |
| B2 (config files, 9 hits) | **Deferred to Track 4** — install-time template substitution + runtime auto-discovery for devices.json | T4.1.1 |
| B3 (Python/shell, 17 hits) | Done | `7dcbf90` + `d4ef8ec` |
| B4 (App HTML, 31 hits) | Done | `4deafbb` |
| B5 (docs, 12 hits) | Done | `150b94e` |
| B6 (intentional, 271 hits) | Leave alone (per audit) | — |

**Phase 0.3 actionable work complete.** Track 4 will handle B1+B2 via install.sh template substitution.

## Summary by Bucket

| Bucket | Hits | Files | Action | Driving Task |
|--------|------|-------|--------|--------------|
| **B1** systemd unit files | 3 | 1 | Install-time template substitution (User + WorkingDirectory + ExecStart) | T0.3.7 → installer Track 4 |
| **B2** config files (.json) | 9 | 5 | `${BLACKBOX_ROOT}` substitution at install + ship `.example` versions; `devices.json` becomes runtime-discovered | T0.3.7 → installer Track 4 |
| **B3** Python / shell code | 17 | 11 | Replace with `from Orchestrator.utils.paths import ...` (Python) or `${BLACKBOX_ROOT}` env (shell) | T0.3.4 |
| **B4** App HTML media URLs | 31 | 1 (`Apps/echoes-of-titan/index.html`) | Strip absolute Tailscale FQDN → relative `/ui/uploads/...` (served from origin) | T0.3.5 |
| **B5** customer-facing docs | 12 | 2 (`AUDIT_REPORT.md`, `Apps/landing-page/DEPLOYMENT.md`) | `<TAILSCALE_HOSTNAME>` / `<BLACKBOX_ROOT>` placeholders + brief substitution note | T0.3.6 |
| **B6** intentional / leave alone | 271 | 28 | No action — documentation prose, dev-only files, allowlist | — |
| **TOTAL** | **343** | | | |

**60 actionable + 12 doc cleanup = 72 line edits across 17 files.** The remaining 271 hits stay put.

---

## B1 — systemd unit files (3 hits, 1 file)

**Status: DEFERRED TO TRACK 4 — see install.sh template substitution.**

**Action:** Render this `.service` from a `.service.in` template at install time. Substitute `${SERVICE_USER}` and `${BLACKBOX_ROOT}` from the installer's environment (which already has `BLACKBOX_ROOT` and gets `SERVICE_USER` from `id -un`).

| File | Line | Pattern | Action |
|------|------|---------|--------|
| `Apps/system-monitor/blackbox-monitor.service` | 7 | `User=ai-black-box-fc` | Template `User=${SERVICE_USER}` |
| `Apps/system-monitor/blackbox-monitor.service` | 8 | `WorkingDirectory=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/system-monitor` | Template `WorkingDirectory=${BLACKBOX_ROOT}/Apps/system-monitor` |
| `Apps/system-monitor/blackbox-monitor.service` | 9 | `ExecStart=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/system-monitor/server.py` | Template `ExecStart=${BLACKBOX_ROOT}/Apps/system-monitor/server.py` |

> **Note:** The main `blackbox.service` unit lives in `/etc/systemd/system/` (outside the repo) so it doesn't appear in this sweep, but the same install-time substitution rule applies. Document this in the installer.

---

## B2 — Config files (9 hits, 5 files)

**Status: DEFERRED TO TRACK 4 — see install.sh template substitution.**

**Action:** Three sub-strategies:
- **MCP / Gemini configs** → ship as `.example.json`; installer renders the real file with `${BLACKBOX_ROOT}` substituted. Today the `.example` exists alongside the real one for MCP — fix is to make the installer the single writer.
- **`devices.json`** → first device entry is the localhost; runtime should auto-populate `tailscale_hostname` / `tailscale_dns` from `tailscale status --json`. Ship a stub with `<auto-discovered>` placeholders.
- **`css_manifest.json`** → generated artifact from CSS build pipeline; rebuild on install (or convert to a relative path during generation).

| File | Line | Pattern | Action |
|------|------|---------|--------|
| `.mcp.json` | 6 | MCP server `args[0]` = `/home/ai-black-box-fc/.../MCP/blackbox_mcp_server.py` | Render from `.mcp.json.in` at install with `${BLACKBOX_ROOT}` |
| `.mcp.json` | 10 | `BLACKBOX_ROOT` env var hardcoded | Render from `.mcp.json.in` at install |
| `MCP/claude_mcp_config.example.json` | 6 | Same as above (example file) | Replace literal with `${BLACKBOX_ROOT}` placeholder + comment |
| `MCP/claude_mcp_config.example.json` | 10 | Same | Replace literal with `${BLACKBOX_ROOT}` placeholder |
| `.gemini/settings.json` | 6 | Gemini CLI MCP `args[0]` | Render from `.gemini/settings.json.in` at install |
| `.gemini/settings.json` | 10 | Gemini CLI `BLACKBOX_ROOT` env | Render from `.gemini/settings.json.in` at install |
| `Orchestrator/device_registry/devices.json` | 17 | `tailscale_hostname` literal | Stub `<auto-discovered>`; populate at first boot from `tailscale status --json` |
| `Orchestrator/device_registry/devices.json` | 18 | `tailscale_dns` literal | Same — auto-discover |
| `Portal/styles/css_manifest.json` | 5 | `source.file` absolute path in CSS pipeline manifest | Regenerate at install via the CSS build script (or change generator to emit relative path) |

---

## B3 — Python / shell code (17 hits, 11 files)

**Action:** Python files migrate to `from Orchestrator.utils.paths import blackbox_root, resolve, portal_dir, uploads_dir, ...`. Shell scripts read `${BLACKBOX_ROOT}` (from `Orchestrator/Secrets/.env` or environment). The PelvicVibe `Apps/PelvicVibeAndroid/audio_library/*.py` scripts are one-off TTS generators (likely "stale — not in active use" but listed here in case someone re-runs them).

| File | Line | Pattern | Action |
|------|------|---------|--------|
| `Orchestrator/routes/agent_routes.py` | 801 | `default_dir = "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"` | `default_dir = str(blackbox_root())` |
| `Orchestrator/routes/chat_routes.py` | 3186 | `cwd="/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"` | `cwd=str(blackbox_root())` |
| `Orchestrator/routes/gemini_agent_routes.py` | 474 | `default_dir = "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"` | `default_dir = str(blackbox_root())` |
| `Orchestrator/routes/twilio_routes.py` | 1811 | `os.path.join("/home/ai-black-box-fc/.../blackbox_poc", audio_request.file_path.lstrip('/'))` | `resolve(audio_request.file_path.lstrip('/'))` |
| `Orchestrator/phone/bridge.py` | 2427 | `"working_directory": "/home/.../blackbox_poc"` | `"working_directory": str(blackbox_root())` |
| `Orchestrator/phone/bridge.py` | 2476 | Same as above | Same |
| `Orchestrator/phone/bridge.py` | 2492 | Same as above | Same |
| `Orchestrator/phone/bridge.py` | 2985 | `working_dir = "/home/.../blackbox_poc"` | `working_dir = str(blackbox_root())` |
| `MCP/blackbox_mcp_server.py` | 40 | `BLACKBOX_ROOT = Path(os.getenv("BLACKBOX_ROOT", "/home/.../blackbox_poc"))` | **Already env-aware** — change fallback to `Path(__file__).resolve().parent.parent` (i.e., walk up from MCP/ to repo root). Removes the literal default. |
| `Orchestrator/asterisk/configs/setup.sh` | 2 | `S="/home/.../Orchestrator/asterisk/configs"` | `S="${BLACKBOX_ROOT:?}/Orchestrator/asterisk/configs"` |
| `Scripts/setup-cellular-polkit.sh` | 11 | `SERVICE_USER=$(systemctl show ... -p User --value 2>/dev/null \|\| echo "ai-black-box-fc")` | Change fallback to `$(id -un)` |
| `Apps/PelvicVibeAndroid/audio_library/generate_all_tts.py` | 18 | `BASE_DIR = Path("/home/.../audio_library")` | `BASE_DIR = Path(__file__).resolve().parent` (script-local; doesn't need `paths.py`) |
| `Apps/PelvicVibeAndroid/audio_library/generate_male_numbers.py` | 16 | `Path("/home/.../audio_library/male")` | `Path(__file__).resolve().parent / "male"` |
| `Apps/PelvicVibeAndroid/audio_library/generate_male_numbers_41_60.py` | 17 | Same | Same |
| `Apps/PelvicVibeAndroid/audio_library/generate_male_numbers_41_60.py` | 49 | `env_path = "/home/.../Orchestrator/Secrets/.env"` | Walk up to find repo root, then `.../Orchestrator/Secrets/.env`; or use `paths.resolve("Orchestrator/Secrets/.env")` |
| `Apps/PelvicVibeAndroid/audio_library/generate_tts_curl.sh` | 5 | `AUDIO_LIB="/home/.../audio_library"` | `AUDIO_LIB="$(dirname "$(realpath "$0")")"` |
| `Apps/PelvicVibeAndroid/audio_library/generate_tts_curl.sh` | 6 | `UPLOADS_DIR="/home/.../Portal/uploads"` | `UPLOADS_DIR="${BLACKBOX_ROOT:?}/Portal/uploads"` |

> **Note for T0.3.4:** the 8 Orchestrator hits + the MCP server fallback are the must-fix items. The 5 PelvicVibe TTS scripts and the Asterisk setup.sh are "nice to have" — they're not in the request path.

---

## B4 — App HTML media URLs (31 hits, 1 file)

**Action:** Replace `https://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net/ui/uploads/...` with `/ui/uploads/...` (relative path; same origin). The Echoes of Titan app is served via `/app-proxy/{port}/` — when it loads through the Portal, the browser's origin is the Tailscale hostname automatically, so relative URLs Just Work.

If the proxy doesn't rewrite the document base correctly, add `<base href="/">` to `<head>`.

**All 31 hits are in `Apps/echoes-of-titan/index.html`.** The pattern is uniform: `src="https://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net/ui/uploads/<UUID>.<ext>"` on `<source>`, `<video>`, `<audio>`, `<img>` elements.

| File | Lines | Pattern | Action |
|------|-------|---------|--------|
| `Apps/echoes-of-titan/index.html` | 227, 241, 246, 254, 259, 267, 272, 280, 285, 293, 298, 306, 311, 319, 324, 332, 337, 345, 350, 358, 363, 371, 376, 384, 389, 397, 402, 410, 415, 423, 428 | `src="https://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net/ui/uploads/{UUID}.{ext}"` (28 lines have one match; 3 lines have the `<source>` tag) | Bulk replace `https://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net/ui/uploads/` → `/ui/uploads/` |

> **One-shot fix:** `sed -i 's|https://ai-black-box-fc-a620ai-wifi\.tail401fb3\.ts\.net/ui/uploads/|/ui/uploads/|g' Apps/echoes-of-titan/index.html` — manual verification recommended after.

> **Other Apps/ HTML:** `Apps/landing-page/`, `Apps/system-monitor/`, `Apps/grocery-store/`, etc. did NOT match the grep — they were already built with relative URLs or don't reference uploads. Echoes of Titan is the lone offender.

---

## B5 — Customer-/operator-facing docs (12 hits, 2 files)

**Action:** Replace literal hostname/path with `<TAILSCALE_HOSTNAME>` and `<BLACKBOX_ROOT>` placeholders. Add a sentence at the top of each doc: *"Replace `<TAILSCALE_HOSTNAME>` with your machine's Tailscale FQDN (find it with `tailscale status --json | jq -r .Self.DNSName`) and `<BLACKBOX_ROOT>` with your repo install path."*

| File | Line | Pattern | Action |
|------|------|---------|--------|
| `AUDIT_REPORT.md` | 150 | `default_origin = http://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net:9091/ui/` | `<TAILSCALE_HOSTNAME>` substitution |
| `AUDIT_REPORT.md` | 184 | `✅ Hostname: ai-black-box-fc-a620ai-wifi` | Note as "(example hostname)" |
| `AUDIT_REPORT.md` | 329 | `ReadWritePaths=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc` | `<BLACKBOX_ROOT>` |
| `AUDIT_REPORT.md` | 363 | `cat > /home/ai-black-box-fc/blackbox-monitor.sh` | `cat > $HOME/blackbox-monitor.sh` |
| `AUDIT_REPORT.md` | 374 | `chmod +x /home/ai-black-box-fc/blackbox-monitor.sh` | `chmod +x $HOME/blackbox-monitor.sh` |
| `AUDIT_REPORT.md` | 377 | `echo "0 * * * * /home/ai-black-box-fc/blackbox-monitor.sh" \| crontab -` | `$HOME/blackbox-monitor.sh` |
| `AUDIT_REPORT.md` | 385 | `cat > /home/ai-black-box-fc/blackbox-backup.sh` | `$HOME/...` |
| `AUDIT_REPORT.md` | 394 | `chmod +x /home/ai-black-box-fc/blackbox-backup.sh` | `$HOME/...` |
| `AUDIT_REPORT.md` | 397 | `echo "0 3 * * 0 /home/ai-black-box-fc/blackbox-backup.sh" \| crontab -` | `$HOME/...` |
| `AUDIT_REPORT.md` | 405 | `cat > /home/ai-black-box-fc/blackbox-update.sh` | `$HOME/...` |
| `AUDIT_REPORT.md` | 423 | `chmod +x /home/ai-black-box-fc/blackbox-update.sh` | `$HOME/...` |
| `Apps/landing-page/DEPLOYMENT.md` | 10 | `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/landing-page` | `cd <BLACKBOX_ROOT>/Apps/landing-page` |

> **Note:** `AUDIT_REPORT.md` is dated 2025-11-16 and may itself be obsolete. T0.3.6 should consider whether to substitute placeholders or archive the file. Recommend keeping it but adding a placeholder header.

---

## B6 — Intentional / leave alone (271 hits, 28 files)

These hits are **expected** and should NOT be touched. Grouped by reason:

### B6a — Plan & memory documents (cite paths as examples) — 165 hits

These are dev-team plan files. They reference `/home/ai-black-box-fc/...` because they were written ON this machine and quote real file paths in their narrative. Substituting placeholders would make them less useful as historical references.

| File | Hits | Reason |
|------|------|--------|
| `docs/plans/2026-05-10-onboarding-flow.md` | 82 | THE ACTIVE PLAN driving this very task; cites paths constantly as it specifies where files live |
| `docs/plans/2026-05-07-gpu-offload-three-piece-v2.md` | 30 | Implementation plan; cites Jetson/Linux paths verbatim |
| `docs/plans/2026-04-24-android-context-retrieval-fix.evidence.md` | 21 | Bench evidence with real paths in copy/pasted command output |
| `docs/plans/2026-05-09-cli-agent-codex-and-followups.md` | 8 | Plan with example paths |
| `docs/plans/2026-04-16-ugv-toolvault-voice.md` | 8 | Plan with example paths |
| `docs/plans/2026-02-05-contact-book-implementation.md` | 7 | Plan with example paths |
| `docs/plans/2026-05-05-er-and-live-impl.md` | 6 | Plan with example paths |
| `docs/plans/2026-05-05-gpu-offload-three-piece.md` | 5 | Plan v1 (superseded by v2 above) — still useful reference |
| `docs/plans/2026-04-26-system-audio-via-pipewire.md` | 4 | Plan with example paths |
| `docs/plans/2026-02-18-multi-provider-computer-use.md` | 4 | Plan with example paths |
| `docs/plans/2026-03-15-portal-performance.md` | 3 | Plan with example paths |
| `docs/plans/2026-05-08-gemini-cli-bridge-integration.md` | 2 | Plan with example paths |
| `docs/plans/2026-04-27-estop-fanout.md` | 2 | Plan with example paths |
| `docs/plans/2026-04-24-supervisor-2-5-ga-async-aec.md` | 2 | Plan with example paths |
| `docs/plans/2026-04-24-android-context-retrieval-fix.md` | 2 | Plan with example paths |
| `docs/plans/2026-03-16-android-voice-pipeline-fix.md` | 2 | Plan with example paths |
| `docs/plans/notes/2026-04-26-pulse-preflight.md` | 1 | Plan note |
| `docs/plans/2026-04-30-cli-agent-android-mvp-plan.md` | 1 | Plan with example paths |
| `docs/plans/2026-04-30-cli-agent-android-mvp-design.md` | 1 | Plan with example paths |
| `docs/plans/2026-04-26-imu-zupt-preprocessor.md` | 1 | Plan with example paths |
| `docs/plans/2026-04-25-supervisor-rawws-port.md` | 1 | Plan with example paths |
| `docs/plans/2026-03-24-asterisk-audio-pipeline.md` | 1 | Plan with example paths |
| `docs/plans/2026-02-09-web-tools-robustness.md` | 1 | Plan with example paths |

### B6b — Onboarding discovery + active design docs — 5 hits

| File | Hits | Reason |
|------|------|--------|
| `docs/onboarding/discovery-notes.md` | 5 | Lives in the same family as this audit doc; cites paths to make the case for fixing them. Self-referential. |

### B6c — Dev-team CLAUDE / Claude.md instructions — 4 hits

| File | Hits | Reason |
|------|------|--------|
| `Claude.md` | 4 | Per-machine Claude Code instructions describing THIS deployment. Customer machines will get a different `Claude.md` (or none). Leave alone for the dev box. |

### B6d — Slash command teaching files — 5 hits

| File | Hits | Reason |
|------|------|--------|
| `.claude/commands/register-app.md` | 5 | Teaches Claude how to register an app on THIS box. Per-machine. Customer install will overwrite from template anyway. |

### B6e — Claude Code allowlist (auto-generated) — 62 hits

| File | Hits | Reason |
|------|------|--------|
| `.claude/settings.local.json` | 62 | Permission allowlist auto-written by Claude Code as the user accepts Bash permissions over time. Per-user/per-machine. Should be `.gitignore`d going forward (separate cleanup task — not in scope here). |

### B6f — Memory file index entries (NOT in this grep, but worth flagging)

The `~/.claude/projects/.../memory/MEMORY.md` file is outside the repo so it doesn't appear in the sweep, but it cites paths heavily. **No action** — it's per-user memory, not shipped to customers.

---

## What's Missing From This Audit

This grep is intentionally narrow. The following are NOT covered (out of scope for T0.3.3):

- **Tailscale FQDN** in code that doesn't include `ai-black-box-fc` (e.g., references to `tail401fb3.ts.net` standing alone) — none found in this sweep, but should be re-verified after T0.3.5 lands.
- **Database / SQLite paths** (`tasks.db`, snapshot index) — excluded by the `tasks.db` filter. Already use `BLACKBOX_ROOT`-relative resolution.
- **`venv/`, `node_modules/`, `build/`, `Volume/`, `Manifest/`, `Fossils/`, `Archive/`** — excluded by design (binary / build output / immutable ledger).
- **Symlinks** in `~/Desktop/` etc. — out of scope; the path utility handles canonicalization.

---

## Bucket → Task Mapping

| Bucket | Driving Task | Status | Commits |
|--------|--------------|--------|---------|
| B1 (3 hits, 1 file) | T4.1.1 (installer Track 4) | Deferred | — |
| B2 (9 hits, 5 files) | T4.1.1 (installer Track 4) | Deferred | — |
| B3 (17 hits, 11 files) | T0.3.4 | Done | `7dcbf90` + `d4ef8ec` |
| B4 (31 hits, 1 file) | T0.3.5 | Done | `4deafbb` |
| B5 (12 hits, 2 files) | T0.3.6 | Done | `150b94e` |
| B6 (271 hits, 28 files) | — | No action | — |
