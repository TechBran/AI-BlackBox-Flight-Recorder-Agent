"""Tests for the apt phase of Orchestrator/update/runner.py.

Regression cover for the stale-helper incident (customer, 2026-07-27): an
in-app update died at `apt install novnc failed (rc=4)` because the box's
root-owned /usr/local/sbin/blackbox-apt-install still had a pre-rename
install root baked in, and the whole update — code, pip, everything — was
rolled back over one OPTIONAL package.

Behaviours pinned here, each of which was a separate defect:
  - phase order is MONOTONIC: the single privileged install.sh re-run happens
    in systemd_regen, AFTER apt/pip/mcp. An earlier re-run (before apt) was
    tried and reverted — install.sh's own KillMode=process restart would
    SIGTERM the uvicorn process iterating the update;
  - a SHOULD_HAVE package failure warns and continues, a MUST_HAVE failure
    still aborts + rolls back, and an unclassifiable package is MUST_HAVE;
  - the preflight names the remedy command instead of surfacing `rc=4`;
  - the bucket parser matches the helper's allowlist grep exactly, and
    MUST_HAVE is sticky across a duplicate entry.

Style follows test_git_ops.py: real objects (a real UpdateManager over a
tmp_path root), fakes only at the subprocess boundary, so the phase
sequencing that broke in production is what actually gets exercised.
"""
import asyncio

import pytest

from Orchestrator.update import runner as runner_mod
from Orchestrator.update.manager import UpdateManager
from Orchestrator.update.runner import (
    APT_HELPER, APT_PREFLIGHT_PROBE_PKG,
    APT_HELPER_RC_ALLOWLIST_UNREADABLE, APT_HELPER_RC_NOT_ALLOWLISTED,
    UpdateRunner, _extract_allowlist_path, _parse_pkg_buckets, _parse_pkg_list,
)


FROM_SHA = "a" * 40
TARGET_SHA = "b" * 40

# A stale helper's real exit-4 stderr, verbatim from the customer's log.
STALE_HELPER_OUTPUT = (
    "[blackbox-apt-install] ERROR: allowlist file not readable: "
    "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/"
    "Scripts/onboarding/system-packages.txt\n"
)

ALLOWLIST_HEADER = "# AI BlackBox System Package Requirements\n" \
                   "# Format: <package> # <bucket> # <reason>\n"


# ── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_real_restart(monkeypatch):
    """_run_locked schedules `sudo systemctl restart blackbox.service` via
    call_later. asyncio.run() closes the loop long before it fires, but a
    test suite must never be one scheduling change away from bouncing the
    dev box's service."""
    monkeypatch.setattr(runner_mod, "_fire_detached_restart", lambda: None)


@pytest.fixture
def root(tmp_path):
    """A BlackBox root with just enough tree for the runner to walk."""
    r = tmp_path / "blackbox"
    (r / "Manifest").mkdir(parents=True)
    (r / "Scripts" / "onboarding").mkdir(parents=True)
    return r


@pytest.fixture
def resets(monkeypatch, root):
    """Stub every git_ops call the runner makes; record reset_hard targets
    so rollback assertions read as `TAG in resets`."""
    recorded: list[str] = []
    monkeypatch.setattr(runner_mod.git_ops, "current_sha", lambda p: FROM_SHA)
    monkeypatch.setattr(runner_mod.git_ops, "fetch_origin_main", lambda p: None)
    monkeypatch.setattr(runner_mod.git_ops, "latest_origin_sha", lambda p: TARGET_SHA)
    monkeypatch.setattr(runner_mod.git_ops, "tag", lambda p, t: None)
    monkeypatch.setattr(runner_mod.git_ops, "current_short", lambda p: TARGET_SHA[:7])
    monkeypatch.setattr(runner_mod.git_ops, "reset_hard",
                        lambda p, ref: recorded.append(ref))
    return recorded


def set_changed(monkeypatch, files):
    monkeypatch.setattr(runner_mod.git_ops, "diff_files",
                        lambda p, a, b: list(files))


def write_allowlist(root, body):
    (root / "Scripts" / "onboarding" / "system-packages.txt").write_text(
        ALLOWLIST_HEADER + body)


# ── harness ─────────────────────────────────────────────────────────────

class FakeRunner(UpdateRunner):
    """UpdateRunner with the subprocess boundary replaced by a scripted
    responder. Every command the runner shells out to is recorded in order,
    which is what the phase-ordering assertions read."""

    def __init__(self, root, mgr, responder):
        super().__init__(root, mgr)
        self.calls: list[list[str]] = []
        self._responder = responder

    async def _run(self, cmd, timeout=120.0):
        cmd = list(cmd)
        self.calls.append(cmd)
        return self._responder(self, cmd)


def is_install_sh(cmd):
    return cmd[:3] == ["sudo", "-n", "bash"] and cmd[-1].endswith("Scripts/install.sh")


def is_probe(cmd):
    return APT_HELPER in cmd and cmd[-1] == APT_PREFLIGHT_PROBE_PKG


def is_pkg_install(cmd):
    return APT_HELPER in cmd and cmd[-1] != APT_PREFLIGHT_PROBE_PKG


def healthy(_runner, cmd):
    """Everything works: sudo available, helper reads its allowlist, apt OK."""
    if cmd[:3] == ["sudo", "-n", "true"]:
        return 0, ""
    if is_probe(cmd):
        return APT_HELPER_RC_NOT_ALLOWLISTED, "package not in allowlist"
    if is_pkg_install(cmd):
        return 0, f"{cmd[-1]} installed."
    if is_install_sh(cmd):
        return 0, "[install] done"
    return 0, ""


def drive(runner):
    """Collect every event the runner yields."""
    async def _collect():
        return [ev async for ev in runner.run()]
    return asyncio.run(_collect())


def logs(events):
    return [e["text"] for e in events if e.get("type") == "log"]


def final(events):
    return events[-1]


def make(root, responder=healthy):
    return FakeRunner(root, UpdateManager(root), responder)


# ── D2: the privileged install.sh re-run stays AFTER the apt phase ──────

def test_privileged_regen_runs_after_apt_phase(monkeypatch, root, resets):
    """B1 revert (customer, 2026-07-27, stale helper after commit 5eabbe45):
    an early helper refresh BEFORE apt was tried and is dangerous — install.sh's
    Step 7 restart (KillMode=process) would kill the update mid-flight. Phase
    order must be monotonic: the single install.sh re-run happens in
    systemd_regen, after every apt call (probe + installs)."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    set_changed(monkeypatch, ["installer/templates/blackbox-apt-install.sh",
                              "Scripts/onboarding/system-packages.txt"])
    r = make(root)
    drive(r)

    regen_at = [i for i, c in enumerate(r.calls) if is_install_sh(c)]
    apt_at = [i for i, c in enumerate(r.calls) if APT_HELPER in c]
    assert regen_at, "helpers bucket did not trigger the privileged re-run"
    assert apt_at, "apt phase never ran"
    assert apt_at[-1] < regen_at[0], (
        f"install.sh re-ran at call {regen_at[0]} but apt was still active at "
        f"{apt_at[-1]} — the privileged re-run must stay AFTER the apt phase")


def test_apt_phase_never_reruns_install_sh_early(monkeypatch, root, resets):
    """No mid-apt self-repair: a stale helper blocking a MUST_HAVE package must
    fail + report, NOT trigger an early install.sh re-run inside the apt phase
    (that was the reverted hazard). The one re-run, if any, is systemd_regen."""
    write_allowlist(root, "xvfb  # MUST_HAVE # CU virtual displays\n")
    # apt bucket only — systemd_regen must not fire at all here.
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def stale_helper(_r, cmd):
        if is_probe(cmd):
            return APT_HELPER_RC_ALLOWLIST_UNREADABLE, STALE_HELPER_OUTPUT
        return healthy(_r, cmd)

    r = make(root, stale_helper)
    events = drive(r)
    assert final(events)["succeeded"] is False
    assert not any(is_install_sh(c) for c in r.calls), \
        "apt phase re-ran install.sh early — the reverted self-repair is back"


def test_privileged_regen_runs_exactly_once_per_update(monkeypatch, root, resets):
    """helpers + systemd both lit → still ONE install.sh re-run. Running the
    600s-timeout step twice buys nothing (it is idempotent)."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    set_changed(monkeypatch, ["installer/templates/blackbox-apt-install.sh",
                              "Scripts/install.sh",
                              "Scripts/onboarding/system-packages.txt"])
    r = make(root)
    drive(r)
    assert sum(1 for c in r.calls if is_install_sh(c)) == 1


def test_no_helper_refresh_when_helpers_bucket_is_clear(monkeypatch, root, resets):
    """A pure apt update must not gain a privileged install.sh re-run it
    never used to do."""
    write_allowlist(root, "xvfb  # MUST_HAVE # CU virtual displays\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])
    r = make(root)
    events = drive(r)
    assert not any(is_install_sh(c) for c in r.calls)
    assert final(events)["succeeded"] is True


# ── D3: optional package failures must not discard the update ───────────

def test_should_have_failure_warns_and_completes(monkeypatch, root, resets):
    """novnc is SHOULD_HAVE — 'degraded but functional'. Its failure must
    not throw away a landed code + dependency update."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def novnc_fails(_r, cmd):
        if is_pkg_install(cmd):
            return 100, "E: Unable to locate package novnc"
        return healthy(_r, cmd)

    r = make(root, novnc_fails)
    events = drive(r)

    assert final(events)["succeeded"] is True, "optional package rolled the update back"
    assert r.pre_update_tag not in resets, "rolled back on an optional package"
    warning = [t for t in logs(events) if "novnc" in t and "WARNING" in t]
    assert warning, "no warning named the package that failed"
    assert "apt-get install -y novnc" in warning[0], "warning gave no remedy"


def test_must_have_failure_still_rolls_back(monkeypatch, root, resets):
    """Must not regress: a required package failing is still fatal."""
    write_allowlist(root, "python3-dbus  # MUST_HAVE # XDG portal screenshot\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def required_fails(_r, cmd):
        if is_pkg_install(cmd):
            return 100, "E: Unable to locate package python3-dbus"
        return healthy(_r, cmd)

    r = make(root, required_fails)
    events = drive(r)

    assert final(events)["succeeded"] is False
    assert final(events)["failed_phase"] == "apt_install"
    assert r.pre_update_tag in resets, "required package failed without rollback"


def test_unclassifiable_package_is_treated_as_must_have(monkeypatch, root, resets):
    """If the allowlist can't be parsed we know nothing about the package —
    fail safe (roll back), never fail open (silently continue)."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def novnc_fails(_r, cmd):
        if is_pkg_install(cmd):
            return 100, "boom"
        return healthy(_r, cmd)

    r = make(root, novnc_fails)
    # Simulate an unreadable/unparseable allowlist at bucket-resolution time
    # while the package list itself still yields novnc.
    monkeypatch.setattr(r, "_apt_package_buckets", lambda: {})
    events = drive(r)

    assert final(events)["succeeded"] is False
    assert r.pre_update_tag in resets, "unknown bucket was treated as optional"


# ── D1/F5: preflight must be actionable ─────────────────────────────────

def test_preflight_reports_remedy_instead_of_bare_rc(monkeypatch, root, resets):
    """A stale helper that install.sh cannot repair (e.g. no sudo grant)
    must still produce a sentence a customer can act on, and must not have
    installed anything before saying so."""
    write_allowlist(root, "xvfb  # MUST_HAVE # CU virtual displays\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def stale_helper(_r, cmd):
        if is_probe(cmd):
            return APT_HELPER_RC_ALLOWLIST_UNREADABLE, STALE_HELPER_OUTPUT
        return healthy(_r, cmd)

    r = make(root, stale_helper)
    events = drive(r)
    err = final(events)["error"]

    assert final(events)["succeeded"] is False
    assert f"sudo bash {root}/Scripts/install.sh" in err, \
        "preflight did not name the one-command remedy"
    assert "blackbox-poc-main" in err, "preflight did not name the stale path"
    assert str(root) in err, "preflight did not name where this box actually is"
    assert not any(is_pkg_install(c) for c in r.calls), \
        "preflight let the apt phase mutate the box before reporting"


def test_preflight_failure_with_only_optional_packages_continues(monkeypatch, root, resets):
    """Broken helper + nothing but SHOULD_HAVE packages pending → warn, skip
    them, keep the update. Same call as an optional install failure."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def never_repairable(_r, cmd):
        if is_probe(cmd):
            return APT_HELPER_RC_ALLOWLIST_UNREADABLE, STALE_HELPER_OUTPUT
        return healthy(_r, cmd)

    r = make(root, never_repairable)
    events = drive(r)

    assert final(events)["succeeded"] is True
    assert r.pre_update_tag not in resets
    assert any("novnc" in t for t in logs(events) if "WARNING" in t)


def test_healthy_preflight_costs_one_no_op_probe(monkeypatch, root, resets):
    """The probe must be a package that cannot be in the allowlist, so a
    healthy run never installs anything it wasn't asked to."""
    write_allowlist(root, "xvfb  # MUST_HAVE # CU virtual displays\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])
    r = make(root)
    drive(r)
    probes = [c for c in r.calls if is_probe(c)]
    assert len(probes) == 1
    assert APT_PREFLIGHT_PROBE_PKG not in _parse_pkg_list(
        (root / "Scripts/onboarding/system-packages.txt").read_text())


# ── parser units ────────────────────────────────────────────────────────

def test_bucket_is_read_positionally_not_by_substring():
    """The helper's allowlist grep is anchored on the FIRST #-field. If this
    parser disagreed, the runner would try to install a package the helper
    refuses at rc=5."""
    buckets = _parse_pkg_buckets(
        "adb  # FEATURE_OPTIONAL # pairing, not MUST_HAVE despite this text\n"
        "xvfb # MUST_HAVE # CU virtual displays\n"
        "novnc # SHOULD_HAVE # CU live-view\n"
        "# ── section comment ──\n"
        "\n"
    )
    assert buckets == {"xvfb": "MUST_HAVE", "novnc": "SHOULD_HAVE"}


def test_parse_pkg_list_matches_bucket_keys():
    body = "xvfb # MUST_HAVE # x\nnovnc # SHOULD_HAVE # y\nadb # FEATURE_OPTIONAL # z\n"
    assert _parse_pkg_list(body) == set(_parse_pkg_buckets(body))


def test_extract_allowlist_path_reads_the_helpers_error_line():
    assert _extract_allowlist_path(STALE_HELPER_OUTPUT) == (
        "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/"
        "Scripts/onboarding/system-packages.txt")
    assert _extract_allowlist_path("something else entirely") == ""


# ── M1: MUST_HAVE is sticky across a duplicate, in either order ──────────

def test_must_have_sticky_should_have_first():
    """xvfb appears SHOULD_HAVE then MUST_HAVE (dup) — resolves MUST_HAVE, an
    upgrade. The file keeps xvfb as MUST_HAVE inside the SHOULD_HAVE section,
    the exact editing pattern that produces this dup."""
    assert _parse_pkg_buckets(
        "xvfb # SHOULD_HAVE # oops wrong bucket\n"
        "xvfb # MUST_HAVE # CU gates on this\n"
    )["xvfb"] == "MUST_HAVE"


def test_must_have_sticky_must_have_first():
    """Reverse order: MUST_HAVE then SHOULD_HAVE (dup) must NOT downgrade."""
    assert _parse_pkg_buckets(
        "xvfb # MUST_HAVE # CU gates on this\n"
        "xvfb # SHOULD_HAVE # oops wrong bucket\n"
    )["xvfb"] == "MUST_HAVE"


# ── M2: parser matches the helper's allowlist grep EXACTLY ──────────────

def test_parser_matches_helper_grep_anchoring():
    """The helper builds its allowlist with
    `grep -E '^[a-zA-Z0-9._+-]+\\s+#\\s+(MUST_HAVE|SHOULD_HAVE)'`. A line the
    parser accepts but that grep rejects becomes an rc=5 the runner cannot see
    coming — and (default MUST_HAVE) rolls the whole update back over a typo.
    So the parser must reject exactly what the grep rejects."""
    # Leading indentation — the grep is ^-anchored at column 0.
    assert _parse_pkg_buckets("  xvfb # MUST_HAVE # x\n") == {}
    # No whitespace BEFORE the first # — grep needs \s+#.
    assert _parse_pkg_buckets("xvfb# MUST_HAVE # x\n") == {}
    # No whitespace AFTER the first # — grep needs #\s+.
    assert _parse_pkg_buckets("xvfb #MUST_HAVE # x\n") == {}
    # Canonical form is accepted.
    assert _parse_pkg_buckets("xvfb # MUST_HAVE # x\n") == {"xvfb": "MUST_HAVE"}
