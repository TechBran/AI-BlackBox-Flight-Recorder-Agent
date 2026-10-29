"""The startup warning for a checkout other local accounts can write into
(audit 2026-10-09 rank 79, X04-security-clients-infra-13).

The service runs `python -m uvicorn` with the checkout as its working
directory, so Python imports from it first: a world-writable directory in it
lets any local account plant a module the service runs. install.sh removes the
bits (harden_tree_permissions); this check names what is still wrong in the
journal at every start. Warn only, never raises.
"""
from __future__ import annotations

import os

import pytest

from Orchestrator.utils import tree_perms


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "blackbox"
    for d in ("Orchestrator/routes", "Portal/modules", "Manifest", "credentials",
              "Orchestrator/venv/lib", ".claude/commands"):
        (root / d).mkdir(parents=True)
    for d in root.rglob("*"):
        d.chmod(0o755)
    root.chmod(0o755)
    (root / "Manifest").chmod(0o700)
    (root / "credentials").chmod(0o700)
    return root


@pytest.fixture(autouse=True)
def private_groups(monkeypatch):
    """Every group counts as the owner's own private group unless a test says
    otherwise (CI accounts differ from this box's)."""
    monkeypatch.setattr(tree_perms, "_group_is_shared", lambda gid, uid: False)


def test_a_clean_checkout_has_nothing_to_say(checkout):
    assert tree_perms.audit_checkout(checkout) == []


def test_a_world_writable_code_directory_is_named(checkout):
    (checkout / "Orchestrator" / "routes").chmod(0o777)
    (checkout / ".claude" / "commands").chmod(0o777)
    (warning,) = tree_perms.audit_checkout(checkout)
    assert "Orchestrator/routes" in warning and ".claude/commands" in warning
    assert "2 directories" in warning and "install.sh" in warning


def test_the_root_itself_counts(checkout):
    checkout.chmod(0o777)
    (warning,) = tree_perms.audit_checkout(checkout)
    assert "(the checkout root)" in warning


def test_group_write_counts_only_for_a_group_others_can_hold(checkout, monkeypatch):
    (checkout / "Portal").chmod(0o775)
    assert tree_perms.audit_checkout(checkout) == [], "a private group is the owner alone"
    monkeypatch.setattr(tree_perms, "_group_is_shared", lambda gid, uid: True)
    (warning,) = tree_perms.audit_checkout(checkout)
    assert "Portal" in warning


@pytest.mark.parametrize("name", ["Manifest", "credentials"])
@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750])
def test_manifest_and_credentials_must_be_0700(checkout, name, mode):
    (checkout / name).chmod(mode)
    warnings = tree_perms.audit_checkout(checkout)
    # 0777 is also world-writable, which the directory walk reports on its own.
    assert len(warnings) == (2 if mode & 0o002 else 1), warnings
    assert any(f"{name}/ is mode {mode:o}, not 0700" in w for w in warnings), warnings


def test_a_world_writable_parent_is_named_but_a_sticky_one_is_not(checkout):
    parent = checkout.parent
    try:
        parent.chmod(0o1777)
        assert tree_perms.audit_checkout(checkout) == [], "sticky, like /tmp"
        parent.chmod(0o777)
        (warning,) = tree_perms.audit_checkout(checkout)
        assert str(parent) in warning and "parent" in warning
    finally:
        parent.chmod(0o755)


def test_a_missing_private_directory_is_not_a_problem(checkout):
    (checkout / "credentials").rmdir()
    assert tree_perms.audit_checkout(checkout) == []


def test_venvs_and_build_trees_are_not_walked(checkout):
    (checkout / "Orchestrator" / "venv" / "lib").chmod(0o777)
    (checkout / "installer" / "node_modules" / "x").mkdir(parents=True)
    (checkout / "installer" / "node_modules" / "x").chmod(0o777)
    assert tree_perms.audit_checkout(checkout) == []


def test_symlinks_are_not_followed(checkout, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    outside.chmod(0o777)
    (checkout / "Portal" / "link").symlink_to(outside, target_is_directory=True)
    assert tree_perms.audit_checkout(checkout) == []


def test_a_huge_tree_is_cut_short_and_says_so(checkout):
    for i in range(30):
        d = checkout / "Apps" / f"app{i}"
        d.mkdir(parents=True)
        d.chmod(0o777)
    (warning,) = tree_perms.audit_checkout(checkout, max_dirs=10)
    assert "stopped after 10 directories" in warning


def test_an_unreadable_directory_never_raises(checkout):
    locked = checkout / "Orchestrator" / "locked"
    locked.mkdir()
    (locked / "inner").mkdir()
    locked.chmod(0o000)
    try:
        assert tree_perms.audit_checkout(checkout) == []
    finally:
        locked.chmod(0o755)


def test_a_missing_checkout_never_raises(tmp_path):
    assert tree_perms.audit_checkout(tmp_path / "nope") == []


def test_the_private_group_rule_against_this_process(tmp_path):
    """Real lookup, no monkeypatch: a directory owned by this process with its
    own primary group is shared only if that group lists someone else."""
    import grp
    import pwd
    me = pwd.getpwuid(os.getuid())
    g = grp.getgrgid(os.getgid())
    expected = g.gr_name != me.pw_name or any(m != me.pw_name for m in g.gr_mem)
    assert tree_perms._real_group_is_shared(os.getgid(), os.getuid()) is expected
    assert tree_perms._real_group_is_shared(os.getgid(), os.getuid() + 12345) is True
