#!/usr/bin/env python3
"""MCP media tools may only touch contained uploads (O20-mcp-server-02).

speech_to_text took ``audio_path`` from the tool arguments, did ``Path(audio_path)``
+ ``open(..., "rb")`` and streamed the bytes to /stt -- i.e. to the STT
provider. On the Funnel-exposed HTTP transport a token-bound remote operator
could name Manifest/mcp_tokens.json, config.ini, .env or credentials/* and
ship it off the box.

Now:
  * HTTP (a token-bound operator): only a /ui/uploads/... URL or a path inside
    BLACKBOX_ROOT/Portal/uploads (sessions/ and terminal/ included) is
    accepted, via the shared Orchestrator.utils.paths.resolve_upload_path.
  * stdio (the local, trusted CLI -- it already has filesystem access): any
    local path is still accepted, but it must be a regular audio file under
    the size cap.
  * get_media resolves its url through the same guard (uploads only).

RUN (the supported path -- MCP/venv, no pytest needed; mirrors
test_blackbox_mcp_server.py):
    cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_mcp_media_path_containment.py
"""
import asyncio
import importlib.util as _ilu
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
os.environ.setdefault("BLACKBOX_ROOT", str(_HERE.parent))
os.chdir(_HERE)
sys.path.insert(0, str(_HERE))

_spec = _ilu.spec_from_file_location("bbmcp_media_under_test", str(_HERE / "blackbox_mcp_server.py"))
srv = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(srv)

SECRET = b"PLANTED_TEST_SECRET=canary-not-a-real-credential"
WAV = b"RIFF" + b"\x00" * 40


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"text": "hello"}

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        files = kwargs.get("files") or {}
        body = b""
        if "file" in files:
            fh = files["file"][1]
            body = fh if isinstance(fh, bytes) else fh.read()
        self.calls.append((url, body))
        return _FakeResponse()

    async def get(self, url, **kwargs):
        self.calls.append((url, b""))
        return _FakeResponse()


class _Box:
    """A throwaway BLACKBOX_ROOT with uploads, secrets and an outside dir."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-stt-"))
        self.root = self.tmp / "box"
        sess = self.root / "Portal" / "uploads" / "sessions" / "s1"
        sess.mkdir(parents=True)
        (sess / "clip.wav").write_bytes(WAV)
        (self.root / "config.ini").write_bytes(SECRET)
        (self.root / "Manifest").mkdir()
        (self.root / "Manifest" / "mcp_tokens.json").write_bytes(SECRET)
        out = self.tmp / "outside"
        out.mkdir()
        (out / "secret.wav").write_bytes(SECRET)
        (out / "local.wav").write_bytes(WAV)
        self._old_root = srv.BLACKBOX_ROOT
        self._old_client = srv.httpx.AsyncClient
        self._old_bound = srv._current_bound_operator
        srv.BLACKBOX_ROOT = self.root
        self.calls = []
        srv.httpx.AsyncClient = lambda *a, **k: _FakeClient(self.calls)
        return self

    def bind(self, operator):
        srv._current_bound_operator = lambda: operator

    def stt(self, audio_path):
        return asyncio.run(srv.call_tool("speech_to_text", {"audio_path": audio_path}))

    def __exit__(self, *a):
        srv.BLACKBOX_ROOT = self._old_root
        srv.httpx.AsyncClient = self._old_client
        srv._current_bound_operator = self._old_bound
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def _is_error(res):
    return getattr(res, "isError", False) is True


def _stt_calls(box):
    return [c for c in box.calls if c[0].endswith("/stt")]


def test_bound_operator_cannot_upload_box_secrets():
    with _Box() as box:
        box.bind("alice")
        for bad in (str(box.root / "config.ini"),
                    str(box.root / "Manifest" / "mcp_tokens.json"),
                    "/ui/uploads/../../config.ini",
                    "../../config.ini",
                    str(box.tmp / "outside" / "secret.wav")):
            res = box.stt(bad)
            assert _is_error(res), f"{bad!r} was accepted"
            body = json.loads(res.content[0].text)
            assert body["error"]["code"] in ("invalid_arguments", "not_found"), body
        assert _stt_calls(box) == []


def test_bound_operator_can_transcribe_a_session_upload():
    with _Box() as box:
        box.bind("alice")
        for good in ("/ui/uploads/sessions/s1/clip.wav",
                     str(box.root / "Portal" / "uploads" / "sessions" / "s1" / "clip.wav")):
            res = box.stt(good)
            assert not _is_error(res), res
        assert [body for _, body in _stt_calls(box)] == [WAV, WAV]


def test_stdio_keeps_local_paths_but_only_regular_audio_files():
    with _Box() as box:
        box.bind(None)
        res = box.stt(str(box.tmp / "outside" / "local.wav"))
        assert not _is_error(res), res
        for bad in (str(box.root / "config.ini"), str(box.root / "Portal" / "uploads"),
                    str(box.root / "Manifest" / "mcp_tokens.json")):
            assert _is_error(box.stt(bad)), bad
        assert [body for _, body in _stt_calls(box)] == [WAV]


def test_get_media_reads_only_uploads():
    """get_media shares the guard: /ui/ paths outside uploads are refused."""
    with _Box() as box:
        box.bind(None)
        (box.root / "Portal" / "index.html").write_text("<html>")
        for bad in ("/ui/index.html", "/ui/uploads/../index.html", "/ui/uploads/sessions"):
            res = asyncio.run(srv.call_tool("get_media", {"url": bad}))
            assert _is_error(res), bad
        res = asyncio.run(srv.call_tool("get_media", {"url": "/ui/uploads/sessions/s1/clip.wav"}))
        assert not _is_error(res), res
        assert json.loads(res[0].text)["media_type"] == "audio"


def _run_standalone():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
