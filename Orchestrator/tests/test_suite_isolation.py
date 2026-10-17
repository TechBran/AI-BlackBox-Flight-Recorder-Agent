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
