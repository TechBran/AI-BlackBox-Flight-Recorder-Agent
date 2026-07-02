"""(M8.4) E2E smoke matrix — the regression net for the whole M0-M7 device-control contract.

A runnable pytest that drives the SERVER-SIDE frontier ReAct loop (the cloud "brain",
Orchestrator/frontier_agent_loop.py) against a MOCKED phone (records the `/action` wire frames,
returns canned `action_result`s) + a FAKE driver (canned action sequence), across the three
device profiles — phone / tablet / xr_headset — and asserts the emitted WIRE FRAMES + the
per-profile GATING. It is fully hermetic: no tailscale, no sockets, no real phone, no Gemini.

Canonical flows covered (× profiles):
  * open_app                       — the launch-intent path (capability-independent);
  * read → element_click           — a coordinate snaps to a stable a11y node (works on XR too);
  * type → submit                  — element_set_text THEN a follow-on press_key(enter);
  * navigation intent (back)        — a global_action frame (capability-independent);
  * high-consequence confirm         — the on-device confirm gate's allow (success) AND deny
                                      ("user declined") outcomes both flow back through the loop
                                      WITHOUT it crashing/terminating (deny is recoverable);
  * coordinate tap (phone/tablet)    — a tree-blind tap falls back to coordinate_tap …
  * … vs SKIP on XR                  — the SAME tap is ungroundable on a coordinate-less device
                                      (supportsCoordinateGesture=false) → NO frame dispatched;
  * capture-independent (XR)         — a full flow completes on tree-only observations (no
                                      screenshot ever on the wire).

Run: `pytest Orchestrator/validation/e2e_smoke_matrix.py -q`. Phone MUST pass to release.
"""
import asyncio

import pytest

from Orchestrator import frontier_agent_loop as fal
from Orchestrator.frontier_agent_loop import Decision


# ── device profiles (the device_capability advertised per class) ─────────────────────
PROFILES = {
    "phone":  {"formFactor": "phone",      "hasScreenshot": True,  "supportsCoordinateGesture": True,  "displayId": 0},
    "tablet": {"formFactor": "tablet",     "hasScreenshot": True,  "supportsCoordinateGesture": True,  "displayId": 0},
    "xr":     {"formFactor": "xr_headset", "hasScreenshot": False, "supportsCoordinateGesture": False, "displayId": 0},
}
ALL_PROFILES = list(PROFILES.keys())


def _node(node_id, bounds, *, clickable=False, editable=False, resource_id="", role="View"):
    return {"node_id": node_id, "role": role, "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


# A rich screen: full-screen root (sets device_wh=1080x2400) + one editable field + one submit.
RICH_TREE = [
    _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
    _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
    _node(2, "400,1600,700,1750", clickable=True, resource_id="app:id/submit"),
]
# A tree-blind screen (no actionable nodes) — the coordinate-fallback / XR-skip trigger.
BLIND_TREE = []


def _observation(profile: str, tree):
    cap = dict(PROFILES[profile])
    return {"msg": "observation", "ui_tree": list(tree), "device_capability": cap, "timestamp": 1}


class _FakeDriver:
    """A canned frontier driver: a list of normalized model actions, then done."""

    def __init__(self, script):
        self._script = list(script)
        self.seen_results = []

    async def next_action(self, observation, last_result):
        self.seen_results.append(last_result)
        if not self._script:
            return Decision(kind="done", text="done")
        item = self._script.pop(0)
        if item == "done":
            return Decision(kind="done", text="All set.")
        return Decision(kind="action", model_action=item)


class _MockPhone:
    """Records every posted `/action` frame; returns a fixed observation per pull and a scripted
    `action_result` per post (default success). Screenshot presence follows the profile — an XR
    observation NEVER carries one, proving capture-independence."""

    def __init__(self, observation, results=None):
        self.observation = observation
        self.results = list(results or [])
        self.posted = []
        self.observations_served = []

    async def pull(self, base_url, task_id, operator, timeout):
        self.observations_served.append(self.observation)
        return self.observation

    async def post(self, base_url, frame, timeout):
        self.posted.append(frame)
        return self.results.pop(0) if self.results else {"msg": "action_result", "success": True}


def _fast(monkeypatch):
    for name, val in (("_retry_max", 0), ("_retry_backoff_secs", 0.0), ("_per_action_secs", 5.0),
                      ("_per_turn_secs", 5.0), ("_session_base_secs", 300.0),
                      ("_session_max_secs", 600.0), ("_max_steps", 40)):
        monkeypatch.setattr(fal, name, lambda v=val: v)


def _drive(monkeypatch, profile, tree, script, results=None):
    """Run the frontier loop over a mock phone + fake driver; return (result, mock_phone)."""
    _fast(monkeypatch)
    phone = _MockPhone(_observation(profile, tree), results=results)
    monkeypatch.setattr(fal, "_pull_observation", phone.pull)
    monkeypatch.setattr(fal, "_post_action", phone.post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _FakeDriver(script))
    result = asyncio.run(fal.run_frontier_loop("http://phone:8765", "task", "Brandon"))
    return result, phone


def _frames_of_type(phone, t):
    return [f for f in phone.posted if f.get("type") == t]


# ── open_app (capability-independent) ────────────────────────────────────────────────
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_open_app(monkeypatch, profile):
    res, phone = _drive(monkeypatch, profile, RICH_TREE,
                        [{"op": "open_app", "app": "com.foo.bar"}, "done"])
    assert res.success is True
    frames = _frames_of_type(phone, "open_app")
    assert len(frames) == 1
    assert frames[0]["package"] == "com.foo.bar"
    assert frames[0]["msg"] == "action" and frames[0]["operator"] == "Brandon"


# ── read → element_click (a11y-node snap; works on XR too) ───────────────────────────
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_read_then_element_click(monkeypatch, profile):
    res, phone = _drive(monkeypatch, profile, RICH_TREE,
                        [{"op": "tap", "x": 500, "y": 700}, "done"])
    assert res.success is True
    clicks = _frames_of_type(phone, "element_click")
    assert len(clicks) == 1
    # snapped to the stable resource_id (never a raw coordinate on a rich tree).
    assert clicks[0]["resource_id"] == "app:id/submit"
    assert not _frames_of_type(phone, "coordinate_tap")


# ── type → submit (element_set_text + follow-on press_key enter) ─────────────────────
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_type_then_submit(monkeypatch, profile):
    res, phone = _drive(monkeypatch, profile, RICH_TREE,
                        [{"op": "type", "x": 500, "y": 300, "text": "hello", "press_enter": True}, "done"])
    assert res.success is True
    sets = _frames_of_type(phone, "element_set_text")
    assert len(sets) == 1 and sets[0]["resource_id"] == "app:id/field" and sets[0]["text"] == "hello"
    # the submit is a coordinate-free press_key(enter) — the "type → submit" flow.
    keys = _frames_of_type(phone, "press_key")
    assert len(keys) == 1 and keys[0]["key"] == "enter"


# ── navigation intent (global_action back) — capability-independent ──────────────────
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_navigation_intent_back(monkeypatch, profile):
    res, phone = _drive(monkeypatch, profile, RICH_TREE, [{"op": "back"}, "done"])
    assert res.success is True
    globals_ = _frames_of_type(phone, "global_action")
    assert len(globals_) == 1 and globals_[0]["action"] == "back"


# ── high-consequence confirm gate: allow AND deny both flow through (no crash) ────────
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_high_consequence_confirm_allow(monkeypatch, profile):
    """Verifies the LOOP's handling of an ALLOWED high-consequence confirm: fed the OUTCOME the
    on-device gate produces when the user taps Allow (action_result success=true), the loop treats
    the step as a success and completes. The on-device confirm GATE itself — that a high-consequence
    action raises the Allow/Deny overlay and fails safe to DENY — is covered by the Kotlin
    actuator/overlay tests, not here; this fixture supplies the gate's decided outcome."""
    res, phone = _drive(monkeypatch, profile, RICH_TREE,
                        [{"op": "tap", "x": 500, "y": 1675}, "done"],
                        results=[{"msg": "action_result", "success": True, "detail": "sent"}])
    assert res.success is True
    assert res.telemetry and res.telemetry[0]["success"] is True


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_high_consequence_confirm_deny(monkeypatch, profile):
    """Verifies the LOOP's handling of a DENIED high-consequence confirm: fed the OUTCOME the
    on-device gate produces when the user taps Deny ("user declined": success=false, NO error), the
    loop feeds it back as a RECOVERABLE, non-terminal result and finishes normally — it must NOT
    crash or short-circuit as a terminal error. As above, the gate FIRING is a Kotlin concern; this
    exercises the loop's reaction to the decided outcome."""
    res, phone = _drive(monkeypatch, profile, RICH_TREE,
                        [{"op": "tap", "x": 500, "y": 1675}, "done"],
                        results=[{"msg": "action_result", "success": False, "detail": "user declined"}])
    assert res.success is True                 # loop completed (the driver said done)
    assert res.error_kind is None              # a deny is NOT a terminal error
    assert res.telemetry and res.telemetry[0]["success"] is False   # recorded as a failed step


# ── coordinate tap: dispatched on phone/tablet, SKIPPED on XR (the key gating row) ───
@pytest.mark.parametrize("profile", ["phone", "tablet"])
def test_coordinate_tap_dispatched_on_flat_devices(monkeypatch, profile):
    # A tree-blind tap has no a11y node to snap to → falls back to a raw coordinate_tap.
    res, phone = _drive(monkeypatch, profile, BLIND_TREE, [{"op": "tap", "x": 500, "y": 500}, "done"])
    assert res.success is True
    taps = _frames_of_type(phone, "coordinate_tap")
    assert len(taps) == 1                       # the coordinate fallback fired


def test_coordinate_tap_skipped_on_xr(monkeypatch):
    # The SAME tree-blind tap on XR (supportsCoordinateGesture=false) is UNGROUNDABLE — the loop
    # never dispatches a coordinate frame; it feeds the benign failure back and the driver finishes.
    res, phone = _drive(monkeypatch, "xr", BLIND_TREE, [{"op": "tap", "x": 500, "y": 500}, "done"])
    assert res.success is True
    assert not _frames_of_type(phone, "coordinate_tap")   # NO coordinate frame reached the phone
    assert phone.posted == []                             # nothing dispatched at all


# ── capture-independent (XR): a full flow completes on tree-only observations ─────────
def test_capture_independent_xr(monkeypatch):
    """Capture-independence on XR: a multi-step flow completes on TREE-ONLY observations and the
    loop marshals ONLY capture-independent frames — open_app + element_click snapped to a11y nodes,
    with NO coordinate frame on the wire.

    This asserts the loop's ROUTING stays tree/element/intent-only on a capture-less device; it does
    NOT claim "zero screenshots on the wire" (the fixture observation simply never carries one, so
    that check would be a tautology). The load-bearing guarantees that the loop never REQUESTS a
    screenshot on XR (prompt forbids it + coordinate tools pruned) are covered by
    test_frontier_agent_loop.py's XR unit tests."""
    res, phone = _drive(monkeypatch, "xr", RICH_TREE,
                        [{"op": "open_app", "app": "com.foo"}, {"op": "tap", "x": 500, "y": 700}, "done"])
    assert res.success is True
    # completed on tree-only observations — the loop never needed a screenshot to proceed.
    assert all("screenshot" not in obs for obs in phone.observations_served)
    # capability-independent frames only — NO coordinate frame ever reached the phone.
    assert _frames_of_type(phone, "open_app") and _frames_of_type(phone, "element_click")
    assert not _frames_of_type(phone, "coordinate_tap")
    assert not _frames_of_type(phone, "coordinate_swipe")


# ── coverage report (informational; phone MUST pass to release) ──────────────────────
def test_matrix_coverage_summary():
    # A guard that the matrix exercises all three profiles across the canonical flows — if a
    # profile is dropped from PROFILES this fails loudly.
    assert set(PROFILES) == {"phone", "tablet", "xr"}
    assert PROFILES["xr"]["supportsCoordinateGesture"] is False and PROFILES["xr"]["hasScreenshot"] is False
    assert PROFILES["phone"]["supportsCoordinateGesture"] is True
