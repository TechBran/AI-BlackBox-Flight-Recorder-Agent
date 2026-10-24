"""Orchestrator/utils/atomic_json.py: the one atomic, locked, corrupt-safe
store helper every small runtime store goes through (audit rank 47)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from Orchestrator.utils import atomic_json
from Orchestrator.utils.atomic_json import CorruptStoreError, UnreadableStoreError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _quarantined(path: Path):
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def test_round_trip_is_owner_only_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "store.json"
    atomic_json.write_json(path, {"a": 1})
    assert atomic_json.read_json(path, {}) == {"a": 1}
    assert _mode(path) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_missing_file_yields_a_copy_of_the_default(tmp_path):
    default = {"servers": []}
    got = atomic_json.read_json(tmp_path / "nope.json", default)
    got["servers"].append("x")
    assert default == {"servers": []}


def test_corrupt_file_is_quarantined_on_read_not_left_to_be_overwritten(tmp_path):
    path = tmp_path / "store.json"
    path.write_bytes(b'{"half": ')
    assert atomic_json.read_json(path, {}) == {}
    assert not path.exists()
    (moved,) = _quarantined(path)
    assert moved.read_bytes() == b'{"half": '


def test_wrong_shape_counts_as_corrupt(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("[1, 2]")
    assert atomic_json.read_json(path, {}, valid=lambda d: isinstance(d, dict)) == {}
    (moved,) = _quarantined(path)
    assert moved.read_text() == "[1, 2]"


def test_a_reader_never_quarantines_a_file_a_writer_just_replaced(tmp_path, monkeypatch):
    """A reader holds the corrupt bytes it read; a writer then moves them
    aside and writes good data; the reader's quarantine must not take the
    writer's good file with it (review of rank 47: the live store vanished)."""
    path = tmp_path / "store.json"
    path.write_bytes(b'{"truncated": ')
    real_parse = atomic_json._parse
    state = {"interleaved": False}

    def parse(raw):
        if raw == b'{"truncated": ' and not state["interleaved"]:
            state["interleaved"] = True
            # The writer lands between the reader's read and its quarantine.
            atomic_json.write_json(path, {"good": "written after the corrupt file was moved"})
        return real_parse(raw)

    monkeypatch.setattr(atomic_json, "_parse", parse)
    got = atomic_json.read_json(path, {})
    assert state["interleaved"]
    assert got == {"good": "written after the corrupt file was moved"}
    assert json.loads(path.read_text()) == {"good": "written after the corrupt file was moved"}
    (moved,) = _quarantined(path)                    # only the corrupt bytes
    assert moved.read_bytes() == b'{"truncated": '


def test_a_reader_quarantines_under_the_store_lock(tmp_path):
    path = tmp_path / "store.json"
    path.write_bytes(b"{not json")
    done = threading.Event()
    with atomic_json.locked(path):
        reader = threading.Thread(target=lambda: (atomic_json.read_json(path, {}), done.set()))
        reader.start()
        assert not done.wait(0.3), "the reader moved the store aside without its lock"
        assert path.exists()
    reader.join(5)
    assert done.is_set() and not path.exists() and len(_quarantined(path)) == 1


def test_a_reader_can_leave_a_corrupt_file_to_the_locked_writer(tmp_path):
    path = tmp_path / "store.json"
    path.write_bytes(b"{not json")
    assert atomic_json.read_json(path, {"d": 1}, quarantine=False) == {"d": 1}
    assert path.read_bytes() == b"{not json" and _quarantined(path) == []
    with atomic_json.update_json(path, {}) as data:   # the writer still moves it aside
        data["x"] = 1
    assert json.loads(path.read_text()) == {"x": 1}
    (moved,) = _quarantined(path)
    assert moved.read_bytes() == b"{not json"


def test_write_over_a_corrupt_file_quarantines_it_first(tmp_path):
    path = tmp_path / "store.json"
    path.write_bytes(b"\x00\x01 truncated")
    atomic_json.write_json(path, {"fresh": True})
    assert json.loads(path.read_text()) == {"fresh": True}
    (moved,) = _quarantined(path)
    assert moved.read_bytes() == b"\x00\x01 truncated"


def test_write_refuses_when_a_corrupt_file_cannot_be_moved_aside(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    path.write_bytes(b"{not json")

    def no_rename(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(atomic_json.os, "rename", no_rename)
    with pytest.raises(CorruptStoreError):
        atomic_json.write_json(path, {"fresh": True})
    assert path.read_bytes() == b"{not json"


def test_update_refuses_when_a_corrupt_file_cannot_be_moved_aside(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    path.write_bytes(b"{not json")
    monkeypatch.setattr(atomic_json.os, "rename",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    with pytest.raises(CorruptStoreError):
        with atomic_json.update_json(path, {}) as data:
            data["x"] = 1
    assert path.read_bytes() == b"{not json"


def test_quarantine_never_replaces_an_earlier_quarantined_copy(tmp_path):
    path = tmp_path / "store.json"
    for n in range(3):
        path.write_text(f"bad {n}")
        atomic_json.read_json(path, {})
    assert sorted(p.read_text() for p in _quarantined(path)) == ["bad 0", "bad 1", "bad 2"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_load_for_a_writer_raises_on_an_unreadable_store(tmp_path):
    path = tmp_path / "store.json"
    path.write_text('{"keep": 1}')
    path.chmod(0)
    try:
        with pytest.raises(UnreadableStoreError):
            atomic_json.load_json(path, {})
        # A reader still gets the default instead of an exception.
        assert atomic_json.read_json(path, {"d": 1}) == {"d": 1}
    finally:
        path.chmod(0o600)
    assert path.read_text() == '{"keep": 1}'


def test_unserializable_data_leaves_the_store_untouched(tmp_path):
    path = tmp_path / "store.json"
    atomic_json.write_json(path, {"ok": 1})
    with pytest.raises(TypeError):
        atomic_json.write_json(path, {"bad": object()})
    assert json.loads(path.read_text()) == {"ok": 1}


def test_update_writes_on_clean_exit_only(tmp_path):
    path = tmp_path / "store.json"
    atomic_json.write_json(path, {"n": 1})
    with pytest.raises(RuntimeError):
        with atomic_json.update_json(path, {}) as data:
            data["n"] = 99
            raise RuntimeError("caller bailed")
    assert json.loads(path.read_text()) == {"n": 1}
    with atomic_json.update_json(path, {}) as data:
        data["n"] = 2
    assert json.loads(path.read_text()) == {"n": 2}


def test_unchanged_update_does_not_create_the_store(tmp_path):
    path = tmp_path / "store.json"
    with atomic_json.update_json(path, {}) as data:
        assert data == {}
    assert not path.exists()


def test_mode_none_keeps_the_existing_mode(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{}")
    path.chmod(0o644)
    atomic_json.write_json(path, {"a": 1}, mode=None)
    assert _mode(path) == 0o644
    fresh = tmp_path / "fresh.json"
    atomic_json.write_json(fresh, {}, mode=None)
    assert _mode(fresh) == 0o600


def test_write_through_a_symlink_keeps_the_link(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    atomic_json.write_json(link, {"via": "link"})
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"via": "link"}


def test_concurrent_updates_from_threads_lose_nothing(tmp_path):
    path = tmp_path / "store.json"
    threads, per_thread = 8, 25

    def worker(tid):
        for i in range(per_thread):
            with atomic_json.update_json(path, {}) as data:
                data[f"{tid}-{i}"] = i

    pool = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert len(json.loads(path.read_text())) == threads * per_thread


def test_the_lock_is_reentrant(tmp_path):
    path = tmp_path / "store.json"
    with atomic_json.locked(path):
        with atomic_json.update_json(path, {}) as data:
            data["inner"] = True
    assert json.loads(path.read_text()) == {"inner": True}


_CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from Orchestrator.utils import atomic_json
for i in range(int(sys.argv[3])):
    with atomic_json.update_json(sys.argv[2], {}, cross_process=True) as data:
        data["count"] = data.get("count", 0) + 1
"""


def test_concurrent_updates_from_processes_lose_nothing(tmp_path):
    """The MCP server and the CLI token minter share stores with this process:
    cross_process=True serializes them through the flock sidecar."""
    path = tmp_path / "store.json"
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(REPO_ROOT), str(path), "40"])
             for _ in range(4)]
    for p in procs:
        assert p.wait(timeout=60) == 0
    assert json.loads(path.read_text()) == {"count": 160}


def test_a_single_process_store_leaves_no_lock_file(tmp_path):
    path = tmp_path / "store.json"
    with atomic_json.update_json(path, {}) as data:
        data["a"] = 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["store.json"]


def test_stdlib_only():
    """The MCP server's lean venv imports this module."""
    src = Path(atomic_json.__file__).read_text()
    for line in src.splitlines():
        if line.startswith(("import ", "from ")) and "__future__" not in line:
            module = line.split()[1].split(".")[0]
            assert module in sys.stdlib_module_names, line
