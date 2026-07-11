"""MN.3 — notification subscription REST API tests (TestClient).

Endpoints:
  POST   /notifications/subscribe              upsert (USERS_LIST-gated)
  GET    /notifications/subscriptions?device_id=   one device's sub (or 404)
  GET    /notifications/subscriptions          admin list (all devices)
  DELETE /notifications/subscriptions/{device_id}  unsubscribe
  POST   /notifications/send                   drive notify() for non-Python callers

The store is redirected to a tmp file so the suite never writes the real
Manifest/. The bus's device fan-out is mocked so /send runs fully offline.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Importing Orchestrator.app registers every route onto the shared app.
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from Orchestrator.checkpoint import app

    # Redirect the subscription store to a throwaway file.
    import Orchestrator.notifications.subscriptions as subs_mod

    monkeypatch.setattr(
        subs_mod, "DEFAULT_SUBS_FILE", tmp_path / "device_notification_subs.json"
    )

    # Constrain USERS_LIST so validation is deterministic regardless of box config.
    import Orchestrator.routes.notification_routes as nroutes

    monkeypatch.setattr(nroutes, "USERS_LIST", ["Brandon", "Casey"])

    return TestClient(app)


def test_subscribe_unknown_operator_422(client):
    """An operator not in USERS_LIST (and not 'all') → 422."""
    resp = client.post(
        "/notifications/subscribe",
        json={"device_id": "d-1", "operators": ["Nobody"]},
    )
    assert resp.status_code == 422, resp.text


def test_subscribe_known_operator_upserts_and_get(client):
    """A known operator upserts; GET by device_id returns the row."""
    resp = client.post(
        "/notifications/subscribe",
        json={
            "device_id": "d-1",
            "operators": ["Brandon"],
            "tailnet_name": "brandon-fold6",
            "device_kind": "android",
            "display_name": "Fold",
        },
    )
    assert resp.status_code == 200, resp.text

    got = client.get("/notifications/subscriptions", params={"device_id": "d-1"})
    assert got.status_code == 200, got.text
    row = got.json()
    assert row["all"] is False
    assert row["operators"] == ["Brandon"]
    assert row["tailnet_name"] == "brandon-fold6"
    assert row["device_kind"] == "android"


def test_subscribe_all_sentinel_maps_to_all_true(client):
    """The literal 'all' sentinel maps to all=True (no per-operator list)."""
    resp = client.post(
        "/notifications/subscribe",
        json={"device_id": "d-all", "operators": ["all"]},
    )
    assert resp.status_code == 200, resp.text

    row = client.get(
        "/notifications/subscriptions", params={"device_id": "d-all"}
    ).json()
    assert row["all"] is True
    assert row["operators"] == []


def test_get_unknown_device_404(client):
    """GET for a device with no subscription → 404."""
    resp = client.get("/notifications/subscriptions", params={"device_id": "ghost"})
    assert resp.status_code == 404


def test_admin_list_returns_all_devices(client):
    """GET without device_id → the admin device→operators map."""
    client.post(
        "/notifications/subscribe", json={"device_id": "d-1", "operators": ["Brandon"]}
    )
    client.post(
        "/notifications/subscribe", json={"device_id": "d-2", "operators": ["all"]}
    )
    resp = client.get("/notifications/subscriptions")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"d-1", "d-2"}
    assert data["d-2"]["all"] is True


def test_delete_unsubscribes(client):
    """DELETE removes the device; subsequent GET is 404."""
    client.post(
        "/notifications/subscribe", json={"device_id": "d-1", "operators": ["Brandon"]}
    )
    resp = client.delete("/notifications/subscriptions/d-1")
    assert resp.status_code == 200, resp.text
    assert client.get(
        "/notifications/subscriptions", params={"device_id": "d-1"}
    ).status_code == 404


def test_send_returns_result_without_minting(client, monkeypatch):
    """POST /notifications/send drives notify() and returns the NotifyResult.

    Notifications are transient (2026-07-11): the wire shape keeps `recorded`
    for compat but it is ALWAYS False, and no snapshot is minted — the tripwire
    on checkpoint.mint_with_content must stay uncalled.
    """
    import Orchestrator.checkpoint as checkpoint_mod
    import Orchestrator.notifications.bus as bus_mod

    monkeypatch.setattr(bus_mod, "reachable_subscribers", lambda operator: [])
    mints = []
    monkeypatch.setattr(
        checkpoint_mod, "mint_with_content",
        lambda *a, **k: mints.append(a) or {"snap_id": "SNAP-TEST"},
    )

    resp = client.post(
        "/notifications/send",
        json={"operator": "Brandon", "title": "Hi", "body": "There", "category": "test"},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["recorded"] is False  # transient — never minted
    assert out["delivered"] == []
    assert out["notif_id"]
    assert mints == []  # the ledger stayed untouched
