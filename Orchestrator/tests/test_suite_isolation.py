"""The suite runs sealed off from this box (root conftest.py + app_client).

Hostile checks for audit O21-python-tests-01/-02: no test may hold the box's
real credentials, reach the network or the live service, load the serving
tree's .env, or write a route's state into the checkout's stores.
"""
import asyncio
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _root_conftest(request):
    """The repo-root conftest.py module, as pytest loaded it."""
    path = str(REPO_ROOT / "conftest.py")
    for plugin in request.config.pluginmanager.get_plugins():
        if getattr(plugin, "__file__", None) == path:
            return plugin
    raise AssertionError("the root conftest.py is not loaded")


class _Item(SimpleNamespace):
    """Just enough of a pytest item for the root conftest's hooks."""

    def get_closest_marker(self, name):
        return SimpleNamespace(name=name) if name in self.markers else None


# ── env guard (root conftest.py) ─────────────────────────────────────────────

def test_no_credential_shaped_variable_survives():
    leaked = [name for name in os.environ
              if name.endswith(("_API_KEY", "_SECRET", "_PASSWORD", "_TOKEN", "_ACCOUNT_SID"))
              or name == "GOOGLE_APPLICATION_CREDENTIALS"]
    assert leaked == []


def test_bare_load_dotenv_never_walks_into_another_env_file(tmp_path, monkeypatch):
    """config.py's bare load_dotenv() used to find the serving tree's .env from
    a nested worktree; an explicit path still loads."""
    import dotenv

    monkeypatch.delenv("U13_DOTENV_PROBE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("U13_DOTENV_PROBE=from-file\n")
    monkeypatch.chdir(tmp_path)
    assert dotenv.load_dotenv() is False
    assert "U13_DOTENV_PROBE" not in os.environ
    assert dotenv.load_dotenv(env_file) is True
    assert os.environ["U13_DOTENV_PROBE"] == "from-file"


def test_gcloud_application_default_credentials_are_hidden():
    assert os.environ.get("NO_GCE_CHECK") == "true"
    assert not Path(os.environ["CLOUDSDK_CONFIG"]).exists()


def test_outbound_connect_is_refused():
    # 192.0.2.1 is TEST-NET-1: without the guard this would hang to the timeout.
    with pytest.raises(OSError, match="network access is blocked"):
        socket.create_connection(("192.0.2.1", 443), timeout=0.5)


def test_dns_lookup_is_refused():
    with pytest.raises(OSError, match="network access is blocked"):
        socket.getaddrinfo("api.example.com", 443)


@pytest.mark.parametrize("port", [9091, 9093])
def test_the_live_service_ports_are_refused(port):
    with pytest.raises(OSError, match="live service"):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)


def test_a_datagram_off_the_box_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="network access is blocked"):
            sock.sendto(b"x", ("192.0.2.1", 9))


def test_gethostbyname_is_refused():
    with pytest.raises(OSError, match="network access is blocked"):
        socket.gethostbyname("api.example.com")


def test_a_raw_socket_connect_is_refused():
    """_socket.socket has no Python-level connect to patch; the guard is an
    audit hook, so it still sees it."""
    import _socket

    raw = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    raw.settimeout(0.5)
    try:
        with pytest.raises(OSError, match="network access is blocked"):
            raw.connect_ex(("192.0.2.1", 443))
    finally:
        raw.close()


# The box's own loopback services (AMI could place a call, llama-swap loads a
# model); on this box they listen, so without the guard these connect.
@pytest.mark.parametrize("port", [9097, 9098, 11434, 5037, 5038, 8088, 631, 53])
def test_the_box_loopback_services_are_refused(port):
    with pytest.raises(OSError, match="live service"):
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()


def test_probe_live_items_run_with_the_network_open(request, checkout_guard):
    """The voice probes (diagnostics/voice_probes, only ever selected by name)
    call the providers by design: the documented command must not fail on the
    network guard. Every other item runs with it closed."""
    root = _root_conftest(request)
    seen = {}
    for name, markers in (("probe", {"probe_live"}), ("plain", set())):
        run = root.pytest_runtest_protocol(_Item(markers=markers), None)
        next(run)
        seen[name] = root._network_guard.open
        with pytest.raises(StopIteration):
            run.send(None)
        assert root._network_guard.open is False
    checkout_guard.active = True  # the nested run switched it off for this test
    assert seen == {"probe": True, "plain": False}


@pytest.mark.parametrize("marker", ["live_store", "live_service"])
def test_live_tests_skip_without_the_opt_in(request, marker):
    """Selected by name but run without BLACKBOX_TEST_LIVE=1, a live test
    would fail on the isolation (no key, no network), not on what it checks."""
    root = _root_conftest(request)
    with pytest.raises(pytest.skip.Exception, match="BLACKBOX_TEST_LIVE=1"):
        root.pytest_runtest_setup(_Item(markers={marker}))
    root.pytest_runtest_setup(_Item(markers=set()))


def test_loopback_servers_still_work():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


# ── host commands (root conftest.py) ────────────────────────────────────────
#
# A test used to wake this box's monitor through xset on every run, and the
# MCP status test ran `sudo -n tailscale funnel status`. The programs that act
# on the box itself are refused as if they were not installed. Harmless argv
# below, since without the guard they run.

def _installed(program):
    return pytest.mark.skipif(shutil.which(program) is None, reason=f"{program} not installed")


@pytest.mark.parametrize("argv", [
    pytest.param(["sudo", "--version"], marks=_installed("sudo")),
    pytest.param(["systemctl", "--version"], marks=_installed("systemctl")),
    pytest.param(["tailscale", "version"], marks=_installed("tailscale")),
    pytest.param(["xset", "q"], marks=_installed("xset")),
])
def test_host_commands_are_refused(argv):
    with pytest.raises(FileNotFoundError, match="host command is blocked"):
        subprocess.run(argv, capture_output=True, timeout=10)


@_installed("systemctl")
def test_host_commands_are_refused_through_a_shell_and_os_system():
    with pytest.raises(FileNotFoundError, match="host command is blocked"):
        subprocess.run("SYSTEMD_PAGER= systemctl --version", shell=True,
                       capture_output=True, timeout=10)
    with pytest.raises(FileNotFoundError, match="host command is blocked"):
        os.system("systemctl --version >/dev/null")
    with pytest.raises(FileNotFoundError, match="host command is blocked"):
        subprocess.run([shutil.which("systemctl"), "--version"], capture_output=True, timeout=10)


@_installed("systemctl")
def test_host_commands_are_refused_from_asyncio():
    async def spawn():
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--version", stdout=asyncio.subprocess.DEVNULL)
        await proc.wait()

    with pytest.raises(FileNotFoundError, match="host command is blocked"):
        asyncio.run(spawn())


def test_a_fake_host_command_on_path_under_tmp_still_runs(tmp_path):
    fake = tmp_path / "bin" / "systemctl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho fake-systemctl\n")
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    out = subprocess.run(["systemctl", "status"], env=env, capture_output=True,
                         text=True, timeout=10)
    assert out.stdout.strip() == "fake-systemctl"
    assert subprocess.run([str(fake)], capture_output=True, text=True,
                          timeout=10).stdout.strip() == "fake-systemctl"


def test_ordinary_programs_still_run():
    assert subprocess.run(["true"], timeout=10).returncode == 0


# ── app_client store isolation (Orchestrator/tests/conftest.py) ──────────────

def test_app_client_points_every_store_into_tmp(app_client, isolated_stores, tmp_path):
    from Orchestrator import contacts, models
    from Orchestrator.device_registry import pairing, registry
    from Orchestrator.scheduler import manager
    from Orchestrator.sms import message_store

    stores = [models.task_db.db_path, contacts.CONTACTS_FILE, registry.DEVICES_FILE,
              pairing.PAIRING_PATH, manager.DB_PATH, message_store.DB_PATH]
    outside = [str(p) for p in stores if not Path(p).resolve().is_relative_to(tmp_path)]
    assert outside == []
    assert Path(os.getcwd()).resolve() == isolated_stores.root.resolve()


def test_a_route_write_lands_in_tmp_not_in_the_checkout(app_client, isolated_stores):
    checkout_book = REPO_ROOT / "Contacts" / "contacts.json"
    before = checkout_book.read_bytes() if checkout_book.exists() else None

    resp = app_client.post("/contacts", json={"operator": "isolation-op",
                                              "name": "Isolation Probe"})
    assert resp.status_code == 200, resp.text

    assert "Isolation Probe" in (isolated_stores.root / "Contacts" / "contacts.json").read_text()
    after = checkout_book.read_bytes() if checkout_book.exists() else None
    assert after == before


def test_app_client_task_db_starts_empty(app_client):
    resp = app_client.get("/tasks/status/task-that-does-not-exist")
    assert resp.status_code == 404


# ── no test writes into the checkout (root conftest.py write guard) ──────────
#
# Run from the serving tree, the suite used to rewrite that box's state: the
# telephony wizard test generated a phone PIN into its .env (the credential
# strip leaves the gate unconfigured), the onboarding tests rewrote
# .onboarding_state.json, a re-embed test replaced ToolVault/embeddings.json,
# and the worker tests wrote task rows into data/tasks.db. A write into the
# checkout is now refused when it is attempted, and fails the test.

_PROBE = ".u13-checkout-write-probe"


def _cleanup(path):
    # Only finds something on a tree without the guard, where the write went
    # through.
    if path.exists():
        path.unlink()


def test_writing_a_file_into_the_checkout_is_refused(request):
    probe = REPO_ROOT / _PROBE
    try:
        with pytest.raises(OSError, match="into the checkout"):
            probe.write_text("x")
        assert not probe.exists()
    finally:
        _cleanup(probe)
    refused = request.getfixturevalue("checkout_guard").take()
    assert [v.path for v in refused] == [str(probe)]


def test_replacing_a_checkout_file_is_refused(request, tmp_path):
    staged = tmp_path / "embeddings.json"
    staged.write_text("{}")
    target = REPO_ROOT / _PROBE
    try:
        with pytest.raises(OSError, match="into the checkout"):
            os.replace(staged, target)
        assert staged.exists() and not target.exists()
    finally:
        _cleanup(target)
    assert request.getfixturevalue("checkout_guard").take()


def test_deleting_a_checkout_file_is_refused(request):
    # A name that does not exist: without the guard this is FileNotFoundError.
    with pytest.raises(OSError, match="into the checkout"):
        os.remove(REPO_ROOT / "pytest.ini.u13-never-there")
    assert request.getfixturevalue("checkout_guard").take()


def test_opening_a_sqlite_database_in_the_checkout_is_refused(request):
    import sqlite3

    db = REPO_ROOT / (_PROBE + ".db")
    try:
        with pytest.raises(OSError, match="into the checkout"):
            sqlite3.connect(db).close()
        assert not db.exists()
    finally:
        _cleanup(db)
    assert request.getfixturevalue("checkout_guard").take()


def test_a_refused_write_the_code_swallows_is_still_recorded(checkout_guard):
    """Product code that catches OSError (best-effort writes) hides the
    refusal; the guard keeps the record, and its teardown fails the test."""
    probe = REPO_ROOT / _PROBE
    try:
        probe.write_text("x")
    except OSError:
        pass
    refused = checkout_guard.take()
    assert [v.path for v in refused] == [str(probe)]
    assert "test_suite_isolation.py" in refused[0].where


def test_a_sqlite_uri_that_can_write_is_refused(checkout_guard):
    db = REPO_ROOT / (_PROBE + ".db")
    try:
        with pytest.raises(OSError, match="into the checkout"):
            sqlite3.connect(f"file:{db}?mode=rwc", uri=True).close()
        assert not db.exists()
    finally:
        _cleanup(db)
    assert checkout_guard.take()
    # A read-only URI is how a test reads a store: not a write.
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute("select 1")
    assert checkout_guard.take() == []


def test_making_a_directory_in_the_checkout_is_refused(checkout_guard):
    probe = REPO_ROOT / (_PROBE + "-dir")
    try:
        with pytest.raises(OSError, match="into the checkout"):
            os.mkdir(probe)
        assert not probe.exists()
    finally:
        if probe.is_dir():
            probe.rmdir()
    assert checkout_guard.take()
    # mkdir(exist_ok=True) of a directory the tree already has writes nothing.
    (REPO_ROOT / "Orchestrator").mkdir(exist_ok=True)
    assert checkout_guard.take() == []


def test_changing_a_checkout_file_mode_or_times_is_refused(checkout_guard):
    target = REPO_ROOT / "pytest.ini"
    st = target.stat()
    # The file's own mode and times: nothing changes even without the guard.
    with pytest.raises(OSError, match="into the checkout"):
        os.chmod(target, stat.S_IMODE(st.st_mode))
    with pytest.raises(OSError, match="into the checkout"):
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert len(checkout_guard.take()) == 2


def test_writes_outside_the_checkout_and_bytecode_caches_are_allowed(checkout_guard, tmp_path):
    (tmp_path / "ok.txt").write_text("ok")
    cache = REPO_ROOT / "Orchestrator" / "tests" / "__pycache__"
    cache.mkdir(exist_ok=True)
    probe = cache / (_PROBE + ".pyc")
    probe.write_bytes(b"")
    probe.unlink()
    assert checkout_guard.take() == []


# ── the box's stores are off the checkout for EVERY test (session sandbox) ───

def test_box_stores_point_off_the_checkout_without_app_client():
    """Not only app_client tests: for the whole session the task DB, cron DB,
    contacts, .env, onboarding, operator and local-model download state, the
    media index and the telephony gateway registry live in a sandbox, so a
    worker or route call in any test cannot reach the box's."""
    from Orchestrator import contacts, local_stack, media_index, models
    from Orchestrator import state as operator_state
    from Orchestrator.asterisk import config as asterisk_config
    from Orchestrator.asterisk import gateway_manager
    from Orchestrator.onboarding import secrets_writer, state
    from Orchestrator.scheduler import manager

    stores = {
        "task_db": models.task_db.db_path,
        "cron": manager.DB_PATH,
        "contacts": contacts.CONTACTS_FILE,
        ".env": secrets_writer.ENV_FILE,
        "onboarding": state.STATE_FILE,
        "onboarding_complete": state.COMPLETE_SENTINEL,
        "downloads": local_stack.DOWNLOAD_STATE_PATH,
        "operator_state": operator_state.OPERATOR_STATE_FILE,
        "media_index": media_index.MEDIA_INDEX_FILE,
        # /asterisk/call resolves its trunk here, and load_gateways re-saves a
        # record it migrates (audit O11-telephony-asterisk-cellular-07 fix).
        "gateways": gateway_manager.GATEWAYS_FILE,
        "gateways (contacts' reader)": asterisk_config.GATEWAYS_FILE,
    }
    in_checkout = {name: str(p) for name, p in stores.items()
                   if Path(p).resolve().is_relative_to(REPO_ROOT)}
    assert in_checkout == {}


# ── every test runs against the checkout it belongs to ───────────────────────

_ABSOLUTE_CWD = re.compile(r"""cwd\s*=\s*[rbu]?["']/""")


def test_no_test_pins_an_absolute_working_directory():
    """A test that runs a tool in a fixed absolute directory checks that box's
    tree, not the one under test, and errors on CI where the path does not
    exist (test_stt_no_hardcoded_whisper1 grepped the serving tree)."""
    roots = ["Orchestrator", "tests", "MCP"]
    offenders = []
    for root in roots:
        for path in (REPO_ROOT / root).rglob("test_*.py"):
            if "venv" in path.parts or "node_modules" in path.parts:
                continue
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if _ABSOLUTE_CWD.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert offenders == []


# ── no live voice session carries over (root conftest.py) ────────────────────

def test_the_isolated_registries_are_the_ones_the_reaper_sweeps(request):
    """A registry the reaper sweeps but the root conftest leaves alone would
    let one test's disconnected voice session reach another test's
    reap_all() (test_voice_transcript_spool counted five strangers on CI)."""
    from Orchestrator import live_session_reaper, models
    root = _root_conftest(request)
    swept = {id(registry) for registry, _ in live_session_reaper._registries().values()}
    isolated = {id(getattr(models, name)) for name in root._LIVE_SESSION_REGISTRIES}
    assert isolated == swept
    assert "_no_live_voice_session_carries_over" in request.fixturenames


def test_a_live_session_a_test_leaves_behind_does_not_reach_the_next(request):
    from Orchestrator.models import (
        GROK_LIVE_SESSIONS, REALTIME_SESSIONS, GrokLiveSession, RealtimeSession)
    root = _root_conftest(request)
    outer = GrokLiveSession(session_id="iso-outer")
    GROK_LIVE_SESSIONS["iso-outer"] = outer   # held before the "test" began
    try:
        with root._live_voice_sessions_isolated():
            assert GROK_LIVE_SESSIONS == {} and REALTIME_SESSIONS == {}
            REALTIME_SESSIONS["iso-left-behind"] = RealtimeSession(
                session_id="iso-left-behind", status="disconnected")
        assert "iso-left-behind" not in REALTIME_SESSIONS
        assert GROK_LIVE_SESSIONS == {"iso-outer": outer}
    finally:
        GROK_LIVE_SESSIONS.pop("iso-outer", None)
