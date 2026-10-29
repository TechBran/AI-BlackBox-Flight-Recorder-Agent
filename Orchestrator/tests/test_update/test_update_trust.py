"""The in-app updater installs only from the pinned origin, and only signed
commits once a release key is pinned (audit 2026-10-09 rank 79,
X04-security-clients-infra-11).

Real git and real SSH signatures (ssh-keygen) against a bare fake remote: the
box's origin is a local path, the pins live in the per-test trust directory
(conftest.py), and the hostile cases plant the config an attacker who can write
.git/config would plant. A refused update changes nothing: no fetch, no tag, no
stash, no reset. Also here: a failed update that was rolled back puts the local
changes it stashed back (the other half of -11).
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from Orchestrator.update import runner as runner_mod
from Orchestrator.update import verify
from Orchestrator.update.manager import UpdateManager
from Orchestrator.update.runner import UpdateRunner

from ._gitbox import (git, head, make_box, make_signing_key, overwrite_loose_object,
                      plant_commit, push_signed, push_upstream, smudge_filter, stash_entries,
                      tags, tree_with, write_pins)


@pytest.fixture(autouse=True)
def restarts(monkeypatch):
    scheduled = []
    monkeypatch.setattr(runner_mod, "_fire_detached_restart", lambda: None)
    monkeypatch.setattr(runner_mod, "_schedule_restart",
                        lambda delay_s=2.0: scheduled.append(delay_s))
    return scheduled


class FakeRunner(UpdateRunner):
    """Real git, fake pip/apt/sudo."""

    def __init__(self, root, responder=None, **kw):
        super().__init__(root, UpdateManager(root), **kw)
        self.calls: list[list[str]] = []
        self._responder = responder or (lambda runner, cmd: (0, ""))

    async def _run(self, cmd, timeout=120.0):
        self.calls.append(list(cmd))
        return self._responder(self, list(cmd))


def collect(agen):
    async def _go():
        return [ev async for ev in agen]
    return asyncio.run(_go())


@pytest.fixture
def box(tmp_path):
    return make_box(tmp_path)


@pytest.fixture
def key(tmp_path):
    return make_signing_key(tmp_path / "keys")


def logs(events):
    return [e["text"] for e in events if e["type"] == "log"]


def nothing_changed(box, events):
    done = events[-1]
    assert done["type"] == "complete" and done["succeeded"] is False
    assert done["refused"] == "unverified_update"
    assert done["restart_scheduled"] is False
    assert head(box.root) == box.v1
    assert tags(box.root) == [] and stash_entries(box.root) == []
    return done


# ── the pinned origin ───────────────────────────────────────────────────

def test_an_origin_the_pin_does_not_list_is_refused_before_the_fetch(box, trust):
    push_upstream(box)
    write_pins(trust, origins=["https://github.com/example/the-real-repo.git"])
    events = collect(FakeRunner(box.root).run())

    done = nothing_changed(box, events)
    assert str(box.origin) in done["error"] and "the-real-repo" in done["error"]
    # Never fetched from the unpinned origin: origin/main still names v1.
    assert git(box.root, "rev-parse", "origin/main") == box.v1


def test_the_pinned_origin_matches_ignoring_case_a_trailing_slash_and_dot_git(box, trust):
    v2 = push_upstream(box)
    pin = str(box.origin)[: -len(".git")].upper() + "/"
    write_pins(trust, origins=["https://github.com/example/other.git", pin])
    done = collect(FakeRunner(box.root).run())[-1]
    assert done["succeeded"] is True, done
    assert head(box.root) == v2


@pytest.mark.parametrize("url, pinned", [
    ("https://github.com/TechBran/AI-BlackBox-Flight-Recorder-Agent.git",
     "https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git"),
    ("https://github.com/TechBran/ai-blackbox-flight-recorder-agent",
     "https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git"),
    ("https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git/",
     "https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git"),
])
def test_normalize_origin_equates_github_spellings(url, pinned):
    assert verify.normalize_origin(url) == verify.normalize_origin(pinned)


@pytest.mark.parametrize("url", [
    "https://github.com/TechBran/blackbox-poc.git",            # the retired slug
    "https://github.com/TechBran/ai-blackbox-flight-recorder-agent-fork.git",
    "https://github.example.com/TechBran/ai-blackbox-flight-recorder-agent.git",
    "http://github.com/TechBran/ai-blackbox-flight-recorder-agent.git",
])
def test_normalize_origin_keeps_other_repositories_apart(url):
    assert verify.normalize_origin(url) != verify.normalize_origin(
        "https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git")


def test_every_origin_the_code_sets_is_a_pinned_one():
    """A fresh box clones (install.sh Step 0a, git_ops.lazy_init) or re-points
    (update.sh's rename self-heal) its origin; each URL must be one the shipped
    pin accepts, or the box would refuse its own updates."""
    import re
    from pathlib import Path
    from Orchestrator.update import git_ops
    repo = Path(__file__).resolve().parents[3]
    pins = {verify.normalize_origin(line)
            for line in (repo / "installer" / "update-origin").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")}
    assert pins, "installer/update-origin pins nothing"
    assert verify.normalize_origin(git_ops.DEFAULT_REMOTE_URL) in pins
    for script in ("Scripts/install.sh", "Scripts/update.sh"):
        urls = re.findall(r"https://github\.com/TechBran/[^\s'\"]+", (repo / script).read_text())
        assert urls, script
        for url in urls:
            assert verify.normalize_origin(url) in pins, f"{script}: {url}"


def test_an_insteadof_rewrite_in_git_config_is_seen_through(box, trust):
    """The URL that matters is the one fetch uses: a planted url.*.insteadOf
    that rewrites the pinned origin to another one is refused."""
    push_upstream(box)
    write_pins(trust, origins=["https://github.com/example/the-real-repo.git"])
    git(box.root, "remote", "set-url", "origin", "https://github.com/example/the-real-repo.git")
    git(box.root, "config", f"url.{box.origin}.insteadOf",
        "https://github.com/example/the-real-repo.git")
    nothing_changed(box, collect(FakeRunner(box.root).run()))


def test_an_empty_origin_pin_accepts_nothing(box, trust):
    push_upstream(box)
    write_pins(trust, origins=[])
    done = nothing_changed(box, collect(FakeRunner(box.root).run()))
    assert "lists no update origin" in done["error"]


def test_a_pin_the_service_could_have_written_is_not_a_pin(box, trust, monkeypatch):
    push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    monkeypatch.setattr(verify, "TRUSTED_UID", os.getuid() + 1)   # i.e. not root-owned
    done = nothing_changed(box, collect(FakeRunner(box.root).run()))
    assert "not a trusted pin" in done["error"]


@pytest.mark.parametrize("hostile", ["group-writable file", "world-writable dir", "symlink"])
def test_a_writable_or_symlinked_pin_is_not_a_pin(box, trust, tmp_path, hostile):
    push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    pin = trust / "update-origin"
    if hostile == "group-writable file":
        pin.chmod(0o664)
    elif hostile == "world-writable dir":
        trust.chmod(0o777)
    else:
        elsewhere = tmp_path / "service-written-origin"
        elsewhere.write_text(f"{box.origin}\n")
        pin.unlink()
        pin.symlink_to(elsewhere)
    try:
        done = nothing_changed(box, collect(FakeRunner(box.root).run()))
    finally:
        trust.chmod(0o755)
    assert "not a trusted pin" in done["error"]


def test_an_unpinned_box_updates_and_says_so(box):
    v2 = push_upstream(box)
    events = collect(FakeRunner(box.root).run())
    assert events[-1]["succeeded"] is True and head(box.root) == v2
    text = "\n".join(logs(events))
    assert "no update origin is pinned" in text
    assert "no release signing key is pinned" in text


# ── the release signature ───────────────────────────────────────────────

def test_with_a_pinned_key_an_unsigned_commit_is_refused(box, trust, key):
    push_upstream(box)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    done = nothing_changed(box, collect(FakeRunner(box.root).run()))
    assert "not signed with a release key" in done["error"]


def test_a_commit_signed_by_another_key_is_refused(box, trust, key, tmp_path):
    stranger = make_signing_key(tmp_path / "stranger-keys", "stranger")
    push_signed(box, stranger)
    write_pins(trust, signers=[key.line])
    done = nothing_changed(box, collect(FakeRunner(box.root).run()))
    assert "not from a pinned release key" in done["error"]


def test_a_commit_signed_by_the_pinned_key_is_installed(box, trust, key):
    signed = push_signed(box, key)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    events = collect(FakeRunner(box.root).run())
    assert events[-1]["succeeded"] is True, events[-1]
    assert head(box.root) == signed
    assert not [t for t in logs(events) if "WARNING: no" in t]


def test_a_key_file_with_only_comments_does_not_enforce_signatures(box, trust):
    v2 = push_upstream(box)
    write_pins(trust, signers=[])
    events = collect(FakeRunner(box.root).run())
    assert events[-1]["succeeded"] is True and head(box.root) == v2
    assert any("holds no key" in t for t in logs(events))


def test_a_planted_allowed_signers_file_in_git_config_is_ignored(box, trust, key, tmp_path):
    """An attacker who can write .git/config points git at their own allowed
    signers file and signs with their own key. Plain `git verify-commit` then
    passes; the updater's must not."""
    attacker = make_signing_key(tmp_path / "attacker-keys", "attacker")
    signed = push_signed(box, attacker)
    planted = tmp_path / "attacker_allowed_signers"
    planted.write_text(attacker.line + "\n")
    git(box.root, "config", "gpg.ssh.allowedSignersFile", str(planted))
    git(box.root, "fetch", "-q", "origin")
    plain = subprocess.run(["git", "verify-commit", signed], cwd=str(box.root),
                           capture_output=True, text=True)
    assert plain.returncode == 0, "precondition: the planted config fools plain git"

    write_pins(trust, signers=[key.line])
    nothing_changed(box, collect(FakeRunner(box.root).run()))


def test_a_planted_ssh_program_in_git_config_is_ignored(box, trust, key, tmp_path):
    """...or replaces the verifier itself with one that always says Good."""
    attacker = make_signing_key(tmp_path / "attacker-keys", "attacker")
    signed = push_signed(box, attacker)
    fake = tmp_path / "always-good-ssh-keygen"
    fake.write_text("#!/bin/sh\n"
                    "case \" $* \" in *find-principals*) echo release@example.invalid ;;\n"
                    "*) echo 'Good \"git\" signature for release@example.invalid with ED25519 key SHA256:x' ;; esac\n"
                    "exit 0\n")
    fake.chmod(0o755)
    planted = tmp_path / "planted_signers"
    planted.write_text(key.line + "\n")
    git(box.root, "config", "gpg.ssh.program", str(fake))
    git(box.root, "config", "gpg.ssh.allowedSignersFile", str(planted))
    git(box.root, "fetch", "-q", "origin")
    plain = subprocess.run(["git", "verify-commit", signed], cwd=str(box.root),
                           capture_output=True, text=True)
    assert plain.returncode == 0, f"precondition: the planted verifier fools plain git: {plain.stderr}"

    write_pins(trust, signers=[key.line])
    nothing_changed(box, collect(FakeRunner(box.root).run()))


def test_the_reset_installs_the_verified_commit_even_if_origin_main_moves(box, trust, key,
                                                                         monkeypatch):
    """Between the signature check and the reset, origin/main is re-pointed at
    an unsigned commit (a hostile .git, or a fetch racing the update). The
    commit installed is the one that was verified."""
    signed = push_signed(box, key)
    (box.seed / "app.py").write_text("VERSION = 666\n")
    git(box.seed, "commit", "-q", "-am", "unsigned, kept off main")
    evil = git(box.seed, "rev-parse", "HEAD")
    git(box.seed, "push", "-q", "origin", "HEAD:refs/heads/evil")
    write_pins(trust, signers=[key.line])
    real_check = verify.check_commit

    def check_then_move(root, sha):
        notes = real_check(root, sha)
        git(root, "fetch", "-q", "origin", "evil")
        git(root, "update-ref", "refs/remotes/origin/main", evil)
        return notes

    monkeypatch.setattr(runner_mod.verify, "check_commit", check_then_move)
    done = collect(FakeRunner(box.root).run())[-1]
    assert done["succeeded"] is True, done
    assert head(box.root) == signed


def test_an_update_that_changes_the_pin_sources_says_they_need_install_sh(box):
    (box.seed / "installer").mkdir()
    (box.seed / "installer" / "allowed_signers").write_text("# a key arrives here\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v2 adds the release key file")
    git(box.seed, "push", "-q", "origin", "main")
    events = collect(FakeRunner(box.root).run())
    assert events[-1]["succeeded"] is True
    assert any("installer/allowed_signers" in t and "install.sh" in t for t in logs(events))


# ── what .git says cannot choose or change the commit installed ─────────
#
# 2026-10 review of rank 79: the origin pin checked the URL, but the commit
# installed was read back from the checkout's own refs, and the reset wrote
# whatever the object store, a replace ref, a hook or a filter made of it.
# Each attacker below can write .git and nothing else.

def test_a_tag_named_origin_main_does_not_choose_the_commit(box, trust, tmp_path):
    v2 = push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    evil = plant_commit(box, tmp_path, {"app.py": "EVIL = True\n"})
    # Data only: a tag wins git's lookup of "origin/main" over the remote ref.
    git(box.root, "update-ref", "refs/tags/origin/main", evil)
    assert git(box.root, "rev-parse", "origin/main") == evil, "precondition: the tag shadows"

    done = collect(FakeRunner(box.root).run())[-1]
    assert done["succeeded"] is True, done
    assert head(box.root) == v2
    assert (box.root / "app.py").read_text() == "VERSION = 2\n"


def test_a_remote_fetch_refspec_does_not_choose_the_commit(box, trust, tmp_path):
    v2 = push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    evil = plant_commit(box, tmp_path, {"app.py": "EVIL = True\n"})
    git(box.root, "update-ref", "refs/remotes/origin/main", evil)
    # One config line: a plain `git fetch origin main` no longer moves origin/main.
    git(box.root, "config", "--replace-all", "remote.origin.fetch",
        "+refs/heads/main:refs/remotes/origin/upstream")

    done = collect(FakeRunner(box.root).run())[-1]
    assert done["succeeded"] is True, done
    assert head(box.root) == v2


def test_the_origins_main_is_asked_without_the_callers_git_variables(box, tmp_path, monkeypatch):
    """The service's environment comes partly from the service-writable .env:
    a GIT_DIR or GIT_CONFIG_PARAMETERS there would bring an insteadOf that
    sends the question to another remote."""
    from Orchestrator.update import git_ops
    v2 = push_upstream(box)
    elsewhere = tmp_path / "elsewhere.git"
    git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    evil = plant_commit(box, tmp_path, {"app.py": "EVIL = True\n"})
    git(box.root, "push", "-q", str(elsewhere), f"{evil}:refs/heads/main")
    cfg = tmp_path / "cfg.git"
    git(tmp_path, "init", "-q", "--bare", str(cfg))
    git(cfg, "config", f"url.{elsewhere}.insteadOf", str(box.origin))
    monkeypatch.setenv("GIT_DIR", str(cfg))
    assert git_ops.remote_main_sha(str(box.origin)) == v2
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'url.{elsewhere}.insteadof'='{box.origin}'")
    assert git_ops.remote_main_sha(str(box.origin)) == v2
    with pytest.raises(subprocess.CalledProcessError):
        git_ops.remote_main_sha(str(tmp_path / "no-such-remote.git"))


def test_a_replace_ref_cannot_swap_the_tree_under_a_signed_commit(box, trust, key, tmp_path):
    signed = push_signed(box, key)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    git(box.root, "fetch", "-q", "origin", "main")
    evil_tree = tree_with(box.root, signed, "app.py", "EVIL = True\n", tmp_path / "idx")
    git(box.root, "replace", git(box.root, "rev-parse", f"{signed}^{{tree}}"), evil_tree)

    done = collect(FakeRunner(box.root).run())[-1]
    assert done["succeeded"] is True, done
    assert head(box.root) == signed
    assert (box.root / "app.py").read_text() == "VERSION = 3\n", "the replace ref was honoured"


def test_a_rewritten_object_is_refused_before_anything_changes(box, trust, key, tmp_path):
    signed = push_signed(box, key)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    git(box.root, "-c", "fetch.unpackLimit=100000", "fetch", "-q", "origin", "main")
    tree = git(box.root, "rev-parse", f"{signed}^{{tree}}")
    evil_tree = tree_with(box.root, signed, "app.py", "EVIL = True\n", tmp_path / "idx")
    raw = subprocess.run(["git", "cat-file", "tree", evil_tree], cwd=str(box.root),
                         capture_output=True, check=True).stdout
    overwrite_loose_object(box.root, tree, "tree", raw)

    done = nothing_changed(box, collect(FakeRunner(box.root).run()))
    assert "does not hash to its id" in done["error"]


def test_a_filter_that_rewrites_files_during_the_reset_is_rolled_back(box, trust, tmp_path):
    push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    smudge_filter(box.root, tmp_path, "app.py", "EVIL = True\n")

    r = FakeRunner(box.root)
    done = collect(r.run())[-1]
    assert done["succeeded"] is False and done["failed_phase"] == "reset_hard", done
    assert "app.py: differs from the release" in done["error"]
    assert "rolled back" in done["error"]
    assert head(box.root) == box.v1
    assert not [c for c in r.calls if "pip" in " ".join(c) and "install" in c], \
        "nothing may be installed from a tree that is not the commit"


def test_no_hook_runs_during_the_update(box, trust, tmp_path):
    v2 = push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    marker = tmp_path / "hook-ran"
    for name in ("reference-transaction", "post-checkout", "post-merge", "pre-auto-gc"):
        hook = box.root / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        hook.chmod(0o755)

    done = collect(FakeRunner(box.root).run())[-1]
    assert done["succeeded"] is True and head(box.root) == v2
    assert not marker.exists(), marker.read_text()


def test_the_retired_slug_is_repointed_before_the_pin_check(box, trust):
    """GitHub still redirects the pre-rename slug, so a box only ever updated
    from the Portal can still have it; the pin lists only the new URL."""
    from Orchestrator.update import git_ops
    git(box.root, "remote", "set-url", "origin", "https://github.com/TechBran/blackbox-poc.git")
    write_pins(trust, origins=[git_ops.DEFAULT_REMOTE_URL])

    notes = verify.check_origin(box.root)
    assert git(box.root, "remote", "get-url", "origin") == git_ops.DEFAULT_REMOTE_URL
    assert any("retired" in n for n in notes)
    assert verify.check_origin(box.root) == [], "a second check has nothing to re-point"


# ── the update panels never fetch from, or offer, an unpinned origin ────

@pytest.fixture
def panel(box, monkeypatch, app_client):
    from .test_updater_safety import wire_routes
    return wire_routes(box, monkeypatch, app_client)


def test_status_and_preflight_refuse_an_unpinned_origin_without_fetching(panel, box, trust):
    push_upstream(box)
    write_pins(trust, origins=["https://github.com/example/the-real-repo.git"])
    for status in (panel.client.get("/update/status").json(),
                   panel.client.post("/update/preflight").json()):
        assert status["refused"] == "unverified_update"
        assert "the-real-repo" in status["fetch_error"]
        assert "commits" not in status and "latest_sha" not in status
    assert git(box.root, "rev-parse", "refs/remotes/origin/main") == box.v1, "nothing fetched"


def test_status_offers_main_as_the_origin_reports_it(panel, box, trust, tmp_path):
    v2 = push_upstream(box)
    write_pins(trust, origins=[str(box.origin)])
    evil = plant_commit(box, tmp_path, {"app.py": "EVIL = True\n"})
    git(box.root, "update-ref", "refs/tags/origin/main", evil)
    status = panel.client.post("/update/preflight").json()
    assert status["latest_sha"] == v2 and status["commits_behind"] == 1
    assert [c["sha"] for c in status["commits"]] == [v2]


# ── a failed update gives back the local changes it stashed ─────────────

def _needs_a_package(box):
    (box.seed / "Scripts" / "onboarding").mkdir(parents=True)
    (box.seed / "Scripts" / "onboarding" / "system-packages.txt").write_text(
        "newpkg                # MUST_HAVE # needed\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v2 needs a package")
    git(box.seed, "push", "-q", "origin", "main")


def _apt_fails(runner, cmd):
    if cmd[-1] == runner_mod.APT_PREFLIGHT_PROBE_PKG:
        return runner_mod.APT_HELPER_RC_NOT_ALLOWLISTED, ""
    if cmd[-1] == "newpkg":
        return 1, "E: unable to locate package"
    return 0, ""


def test_a_rolled_back_update_puts_the_stashed_local_changes_back(box):
    _needs_a_package(box)
    (box.root / "app.py").write_text("VERSION = 1  # hand fix on the box\n")
    r = FakeRunner(box.root, responder=_apt_fails, discard_local_changes=True)
    done = collect(r.run())[-1]

    assert done["succeeded"] is False and done["failed_phase"] == "apt_install"
    assert head(box.root) == box.v1
    assert (box.root / "app.py").read_text() == "VERSION = 1  # hand fix on the box\n"
    assert stash_entries(box.root) == [], "the re-applied stash is dropped"
    assert done["stash_refs"] == [] and done["stash_ref"] == ""
    assert "put back" in done["error"] and "saved in stash" not in done["error"]


def test_a_stash_that_cannot_go_back_is_kept_and_named(box, monkeypatch):
    _needs_a_package(box)
    (box.root / "app.py").write_text("VERSION = 1  # hand fix on the box\n")

    def no_apply(root, sha):
        raise subprocess.CalledProcessError(1, ["git", "stash", "apply"], "", "conflict")

    monkeypatch.setattr(runner_mod.git_ops, "stash_apply", no_apply)
    r = FakeRunner(box.root, responder=_apt_fails, discard_local_changes=True)
    done = collect(r.run())[-1]

    assert head(box.root) == box.v1
    entries = stash_entries(box.root)
    assert len(entries) == 1 and entries[0].endswith(f"auto-stash-{r.pre_update_tag}")
    assert done["stash_ref"] == git(box.root, "rev-parse", "stash@{0}")
    assert "saved in stash" in done["error"]
