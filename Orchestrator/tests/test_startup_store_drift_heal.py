"""Startup drift-heal from the trusted template store: the rc==2 branch.

2026-10-09 audit W07 / W08 (rank 18): a box that pulled the July 27 code but
never re-ran `sudo bash Scripts/install.sh` still has the May-24
/usr/local/sbin/blackbox-write-systemd, which demands `<target_kind>
<source_file>` and exits 2 on the kind-only call, and has no
/etc/blackbox/templates. Every restart logged an opaque
"drift-heal helper failed rc=2". The hook must say exactly what is wrong and
the one command that fixes it, and must never crash startup.

Orchestrator/startup.py cannot be imported hermetically (it reads config.ini
from the cwd and builds the whole app), so the two store helpers are lifted
out of its AST and executed in a namespace holding only what they use.
"""
import ast
import logging
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP = REPO_ROOT / "Orchestrator" / "startup.py"
TEMPLATE = REPO_ROOT / "installer" / "templates" / "sudoers-blackbox-system"
HELPER = "/usr/local/sbin/blackbox-write-systemd"
NAMES = {"_store_entry_path", "_drift_heal_from_store"}


@pytest.fixture
def heal(monkeypatch, tmp_path):
    """Return (drift_heal_from_store, calls, sandbox) with sudo faked."""
    tree = ast.parse(STARTUP.read_text(encoding="utf-8"))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
    assert {f.name for f in fns} == NAMES, "startup.py lost a store drift-heal helper"
    logger = logging.getLogger("test.startup.drift_heal")
    ns = {"os": os, "logger": logger, "Path": Path, "__file__": str(STARTUP)}
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(STARTUP), "exec"), ns)

    sandbox = tmp_path / "root"
    monkeypatch.setenv("BLACKBOX_WRITE_SYSTEMD_DEST_ROOT", str(sandbox))
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile",
                        lambda p: True if p == HELPER else real_isfile(p))
    calls = []
    state = {"rc": 0, "stderr": "", "stdout": ""}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, state["rc"], stdout=state["stdout"],
                                           stderr=state["stderr"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    return ns["_drift_heal_from_store"], calls, state, sandbox


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_rc2_stale_helper_logs_the_install_sh_remedy_loudly(heal, caplog, tmp_path):
    drift_heal, calls, state, _ = heal
    state.update(rc=2, stderr="[blackbox-write-systemd] ERROR: usage: "
                              "blackbox-write-systemd <target_kind> <source_file>")
    deployed = tmp_path / "blackbox-system"
    with caplog.at_level(logging.INFO, logger="test.startup.drift_heal"):
        drift_heal("sudoers-system", str(deployed), "sudoers")     # must not raise
    assert calls == [["sudo", "-n", HELPER, "sudoers-system"]]
    errors = _messages(caplog, logging.ERROR)
    assert errors, f"rc=2 was not logged at ERROR: {caplog.records}"
    msg = errors[0]
    assert "sudo bash" in msg and "Scripts/install.sh" in msg
    assert str(REPO_ROOT) in msg                 # the exact command to paste
    assert "rc=2" in msg
    assert "template store" in msg               # names what is missing
    assert str(deployed) in msg
    assert not [m for m in _messages(caplog, logging.WARNING) if "failed rc=2" in m]


def test_rc2_says_whether_the_store_was_ever_published(heal, caplog, tmp_path):
    drift_heal, _, state, sandbox = heal
    state.update(rc=2)
    with caplog.at_level(logging.INFO, logger="test.startup.drift_heal"):
        drift_heal("sudoers-system", str(tmp_path / "x"), "sudoers")
    assert "not been published" in _messages(caplog, logging.ERROR)[0]

    caplog.clear()
    (sandbox / "etc" / "blackbox" / "templates").mkdir(parents=True)
    with caplog.at_level(logging.INFO, logger="test.startup.drift_heal"):
        drift_heal("sudoers-system", str(tmp_path / "x"), "sudoers")
    assert "not been published" not in _messages(caplog, logging.ERROR)[0]


def test_rc6_still_names_install_sh(heal, caplog, tmp_path):
    drift_heal, _, state, _ = heal
    state.update(rc=6)
    with caplog.at_level(logging.INFO, logger="test.startup.drift_heal"):
        drift_heal("apt-install-helper", str(tmp_path / "apt"), "helpers")
    warnings = _messages(caplog, logging.WARNING)
    assert warnings and "install.sh" in warnings[0] and "rc=6" in warnings[0]


def test_rc0_logs_the_heal(heal, caplog, tmp_path):
    drift_heal, calls, state, _ = heal
    state.update(rc=0)
    with caplog.at_level(logging.INFO, logger="test.startup.drift_heal"):
        drift_heal("sudoers-system", str(tmp_path / "s"), "sudoers")
    assert any("drift-healed" in m for m in _messages(caplog, logging.INFO))
    assert not _messages(caplog, logging.ERROR)


def test_rc0_already_current_logs_a_match_not_a_heal(heal, caplog, tmp_path):
    """The service cannot read the root-only 0440 sudoers file, so the helper
    (root) compares it: an already-current file is reported as a match, with
    no 'drift-healed' claim and no warning (it used to log a failed write on
    every boot because /etc is read-only inside the service namespace)."""
    drift_heal, calls, state, _ = heal
    state.update(rc=0, stdout="[blackbox-write-systemd] /etc/sudoers.d/blackbox-system "
                              "already current (matches /etc/blackbox/templates/sudoers-system)\n")
    with caplog.at_level(logging.INFO, logger="test.startup.drift_heal"):
        drift_heal("sudoers-system", str(tmp_path / "unreadable-sudoers"), "sudoers")
    info = _messages(caplog, logging.INFO)
    assert any("already matches the trusted store" in m for m in info), info
    assert not any("drift-healed" in m for m in info)
    assert not _messages(caplog, logging.WARNING) and not _messages(caplog, logging.ERROR)


def test_startup_dispatches_only_kinds_the_sudoers_template_grants():
    """The kind-only calls the startup hooks make must each be an exact grant
    (the template no longer grants `blackbox-write-systemd *`)."""
    kinds = set(re.findall(r'_drift_heal_from_store\(\s*"([a-z-]+)"',
                           STARTUP.read_text(encoding="utf-8")))
    assert kinds == {"sudoers-system", "apt-install-helper"}
    granted = set(re.findall(
        r"^REAL_USER_PLACEHOLDER ALL=\(root\) NOPASSWD: "
        + re.escape(HELPER) + r" ([a-z-]+)$",
        TEMPLATE.read_text(encoding="utf-8"), flags=re.M))
    assert kinds <= granted, f"startup dispatches {kinds - granted} with no grant"
