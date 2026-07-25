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
    assert set(props) == {"destination", "device", "mode", "avoid", "app", "delivery"}
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
                               "mode": "d", "avoid": "tf", "delivery": "auto"}
    # no screenshot / observation / session keys leak into the frame
    assert set(frame) == {"msg", "task_id", "operator", "type", "name", "params"}
    assert res.data["device"] == NODE.dns_name
    assert res.data["task_id"] == frame["task_id"]
    assert "Hamilton" in res.result


def test_optional_params_omitted_when_absent(monkeypatch):
    """mode/avoid/package are omitted when absent — but `delivery` is ALWAYS present, even
    as 'auto'. An absent key would let a device build read it as "legacy caller, launch
    directly", which is the silent-false-success M3 exists to kill."""
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls))
    _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert calls[0]["frame"]["params"] == {"destination": "CN Tower", "delivery": "auto"}


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


# ═══════════════════════════════════════════════════════════════════════════════════════
# M3 — delivery, and the honesty of the RESULT
#
# THE MEASURED DEFECT: with the app backgrounded on a real Fold 6, a navigate push returned
# {"success": true, "detail": "started navigation"} and Maps NEVER OPENED — targetSdk-36
# Background-Activity-Launch restriction discards startActivity WITHOUT throwing, so the
# actuator could not tell and reported success. A 7:30am cron would then have told Brandon
# "I've started navigation to your job site" while the phone sat in his pocket doing
# nothing. These tests exist so a notification-delivered result can never be mistaken for a
# completed navigation, and so the new device-side refusals get honest, speakable messages.
# ═══════════════════════════════════════════════════════════════════════════════════════

NOTIFY_OK = {"msg": "action_result", "success": True,
             "detail": "queued navigation notification"}
# An older (pre-M3) device build: it says nothing about delivery because it has no concept
# of one — it always launched directly, which is the launch Android silently discards.
LEGACY_OK = {"msg": "action_result", "success": True, "detail": "started navigation"}
SILENT_OK = {"msg": "action_result", "success": True}


# ── the param ────────────────────────────────────────────────────────────────────────
def test_schema_advertises_delivery_and_tells_the_model_when_to_use_notify():
    prop = SCHEMA["parameters"]["properties"]["delivery"]
    assert prop["enum"] == ["auto", "direct", "notify"]
    desc = prop["description"].lower()
    # the model must learn WHEN to pick notify, not just that it exists
    assert "auto" in desc and "default" in desc
    assert "scheduled" in desc and "unattended" in desc
    assert "notification" in desc
    # and that a notification is not a started navigation
    assert "not a started navigation" in desc or "not a started navigation" in \
        SCHEMA["returns"].lower()
    # every new error kind is documented for the model
    for kind in ("background_blocked", "notifications_blocked", "unsupported_delivery"):
        assert kind in SCHEMA["returns"]


def test_delivery_normalization():
    assert nd.normalize_delivery(None) == "auto"          # unattended default is the SAFE one
    assert nd.normalize_delivery("") == "auto"
    assert nd.normalize_delivery(" NOTIFY ") == "notify"
    assert nd.normalize_delivery("notification") == "notify"
    assert nd.normalize_delivery("direct") == "direct"
    assert nd.normalize_delivery("now") == "direct"
    assert nd.normalize_delivery("carrier pigeon") is None


def test_invalid_delivery_is_rejected_before_the_wire(monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("must not resolve a device for an invalid delivery mode")
    monkeypatch.setattr(nd.mesh, "resolve_device", _boom)
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "email"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
    assert res.data["field"] == "delivery"
    # speakable, and names the modes so the model can retry correctly
    assert "notify" in res.result and "direct" in res.result and "auto" in res.result


@pytest.mark.parametrize("word,wire", [("auto", "auto"), ("direct", "direct"),
                                       ("notify", "notify"), ("notification", "notify"),
                                       (None, "auto")])
def test_delivery_goes_on_the_wire(word, wire, monkeypatch):
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls, response=NOTIFY_OK))
    params = {"destination": "CN Tower"}
    if word is not None:
        params["delivery"] = word
    _run(nd.execute(params, CTX))
    # 'auto' is resolved ON THE DEVICE (only it knows if it is foreground), so it is sent
    # verbatim rather than decided here.
    assert calls[0]["frame"]["params"]["delivery"] == wire


# ── which delivery ACTUALLY happened ─────────────────────────────────────────────────
def test_resolve_delivery_prefers_the_devices_explicit_field():
    assert nd.resolve_delivery({"delivery": "notify", "detail": "started navigation"}) \
        == ("notify", True)
    assert nd.resolve_delivery({"delivery": "direct"}) == ("direct", True)


def test_resolve_delivery_falls_back_to_the_detail_phrase():
    """action_result.json is additionalProperties:false, so today the fixed success detail
    phrase is the ONLY channel the device has to say what it did."""
    # only an M3 build can post a notification, so the phrase alone is a knowing statement
    assert nd.resolve_delivery({"detail": "queued navigation notification"}) == ("notify", True)
    assert nd.resolve_delivery({"detail": "posted notification"}) == ("notify", True)
    # …and one naming the foreground is an M3 build explaining why it went direct
    assert nd.resolve_delivery({"detail": "started navigation (foreground)"}) == ("direct", True)


def test_the_legacy_direct_phrase_is_an_outcome_but_never_a_confirmation():
    """'started navigation' is M2's phrase. An M3 build launching from the foreground and a
    pre-M3 build that ignored `delivery` produce it identically — and the pre-M3 launch is
    the one Android discards in silence. So: direct, but NOT confirmed."""
    assert nd.resolve_delivery({"detail": "started navigation"}) == ("direct", False)


def test_resolve_delivery_is_empty_when_the_device_never_said():
    # "" is a REAL answer — an older build — and must never be guessed into a notification.
    assert nd.resolve_delivery({}) == ("", False)
    assert nd.resolve_delivery({"detail": ""}) == ("", False)
    assert nd.resolve_delivery("not a dict") == ("", False)


# ── THE ONE THAT MATTERS: a notification is not a navigation ─────────────────────────
def test_notification_delivery_cannot_be_read_as_a_started_navigation(monkeypatch):
    """The model reads res.result and res.data. NEITHER may suggest Maps is up and guiding:
    nothing moves until a human taps."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=NOTIFY_OK))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "notify"}, CTX))

    assert res.success is True                       # it WAS delivered — just not started
    assert res.data["delivery"] == "notify"          # what happened…
    assert res.data["delivery_requested"] == "notify"
    assert res.data["navigating"] is False           # …and the unmistakable flags
    assert res.data["awaiting_user_tap"] is True
    assert res.data["delivery_confirmed"] is True

    # the M2 "it's running" sentence must not appear
    low = res.result.lower()
    assert "is starting on" not in low
    assert "opening there now" not in low
    assert "notification" in low and "tap" in low
    assert "nothing is navigating yet" in low
    # the structured blob the model actually sees carries the same story
    assert '"navigating": false' in res.rich_result().lower()


def test_direct_delivery_says_maps_is_open(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=LEGACY_OK))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "direct"}, CTX))
    assert res.success is True
    assert res.data["delivery"] == "direct"
    assert res.data["navigating"] is True
    assert res.data["awaiting_user_tap"] is False
    assert "notification" not in res.result.lower()


def test_the_two_outcomes_never_share_a_sentence(monkeypatch):
    """Same request, two device outcomes — the prose must differ, or the model cannot tell
    Brandon which one to expect."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=LEGACY_OK))
    direct = _run(nd.execute({"destination": "CN Tower"}, CTX))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=NOTIFY_OK))
    notify = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert direct.result != notify.result
    assert direct.data["navigating"] != notify.data["navigating"]


def test_actual_delivery_wins_over_the_requested_one(monkeypatch):
    """'direct' was asked for; the device fell back to a notification because it was
    backgrounded. Report what HAPPENED — echoing the request is how the lie gets told."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=NOTIFY_OK))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "direct"}, CTX))
    assert res.data["delivery_requested"] == "direct"
    assert res.data["delivery"] == "notify"
    assert res.data["navigating"] is False
    assert "tap" in res.result.lower()


def test_auto_reports_whichever_path_the_device_took(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=NOTIFY_OK))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "auto"}, CTX))
    assert res.data["delivery"] == "notify" and res.data["navigating"] is False


# ── older device builds: never claim what cannot be verified ─────────────────────────
@pytest.mark.parametrize("response", [LEGACY_OK, SILENT_OK])
def test_notify_on_a_build_that_cannot_confirm_it_is_not_a_success(response, monkeypatch):
    """The worst case in miniature: success:true from a build that only launches directly.
    Claiming a notification (or a navigation) would be exactly the false success M3 kills."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=response))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "notify"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "unsupported_delivery"
    assert res.data["navigating"] is False
    assert res.data["awaiting_user_tap"] is False
    assert res.data["delivery"] is None
    assert res.data["delivery_confirmed"] is False
    assert "can't confirm" in res.result.lower()


def test_notify_that_the_device_knowingly_took_direct_is_a_real_success(monkeypatch):
    """The flip side: an M3-aware build that SAYS it launched directly (it was foreground,
    so the launch really did land) is trustworthy. Refusing that would be its own lie."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": True, "delivery": "direct",
        "detail": "started navigation"}))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "notify"}, CTX))
    assert res.success is True
    assert res.data["delivery"] == "direct"          # what happened, not what was asked
    assert res.data["delivery_requested"] == "notify"
    assert res.data["navigating"] is True


@pytest.mark.parametrize("delivery", ["auto", "direct"])
def test_older_build_still_reports_the_device_proven_direct_path(delivery, monkeypatch):
    """M2 is device-proven and must not regress: auto/direct on a silent build is the direct
    launch it performed — reported as direct, flagged unconfirmed rather than assumed."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=SILENT_OK))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": delivery}, CTX))
    assert res.success is True
    assert res.data["delivery"] == "direct"
    assert res.data["delivery_confirmed"] is False


# ── the new device-reported refusals ─────────────────────────────────────────────────
@pytest.mark.parametrize("response,kind", [
    # explicit error codes, should the closed action_result error enum ever be widened
    ({"msg": "action_result", "success": False, "error": "background_blocked",
      "detail": "background activity launch blocked"}, "background_blocked"),
    ({"msg": "action_result", "success": False, "error": "notifications_blocked",
      "detail": "notification permission denied"}, "notifications_blocked"),
    ({"msg": "action_result", "success": False, "error": "unsupported_delivery",
      "detail": "unsupported delivery mode"}, "unsupported_delivery"),
    # …and the fixed detail phrases, which is all the device can send TODAY
    ({"msg": "action_result", "success": False, "error": "dispatch_failed",
      "detail": "background launch restricted"}, "background_blocked"),
    ({"msg": "action_result", "success": False, "error": "dispatch_failed",
      "detail": "notifications disabled for this app"}, "notifications_blocked"),
    ({"msg": "action_result", "success": False, "error": "invalid_argument",
      "detail": "unknown delivery mode: notify"}, "unsupported_delivery"),
])
def test_new_delivery_failures_map_to_speakable_error_kinds(response, kind, monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response=response))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == kind
    assert res.data["device"] == NODE.dns_name
    assert res.data["destination"] == "CN Tower"
    assert res.data["delivery_requested"] == "auto"
    # a sentence the model reads aloud, not a bare code
    assert len(res.result) > 40 and res.result.rstrip().endswith(".")


def test_background_blocked_is_not_reported_as_missing_maps(monkeypatch):
    """A BAL refusal arrives as error='dispatch_failed' — the SAME code as "no navigation app
    installed". Reported as maps_missing it would send Brandon to reinstall an app that is
    already there while the real cause (a backgrounded launch) went unmentioned."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": False, "error": "dispatch_failed",
        "detail": "background activity launch blocked"}))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.data["error_kind"] == "background_blocked"
    assert "installed" not in res.result.lower()
    low = res.result.lower()
    assert "nothing is navigating" in low          # the honest part
    assert "notification" in low                   # …and the way out


def test_notifications_blocked_says_there_is_nothing_to_tap(monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": False, "error": "dispatch_failed",
        "detail": "notifications not permitted"}))
    res = _run(nd.execute({"destination": "CN Tower", "delivery": "notify"}, CTX))
    assert res.data["error_kind"] == "notifications_blocked"
    assert "nothing there to tap" in res.result.lower()


def test_a_decline_is_still_a_decline_not_a_delivery_error(monkeypatch):
    """The delivery branches must not swallow the benign user-decline path (success=false
    with no error), which stays error_kind 'declined'."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": False,
        "detail": "user declined navigation delivery"}))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.data["error_kind"] == "declined"


# ── the cross-language handshake with the device half ────────────────────────────────
# VERBATIM copies of the device's own wire strings (overlay/NavigationDelivery.kt consts),
# which classifyActuatorError lifts token-first into action_result.error. Held as literals
# rather than parsed out of the Kotlin so this file stays hermetic — the constants are
# `const val`s, so drift is a deliberate act on the device side and this is where it lands.
_DEV_BACKGROUND_BLOCKED = (
    "background_launch_blocked — the BlackBox app is not in the foreground, so Android "
    "discards the navigation launch and NOTHING opened. Open the app on the phone first, "
    "or use delivery=notify to send a tappable navigation notification.")
_DEV_NOTIF_PERMISSION = (
    "notification_permission_missing — the phone has not granted BlackBox permission to "
    "post notifications, so the navigation prompt was NOT delivered. Grant notifications "
    "for BlackBox on the device.")
_DEV_NOTIF_FAILED = (
    "notification_delivery_failed — the navigation notification could not be posted on "
    "this device, so nothing was delivered.")
_DEV_NOTIFY_POSTED = "navigation notification posted — waiting for the user to tap Navigate"
_DEV_DIRECT_OK = "started navigation"


@pytest.mark.parametrize("error,detail,kind", [
    ("background_launch_blocked", _DEV_BACKGROUND_BLOCKED, "background_blocked"),
    ("notification_permission_missing", _DEV_NOTIF_PERMISSION, "notifications_blocked"),
    ("notification_delivery_failed", _DEV_NOTIF_FAILED, "unsupported_delivery"),
    # …and the same details if a build reports them WITHOUT lifting the token into `error`
    ("dispatch_failed", _DEV_BACKGROUND_BLOCKED, "background_blocked"),
    ("dispatch_failed", _DEV_NOTIF_PERMISSION, "notifications_blocked"),
    ("dispatch_failed", _DEV_NOTIF_FAILED, "unsupported_delivery"),
])
def test_the_devices_real_wire_strings_classify_correctly(error, detail, kind, monkeypatch):
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": False, "error": error, "detail": detail}))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == kind
    assert res.data["device_detail"] == detail       # the device's own words are preserved


def test_background_refusal_is_not_misread_as_a_notification_permission_problem():
    """The device's background-blocked detail names the notification as the WAY OUT ("or use
    delivery=notify…") and contains the word "blocked". A loose notification test swallows
    it and sends the operator off to enable a permission that was never the problem."""
    low = _DEV_BACKGROUND_BLOCKED.lower()
    assert "notif" in low and "blocked" in low       # the collision is real, not theoretical
    res = nd._device_failure(
        {"success": False, "error": "background_launch_blocked",
         "detail": _DEV_BACKGROUND_BLOCKED}, "fold6", "CN Tower")
    assert res.data["error_kind"] == "background_blocked"


def test_the_devices_real_success_details_resolve_to_the_right_delivery():
    assert nd.resolve_delivery({"detail": _DEV_NOTIFY_POSTED}) == ("notify", True)
    # M2's phrase is unchanged on the device's DIRECT path — which is why it can never
    # confirm a notify request (an older build emits the identical string).
    assert nd.resolve_delivery({"detail": _DEV_DIRECT_OK}) == ("direct", False)


def test_an_invalid_delivery_that_reaches_the_device_reads_back_its_reason(monkeypatch):
    """deliveryRejectionReason() on the device — classified invalid_argument there. It must
    surface as a DELIVERY problem here, not as a bad destination."""
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post([], response={
        "msg": "action_result", "success": False, "error": "invalid_argument",
        "detail": ("invalid navigation delivery — use one of: auto (direct when the app is "
                   "open, otherwise a tappable notification), direct, notify")}))
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.data["error_kind"] == "unsupported_delivery"
    assert "invalid navigation delivery" in res.result


def test_end_to_end_notify_on_the_real_device_contract(monkeypatch):
    """The M3 cron path, exactly as the device will answer it: notification posted, and the
    model is told in three independent ways that nothing is navigating yet."""
    calls = []
    monkeypatch.setattr(nd.mesh, "resolve_device", _resolves_to(NODE))
    monkeypatch.setattr(nd, "_post_intent", _capture_post(calls, response={
        "msg": "action_result", "success": True, "detail": _DEV_NOTIFY_POSTED}))
    res = _run(nd.execute({"destination": "1247 Main St W, Hamilton, ON",
                           "delivery": "notify"}, CTX))
    assert calls[0]["frame"]["params"]["delivery"] == "notify"
    assert res.success is True
    assert res.data["delivery"] == "notify"
    assert res.data["navigating"] is False
    assert res.data["awaiting_user_tap"] is True
    assert "tap" in res.result.lower() and "nothing is navigating yet" in res.result.lower()
