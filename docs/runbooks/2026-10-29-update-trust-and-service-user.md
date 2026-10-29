# Runbook: pinned update origin, signed releases, and the BlackBox service account

**Applies to:** every box, from the update that lands the 2026-10 audit rank 79
change (X04-security-clients-infra-11/-12/-13) onward. Nothing here changes a box
until a human runs `sudo bash Scripts/install.sh`, and the service-account move
is opt-in on top of that.

---

## 1. Updates come only from the pinned origin, and only signed once a key is pinned

Before, `Scripts/update.sh` fetched from whatever `origin` `.git/config` named,
reset to `origin/main` and ran `sudo install.sh` from the result. `.git/config`
and the whole checkout are writable by the running service, so a change to the
origin (or a hijacked or renamed repository) delivered code that the operator
then ran as root. The in-app updater fetched the same way.

Now both updaters (`Scripts/update.sh`, and `Orchestrator/update/verify.py` for
the Portal's **Install update**) apply two checks before they change anything:

| Check | Trust anchor (root-owned, written only by `sudo bash install.sh`) | Rule |
| --- | --- | --- |
| Origin | `/etc/blackbox/update-origin`, copied from the tracked `installer/update-origin` | `git remote get-url origin` (insteadOf applied) must be one of its lines, ignoring case, one trailing `/` and one trailing `.git`. Checked **before** the fetch. |
| Signature | `/etc/blackbox/update-allowed-signers`, copied from the tracked `installer/allowed_signers` | Once it holds a key, the commit being installed must carry an SSH signature that `git verify-commit` accepts against that file alone. |

* The pins are read only from those fixed paths. A pin that is a symlink, not
  owned by root, or writable by group or others is **not** a pin: the update is
  refused. `/etc/blackbox` stays outside the service's `ReadWritePaths`.
* `git verify-commit` runs with `-c gpg.ssh.allowedSignersFile=<pin>`,
  `-c gpg.ssh.program=/usr/bin/ssh-keygen` and the PGP / X.509 programs set to
  `false`. Command-line `-c` beats `.git/config`, so a planted verifier or
  signers file cannot pass a commit, and only SSH signatures count.
* The reset installs the exact SHA that was checked, never whatever
  `origin/main` names by the time of the reset.
* **A missing pin only warns** ("no update origin is pinned" / "no release
  signing key is pinned"): a box that has not run the new `install.sh` yet, or
  a repository that ships no key yet, keeps updating as before, and its update
  log says that it was not checked. `installer/allowed_signers` ships with **no
  key** today, so signatures are not enforced anywhere until the owner adds one
  (section 3).

What the gate does not cover: code already on disk. The in-app runner is itself
service code, and `install.sh` runs as root from a checkout the service can
write. The gate protects the box from what arrives over the network. Section
5 (the dedicated service account) is what stops a compromised service from
reaching the operator's own account.

### Refused updates

`Scripts/update.sh` exits **4** and the Portal / Android update log shows
`FAILED: …` with the reason. Nothing was fetched (origin refusal), tagged,
stashed or reset. Typical causes and fixes:

| Message | Fix |
| --- | --- |
| `fetches updates from X, which is not a pinned update origin` | Put the origin back: `git remote set-url origin https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git` (as the checkout's owner). A deliberate mirror is added to `/etc/blackbox/update-origin` by root (and again after each `install.sh`, which rewrites it). |
| `… is not a trusted pin` | `sudo bash Scripts/install.sh` rewrites the pins root-owned 0644 and the directory without group/other write. |
| `not signed with a release key` / `not from a pinned release key` | The commit at the tip of `main` is unsigned, or signed by a key this box does not pin. Do not work around it on the box: fix the release (section 3). |

### A failed `install.sh` puts more back

When `install.sh` fails during `Scripts/update.sh`, the rollback now also
* puts the local edits stashed for the update back in the working tree (and
  drops that stash entry once they applied), and
* republishes the root-owned template store from the old code, with
  `sudo bash Scripts/install.sh --publish-store-only` (the failed run had
  already published the new templates: Steps 0b/0c run first, and the startup
  hooks re-assert the store onto the deployed sudoers and helpers). An old
  `install.sh` that predates the option is not run; the log names the command
  instead.

The in-app updater's rollback after a failed phase also puts the stashed local
changes back. It never publishes the store, so there is nothing to restore there.

## 2. Adopting the pins on an existing box

One human run, as for the template store:

```bash
sudo bash Scripts/install.sh
ls -l /etc/blackbox/update-origin /etc/blackbox/update-allowed-signers   # root root, -rw-r--r--
./Scripts/update.sh --status        # exit 0, or 4 with the reason if the origin is not the pinned one
```

`install.sh --publish-store-only` publishes only the trust anchors (dispatch
helpers, apt allowlist, template store, update pins) and stops, when the rest of
the installer should not run.

## 3. Signing releases (owner)

Enforcement starts on a box once `installer/allowed_signers` holds a key **and**
that box has run `sudo bash Scripts/install.sh` from a commit that has it.
`Scripts/update.sh` runs `install.sh` on every update; the in-app updater never
does (its log says so when an update changes the pin sources).

1. Once, on the machine that holds the key:

   ```bash
   ssh-keygen -t ed25519 -C blackbox-releases -f ~/.ssh/blackbox_release
   git config gpg.format ssh
   git config user.signingkey ~/.ssh/blackbox_release
   git config commit.gpgsign true
   ```

2. Add the public key to `installer/allowed_signers`:

   ```
   releases@<your domain> namespaces="git" ssh-ed25519 AAAA…   # the .pub file's first two fields
   ```

   Commit it (signed) and push. `scripts/tests/test_publish_update_pins.sh`
   checks the file's format.

3. From then on **every commit that reaches `main` must be signed**: the
   updaters check the tip they install. Merge and rebase locally and push; a
   merge made on GitHub's website carries GitHub's signature, which is not in the
   file, and every enforcing box would refuse it. Check before pushing:

   ```bash
   git -c gpg.ssh.allowedSignersFile=installer/allowed_signers verify-commit HEAD
   ```

4. Each box enforces from its next `sudo bash Scripts/install.sh`
   (`Scripts/update.sh` does it). Boxes updated only from the Portal need that
   one run.

**Rotating a key:** add the new key in a commit signed by the old one, wait
until every box has run `install.sh` from it, then sign with the new key, and
only later remove the old line (in a commit signed by the new key).

**Lost key:** boxes that enforce refuse everything signed otherwise. Recovery is
an operator step per box: check out the new release by hand (verify it some
other way), then `sudo bash Scripts/install.sh` to pin the new key.

## 4. Nobody else writes into the checkout

The service starts uvicorn with the checkout as its working directory, so
Python imports from it first, and the Portal, the MCP server and the CLI agents'
slash commands come from it too. The dev box had the root, `Orchestrator/`,
`Portal/`, `Manifest/` and `.claude/commands/` at 0777 and nothing normalized
them (X04-security-clients-infra-13).

* `install.sh` Step 0e (`harden_tree_permissions`) runs **as the checkout's
  owner** on every install: directories lose group and other write, files lose
  other write, and `Manifest/` and `credentials/` become 0700 (created if
  missing). It never adds a permission and never runs `chmod` as root.
* At every start the service logs `[PERMS]` warnings for what is still wrong:
  a directory other accounts can write (group write counts only for a group
  someone else can hold, not the owner's private group), a world-writable
  parent directory, or `Manifest/` / `credentials/` not 0700. Warn only.

```bash
journalctl -u blackbox.service -b | grep '\[PERMS\]'    # nothing = clean
```

## 5. Optional: run the BlackBox as a dedicated service account

By default the service runs as the desktop/admin account that ran `install.sh`:
a member of `sudo`, whose whole `$HOME` the service can write (the CLI-agent
drop-in sets `ProtectHome=no`). A compromised service can then rewrite that
account's shell rc files or autostart entries and wait for its next
interactive `sudo` (X04-security-clients-infra-12). The service logs a
`[PERMS] This service runs as …, a member of sudo` warning at every start while
that is the case.

`install.sh --service-user NAME` moves the BlackBox to a dedicated, non-login
system account outside the sudo group. It is **opt-in, off by default, and
was not run on the dev box**: the code path is proven in a sandbox
(`scripts/tests/test_install_service_user.sh`), not yet on hardware. Do it on a
test box first.

### Before you start

* End the CLI-agent terminal sessions: they belong to the old account and do
  not carry over.
* If the opt-in vLLM reranker (`vllm-reranker.service`) is enabled, the move
  refuses: `sudo systemctl disable --now vllm-reranker.service` first (re-provision
  it afterwards with `RERANK_PROVIDER=vllm` if you still want it).
* Check `.env` and `config.ini` for paths into the admin's home (for example a
  `GOOGLE_APPLICATION_CREDENTIALS` file): the service account cannot read
  them. Move those files into the checkout's `credentials/` first.

### Preview, then move

```bash
sudo bash Scripts/install.sh --service-user blackbox --dry-run   # prints every change, makes none
sudo bash Scripts/install.sh --service-user blackbox             # the move, then the full install
```

The dry run lists every privileged step: the `useradd`, each traverse ACL,
each unit it stops, one `chown -hR` per top-level entry of the checkout (with
the number of entries each changes), each move of per-user state, the display
access entry and the recorded choice. The move then does, in order:

| Step | What |
| --- | --- |
| Account | `useradd --system --user-group --create-home --home-dir /var/lib/blackbox --shell /usr/sbin/nologin` (an existing account must not be root, must not be in sudo/admin/wheel, and must have its own primary group). Home 0700. |
| Reach the checkout | Traverse-only ACLs (`setfacl -m u:<uid>:x`) on the directories above the checkout the account could not pass (the checkout usually sits in the admin's 0750 home). No read and no write anywhere in that home. |
| Stop | The BlackBox units that are running (they start again at the end). |
| Checkout | `chown -hR blackbox:blackbox` of every top-level entry: the runtime data (`Volumes/`, `Manifest/`, `data/`, `credentials/`, `Portal/uploads/`), the venvs (`Orchestrator/venv`, `MCP/venv`), the code and `.git`. The whole checkout, because the in-app updater resets it, the wizard writes `.env` and `config.ini`, and the CLI agents edit it. |
| Per-user state | Moved from the admin's home into `/var/lib/blackbox`: `.blackbox` (the local model stack; left as a root-owned symlink at its old path, because its venv scripts carry absolute shebangs), `.local/share/zellij`, `.local/share/blackbox`, `.claude-bbox`, `agent-workspaces`. The admin's own `.claude`, `.gemini`, `.codex`, `.nvm` are **not** touched. |
| Display | `~/.config/autostart/blackbox-service-display.desktop` in the desktop session runs `xhost +SI:localuser:blackbox` at login (Computer Use on the real display). |
| Record | `/etc/blackbox/service-user` (root-owned): `SERVICE_USER=blackbox`, `PREVIOUS_USER=<admin>`. |

The rest of that `install.sh` run then renders every unit with
`User=blackbox` (blackbox, blackbox-mcp, zellij-web, blackbox-models; ydotoold's
socket for its uid), the sudoers grants (`/etc/sudoers.d/blackbox-system`, and
`blackbox-asterisk` when Asterisk is installed) and the template store for
`blackbox` only, makes the account's whole home writable in the CLI-agent
drop-in, adds it to `systemd-journal` and `input`, and installs nvm, the CLI
agents and the MCP registration into its home. Every later `install.sh` run
(including the one `Scripts/update.sh` makes) reads the recorded choice and
keeps it; a half-finished move finishes there.

### After the move

```bash
systemctl show -p User blackbox.service blackbox-mcp.service zellij-web.service   # User=blackbox
sudo -l -U blackbox | grep NOPASSWD | head -3                                      # the bounded grants, for blackbox
sudo -l -U <admin> | grep -c blackbox-write-systemd                                # 0: the admin's grants are gone
journalctl -u blackbox.service -b | grep '\[PERMS\]'                                # no "member of sudo" line
```

* **Sign the CLI agents in again**, as the service account, from a CLI-agent
  terminal in the Portal (`claude`, `gemini`, `codex` each ask on first use).
* **Log out and back in** on the desktop (or run
  `xhost +SI:localuser:blackbox` in the desktop session) so Computer Use can
  reach the display.
* **Working in the checkout as the admin** now goes through the owner:
  `sudo -u blackbox git -C <checkout> status`. `Scripts/update.sh` does this by
  itself (it runs its git and pip steps as the checkout's owner).
* Known gaps of a separate account, by design of the desktop session: the
  service cannot use the admin's session D-Bus (GNOME settings, `xdg-open`
  into the admin's browser, so a CLI agent's browser sign-in shows a URL to
  open by hand) or the admin's audio session.

### Rolling back

```bash
sudo bash Scripts/install.sh --revert-service-user --dry-run
sudo bash Scripts/install.sh --revert-service-user
```

Run it as the account the checkout came from (`PREVIOUS_USER`). It stops the
units, gives the checkout back, moves the per-user state back (removing the
`.blackbox` symlink), removes the traverse ACLs, the display entry and
`/etc/blackbox/service-user`, and the rest of the run renders everything for
the admin again. The account and its home are kept (they hold its CLI-agent
sign-ins); remove them with `sudo userdel blackbox && sudo rm -rf /var/lib/blackbox`
once nothing in them is needed.

### What still holds after the move

`sudo bash Scripts/install.sh` runs as root from a checkout the service can
write. With the move, a compromised service can no longer reach the admin's
account, but it can still change the code that a later `install.sh` runs as
root. Run `install.sh` from a checkout you trust. `Scripts/update.sh` checks the
commit it installs (section 1) and runs `install.sh` right after the reset,
which narrows that window but does not close it; closing it needs the
installer to run from root-owned code, a later change.
