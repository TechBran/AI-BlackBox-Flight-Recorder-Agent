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
    WRITE_SYSTEMD_HELPER, _REGEN_TARGETS,
    UpdateRunner, _extract_allowlist_path, _parse_pkg_buckets, _parse_pkg_list,
)


# Repo-relative template paths + write-systemd kinds the decomposed regen
# dispatches (install.sh-grant removal, 2026-07-27), looked up from the runner's
# own dispatch table so these tests track it rather than restating it.
_KIND_BY_TEMPLATE = {t.template: t.kind for t in _REGEN_TARGETS}
SUDOERS_TEMPLATE = "installer/templates/sudoers-blackbox-system"
APT_HELPER_TEMPLATE = "installer/templates/blackbox-apt-install.sh"
ZELLIJ_UNIT_TEMPLATE = "installer/templates/zellij-web.service"


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


@pytest.fixture(autouse=True)
def store_sandbox(monkeypatch, tmp_path):
    """Rebase the root-owned trusted template store under a hermetic tmp dir so
    _privileged_regen's repo-render-vs-store compare (install.sh-grant removal,
    2026-07-27 / M3) never reads the real /etc/blackbox/templates. Mirrors
    blackbox-write-systemd's BLACKBOX_WRITE_SYSTEMD_DEST_ROOT sandbox rebase.

    Autouse so EVERY test in this module is isolated from /etc; returns a
    `publish(kind, body)` so a test can stage what the human `install.sh` would
    have published. A test that does NOT publish a kind leaves it 'unpublished'
    (store entry absent) — the store-staleness case the runner must NOT dispatch.
    """
    store_root = tmp_path / "store-root"
    store_dir = store_root / "etc" / "blackbox" / "templates"
    store_dir.mkdir(parents=True)
    monkeypatch.setenv("BLACKBOX_WRITE_SYSTEMD_DEST_ROOT", str(store_root))

    def publish(kind, body="# regen template fixture\n"):
        (store_dir / kind).write_text(body)

    return publish


def set_changed(monkeypatch, files):
    monkeypatch.setattr(runner_mod.git_ops, "diff_files",
                        lambda p, a, b: list(files))


def write_allowlist(root, body):
    (root / "Scripts" / "onboarding" / "system-packages.txt").write_text(
        ALLOWLIST_HEADER + body)


def write_template(root, relpath, body="# regen template fixture\n"):
    """Materialize a git-tracked regen template under the fake root so
    _render_template can read it (the runner reads self.root / template)."""
    dst = root / relpath
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(body)


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
    """A `sudo -n bash <root>/Scripts/install.sh` re-run. install.sh-grant
    removal, 2026-07-27: the runner must NEVER emit this anymore — kept as the
    regression guard the phase tests assert against."""
    return cmd[:3] == ["sudo", "-n", "bash"] and cmd[-1].endswith("Scripts/install.sh")


def is_probe(cmd):
    return APT_HELPER in cmd and cmd[-1] == APT_PREFLIGHT_PROBE_PKG


def is_pkg_install(cmd):
    return APT_HELPER in cmd and cmd[-1] != APT_PREFLIGHT_PROBE_PKG


def is_write_systemd(cmd):
    """A bounded blackbox-write-systemd dispatch. install.sh-grant removal,
    2026-07-27 (M3): NO source arg — cmd is exactly
    ['sudo','-n',WRITE_SYSTEMD_HELPER,<kind>]. The helper copies the root-owned
    store entry to its hardcoded dest; the caller passes only the kind."""
    return WRITE_SYSTEMD_HELPER in cmd


def write_systemd_kind(cmd):
    """The target_kind arg of a write-systemd dispatch (positional: right after
    the helper path — and, since M3, the LAST arg: there is no source)."""
    i = cmd.index(WRITE_SYSTEMD_HELPER)
    return cmd[i + 1]


def no_source_in_dispatch(cmd):
    """True iff a write-systemd dispatch carries ONLY the kind (no source path).
    The M3 security invariant: the caller never hands the helper any content."""
    i = cmd.index(WRITE_SYSTEMD_HELPER)
    return cmd[i:] == [WRITE_SYSTEMD_HELPER, write_systemd_kind(cmd)]


def is_systemctl_restart(cmd, service=None):
    hit = cmd[:4] == ["sudo", "-n", "systemctl", "restart"]
    return hit and (service is None or cmd[4] == service)


def healthy(_runner, cmd):
    """Everything works: sudo available, helper reads its allowlist, apt OK,
    bounded write-systemd dispatch + aux restart succeed."""
    if cmd[:3] == ["sudo", "-n", "true"]:
        return 0, ""
    if is_probe(cmd):
        return APT_HELPER_RC_NOT_ALLOWLISTED, "package not in allowlist"
    if is_pkg_install(cmd):
        return 0, f"{cmd[-1]} installed."
    if is_write_systemd(cmd):
        return 0, f"[blackbox-write-systemd] Wrote ({write_systemd_kind(cmd)})"
    if is_systemctl_restart(cmd):
        return 0, ""
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


# ── D2: system-level regen dispatches bounded helpers, never install.sh ──

def test_regen_dispatch_runs_after_apt_phase(monkeypatch, root, resets, store_sandbox):
    """install.sh-grant removal, 2026-07-27: the regen no longer re-runs
    install.sh — it dispatches the changed template through blackbox-write-systemd.
    Phase order must stay monotonic: that dispatch happens in systemd_regen,
    AFTER every apt call (probe + installs). An earlier privileged step was tried
    and reverted (a blackbox.service restart mid-update, KillMode=process, would
    SIGTERM the uvicorn process iterating the update).

    M3: the change must be PUBLISHED to the trusted store (store == repo render)
    for the dispatch to fire — so this stages the store as the human install.sh
    would have."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    write_template(root, APT_HELPER_TEMPLATE)
    store_sandbox(_KIND_BY_TEMPLATE[APT_HELPER_TEMPLATE])  # human published it
    set_changed(monkeypatch, [APT_HELPER_TEMPLATE,
                              "Scripts/onboarding/system-packages.txt"])
    r = make(root)
    drive(r)

    assert not any(is_install_sh(c) for c in r.calls), \
        "the install.sh re-run must be gone (its sudo grant was removed)"
    regen_at = [i for i, c in enumerate(r.calls) if is_write_systemd(c)]
    apt_at = [i for i, c in enumerate(r.calls) if APT_HELPER in c]
    assert regen_at, "helpers bucket did not trigger a write-systemd dispatch"
    assert apt_at, "apt phase never ran"
    assert apt_at[-1] < regen_at[0], (
        f"write-systemd dispatched at call {regen_at[0]} but apt was still "
        f"active at {apt_at[-1]} — regen must stay AFTER the apt phase")
    # The changed template is the apt helper → its matching bounded kind.
    assert write_systemd_kind(r.calls[regen_at[0]]) == \
        _KIND_BY_TEMPLATE[APT_HELPER_TEMPLATE]
    # M3 security invariant: the dispatch carries ONLY the kind, no source path.
    assert no_source_in_dispatch(r.calls[regen_at[0]]), \
        "write-systemd dispatch leaked a source argument (M0/M3 forbids it)"


def test_apt_phase_never_dispatches_regen_early(monkeypatch, root, resets):
    """No mid-apt self-repair: a stale helper blocking a MUST_HAVE package must
    fail + report, NOT trigger an early write-systemd dispatch (or install.sh
    re-run) inside the apt phase. Any regen is systemd_regen, after apt."""
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
        "apt phase re-ran install.sh — the removed grant must never be used"
    assert not any(is_write_systemd(c) for c in r.calls), \
        "apt phase dispatched regen early — the reverted self-repair is back"


def test_changed_template_dispatches_once(monkeypatch, root, resets, store_sandbox):
    """helpers + systemd buckets both lit → the ONE changed (and PUBLISHED)
    template dispatches exactly once. install.sh-grant removal, 2026-07-27: no
    install.sh re-run at all, and no duplicate dispatch of the same bounded kind."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    write_template(root, APT_HELPER_TEMPLATE)
    store_sandbox(_KIND_BY_TEMPLATE[APT_HELPER_TEMPLATE])  # human published it
    set_changed(monkeypatch, [APT_HELPER_TEMPLATE,
                              "Scripts/install.sh",
                              "Scripts/onboarding/system-packages.txt"])
    r = make(root)
    drive(r)
    assert not any(is_install_sh(c) for c in r.calls)
    dispatched = [write_systemd_kind(c) for c in r.calls if is_write_systemd(c)]
    assert dispatched == [_KIND_BY_TEMPLATE[APT_HELPER_TEMPLATE]], \
        f"expected one apt-install-helper dispatch, got {dispatched}"


def test_no_regen_dispatch_when_only_allowlist_changed(monkeypatch, root, resets):
    """A pure apt update (only the allowlist moved) must not gain a privileged
    dispatch it never used to do — no write-systemd, no install.sh."""
    write_allowlist(root, "xvfb  # MUST_HAVE # CU virtual displays\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])
    r = make(root)
    events = drive(r)
    assert not any(is_install_sh(c) for c in r.calls)
    assert not any(is_write_systemd(c) for c in r.calls)
    assert final(events)["succeeded"] is True


# ── M3: decomposed regen — per-template dispatch precision + new-pkg policy ──

def test_changed_unit_template_dispatches_matching_kind_only(monkeypatch, root, resets, store_sandbox):
    """A changed systemd-unit template dispatches EXACTLY its matching bounded
    kind (nothing else), and restarts only that aux service — never
    blackbox.service, whose restart is the detached one after `complete`. This
    is the 'only rewrite what moved' guarantee: no blanket regen."""
    # Materialize EVERY regen template on disk so the changed-filter — not a
    # missing-file skip — is what's under test: a blanket regen would now
    # dispatch all four kinds, exposing the mutation (install.sh-grant removal,
    # 2026-07-27).
    for t in _REGEN_TARGETS:
        write_template(root, t.template)
    # Publish ONLY the zellij store entry (as the human install.sh would). M3:
    # only a change PUBLISHED to the store may dispatch — this also proves the
    # other three kinds don't dispatch even though their templates exist on disk.
    store_sandbox(_KIND_BY_TEMPLATE[ZELLIJ_UNIT_TEMPLATE])
    # zellij-web.service is not in any changes.py bucket; the regen phase still
    # enters because a dispatchable template moved. Only ONE template changed.
    set_changed(monkeypatch, [ZELLIJ_UNIT_TEMPLATE])
    r = make(root)
    events = drive(r)

    assert final(events)["succeeded"] is True
    dispatched = [write_systemd_kind(c) for c in r.calls if is_write_systemd(c)]
    assert dispatched == [_KIND_BY_TEMPLATE[ZELLIJ_UNIT_TEMPLATE]], \
        f"expected exactly the zellij-web-unit kind, got {dispatched}"
    # Restarts the aux unit via its own grant …
    assert any(is_systemctl_restart(c, "zellij-web.service") for c in r.calls)
    # … and never the service iterating this update.
    assert not any(is_systemctl_restart(c, "blackbox.service") for c in r.calls), \
        "regen restarted blackbox.service — that SIGTERMs the running update"
    assert not any(is_install_sh(c) for c in r.calls)


# ── M3: store-staleness — a changed-but-unpublished template must NOT falsely
#        claim success (the exact gap the M0 review flagged) ──────────────────

def test_changed_but_unpublished_template_surfaces_remedy_not_dispatch(
        monkeypatch, root, resets, store_sandbox):
    """The store is human-`install.sh`-only. If a commit CHANGES a privileged
    template but the human has not re-published the store, the runner must NOT
    dispatch write-systemd (which would copy the STALE store back and log a false
    "Regenerated" success — the store-staleness bug). It must surface the
    `sudo bash install.sh` remedy and let the rest of the update complete."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    write_template(root, APT_HELPER_TEMPLATE)
    # Deliberately do NOT publish the store entry for this kind.
    set_changed(monkeypatch, [APT_HELPER_TEMPLATE,
                              "Scripts/onboarding/system-packages.txt"])
    r = make(root)
    events = drive(r)

    # No dispatch, no install.sh re-run: the stale store was never applied.
    assert not any(is_write_systemd(c) for c in r.calls), \
        "dispatched a STALE store back — the store-staleness false-success bug"
    assert not any(is_install_sh(c) for c in r.calls)
    # The privileged change did not silently vanish: the operator remedy is named.
    remedy = [t for t in logs(events)
              if "NOT" in t and f"sudo bash {root}/Scripts/install.sh" in t]
    assert remedy, "no actionable 'run sudo bash install.sh' remedy was surfaced"
    # The rest of the update (code, pip) still completes — this is not a rollback.
    assert final(events)["succeeded"] is True
    assert r.pre_update_tag not in resets, \
        "an unpublished privileged change must not roll the whole update back"


def test_stale_store_differing_from_repo_render_surfaces_remedy(
        monkeypatch, root, resets, store_sandbox):
    """Store entry PRESENT but byte-differing from the repo render (the human ran
    an OLDER install.sh): still unpublished for THIS commit. Must not dispatch,
    must surface the remedy — dispatching would re-assert the old store."""
    write_allowlist(root, "novnc  # SHOULD_HAVE # CU live-view browser client\n")
    write_template(root, APT_HELPER_TEMPLATE)  # rendered == fixture body
    store_sandbox(_KIND_BY_TEMPLATE[APT_HELPER_TEMPLATE],
                  body="# a DIFFERENT, older published version\n")
    set_changed(monkeypatch, [APT_HELPER_TEMPLATE,
                              "Scripts/onboarding/system-packages.txt"])
    r = make(root)
    events = drive(r)

    assert not any(is_write_systemd(c) for c in r.calls), \
        "dispatched despite repo != store — would re-assert the stale store"
    remedy = [t for t in logs(events) if "NOT" in t and "install.sh" in t]
    assert remedy, "stale store did not surface the operator remedy"
    assert final(events)["succeeded"] is True


def test_published_store_matching_repo_render_dispatches_and_heals(
        monkeypatch, root, resets, store_sandbox):
    """Positive control: store == repo render (human published this commit) →
    dispatch the matching kind exactly once, with NO source, and no remedy noise."""
    write_template(root, ZELLIJ_UNIT_TEMPLATE)
    store_sandbox(_KIND_BY_TEMPLATE[ZELLIJ_UNIT_TEMPLATE])  # published, == render
    set_changed(monkeypatch, [ZELLIJ_UNIT_TEMPLATE])
    r = make(root)
    events = drive(r)

    dispatched = [c for c in r.calls if is_write_systemd(c)]
    assert [write_systemd_kind(c) for c in dispatched] == \
        [_KIND_BY_TEMPLATE[ZELLIJ_UNIT_TEMPLATE]]
    assert all(no_source_in_dispatch(c) for c in dispatched)
    assert not any("NOT applied" in t for t in logs(events)), \
        "a PUBLISHED change wrongly surfaced the unpublished-store remedy"
    assert final(events)["succeeded"] is True


def test_new_must_have_not_in_allowlist_reports_remedy_and_rolls_back(
        monkeypatch, root, resets):
    """install.sh-grant removal, 2026-07-27: the apt helper now trusts only the
    root-owned /etc allowlist. A commit adding a NEW MUST_HAVE package therefore
    reaches the helper as rc=5 (not allowlisted) until an operator refreshes /etc.
    That must fail the phase with the actionable 'run sudo bash install.sh once'
    remedy AND roll the code back — not a bare rc, not a silent continue."""
    write_allowlist(root, "brand-new-pkg  # MUST_HAVE # newly required dep\n")
    set_changed(monkeypatch, ["Scripts/onboarding/system-packages.txt"])

    def not_allowlisted(_r, cmd):
        if is_pkg_install(cmd):
            return APT_HELPER_RC_NOT_ALLOWLISTED, "package not in allowlist"
        return healthy(_r, cmd)

    r = make(root, not_allowlisted)
    events = drive(r)
    err = final(events)["error"]

    assert final(events)["succeeded"] is False
    assert final(events)["failed_phase"] == "apt_install"
    assert r.pre_update_tag in resets, "new MUST_HAVE failure did not roll back"
    assert f"sudo bash {root}/Scripts/install.sh" in err, \
        "remedy did not name the one operator command"
    assert "/etc/blackbox/system-packages.txt" in err, \
        "remedy did not name the root-owned allowlist"
    assert "brand-new-pkg" in err, "remedy did not name the blocked package"


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
