"""M1-T7 / F1: voice agents can drive computer use and do a one-shot status check.

F1 (fire-and-forget): the shared CU guidance no longer tells the model to poll
``get_task_status`` in a loop inside the streaming turn (each poll re-prefills the
whole conversation and can burn the tool-iteration cap before the task finishes).
Group 3's live task pill surfaces progress/completion out-of-band, so the model
now announces the launch, points the user at the pill, and ENDS the turn;
``get_task_status`` remains exposed for a single explicit "is it done?" check.


Three things guarded here:

  1. GROUPS — ``get_task_status`` is now exposed to all three voice surfaces
     (groups ``realtime`` / ``gemini_live`` / ``grok_live``) IN ADDITION to its
     original four (``chat`` / ``chat_cu`` / ``phone`` / ``mcp``). Without it,
     a voice agent could launch a ``use_computer`` task and then had no way to
     learn whether it finished. It must serialize to the FLAT shape each voice
     surface accepts (plain string params — no enum / nested object).

  2. PROMPTS — the COMPUTER CONTROL guidance is a SINGLE shared constant
     (``Orchestrator.routes.voice_prompts.CU_CONTROL_BLOCK``) interpolated into
     all SIX voice ``system_instructions`` branches (a ``custom_role`` branch +
     a default branch, in each of the three routes). Because there is one copy,
     drift is structurally impossible and there is no per-branch content guard
     to keep in sync. The invariant is now: exactly six branches, each
     interpolating the constant; plus a content check on the constant itself.

  3. RENDER — every one of the six branches actually EVALUATES without raising.
     This is the guard the earlier AST-only test could not provide: a plain
     f-string could carry a valid-but-broken interpolation that py_compile and
     ast.parse both accept and that only raises at a live voice session. Four of
     the six branches are otherwise never runtime-rendered by any test.
"""
import ast
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.tools.tool_registry import (
    get_openai_realtime_tools,
    get_gemini_live_tools,
    reset_cache,
)
from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK

ROUTES = pathlib.Path(__file__).resolve().parents[1] / "routes"

VOICE_ROUTE_FILES = [
    "realtime_routes.py",
    "gemini_live_routes.py",
    "grok_live_routes.py",
]

CONSTANT_NAME = "CU_CONTROL_BLOCK"

# Every semantic piece the shared CU_CONTROL_BLOCK must teach, keyed to a tuple
# of distinctive substrings that prove it (ALL must be present). Because the
# block is ONE constant, this content check runs once against the single source
# of truth — not per-branch. Note the SYNCHRONOUS piece asserts the LITERAL
# brace shape ``{"success": false, ...}``: the constant is a plain string, so it
# carries real braces with no f-string escaping.
REQUIRED_PIECES = {
    "async task_id claim": ("ASYNCHRONOUS", "returns a task_id"),
    "fire-and-forget: announce, point at the task pill, end the turn": (
        "watch its progress on the live task pill", "END your turn"),
    "no in-turn poll loop; one explicit check is allowed": (
        "Do NOT poll get_task_status in a loop", "check get_task_status(task_id) once"),
    "stall directive (SNAP-3675)": ("ACTUALLY CALL IT",),
    "model CLASS list (all five)": ("opus", "sonnet", "fable", "gemini", "gpt"),
    "SYNCHRONOUS structured-failure handling": ('{"success": false, ...}', '"available"', '"retryable"'),
    "ASYNCHRONOUS single-tenant display refusal (surfaced by the pill / one-shot check)": ("single-tenant", "FAILED"),
}


@pytest.fixture(autouse=True)
def _fresh():
    """Pick up on-disk schema edits around every test (both cache layers)."""
    registry.invalidate_cache()
    reset_cache()
    yield
    registry.invalidate_cache()
    reset_cache()


# ---------------------------------------------------------------------------
# 1. get_task_status groups + flat serialization
# ---------------------------------------------------------------------------

def test_get_task_status_has_all_voice_groups_and_keeps_originals():
    tool = registry.get_tool("get_task_status")
    assert tool is not None
    groups = set(tool["groups"])
    # Original four must survive.
    assert {"chat", "chat_cu", "phone", "mcp"}.issubset(groups), (
        f"lost an original group: {groups}")
    # Three voice groups added.
    assert {"realtime", "gemini_live", "grok_live"}.issubset(groups), (
        f"missing a voice group: {groups}")


def _assert_flat(param_schema):
    """No enum / nested object anywhere in the properties (the voice-flat shape
    the OpenAI-realtime and Gemini-live flatteners require)."""
    for name, p in param_schema.get("properties", {}).items():
        assert "enum" not in p, f"{name} carries an enum (rejected by voice surfaces)"
        assert "properties" not in p, f"{name} is a nested object (rejected)"
        assert p.get("type") != "object", f"{name} is an object type (rejected)"


def test_get_task_status_in_openai_realtime_group_and_flat():
    tools = {t["name"]: t for t in get_openai_realtime_tools("realtime")}
    assert "get_task_status" in tools
    _assert_flat(tools["get_task_status"]["parameters"])


def test_get_task_status_in_grok_live_group_and_flat():
    # Grok Live reuses the OpenAI-realtime flat shape.
    tools = {t["name"]: t for t in get_openai_realtime_tools("grok_live")}
    assert "get_task_status" in tools
    _assert_flat(tools["get_task_status"]["parameters"])


def test_get_task_status_in_gemini_live_group_and_flat():
    decls = get_gemini_live_tools("gemini_live")
    # Gemini Live shape: [{"functionDeclarations": [...]}].
    fns = {f["name"]: f for f in decls[0]["functionDeclarations"]}
    assert "get_task_status" in fns
    _assert_flat(fns["get_task_status"]["parameters"])


# ---------------------------------------------------------------------------
# 2a. Content of the single source of truth (checked ONCE, not per branch)
# ---------------------------------------------------------------------------

def test_cu_control_block_teaches_every_piece():
    for piece, needles in REQUIRED_PIECES.items():
        missing = [n for n in needles if n not in CU_CONTROL_BLOCK]
        assert not missing, (
            f"CU_CONTROL_BLOCK is missing the {piece!r} guidance (absent substrings: {missing})")


# ---------------------------------------------------------------------------
# 2b. Structural guard: exactly six branches, each interpolating the constant
# ---------------------------------------------------------------------------

def _system_instruction_joinedstrs(path):
    """Every ``system_instructions = f"..."`` assignment's JoinedStr node in a file."""
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "system_instructions" in targets and isinstance(node.value, ast.JoinedStr):
                out.append(node.value)
    return out


def _interpolates(joinedstr, name):
    """True iff the f-string contains a ``{name}`` replacement field (a bare Name)."""
    for v in joinedstr.values:
        if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id == name:
            return True
    return False


def test_exactly_six_system_instruction_branches():
    total = sum(len(_system_instruction_joinedstrs(ROUTES / fn)) for fn in VOICE_ROUTE_FILES)
    assert total == 6, f"expected 6 system_instructions branches across voice routes, found {total}"


def test_all_six_branches_interpolate_the_cu_control_constant():
    for fn in VOICE_ROUTE_FILES:
        nodes = _system_instruction_joinedstrs(ROUTES / fn)
        assert len(nodes) == 2, f"{fn}: expected 2 system_instructions branches, got {len(nodes)}"
        for i, js in enumerate(nodes):
            assert _interpolates(js, CONSTANT_NAME), (
                f"{fn} branch #{i} does not interpolate {{{CONSTANT_NAME}}} — "
                f"the COMPUTER CONTROL guidance would be absent from that branch")


# ---------------------------------------------------------------------------
# 3. Render guard: all six branches evaluate without raising, and the block
#    actually lands in the payload sent upstream (proves interpolation worked
#    AND that the literal braces survived — the exact failure that only shows
#    up at a live voice session otherwise).
# ---------------------------------------------------------------------------

@pytest.fixture
def _stub_fossil(monkeypatch):
    """Stub build_fossil_context in all three route modules (mirrors
    test_live_models) so configure_* never touches real snapshots."""
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})

    for mod in VOICE_ROUTE_FILES:
        modname = mod[:-3]  # strip ".py"
        monkeypatch.setattr(f"Orchestrator.routes.{modname}.build_fossil_context", _stub)


def _make_openai_session():
    s = MagicMock()
    s.openai_ws = MagicMock()
    s.openai_ws.send = AsyncMock()
    s.provenance = {}
    s.context_injected = False
    return s


def _make_gemini_session():
    s = MagicMock()
    s.gemini_ws = MagicMock()
    s.gemini_ws.send = AsyncMock()
    s.resumption_handle = None
    s.provenance = {}
    s.context_injected = False
    s.voice = ""
    return s


def _make_grok_session():
    s = MagicMock()
    s.grok_ws = MagicMock()
    s.grok_ws.send = AsyncMock()
    s.provenance = {}
    s.context_injected = False
    s.voice = ""
    return s


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _assert_cu_block_sent(send_mock, label):
    """The CU block rendered into at least one payload sent upstream."""
    assert send_mock.await_count >= 1, f"{label}: configure sent nothing upstream"
    blob = ""
    for call in send_mock.await_args_list:
        raw = call.args[0]
        try:
            blob += "\n".join(_iter_strings(json.loads(raw)))
        except Exception:
            blob += str(raw)
    assert "COMPUTER CONTROL:" in blob, f"{label}: CU block heading not in any sent payload"
    assert "watch its progress on the live task pill" in blob, (
        f"{label}: fire-and-forget task-pill directive missing")
    # Literal braces survived (no f-string escaping artifact like {{ or a
    # ValueError from a broken replacement field).
    assert '{"success": false, ...}' in blob, f"{label}: literal failure-shape braces missing/mangled"


@pytest.mark.asyncio
async def test_all_six_voice_branches_render_without_raising(_stub_fossil):
    from Orchestrator.routes.realtime_routes import configure_openai_session
    from Orchestrator.routes.gemini_live_routes import configure_gemini_session
    from Orchestrator.routes.grok_live_routes import configure_grok_session

    CUSTOM = "You are a friendly outbound-call assistant for a dentist office."

    # realtime — default branch (custom_role="") then custom_role branch
    s = _make_openai_session()
    await configure_openai_session(session=s, operator="op", voice="ash")
    _assert_cu_block_sent(s.openai_ws.send, "realtime/default")
    s = _make_openai_session()
    await configure_openai_session(session=s, operator="op", voice="ash", custom_role=CUSTOM)
    _assert_cu_block_sent(s.openai_ws.send, "realtime/custom_role")

    # gemini_live — default then custom_role
    s = _make_gemini_session()
    await configure_gemini_session(session=s, operator="op", voice="Charon")
    _assert_cu_block_sent(s.gemini_ws.send, "gemini_live/default")
    s = _make_gemini_session()
    await configure_gemini_session(session=s, operator="op", voice="Charon", custom_role=CUSTOM)
    _assert_cu_block_sent(s.gemini_ws.send, "gemini_live/custom_role")

    # grok_live — default then custom_role
    s = _make_grok_session()
    await configure_grok_session(session=s, operator="op", voice="Ara")
    _assert_cu_block_sent(s.grok_ws.send, "grok_live/default")
    s = _make_grok_session()
    await configure_grok_session(session=s, operator="op", voice="Ara", custom_role=CUSTOM)
    _assert_cu_block_sent(s.grok_ws.send, "grok_live/custom_role")
