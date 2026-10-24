"""GET /sms/messages serves the newest window, with cursor paging (audit O12-messaging-03).

The route served the OLDEST page of a thread (oldest-first LIMIT/OFFSET), and the
Portal and Android both asked for one page of 100 and never paged: on a long
thread neither ever showed a recent message. The default is now the newest
`limit` messages (still returned oldest-first, as the clients render them), and
`before_id` pages back from there. The read stays side-effect free: it never
marks the thread read (clients POST /sms/mark-read).

Real app (app_client: lifespan entered, side-effect hooks off, stores in
tmp_path) over a real MessageStore; the gateway manager is a stub, no SMS is
ever sent.
"""
from __future__ import annotations

import pytest

PHONE = "+14105550001"


class _Manager:
    def default(self):
        return object()

    def get(self, gateway_id):
        return None


@pytest.fixture
def sms_store(app_client, monkeypatch):
    import Orchestrator.sms as sms_pkg
    from Orchestrator.sms import message_store as msm

    store = msm.MessageStore()   # DB_PATH already points into tmp_path (isolated_stores)
    monkeypatch.setattr(sms_pkg, "_manager", _Manager())
    monkeypatch.setattr(sms_pkg, "_message_store", store)
    return store


def _fill(store, n):
    for i in range(n):
        store.store_message(operator="Ana", direction="inbound", phone_number=PHONE,
                            contact_name="Pat", body=f"m{i:03d}",
                            timestamp=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00")


def _get(client, **params):
    resp = client.get("/sms/messages", params={"operator": "Ana", "phone": PHONE, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_default_page_is_the_newest_messages_oldest_first(app_client, sms_store):
    _fill(sms_store, 130)
    out = _get(app_client, limit=100)
    bodies = [m["body"] for m in out["messages"]]
    assert bodies == [f"m{i:03d}" for i in range(30, 130)]
    assert out["has_more"] is True
    assert out["next_before_id"] == out["messages"][0]["id"]

    default = _get(app_client)
    assert [m["body"] for m in default["messages"]] == [f"m{i:03d}" for i in range(80, 130)]


def test_before_id_pages_back_to_the_start(app_client, sms_store):
    _fill(sms_store, 130)
    first = _get(app_client, limit=100)
    older = _get(app_client, limit=100, before_id=first["next_before_id"])
    assert [m["body"] for m in older["messages"]] == [f"m{i:03d}" for i in range(30)]
    assert older["has_more"] is False
    assert older["next_before_id"] is None


def test_a_short_thread_is_one_page(app_client, sms_store):
    _fill(sms_store, 3)
    out = _get(app_client, limit=100)
    assert [m["body"] for m in out["messages"]] == ["m000", "m001", "m002"]
    assert out["has_more"] is False and out["next_before_id"] is None
    assert "gateway_recvtime" in out["messages"][0] and "line_number" in out["messages"][0]


def test_reading_a_thread_leaves_it_unread(app_client, sms_store):
    _fill(sms_store, 4)
    _get(app_client, limit=100)
    unread = app_client.get("/sms/unread", params={"operator": "Ana"}).json()
    assert unread["unread"] == 4


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"before_id": 0}, {"offset": -1}])
def test_out_of_range_paging_is_refused(app_client, sms_store, params):
    resp = app_client.get("/sms/messages", params={"operator": "Ana", "phone": PHONE, **params})
    assert resp.status_code == 422


def test_threads_list_one_row_per_phone(app_client, sms_store):
    same = "2026-10-01T12:00:00+00:00"
    for body in ("burst one", "burst two"):
        sms_store.store_message(operator="Ana", direction="inbound", phone_number=PHONE,
                                contact_name="Pat", body=body, timestamp=same)
    threads = app_client.get("/sms/threads", params={"operator": "Ana"}).json()["threads"]
    assert [t["phone_number"] for t in threads] == ["4105550001"]
    assert threads[0]["last_message"] == "burst two"
