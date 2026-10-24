"""Operator names are validated before they reach a file path or the ledger
(audit rank 16, X03-security-backend-06).

The operator arrives raw in the request body of /chat, /chat/stream,
/chat/save, /mint, /checkpoint, /agent/dev-snapshot, /local/turn/complete,
/artifacts/reprocess and /operator/add. artifacts.py joined it straight into
``ARTIFACTS_DIR / f"{operator}_{hex}_{filename}"``, so an operator such as
``'../../../../home/<user>/.config/autostart/x'`` wrote a caller-controlled
file anywhere the service could write; a newline forged ledger lines
(``OPERATOR: x\\nSNAP_ID: ...``) in the snapshot body; and /operator/add
persisted it into config.ini, where a comma splits the roster.

``validate_operator`` is syntactic only — no roster membership: ``system`` is
a legitimate operator for apps, MCP and background paths without being in
/operators, and /operator/add exists to create new ones. Internal spaces are
allowed ('Flight Recorder', 'B2 Tester').
"""
import asyncio
from pathlib import Path

import pytest

# Import before any test chdirs: config.py reads ./config.ini at import time,
# and every route registers on the shared app here.
import Orchestrator.app  # noqa: F401

HOSTILE = [
    "../x",
    "../../../../tmp/autostart/x",
    "a/b",
    "/abs",
    "a\\b",
    "a\x00",
    "\n",
    "Brandon\nSNAP_ID: SNAP-20990101-0001",
    "tab\tname",
    " Brandon",
    "Brandon ",
    "",
    "   ",
    ".",
    "..",
    "a..b",
    "a,b",
    "x" * 65,
    "line sep",
    "bidi‮flip",
    "nb sp",
]

VALID = ["Flight Recorder", "Brandon-DEV", "system", "B2 Tester", "Brandon-FA Tech",
         "test 2", "Anna", "José", "o'brien", "x" * 64]


# ----------------------------------------------------------- pure function

@pytest.fixture
def ops():
    from Orchestrator.utils import operators
    return operators


@pytest.mark.parametrize("bad", HOSTILE)
def test_validate_operator_refuses(ops, bad):
    with pytest.raises(ops.InvalidOperatorName):
        ops.validate_operator(bad)


@pytest.mark.parametrize("bad", [None, 7, ["Brandon"], b"Brandon"])
def test_validate_operator_refuses_non_strings(ops, bad):
    with pytest.raises(ops.InvalidOperatorName):
        ops.validate_operator(bad)


@pytest.mark.parametrize("good", VALID)
def test_validate_operator_accepts(ops, good):
    assert ops.validate_operator(good) == good


def test_invalid_operator_is_a_value_error(ops):
    assert issubclass(ops.InvalidOperatorName, ValueError)


# ------------------------------------------------------- artifact filenames

@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    import Orchestrator.artifacts as art
    root = tmp_path / "box"
    d = root / "Portal" / "artifacts"
    d.mkdir(parents=True)
    monkeypatch.setattr(art, "ARTIFACTS_DIR", d)
    return d


def _everything_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("filename", [
    "../../escape.txt",
    "../../../../tmp/autostart/x.desktop",
    "/etc/cron.d/x",
    '"><img src=x onerror=alert(1)>.txt',
    "..",
])
def test_artifact_filename_reduced_to_safe_basename(artifacts_dir, filename):
    from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
    reply = f"[ARTIFACT:{filename}:text]payload[/ARTIFACT]"
    html_out, metas = parse_and_process_artifacts_with_meta(reply, "Tester")
    root = artifacts_dir.parent.parent
    written = [p for p in root.rglob("*") if p.is_file()]
    assert written and all(p.parent == artifacts_dir for p in written), _everything_under(root)
    for meta in metas:
        assert "/" not in meta["filename"] and "\\" not in meta["filename"]
        assert "<" not in meta["filename"] and '"' not in meta["filename"]
        assert not meta["filename"].startswith(".")
    assert "<img" not in html_out


def test_artifact_operator_component_cannot_escape(artifacts_dir):
    """Defense in depth inside artifacts.py: even a caller that skips the route
    guard cannot steer the write out of ARTIFACTS_DIR."""
    from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
    parse_and_process_artifacts_with_meta("[ARTIFACT:a.txt:text]x[/ARTIFACT]", "../../../outside")
    root = artifacts_dir.parent.parent
    written = [p for p in root.parent.rglob("*") if p.is_file()]
    assert written and all(p.parent == artifacts_dir for p in written)


def test_legit_artifact_names_survive(artifacts_dir):
    from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
    _, metas = parse_and_process_artifacts_with_meta(
        "[ARTIFACT:Q3 Report-v2.pdf:text]x[/ARTIFACT]", "Flight Recorder")
    assert metas[0]["filename"] == "Q3 Report-v2.pdf"
    assert metas[0]["url"].startswith("/artifacts/Flight Recorder_")


@pytest.mark.parametrize("operator", ["Brandon-DEV", "o'brien", "José", "Flight Recorder"])
def test_artifact_download_keeps_the_original_filename(artifacts_dir, monkeypatch, operator):
    """The download name is parsed back out of the id even when the reduced
    operator prefix contains '_' (the old split('_', 2) assumed it never did)."""
    import Orchestrator.routes.admin_routes as ar
    from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
    monkeypatch.setattr(ar, "ARTIFACTS_DIR", artifacts_dir)
    _, metas = parse_and_process_artifacts_with_meta(
        "[ARTIFACT:notes_v2.txt:text]hello[/ARTIFACT]", operator)
    resp = ar.download_artifact(metas[0]["url"].rsplit("/", 1)[-1])
    assert resp.filename == "notes_v2.txt"


# ------------------------------------------------------------- /chat/save

class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def save_env(monkeypatch, artifacts_dir, tmp_path):
    """/chat/save with the REAL perform_mint over a recorded ledger append and a
    tmp ARTIFACTS_DIR, so 'nothing written' is observable."""
    import Orchestrator.checkpoint as ckpt
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks

    ledger = []
    monkeypatch.setattr(cr, "AUTO_ENABLE", True)
    monkeypatch.setattr(cr, "TURNS_THRESHOLD", 1)
    monkeypatch.setattr(cr, "DEBOUNCE_MS", 0)
    monkeypatch.setattr(cr, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(cr, "save_operator_state", lambda *a, **k: None)
    monkeypatch.setattr(ckpt, "save_operator_state", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "collect_pending_media_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(ckpt, "verify_gm_or_halt", lambda: None)
    monkeypatch.setattr(ckpt, "read_text_safe", lambda _p: "")
    monkeypatch.setattr(ckpt, "parse_tail",
                        lambda _t: {"tail_id": "SNAP-20261017-0000", "utc": "2026-10-17T00:00:00Z"})
    monkeypatch.setattr(ckpt, "next_snap_id_from_tail", lambda _t: "SNAP-20261017-0001")
    monkeypatch.setattr(ckpt, "append_snapshot_text", lambda b: ledger.append(b))
    monkeypatch.setattr(ckpt, "VOL_PATH", tmp_path / "no-such-volume.txt")
    monkeypatch.setattr(ckpt, "_embed_for_index", lambda *a, **k: {})
    monkeypatch.setattr(ckpt, "update_snapshot_index", lambda *a, **k: None)
    monkeypatch.setattr(ckpt, "archive_volume", lambda *_a, **_k: ("/tmp/arc.txt", "deadbeef", "utc"))
    return cr, ledger


def _save(cr, operator):
    return asyncio.run(cr.chat_save(_Req({
        "operator": operator,
        "user_message": "make me a file",
        "assistant_response": "here [ARTIFACT:notes.txt:text]planted[/ARTIFACT]",
        "model": "test-model",
        "tokens": {"prompt": 1, "completion": 1},
    })))


@pytest.mark.parametrize("bad", ["../../escape", "a/b", "evil\nSNAP_ID: SNAP-20990101-0001", "a\x00"])
def test_chat_save_traversal_operator_400_nothing_written(save_env, artifacts_dir, bad):
    from Orchestrator.state import state_by_op
    cr, ledger = save_env
    root = artifacts_dir.parent.parent
    before = _everything_under(root.parent)
    r = _save(cr, bad)
    assert getattr(r, "status_code", 200) == 400
    assert ledger == []
    assert bad not in state_by_op
    assert _everything_under(root.parent) == before


def test_chat_save_valid_operator_still_mints(save_env, artifacts_dir):
    cr, ledger = save_env
    r = _save(cr, "Flight Recorder")
    assert isinstance(r, dict) and r["success"] is True
    assert len(ledger) == 1 and "OPERATOR: Flight Recorder" in ledger[0]
    assert [p.parent for p in artifacts_dir.parent.parent.rglob("*") if p.is_file()] == [artifacts_dir]


def test_chat_stream_refuses_traversal_operator(monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    built = []
    monkeypatch.setattr(cr, "build_streaming_context", lambda *a, **k: built.append(a) or ([], {}, {}))
    r = asyncio.run(cr.chat_stream_post(_Req({
        "messages": [{"role": "user", "content": "hi"}],
        "provider": "openai", "model": "test", "operator": "../../x"})))
    assert getattr(r, "status_code", 200) == 400
    assert built == []


# The old GET twin of /chat/stream took ``operator`` raw from the query string,
# so switching the method skipped the POST route's check (review finding). The
# twin is gone (audit M3 U20): the POST route is the only way in.
def test_chat_stream_has_no_get_twin():
    import Orchestrator.routes.chat_routes as cr
    assert not hasattr(cr, "chat_stream")


@pytest.mark.parametrize("bad", ["../../x", "evil\n=== END SNAPSHOT ===\nOPERATOR: Brandon", "a\x00"])
def test_chat_stream_refuses_hostile_operator_for_computer_use(monkeypatch, bad):
    """With provider computer-use on a Google model the name would go into a
    Chrome-less Gemini desktop session and on to perform_mint's OPERATOR: line."""
    import Orchestrator.routes.chat_routes as cr
    built = []
    monkeypatch.setattr(cr, "build_streaming_context", lambda *a, **k: built.append(a) or ([], {}, {}))
    r = asyncio.run(cr.chat_stream_post(_Req({
        "messages": [{"role": "user", "content": "hi"}],
        "provider": "computer-use", "model": "gemini-2.5-computer-use", "operator": bad})))
    assert getattr(r, "status_code", 200) == 400
    assert built == []


@pytest.mark.parametrize("extra, expected", [
    ({}, None),                               # absent -> the box default, as before
    ({"operator": "Flight Recorder"}, "Flight Recorder"),
])
def test_chat_stream_keeps_legit_operators(monkeypatch, extra, expected):
    import Orchestrator.routes.chat_routes as cr
    built = []
    monkeypatch.setattr(cr, "build_streaming_context", lambda *a, **k: built.append(a) or ([], {}, {}))

    async def _fake_stream(*a, **k):  # hermetic: never reach the real provider
        yield {"type": "content", "data": "ok"}

    monkeypatch.setattr(cr, "stream_openai_with_reasoning", _fake_stream)
    body = {"messages": [{"role": "user", "content": "hi"}],
            "provider": "openai", "model": "test", **extra}
    async def go():
        resp = await cr.chat_stream_post(_Req(body))
        # The context builds inside the SSE generator (audit rank 45), so
        # drain the stream before looking at what it was built with.
        async for _ in resp.body_iterator:
            pass
        return resp
    r = asyncio.run(go())
    assert r.status_code == 200
    assert built[0][1] == (expected if expected is not None else cr.current_default())


# ------------------------------------------- the ledger choke point (mint)

LEDGER_FORGERY = ["evil\n=== END SNAPSHOT ===\nOPERATOR: Brandon", "../x", "a\x00b"]


@pytest.mark.parametrize("bad", LEDGER_FORGERY)
def test_mint_functions_refuse_hostile_operator(save_env, bad):
    """perform_mint / mint_with_content render the operator verbatim into the
    snapshot body: refused there too, whatever route reached them."""
    import Orchestrator.checkpoint as ckpt
    from Orchestrator.state import state_by_op
    from Orchestrator.utils.operators import InvalidOperatorName
    _, ledger = save_env
    with pytest.raises(InvalidOperatorName):
        ckpt.perform_mint(bad, "MANUAL")
    with pytest.raises(InvalidOperatorName):
        ckpt.mint_with_content(bad, "content", reason="MCP")
    assert ledger == []
    assert bad not in state_by_op


def test_mint_with_content_valid_operator_still_mints(save_env):
    import Orchestrator.checkpoint as ckpt
    _, ledger = save_env
    ckpt.mint_with_content("Flight Recorder", "content", reason="MCP")
    assert len(ledger) == 1 and "OPERATOR: Flight Recorder" in ledger[0]


def _cu_session():
    from types import SimpleNamespace
    return SimpleNamespace(usage={"prompt_tokens": 1, "completion_tokens": 1}, provenance={})


@pytest.mark.parametrize("bad", LEDGER_FORGERY)
def test_cu_save_refuses_hostile_operator(save_env, bad):
    """_cu_save_to_blackbox is the CU agent loops' in-process /chat/save."""
    from Orchestrator.state import state_by_op
    cr, ledger = save_env
    asyncio.run(cr._cu_save_to_blackbox(bad, "do it", "done", _cu_session()))
    assert ledger == []
    assert bad not in state_by_op


def test_cu_save_valid_operator_still_mints(save_env):
    cr, ledger = save_env
    asyncio.run(cr._cu_save_to_blackbox("Flight Recorder", "do it", "done", _cu_session()))
    assert len(ledger) == 1 and "OPERATOR: Flight Recorder" in ledger[0]


# ---------------------------------------------- Gemini computer-use sessions

@pytest.mark.parametrize("bad", ["x/../../../../out/pwned", "a\nb", "../x"])
def test_gemini_cu_sessions_refuse_hostile_operator(bad):
    """A Gemini desktop session never builds a ChromeInstance, so its own
    constructor is the check (the operator names its screenshot files)."""
    from Orchestrator.gemini_cu import session_manager as sm
    from Orchestrator.utils.operators import InvalidOperatorName
    with pytest.raises(InvalidOperatorName):
        sm.GeminiCUSession(bad, "blackbox", "desktop")
    with pytest.raises(InvalidOperatorName):
        sm.get_or_create_session(bad, "blackbox", "desktop")
    with pytest.raises(InvalidOperatorName):
        sm.create_task_session(bad, "blackbox", "desktop")
    assert bad not in sm._operator_sessions


def test_gemini_cu_screenshot_name_stays_in_uploads(tmp_path, monkeypatch):
    """Belt and braces: the screenshot file name reduces the operator to a safe
    basename even if a session's operator were changed after construction."""
    from Orchestrator.gemini_cu import agent_loop
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession
    from Orchestrator.utils import paths
    root = tmp_path / "box"
    uploads = root / "Portal" / "uploads"
    # The directory the old f-string needed for its first component to resolve.
    (uploads / "gemini_cu_x").mkdir(parents=True)
    monkeypatch.setenv("BLACKBOX_ROOT", str(root))
    paths.blackbox_root.cache_clear()
    try:
        s = GeminiCUSession("Flight Recorder", "blackbox", "desktop")
        assert agent_loop._save_screenshot(b"png", s) == "/ui/uploads/gemini_cu_Flight Recorder_000.png"
        s.operator = "x/../../../../out/pwned"
        url = agent_loop._save_screenshot(b"png", s)
    finally:
        monkeypatch.delenv("BLACKBOX_ROOT")
        paths.blackbox_root.cache_clear()
    assert "/" not in url[len("/ui/uploads/"):]
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 2 and all(p.parent == uploads for p in written), written


# ------------------------------------------------------- HTTP edge routes

@pytest.fixture
def client():
    import Orchestrator.app  # noqa: F401  (registers every route on the shared app)
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


def test_chat_route_refuses_traversal_operator(client, monkeypatch):
    import Orchestrator.config as cfg
    import Orchestrator.routes.chat_routes as cr
    created = []
    monkeypatch.setattr(cr, "create_task", lambda *a, **k: created.append(k))
    monkeypatch.setattr(cfg, "CURRENT_OPERATOR", cfg.CURRENT_OPERATOR)
    before = cfg.current_operator()
    r = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}],
                                   "operator": "../../x"})
    assert r.status_code == 400
    assert created == []
    assert cfg.current_operator() == before


def test_artifacts_reprocess_refuses_traversal_operator(client, artifacts_dir):
    root = artifacts_dir.parent.parent
    before = _everything_under(root.parent)
    r = client.post("/artifacts/reprocess", json={
        "content": "[ARTIFACT:x.txt:text]planted[/ARTIFACT]", "operator": "../../../escape"})
    assert r.status_code == 400
    assert _everything_under(root.parent) == before


@pytest.mark.parametrize("route, body", [
    ("/mint", {"content": "hello", "operator": "../x"}),
    ("/checkpoint", {"operator": "a/b"}),
    ("/agent/dev-snapshot", {"content": "x" * 80, "operator": "bad\nSNAP_ID: SNAP-1"}),
])
def test_ledger_routes_refuse_hostile_operator(client, monkeypatch, route, body):
    import Orchestrator.checkpoint as ckpt
    import Orchestrator.routes.admin_routes as ar
    minted = []
    monkeypatch.setattr(ckpt, "mint_with_content", lambda *a, **k: minted.append(a) or {})
    monkeypatch.setattr(ckpt, "perform_mint", lambda *a, **k: minted.append(a) or {})
    monkeypatch.setattr(ar, "perform_mint", lambda *a, **k: minted.append(a) or {})
    monkeypatch.setattr(ar, "create_checkpoint_async", lambda *a, **k: minted.append(a))
    r = client.post(route, json=body)
    assert r.status_code == 400, r.text
    assert minted == []


def test_local_turn_complete_refuses_hostile_operator(client, monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    called = []

    async def _persist(*a, **k):
        called.append(a)
        return {}

    monkeypatch.setattr(cr, "persist_local_turn_and_mint", _persist)
    r = client.post("/local/turn/complete", json={
        "turn_id": "t1", "operator": "../x", "prompt": "p", "final_response": "r"})
    assert r.status_code == 400
    assert called == []


# ---------------------------------------------------------- /operator/add

@pytest.fixture
def roster(tmp_path, monkeypatch):
    """Isolated like test_operator_roster_live: config.ini writes land in tmp,
    shared roster state is snapshotted and restored."""
    import Orchestrator.config as cfg
    import Orchestrator.routes.admin_routes as ar
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    real_config = Path.cwd() / "config.ini"
    real_bytes = real_config.read_bytes()
    snap_list = list(cfg.USERS_LIST)
    snap_cfg_list = cfg.CFG.get("users", "list", fallback="")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ar, "USERS_LIST", cfg.USERS_LIST)
    cfg.USERS_LIST[:] = ["Brandon"]
    if not cfg.CFG.has_section("users"):
        cfg.CFG.add_section("users")
    cfg.CFG.set("users", "list", "Brandon")

    app = FastAPI()
    app.add_api_route("/operator/add", ar.add_operator, methods=["POST"])
    yield TestClient(app), tmp_path
    cfg.USERS_LIST[:] = snap_list
    cfg.CFG.set("users", "list", snap_cfg_list)
    assert real_config.read_bytes() == real_bytes, "TEST POLLUTED THE REAL config.ini"


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "evil\n[paths]", "a,b", "x" * 65, "a\x00b"])
def test_operator_add_refuses_hostile_names(roster, bad):
    import Orchestrator.config as cfg
    client, tmp = roster
    r = client.post("/operator/add", json={"name": bad})
    assert r.status_code == 400, r.text
    assert cfg.USERS_LIST == ["Brandon"]
    assert not (tmp / "config.ini").exists()


def test_operator_add_accepts_a_spaced_name(roster):
    import Orchestrator.config as cfg
    client, _ = roster
    r = client.post("/operator/add", json={"name": "B2 Tester"})
    assert r.status_code == 200 and r.json()["status"] == "success"
    assert "B2 Tester" in cfg.USERS_LIST


# ------------------------------------------------- other operator-path joins

def test_chrome_profile_dir_refuses_hostile_operator(tmp_path, monkeypatch):
    """The CU Chrome profile is PROFILE_BASE / operator, mkdir'ed and passed
    to Chrome as --user-data-dir."""
    import Orchestrator.browser.chrome as chrome
    from Orchestrator.utils.operators import InvalidOperatorName
    monkeypatch.setattr(chrome, "PROFILE_BASE", tmp_path / "profiles")
    for bad in ("../../escape", "a/b", "x\n"):
        with pytest.raises(InvalidOperatorName):
            chrome.ChromeInstance(operator=bad)
    assert chrome.ChromeInstance(operator="Flight Recorder").profile_dir == \
        tmp_path / "profiles" / "Flight Recorder"


def test_archive_download_refuses_path_syntax_in_utc(client):
    for bad in ("../../config", "x/../../y", "a\\b"):
        assert client.get("/artifact", params={"which": "x", "utc": bad}).status_code == 400


# ---------------------------------------------------- computer-use entry routes

class _FakeDevice:
    protocol = None


class _FakeRegistry:
    def get_device(self, _device_id):
        return _FakeDevice()


@pytest.mark.parametrize("route", ["/gemini-cu/run", "/gemini-cu/stream"])
def test_gemini_cu_routes_refuse_hostile_operator(client, monkeypatch, route):
    import Orchestrator.routes.gemini_cu_routes as gr
    import Orchestrator.tasks as tasks
    started = []

    async def _no_run(*a, **k):
        started.append(a)

    monkeypatch.setattr(gr, "get_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(gr, "create_task_session", lambda *a, **k: started.append(a))
    monkeypatch.setattr(gr, "_run_task", _no_run)
    monkeypatch.setattr(tasks, "create_task", lambda *a, **k: started.append(k) or
                        type("T", (), {"task_id": "t-1"})())
    r = client.post(route, json={"prompt": "p", "operator": "../x"})
    assert r.status_code == 400, r.text
    assert started == []


def test_browser_routes_refuse_hostile_operator(client, monkeypatch):
    import Orchestrator.browser.session_manager as bsm
    import Orchestrator.routes.browser_routes as br
    created = []

    def _no_session(*a, **k):
        created.append(a)
        raise RuntimeError("no display in tests")

    monkeypatch.setattr(br, "create_task", lambda *a, **k: created.append(k))
    monkeypatch.setattr(bsm, "get_or_create_session", _no_session)
    monkeypatch.setattr(bsm, "get_operator_session", lambda *a, **k: None)
    assert client.post("/browser/run", json={"prompt": "p", "operator": "../x"}).status_code == 400
    assert client.post("/cu/session/open", json={"operator": "a/b"}).status_code == 400
    assert created == []
