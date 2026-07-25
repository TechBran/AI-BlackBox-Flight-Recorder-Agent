"""Hermetic tests for the navigate_device ToolVault module (M2 — the one-shot nav intent).

The executor is loaded straight from its file (the same mechanism the registry uses).
mesh.resolve_device and the single HTTP POST are monkeypatched, so no tailscale, no sockets
and no real phone are touched. What these pin down:

  * the schema is structurally valid (the CI gate's own validator) and advertises the params;
  * device resolution precedence — explicit `device` > ctx.origin_device_id > operator primary
    (all threaded to mesh.resolve_device, which owns the rule);
  * the ONE-SHOT wire shape — exactly one POST of a single {msg:action, type:intent,
    name:navigate} frame to <node>:8765/action, and NO frontier ReAct loop / screenshot;
  * every structured error path (data["error_kind"]) with a speakable message.
"""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node, DeviceResolutionError
from Orchestrator.toolvault.schema_spec import validate_module_dict, KNOWN_GROUPS

_TOOL_DIR = (Path(__file__).resolve().parents[2]
             / "ToolVault" / "tools" / "navigate_device")
_spec = importlib.util.spec_from_file_location(
    "navigate_device_executor", _TOOL_DIR / "executor.py")
nd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nd)

SCHEMA = json.loads((_TOOL_DIR / "schema.json").read_text())

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")
CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091")
ORIGIN_CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091",
                         origin_device_id="brandon-fold6")

OK = {"msg": "action_result", "success": True, "detail": "started navigation"}


def _run(coro):
    return asyncio.run(coro)


def _resolves_to(node, seen=None):
    def _f(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return node
    return _f


def _raises(kind, message="nope", detail=None):
    def _f(**kwargs):
        raise DeviceResolutionError(kind, message, detail=detail)
    return _f


def _capture_post(calls, response=OK):
    async def _f(base_url, frame):
        calls.append({"base_url": base_url, "frame": frame})
        if isinstance(response, Exception):
            raise response
        return response
    return _f


@pytest.fixture(autouse=True)
def _no_react_loop(monkeypatch):
    """Hard guarantee of the design contract: navigate_device must NEVER start the frontier
    ReAct loop (that is control_device). Any call here fails the test."""
    def _boom(*a, **k):
        raise AssertionError("navigate_device must not run the frontier ReAct loop")
    monkeypatch.setattr(nd.frontier_agent_loop, "run_frontier_loop", _boom)


# ── schema ───────────────────────────────────────────────────────────────────────────
def test_schema_passes_the_ci_validator():
    errors = validate_module_dict(SCHEMA, "navigate_device", known_sources={"operators"})
    assert errors == [], errors


def test_schema_shape():
    assert SCHEMA["name"] == "navigate_device"
    assert SCHEMA["parameters"]["required"] == ["destination"]
    props = SCHEMA["parameters"]["properties"]
    assert set(props) == {"destination", "device", "mode", "avoid", "app"}
    # mirrors IntentActions.NAVIGATION_MODES (d/b/l/w) — 'l' is TWO-WHEELER, and
    # google.navigation has no transit mode, so 'transit' must NOT be offered here.
    assert props["mode"]["enum"] == ["driving", "walking", "bicycling", "two_wheeler"]
    assert set(SCHEMA["groups"]) <= KNOWN_GROUPS
    # chat covers the Portal/Android chat turn AND the cron job (cron runs a /chat turn);
    # mcp covers the remote tool server; phone covers the voice/SMS surface.
    assert {"chat", "mcp", "phone"} <= set(SCHEMA["groups"])


def test_registry_loads_the_module():
    from Orchestrator.toolvault import registry
    assert any(t["name"] == "navigate_device" for t in registry.load_canonical())
    assert registry.get_executor("navigate_device") is not None


# ── pure normalizers ─────────────────────────────────────────────────────────────────
def test_destination_normalization():
    assert nd.normalize_destination("  1600 Amphitheatre Pkwy \n Mountain View ") == \
        "1600 Amphitheatre Pkwy Mountain View"
    # 'lat, lon' collapses to the comma-literal form the on-device URI builder expects
    assert nd.normalize_destination("37.4224, -122.0841") == "37.4224,-122.0841"


@pytest.mark.parametrize("bad", ["", "   ", None, 42, "x" * 513, "a\x00b",
                                 "91.0,0.0", "0.0,181.0"])
def test_destination_rejects_junk(bad):
    assert nd.normalize_destination(bad) == ""


def test_app_normalization():
    assert nd.normalize_app(None) == ""
    assert nd.normalize_app("Google Maps") == "com.google.android.apps.maps"
    assert nd.normalize_app("waze") == "com.waze"
    assert nd.normalize_app("any") == "any"                  # implicit-intent sentinel
    assert nd.normalize_app("com.example.nav") == "com.example.nav"   # explicit package
    assert nd.normalize_app("that map thing") is None


def test_avoid_normalization():
    assert nd.normalize_avoid(None) == ""
    assert nd.normalize_avoid("tolls, ferries") == "tf"
    assert nd.normalize_avoid(["highways", "tolls"]) == "th"   # stable t/h/f order
    assert nd.normalize_avoid("bears") is None


# ── argument validation (no network touched) ─────────────────────────────────────────
def test_missing_destination(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _raises("no_device", "should not be called"))
    res = _run(nd.execute({}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
    assert res.data["field"] == "destination"


def test_malformed_destination_is_rejected_before_resolution(monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("must not resolve a device for a malformed destination")
    monkeypatch.setattr(nd.mesh, "resolve_device", _boom)
    res = _run(nd.execute({"destination": "95.0,0.0"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"


def test_bad_mode(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    res = _run(nd.execute({"destination": "CN Tower", "mode": "teleport"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
    assert res.data["field"] == "mode"


def test_bad_avoid(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    res = _run(nd.execute({"destination": "CN Tower", "avoid": "traffic"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
    assert res.data["field"] == "avoid"


# ── device resolution precedence ─────────────────────────────────────────────────────
def test_explicit_device_is_passed_as_the_target(monkeypatch):
    seen, calls = {}, []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE, seen))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    res = _run(nd.execute({"destination": "CN Tower", "device": "shop-tablet"}, ORIGIN_CTX))
    assert res.success is True
    assert seen["target_device_id"] == "shop-tablet"
    assert seen["origin_device_id"] == "brandon-fold6"   # both threaded; mesh owns precedence
    assert seen["operator"] == "Brandon"


def test_origin_device_used_when_no_explicit_device(monkeypatch):
    seen, calls = {}, []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE, seen))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    _run(nd.execute({"destination": "CN Tower"}, ORIGIN_CTX))
    assert seen["target_device_id"] is None
    assert seen["origin_device_id"] == "brandon-fold6"


def test_primary_fallback_when_no_origin(monkeypatch):
    seen, calls = {}, []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE, seen))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    _run(nd.execute({"destination": "CN Tower"}, CTX))   # box-originated: no origin device
    assert seen["target_device_id"] is None
    assert seen["origin_device_id"] is None              # mesh then falls back to PRIMARY


@pytest.mark.parametrize("kind", ["no_device", "no_primary_device", "invalid_target",
                                  "origin_mismatch"])
def test_resolution_errors_are_structured_and_speakable(kind, monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device",
                        _raises(kind, "mesh detail", detail={"operator": "Brandon"}))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == kind
    assert res.data["resolution_detail"] == "mesh detail"
    assert res.result == nd._RESOLUTION_MESSAGES[kind]
    assert len(res.result) > 40 and "navigat" in res.result.lower()


# ── the one-shot POST ────────────────────────────────────────────────────────────────
def test_one_shot_frame_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))

    res = _run(nd.execute({"destination": "1247 Main St W, Hamilton, ON",
                           "mode": "driving", "avoid": "tolls,ferries"}, CTX))
    assert res.success is True
    assert len(calls) == 1                                     # ONE request, no loop
    call = calls[0]
    assert call["base_url"] == f"http://{NODE.dns_name}:{nd.REMOTE_CONTROL_PORT}"
    frame = call["frame"]
    assert frame["msg"] == "action"
    assert frame["type"] == "intent"
    assert frame["name"] == "navigate"
    assert frame["operator"] == "Brandon"
    assert frame["task_id"]                                    # correlation id present
    assert frame["params"] == {"destination": "1247 Main St W, Hamilton, ON",
                               "mode": "d", "avoid": "tf"}
    # no screenshot / observation / session keys leak into the frame
    assert set(frame) == {"msg", "task_id", "operator", "type", "name", "params"}
    assert res.data["device"] == NODE.dns_name
    assert res.data["task_id"] == frame["task_id"]
    assert "Hamilton" in res.result


def test_optional_params_omitted_when_absent(monkeypatch):
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert calls[0]["frame"]["params"] == {"destination": "CN Tower"}


def test_free_text_destination_is_passed_through_unchanged(monkeypatch):
    """No geocoder on this box — a calendar address goes to Maps verbatim."""
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    addr = "Building C, 45 O'Connor St, Ottawa ON K1P 1A4"
    _run(nd.execute({"destination": addr}, CTX))
    assert calls[0]["frame"]["params"]["destination"] == addr


# ── delivery errors ──────────────────────────────────────────────────────────────────
class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


def test_device_unreachable(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent",
                        _capture_post([], response=OSError("connection refused")))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "lost_contact"
    assert NODE.dns_name in res.result


def test_operator_not_authorized(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=_HttpError(403)))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "not_authorized"
    assert res.data["http_status"] == 403
    assert "Brandon" in res.result


def test_bad_http_response(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=_HttpError(500)))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "bad_response"


def test_unreadable_response(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response="not json"))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "bad_response"


# ── device-reported failures ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("result,kind", [
    ({"msg": "action_result", "success": False, "error": "dispatch_failed",
      "detail": "no activity found"}, "maps_missing"),
    ({"msg": "action_result", "success": False, "error": "unknown_action",
      "detail": "unknown intent action: navigate"}, "unsupported_device"),
    ({"msg": "action_result", "success": False, "error": "invalid_argument",
      "detail": "destination required"}, "invalid_destination"),
    ({"msg": "action_result", "success": False, "detail": "user declined"}, "declined"),
    ({"msg": "action_result", "success": False, "error": "not_enabled",
      "detail": "service off"}, "device_error"),
])
def test_device_failures_map_to_error_kinds(result, kind, monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=result))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == kind
    assert res.data["device"] == NODE.dns_name
    assert res.data["destination"] == "CN Tower"
    # a speakable sentence the model can read aloud, not a bare error code
    assert len(res.result) > 40 and res.result.rstrip().endswith(".")


def test_maps_missing_message_names_the_device(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(
        [], response={"msg": "action_result", "success": False, "error": "dispatch_failed"}))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert NODE.dns_name in res.result and "Maps" in res.result


# ── provider dispatch (the documented control_phone gotcha) ──────────────────────────
# A directly-callable ToolVault tool is silently dead on any provider loop whose tool
# if/elif chain has no name-agnostic catch-all. These pin every loop that can call this
# tool, so "we forgot provider X" fails here instead of in front of a customer.
_PROVIDER_LOOPS = [
    "stream_openai_with_reasoning",      # OpenAI      (streaming)
    "stream_anthropic_with_thinking",    # Anthropic   (streaming)
    "stream_gemini_with_thinking",       # Gemini      (streaming)
    "stream_xai_with_reasoning",         # xAI / Grok  (streaming)
    "stream_custom_with_reasoning",      # custom LAN  (streaming)
    "call_openai",                       # OpenAI      (non-stream)
    "call_anthropic",                    # Anthropic   (non-stream)
    "call_gemini",                       # Gemini      (non-stream)
    "call_xai",                          # xAI / Grok  (non-stream)
    "call_custom",                       # custom LAN  (non-stream)
]


@pytest.mark.parametrize("fn_name", _PROVIDER_LOOPS)
def test_every_provider_loop_has_a_name_agnostic_catch_all(fn_name):
    import inspect
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(getattr(chat_routes, fn_name))
    assert "catch-all" in src.lower(), f"{fn_name} has no catch-all branch"
    assert "BlackBoxToolExecutor" in src, f"{fn_name} never reaches the ToolVault executor"
    # navigate_device is never named anywhere: dispatch must be name-agnostic.
    assert "navigate_device" not in src


@pytest.mark.parametrize("fn_name", _PROVIDER_LOOPS[:5])
def test_streaming_catch_alls_thread_the_origin_device(fn_name):
    """Origin-aware routing only works if the catch-all builds the executor with the turn's
    origin device — otherwise a phone-originated 'navigate me there' targets the primary."""
    import inspect
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(getattr(chat_routes, fn_name))
    assert "origin_device_id=_ORIGIN_DEVICE_ID.get()" in src


def test_dispatch_reaches_this_executor_by_name():
    """End-to-end through the shared seam every catch-all uses: an unknown name would come
    back 'Unknown tool'; reaching OUR argument validation proves the chain is wired."""
    from Orchestrator.tools import BlackBoxToolExecutor
    res = _run(BlackBoxToolExecutor(operator="Brandon").execute("navigate_device", {}))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"    # our message, not "Unknown tool"


# ── mode / app contract (mirrors IntentActions.kt exactly) ───────────────────────────
@pytest.mark.parametrize("word,code", [("driving", "d"), ("walking", "w"),
                                       ("bicycling", "b"), ("two_wheeler", "l"),
                                       ("motorcycle", "l"), ("bike", "b")])
def test_mode_words_map_to_the_google_navigation_letters(word, code, monkeypatch):
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    _run(nd.execute({"destination": "CN Tower", "mode": word}, CTX))
    assert calls[0]["frame"]["params"]["mode"] == code


@pytest.mark.parametrize("word", ["transit", "bus", "public transport"])
def test_transit_is_refused_not_silently_substituted(word, monkeypatch):
    """`l` is TWO-WHEELER, not transit. Mapping transit onto it would navigate the user
    the wrong way — refuse with a speakable explanation instead."""
    def _boom(**kwargs):
        raise AssertionError("must not resolve a device for an unsupported mode")
    monkeypatch.setattr(nd.mesh, "resolve_device", _boom)
    res = _run(nd.execute({"destination": "CN Tower", "mode": word}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
    assert res.data["field"] == "mode"
    assert "transit" in res.result.lower()


def test_named_app_is_sent_as_the_package(monkeypatch):
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    _run(nd.execute({"destination": "CN Tower", "app": "Waze"}, CTX))
    assert calls[0]["frame"]["params"]["package"] == "com.waze"


def test_unknown_app_is_rejected_before_the_wire(monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("must not resolve a device for an unknown app")
    monkeypatch.setattr(nd.mesh, "resolve_device", _boom)
    res = _run(nd.execute({"destination": "CN Tower", "app": "map thing"}, CTX))
    assert res.success is False
    assert res.data["field"] == "app"


def test_device_rejecting_a_named_app_reads_back_its_own_reason(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": False, "error": "invalid_argument",
        "detail": "navigation app not installed: com.waze"}))
    res = _run(nd.execute({"destination": "CN Tower", "app": "waze"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
    assert "com.waze" in res.result
