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
