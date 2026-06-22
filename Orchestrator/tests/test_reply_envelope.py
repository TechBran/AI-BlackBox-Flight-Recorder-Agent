"""Tests for Orchestrator.reply_envelope.unwrap_reply_envelope.

Phase 0 of the pure-production reply/snapshot parsing plan: a defensive,
total-function unwrap that NEVER writes a parse-error sentinel into memory.
"""

import json

from Orchestrator.reply_envelope import unwrap_reply_envelope

# Sentinels that the OLD code embedded into searchable memory. The whole point
# of Phase 0 is that these must NEVER be returned.
_TRUNCATED_SENTINEL = "(Response was truncated - snapshot_perspective unavailable)"
_PARSE_SENTINEL = "(Could not parse snapshot_perspective)"


def _assert_no_sentinel(reply, perspective):
    for sentinel in ("(Could not parse", "(Response was truncated"):
        assert sentinel not in reply
        assert sentinel not in perspective


def test_valid_envelope():
    text = json.dumps({"ui_reply": "hi", "snapshot_perspective": "p"})
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == "hi"
    assert perspective == "p"


def test_fenced_envelope():
    inner = json.dumps({"ui_reply": "hello", "snapshot_perspective": "persp"})
    text = "```json\n" + inner + "\n```"
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == "hello"
    assert perspective == "persp"


def test_bare_fenced_envelope():
    inner = json.dumps({"ui_reply": "hey", "snapshot_perspective": "px"})
    text = "```\n" + inner + "\n```"
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == "hey"
    assert perspective == "px"


def test_triple_nested_envelope():
    # ui_reply value is itself a JSON string of an envelope (one level of nesting).
    inner = json.dumps({"ui_reply": "deep reply", "snapshot_perspective": "deep persp"})
    outer = json.dumps({"ui_reply": inner, "snapshot_perspective": "outer persp"})
    reply, perspective = unwrap_reply_envelope(outer)
    assert reply == "deep reply"
    # The inner envelope's perspective wins on the depth-1 unwrap.
    assert perspective == "deep persp"


def test_envelope_ui_reply_only():
    text = json.dumps({"ui_reply": "x"})
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == "x"
    assert perspective == ""


def test_non_envelope_dict_returns_original_text():
    text = json.dumps({"foo": 1})
    reply, perspective = unwrap_reply_envelope(text)
    # Not mistaken for a reply; stored as-is, no sentinel.
    assert reply == text
    assert perspective == ""
    _assert_no_sentinel(reply, perspective)


def test_plain_prose():
    prose = "Just a normal answer with no JSON at all."
    reply, perspective = unwrap_reply_envelope(prose)
    assert reply == prose
    assert perspective == ""


def test_malformed_truncated_json_stores_raw_no_sentinel():
    raw = '{"ui_reply": "partial answer that got cut off mid'
    reply, perspective = unwrap_reply_envelope(raw)
    assert perspective == ""
    _assert_no_sentinel(reply, perspective)
    # Never the old sentinels.
    assert reply != _TRUNCATED_SENTINEL
    assert reply != _PARSE_SENTINEL
    assert perspective != _TRUNCATED_SENTINEL
    assert perspective != _PARSE_SENTINEL


def test_empty_string():
    reply, perspective = unwrap_reply_envelope("")
    assert reply == ""
    assert perspective == ""


def test_none_input_no_raise():
    reply, perspective = unwrap_reply_envelope(None)
    assert reply == ""
    assert perspective == ""


def test_non_string_input_no_raise():
    reply, perspective = unwrap_reply_envelope(12345)
    # Falls into the "not a non-empty str" guard -> ("", "").
    assert reply == ""
    assert perspective == ""


def test_empty_ui_reply_falls_back_to_original():
    # Envelope with an empty ui_reply -> fall back to the original text (never empty + lossy).
    text = json.dumps({"ui_reply": "", "snapshot_perspective": "p"})
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == text
    assert perspective == "p"


def test_prose_then_envelope_extracts_envelope():
    # Inline (not whole-string) leak: prose then a JSON object.
    inner = json.dumps({"ui_reply": "extracted", "snapshot_perspective": "sp"})
    text = "Here is the answer: " + inner
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == "extracted"
    assert perspective == "sp"


def test_inline_fence_left_alone():
    # A fence that does NOT wrap the entire string should be left intact (no envelope present).
    text = "Use this code:\n```python\nprint('hi')\n```\nDone."
    reply, perspective = unwrap_reply_envelope(text)
    assert reply == text
    assert perspective == ""
