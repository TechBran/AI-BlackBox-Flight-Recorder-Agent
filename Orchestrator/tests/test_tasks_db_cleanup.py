"""F2 — tasks.db cleanup: created_at index, /tasks/list projection, and the
base64-media relocation migration.

Pins:
  * Part 1 — idx_tasks_created_at makes `ORDER BY created_at DESC` stop using a
    TEMP B-TREE sort.
  * Part 2 — get_task_list / list_tasks project the pill fields (incl. device_id
    from its real column) and do NOT return the result_data base64 blob.
  * Part 3 — relocate_reference_images is lossless / non-destructive / idempotent,
    and the migration relocates every inline blob, backfills device_id, preserves
    the row count, and shrinks the DB — without losing an artifact.
"""
import base64
import json
import os
import sqlite3

import pytest

from Orchestrator.media_relocation import relocate_reference_images, REFIMAGES_SUBDIR
from Orchestrator.migrations import relocate_reference_images as migration
from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
from Orchestrator.routes import task_routes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _columns(db_path: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    finally:
        conn.close()


def _query_plan(db_path: str, sql: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return " | ".join(str(r) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    finally:
        conn.close()


def _mk(task_id, **kw) -> Task:
    now = "2026-07-10T00:00:00Z"
    base = dict(
        task_id=task_id,
        task_type=TaskType.IMAGE_GENERATION,
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
        operator="system",
    )
    base.update(kw)
    return Task(**base)


PNG_BYTES = base64.b64decode(
    # 1x1 transparent PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


# ---------------------------------------------------------------------------
# Part 1 — created_at index
# ---------------------------------------------------------------------------
def test_created_at_index_created_and_used(tmp_path):
    db = TaskDatabase(str(tmp_path / "t.db"))
    assert "idx_tasks_created_at" in {
        r[1] for r in sqlite3.connect(db.db_path).execute("PRAGMA index_list(tasks)")
    }
    plan = _query_plan(
        db.db_path,
        "SELECT task_id FROM tasks ORDER BY created_at DESC",
    )
    # The whole point: no temp-b-tree sort, and the created_at index is used.
    assert "TEMP B-TREE" not in plan.upper(), plan
    assert "idx_tasks_created_at" in plan, plan


def test_index_absent_would_sort(tmp_path):
    """Sanity/mutation guard: without the index the SAME query DOES temp-sort —
    proving the assertion above is meaningful, not vacuous."""
    db_path = str(tmp_path / "noidx.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    plan = _query_plan(db_path, "SELECT task_id FROM tasks ORDER BY created_at DESC")
    assert "TEMP B-TREE" in plan.upper(), plan


# ---------------------------------------------------------------------------
# Part 2 — projection + device_id column
# ---------------------------------------------------------------------------
def test_device_id_column_mirrors_result_data(tmp_path):
    db = TaskDatabase(str(tmp_path / "t.db"))
    assert "device_id" in _columns(db.db_path)
    # save_task derives the column from result_data['device_id'] with no writer change.
    db.save_task(_mk("d1", task_type=TaskType.USE_COMPUTER,
                      result_data={"device_id": "pixel-9", "x": 1}))
    row = sqlite3.connect(db.db_path).execute(
        "SELECT device_id FROM tasks WHERE task_id='d1'").fetchone()
    assert row[0] == "pixel-9"
    # round-trips onto the Task object too.
    assert db.get_task("d1").device_id == "pixel-9"


def test_get_task_list_projects_fields_without_result_data(tmp_path):
    db = TaskDatabase(str(tmp_path / "t.db"))
    big = "Q" * 2_000_000  # a stand-in for a base64 blob
    db.save_task(_mk("p1", task_type=TaskType.USE_COMPUTER,
                     prompt="x" * 300,
                     progress_text="step 3/10",
                     result_data={"device_id": "pixel-9", "options": {"blob": big}}))
    rows = db.get_task_list()
    assert len(rows) == 1
    r = rows[0]
    # Needed fields present, incl. device_id from the column.
    for k in ("task_id", "task_type", "status", "progress", "created_at",
              "updated_at", "result_url", "operator", "prompt",
              "progress_text", "device_id"):
        assert k in r, k
    assert r["device_id"] == "pixel-9"
    assert r["progress_text"] == "step 3/10"
    # The projection must NOT carry result_data / the blob.
    assert "result_data" not in r
    assert big not in json.dumps(r)


def test_list_tasks_endpoint_shape_additive(tmp_path, monkeypatch):
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(task_routes, "task_db", db)
    db.save_task(_mk("a1", task_type=TaskType.USE_COMPUTER, status=TaskStatus.PROCESSING,
                     result_data={"device_id": "pixel-9", "big": "Z" * 500000},
                     progress_text="step 1/9"))
    db.save_task(_mk("a2", task_type=TaskType.USE_COMPUTER, status=TaskStatus.PROCESSING,
                     result_data=None))

    resp = task_routes.list_tasks(all=True)
    by_id = {x["task_id"]: x for x in resp["tasks"]}
    assert by_id["a1"]["device_id"] == "pixel-9"
    assert by_id["a1"]["progress_text"] == "step 1/9"
    # Default preserves historical semantics.
    assert by_id["a2"]["device_id"] == "blackbox"
    # No blob leaks into the list response.
    assert "Z" * 500000 not in json.dumps(resp)
    # Existing keys still present (additive, not a rename).
    for k in ("status", "progress", "result_url", "operator", "created_at"):
        assert k in by_id["a1"]
    assert resp["total_tasks"] == 2


def test_tasks_status_still_returns_result_data(tmp_path, monkeypatch):
    """/tasks/status is a single-row get-by-PK — it must keep returning full
    result_data (incl. device_id inside it), unchanged."""
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(task_routes, "task_db", db)
    db.save_task(_mk("s1", task_type=TaskType.USE_COMPUTER,
                     result_data={"device_id": "blackbox", "detail": "keep"}))
    resp = task_routes.get_task_status("s1")
    assert resp["result_data"]["device_id"] == "blackbox"
    assert resp["result_data"]["detail"] == "keep"


# ---------------------------------------------------------------------------
# Part 3 — relocate_reference_images helper
# ---------------------------------------------------------------------------
def test_relocate_helper_lossless_and_replaces_data(tmp_path):
    rd = {"options": {"aspectRatio": "16:9",
                      "referenceImages": [
                          {"data": PNG_B64, "mimeType": "image/png"},
                          {"data": PNG_B64, "mimeType": "image/jpeg"},
                      ]}}
    new_rd, n = relocate_reference_images(rd, "task-xyz", str(tmp_path))
    assert n == 2
    refs = new_rd["options"]["referenceImages"]
    for i, ref in enumerate(refs):
        assert "data" not in ref                       # inline base64 gone
        assert ref["url"].startswith(f"/ui/uploads/{REFIMAGES_SUBDIR}/task-xyz_ref{i}")
        assert ref["relocated"] is True
        # LOSSLESS: the on-disk bytes equal the original decoded artifact.
        fname = ref["url"].split("/")[-1]
        on_disk = (tmp_path / REFIMAGES_SUBDIR / fname).read_bytes()
        assert on_disk == PNG_BYTES
    # extension from mimeType
    assert refs[0]["url"].endswith(".png")
    assert refs[1]["url"].endswith(".jpg")
    # Input dict not mutated.
    assert rd["options"]["referenceImages"][0]["data"] == PNG_B64


def test_process_image_generation_strips_base64_going_forward(tmp_path, monkeypatch):
    """Going-forward path: a NEW completed image task must NOT persist inline
    referenceImages base64 in result_data — process_image_generation relocates it
    to disk before saving. This is what keeps the DB slim after the one-shot
    migration (so it never re-bloats)."""
    from Orchestrator import tasks as tasks_mod

    db = TaskDatabase(str(tmp_path / "t.db"))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tasks_mod, "task_db", db)
    monkeypatch.setattr(tasks_mod, "UPLOADS_DIR", uploads)
    monkeypatch.setattr(tasks_mod, "add_media_entry", lambda **kw: None)
    monkeypatch.setattr(tasks_mod, "_default_image_provider", lambda: "gemini")
    monkeypatch.setattr(tasks_mod, "IMAGE_PROVIDERS", {"gemini": lambda prompt, opts: [PNG_BYTES]})

    task = _mk(
        "gen-1",
        task_type=TaskType.IMAGE_GENERATION,
        status=TaskStatus.PROCESSING,
        prompt="a neon sign",
        result_data={"options": {"numberOfImages": 1,
                                  "referenceImages": [{"data": PNG_B64, "mimeType": "image/png"}]}},
    )
    db.save_task(task)
    tasks_mod.process_image_generation(task)

    saved = db.get_task("gen-1")
    assert saved.status == TaskStatus.COMPLETED
    ref = saved.result_data["options"]["referenceImages"][0]
    # base64 relocated to disk, replaced by a served URL reference.
    assert "data" not in ref
    assert ref["url"].startswith(f"/ui/uploads/{REFIMAGES_SUBDIR}/gen-1_ref0")
    assert (uploads / REFIMAGES_SUBDIR / ref["url"].split("/")[-1]).read_bytes() == PNG_BYTES
    # The generated OUTPUT is still referenced (unaffected by the strip).
    assert saved.result_url and saved.result_url.startswith("/ui/uploads/")
    # No large inline base64 remains in the stored row.
    assert PNG_B64 not in json.dumps(saved.result_data)


def test_relocate_helper_idempotent_and_noop_paths(tmp_path):
    # Already relocated (no 'data') -> untouched, count 0.
    rd = {"options": {"referenceImages": [{"url": "/ui/uploads/refimages/x.png",
                                           "relocated": True}]}}
    out, n = relocate_reference_images(rd, "t", str(tmp_path))
    assert n == 0 and out is rd
    # No options / no refs / non-dict -> pass through untouched.
    for junk in ({}, {"options": {}}, {"options": {"referenceImages": []}}, None, "x"):
        out, n = relocate_reference_images(junk, "t", str(tmp_path))
        assert n == 0 and out is junk


def test_relocate_helper_keeps_data_on_decode_failure(tmp_path):
    """NON-DESTRUCTIVE: an entry whose base64 cannot be decoded to real bytes keeps
    its inline data rather than being dropped (never lose an artifact)."""
    rd = {"options": {"referenceImages": [{"data": "!!!not-base64!!!",
                                           "mimeType": "image/png"}]}}
    out, n = relocate_reference_images(rd, "t", str(tmp_path))
    # base64 of "!!!..." may partially decode; the guarantee we assert is: if it is
    # NOT relocated, the inline data survives; if it IS, a real file exists.
    ref = out["options"]["referenceImages"][0]
    if n == 0:
        assert ref.get("data") == "!!!not-base64!!!"
    else:
        assert (tmp_path / REFIMAGES_SUBDIR / ref["url"].split("/")[-1]).exists()


# ---------------------------------------------------------------------------
# Part 3 — the migration end-to-end on a pre-F2 DB
# ---------------------------------------------------------------------------
def _build_pre_f2_db(db_path: str):
    """A pre-F2 tasks.db: 15 columns (through progress_text), NO device_id column,
    NO created_at index, with inline base64 reference images and a CU row carrying
    device_id inside result_data."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, prompt TEXT,
            input_file TEXT, result_url TEXT, result_data TEXT, error_message TEXT,
            progress INTEGER DEFAULT 0, operator TEXT, image_data TEXT,
            google_video_uri TEXT, progress_text TEXT
        )"""
    )
    # Two image rows with inline base64 reference-image INPUTS.
    for tid in ("img-1", "img-2"):
        rd = {"options": {"aspectRatio": "16:9",
                          "referenceImages": [{"data": PNG_B64, "mimeType": "image/png"}]},
              "all_urls": [f"/ui/uploads/out_{tid}.png"],
              "artifact": {"type": "image", "url": f"/ui/uploads/out_{tid}.png"}}
        conn.execute(
            "INSERT INTO tasks (task_id, task_type, status, created_at, updated_at, "
            "result_url, result_data, progress) VALUES (?,?,?,?,?,?,?,?)",
            (tid, "image_generation", "completed",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
             f"/ui/uploads/out_{tid}.png", json.dumps(rd), 100),
        )
    # A CU row with device_id inside result_data (to test backfill).
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_at, updated_at, "
        "result_data, progress) VALUES (?,?,?,?,?,?,?)",
        ("cu-1", "use_computer", "completed",
         "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
         json.dumps({"device_id": "samsung-fold", "steps": 3}), 100),
    )
    # A plain chat row (no options) — must be left untouched.
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_at, updated_at, "
        "prompt, progress) VALUES (?,?,?,?,?,?,?)",
        ("chat-1", "chat", "completed",
         "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z", "hi", 100),
    )
    conn.commit()
    conn.close()


def test_migration_relocates_backfills_and_preserves(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    _build_pre_f2_db(db_path)

    assert "device_id" not in _columns(db_path)   # pre-F2

    summary = migration.run(db_path, str(uploads), dry_run=False)

    # Row count preserved — NOT ONE row lost.
    assert summary["row_count_before"] == summary["row_count_after"] == 4
    assert summary["rows_relocated"] == 2
    assert summary["images_relocated"] == 2
    assert summary["device_id_backfilled"] == 1

    # Schema brought up to F2.
    assert "device_id" in _columns(db_path)
    idx = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA index_list(tasks)")}
    assert "idx_tasks_created_at" in idx

    conn = sqlite3.connect(db_path)
    # device_id backfilled from result_data for the CU row.
    assert conn.execute(
        "SELECT device_id FROM tasks WHERE task_id='cu-1'").fetchone()[0] == "samsung-fold"

    # The image rows: inline base64 GONE, url reference present, and the relocated
    # file on disk equals the original artifact bytes (LOSSLESS).
    for tid in ("img-1", "img-2"):
        rd = json.loads(conn.execute(
            "SELECT result_data FROM tasks WHERE task_id=?", (tid,)).fetchone()[0])
        ref = rd["options"]["referenceImages"][0]
        assert "data" not in ref
        assert ref["url"].startswith(f"/ui/uploads/{REFIMAGES_SUBDIR}/{tid}_ref0")
        fname = ref["url"].split("/")[-1]
        assert (uploads / REFIMAGES_SUBDIR / fname).read_bytes() == PNG_BYTES
        # Non-relocated fields preserved.
        assert rd["all_urls"] == [f"/ui/uploads/out_{tid}.png"]

    # The chat row is untouched.
    chat_rd = conn.execute(
        "SELECT result_data FROM tasks WHERE task_id='chat-1'").fetchone()[0]
    assert chat_rd is None
    conn.close()

    # No inline base64 blob remains anywhere in result_data.
    conn = sqlite3.connect(db_path)
    total_blob = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(result_data)),0) FROM tasks").fetchone()[0]
    conn.close()
    assert PNG_B64 not in _dump_all_result_data(db_path)
    # The DB no longer stores the (2x) base64 — result_data is now tiny.
    assert total_blob < 2000


def _dump_all_result_data(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return " ".join(
            r[0] or "" for r in conn.execute("SELECT result_data FROM tasks"))
    finally:
        conn.close()


def test_migration_idempotent(tmp_path):
    """Re-running the migration is a safe no-op (0 further relocations)."""
    db_path = str(tmp_path / "tasks.db")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    _build_pre_f2_db(db_path)

    first = migration.run(db_path, str(uploads))
    assert first["images_relocated"] == 2
    second = migration.run(db_path, str(uploads))
    assert second["images_relocated"] == 0
    assert second["rows_relocated"] == 0
    assert second["row_count_before"] == second["row_count_after"] == 4


def test_migration_dry_run_changes_nothing(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    _build_pre_f2_db(db_path)

    before = _dump_all_result_data(db_path)
    summary = migration.run(db_path, str(uploads), dry_run=True)
    assert summary["images_relocated"] == 2   # reports what WOULD happen
    # ...but the data is unchanged and no files were written.
    assert _dump_all_result_data(db_path) == before
    assert not (uploads / REFIMAGES_SUBDIR).exists()
