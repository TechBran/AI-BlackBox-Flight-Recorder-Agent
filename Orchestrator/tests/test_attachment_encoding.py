"""One attachment encoder per wire format, shared by the stream and the
non-stream path (audit rank 74, X05-architecture-11).

The adapters kept a stream copy and a non-stream copy of each encoder, and the
copies disagreed in the way the audit found elsewhere (a fix landed in one):

  * the OpenAI stream sent a Portal upload ("/ui/uploads/photo.png") and an
    Anthropic-format base64 image to OpenAI raw, which it cannot read; the xAI
    and custom streams and call_openai converted both to data URLs;
  * the Gemini and Anthropic streams dropped a base64 image part that their
    non-stream twins forward;
  * Gemini's stream typed a .webp as application/octet-stream and both
    Anthropic paths typed it image/png;
  * call_anthropic dropped a document it could not inline without a word, and
    the non-stream paths of every adapter used their own older notes for
    video and documents instead of the shared degrade the streams use.

Each adapter now encodes a message one way on both paths. Hermetic: the
provider-golden harness (fake transports, uploads under tmp_path).
"""
import copy

import pytest

from Orchestrator.tests import provider_goldens as pg

_SPECS = {s["name"]: s for s in pg.scenario_specs()}
PROVIDERS = ("openai", "anthropic", "google", "xai", "custom")


def _user_parts(provider, observed):
    """The parts the adapter sent upstream for the turn's last user message."""
    body = observed["upstream"][0]["json"]
    if provider == "google":
        return body["contents"][-1]["parts"]
    return body["messages"][-1]["content"]


def _run(provider, path, monkeypatch, tmp_path, user=None):
    spec = copy.deepcopy(_SPECS[f"{provider}-{path}-attachments"])
    if user is not None:
        spec["messages"][-1]["content"] = user
    work = tmp_path / path
    work.mkdir()
    return _user_parts(provider, pg.run(spec, monkeypatch, work))


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_stream_and_the_nonstream_path_encode_attachments_alike(provider, monkeypatch, tmp_path):
    stream = _run(provider, "stream", monkeypatch, tmp_path)
    call = _run(provider, "nonstream", monkeypatch, tmp_path)
    assert stream == call


@pytest.mark.parametrize("path", ["stream", "nonstream"])
@pytest.mark.parametrize("provider", ["openai", "xai", "custom"])
def test_an_upload_and_a_base64_image_reach_every_openai_compatible_family_as_data_urls(
        provider, path, monkeypatch, tmp_path):
    parts = _run(provider, path, monkeypatch, tmp_path)
    urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    assert urls == ["data:image/png;base64,iVBORyBnb2xkZW4gcGhvdG8gYnl0ZXM=",
                    "data:image/jpeg;base64,QkFTRTY0"]
    assert {"type": "text", "text": "[Image could not be loaded]"} in parts   # the missing upload


@pytest.mark.parametrize("path", ["stream", "nonstream"])
def test_openai_still_fetches_a_public_image_url_itself(path, monkeypatch, tmp_path):
    user = [{"type": "text", "text": "this one"},
            {"type": "image_url", "image_url": {"url": "https://images.example.com/cat.png"}}]
    parts = _run("openai", path, monkeypatch, tmp_path, user=user)
    assert parts == user


@pytest.mark.parametrize("path", ["stream", "nonstream"])
@pytest.mark.parametrize("provider", ["anthropic", "google"])
def test_the_base64_image_is_forwarded_on_both_paths(provider, path, monkeypatch, tmp_path):
    parts = _run(provider, path, monkeypatch, tmp_path)
    if provider == "google":
        assert {"inline_data": {"mime_type": "image/jpeg", "data": "QkFTRTY0"}} in parts
    else:
        assert {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": "QkFTRTY0"}} in parts


@pytest.mark.parametrize("path", ["stream", "nonstream"])
@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_webp_upload_is_typed_image_webp(provider, path, monkeypatch, tmp_path):
    monkeypatch.setattr(pg, "UPLOADS", {**pg.UPLOADS, "shot.webp": b"RIFF golden webp"})
    user = [{"type": "text", "text": "this one"},
            {"type": "image_url", "image_url": {"url": "/ui/uploads/shot.webp"}}]
    parts = _run(provider, path, monkeypatch, tmp_path, user=user)
    data = "UklGRiBnb2xkZW4gd2VicA=="
    expected = {
        "google": {"inline_data": {"mime_type": "image/webp", "data": data}},
        "anthropic": {"type": "image", "source": {"type": "base64", "media_type": "image/webp", "data": data}},
    }.get(provider, {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{data}"}})
    assert expected in parts


@pytest.mark.parametrize("path", ["stream", "nonstream"])
def test_claude_is_told_about_a_document_it_cannot_read(path, monkeypatch, tmp_path):
    monkeypatch.setattr(pg, "UPLOADS", {**pg.UPLOADS, "plan.docx": b"PK golden docx"})
    user = [{"type": "text", "text": "read it"},
            {"type": "document_url", "document_url": {"url": "/ui/uploads/plan.docx"}}]
    parts = _run("anthropic", path, monkeypatch, tmp_path, user=user)
    assert parts[1]["type"] == "text" and "plan.docx" in parts[1]["text"]
