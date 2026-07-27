# Runbook: the new update model after removing the `bash install.sh` sudo grant

**Applies to:** every box, from the update that lands the "install.sh-grant
removal, 2026-07-27" change onward. This documents what the in-app / Portal
update now does on its own, and the two narrow operations that became a one-time
operator step. Nothing here is a routine chore — a normal update needs **zero**
operator action.

---

## What changed and why

The service user held a sudoers grant:

```
REAL_USER ALL=(root) NOPASSWD: /usr/bin/bash <root>/Scripts/install.sh
```

That single line was the one **in-service → arbitrary-root** escalation in the
whole perimeter: any prompt-injection RCE reaching the service could run
`sudo -n bash install.sh` and execute the entire installer as root. Hardening
the apt allowlist while that grant stood was theatre — so the grant is **deleted**
from `installer/templates/sudoers-blackbox-system`.

The update pipeline used the grant in exactly one place: the `systemd_regen`
phase re-ran `install.sh` to re-template system files. That phase
(`Orchestrator/update/runner.py::_privileged_regen`) now **dispatches the
individual bounded `blackbox-write-systemd <target_kind>` helpers** for each
CHANGED git-tracked template instead — the same render → byte-compare →
dispatch-on-diff shape the startup hooks already use. Each helper writes a
**hardcoded** destination and validates before writing (sudoers via `visudo -c`,
apt packages against a root-owned allowlist). There is no longer any command the
service can invoke that produces arbitrary root.

## The new update model — what is automated

A normal in-app / Portal update applies all of this with **no operator action**:

| Change in the pushed commit        | How it applies                                             |
| ---------------------------------- | ---------------------------------------------------------- |
| Application code (Python, Portal)  | `git reset --hard` to the target commit                    |
| Orchestrator / MCP pip deps        | `pip install -r requirements.txt` in each venv             |
| systemd **units already present**  | `blackbox-write-systemd <unit>` per changed template + `daemon-reload` |
| sudoers (`blackbox-system`)        | `blackbox-write-systemd sudoers-system` (visudo-gated)     |
| dispatch helpers (apt / write)     | `blackbox-write-systemd apt-install-helper` / `write-systemd-helper` |
| **already-allowlisted** apt pkgs   | `blackbox-apt-install <pkg>` (root-owned `/etc` allowlist) |

Existing boxes drop the removed grant automatically: `startup_assert_sudoers_current()`
renders the now-grant-less template on the next service restart, byte-compares it
against the deployed `/etc/sudoers.d/blackbox-system`, and re-installs via the
already-granted `blackbox-write-systemd sudoers-system` helper. No manual step.

## The two operations that are now a one-time operator step

Both are deliberate trade-offs for closing the escalation. Both surface an
actionable message in the update log; neither happens silently.

### 1. A brand-new system package (apt dependency)

The apt helper trusts **only** the root-owned `/etc/blackbox/system-packages.txt`
allowlist, which only a human `sudo bash install.sh` writes. So a commit that adds
a **new** package to the repo allowlist is not installable by an automated update
until an operator refreshes the `/etc` copy.

- New **MUST_HAVE** package → the update **fails the apt phase and rolls code
  back**, with:

  > Required package `<pkg>` is not in the root-owned allowlist
  > `/etc/blackbox/system-packages.txt` yet. … Run
  > `sudo bash <root>/Scripts/install.sh` once (it rewrites the /etc allowlist
  > from this commit), then retry the update.

- New **SHOULD_HAVE** package → the update **warns and completes**; the feature
  that needs the package stays unavailable until an operator installs it.

**Fix:** on the box (SSH or a terminal), run once:

```bash
sudo bash <root>/Scripts/install.sh
```

`<root>` is the BlackBox install directory (the checkout the service runs from).
This rewrites `/etc/blackbox/system-packages.txt` from the current commit and
installs everything. Then retry the update from the Portal.

### 2. A genuinely new always-on systemd unit

`blackbox-write-systemd` **writes** unit files but never runs `systemctl enable`,
and `enable <newunit>` is not granted. For **existing** units this is inert (they
are already enabled), so ordinary regens are unaffected. But an update that
introduces a **brand-new** always-on unit needs it enabled once:

```bash
sudo bash <root>/Scripts/install.sh   # installs + enables the new unit
```

(Equivalently, the unit can be enabled by hand: `sudo systemctl enable --now
<newunit>`.) This is the documented residual — no speculative auto-enable
machinery was built for it.

## Where this lives in the code

- **Perimeter:** `installer/templates/sudoers-blackbox-system` — the grant is
  gone; a comment block marks where it was and why.
- **Regen:** `Orchestrator/update/runner.py::_privileged_regen` +
  `_REGEN_TARGETS` — per-template bounded dispatch, never `install.sh`.
- **Restart-time self-heal:** `Orchestrator/startup.py` —
  `startup_assert_sudoers_current()` (ports the grant-less sudoers) and
  `startup_assert_helpers_current()` (ports the apt helper). A comment there
  records the decision NOT to add a `startup_assert_units_current()` hook — unit
  drift is healed solely by the runner (see the decision note in that file).
- **Allowlist:** `blackbox-apt-install` reads only
  `/etc/blackbox/system-packages.txt`; `Scripts/install.sh` is its sole writer.
- **Regression guard:** `scripts/tests/test_install_perimeter_invariants.sh`
  fails if the sudoers template ever reintroduces a `bash … install.sh` grant.
