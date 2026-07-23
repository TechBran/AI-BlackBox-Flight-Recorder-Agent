# Safe Rename Migration — "AI BlackBox Flight Recorder Agent" (Design)

**Date:** 2026-07-23
**Status:** DESIGN — awaiting Brandon's sign-off on the open decisions (§7)
**Goal (Brandon):** Production name **"AI BlackBox Flight Recorder Agent"** everywhere, including the GitHub repo — WITHOUT breaking anything, especially the update pipeline.
**Survey basis:** Full-repo grep sweep of the dev checkout + read-only SSH probes of MS02 (remotes, systemd units, crontab) + dev-box systemd/crontab/MCP configs/venv shebangs/Android/Tauri/installer templates. Spot-verified 2026-07-23 against `Orchestrator/update/git_ops.py`, `Scripts/install.sh`, `Scripts/update.sh`, and live `git remote -v`.

**Headline verdict: LOW RISK.** The update pipeline operates entirely on the local remote *name* `origin` (URL lives in `.git/config`), GitHub 301-redirects all git operations after a repo rename, only 6 literal repo-URL sites exist outside historical docs, and the target display name is already the shipped branding. Every genuinely fragile reference is a filesystem path or machine identifier that gets **grandfathered**, not renamed.

---

## 1. Chosen Names

| Thing | Name | Where it applies |
|---|---|---|
| **GitHub repo slug** | `TechBran/ai-blackbox-flight-recorder-agent` | GitHub repo Settings rename; all clone URLs; `DEFAULT_REMOTE_URL`; installer clone; `Documentation=` unit lines; README |
| **Product display name** | `AI BlackBox Flight Recorder Agent` | README title, Portal `<title>` + home header, Android `app_name`, Tauri `productName`, systemd `Description=` (cosmetic, new installs only), marketing/docs |
| **Short display name** (space-constrained UI) | `AI BlackBox` / `Flight Recorder` | Portal brand-collapsed states, Android launcher (if the full name truncates), tab titles |
| **Machine identifiers** | **UNCHANGED — grandfathered** (§4) | `com.aiblackbox.portal`, `com.blackbox.setup`, `blackbox-mcp`, `blackbox*.service`, `BLACKBOX_*` env vars, existing checkout directories |
| **New-install default directory** (fresh boxes only, optional — D5) | `~/Desktop/ai-blackbox-flight-recorder-agent` | `installer/templates/blackbox-apt-install.sh`, `blackbox-install-zellij-binary.sh`, `MCP/deploy/blackbox-mcp.service`, README clone example |

The display name "AI BlackBox Flight Recorder Agent" is **already shipped** in: `README.md:1`, `Portal/index.html:41` (brand-full span), `.env.template:1`, `CLAUDE.md` heading. This migration is a *convergence*, not an introduction.

---

## 2. Why the Update Pipeline Survives (the load-bearing fact)

Verified mechanics, in order of what actually executes:

1. **Runner code never touches the URL.** `git_ops.fetch_origin_main` (git_ops.py:85-88), `latest_origin_sha` (:91-94), `commits_behind/ahead` vs `origin/main` (:97-108), `reset_hard` (:158-167), all four routes in `Orchestrator/routes/update_routes.py` (`/update/status`, `/preflight`, `/start`, `/rollback`), and `Scripts/update.sh:56-63` all operate on the remote **name** `origin`. The URL is resolved from `.git/config` at fetch time.
2. **GitHub 301-redirects renamed repos indefinitely** for git fetch/push/clone (HTTPS and SSH) and REST API calls. Every existing box — dev, MS02, any customer box — keeps updating *through* the rename with zero action. The commit that updates `DEFAULT_REMOTE_URL` is itself **delivered over the redirect**.
3. **The only code paths embedding the literal URL** are `git_ops.lazy_init` (git_ops.py:29/52 — currently has NO route caller; referenced only by `tests/test_update/test_git_ops.py`) and `Scripts/install.sh:43/50` (fresh installs). Neither runs on an existing box during an update.
4. **No GitHub-API-by-repo-name calls exist anywhere.** The only `api.github.com` use is the name-independent `/zen` connectivity ping (`Scripts/install-preflight.sh:37`). No `.github/` directory, no Actions, no badges, no scripted `gh` usage, no webhooks/deploy-key automation keyed to the name.
5. **Redirect kill-switch (the ONE real hazard):** the redirect dies the instant a *new* repo named `blackbox-poc` is created under `TechBran`. That would silently hijack any box still fetching the old URL. Policy in §5 (do-not-break list) and D3.

---

## 3. Staged Migration Sequence

Each stage is independently shippable and independently verifiable. Do not start stage N+1 until stage N's verification passes.

### S0 — Pre-flight (before touching GitHub)
- Confirm both boxes are clean and current: `git -C <root> status` + `git fetch origin && git rev-parse origin/main` on dev and MS02 (SSH `bbx@192.168.1.153`, checkout `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main`).
- Confirm the tracked feature branch on dev (`feat/android-live-stream-focal-follow`) is pushed or noted — branch upstreams survive the rename via redirect, but know the state before churn.
- Record current SHAs: `git rev-parse origin/main` on both boxes (rollback reference).
- **Verify:** both remotes print `https://github.com/TechBran/blackbox-poc.git`; both fetches succeed.

### S1 — Rename the GitHub repo
- GitHub → repo Settings → rename to `ai-blackbox-flight-recorder-agent`. (Visibility, issues, PRs, stars, collaborators, branch protection, deploy keys, webhooks all survive — keyed to repo id.)
- **Verify (BOTH boxes, old URL still configured):**
  - Dev: `git fetch origin && git rev-parse origin/main` — must succeed via 301 redirect.
  - MS02: same over SSH.
  - `git ls-remote https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git main` — new URL resolves directly.
  - Dev: `curl -s localhost:9091/update/status` — pipeline healthy through the redirect.
- **Rollback:** rename the repo back in Settings. Nothing else has changed; existing boxes never noticed.

### S2 — Repoint existing checkouts explicitly (stop depending on the redirect)
- Dev box: `git remote set-url origin https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git`
- MS02 (SSH): same in `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main`.
- Update the **live** systemd cosmetics on both boxes (informational only, zero runtime effect): `Documentation=` line in `/etc/systemd/system/blackbox.service` (dev + MS02) and MS02's `blackbox-mcp.service` docs line; `sudo systemctl daemon-reload`. May be batched with any later restart — not urgent.
- **Customer boxes / future-proofing (D2):** add a one-shot self-heal to `Scripts/update.sh` (runs on every update): if `git remote get-url origin` matches the old URL, `set-url` to the new one and log it. Idempotent, delivered over the redirect, retires the redirect dependency fleet-wide without SSH.
- **Verify:** `git remote -v` shows the new URL on both boxes; `git fetch origin` succeeds; `/update/status` and `/update/preflight` return healthy on both; run one full `/update/start` cycle on the dev box (this is the dev box — pre-authorized restarts).
- **Rollback:** `git remote set-url origin` back to the old URL (redirect still live either way).

### S3 — In-repo literal URL updates + branding convergence (one commit, delivered via normal update)
The 6 live literal-URL sites (docs/plans left as historical record — do NOT rewrite):
| File | Change |
|---|---|
| `Orchestrator/update/git_ops.py:29` | `DEFAULT_REMOTE_URL` → new URL. Also fix the comment: repo is currently **private**, not public (D6). |
| `Scripts/install.sh:43` | clone URL → new |
| `Scripts/install.sh:50` | lazy-init `remote add origin` → new |
| `Scripts/install.sh:581` | `Documentation=` written into new units → new |
| `README.md:60` | clone command → new URL (+ new default dir if D5 approved) |
| `README.md:833` | GitHub link → new |

Cosmetic branding in the same or an immediately-following commit (Brandon's call, D5):
- `Portal/index.html:5` title → `AI BlackBox Flight Recorder Agent — Portal v<next>`; `:179-180` home header converge on the full name. **Bump `?v=genuiXX` per house rule.**
- Update `tests/test_update/test_git_ops.py` expectations for the new `DEFAULT_REMOTE_URL`.
- **Verify:** `python -m Orchestrator.toolvault.validate` unaffected; update tests pass; grep gate — `grep -rn "TechBran/blackbox-poc" --exclude-dir=venv --exclude-dir=node_modules .` returns ONLY `docs/plans/` + historical docs; dev box pulls the commit through its own `/update/start` (self-hosting proof); Portal hard-refresh shows new titles.
- **Rollback:** `git revert` the commit; boxes pick up the revert on next update. (Per §2, a wrong URL in `DEFAULT_REMOTE_URL` cannot brick existing boxes — they never read it.)

### S4 — Fresh-install defaults (NEW boxes only; existing boxes untouched)
- `installer/templates/blackbox-apt-install.sh:37` and `installer/templates/blackbox-install-zellij-binary.sh:54`: `BLACKBOX_ROOT` default `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main` → new default dir (D5). Both already honor `BLACKBOX_ROOT` env override, so old-path boxes are unaffected.
- `MCP/deploy/blackbox-mcp.service:14,18,20,28`: template paths → new default dir (template only; live units on both boxes are grandfathered).
- `Scripts/install.sh` needs no path change — it derives `BLACKBOX_ROOT` from its own location (`install.sh:19`); it only gets the S3 URL edits.
- Tauri installer: `tauri.conf.json:26` `productName` → `AI BlackBox Flight Recorder Agent Setup`; **`identifier` `com.blackbox.setup` UNCHANGED** (changing it = new app to the OS).
- **Verify (fresh-box gate, per the portable-build rule):** dry-run `install.sh` clone step against the new URL in a scratch dir; shellcheck/spot-read the two templates; confirm `Orchestrator/utils/paths.py` sentinel walk-up (BLACKBOX_ROOT env → CLAUDE.md+Orchestrator/ walk) finds the root under the new dir name — it is name-independent by design.
- **Rollback:** revert the commit. No existing box reads these defaults.

### S5 — Android display name (Portal Android app)
- `app/src/main/res/values/strings.xml:2`: `app_name` `AI BlackBox Portal` → `AI BlackBox Flight Recorder Agent` (or short form if launcher truncation is ugly — check on the Fold).
- **`applicationId`/`namespace` `com.aiblackbox.portal` (app/build.gradle:9,13) UNCHANGED** — survey confirms changing it creates a new app identity that orphans every install and its device pairing. Grandfathered permanently.
- `UpdatesScreen.kt:311` hint copy ("Clones the BlackBox repo…") — optional wording touch-up; no functional client-side repo name exists (`UpdateRepository.kt` only calls `/update/*`).
- **Verify:** `./gradlew :app:testDebugUnitTest --offline` (~35s gate); Fold sideload → launcher name renders, app opens, pairing/session intact (proves identity unchanged). Note: `SessionSwitcherTopBarTest.kt:137-139` fixtures show the *directory basename* in zellij labels — unaffected because local dirs are grandfathered.
- **Rollback:** revert the string; resource-only change.

---

## 4. GRANDFATHER List — must NOT be renamed, ever

GitHub rename alone touches **none** of these. Renaming them is where the actual breakage lives, for zero functional gain.

| Identifier / path | Why renaming breaks things |
|---|---|
| Dev checkout dir `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc` | Bricks BOTH venvs (`Orchestrator/venv/bin/*` + `MCP/venv/bin/*` shebangs embed the absolute path → full rebuilds); breaks dev `/etc/systemd/system/blackbox.service` (WorkingDirectory/EnvironmentFile/ExecStart); breaks `.mcp.json`, `.codex/config.toml`, `~/.claude.json` per-project keys; breaks `Orchestrator/tests/test_stt_no_hardcoded_whisper1.py:10`; changes zellij session labels (dir basename) |
| MS02 checkout dir `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main` | Same class: MS02 `blackbox.service` + `blackbox-mcp.service` (WorkingDirectory/BLACKBOX_ROOT/ExecStart), MS02 venv shebangs |
| systemd unit names `blackbox.service`, `blackbox-mcp.service`, `blackbox-models.service`, `zellij-web.service` | Enable/restart tooling, docs, muscle memory, and the pre-authorized `sudo systemctl restart blackbox.service` all key on these |
| `BLACKBOX_*` env vars (`BLACKBOX_ROOT`, …) | Baked into live units on both boxes, installer templates, `paths.py` resolution |
| Android `applicationId`/`namespace` `com.aiblackbox.portal` | New app identity → orphans all installs, pairing, device-control registrations |
| Tauri `identifier` `com.blackbox.setup` | New app to the OS → duplicate installs, broken upgrade path |
| MCP server protocol name `blackbox-mcp` (MCP/blackbox_mcp_server.py:389) + client config key `blackbox` (.mcp.json:3) | Invalidates remote-MCP client registrations and OAuth grants (74-tool Funnel server is LIVE) |
| Historical docs (`docs/plans/*`, `docs/onboarding/*`, `eval/results/*`) with old URL/paths | Historical record; rewriting corrupts provenance and bloats the diff (~60 files of the 538 raw path matches) |
| `aiblackboxfc.com` references | Marketing domain, orthogonal to the repo rename |

**Rule of thumb:** human-visible strings converge on "AI BlackBox Flight Recorder Agent"; machine identifiers and filesystem paths are permanent legacy. Only *fresh* installs get the new directory default (S4).

---

## 5. Do-Not-Break Checklist (run before declaring victory)

- [ ] **Update pipeline, dev:** `/update/status`, `/update/preflight`, full `/update/start`, `/update/rollback` all green after S2 and again after S3.
- [ ] **Update pipeline, MS02:** `git fetch origin` + `Scripts/update.sh` path works over SSH after S1 (redirect) and S2 (explicit URL).
- [ ] **Old-name squat guard:** NO new repo named `blackbox-poc` is ever created under `TechBran` while any box could still hold the old URL (redirect dies + silent-hijack vector). Optional archived placeholder ONLY after fleet-wide S2 confirmation (D3).
- [ ] **MCP:** dev `.mcp.json` server boots (tools list non-empty — lean-venv rule); MS02 `blackbox-mcp.service` active; remote Funnel clients still authorized (server name unchanged).
- [ ] **Cron:** crontabs were empty on both boxes at survey time — re-check post-migration (`crontab -l` both boxes); in-app cron jobs are repo-name-independent.
- [ ] **systemd:** `systemctl status blackbox.service` healthy on both boxes; `daemon-reload` done if Documentation= lines were touched.
- [ ] **Venvs untouched:** `Orchestrator/venv/bin/python -c "import fastapi"` still works (no dir rename happened).
- [ ] **Android:** unit-test gate passes; Fold install upgrades in place (same applicationId), pairing intact.
- [ ] **Portal:** `?v=` bumped; hard refresh shows new titles; no console errors.
- [ ] **Memory files / CLAUDE.md / AGENTS.md:** references to `blackbox_poc` paths and `blackbox-poc` repo are **informational only** — update opportunistically (CLAUDE.md working-dir examples still correct because dirs are grandfathered); nothing executes from them.
- [ ] **Grep gate:** `grep -rn "TechBran/blackbox-poc"` (venv/node_modules excluded) hits only historical docs.
- [ ] **Snapshot:** `/snapshot-dev` minted documenting the rename (embedding verified in journalctl).

---

## 6. Rollback Summary (per stage)

| Stage | Rollback | Blast radius if skipped |
|---|---|---|
| S1 | Rename repo back in GitHub Settings | None — no box config changed |
| S2 | `git remote set-url origin <old-url>` per box | None — redirect covers either direction |
| S3 | `git revert`; boxes pick it up on next update | Existing boxes unaffected even un-reverted (never read the literals) |
| S4 | `git revert` | Only future fresh installs |
| S5 | Revert strings.xml; reinstall | Display-only |

There is no stage whose failure strands an existing box: the worst case at every point is "old URL + redirect," which is the pre-migration steady state.

---

## 7. Open Decisions (with recommendations)

| # | Decision | Recommendation |
|---|---|---|
| **D1** | GitHub slug | `ai-blackbox-flight-recorder-agent` — matches product name, lowercase-kebab GitHub convention. Alternative `blackbox-flight-recorder` if Brandon wants it shorter. Execute S1+S3 within the same working session so literals never lag the rename by more than one update cycle. |
| **D2** | How existing boxes get `set-url`: manual SSH vs self-heal in `update.sh` | **Both.** Manual SSH for dev+MS02 immediately (S2); ship the idempotent one-shot in `Scripts/update.sh` in the S3 commit so any customer/ZIP box self-heals off the redirect without intervention. ~5 lines, delivered over the redirect itself. |
| **D3** | Old-name policy | Hard rule: never recreate `TechBran/blackbox-poc`. After both boxes (and any customer boxes) are confirmed on the new URL, *optionally* create an empty archived placeholder repo at the old name whose README points to the new slug — this deliberately kills the redirect in a controlled way and permanently blocks squatting. Do NOT do this before fleet confirmation. |
| **D4** | Grandfather ratification | Ratify §4 as written — permanent. Local dirs, venvs, unit names, package ids, MCP names, env prefixes stay. |
| **D5** | New default dir for fresh installs + branding batch timing | Yes to new default dir (`~/Desktop/ai-blackbox-flight-recorder-agent`) — fresh-box gate rule says new boxes should look intentional, and templates already honor `BLACKBOX_ROOT`. Ship S4+S5 cosmetics as a second PR right behind S3 (S3 = correctness, S4/S5 = polish) so a problem in cosmetics never blocks or reverts the URL correctness commit. |
| **D6** | `git_ops.py:29` comment vs reality: comment claims repo is public ("Brandon's T10 decision") but it is private | Surface to Brandon: either (a) flip the repo public at rename time — makes the new clone URL work unauthenticated for fresh installs/lazy_init as the comment intends, or (b) keep private and fix the comment + accept that fresh `install.sh` clones need an auth story (PAT/deploy key) — which they already implicitly do today. The rename itself is neutral here, but the comment must stop lying either way; fix it in the S3 commit. |

---

## 8. Non-Goals

- Renaming local checkout directories on ANY existing box (grandfathered, §4).
- Rewriting historical docs/plans/eval files.
- Changing any machine identifier (package ids, unit names, MCP server name, env prefixes).
- Domain/marketing changes (`aiblackboxfc.com` is a separate surface).
- Snapshot/ledger content — snapshots carry operator names, not the repo name; the Volume is immutable anyway.
