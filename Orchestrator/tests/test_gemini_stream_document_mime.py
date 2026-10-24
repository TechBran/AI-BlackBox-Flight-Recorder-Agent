"""The streaming Gemini path types a document attachment as /chat's Gemini path does.

The Portal sends .txt, .doc and .docx attachments as document_url parts on
the streaming path too (one attachment table for both modes, audit
P01-chat-core-web-16). stream_gemini_with_thinking -- the Portal's default
chat path -- had no entry for those suffixes and shipped them to Gemini as
inline data of type application/octet-stream, while call_gemini (POST /chat)
names each type. The two paths now agree.

Hermetic: the attachment loader and both HTTP clients are replaced; no
network, no uploads directory.
"""
import asyncio
import json

import pytest


@pytest.fixture
def cr(monkeypatch, tmp_path):
    import Orchestrator.routes.chat_routes as cr
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-key-not-real", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    files = {}
    for name in ("notes.txt", "letter.doc", "report.docx", "scan.pdf"):
        p = tmp_path / name
        p.write_bytes(b"x")
        files[f"/ui/uploads/{name}"] = p
    monkeypatch.setattr(cr, "_load_attachment",
                        lambda url, exts, max_bytes=None: (files.get(url), None))
    return cr


def _messages(name):
    return [{"role": "user", "content": [
        {"type": "text", "text": "read this"},
        {"type": "document_url", "document_url": {"url": f"/ui/uploads/{name}"}},
    ]}]


def _inline_mimes(payload):
    return [p["inline_data"]["mime_type"]
            for c in payload["contents"] for p in c["parts"] if "inline_data" in p]


def _stream_payload(cr, monkeypatch, name):
    seen = {}

    class _Resp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kw):
            seen["json"] = kw.get("json")
            return _Ctx()

    monkeypatch.setattr(cr.httpx, "AsyncClient", _Client)

    async def _drain():
        return [ev async for ev in cr.stream_gemini_with_thinking(_messages(name), "gemini-test", "test-op")]

    asyncio.run(_drain())
    return seen["json"]


def _post_payload(cr, monkeypatch, name):
    seen = {}

    class _Resp:
        status_code = 200
        ok = True
        text = ""

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {}}

        def raise_for_status(self):
            return None

    def _post(url, **kw):
        seen.setdefault("json", kw.get("json"))
        return _Resp()

    monkeypatch.setattr(cr.requests, "post", _post)
    try:
        cr.call_gemini(_messages(name), "gemini-test", "test-op")
    except Exception:
        pass  # only the request is under test
    return seen["json"]


@pytest.mark.parametrize("name, mime", [
    ("notes.txt", "text/plain"),
    ("letter.doc", "application/msword"),
    ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("scan.pdf", "application/pdf"),
])
def test_stream_types_documents_like_call_gemini(cr, monkeypatch, name, mime):
    streamed = _inline_mimes(_stream_payload(cr, monkeypatch, name))
    posted = _inline_mimes(_post_payload(cr, monkeypatch, name))
    assert streamed == [mime]
    assert streamed == posted
