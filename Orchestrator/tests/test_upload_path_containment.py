"""Containment of client- and model-supplied upload paths (audit rank 16).

Every loader that opens a path named in a request body, a chat message part or
a model tool call used to join it onto a CWD-relative ``Path("Portal")`` (or
take it verbatim) with no containment check, then base64-encode the bytes and
ship them to a third-party provider. ``'/ui/../.env'`` read the repo-root env
file into a Veo request; ``'/etc/passwd'`` went straight to xAI.

``resolve_upload_path`` is the one shared guard: it accepts ``/ui/uploads/...``
URLs (any host), absolute paths inside ``Portal/uploads`` and paths relative to
it (the session and terminal subfolders included), and refuses everything else
— absolute paths elsewhere, NUL, backslashes, ``..`` segments, symlinks that
escape the root, non-regular files, oversized files and off-list extensions.

Hermetic: a throwaway BLACKBOX_ROOT under tmp_path (``blackbox_root`` re-reads
the env after ``cache_clear``). The tier-two tests also chdir into it, so the
pre-fix CWD-relative loaders resolve into the sandbox too — that is what lets
them fail on the base code for the right reason (the secret is read and
shipped) instead of erroring on a missing file.
"""
import asyncio
import base64
import os

import pytest

# Import before any test chdirs into the sandbox: config.py reads ./config.ini
# at import time, and every route registers on the shared app here.
import Orchestrator.app  # noqa: F401
from Orchestrator.utils import paths

SECRET = b"PLANTED_TEST_SECRET=canary-not-a-real-credential"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WAV = b"RIFF" + b"\x00" * 40


@pytest.fixture
def box(tmp_path):
    """A fresh-box layout: Portal/uploads (+ sessions/terminal), a secrets file
    and a config.ini next to it, and a file outside the box entirely."""
    root = tmp_path / "box"
    uploads = root / "Portal" / "uploads"
    (uploads / "sessions" / "session-1").mkdir(parents=True)
    (uploads / "terminal" / "term-1").mkdir(parents=True)
    (root / "credentials").mkdir()
    (root / "credentials" / ".env").write_bytes(SECRET)
    (root / ".env").write_bytes(SECRET)
    (root / "config.ini").write_bytes(SECRET)
    (uploads / "x.png").write_bytes(PNG)
    (uploads / "notes.txt").write_bytes(b"plain notes")
    (uploads / "sessions" / "session-1" / "clip.wav").write_bytes(WAV)
    (uploads / "terminal" / "term-1" / "my notes.txt").write_bytes(b"terminal notes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.wav").write_bytes(SECRET)

    old = os.environ.get("BLACKBOX_ROOT")
    os.environ["BLACKBOX_ROOT"] = str(root)
    paths.blackbox_root.cache_clear()
    try:
        yield root
    finally:
        if old is None:
            os.environ.pop("BLACKBOX_ROOT", None)
        else:
            os.environ["BLACKBOX_ROOT"] = old
        paths.blackbox_root.cache_clear()


def _uploads(box):
    return (box / "Portal" / "uploads").resolve()


# ------------------------------------------------------------ helper: accepts

@pytest.mark.parametrize("value, rel", [
    ("/ui/uploads/x.png", "x.png"),
    ("http://100.64.0.7:9091/ui/uploads/x.png", "x.png"),
    ("https://box.example.ts.net/ui/uploads/x.png?v=3#frag", "x.png"),
    ("x.png", "x.png"),
    ("Portal/uploads/x.png", "x.png"),
    # A leading './' is a no-op (the stdio CLI and old CWD-relative callers).
    ("./x.png", "x.png"),
    ("./Portal/uploads/x.png", "x.png"),
    ("sessions/session-1/clip.wav", "sessions/session-1/clip.wav"),
    ("/ui/uploads/sessions/session-1/clip.wav", "sessions/session-1/clip.wav"),
    # The terminal attach route percent-quotes the URL it hands back.
    ("/ui/uploads/terminal/term-1/my%20notes.txt", "terminal/term-1/my notes.txt"),
])
def test_accepts_contained_upload_references(box, value, rel):
    got = paths.resolve_upload_path(value)
    assert got == _uploads(box) / rel
    assert got.is_absolute()


def test_accepts_absolute_session_path_inside_uploads(box):
    p = box / "Portal" / "uploads" / "sessions" / "session-1" / "clip.wav"
    assert paths.resolve_upload_path(str(p), allowed_exts=paths.AUDIO_EXTS) == p.resolve()
    assert paths.resolve_upload_path(p) == p.resolve()  # a Path works too


def test_accepts_symlink_that_stays_inside_the_root(box):
    link = box / "Portal" / "uploads" / "alias.png"
    link.symlink_to(box / "Portal" / "uploads" / "x.png")
    assert paths.resolve_upload_path("/ui/uploads/alias.png",
                                     allowed_exts=paths.IMAGE_EXTS) == _uploads(box) / "x.png"


# ------------------------------------------------------------ helper: refuses

TRAVERSAL = [
    "../../credentials/.env",
    "../.env",
    "/etc/passwd",
    "/ui/uploads/../../config.ini",
    "/ui/uploads/../../.env",
    "/ui/../.env",
    "/ui/../credentials/.env",
    "http://evil.example/ui/uploads/../../config.ini",
    "/ui/uploads/%2e%2e/%2e%2e/config.ini",
    "/ui/uploads/x.png\x00.txt",
    "sessions\\session-1\\clip.wav",
    "..\\..\\config.ini",
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "https://evil.example/etc/passwd",
    "Portal/index.html",
    "./../config.ini",
    "./../../credentials/.env",
    "./Portal/uploads/../../config.ini",
    "",
    "   ",
]


@pytest.mark.parametrize("bad", TRAVERSAL)
def test_refuses_traversal_and_foreign_references(box, bad):
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path(bad)


@pytest.mark.parametrize("bad", [None, 123, {"url": "/ui/uploads/x.png"}, ["/ui/uploads/x.png"], b"x.png"])
def test_refuses_non_string_values(box, bad):
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path(bad)


def test_refuses_absolute_paths_outside_the_root(box):
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path(str(box / "config.ini"))
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path(str(box.parent / "outside" / "secret.wav"))


def test_refuses_absolute_path_outside_even_if_it_links_inside(box):
    """Lexically outside is refused even when the target lands in uploads."""
    link = box.parent / "outside" / "sneaky.png"
    link.symlink_to(box / "Portal" / "uploads" / "x.png")
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path(str(link))


@pytest.mark.parametrize("target", ["credentials/.env", "config.ini", "../outside/secret.wav"])
def test_refuses_symlink_escaping_the_root(box, target):
    link = box / "Portal" / "uploads" / "sessions" / "session-1" / "evil.wav"
    link.symlink_to((box / target).resolve())
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/sessions/session-1/evil.wav")
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path(str(link))


def test_refuses_symlinked_directory_escaping_the_root(box):
    (box / "Portal" / "uploads" / "sessions" / "linked").symlink_to(box / "credentials")
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/sessions/linked/.env")


def test_refuses_directories_and_fifos(box):
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/sessions")
    fifo = box / "Portal" / "uploads" / "pipe.wav"
    os.mkfifo(fifo)
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/pipe.wav")


def test_refuses_oversized_file(box):
    (box / "Portal" / "uploads" / "big.png").write_bytes(b"\x00" * 2048)
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/big.png", max_bytes=1024)
    assert paths.resolve_upload_path("/ui/uploads/big.png", max_bytes=4096)


def test_default_size_cap_applies(box, monkeypatch):
    monkeypatch.setattr(paths, "DEFAULT_MAX_UPLOAD_BYTES", 16)
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/x.png")


def test_refuses_extension_outside_the_allowlist(box):
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/notes.txt", allowed_exts=paths.IMAGE_EXTS)
    assert paths.resolve_upload_path("/ui/uploads/notes.txt", allowed_exts=paths.DOC_EXTS)
    # The extension of the RESOLVED file is what counts, not the link's name.
    (box / "Portal" / "uploads" / "disguised.png").symlink_to(box / "Portal" / "uploads" / "notes.txt")
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("/ui/uploads/disguised.png", allowed_exts=paths.IMAGE_EXTS)


def test_missing_file_is_a_not_found_subclass(box):
    with pytest.raises(paths.UploadNotFoundError):
        paths.resolve_upload_path("/ui/uploads/nope.png")
    assert issubclass(paths.UploadNotFoundError, paths.UploadPathError)
    assert issubclass(paths.UploadPathError, ValueError)


def test_huge_value_is_refused_cleanly(box):
    with pytest.raises(paths.UploadPathError):
        paths.resolve_upload_path("a" * 100_000)


def test_error_message_does_not_echo_the_box_root(box):
    with pytest.raises(paths.UploadPathError) as exc:
        paths.resolve_upload_path("/ui/uploads/../../config.ini")
    assert str(box) not in str(exc.value)


def test_root_is_anchored_to_blackbox_root_not_cwd(box, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert paths.resolve_upload_path("/ui/uploads/x.png") == _uploads(box) / "x.png"


# ------------------------------------------------- tier two: the real loaders

def _task(task_type, **kw):
    from Orchestrator.models import Task, TaskStatus
    return Task(task_id="t-containment", task_type=task_type, status=TaskStatus.PROCESSING,
                created_at="2026-10-17T00:00:00Z", updated_at="2026-10-17T00:00:00Z", **kw)


@pytest.fixture
def in_box(box, monkeypatch):
    """chdir into the box so the pre-fix CWD-relative Path("Portal") loaders
    resolve into the sandbox (and read its planted secret)."""
    monkeypatch.chdir(box)
    return box


def test_video_task_refuses_image_url_traversal(in_box, monkeypatch):
    """O04-tasks-retrieval-01 / X05-architecture-18: '/ui/../.env' used to be
    read, base64-encoded and POSTed to Veo."""
    from Orchestrator import tasks
    from Orchestrator.models import TaskType

    posted = []
    monkeypatch.setattr(tasks, "GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(tasks, "update_task", lambda *a, **k: None)

    def _no_network(*a, **k):
        posted.append(k.get("json"))
        raise AssertionError("Veo must not be called for a refused image_url")

    monkeypatch.setattr(tasks.requests, "post", _no_network)
    for bad in ("/ui/../.env", "/ui/../credentials/.env", "/ui/uploads/../../config.ini",
                "http://evil.example/ui/../.env"):
        task = _task(TaskType.VIDEO_GENERATION, prompt="p",
                     result_data={"options": {"image_url": bad}})
        with pytest.raises(Exception) as exc:
            tasks.process_video_generation(task)
        assert not isinstance(exc.value, AssertionError), f"{bad!r} reached Veo"
    assert posted == []
    assert base64.b64encode(SECRET).decode() not in repr(posted)


def test_video_task_still_animates_a_contained_upload(in_box, monkeypatch):
    from Orchestrator import tasks
    from Orchestrator.models import TaskType

    seen = {}
    monkeypatch.setattr(tasks, "GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(tasks, "update_task", lambda *a, **k: None)

    class _Stop(Exception):
        pass

    def _capture(url, **k):
        seen["json"] = k.get("json")
        raise _Stop()

    monkeypatch.setattr(tasks.requests, "post", _capture)
    task = _task(TaskType.VIDEO_GENERATION, prompt="p",
                 result_data={"options": {"image_url": "https://box.example.ts.net/ui/uploads/x.png"}})
    with pytest.raises(_Stop):
        tasks.process_video_generation(task)
    image = seen["json"]["instances"][0]["image"]
    assert base64.b64decode(image["bytesBase64Encoded"]) == PNG
    assert image["mimeType"] == "image/png"


class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _openai_ok():
    return _FakeResp({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                      "usage": {}})


def test_openai_loader_never_inlines_an_outside_file(in_box, monkeypatch):
    """O02-chat-routes-A-05: image_url / document_url parts were joined onto
    Path('Portal') — '/etc/passwd'-style absolute values replace it outright."""
    import Orchestrator.routes.chat_routes as cr

    payloads = []
    monkeypatch.setattr(cr, "OPENAI_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr.requests, "post",
                        lambda url, **k: payloads.append(k["json"]) or _openai_ok())
    secret_abs = str(in_box / "credentials" / ".env")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": secret_abs}},
        {"type": "image_url", "image_url": {"url": "/ui/uploads/../../config.ini"}},
        {"type": "document_url", "document_url": {"url": "/ui/../credentials/.env"}},
        {"type": "image_url", "image_url": {"url": "/ui/uploads/x.png"}},
    ]}]
    cr.call_openai(messages, "gpt-test", "Tester")
    sent = repr(payloads[0]["messages"])
    assert SECRET.decode() not in sent
    assert base64.b64encode(SECRET).decode() not in sent
    # The legitimate upload is still inlined.
    assert base64.b64encode(PNG).decode() in sent


def test_xai_loader_drops_the_absolute_path_branch(in_box, monkeypatch):
    """O03-chat-routes-B-03: xAI/custom accepted ANY absolute path outright."""
    import Orchestrator.routes.chat_routes as cr

    payloads = []
    monkeypatch.setattr(cr, "XAI_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr.requests, "post",
                        lambda url, **k: payloads.append(k["json"]) or _openai_ok())
    outside = str(in_box.parent / "outside" / "secret.wav")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": outside}},
        {"type": "image_url", "image_url": {"url": str(in_box / "config.ini")}},
        {"type": "image_url", "image_url": {"url": str(in_box / "Portal" / "uploads" / "x.png")}},
    ]}]
    cr.call_xai(messages, "grok-test", "Tester")
    sent = repr(payloads[0]["messages"])
    assert base64.b64encode(SECRET).decode() not in sent
    assert base64.b64encode(PNG).decode() in sent


def test_gemini_loader_refuses_traversal_for_every_media_kind(in_box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    payloads = []
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])

    def _post(url, **k):
        payloads.append(k.get("json"))
        return _FakeResp({"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                          "finishReason": "STOP"}], "usageMetadata": {}})

    monkeypatch.setattr(cr.requests, "post", _post)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "audio_url", "audio_url": {"url": "/ui/../.env"}},
        {"type": "video_url", "video_url": {"url": str(in_box / "config.ini")}},
        {"type": "document_url", "document_url": {"url": "/ui/uploads/../../credentials/.env"}},
        {"type": "audio_url", "audio_url": {"url": "/ui/uploads/sessions/session-1/clip.wav"}},
    ]}]
    try:
        cr.call_gemini(messages, "gemini-test", "Tester")
    except Exception:
        pass  # response parsing is not what this test is about
    assert payloads, "gemini was never called"
    sent = repr(payloads[0])
    assert base64.b64encode(SECRET).decode() not in sent
    assert base64.b64encode(WAV).decode() in sent


def test_inline_loaders_apply_the_inline_size_cap(in_box, monkeypatch):
    """Chat attachments are base64'd into the request: the tighter inline cap
    applies, not the 500 MB upload ceiling."""
    import Orchestrator.routes.chat_routes as cr
    assert cr._resolve_attachment("/ui/uploads/x.png", paths.IMAGE_EXTS) is not None
    monkeypatch.setattr(cr, "INLINE_MAX_BYTES", 8)
    assert cr._resolve_attachment("/ui/uploads/x.png", paths.IMAGE_EXTS) is None


# An upload refused for its size or type used to vanish with only a server
# print, so the model answered as if nothing was attached (review finding).
# Now such a part becomes a short text note; a missing file, an external URL
# or a non-upload reference keeps the old pass-through / skip behaviour.

def test_load_attachment_notes_only_size_and_type_refusals(in_box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    path, note = cr._load_attachment("/ui/uploads/x.png", paths.IMAGE_EXTS)
    assert path == _uploads(in_box) / "x.png" and note is None
    path, note = cr._load_attachment("/ui/uploads/notes.txt", paths.IMAGE_EXTS)
    assert path is None and "not loaded" in note and "notes.txt" in note
    monkeypatch.setattr(cr, "INLINE_MAX_BYTES", 8)
    path, note = cr._load_attachment("/ui/uploads/x.png", paths.IMAGE_EXTS)
    assert path is None and "larger than" in note and "x.png" in note
    assert str(in_box) not in note
    for other in ("/ui/uploads/nope.png", "https://example.com/cat.png", "/ui/../.env",
                  str(in_box / "config.ini")):
        assert cr._load_attachment(other, paths.IMAGE_EXTS) == (None, None), other


def _user_parts(payload_messages):
    return [m for m in payload_messages if m.get("role") == "user"][-1]["content"]


def test_openai_loader_reports_a_refused_attachment(in_box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    payloads = []
    monkeypatch.setattr(cr, "OPENAI_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr.requests, "post",
                        lambda url, **k: payloads.append(k["json"]) or _openai_ok())
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "/ui/uploads/notes.txt"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
    ]}]
    cr.call_openai(messages, "gpt-test", "Tester")
    parts = _user_parts(payloads[0]["messages"])
    notes = [p["text"] for p in parts if p.get("type") == "text" and "not loaded" in p["text"]]
    assert len(notes) == 1 and "notes.txt" in notes[0]
    # The refused upload's URL is no longer handed to OpenAI to fetch ...
    assert all(p.get("image_url", {}).get("url") != "/ui/uploads/notes.txt" for p in parts)
    # ... while an external image URL still passes through as before.
    assert {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}} in parts


def test_anthropic_loader_reports_audio_sent_as_an_image(in_box, monkeypatch):
    """The Android app sends every attachment, audio included, as image_url."""
    import Orchestrator.routes.chat_routes as cr

    payloads = []
    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])

    def _post(url, **k):
        payloads.append(k.get("json"))
        return _FakeResp({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                          "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(cr.requests, "post", _post)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "/ui/uploads/sessions/session-1/clip.wav"}},
    ]}]
    try:
        cr.call_anthropic(messages, "claude-test", "Tester")
    except Exception:
        pass  # response parsing is not what this test is about
    assert payloads, "anthropic was never called"
    parts = _user_parts(payloads[0]["messages"])
    assert any(p.get("type") == "text" and "not loaded" in p["text"] and "clip.wav" in p["text"]
               for p in parts), parts


def _gemini_post(payloads):
    def _post(url, **k):
        payloads.append(k.get("json"))
        return _FakeResp({"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                          "finishReason": "STOP"}], "usageMetadata": {}})
    return _post


def test_gemini_loader_reports_an_oversized_attachment(in_box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    payloads = []
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr.requests, "post", _gemini_post(payloads))
    monkeypatch.setattr(cr, "GEMINI_INLINE_MAX_BYTES", 8)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "listen"},
        {"type": "audio_url", "audio_url": {"url": "/ui/uploads/sessions/session-1/clip.wav"}},
    ]}]
    try:
        cr.call_gemini(messages, "gemini-test", "Tester")
    except Exception:
        pass
    sent = repr(payloads[0])
    assert base64.b64encode(WAV).decode() not in sent
    assert "not loaded" in sent and "larger than" in sent


def test_gemini_media_loaders_use_the_gemini_inline_limit(in_box, monkeypatch):
    """Gemini takes up to 100 MB of base64 inline data per request (its file
    input docs): the tighter generic cap must not drop media it accepts, such
    as the Android overlay's screen recordings."""
    import Orchestrator.routes.chat_routes as cr
    assert paths.INLINE_MAX_BYTES < paths.GEMINI_INLINE_MAX_BYTES
    assert paths.GEMINI_INLINE_MAX_BYTES * 4 / 3 <= 100 * 1000 * 1000

    payloads = []
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "fake")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr.requests, "post", _gemini_post(payloads))
    monkeypatch.setattr(cr, "INLINE_MAX_BYTES", 8)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "listen"},
        {"type": "audio_url", "audio_url": {"url": "/ui/uploads/sessions/session-1/clip.wav"}},
    ]}]
    try:
        cr.call_gemini(messages, "gemini-test", "Tester")
    except Exception:
        pass
    assert base64.b64encode(WAV).decode() in repr(payloads[0])


def test_get_media_refuses_paths_outside_uploads(in_box):
    import Orchestrator.routes.chat_routes as cr

    for bad in ("/ui/../config.ini", "/ui/uploads/../../.env", "/ui/index.html"):
        out = cr.execute_get_media(url=bad)
        assert "error" in out, bad
        assert "file_size_bytes" not in out
    ok = cr.execute_get_media(url="/ui/uploads/x.png")
    assert ok.get("status") == "found" and ok["media_type"] == "image"


# ------------------------------------------------------------- HTTP endpoints

@pytest.fixture
def client(in_box):
    import Orchestrator.app  # noqa: F401  (registers every route on the shared app)
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


def test_generate_video_route_refuses_image_url_traversal(client, monkeypatch):
    """O07-tts-voice-04."""
    import Orchestrator.routes.tts_routes as tr

    created = []
    monkeypatch.setattr(tr, "create_task", lambda *a, **k: created.append(k) or None)
    for bad in ("/ui/../.env", "/ui/uploads/../../config.ini", "http://h/ui/../credentials/.env"):
        r = client.post("/generate/video", json={"prompt": "p", "image_url": bad})
        assert r.status_code == 400, (bad, r.status_code, r.text)
    assert created == []


@pytest.mark.parametrize("route, extra", [
    ("/elevenlabs/voice-changer", {"target_voice": "elevenlabs:abc"}),
    ("/elevenlabs/isolate", {}),
])
def test_elevenlabs_transform_routes_refuse_outside_paths(client, in_box, monkeypatch, route, extra):
    """O07-tts-voice-05: only os.path.exists() stood between the body and
    ElevenLabs."""
    from Orchestrator.elevenlabs import transform

    calls = []
    monkeypatch.setattr(transform, "change_voice", lambda *a, **k: calls.append(a) or b"x")
    monkeypatch.setattr(transform, "isolate", lambda *a, **k: calls.append(a) or b"x")
    for bad in (str(in_box / "credentials" / ".env"), str(in_box.parent / "outside" / "secret.wav"),
                "/ui/uploads/../../config.ini"):
        r = client.post(route, json={"audio_path": bad, **extra})
        assert r.status_code == 400, (bad, r.status_code, r.text)
    assert calls == []


def test_analyze_audio_route_refuses_outside_paths(client, in_box, monkeypatch):
    import Orchestrator.routes.task_routes as trr

    created = []
    monkeypatch.setattr(trr, "create_task", lambda *a, **k: created.append(k))
    r = client.post("/analyze/audio", json={"file_path": str(in_box / "config.ini"),
                                            "operator": "Tester"})
    assert r.status_code == 400
    assert created == []


def test_audio_analysis_worker_refuses_a_stored_outside_path(in_box, monkeypatch):
    """Defense in depth: a task row carrying an outside input_file (queued
    before this fix, or by another creator) never reaches the Files API."""
    from Orchestrator import tasks
    from Orchestrator.models import TaskType

    monkeypatch.setattr(tasks, "GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(tasks, "update_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("uploaded")))
    task = _task(TaskType.AUDIO_ANALYSIS,
                 input_file=str(in_box / "credentials" / ".env"), result_data={})
    with pytest.raises(Exception) as exc:
        tasks.process_audio_analysis(task)
    assert not isinstance(exc.value, AssertionError)


def test_analyze_sync_endpoint_is_gone(client):
    """X03-security-backend-17 / O06-frontier-agent-01: dead endpoint deleted."""
    from Orchestrator.checkpoint import app
    assert "/analyze/sync" not in {getattr(r, "path", None) for r in app.routes}
    assert client.post("/analyze/sync", json={"file_url": "/etc/passwd"}).status_code in (404, 405)


# ------------------------------------------------------- the upload executors

def _load_executor(name):
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "ToolVault" / "tools" / name / "executor.py"
    spec = importlib.util.spec_from_file_location(f"containment_{name}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx():
    from Orchestrator.toolvault.context import ToolContext
    return ToolContext(operator="Tester", base_url="http://127.0.0.1:1")


def test_elevenlabs_clone_refuses_outside_samples(box, monkeypatch):
    """O18-toolvault-modules-02: os.path.exists() was the only gate."""
    from Orchestrator.elevenlabs import voices

    calls = []
    monkeypatch.setattr(voices, "clone_instant", lambda *a, **k: calls.append(a) or {"voice_id": "v"})
    ex = _load_executor("elevenlabs_clone_voice")
    good = str(box / "Portal" / "uploads" / "sessions" / "session-1" / "clip.wav")
    res = asyncio.run(ex.execute({"name": "V", "confirm_consent": True,
                                  "audio_paths": [good, str(box / "credentials" / ".env")]}, _ctx()))
    assert res.success is False and calls == []
    res = asyncio.run(ex.execute({"name": "V", "confirm_consent": True,
                                  "audio_paths": ["/ui/uploads/sessions/session-1/clip.wav"]}, _ctx()))
    assert res.success is True
    assert calls and list(calls[0][1]) == [str(_uploads(box) / "sessions" / "session-1" / "clip.wav")]


def test_xai_clone_refuses_outside_sample(box, monkeypatch):
    from Orchestrator import xai_voices

    calls = []
    monkeypatch.setattr(xai_voices, "clone_voice", lambda *a, **k: calls.append(a) or {"voice_id": "v"})
    ex = _load_executor("xai_clone_voice")
    res = asyncio.run(ex.execute({"name": "V", "confirm_consent": True,
                                  "audio_path": str(box.parent / "outside" / "secret.wav")}, _ctx()))
    assert res.success is False and calls == []


def test_speech_to_text_refuses_outside_audio(box, monkeypatch):
    ex = _load_executor("speech_to_text")
    calls = []

    async def _fake_post(base_url, audio_file, content_type, provider, diarize):
        calls.append(audio_file)
        return {"text": "hi"}

    monkeypatch.setattr(ex, "_post_stt", _fake_post)
    for bad in (str(box / "config.ini"), "/ui/uploads/../../.env", str(box.parent / "outside" / "secret.wav")):
        res = asyncio.run(ex.execute({"audio_path": bad}, _ctx()))
        assert res.success is False, bad
    assert calls == []
    res = asyncio.run(ex.execute({"audio_path": "/ui/uploads/sessions/session-1/clip.wav"}, _ctx()))
    assert res.success is True and calls


@pytest.mark.parametrize("tool, extra", [
    ("analyze_audio", {}),
    ("elevenlabs_isolate_voice", {}),
    ("elevenlabs_voice_changer", {"target_voice": "elevenlabs:abc"}),
])
def test_forwarding_executors_refuse_before_any_request(box, tool, extra):
    """The three executors that forward a path to a route refuse it up front
    (base_url points at a dead port, so a forwarded request would surface as
    a connection error, not as this refusal)."""
    ex = _load_executor(tool)
    key = "file_path" if tool == "analyze_audio" else "audio_path"
    res = asyncio.run(ex.execute({key: str(box / "credentials" / ".env"), **extra}, _ctx()))
    assert res.success is False
    assert "upload" in res.result.lower()
