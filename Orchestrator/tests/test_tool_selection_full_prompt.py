#!/usr/bin/env python3
"""
test_tool_selection_full_prompt.py — regression tests for the cron-SMS
tool-selection bug.

THE BUG: a cron job with delivery=sms fires with a prompt shaped like
``[Scheduled Task: …]\n\n{long body}\n\nDELIVERY: …send an SMS to <target>…``.
Tool selection embedded ``_last_user_msg(messages)`` which TRUNCATED the prompt
to the FIRST 500 chars. For a long body the trailing SMS instruction fell past
char 500, never reached the embedder, ``send_sms`` scored below the 0.5
threshold, and the model got no SMS tool — so no SMS was ever sent.

THE FIX: tool selection uses the FULL last user message (the embedding provider
layer self-caps at EMBEDDING_MAX_CHARS=10000, far above 500, so no app-level cap
is needed). The mirror truncation in tasks.py is removed too, and the "google"
provider alias is normalized to gemini so the system-prompt tool instructions are
formatted for the right provider.

These tests exercise the REAL injector (the local embedding model is offline-
usable here, ~1s/query), plus a deterministic ``_last_user_msg`` assertion that
holds even if the embedder is unavailable.
"""

import pytest

from Orchestrator.routes.chat_routes import _last_user_msg


# A cron-SHAPED long prompt: the SMS DELIVERY instruction is APPENDED at the END,
# well past char 500, exactly like the scheduler's executor builds it.
_LONG_BODY = "Summarize today's standup notes and the key decisions made. " * 14
CRON_SMS_PROMPT = (
    "[Scheduled Task: Daily Standup Digest]\n\n"
    + _LONG_BODY
    + "\n\nDELIVERY: send an SMS to +15551234567 with the digest."
)


def _messages(text):
    return [{"role": "user", "content": text}]


# ---------------------------------------------------------------------------
# Deterministic: _last_user_msg no longer truncates (holds without the embedder)
# ---------------------------------------------------------------------------

def test_last_user_msg_returns_full_text_including_sms_tail():
    """The full last user message is returned — the trailing SMS instruction
    (past char 500) is preserved, NOT dropped by an old [:500] cap."""
    assert len(CRON_SMS_PROMPT) > 700  # genuinely long, tail past char 500
    out = _last_user_msg(_messages(CRON_SMS_PROMPT))
    assert out == CRON_SMS_PROMPT
    assert "send an SMS" in out
    # The old [:500] cap would have dropped the tail.
    assert "send an SMS" not in CRON_SMS_PROMPT[:500]


def test_last_user_msg_list_content_full_text():
    """List-shaped content (multi-part) is also returned untruncated."""
    long_text = "x" * 900 + " send an SMS now"
    msgs = [{"role": "user", "content": [{"type": "text", "text": long_text}]}]
    out = _last_user_msg(msgs)
    assert out == long_text
    assert "send an SMS now" in out


def test_short_prompt_unchanged_no_regression():
    """A normal short prompt round-trips byte-identically (interactive hot path)."""
    short = "what's the weather today?"
    assert _last_user_msg(_messages(short)) == short


# ---------------------------------------------------------------------------
# Real injector: full prompt selects send_sms; old-truncated prompt does not.
# ---------------------------------------------------------------------------

def _real_injector_or_skip():
    """Return get_injected_tool_names if the local embedding model is usable,
    else skip (CI without an offline embed model)."""
    try:
        from Orchestrator.toolvault.injector import get_injected_tool_names

        # Probe: a trivial query must not raise (needs the live/local embedder).
        get_injected_tool_names("send a text message")
        return get_injected_tool_names
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"real injector/embedding model unavailable: {e}")


def test_full_cron_prompt_selects_send_sms_real_injector():
    """The FULL cron-shaped prompt selects send_sms via the real hybrid search
    (threshold 0.5). This is what the fix delivers to cron jobs."""
    get_injected_tool_names = _real_injector_or_skip()
    selected = _last_user_msg(_messages(CRON_SMS_PROMPT))
    names = [n for n, _ in get_injected_tool_names(selected)]
    assert "send_sms" in names, (
        "send_sms must be selected from the FULL cron prompt; got: " + repr(names)
    )


def test_truncated_cron_prompt_drops_send_sms_real_injector():
    """Demonstrates the ROOT CAUSE: the OLD [:500] truncation drops the SMS tail
    so send_sms is NOT selected. This is the behavior the fix removes."""
    get_injected_tool_names = _real_injector_or_skip()
    old_truncated = CRON_SMS_PROMPT[:500]  # what _last_user_msg used to return
    assert "send an SMS" not in old_truncated
    names = [n for n, _ in get_injected_tool_names(old_truncated)]
    assert "send_sms" not in names, (
        "the truncated prompt must NOT select send_sms (proves the bug)"
    )


# ---------------------------------------------------------------------------
# Provider normalization: "google" formats as gemini (not openai_rest fallback)
# ---------------------------------------------------------------------------

def test_google_provider_formats_as_gemini():
    """inject_for_prompt(..., "google") must produce gemini-formatted tools
    (function_declarations wrapper), NOT the openai_rest fallback shape."""
    from Orchestrator.toolvault.injector import inject_for_prompt

    try:
        tools_google, instr_google = inject_for_prompt("send a text message", "google")
        tools_gemini, instr_gemini = inject_for_prompt("send a text message", "gemini")
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"real injector/embedding model unavailable: {e}")

    # Gemini format wraps everything in a single function_declarations dict.
    assert isinstance(tools_google, list) and tools_google
    assert all(isinstance(t, dict) for t in tools_google)
    assert any("function_declarations" in t for t in tools_google), (
        "google must use the gemini wrapper, got: " + repr(tools_google[:1])
    )
    # NOT the openai_rest shape ({"type": "function", ...}).
    assert not any(t.get("type") == "function" for t in tools_google)
    # And it matches the explicit "gemini" provider output shape.
    assert any("function_declarations" in t for t in tools_gemini)
