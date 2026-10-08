"""Retired Claude model ids must never be hard-coded (2026-10-07).

claude-sonnet-4-20250514 sat hard-coded in the Twilio SMS handler (and its
dead twin, phone/sms_processor.py) after Anthropic retired it, so every SMS
reply 404'd with not_found_error. The static /models/anthropic fallback
catalog carried four more retired ids. These tests:

  1. AST-scan every Python source under Orchestrator/ (excluding tests/venv)
     for a string literal that IS a retired Claude id. Legacy-detection
     tables (`*_PREFIXES`, `*RETIRED*`, `*LEGACY*`, `*DEPRECATED*`
     assignments) are exempt — they match old ids, they never send them.
  2. Drive the Twilio SMS webhook and process_incoming_sms with aiohttp and
     the tool executor stubbed, asserting both Messages calls (initial +
     tool-result follow-up) take their model from config.ANTHROPIC_MODEL_DEFAULT
     and carry nothing the current models reject.
  3. Check the static fallback catalogs contain only live ids.
"""
import asyncio
import ast
import copy
import json
import re
import warnings
from pathlib import Path

import pytest

from Orchestrator import config
from Orchestrator.toolvault.context import ToolResult

ORCH_DIR = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {"tests", "venv", ".venv", "__pycache__", "site-packages", "node_modules"}

# Full-literal match. Retired: Claude 4.0 (claude-{sonnet,opus}-4, -4-0,
# -4-20250514), Opus 4.1 (claude-opus-4-1, dated or not), every claude-3*,
# claude-2*, claude-instant*. Current 4.5+ ids (claude-sonnet-4-5, ...) do
# not match.
RETIRED_ID_RX = re.compile(
    r"claude-(?:"
    r"(?:sonnet|opus)-4(?:-0|-1)?(?:-\d{8})?"
    r"|3(?:[.-][\w.-]*)?"
    r"|2(?:[.-][\w.-]*)?"
    r"|instant(?:-[\w.-]*)?"
    r")"
)
_EXEMPT_TABLE_RX = re.compile(r"PREFIX|RETIRED|LEGACY|DEPRECATED", re.IGNORECASE)
_QUOTED_CLAUDE_RX = re.compile(r"""["'](claude-[\w.-]+)["']""")

# /v1/models on this account, 2026-10-07.
LIVE_IDS = (
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1",
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
)


def _is_retired(model_id: str) -> bool:
    return RETIRED_ID_RX.fullmatch(model_id or "") is not None


def _iter_sources():
    for path in sorted(ORCH_DIR.rglob("*.py")):
        if _SKIP_DIRS.intersection(path.relative_to(ORCH_DIR).parts):
            continue
        yield path


def _target_names(node):
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for t in targets:
        if isinstance(t, ast.Name):
            yield t.id
        elif isinstance(t, ast.Attribute):
            yield t.attr


def _retired_literals(src: str):
    """(lineno, value) for every string literal that is a retired Claude id."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(src)
    except SyntaxError:
        # Unparseable (e.g. mid-edit): fall back to a plain quoted-literal scan.
        return [(src.count("\n", 0, m.start()) + 1, m.group(1))
                for m in _QUOTED_CLAUDE_RX.finditer(src) if _is_retired(m.group(1))]
    exempt = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if any(_EXEMPT_TABLE_RX.search(n) for n in _target_names(node)):
                exempt.update(id(c) for c in ast.walk(node.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in exempt and _is_retired(node.value)]


# ── 1. Source guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("model_id", [
    "claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-sonnet-4",
    "claude-opus-4", "claude-sonnet-4-0", "claude-opus-4-0", "claude-opus-4-1",
    "claude-opus-4-1-20250805", "claude-3-7-sonnet-20250219",
    "claude-3-5-haiku-20241022", "claude-3-5-sonnet-latest", "claude-3-opus-20240229",
    "claude-2.1", "claude-instant-1.2",
])
def test_retired_regex_matches_retired_ids(model_id):
    assert _is_retired(model_id)


@pytest.mark.parametrize("model_id", LIVE_IDS + (
    "claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5",   # live aliases
    "claude-mythos-5-1", "claude-mythos-5",
))
def test_retired_regex_spares_live_ids(model_id):
    assert not _is_retired(model_id)


def test_guard_scanner_flags_literals_and_exempts_prefix_tables():
    src = (
        'MODEL = "claude-sonnet-4-20250514"\n'
        'payload = {"model": "claude-opus-4-1"}\n'
        'LEGACY_PREFIXES = ("claude-3", "claude-opus-4-1")\n'
        'ok = "claude-opus-4-8"\n'
        '# "claude-3-7-sonnet-20250219" in a comment is not a literal\n'
    )
    assert _retired_literals(src) == [
        (1, "claude-sonnet-4-20250514"), (2, "claude-opus-4-1")]


def test_no_retired_claude_ids_hardcoded_in_orchestrator():
    offenders = []
    for path in _iter_sources():
        src = path.read_text(encoding="utf-8", errors="replace")
        if "claude-" not in src:
            continue
        rel = path.relative_to(ORCH_DIR.parent)
        offenders += [f"{rel}:{line}: {value!r}" for line, value in _retired_literals(src)]
    assert not offenders, (
        "Retired Claude model ids (404 not_found_error) are hard-coded — use "
        "config.ANTHROPIC_MODEL_DEFAULT or a live /v1/models id:\n  "
        + "\n  ".join(offenders))


def test_configured_default_is_not_retired():
    assert not _is_retired(config.ANTHROPIC_MODEL_DEFAULT)


# ── 2. SMS handlers take the model from config ───────────────────────────────

SENTINEL_MODEL = "claude-sentinel-from-config"


class _FakeResp:
    def __init__(self, body):
        self.status = 200
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_session_factory(captured, replies):
    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None, timeout=None):
            # Deep-copy: the handler appends to `messages` after this call.
            captured.append({"url": url, "payload": copy.deepcopy(json)})
            return _FakeResp(replies.pop(0))
    return _FakeSession


class _FakeExecutor:
    """Stands in for BlackBoxToolExecutor — no real tool (send_sms!) ever runs."""

    def __init__(self, *a, **kw):
        pass

    async def execute(self, tool_name, tool_input):
        return ToolResult(success=True, result="2026-10-07T12:00:00-04:00")


_TOOL_USE_REPLY = {
    "stop_reason": "tool_use",
    "content": [{"type": "tool_use", "id": "toolu_test", "name": "get_current_time", "input": {}}],
}
_TEXT_REPLY = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "It's noon."}]}


@pytest.fixture
def sms_env(monkeypatch):
    import aiohttp
    captured, replies = [], [_TOOL_USE_REPLY, _TEXT_REPLY]
    monkeypatch.setattr(aiohttp, "ClientSession", _fake_session_factory(captured, replies))
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", SENTINEL_MODEL)
    return captured


def _assert_valid_for_current_models(calls):
    assert len(calls) == 2, "expected initial call + tool-result follow-up"
    for call in calls:
        p = call["payload"]
        assert call["url"] == "https://api.anthropic.com/v1/messages"
        assert p["model"] == SENTINEL_MODEL, "model must come from config.ANTHROPIC_MODEL_DEFAULT"
        # Opus 4.7+ / the 5.x tier 400 on non-default sampling params and budget_tokens.
        for key in ("temperature", "top_p", "top_k", "thinking"):
            assert key not in p
        assert 0 < p["max_tokens"] <= 128000
        assert p["messages"][-1]["role"] == "user"   # no assistant prefill
        assert p["tools"] and all("name" in t for t in p["tools"])
    follow_up = calls[1]["payload"]["messages"]
    assert [m["role"] for m in follow_up] == ["user", "assistant", "user"]
    result_block = follow_up[-1]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_test"
    assert isinstance(result_block["content"], str)


def test_twilio_sms_webhook_uses_configured_default_model(sms_env, monkeypatch):
    from Orchestrator.routes import twilio_routes as tw
    monkeypatch.setattr(tw, "BlackBoxToolExecutor", _FakeExecutor)

    resp = asyncio.run(tw.twilio_sms_webhook(
        None, From="+15551234567", To="+15557654321",
        Body="what time is it?", MessageSid="SMtest"))

    _assert_valid_for_current_models(sms_env)
    assert b"It's noon." in resp.body


def test_sms_processor_uses_configured_default_model(sms_env, monkeypatch):
    from Orchestrator.phone import sms_processor
    from Orchestrator.tools import blackbox_tools
    monkeypatch.setattr(blackbox_tools, "BlackBoxToolExecutor", _FakeExecutor)

    reply = asyncio.run(sms_processor.process_incoming_sms(
        "+15551234567", "what time is it?", "SMS-4567"))

    _assert_valid_for_current_models(sms_env)
    assert reply == "It's noon."


# ── 3. Static fallback catalogs list only live ids ───────────────────────────

def test_anthropic_fallback_catalog_has_only_live_ids():
    from Orchestrator.routes.admin_routes import _FALLBACK_MODELS
    ids = [m["id"] for m in _FALLBACK_MODELS["anthropic"]]
    assert ids, "fallback catalog must not be empty"
    assert len(ids) == len(set(ids))
    assert not [i for i in ids if _is_retired(i)]
    # Not just "not retired": a typo'd or invented id 404s exactly the same way.
    assert set(ids) <= set(LIVE_IDS), sorted(set(ids) - set(LIVE_IDS))
    assert all(m["name"].startswith("Claude ") for m in _FALLBACK_MODELS["anthropic"])
    # The shipped default must be selectable when /v1/models is unreachable.
    assert "claude-opus-4-8" in ids


def test_cu_fallback_anthropic_entries_are_live_and_cu_capable():
    from Orchestrator.config import CU_MODEL_FILTERS
    from Orchestrator.routes.admin_routes import _FALLBACK_MODELS
    chat_ids = {m["id"] for m in _FALLBACK_MODELS["anthropic"]}
    cu_ids = [m["id"] for m in _FALLBACK_MODELS["computer-use"] if m["backend"] == "anthropic"]
    assert cu_ids
    for model_id in cu_ids:
        assert model_id in chat_ids, f"{model_id} is not a live Claude id"
        assert re.match(CU_MODEL_FILTERS["anthropic"], model_id)
