"""A real git box for the updater tests: a bare origin (GitHub) and a clone
(the box) one commit behind it, all under a test's tmp_path. Shared by
test_updater_safety.py (the in-app runner and routes) and test_update_sh.py
(the Scripts/update.sh twin)."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def _identity(repo):
    git(repo, "config", "user.email", "updater-test@example.invalid")
    git(repo, "config", "user.name", "updater test")
    git(repo, "config", "commit.gpgsign", "false")


def make_box(tmp_path, extra_files=None):
    """origin.git (GitHub) and blackbox/ (the box) at commit v1.

    extra_files ({relpath: body}) are committed in v1; *.sh files are made
    executable first, so git records them 100755.
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", str(seed))
    git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    _identity(seed)
    (seed / "app.py").write_text("VERSION = 1\n")
    (seed / "notes.txt").write_text("operator notes\n")
    for rel, body in (extra_files or {}).items():
        p = seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "v1")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")

    root = tmp_path / "blackbox"
    git(tmp_path, "clone", "-q", str(origin), str(root))
    _identity(root)
    (root / "Manifest").mkdir()
    # Runtime files a real box always has. Untracked: they must never block.
    (root / "Volumes").mkdir()
    (root / "Volumes" / "SNAPSHOT_VOLUME.txt").write_text("ledger\n")
    return SimpleNamespace(origin=origin, seed=seed, root=root,
                           v1=git(root, "rev-parse", "HEAD"))


def push_upstream(box, *, requirements=False):
    """Publish v2 on origin: app.py changes and, when asked, requirements.txt.
    Returns v2's SHA."""
    (box.seed / "app.py").write_text("VERSION = 2\n")
    if requirements:
        (box.seed / "requirements.txt").write_text("aiohttp==3.14.5\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v2")
    git(box.seed, "push", "-q", "origin", "main")
    return git(box.seed, "rev-parse", "HEAD")


def head(root) -> str:
    return git(root, "rev-parse", "HEAD")


def stash_entries(root) -> list[str]:
    out = git(root, "stash", "list", "--format=%gs")
    return [line for line in out.splitlines() if line.strip()]


def tags(root) -> list[str]:
    return [t for t in git(root, "tag", "--list").splitlines() if t.strip()]


def make_zip_box(box, at):
    """A ZIP-installed box after install.sh Step 0a / git_ops.lazy_init: .git
    points HEAD at origin/main, but the index was never filled and the files
    on disk came from the ZIP (here: commit `at` of box.seed, unpacked).
    Returns its root, next to box.root."""
    from Orchestrator.update import git_ops
    root = box.root.parent / "zipbox"
    root.mkdir()
    archive = subprocess.run(["git", "archive", at], cwd=str(box.seed), check=True,
                             capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(root)], input=archive, check=True)
    git_ops.lazy_init(root, str(box.origin))
    _identity(root)
    (root / "Manifest").mkdir(exist_ok=True)
    return root


# ── release signing (audit X04-security-clients-infra-11) ───────────────

def make_signing_key(directory, name="release"):
    """A fresh ed25519 SSH signing key. `.line` is its ALLOWED SIGNERS entry,
    in the format installer/allowed_signers documents."""
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / f"{name}_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name,
                    "-f", str(key)], check=True, capture_output=True)
    kind, blob = (directory / f"{name}_ed25519.pub").read_text().split()[:2]
    return SimpleNamespace(key=key, line=f'{name}@example.invalid namespaces="git" {kind} {blob}')


def push_signed(box, key, *, version=3):
    """Publish a commit on origin signed with `key` (make_signing_key).
    Returns its SHA."""
    (box.seed / "app.py").write_text(f"VERSION = {version}\n")
    git(box.seed, "add", "-A")
    git(box.seed, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key.key}",
        "commit", "-q", "-S", "-m", f"v{version} (signed)")
    git(box.seed, "push", "-q", "origin", "main")
    return git(box.seed, "rev-parse", "HEAD")


def write_pins(trust_dir, *, origins=None, signers=None):
    """Write the update trust pins install.sh publishes (None = leave absent)."""
    if origins is not None:
        p = trust_dir / "update-origin"
        p.write_text("# pinned by the test\n" + "".join(f"{o}\n" for o in origins))
        p.chmod(0o644)
    if signers is not None:
        p = trust_dir / "update-allowed-signers"
        p.write_text("# release keys\n" + "".join(f"{s}\n" for s in signers))
        p.chmod(0o644)
