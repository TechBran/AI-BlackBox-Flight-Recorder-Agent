# Remove the `bash install.sh` sudo grant — decompose the update regen

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:executing-plans / subagent-driven-development.

**Goal:** Close the in-service→root privilege escalation by removing the
`NOPASSWD: /usr/bin/bash <root>/Scripts/install.sh` sudoers grant, and replacing the
update pipeline's one use of it (the `systemd_regen` phase) with bounded, hardcoded-
destination helpers. Also close the sibling escalation through the apt allowlist by making
the apt helper trust only a root-owned allowlist copy that no automated step can rewrite.

**Architecture:** The grant's ONLY runtime consumer is `_privileged_regen()` in
`Orchestrator/update/runner.py`. Rewrite it to render each changed git-tracked template and
dispatch the matching `blackbox-write-systemd <target_kind>` (the pattern the startup hooks
already use), instead of re-running the whole script as root. Fresh installs are unaffected —
a human still runs `sudo bash Scripts/install.sh`.

**Tech stack:** bash (helpers, install.sh), Python (runner, startup), pytest + bash tests.

---

## Decisions locked (Brandon, 2026-07-27)

- **Close both doors properly** — remove the `bash install.sh` grant, not just harden the
  allowlist (which alone is theater while the grant stands).
- **New system packages become an operator step.** The apt helper trusts only a root-owned
  allowlist; automated updates install only packages already in it. A commit adding a new
  `MUST_HAVE` apt dependency surfaces an actionable "run `sudo bash install.sh` once" message;
  a new `SHOULD_HAVE` one warns and the update completes. This joins the existing set of
  operator-only operations (Tauri build-deps, the `curl|sh` installers).

## The security invariant this whole change serves

> Every bounded root helper must be safe **by construction**: a **hardcoded destination**
> AND **content the caller cannot control**. A helper that copies an attacker-controllable
> *source* into a trust anchor (a unit body, sudoers, the allowlist) is itself the
> escalation — bounding only the destination is not enough.

**Adversarial finding (2026-07-27, M5 verifier) — the design pivot.** The first cut bounded
destinations but left `blackbox-write-systemd` copying a **caller-supplied source file
verbatim** into root-owned systemd units, with only a syntax-only `visudo -c` on the sudoers
kind. Combined with the standing `systemctl restart` grants, that is a full in-service→root
primitive (`override` drop-in with `User=root` + `ExecStartPre=…` → restart → root), and it
also defeats the M2 allowlist lockdown (write a drop-in granting
`ReadWritePaths=/etc/blackbox`, restart, then poison the allowlist). Removing the
`bash install.sh` grant while this stood would have closed one arbitrary-root door and left a
larger one open.

**Chosen fix (Brandon, 2026-07-27): trusted root-owned template store.** All root-trusted
content — every systemd unit/drop-in, both sudoers files, both `/usr/local/sbin` helpers,
and the apt allowlist — is published as **rendered** files under a root-owned
`/etc/blackbox/templates/` store, populated **only** by the human-run `sudo bash install.sh`.
`blackbox-write-systemd <kind>` takes **NO source argument**; it copies
`/etc/blackbox/templates/<kind>` (root-owned, service-immutable) to the kind's hardcoded
destination. The service user therefore controls neither the destination nor the content of
any privileged write, and the only path that populates the store from the (service-writable)
repo — `install.sh` — is no longer service-runnable once M4 removes its grant. Closure by
construction, not by content validation (a denylist on systemd/sudoers syntax is unwinnable).

**Accepted cost:** auto-port-on-restart of config is gone. Existing boxes adopt the lockdown
with one human `sudo bash install.sh`; thereafter a changed unit/sudoers/helper — like a new
apt package (M2) — is an operator step, not an automatic one. Code and pip still update
automatically.

`scripts/tests/test_install_perimeter_invariants.sh` is the guard: every kind's destination
is hardcoded AND `blackbox-write-systemd` takes no caller source (no `$2`/`$SOURCE_FILE`
into any write).

---

## Context: what a regen actually needs (from the 2026-07-27 audit)

Regen fires when `buckets["systemd"|"sudoers"|"helpers"]` are lit (`changes.py:36-52`).
Its real privileged work reduces to:
1. Re-template the two `/usr/local/sbin` helpers + refresh `/etc/blackbox/root` — already
   done on every service start by `startup_assert_helpers_current()` (`startup.py:493-566`).
2. Rewrite whichever systemd artifacts changed, then `daemon-reload`.
3. Rewrite the sudoers file — `startup_assert_sudoers_current()` already does this on start.
4. Restart affected services — each already individually granted.

Everything else install.sh does is first-install-only (guarded by `if-exists`) or runs as
`sudo -u REAL_USER` (needs no root). The audit's full table lives in the task history.

### Existing coverage (do NOT rebuild)
`blackbox-write-systemd` target_kinds today: `unit`, `override`, `cli-agent-overrides`,
`zellij-web-unit`, `models-unit`, `sudoers-system`, `apt-install-helper`,
`write-systemd-helper`. Plus `blackbox-apt-install`, `blackbox-install-zellij-binary`, and
standalone systemctl/journalctl/tailscale grants.

### The one residual (document, do not solve here)
Enabling a **brand-new** always-on systemd unit introduced by a future update: `write-systemd`
writes units but never runs `systemctl enable`, and `enable <newunit>` is not granted. For
*existing* units this is inert (already enabled), so a regen is unaffected. A future update
that adds a genuinely new always-on unit needs a one-time `sudo bash install.sh` (or a
per-unit enable grant added alongside it). Document in the runbook; do not build speculative
machinery.

---

## Milestones (REVISED for the trusted-template-store design, 2026-07-27)

> The uncommitted M1–M4 work in the tree (new target_kinds, root-owned allowlist, decomposed
> regen) is the foundation. The milestones below rework `write-systemd` to the no-source
> model and add the template store. Everything else in the tree is kept.

### M0 — `write-systemd` takes NO source; renders the root-owned template store
**Files:** `installer/templates/blackbox-write-systemd.sh`, `Scripts/install.sh`,
`installer/templates/sudoers-blackbox-system`.

1. **Publish step in `install.sh` (human, root):** render every git-tracked privileged
   template (units, drop-ins, both sudoers files, both `/usr/local/sbin` helper scripts) with
   the real `$BLACKBOX_ROOT`/`$REAL_USER`, and `install` each into
   `/etc/blackbox/templates/<kind>` (root:root; 0440 for sudoers kinds, 0644 otherwise). This
   is the SOLE writer of the store. `/etc/blackbox` must stay out of the service ReadWritePaths
   (already enforced by the perimeter test). The apt allowlist `/etc/blackbox/system-packages.txt`
   (M2) is part of this store, same rules.
2. **`blackbox-write-systemd <kind>` drops the `SOURCE_FILE` argument entirely.** For a given
   kind it reads `/etc/blackbox/templates/<kind>` and copies it to the kind's hardcoded
   destination, then daemon-reload/nothing per kind. No caller content anywhere. The
   `visudo -c` gate stays as a belt-and-braces check on the (now root-owned) sudoers template.
   If `/etc/blackbox/templates/<kind>` is absent (store not yet published on a fresh box), fail
   closed with a message naming `sudo bash install.sh`.
3. **Sudoers grant:** `blackbox-write-systemd *` still fits (the arg is now just the kind).
   Confirm no grant references a source path.
4. **Bootstrap note:** `install.sh` itself still writes the deployed files directly at first
   install (it is root); the store + `write-systemd` are what the *update/startup* paths use
   thereafter. `install.sh` also (re)installs the new no-source `write-systemd` — an existing
   box adopts the whole model on its first post-update human `install.sh`.

**Tests:** `test_write_systemd_targets.sh` — drive each kind with the store populated in a
sandbox and assert it copies the store file to the (sandbox-rebased) dest; assert passing a
second argument is ignored/rejected; assert a missing store entry fails closed. Extend
`test_install_perimeter_invariants.sh` to assert `write-systemd` contains no `$2`/`$SOURCE_FILE`
read into any write path.

### M1 — New bounded `write-systemd` target_kinds
**File:** `installer/templates/blackbox-write-systemd.sh` (+ its header target list).

Add one `case` arm per kind, each with a HARDCODED destination, mirroring the existing arms:
- `mcp-unit` → `/etc/systemd/system/blackbox-mcp.service` (systemd; daemon-reload after)
- `ydotoold-unit` → `/etc/systemd/system/ydotoold.service` (systemd; daemon-reload)
- `time-sync-dropin` → `/etc/systemd/system/blackbox.service.d/time-sync.conf` (systemd)
- `logrotate` → `/etc/logrotate.d/blackbox` (plain 0644, **no** daemon-reload)
- (Asterisk boxes only) `asterisk-dropin` → `/etc/systemd/system/blackbox.service.d/asterisk.conf`;
  `asterisk-sudoers` → `/etc/sudoers.d/blackbox-asterisk` (0440, **visudo -c** gated like
  `sudoers-system`)

**Explicitly NOT added:** any target that takes a caller-supplied destination; any
`system-packages`/allowlist-copy target (see M2 — that would be the laundering hole); any
`root-pointer` target that copies a caller source.

**Tests:** extend `scripts/tests/test_install_perimeter_invariants.sh` — assert each new kind
appears with a hardcoded `/etc/...` destination and that NO kind reads its destination from
`$2`/an argument. Add a `blackbox-write-systemd` behaviour test (sandbox DEST override) for
one systemd kind and the visudo-gated `asterisk-sudoers` kind.

### M2 — Root-owned allowlist; apt helper trusts only `/etc`
**Files:** `installer/templates/blackbox-apt-install.sh`, `Scripts/install.sh`.

1. `install.sh` (human-run, root) writes `/etc/blackbox/system-packages.txt` (root:root
   0644) as a copy of the repo allowlist, at Step 0b/Step 1 time — BEFORE Step 1's apt so a
   fresh box's first install still resolves. This is the ONLY writer of the /etc allowlist.
2. `blackbox-apt-install` reads its allowlist **only** from `/etc/blackbox/system-packages.txt`.
   Remove the repo-path read. If the /etc copy is absent (should only happen pre-Step-1 on a
   fresh box), fail closed (exit 4) with a message naming the remedy.
3. The `/etc/blackbox/root` pointer's role shrinks: the helper no longer needs it to locate a
   repo allowlist. Keep the pointer only if `blackbox-write-systemd`/other helpers still need
   the root; if nothing reads it after M2, **remove it** (and its writer + tests) rather than
   leave a root-owned redirect with no consumer — a redirect nobody reads is attack surface,
   not defence. Decide from the code, document the decision.

**Security note for the implementer:** do NOT add any way for the service user to refresh the
/etc allowlist. That is the whole point — it is human-refreshed only. The perimeter invariant
test already forbids the service getting ReadWritePaths on `/etc/blackbox`
(`test_install_perimeter_invariants.sh:17-29`) — keep it.

**Tests:** update `test_apt_helper_root_resolution.sh` for the /etc-only source; add a case
proving a repo-only allowlist entry (not in /etc) is REJECTED (this is the closed door); add
a case proving `/etc` absence fails closed.

### M3 — Runner: decompose `_privileged_regen` (store-aware)
**File:** `Orchestrator/update/runner.py`.

1. Rewrite `_privileged_regen()` to STOP running `sudo -n bash install.sh`. For each changed
   privileged template, dispatch the matching `blackbox-write-systemd <kind>` (no source arg),
   which re-asserts the ROOT-OWNED store entry, then daemon-reload / restart via existing
   grants.
2. **Store-staleness detection (the key new behaviour).** Because the store is human-refreshed
   only, an automated update that CHANGES a privileged template cannot apply it — the store is
   still the last human-published version. Detect this: for each changed template, compare the
   repo-rendered content against `/etc/blackbox/templates/<kind>`; if they differ, the regen
   must NOT silently apply stale config and must NOT claim success — surface an actionable
   "a privileged config change needs `sudo bash install.sh` once" message (same shape as the
   new-package case), and let the rest of the update complete. Re-asserting an UNCHANGED store
   entry (drift-heal) is fine and silent.
3. **apt phase (new-package policy):** unchanged from the current tree — new `MUST_HAVE` not in
   the /etc allowlist → actionable remedy + rollback; new `SHOULD_HAVE` → warn + continue.
4. Keep the sudo-probe graceful-skip; fallback message points at the bounded helper, not a
   `bash install.sh` grant (which no longer exists).

**Tests:** rewrite the phase-ordering tests that assumed a `bash install.sh` re-run to assert
the store-dispatch. Add: a changed-but-unpublished template surfaces the operator remedy and
does NOT apply stale content; an unchanged template re-asserts silently; a changed+published
template dispatches exactly the matching `write-systemd <kind>` and nothing else.

### M3b — Startup hooks become drift-heal against the store
**File:** `Orchestrator/startup.py`.

`startup_assert_sudoers_current` / `startup_assert_helpers_current` currently render the REPO
template and dispatch with a repo source. Rework them to: compare the deployed file against
the ROOT-OWNED `/etc/blackbox/templates/<kind>` and dispatch `write-systemd <kind>` (no source)
only on drift. They now heal deployed-vs-store drift, NOT repo→deployed — the repo→store bridge
is exclusively the human `install.sh`. Same non-fatal, idempotent shape. Document that the
auto-port-of-new-repo-templates capability is deliberately removed (it was the laundering
vector). Add a startup hook to also re-assert units from the store if that is the drift-heal
policy chosen (decide with M3, do not duplicate).

### M4 — Remove the grant + update startup coverage + docs
**Files:** `installer/templates/sudoers-blackbox-system`, `Orchestrator/startup.py`,
`docs/runbooks/`.

1. Delete the `NOPASSWD: /usr/bin/bash BLACKBOX_ROOT_PLACEHOLDER/Scripts/install.sh` line.
2. Because the sudoers template is ported to existing boxes by
   `startup_assert_sudoers_current()` on the next restart, existing boxes lose the grant
   automatically after they take this update + restart — no manual step. Confirm the startup
   hook still functions with the line gone.
3. Consider a `startup_assert_units_current()` hook (same shape as the sudoers/helpers hooks)
   so unit-template drift self-heals on restart too — OR keep unit regen solely in the runner.
   Decide and justify in the plan-execution notes; do not build both.
4. Runbook `docs/runbooks/2026-07-27-remove-install-sh-grant.md`: the new update model
   (automated code/pip/units/sudoers/helpers; operator-run `sudo bash install.sh` for new
   system packages and new always-on units), and the residual new-unit-enable note.

**Tests:** add an assertion to `test_install_perimeter_invariants.sh` that the sudoers
template contains NO `bash .*install.sh` grant (the regression guard for the whole change).

### M5 — Full verification
- `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -q` green (baseline 4146; one
  known tool-selection flake that passes in isolation).
- All bash suites under `scripts/tests/` green.
- Adversarial security review: prove that with the grant gone and the /etc allowlist in place,
  no sequence of the REMAINING bounded grants escalates to arbitrary root. Specifically walk
  the apt-install + every write-systemd target_kind and show none takes an
  attacker-controllable source into a trust anchor.
- Confirm `sudoers-blackbox-system` diff vs HEAD contains ONLY the removed grant line plus the
  handful of new bounded systemctl grants (if any) — nothing widened.

## Non-goals
- No signing / root-owned-updater (that was the rejected Option B). New system packages are an
  operator step; that is the accepted trade.
- No speculative new-unit-enable machinery — documented as an operator step.
- Fresh-install `install.sh` behaviour is unchanged; do not refactor it beyond the /etc
  allowlist write and removing the now-unused pointer if M2 finds it dead.
- Web Portal / Android untouched.

## Rollout note
Existing boxes get this via `git pull` + restart: the startup hooks port the new sudoers
(grant removed) and helpers, and the next update uses the decomposed regen. MS02 and prod
self-heal on restart — see [[ms02_deploy_mechanism]].
