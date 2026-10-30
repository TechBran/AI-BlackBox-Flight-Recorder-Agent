"""Stop & erase (spec §3.6): the DB and its -wal/-shm are gone, the key is
revoked, the phone is told to unbind, the enrollment is removed, the tasks.db
rows listed in task_ref are scrubbed, disconnect keeps or deletes history as
chosen, and DELETE /operator/{name} runs the same erase."""
from __future__ import annotations

import json

import pytest

from Orchestrator.tests.wellness_support import (OPERATOR, close_box, fixtures, make_box)
from Orchestrator.wellness import enroll, erase, paths

FX = fixtures()


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    b = make_box(isolated_stores, monkeypatch)
    r = b.enroll()
    assert r.status_code == 200
    b.enr = r.json()["enrollment_id"]
    batch = dict(FX.load("days_10")["batches"][0], enrollment_id=b.enr, epoch=1)
    assert b.push(batch).status_code == 200
    yield b
    close_box(b)


def _files(op_key):
    db = paths.db_path(op_key)
    return [p for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
            if p.exists()]


def test_erase_needs_the_phone(box):
    assert box.post("/wellness/erase", {"operator": OPERATOR}).status_code == 401
    assert enroll.get_enrollment(OPERATOR) is not None


def test_erase_removes_everything(box):
    rec = enroll.get_enrollment(OPERATOR)
    key, enr = box.phone.key, box.phone.enrollment_id
    assert _files(rec["op_key"])
    r = box.post("/wellness/erase", {"operator": OPERATOR}, signed=True)
    assert r.status_code == 200 and r.json() == {"state": "not_set_up"}
    assert _files(rec["op_key"]) == []
    assert enroll.get_enrollment(OPERATOR) is None
    assert box.enr not in json.loads(paths.keys_path().read_text())
    assert ("POST", "/wellness/unbind") in [(m, p) for m, p, _ in box.phone.calls]
    assert box.get(f"/wellness/status?operator={OPERATOR}").json()["state"] == "not_set_up"
    box.phone.key, box.phone.enrollment_id = key, enr
    r = box.push(dict(FX.load("days_10")["batches"][1], enrollment_id=enr, epoch=1))
    assert r.status_code == 410 and r.json() == {"error": "revoked"}


def test_disconnect_keep_history_keeps_the_store(box):
    rec = enroll.get_enrollment(OPERATOR)
    r = box.post("/wellness/disconnect", {"operator": OPERATOR, "keep_history": True})
    assert r.status_code == 200 and r.json() == {"state": "revoked"}
    assert _files(rec["op_key"])
    assert enroll.get_enrollment(OPERATOR)["state"] == "revoked"
    again = box.enroll()
    assert again.status_code == 200 and again.json()["epoch"] == 2
    assert enroll.get_enrollment(OPERATOR)["op_key"] == rec["op_key"]


def test_disconnect_delete_history_needs_the_phone_and_erases(box):
    rec = enroll.get_enrollment(OPERATOR)
    assert box.post("/wellness/disconnect", {"operator": OPERATOR, "keep_history": False}
                    ).status_code == 401
    r = box.post("/wellness/disconnect", {"operator": OPERATOR, "keep_history": False},
                 signed=True)
    assert r.status_code == 200 and r.json() == {"state": "not_set_up"}
    assert _files(rec["op_key"]) == []


def test_disconnect_has_no_default(box):
    r = box.post("/wellness/disconnect", {"operator": OPERATOR})
    assert r.status_code == 422


def test_task_ref_rows_are_scrubbed(box):
    from Orchestrator import models
    rec = enroll.get_enrollment(OPERATOR)
    conn = models.task_db._connect()
    try:
        for tid, op in (("task-w1", OPERATOR), ("task-keep", OPERATOR), ("task-w2", "other")):
            conn.execute("INSERT INTO tasks(task_id, task_type, status, created_at, updated_at, "
                         "operator) VALUES (?, 'chat', 'done', 'x', 'x', ?)", (tid, op))
        conn.commit()
    finally:
        conn.close()
    st = enroll.store_for(rec)
    st.add_task_ref("task-w1", now=1)
    st.add_task_ref("task-w2", now=2)        # another operator's id is never touched
    out = erase.erase_operator(OPERATOR)
    assert out["erased"] and out["tasks"] == 1
    conn = models.task_db._connect()
    try:
        left = {r[0] for r in conn.execute("SELECT task_id FROM tasks")}
    finally:
        conn.close()
    assert {"task-keep", "task-w2"} <= left and "task-w1" not in left


def test_operator_delete_erases_their_health(box, isolated_stores):
    from Orchestrator.utils.config_ini import update_config_ini

    def roster(p):
        if not p.has_section("users"):
            p.add_section("users")
        p.set("users", "list", f"{OPERATOR}, other-human")
        p.set("users", "default", "other-human")
    update_config_ini(roster)
    rec = enroll.get_enrollment(OPERATOR)
    r = box.client.delete(f"/operator/{OPERATOR}")
    assert r.status_code == 200 and r.json()["status"] == "removed"
    assert enroll.get_enrollment(OPERATOR) is None
    assert _files(rec["op_key"]) == []
    assert ("POST", "/wellness/unbind") in [(m, p) for m, p, _ in box.phone.calls]


def test_erase_is_idempotent_and_never_raises_from_the_hook(box, monkeypatch):
    assert erase.erase_operator("never-enrolled") == {"erased": False, "files": 0, "tasks": 0}
    monkeypatch.setattr(erase, "erase_operator", lambda *a, **k: 1 / 0)
    erase.on_operator_deleted(OPERATOR)       # logged, not raised


def test_an_erase_that_races_a_batch_leaves_no_store(box, monkeypatch):
    """The batch's signature was checked before the erase; when it reaches
    the store the key is gone and the file unlinked, so it is refused (410)
    and no orphan store holding its rows appears."""
    from Orchestrator.wellness import ingest
    op_key = enroll.get_enrollment(OPERATOR)["op_key"]
    real = ingest.decompress
    fired = []

    def racing(raw, enc):
        if not fired:
            fired.append(1)
            erase.erase_operator(OPERATOR, reason="erase", unbind=False)
        return real(raw, enc)
    monkeypatch.setattr(ingest, "decompress", racing)
    r = box.push(dict(FX.load("days_10")["batches"][1], enrollment_id=box.enr, epoch=1))
    assert fired and r.status_code == 410 and r.json() == {"error": "revoked"}
    assert enroll.get_enrollment(OPERATOR) is None
    assert _files(op_key) == []


def test_erase_revokes_before_it_tells_the_phone(box, monkeypatch):
    """The key is gone (and the store with it) before the 5 s unbind call,
    which runs with no lock held."""
    import threading
    seen = {}

    def lock_free():
        # From another thread: the lock is reentrant for the erasing one.
        got = enroll._ENROLL_LOCK.acquire(blocking=False)
        if got:
            enroll._ENROLL_LOCK.release()
        seen["lock_free"] = got

    def unbind(rec):
        seen["key"] = enroll.key_for(rec["enrollment_id"])
        seen["files"] = _files(rec["op_key"])
        t = threading.Thread(target=lock_free)
        t.start()
        t.join()
        return 200
    monkeypatch.setattr(enroll, "unbind_phone", unbind)
    assert erase.erase_operator(OPERATOR)["erased"]
    assert seen == {"key": None, "files": [], "lock_free": True}
