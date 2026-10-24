"""A snapshot carries only the media that landed since the last one (rank 49 review).

perform_mint attaches collect_pending_media_artifacts(operator) to the new
snapshot's index entry. That used to be the operator's WHOLE completed-media
history, so every mint re-stored every artifact the operator ever made, and
the index (rewritten in full on every mint) grew quadratically: on the dev box
84% of a 17 MB snapshot_index.json was repeated media_artifacts. Attributing
chat media to the requesting operator (audit rank 49) moved that media onto
the operators who mint most often, so the growth would have sped up.

Pending now means "not attached to any snapshot yet": each artifact rides
exactly one snapshot, the first mint after its task completes (an async video
that finishes after a mint rides the next one), and a rebuilt index keeps the
attachments it had so nothing is re-attached.

Hermetic: a fresh TaskDatabase and snapshot index under tmp_path; the mint's
ledger, embedding and archive steps are stubs.
"""
import json

import pytest

from Orchestrator import fossils, models
from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType

OP = "op-mint-media"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import Orchestrator.tasks as tasks

    database = TaskDatabase(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(models, "task_db", database)
    monkeypatch.setattr(tasks, "task_db", database)
    index_path = tmp_path / "snapshot_index.json"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "SCHEMA_VERSION_FILE", tmp_path / "schema_version.json")
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    return tasks, database, index_path


def _media(db, n, *, operator=OP, status=TaskStatus.COMPLETED,
           task_type=TaskType.IMAGE_GENERATION):
    stamp = f"2026-10-01T00:{n // 60 % 60:02d}:{n % 60:02d}Z"
    task_id = f"media-{operator}-{n}"
    db.save_task(Task(
        task_id=task_id, task_type=task_type, status=status,
        created_at=stamp, updated_at=stamp, prompt=f"prompt {n}",
        result_url=f"/ui/uploads/{n}.png", operator=operator,
        result_data={"artifact": {"type": "image", "url": f"/ui/uploads/{n}.png",
                                  "prompt": f"prompt {n}"}}))
    return task_id


def _complete(db, task_id):
    task = db.get_task(task_id)
    task.status = TaskStatus.COMPLETED
    db.save_task(task)


def _index_mint(n, artifacts, operator=OP):
    fossils.update_snapshot_index(
        f"SNAP-20261001-{n:04d}", n * 100, n * 100 + 99, operator,
        f"2026-10-01T01:00:{n:02d}Z", media_artifacts=artifacts or None)


def _ids(artifacts):
    return [a["task_id"] for a in artifacts]


def test_an_attached_artifact_is_not_collected_again(env):
    tasks, db, _ = env
    first = [_media(db, n) for n in (1, 2, 3)]

    pending = tasks.collect_pending_media_artifacts(OP)
    assert sorted(_ids(pending)) == sorted(first)
    _index_mint(1, pending)

    assert tasks.collect_pending_media_artifacts(OP) == []

    new = _media(db, 4)
    assert _ids(tasks.collect_pending_media_artifacts(OP)) == [new]


def test_media_that_finishes_after_a_mint_rides_the_next_one(env):
    tasks, db, _ = env
    done = _media(db, 1)
    video = _media(db, 2, status=TaskStatus.PROCESSING, task_type=TaskType.VIDEO_GENERATION)

    pending = tasks.collect_pending_media_artifacts(OP)
    assert _ids(pending) == [done]
    _index_mint(1, pending)

    _complete(db, video)
    assert _ids(tasks.collect_pending_media_artifacts(OP)) == [video]


def test_repeated_mints_store_each_artifact_once(env):
    tasks, db, index_path = env
    for n in range(1, 21):
        _media(db, n)
        _index_mint(n, tasks.collect_pending_media_artifacts(OP))

    index = json.loads(index_path.read_text(encoding="utf-8"))
    stored = [a["task_id"] for e in index.values() for a in e.get("media_artifacts", [])]
    # One artifact per mint: 20 rows, not the 210 (1 + 2 + ... + 20) the
    # whole-history list stored.
    assert len(stored) == 20
    assert len(set(stored)) == 20


def test_media_already_on_a_legacy_entry_is_not_attached_again(env):
    """An index written before this fix carries whole-history lists: none of
    those artifacts may come back, whichever entry holds them."""
    tasks, db, _ = env
    legacy = [{"task_id": _media(db, n), "url": f"/ui/uploads/{n}.png"} for n in (1, 2)]
    _index_mint(1, legacy[:1])
    _index_mint(2, legacy)

    new = _media(db, 3)
    assert _ids(tasks.collect_pending_media_artifacts(OP)) == [new]


def test_other_operators_media_is_still_never_collected(env):
    tasks, db, _ = env
    _media(db, 1, operator="op-mint-other")
    mine = _media(db, 2)
    assert _ids(tasks.collect_pending_media_artifacts(OP)) == [mine]


def test_a_rebuilt_index_keeps_its_media_attachments(env, tmp_path, monkeypatch):
    tasks, db, index_path = env
    attached = _media(db, 1)
    snap = "SNAP-20261001-0001"
    body = (f"=== START SNAPSHOT — UTC 2026-10-01T01:00:01Z — {snap} ===\n"
            f"OPERATOR: {OP}\nturn text\n"
            f"=== END SNAPSHOT — {snap} ===\n").encode("utf-8")
    volume = tmp_path / "SNAPSHOT_VOLUME.txt"
    volume.write_bytes(body)
    monkeypatch.setattr(fossils, "VOL_PATH", volume)
    fossils.update_snapshot_index(snap, 0, len(body), OP, "2026-10-01T01:00:01Z",
                                  media_artifacts=tasks.collect_pending_media_artifacts(OP))

    rebuilt = fossils.rebuild_snapshot_index()

    assert _ids(rebuilt[snap]["media_artifacts"]) == [attached]
    assert tasks.collect_pending_media_artifacts(OP) == []


def test_perform_mint_attaches_only_the_new_media(env, tmp_path, monkeypatch):
    """End to end through perform_mint: the second snapshot's index entry
    carries the one image made after the first mint, not both."""
    import Orchestrator.checkpoint as ckpt

    tasks, db, index_path = env
    snap_ids = iter(["SNAP-20261001-0001", "SNAP-20261001-0002"])
    monkeypatch.setattr(ckpt, "verify_gm_or_halt", lambda: None)
    monkeypatch.setattr(ckpt, "read_text_safe", lambda _p: "")
    monkeypatch.setattr(ckpt, "parse_tail",
                        lambda _t: {"tail_id": "SNAP-20261001-0000", "utc": "2026-10-01T00:00:00Z"})
    monkeypatch.setattr(ckpt, "next_snap_id_from_tail", lambda _t: next(snap_ids))
    monkeypatch.setattr(ckpt, "append_snapshot_text", lambda body: None)
    monkeypatch.setattr(ckpt, "VOL_PATH", tmp_path / "no-such-volume.txt")
    monkeypatch.setattr(ckpt, "_embed_for_index", lambda *a, **k: {})
    monkeypatch.setattr(ckpt, "archive_volume", lambda: ("/tmp/arc.txt", "deadbeef", "utc"))
    monkeypatch.setattr(ckpt, "save_operator_state", lambda *a, **k: None)

    first = _media(db, 1)
    ckpt.perform_mint(OP, reason="TEST")
    second = _media(db, 2)
    ckpt.perform_mint(OP, reason="TEST")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert _ids(index["SNAP-20261001-0001"]["media_artifacts"]) == [first]
    assert _ids(index["SNAP-20261001-0002"]["media_artifacts"]) == [second]
