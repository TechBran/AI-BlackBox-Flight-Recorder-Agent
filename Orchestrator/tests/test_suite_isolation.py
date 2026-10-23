"""The suite runs sealed off from this box (root conftest.py + app_client).

Hostile checks for audit O21-python-tests-01/-02: no test may hold the box's
real credentials, reach the network or the live service, load the serving
tree's .env, or write a route's state into the checkout's stores.
"""
import os
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_loopback_servers_still_work():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


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
    contacts, .env, onboarding, operator and local-model download state and
    the media index live in a sandbox, so a worker or route call in any test
    cannot reach the box's."""
    from Orchestrator import contacts, local_stack, media_index, models
    from Orchestrator import state as operator_state
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
    }
    in_checkout = {name: str(p) for name, p in stores.items()
                   if Path(p).resolve().is_relative_to(REPO_ROOT)}
    assert in_checkout == {}
