# Track 3 (Tauri Shell App) — Pre-Execution Audit

**Written:** 2026-05-12, after Phase 2.10 closeout
**Purpose:** Surface plan-vs-reality deltas in Track 3 of `docs/plans/2026-05-10-onboarding-flow.md` (lines 2891-3276) before dispatching the first implementer subagent.

**Audit pattern precedent:** The same pre-execution audit on Phase 2.10 caught two real plan deviations (the `manage_landing.js` design that wasn't needed; `ui-setup.js` that DID exist despite plan implying it didn't), saving multiple subagent cycles. Track 3 was written 2026-05-10 against a project state that has since evolved (Track 1 + 2 + foundation-refinements all shipped). It deserves the same audit pass.

---

## TL;DR

The Track 3 plan is **structurally sound** but contains:
- **3 blocker-class** issues that would fail compilation on day one
- **5 major** issues that affect architectural correctness
- **7 minor** polish issues worth fixing opportunistically

Recommended action: apply the 3 blocker fixes + 5 major fixes to the master plan **before** the next session opens. Pre-flight checklist (Rust install + apt -dev packages) becomes a new Task 3.0. Estimated rework: 30-45 minutes of plan editing, no code yet.

---

## Environment reality check (probed 2026-05-12)

| Probe | Result | Plan assumption | Delta |
|---|---|---|---|
| Distro | Ubuntu 24.04.4 LTS (Noble Numbat) | Ubuntu 24.04 | ✓ matches |
| Arch | x86_64 | implied x86_64 | ✓ matches |
| Display | X11 on GNOME (Wayland disabled) | not specified | ✓ Tauri-friendly |
| Rust toolchain | **NOT installed** | plan installs in T3.1.1 Step 1 | ⚠ pre-flight gating |
| `libwebkit2gtk-4.1-0` runtime | installed (2.52.3) | implied | ✓ |
| `libwebkit2gtk-4.1-dev` | **MISSING** | plan installs in T3.1.1 Step 1 | ⚠ pre-flight gating |
| `libxdo-dev` | **MISSING** | plan installs | ⚠ pre-flight |
| `libssl-dev` | **MISSING** | plan installs | ⚠ pre-flight |
| `libayatana-appindicator3-dev` | **MISSING** | plan installs | ⚠ pre-flight |
| `librsvg2-dev` | **MISSING** | plan installs | ⚠ pre-flight |
| `libsoup-3.0-dev` | **MISSING** | **NOT in plan's apt list** | 🔴 plan gap |
| `build-essential` | **MISSING** | plan installs | ⚠ pre-flight |
| `/health` endpoint | **HTTP 200** (returns `{...}`) | plan calls it | ✓ |
| `/onboarding/state` shape | `{is_complete: bool, completed_steps: [...], ...}` | plan reads `is_complete` | ✓ matches |
| Existing Tauri scaffolding on disk | none | greenfield | ✓ clean slate |
| `Scripts/install.sh` exists? | **NO — not present** | Track 4 will create | (Track 4 concern) |
| BlackBox systemd `WorkingDirectory` | dev path (`/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`) | not addressed | (Track 4 concern) |

---

## Findings — categorized by severity

### 🔴 CRITICAL (would block build)

#### C1. Tauri 1.x API names in `main.rs` examples but Tauri 2.x schema in `tauri.conf.json`

**Where:** `docs/plans/2026-05-10-onboarding-flow.md` lines 3214-3225 (Task 3.5.2 main.rs body)

**Problem:** The example uses `tauri::WindowBuilder::new(...)` and `tauri::WindowUrl::External(...)` — these are **Tauri 1.x** type names. In Tauri 2.x (current stable, GA October 2024), they were renamed:
- `WindowBuilder` → `WebviewWindowBuilder`
- `WindowUrl::External(...)` → `WebviewUrl::External(...)`

Meanwhile, the `tauri.conf.json` example at lines 2937-2970 uses Tauri 2.x schema (uses `app.windows[]`, `build.devUrl`, `build.frontendDist`, `bundle.targets`/`bundle.identifier` at root). So the plan is mixing v1 API code with v2 config. Implementer would hit compile errors immediately.

**Recommended fix:**
```rust
// BEFORE (plan's current):
let window = tauri::WindowBuilder::new(
    app, "main", tauri::WindowUrl::External(url.parse().unwrap()),
)

// AFTER (Tauri 2.x correct):
let window = tauri::WebviewWindowBuilder::new(
    app, "main", tauri::WebviewUrl::External(url.parse().unwrap()),
)
```

Also pin Tauri version in `cargo install create-tauri-app`:
```bash
cargo install create-tauri-app --version "^4" --locked  # v4.x scaffolds Tauri 2.x apps
```

#### C2. Missing Cargo dependencies in plan

**Where:** Plan never instructs implementer to add `reqwest`, `dirs`, or `serde_json` to `installer/src-tauri/Cargo.toml`, but the example main.rs uses all three (lines 3017, 3148, 3157, 3206). Tauri 2.x bundles `serde_json` transitively via the `tauri` crate so that one is borderline-OK, but `reqwest` and `dirs` are explicit requirements.

**Recommended fix:** Add a Cargo deps block to T3.1.1 Step 4 (or new Step 3.5):
```toml
# installer/src-tauri/Cargo.toml — add to [dependencies]
reqwest = { version = "0.12", features = ["blocking", "json"] }
dirs    = "5"
serde_json = "1"
```

Note: `reqwest` with the `blocking` feature pulls in tokio's blocking runtime, which adds ~2-3MB to the binary. Acceptable for a setup-shell app. Alternative: use `ureq` (much smaller, blocking-only) for a slimmer binary.

#### C3. `libsoup-3.0-dev` missing from plan's apt install list

**Where:** Plan T3.1.1 Step 1 (line 2916).

**Problem:** Tauri 2.x on Ubuntu 24.04 builds against `webkit2gtk-4.1`, which transitively requires `libsoup-3.0`. The plan installs `libwebkit2gtk-4.1-dev` but not `libsoup-3.0-dev`. On the dev box, `libsoup-3.0-0` runtime is installed but the `-dev` package isn't, and webkit-pkg-config will fail without it.

**Recommended fix:** Add `libsoup-3.0-dev` to the apt install list:
```bash
sudo apt install -y libwebkit2gtk-4.1-dev libsoup-3.0-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
```

---

### 🟠 MAJOR (architectural correctness)

#### M1. `installer/dist/` directory collision (Tauri frontendDist vs .desktop files)

**Where:** Plan T3.1.1 Step 3 sets `"frontendDist": "../dist"` (line 2940). Phase 3.4 puts `.desktop` files in `installer/dist/blackbox-setup-autostart.desktop` and `installer/dist/blackbox-setup.desktop` (lines 3075, 3104).

**Problem:** Tauri will treat `installer/dist/` as the bundled webview content path. Mixing `.desktop` files in there is wrong: (a) they're not webview content, (b) Tauri may try to bundle them into the .deb/.AppImage as part of frontend assets.

**Recommended fix:** Two options:
1. **Preferred:** Move `.desktop` files to `installer/desktop-entries/` (separate dir, won't conflict with anything Tauri owns). Update Phase 3.4.1 + 3.4.2 paths and Track 4's install script reference accordingly.
2. **Alternative:** Set `"frontendDist": ""` in `tauri.conf.json` since we're pointing at an external URL (`http://localhost:9091/onboarding/`) and don't bundle any frontend. Then `.desktop` files in `installer/dist/` are harmless. **Recommended over option 1** — less file churn, more accurate semantics.

#### M2. Window `url` field in tauri.conf.json may be unused/redundant

**Where:** Plan T3.1.1 Step 3 (line 2953) sets `"url": "http://localhost:9091/onboarding/"` inside the static window config.

**Problem:** Tauri 2.x's static `windows[].url` in tauri.conf.json is intended for built-in frontend assets (e.g., `index.html`). For external URLs, the recommended pattern is to construct the webview programmatically via `WebviewWindowBuilder` (which the plan correctly does in Phase 3.5.2). The static `url` field becomes redundant and may even be ignored.

**Recommended fix:** Remove `"url"` from the static window config in Phase 3.1.1's tauri.conf.json. Rely entirely on the programmatic WebviewWindowBuilder in Phase 3.5.2 to set the URL. Add a comment explaining why.

#### M3. Binary install path mismatch — `/usr/local/bin/` vs `/usr/bin/`

**Where:** Plan's `.desktop` files (lines 3083, 3113) reference `Exec=/usr/local/bin/blackbox-setup`. But Tauri's `cargo tauri build` produces a `.deb` that installs to `/usr/bin/blackbox-setup` by default (Tauri bundle convention).

**Problem:** Customers installing via `.deb` would get the binary at `/usr/bin/blackbox-setup` but the `.desktop` files would point to `/usr/local/bin/blackbox-setup` — broken launcher.

**Recommended fix:** Either:
1. Update `.desktop` Exec= lines to `/usr/bin/blackbox-setup` (matches Tauri default — least friction).
2. Override Tauri bundle config to install to `/usr/local/bin/` (more "Linux convention" since Tauri builds aren't from a distro repo, but adds bundle-config complexity).

**Recommended:** Option 1. `/usr/bin/` for .deb is fine and standard for Tauri-bundled apps.

#### M4. `wait_for_server` 90s timeout too tight

**Where:** Plan T3.2.1 Step 2 (line 3026) and T3.5.2 (line 3193).

**Problem:** CLAUDE.md warns BlackBox warmup takes 60-90s due to snapshot index rebuild. A 90s timeout is right at the edge — first-boot-after-install (worst case: cold disk cache + index rebuild from scratch) could exceed 90s.

**Recommended fix:** Bump to 180s. Trade-off: 90 extra seconds of "splash + waiting" UX in the worst case, vs. avoiding a "Orchestrator failed to come up" hard failure on slow hardware.

#### M5. Phase 3.3 promises "branding (icons + splash)" but only implements icons

**Where:** Plan Phase 3.3 (line 3043) section title.

**Problem:** Splash screen is mentioned in the title and implied throughout (good first-launch UX while waiting for server) but never actually implemented. T3.3.1 just generates icons.

**Recommended fix:** Either:
1. Add Task 3.3.2 implementing a splash window (Tauri's native splash pattern: small undecorated WebviewWindow with a static branded HTML/CSS that's destroyed when main window opens).
2. Drop "splash" from the section title and explicitly defer to v1.1.

**Recommended:** Option 2 for v1 (faster to ship). The `wait_for_server` function already prevents blank-page UX. A branded splash is polish, not function.

---

### 🟡 MINOR (polish opportunities)

#### m1. `cargo install create-tauri-app --locked` is unpinned
Recommend `--version "^4"` (4.x scaffolds Tauri 2.x apps; 5.x or future versions could change scaffolding).

#### m2. `X-GNOME-Autostart-enabled=true` is GNOME-specific
Brandon's box is Ubuntu+GNOME so this works. Customers on KDE Plasma or XFCE would need different keys. v1.1 concern.

#### m3. No dev-test command for mode-aware launch
T3.5.2 Step 2 instructs `/usr/local/bin/blackbox-setup --first-run` to test, but the binary isn't there until Track 4 install. Add a dev-test using `cargo tauri dev -- --first-run`.

#### m4. Tauri build estimated time (5-10 min) doesn't account for cold cache
First `cargo build --release` on a fresh `~/.cargo/` registry can take 15-25 min downloading + compiling 200+ transitive crates. Realistic worst case is ~25 min.

#### m5. No mention of code-signing
For Linux .deb/.AppImage v1 this is fine (no signing required). Worth noting as a future-deferral if Brandon ships to mac/windows later.

#### m6. No reproducible-build guidance
For commercial product distribution, locking the Rust toolchain (`rust-toolchain.toml`) and Cargo.lock commit policy matter. v1.1 concern.

#### m7. `installer/dist/blackbox-setup.desktop` and `blackbox-setup-autostart.desktop` differ only in Exec= flag and Categories
Could be DRYed into a single template + sed substitution, but two distinct files is fine for clarity.

---

## Recommended new tasks (NOT yet in plan)

### Task 3.0.1: System prerequisites verification + install (NEW)

**Currently:** Pre-flight steps are buried inside T3.1.1 Step 1. Splitting them into a standalone Task 3.0 makes the verification path more deliberate, and lets a fresh subagent confirm the environment before any other work.

**Files:** None (system-state operations only).

**Steps:**
1. Verify Rust toolchain: `which cargo && cargo --version`. If missing, run `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y` then `source $HOME/.cargo/env`.
2. Install Tauri system deps:
   ```bash
   sudo apt update
   sudo apt install -y libwebkit2gtk-4.1-dev libsoup-3.0-dev build-essential curl wget file \
     libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
   ```
3. Verify all 8 deps are now installed: `for p in libwebkit2gtk-4.1-dev libsoup-3.0-dev build-essential libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev; do dpkg -l "$p" 2>/dev/null | grep -q "^ii" && echo "OK $p" || echo "MISSING $p"; done` — all should print OK.
4. Install pinned `create-tauri-app`: `cargo install create-tauri-app --version "^4" --locked`.

**Verification:** `cargo --version` prints rustc 1.x; all 7 -dev packages report OK; `cargo create-tauri-app --version` prints 4.x.

**Commit:** None (system-state, not file changes).

### Task 3.6.3: End-to-end dev test (NEW)

**Currently:** Plan jumps from T3.5.2 (mode-aware launch logic) to T3.6.1 (build .deb). No interim dev-test step. Without an end-to-end dev-mode smoke test, the implementer doesn't know if the wait-for-server + mode-detection + WebviewWindowBuilder all wire correctly until they've already built the .deb.

**Files:** None (test-only).

**Steps:**
1. From `installer/`, run `cargo tauri dev`. Tauri dev loop opens the window pointing at `http://localhost:9091/onboarding/?mode=manage` (since BlackBox sentinel exists).
2. Verify: window opens, decorations present, wizard renders, click through 1-2 steps to confirm rehydrate works.
3. Close window. Verify the autostart-removal logic does NOT fire (because no `--first-run` flag was passed and we didn't simulate completion).
4. Re-run `cargo tauri dev -- --first-run`. Window opens fullscreen, no decorations, mode=setup. Confirm wait-for-server logging.
5. Document in plan: this dev-test step gives confidence before .deb build.

---

## Plan edits required (recommended order)

If approved, apply these to `docs/plans/2026-05-10-onboarding-flow.md` in this order:

1. **Insert new section "Phase 3.0: System prerequisites" before Phase 3.1** (around line 2899) — covers Task 3.0.1 above.
2. **Edit Phase 3.1 Step 1 (line 2916):** add `libsoup-3.0-dev` to apt list. Pin `cargo install create-tauri-app --version "^4"`.
3. **Edit Phase 3.1 Step 3 (line 2953):** remove the static `"url"` field from the window config (M2). Add `"frontendDist": ""` instead of `"../dist"` (M1).
4. **Add new Step 3.5 to Phase 3.1 (after line 2978):** "Add Cargo dependencies" with the `reqwest` + `dirs` + `serde_json` block (C2).
5. **Edit Phase 3.2 Step 2 (line 3026):** bump `wait_for_server` timeout from 90s to 180s (M4).
6. **Edit Phase 3.3 section title (line 3043):** drop "+ splash" since we're not implementing splash in v1 (M5).
7. **Edit Phase 3.4.1 Step 1 (line 3083) + Phase 3.4.2 Step 1 (line 3113):** change `Exec=/usr/local/bin/blackbox-setup` to `Exec=/usr/bin/blackbox-setup` (M3).
8. **Edit Phase 3.5.2 main.rs example (lines 3214-3225):** replace `WindowBuilder` with `WebviewWindowBuilder` and `WindowUrl` with `WebviewUrl` (C1).
9. **Insert new Task 3.6.3 "End-to-end dev test" between T3.6.1 and T3.6.2** (around line 3271) — covers the new dev-test task above.

Net effect: adds ~80 lines, fixes 3 critical + 5 major issues, makes the plan implementer-ready.

---

## Pre-flight checklist for next session

Before dispatching T3.0.1 (system prep) or T3.1.1 (Cargo skeleton):

- [ ] Read this audit doc end-to-end
- [ ] Read updated Phase 3.0-3.6 of master plan (post-edits)
- [ ] Confirm BlackBox is healthy: `curl -sS http://localhost:9091/health` returns 200
- [ ] Confirm `/onboarding/state` returns `is_complete: true` (or accept setup mode for testing)
- [ ] Decide: `reqwest` (large binary) vs `ureq` (slim binary) for HTTP probes — both work; preference is style
- [ ] Decide: Tauri install path `/usr/bin/` (Tauri default) vs `/usr/local/bin/` (Linux convention) — recommend `/usr/bin/` for v1 simplicity
- [ ] Confirm v1 Linux distro target: Ubuntu 24.04 only, or also Fedora/Arch? Affects Track 4, may affect Track 3 (different webkit-gtk package names per distro)

---

## Out of scope for this audit (Track 4 concerns flagged for later)

- BlackBox install location is currently `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc` (dev path). Customer install must move to `/opt/blackbox/` or similar. Track 4's install.sh handles this.
- systemd unit's `MemoryMax=24G CPUWeight=200 Nice=-5` are dev-machine numbers. Customer hardware sizing TBD.
- `Scripts/install.sh` doesn't exist yet. Track 4 creates it.
- Different desktop environments (KDE/XFCE) have different autostart conventions (m2 above).

These are intentionally out-of-scope for Track 3 audit but recorded for Track 4 audit when that time comes.

---

## Significance

The Phase 2.10 audit pattern proved that 30 minutes of pre-execution diligence saves multiple subagent cycles. Track 3 has more surface area than Phase 2.10 (1-2 weeks vs ~30 minutes of implementation), so the leverage is even higher.

If we apply the recommended edits, the next session opens, reads this audit + the corrected master plan, dispatches T3.0.1 (sysprep) cleanly, and rolls into T3.1.1 with a known-good environment. No "implementer hit a wall on day 1" surprises.
