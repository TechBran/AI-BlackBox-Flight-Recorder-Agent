from ugv_tools_api.supervisor.handle_store import HandleStore


def test_set_and_get(tmp_path):
    p = tmp_path / "session_handle.txt"
    s = HandleStore(p)
    assert s.get() is None
    s.set("handle-abc-123")
    assert s.get() == "handle-abc-123"
    # Survives new instance (simulates restart)
    s2 = HandleStore(p)
    assert s2.get() == "handle-abc-123"


def test_clear(tmp_path):
    p = tmp_path / "session_handle.txt"
    s = HandleStore(p)
    s.set("x")
    s.clear()
    assert s.get() is None


def test_creates_parent_dir(tmp_path):
    p = tmp_path / "sub" / "dir" / "session_handle.txt"
    s = HandleStore(p)
    s.set("y")
    assert p.exists()


def test_empty_file_returns_none(tmp_path):
    p = tmp_path / "session_handle.txt"
    p.write_text("")
    assert HandleStore(p).get() is None


def test_set_overwrites(tmp_path):
    s = HandleStore(tmp_path / "h.txt")
    s.set("first")
    s.set("second")
    assert s.get() == "second"
