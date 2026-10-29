"""The non-stream chat path must deliver attachments to the provider (W04).

call_anthropic and call_gemini serve every turn that is not a Portal/Android
stream: cron jobs and SMS (tasks.process_chat_task), MCP chat_with_context and
the checkpoint compressor. Both functions once re-imported ``Path`` inside
their bodies, which made ``Path`` a local name for the WHOLE function: the
attachment loader near the top raised UnboundLocalError, the surrounding
``except Exception`` only printed, and every image and document was dropped
without a word. The model then answered as if nothing had been attached.

These tests post a real upload through each function with the provider HTTP
mocked and assert the bytes reach the provider payload. A structural guard
pins the root cause: no chat-path function may bind a name the module already
imports (that is what turns the module import into an unbound local).

Hermetic: a throwaway BLACKBOX_ROOT under tmp_path holds the uploads, and
requests.post is a recorder.
"""
import base64
import copy
import symtable
from pathlib import Path

import pytest

# Import before the fixture chdirs into the sandbox: config.py reads
# ./config.ini at import time.
import Orchestrator.routes.chat_routes  # noqa: F401
from Orchestrator.utils import paths

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n% test document\n%%EOF\n"

# Since audit rank 74 the chat-path functions below are thin wrappers in the
# chat routes' providers part; the loops themselves live in the adapter modules,
# and the rest of the chat routes in the other parts (every function checked).
_CHAT_ROUTES = Path(__file__).resolve().parents[1] / "routes" / "chat" / "providers.py"
_ADAPTERS = [Path(__file__).resolve().parents[1] / "providers" / name for name in (
    "openai_compat.py", "anthropic_messages.py", "gemini_generate.py", "tool_dispatch.py")]
_ADAPTERS += sorted((Path(__file__).resolve().parents[1] / "routes" / "chat").glob("*.py"))

# The chat-path provider loops: the five non-stream call_* functions and the
# five streaming chat generators.
_CHAT_PATH_FUNCTIONS = (
    "call_openai", "call_anthropic", "call_gemini", "call_xai", "call_custom",
    "stream_openai_with_reasoning", "stream_anthropic_with_thinking",
    "stream_gemini_with_thinking", "stream_xai_with_reasoning",
    "stream_custom_with_reasoning",
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A box whose Portal/uploads holds one PNG and one PDF. The test also runs
    from its root, so a CWD-relative loader would find the same files."""
    root = tmp_path / "box"
    uploads = root / "Portal" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "photo.png").write_bytes(PNG)
    (uploads / "brief.pdf").write_bytes(PDF)
    monkeypatch.setenv("BLACKBOX_ROOT", str(root))
    monkeypatch.chdir(root)
    paths.blackbox_root.cache_clear()
    yield root
    paths.blackbox_root.cache_clear()


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake"

    def json(self):
        return self._payload


@pytest.fixture
def cr(monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-google", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")
    return cr


def _record_posts(monkeypatch, cr, reply):
    sent = []

    def fake_post(url, headers=None, json=None, **kw):
        sent.append(copy.deepcopy(json))
        return _FakeResp(reply)

    monkeypatch.setattr(cr.requests, "post", fake_post)
    return sent


_ANTHROPIC_OK = {"content": [{"type": "text", "text": "I see it."}],
                 "stop_reason": "end_turn",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}
_GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "I see it."}]}}],
              "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


def _user_turn(*parts):
    return [{"role": "user", "content": [{"type": "text", "text": "what is this?"}, *parts]}]


# --------------------------------------------------------------------------- #
# call_anthropic                                                              #
# --------------------------------------------------------------------------- #

def test_call_anthropic_sends_an_attached_image(box, cr, monkeypatch):
    sent = _record_posts(monkeypatch, cr, _ANTHROPIC_OK)
    cr.call_anthropic(
        _user_turn({"type": "image_url", "image_url": {"url": "/ui/uploads/photo.png"}}),
        "claude-opus-5-5", operator="test-op")

    content = sent[0]["messages"][0]["content"]
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 1, f"the image never reached Anthropic: {content}"
    assert images[0]["source"] == {
        "type": "base64", "media_type": "image/png",
        "data": base64.b64encode(PNG).decode()}


def test_call_anthropic_sends_an_attached_pdf(box, cr, monkeypatch):
    sent = _record_posts(monkeypatch, cr, _ANTHROPIC_OK)
    cr.call_anthropic(
        _user_turn({"type": "document_url", "document_url": {"url": "/ui/uploads/brief.pdf"}}),
        "claude-opus-5-5", operator="test-op")

    docs = [b for b in sent[0]["messages"][0]["content"] if b.get("type") == "document"]
    assert len(docs) == 1, "the PDF never reached Anthropic"
    assert docs[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(docs[0]["source"]["data"]) == PDF


def test_call_anthropic_says_so_when_an_attachment_cannot_be_read(box, cr, monkeypatch):
    # A loader failure must not drop the attachment silently: the model gets a
    # note, so it can tell the user instead of answering as if nothing came.
    sent = _record_posts(monkeypatch, cr, _ANTHROPIC_OK)

    def broken(*a, **k):
        raise OSError("disk read failed")

    monkeypatch.setattr(cr, "_load_attachment", broken)
    cr.call_anthropic(
        _user_turn({"type": "image_url", "image_url": {"url": "/ui/uploads/photo.png"}}),
        "claude-opus-5-5", operator="test-op")

    texts = [b["text"] for b in sent[0]["messages"][0]["content"] if b.get("type") == "text"]
    assert any("could not be loaded" in t for t in texts), texts


# --------------------------------------------------------------------------- #
# call_gemini                                                                 #
# --------------------------------------------------------------------------- #

def test_call_gemini_sends_an_attached_image(box, cr, monkeypatch):
    sent = _record_posts(monkeypatch, cr, _GEMINI_OK)
    cr.call_gemini(
        _user_turn({"type": "image_url", "image_url": {"url": "/ui/uploads/photo.png"}}),
        "gemini-3.1-pro-preview", operator="test-op")

    parts = sent[0]["contents"][0]["parts"]
    inline = [p["inline_data"] for p in parts if "inline_data" in p]
    assert inline == [{"mime_type": "image/png", "data": base64.b64encode(PNG).decode()}], (
        f"the image never reached Gemini: {parts}")


def test_call_gemini_sends_an_attached_pdf(box, cr, monkeypatch):
    sent = _record_posts(monkeypatch, cr, _GEMINI_OK)
    cr.call_gemini(
        _user_turn({"type": "document_url", "document_url": {"url": "/ui/uploads/brief.pdf"}}),
        "gemini-3.1-pro-preview", operator="test-op")

    inline = [p["inline_data"] for p in sent[0]["contents"][0]["parts"] if "inline_data" in p]
    assert len(inline) == 1 and inline[0]["mime_type"] == "application/pdf"


def test_call_gemini_says_so_when_an_attachment_cannot_be_read(box, cr, monkeypatch):
    sent = _record_posts(monkeypatch, cr, _GEMINI_OK)

    def broken(*a, **k):
        raise OSError("disk read failed")

    monkeypatch.setattr(cr, "_load_attachment", broken)
    cr.call_gemini(
        _user_turn({"type": "image_url", "image_url": {"url": "/ui/uploads/photo.png"}}),
        "gemini-3.1-pro-preview", operator="test-op")

    texts = [p["text"] for p in sent[0]["contents"][0]["parts"] if "text" in p]
    assert any("could not be loaded" in t for t in texts), texts


# --------------------------------------------------------------------------- #
# Structural guard: the UnboundLocalError class                               #
# --------------------------------------------------------------------------- #

def _shadowed_module_names(path=_CHAT_ROUTES, functions=_CHAT_PATH_FUNCTIONS):
    """{function: [module-level names it binds locally]} for the chat-path loops
    (``functions=None``: every function in the module)."""
    table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    module_names = {s.get_name() for s in table.get_symbols() if s.is_imported() or s.is_assigned()}
    found = {}
    for child in table.get_children():
        if child.get_type() == "function" and (functions is None or child.get_name() in functions):
            bad = sorted(s.get_name() for s in child.get_symbols()
                         if s.is_local() and not s.is_parameter() and s.get_name() in module_names)
            if bad:
                found[child.get_name()] = bad
    return found


def test_chat_path_functions_never_rebind_a_module_name():
    # A function-scoped `from pathlib import Path` (or `import asyncio`) makes
    # the name local to the whole function, so any use above the import raises
    # UnboundLocalError. Use the module's import instead.
    seen = {f for f in _CHAT_PATH_FUNCTIONS}
    table = symtable.symtable(_CHAT_ROUTES.read_text(encoding="utf-8"), str(_CHAT_ROUTES), "exec")
    assert seen <= {c.get_name() for c in table.get_children()}, "a chat-path function was renamed"
    assert _shadowed_module_names() == {}


@pytest.mark.parametrize("path", _ADAPTERS, ids=lambda p: p.name)
def test_provider_adapter_functions_never_rebind_a_module_name(path):
    assert _shadowed_module_names(path, None) == {}
