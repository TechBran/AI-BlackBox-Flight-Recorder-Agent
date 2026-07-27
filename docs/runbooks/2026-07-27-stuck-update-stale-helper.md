# Runbook: in-app update fails at the apt phase (`rc=4`, stale helper)

**Applies to:** any box whose `/usr/local/sbin/blackbox-apt-install` predates
the fix landed 2026-07-27. One command over SSH (or in a terminal on the box)
repairs it immediately and permanently. That command is a **convenience, not a
requirement**: once a box is on the fixed code it self-heals a stale helper on
its own — see [How the fix heals a stale helper](#how-the-fix-heals-a-stale-helper)
for the exact (and narrower-than-it-first-looked) mechanism.

---

## Symptom

The Portal's Updates panel runs, gets through `RESET_HARD`, then dies:

```
--- RESET_HARD ---   git reset --hard 2645c198 OK
--- APT_INSTALL ---  Installing novnc...
x FAILED: apt install novnc failed (rc=4): [blackbox-apt-install] ERROR:
  allowlist file not readable:
  /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Scripts/onboarding/system-packages.txt
```

The path in the error is **not** where the BlackBox is installed. The update
then rolls the code back, so the box stays on the old commit and the next
update attempt fails identically.

## Root cause

`/usr/local/sbin/blackbox-apt-install` is a **root-owned copy** of
`installer/templates/blackbox-apt-install.sh`, installed once by
`Scripts/install.sh`. It used to be installed verbatim, with the install root
hardcoded in the template. Two ways that goes stale:

* the constant changed in the repo (the `5eabbe45` rename moved it from
  `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main` to
  `/home/bbx/Desktop/ai-blackbox-flight-recorder-agent`), or
* the box was simply installed somewhere else in the first place.

`sudo` strips the environment (`env_reset`), so the baked-in constant is what
the helper actually uses. `git reset --hard` refreshes the repo copy of the
template but can never touch a root-owned file under `/usr/local/sbin`.

## The remedy — one command

```bash
sudo bash "$(systemctl show blackbox.service -p WorkingDirectory --value)/Scripts/install.sh"
```

`install.sh` is idempotent. It reinstalls both dispatch helpers with **this
box's real root** substituted in, writes `/etc/blackbox/root` as a durable
pointer, and rewrites the sudoers grants. Runtime ~1-3 minutes.

If you would rather see the path before running anything:

```bash
# Where is this box actually installed?
systemctl show blackbox.service -p WorkingDirectory -p ReadWritePaths

# Then, with that path:
sudo bash /path/from/above/Scripts/install.sh
```

`WorkingDirectory` is the install root. `ReadWritePaths` is a useful
cross-check: the ledger directories listed there all sit under the same root.

### Verify the repair

```bash
# 1. The helper now points at the real root (this must print YOUR install path)
grep 'BLACKBOX_ROOT:-' /usr/local/sbin/blackbox-apt-install

# 2. The pointer file exists and agrees
cat /etc/blackbox/root

# 3. End-to-end: a package that is NOT in the allowlist must exit 5
#    ("package not in allowlist"), NOT 4 ("allowlist file not readable").
sudo /usr/local/sbin/blackbox-apt-install zzz-not-a-real-package; echo "exit=$?"
```

`exit=5` means the helper read its allowlist and correctly refused an
unlisted package — the machine is healthy. `exit=4` means it still cannot
find the allowlist; re-check the path from `systemctl show`.

Then re-run the update from the Portal's Updates panel. Nothing was lost: the
failed update rolled the code back cleanly.

## How the fix heals a stale helper

An earlier draft of this runbook claimed a stuck box **could not be rescued by
shipping an update**. That was too strong. The truth is narrower, and worth
stating precisely.

**The one real ceiling — the runner cannot fix *itself* mid-flight.**
`Orchestrator/routes/update_routes.py` imports `UpdateRunner` at module load and
iterates **one instance** for the whole update. There is no `importlib` reload
and no re-exec, so the already-running Python process executes the **old**
`runner.py` for the entire update, including everything after `git reset --hard`.
A fix inside `runner.py` therefore takes effect on the *next* update, never the
one that delivers it. That is inherent to self-updating software, and it is the
one thing the manual command is guaranteed to beat: the box that predates
`F4` runs the *old* apt logic during the very update that would deliver `F4`, so
that first delivery can still roll back at a stale helper.

**But a box already on the fixed code self-heals a stale helper, two ways:**

1. **On its next update (F4 + F1).** A `SHOULD_HAVE` package that fails at a
   stale helper (exactly the incident — `novnc`) no longer aborts; the apt phase
   warns, skips it, and the update proceeds to its `systemd_regen` phase, which
   re-runs `install.sh`. `install.sh` Step 0b re-templates the root-owned helper
   with this box's real root (`F1`) and rewrites `/etc/blackbox/root`. The helper
   is fixed, in place, with no SSH — for any update whose diff touches the
   helpers / systemd / sudoers buckets. (A stale helper blocking a `MUST_HAVE`
   package still fails that update with an actionable message and rolls back;
   that is the case the manual command exists for.)
2. **On its next service restart (startup hook).**
   `Orchestrator/startup.py::startup_assert_helpers_current()` renders the
   git-tracked `blackbox-apt-install.sh` template with the real root, byte-compares
   it against the deployed copy, and — only if they differ — reinstalls it through
   the already-granted `blackbox-write-systemd apt-install-helper` target. Non-fatal,
   idempotent, mirrors the long-standing `startup_assert_sudoers_current()`.
   **Honest scope:** this only reaches a box that already has the *new*
   `blackbox-write-systemd` (the one carrying the `apt-install-helper`
   `target_kind`). A box on an older `write-systemd` gets `unknown target_kind`
   here — harmless — and is healed by path 1 instead.

So "a new grant / helper can never reach an existing box" is **false** — the
startup hooks port both the sudoers grant *and* the apt helper onto existing
boxes at every start. A narrow *dedicated* grant was still not the answer for a
different reason: a refresh script inside the repo tree is service-writable (the
same unbounded root primitive as the existing `install.sh` grant), and the
root-owned helper it would refresh can only be delivered by `install.sh` in the
first place. Re-templating the *existing* helper through the *existing*
wildcard `blackbox-write-systemd` grant sidesteps both.

The durable fixes:

| Fix | Where | Reaches a box when |
| --- | --- | --- |
| Real root templated into the helper (sed-escaped, `$BLACKBOX_ROOT` charset-validated) | `Scripts/install.sh` Step 0b + top | next `install.sh` run |
| `/etc/blackbox/root` pointer | `Scripts/install.sh` Step 0b | next `install.sh` run |
| Helper install moved ahead of Step 1's apt run | `Scripts/install.sh` Step 0b | next `install.sh` run |
| Restart skipped while an update holds the lock | `Scripts/install.sh` Step 7 | next `install.sh` run (incl. the update that ships it) |
| Root-resolution ladder (env → pointer → templated) | `installer/templates/blackbox-apt-install.sh` | next `install.sh` run |
| Restart-time helper self-heal (re-template via `apt-install-helper` target) | `Orchestrator/startup.py` + `blackbox-write-systemd.sh` | next service restart (needs new `write-systemd`) |
| Preflight with an actionable message | `Orchestrator/update/runner.py` | next update after this one |
| `SHOULD_HAVE` failures warn instead of rolling back | `Orchestrator/update/runner.py` | next update after this one |

Once a box has run the repaired `install.sh` even once, the helper is
self-healing: it reads `/etc/blackbox/root`, which `install.sh` rewrites on
every run, so **moving the repo is repaired by re-running `install.sh`
alone** — nothing under `/usr/local/sbin` ever needs hand-editing again.

## Related behaviour changes (2026-07-27)

* An **optional** (`SHOULD_HAVE`) package that fails to install now logs a
  warning naming the package and the manual `apt-get install` remedy, and the
  update **completes**. Previously one optional package — `novnc` — discarded
  an entire successful code + dependency update. `MUST_HAVE` failures still
  abort and roll back, and a package whose bucket cannot be determined is
  treated as `MUST_HAVE`.
* Phase order is **monotonic**: the single privileged `install.sh` re-run stays
  in the `systemd_regen` phase, **after** apt/pip/mcp. An earlier draft ran it
  *before* the apt phase; that was reverted. It was both dangerous (`install.sh`
  Step 7 `systemctl restart` under the unit's `KillMode=process` would SIGTERM
  the uvicorn process iterating the update) and ineffective (`install.sh` runs
  under `set -e` and would abort at its own apt step before ever refreshing the
  helper). Within `install.sh`, the helper install itself did move to **Step 0b**
  — ahead of that script's own Step 1 apt run — so a `systemd_regen` re-run
  refreshes the helper even if a later install step fails.
* The apt phase preflights the installed helper before installing anything and
  reports the actionable sentence above instead of a bare `rc=4`. It only
  **reports** — it does not re-run `install.sh` mid-phase (that would be the
  reverted early re-run). A stale helper blocking only `SHOULD_HAVE` packages is
  skipped, the update reaches `systemd_regen`, and the helper heals there.
* `install.sh` no longer restarts `blackbox.service` while an update holds
  `Manifest/update.lock` (probed with `flock -n`). The unit sets
  `KillMode=process`, so an unconditional restart from a `systemd_regen` re-run
  would SIGTERM the uvicorn process *iterating the update*. The update pipeline
  fires its own restart when it finishes.

## Known gap — allowlist integrity (follow-up, NOT closed by this fix)

Recorded here because the helper's own header used to overstate the perimeter
(it claimed the allowlist was "customer-non-writable") and a future reader
should not rely on that.

`blackbox-apt-install`'s two checks — the `^[a-z0-9.+-]+$` package-name regex
and allowlist membership — bound **argv injection through the sudo grant** and
accidental misuse. They do **not** bound a service-user RCE:

* the unit's `ReadWritePaths` includes all of `$BLACKBOX_ROOT`, so the service
  user can append `evil-pkg  # MUST_HAVE # x` to
  `Scripts/onboarding/system-packages.txt` and then call the granted helper
  legitimately;
* the same user can edit `Scripts/install.sh`, which has its own bounded
  `NOPASSWD: /usr/bin/bash <root>/Scripts/install.sh` grant.

This is pre-existing and unchanged by the 2026-07-27 work (which adds no new
reachable redirect — `/etc/blackbox` is root-owned and deliberately absent from
every `ReadWritePaths`, pinned by
`scripts/tests/test_install_perimeter_invariants.sh`). Closing it needs
*integrity*, not a stricter argv check. Sketch for whoever picks it up:

* have the helper validate the requested package against a root-owned copy of
  the allowlist (e.g. `/etc/blackbox/system-packages.txt`, refreshed by
  `install.sh`) or against `git show HEAD:Scripts/onboarding/system-packages.txt`
  rather than the working tree;
* re-scope the `bash install.sh` grant, which is today an unbounded root
  primitive for anyone who can write the repo tree.

## Tests that pin this

| Test | Guards |
| --- | --- |
| `scripts/tests/test_install_update_helpers.sh` | install.sh substitutes the real root (sed metacharacters escaped — an `&` in the path used to bake a corrupt one); no `PLACEHOLDER` survives; pointer written; 0755 and the root:root/sudo pin for production destinations |
| `scripts/tests/test_apt_helper_root_resolution.sh` | env → pointer → templated ladder; pointer with spaces/parens honoured; non-absolute and non-BlackBox pointers rejected; regex + allowlist gates still reject |
| `scripts/tests/test_install_perimeter_invariants.sh` | `/etc/blackbox` never in `ReadWritePaths`; the Step 7 restart is update-lock guarded; helpers install before Step 1's apt run; `blackbox-write-systemd` `apt-install-helper` target hardcodes its dest; `startup_assert_helpers_current()` exists |
| `Orchestrator/tests/test_update/test_runner_apt.py` | privileged re-run stays AFTER apt (no early / mid-apt re-run); `SHOULD_HAVE` continues; `MUST_HAVE` rolls back; unknown bucket = `MUST_HAVE`; preflight message; parser matches the helper grep + MUST_HAVE sticky |
| `Orchestrator/tests/test_update/test_install_helpers_shell.py` | runs the two shell suites inside `pytest Orchestrator/tests/` |
