"""Frontier-driven device-control ReAct loop (M2 — the cloud "brain"; the MVP).

Moves the device-control brain OFF the on-device Gemma and ONTO a cloud frontier model.
The phone (built in M1) is the hands: it emits ``observation`` frames (a11y tree + device
capability + optional screenshot) UP over the Tailscale 8765 channel and executes
``action`` frames DOWN. This module is the server-side ReAct loop that closes that circuit:

    device ──observation──▶ frontier model ──action──▶ device ──action_result(+obs?)──▶ …

Per step it (1) pulls an ``observation`` from the phone, (2) feeds the a11y tree (+ optional
screenshot) to the frontier model, (3) parses the model's chosen action, (4) HYBRID-GROUNDS
it to an ``action.json`` frame (element-preferred, coordinate fallback — see
``frontier_grounding``), (5) POSTs it to ``/action``, and (6) loops until the model declares
done or a per-action / per-turn / session timeout fires. It never crashes on a malformed
model action — it grounds what it can, feeds a benign failure back, and re-observes.

Correlation with M1's REAL endpoints (RemoteControlServer.kt):
  * observations  ← ``GET /stream/{task_id}?operator=…`` — M1 emits ONE real ``observation``
    frame per open (opening it also flips the on-device consent banner on via
    RemoteSessionBus), so the loop pulls one frame per step.
  * actions       → ``POST /action`` — body = a full ``action.json`` frame plus the transport
    envelope ``{task_id, operator}``; returns an ``action_result`` that MAY embed a fresh
    follow-on ``observation``.
  * The ``task_id`` (one UUID the loop mints) correlates both halves; both are operator-scoped
    + tailnet-gated by M1. To avoid the double-observe race the M0 README flags, exactly ONE
    observation is consumed per step: the embedded ``action_result.observation`` when present,
    else a ``/stream`` pull.

Model: the plan targets Gemini 3.5 Flash ``environment:'mobile'``. That environment is NOT in
the installed google-genai SDK (1.64.0 exposes only ENVIRONMENT_BROWSER), and the only
computer-use model reachable with the box's key is ``gemini-2.5-computer-use-preview-10-2025``.
So M2 SUBSTITUTES that available Gemini CU model, driven in the proven Android configuration
(ENVIRONMENT_BROWSER + browser-only functions excluded + custom Android function declarations,
0-999 coords) — mirroring ``Orchestrator/gemini_cu/agent_loop.py``. The provider + model are
config-knobbed (``[computer_use] frontier_provider`` / ``frontier_model``), never hardcoded, so
the real mobile model (or M7's Claude/OpenAI) drops in without a code change. Safety gates live
on the PHONE (M1/M4) — this loop never re-implements them server-side.

Structured error kinds surfaced to the caller (data.error_kind): no_device / lost_contact /
timeout / model_error / config_error / max_steps / invalid_argument, plus the TERMINAL device
states the loop short-circuits on instead of burning model calls (F2): stopped (the user hit
STOP on the device) and accessibility_off (the device's accessibility service is disabled).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from Orchestrator.frontier_grounding import (
    GroundedAction,
    derive_device_dimensions,
    snap_swipe_to_coordinate,
    snap_to_element,
)

# The device capability the loop assumes BEFORE the first observation lands. A phone's
# defaults (all paths available). Overridden by the observation's authoritative
# device_capability the instant the first frame arrives (fresh-box-safe — no hardcoded host).
DEFAULT_CAPABILITY: Dict = {
    "formFactor": "phone",
    "hasScreenshot": True,
    "supportsCoordinateGesture": True,
    "displayId": 0,
}


# ── Config knobs (read live from [computer_use]; all overridable, none hardcoded) ─────
def _cfg(kind: str, key: str, fallback):
    try:
        from Orchestrator.config import CFG
        getter = getattr(CFG, kind)
        return getter("computer_use", key, fallback=fallback)
    except Exception:
        return fallback


def _frontier_provider() -> str:
    try:
        from Orchestrator.config import CU_FRONTIER_PROVIDER
        return (CU_FRONTIER_PROVIDER or "gemini").strip()
    except Exception:
        return "gemini"


def _frontier_model() -> str:
    try:
        from Orchestrator.config import CU_FRONTIER_MODEL
        return (CU_FRONTIER_MODEL or "").strip() or "gemini-2.5-computer-use-preview-10-2025"
    except Exception:
        return "gemini-2.5-computer-use-preview-10-2025"


def _per_action_secs() -> float:
    return float(_cfg("getfloat", "frontier_per_action_secs", 10.0))


def _per_turn_secs() -> float:
    return float(_cfg("getfloat", "frontier_per_turn_secs", 30.0))


def _session_base_secs() -> float:
    return float(_cfg("getfloat", "frontier_session_base_secs", 180.0))


def _session_max_secs() -> float:
    return float(_cfg("getfloat", "frontier_session_max_secs", 600.0))


def _session_step_extend_secs() -> float:
    return float(_cfg("getfloat", "frontier_session_step_extend_secs", 20.0))


def _max_steps() -> int:
    return int(_cfg("getint", "frontier_max_steps", 40))


def _retry_max() -> int:
    return int(_cfg("getint", "frontier_retry_max", 2))


def _retry_backoff_secs() -> float:
    return float(_cfg("getfloat", "frontier_retry_backoff_secs", 0.5))


def _clip(value, limit: int = 300) -> str:
    s = str(value)
    return s if len(s) <= limit else s[:limit - 1] + "…"


# ── Result contract ──────────────────────────────────────────────────────────────────
@dataclass
class FrontierResult:
    """Outcome of a frontier loop run. The executor wraps this into a ToolResult."""
    success: bool
    message: str
    error_kind: Optional[str] = None
    steps: int = 0
    device: Optional[str] = None
    final_text: Optional[str] = None

    def to_data(self) -> Dict:
        d: Dict = {"steps": self.steps}
        if self.device:
            d["device"] = self.device
        if self.error_kind:
            d["error_kind"] = self.error_kind
        return d


@dataclass
class Decision:
    """A driver's per-step decision. kind ∈ {action, done}."""
    kind: str
    model_action: Optional[Dict] = None   # normalized {op, …} (provider-neutral)
    text: str = ""


# ── Normalized model action → action.json frame (hybrid grounding) ───────────────────
def _merge_capability(prev: Dict, observation: Dict) -> Dict:
    """The observation's device_capability is authoritative once it arrives."""
    cap = observation.get("device_capability")
    if isinstance(cap, dict) and cap:
        return cap
    return prev


def _ground(model_action: Dict, observation: Dict, capability: Dict) -> GroundedAction:
    """Map a provider-neutral model action + the current observation → an action frame.

    Element-preferred grounding for taps/typing (snap the 0-999 coord to a stable a11y
    node), direction-based scroll (XR-portable), semantic global/open_app, and coordinate
    swipe for drags/long-presses. Returns ``GroundedAction`` with ``frame=None`` when the
    action can't be actuated on this device (unsupported op, coordinate action on a
    coordinate-less device, or an empty tree) — the loop feeds that back to the model.
    """
    tree: List[Dict] = observation.get("ui_tree") or []
    device_wh = derive_device_dimensions(observation)
    supports_coord = bool(capability.get("supportsCoordinateGesture", True))
    op = model_action.get("op")

    if op == "tap":
        return snap_to_element((model_action.get("x", 500), model_action.get("y", 500)),
                               tree, device_wh, editable=False, supports_coordinate=supports_coord)
    if op == "type":
        return snap_to_element((model_action.get("x", 500), model_action.get("y", 500)),
                               tree, device_wh, editable=True,
                               text=model_action.get("text", ""), supports_coordinate=supports_coord)
    if op == "long_press":
        x, y = model_action.get("x", 500), model_action.get("y", 500)
        return snap_swipe_to_coordinate((x, y), (x, y), device_wh, duration_ms=800,
                                        supports_coordinate=supports_coord)
    if op == "drag":
        return snap_swipe_to_coordinate(
            (model_action.get("x", 0), model_action.get("y", 0)),
            (model_action.get("x2", 0), model_action.get("y2", 0)),
            device_wh, supports_coordinate=supports_coord)
    if op == "scroll":
        direction = str(model_action.get("direction", "down")).lower()
        return GroundedAction(frame={"type": "scroll", "direction": direction}, method="global")
    if op == "open_app":
        pkg = (model_action.get("app") or "").strip()
        if not pkg:
            return GroundedAction(frame=None, method="none")
        return GroundedAction(frame={"type": "open_app", "package": pkg}, method="global")
    if op == "back":
        return GroundedAction(frame={"type": "global_action", "action": "back"}, method="global")
    if op == "home":
        return GroundedAction(frame={"type": "global_action", "action": "home"}, method="global")
    if op == "recents":
        return GroundedAction(frame={"type": "global_action", "action": "recents"}, method="global")
    if op == "press_key":
        # F1: a coordinate-free key press (enter submits the focused field via
        # ACTION_IME_ENTER; back/home/recents reuse performGlobalAction). Capability-safe on
        # every form factor (no coordinate) → never gated. Unknown keys are ungroundable.
        key = str(model_action.get("key", "enter")).strip().lower()
        if key not in PRESS_KEYS:
            return GroundedAction(frame=None, method="none")
        return GroundedAction(frame={"type": "press_key", "key": key}, method="global")
    # wait is handled in the loop; unsupported / unknown → ungroundable
    return GroundedAction(frame=None, method="none")


# The press_key `key` enum (mirrors docs/schema/action.json press_key.key + the on-device
# RemoteActionChannel PRESS_KEYS). `enter` is the critical one (submit a focused field).
PRESS_KEYS = ("enter", "back", "home", "recents", "tab", "delete")


def _wire_frame(task_id: str, operator: str, variant: Dict) -> Dict:
    """Wrap a grounded action VARIANT in the on-wire transport envelope: the ``msg`` kind plus
    the ``task_id`` / ``operator`` framing keys (see docs/schema/README.md → Transport
    envelope). The device strips the framing and validates the variant on its own."""
    frame: Dict = {"msg": "action", "task_id": task_id, "operator": operator}
    frame.update(variant)
    return frame


# ── Phone I/O seams (lazy httpx import; monkeypatched in tests) ───────────────────────
async def _pull_observation(base_url: str, task_id: str, operator: str,
                            timeout_secs: float) -> Optional[Dict]:
    """GET /stream/{task_id}?operator=… — read the first SSE ``data:`` frame → observation.

    M1 emits one real observation frame per open (then closes); opening it also marks the
    on-device session active (consent banner). Returns the parsed observation dict, or
    ``None`` when the stream carried only SSE comment lines (no observation source wired /
    no device state). Raises on a transport error (the retry layer handles it).
    """
    import httpx  # lazy: keep module import dependency-light + test-friendly
    url = f"{base_url}/stream/{task_id}"
    async with httpx.AsyncClient(timeout=timeout_secs) as client:
        async with client.stream("GET", url, params={"operator": operator}) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload:
                        try:
                            return json.loads(payload)
                        except (json.JSONDecodeError, ValueError):
                            return None
    return None


async def _post_action(base_url: str, action_frame: Dict, timeout_secs: float) -> Dict:
    """POST /action with a full action frame; return the ``action_result`` JSON dict."""
    import httpx
    url = f"{base_url}/action"
    async with httpx.AsyncClient(timeout=timeout_secs) as client:
        resp = await client.post(url, json=action_frame)
        resp.raise_for_status()
        return resp.json()


async def _pull_with_retry(base_url: str, task_id: str, operator: str,
                           deadline: float) -> Optional[Dict]:
    """Pull an observation with bounded retries. ``None`` on exhaustion (transport failure
    OR persistent empty stream). CancellationError propagates (BaseException, not caught)."""
    attempts = _retry_max()
    for i in range(attempts + 1):
        try:
            obs = await asyncio.wait_for(
                _pull_observation(base_url, task_id, operator, _per_action_secs()),
                _per_action_secs() + 2.0)
            if obs is not None:
                return obs
        except Exception:
            pass
        if time.monotonic() >= deadline or i >= attempts:
            break
        await asyncio.sleep(_retry_backoff_secs() * (i + 1))
    return None


async def _post_with_retry(base_url: str, frame: Dict, deadline: float) -> Optional[Dict]:
    """POST an action with bounded retries. ``None`` on exhaustion."""
    attempts = _retry_max()
    for i in range(attempts + 1):
        try:
            return await asyncio.wait_for(
                _post_action(base_url, frame, _per_action_secs()),
                _per_action_secs() + 2.0)
        except Exception:
            pass
        if time.monotonic() >= deadline or i >= attempts:
            break
        await asyncio.sleep(_retry_backoff_secs() * (i + 1))
    return None


# ── Terminal-state detection (F2 — stop burning model calls on an unrecoverable device) ─
def _terminal_error(result: Optional[Dict]) -> Optional[Tuple[str, str]]:
    """Inspect an ``action_result`` for a TERMINAL device state that no amount of re-planning
    can recover, so the loop short-circuits instead of feeding the failure back and spending
    another model call. Keys on the machine-detectable ``error`` code first, then a stable
    ``detail`` phrase. Returns ``(error_kind, message)`` to short-circuit, or ``None`` to keep
    going (an ordinary, recoverable action failure like ``node_not_found`` is fed back, not
    terminal). Never raises.

    Terminal states (F2):
      * ``error == "not_wired"``          → ``no_device`` (the device's control channel is not bound);
      * detail contains ``stopped``       → ``stopped`` (the user hit STOP on the device);
      * ``not_enabled`` / ``accessibility`` → ``accessibility_off`` (the a11y service is off).
    """
    if not isinstance(result, dict) or result.get("success"):
        return None
    error = str(result.get("error") or "").strip().lower()
    detail = str(result.get("detail") or "").strip().lower()
    if error == "not_wired":
        return ("no_device",
                "Device control isn't wired on this device (no action dispatcher bound). "
                "Cannot drive it remotely.")
    if "stopped by user" in detail or "stopped" in detail:
        return ("stopped", "Remote control was stopped on the device.")
    if error == "not_enabled" or "accessibility" in detail or "not enabled" in detail:
        return ("accessibility_off",
                "The device's accessibility service is off — enable it on the device to "
                "allow remote control.")
    return None


async def _aclose_driver(driver) -> None:
    """Best-effort close of a driver's transport (MINOR 5). No-op when the driver has no
    ``aclose`` (e.g. the test FakeDriver). Never raises."""
    aclose = getattr(driver, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        pass


# ── Driver factory (Gemini today; M7 adds Claude/OpenAI behind this seam) ─────────────
def _make_driver(provider: str, model: str, task: str, operator: str, capability: Dict):
    """Build the per-provider frontier driver. M2 = Gemini only; M7 registers the rest."""
    p = (provider or "gemini").strip().lower()
    if p in ("gemini", "google"):
        return GeminiMobileDriver(model, task, operator, capability)
    raise NotImplementedError(
        f"frontier provider '{provider}' is not supported in M2 (Gemini only; "
        f"M7 adds Claude/OpenAI)")


# ── The loop ──────────────────────────────────────────────────────────────────────────
async def run_frontier_loop(device_base_url: str, task: str, operator: str,
                            model: Optional[str] = None,
                            capability: Optional[Dict] = None) -> FrontierResult:
    """Drive ``device_base_url`` (a phone's RemoteControlServer base URL) to complete ``task``.

    Async ReAct loop over the M1 ``/stream`` (observations) + ``/action`` (actions) channels,
    correlated by a minted ``task_id``. Returns a :class:`FrontierResult`; never raises on a
    device/model failure (structured error_kind instead). CancellationError propagates.
    """
    task = (task or "").strip()
    if not task:
        return FrontierResult(False, "task is required (what to do on the device).",
                              error_kind="invalid_argument")

    base_url = (device_base_url or "").rstrip("/")
    if not base_url:
        return FrontierResult(False, "device_base_url is required.", error_kind="invalid_argument")

    provider = _frontier_provider()
    model = model or _frontier_model()
    capability = dict(capability) if capability else dict(DEFAULT_CAPABILITY)
    task_id = uuid.uuid4().hex

    hardcap = time.monotonic() + _session_max_secs()
    deadline = min(hardcap, time.monotonic() + _session_base_secs())

    # 1) initial observation — also opens the on-device session (consent banner on M1).
    obs = await _pull_with_retry(base_url, task_id, operator, deadline)
    if obs is None:
        return FrontierResult(
            False,
            "No device observation — the device may be offline, off the tailnet, or has no "
            "observation source wired. Cannot drive it remotely.",
            error_kind="no_device", device=base_url, steps=0)
    capability = _merge_capability(capability, obs)

    try:
        driver = _make_driver(provider, model, task, operator, capability)
    except Exception as e:
        return FrontierResult(False, f"Could not start the frontier driver: {_clip(e)}",
                              error_kind="config_error", device=base_url, steps=0)

    last_result: Optional[Dict] = None
    steps = 0
    max_steps = _max_steps()

    try:
        while steps < max_steps:
            if time.monotonic() >= deadline:
                return FrontierResult(
                    False, f"Timed out (session budget) after {steps} step(s) while working.",
                    error_kind="timeout", device=base_url, steps=steps)
            steps += 1

            # 2) the model decides the next action from the current screen.
            try:
                decision = await asyncio.wait_for(driver.next_action(obs, last_result),
                                                  _per_turn_secs())
            except asyncio.TimeoutError:
                return FrontierResult(
                    False, f"The model did not respond within {int(_per_turn_secs())}s (step {steps}).",
                    error_kind="timeout", device=base_url, steps=steps)
            except Exception as e:
                return FrontierResult(False, f"The model call failed (step {steps}): {_clip(e)}",
                                      error_kind="model_error", device=base_url, steps=steps)

            if decision.kind == "done":
                return FrontierResult(True, decision.text or "Done.", device=base_url,
                                      steps=steps, final_text=decision.text)

            model_action = decision.model_action or {}
            op = model_action.get("op")

            # A model-requested wait is local (no phone action) but STILL a function call in the
            # provider's protocol → it produces a last_result so the driver can respond next turn.
            if op == "wait":
                await asyncio.sleep(min(float(model_action.get("seconds", 0) or 0), 5.0))
                nxt = await _pull_with_retry(base_url, task_id, operator, deadline)
                if nxt is None:
                    return FrontierResult(False, "Lost contact with the device while waiting.",
                                          error_kind="lost_contact", device=base_url, steps=steps)
                obs = nxt
                capability = _merge_capability(capability, obs)
                last_result = {"success": True, "detail": "waited"}
                continue

            # 3) hybrid-ground the model action → an action.json frame.
            grounded = _ground(model_action, obs, capability)
            if grounded.frame is None:
                # Ungroundable (unsupported op / coordinate action on a coordinate-less device /
                # empty tree). Report a benign failure back to the model + re-observe; never crash.
                last_result = {"success": False,
                               "detail": f"could not perform '{op}' on this device — re-plan from the screen"}
                continue

            # 4) dispatch it to the phone (variant + transport envelope).
            result = await _post_with_retry(
                base_url, _wire_frame(task_id, operator, grounded.frame), deadline)
            if result is None:
                return FrontierResult(
                    False, f"Lost contact with the device dispatching '{op}' (step {steps}).",
                    error_kind="lost_contact", device=base_url, steps=steps)
            last_result = result

            # F2: a TERMINAL device state (not_wired / stopped / accessibility_off) is
            # unrecoverable — short-circuit with the right error_kind instead of spending another
            # model call re-planning against a wall. (An ordinary recoverable failure, e.g.
            # node_not_found, is NOT terminal and is fed back to the model as last_result.)
            terminal = _terminal_error(result)
            if terminal is not None:
                kind, message = terminal
                return FrontierResult(False, message, error_kind=kind, device=base_url, steps=steps)

            # Adaptive session budget: each successful step earns more time (bounded by the hard
            # cap). The Gemma cold-load is gone, so the base is generous and progress extends it.
            if result.get("success"):
                deadline = min(hardcap, max(deadline, time.monotonic() + _session_step_extend_secs()))

            # F1: honor type_text_at(press_enter=true) — after the text is SET, submit the field
            # with a follow-on Enter keypress (its own press_key/enter action.json frame). Its
            # outcome becomes the step's last_result and forces a fresh re-observation (the type
            # frame's embedded observation now predates the submit → stale).
            followon_fired = False
            if op == "type" and model_action.get("press_enter") and result.get("success"):
                enter_grounded = _ground({"op": "press_key", "key": "enter"}, obs, capability)
                if enter_grounded.frame is not None:
                    enter_result = await _post_with_retry(
                        base_url, _wire_frame(task_id, operator, enter_grounded.frame), deadline)
                    if enter_result is None:
                        return FrontierResult(
                            False, f"Lost contact submitting after typing (step {steps}).",
                            error_kind="lost_contact", device=base_url, steps=steps)
                    last_result = enter_result
                    followon_fired = True
                    terminal = _terminal_error(enter_result)
                    if terminal is not None:
                        kind, message = terminal
                        return FrontierResult(False, message, error_kind=kind, device=base_url, steps=steps)
                    if enter_result.get("success"):
                        deadline = min(hardcap,
                                       max(deadline, time.monotonic() + _session_step_extend_secs()))

            # 5) exactly ONE observation per step: prefer the embedded follow-on (saves a
            # round-trip; avoids the double-observe race), else pull a fresh /stream frame. A
            # press_enter submit invalidates the type frame's embedded observation → always pull.
            embedded = None if followon_fired else result.get("observation")
            if isinstance(embedded, dict) and embedded:
                obs = embedded
            else:
                nxt = await _pull_with_retry(base_url, task_id, operator, deadline)
                if nxt is None:
                    return FrontierResult(
                        False, f"Lost contact with the device after '{op}' (step {steps}).",
                        error_kind="lost_contact", device=base_url, steps=steps)
                obs = nxt
            capability = _merge_capability(capability, obs)

        return FrontierResult(
            False, f"Reached the step limit ({max_steps}) without completing the task.",
            error_kind="max_steps", device=base_url, steps=steps)
    finally:
        # MINOR 5: release the driver's httpx transport (best-effort) on every exit path.
        await _aclose_driver(driver)


# ── Gemini mobile driver ──────────────────────────────────────────────────────────────
# Gemini CU (Android) function name → provider-neutral normalized action op. Mirrors the
# proven Android branch in Orchestrator/gemini_cu/agent_loop.py.
def _normalize_gemini_call(name: str, args: Dict) -> Dict:
    a = dict(args or {})
    if name == "click_at":
        return {"op": "tap", "x": a.get("x", 500), "y": a.get("y", 500)}
    if name == "type_text_at":
        # F1: honor press_enter — the loop emits the type frame THEN a follow-on press_key(enter)
        # so a "type → submit" flow (search box, chat input) completes without a coordinate.
        return {"op": "type", "x": a.get("x", 500), "y": a.get("y", 500),
                "text": a.get("text", ""), "clear": bool(a.get("clear_before_typing", False)),
                "press_enter": bool(a.get("press_enter", False))}
    if name == "long_press_at":
        return {"op": "long_press", "x": a.get("x", 500), "y": a.get("y", 500)}
    if name == "drag_and_drop":
        return {"op": "drag", "x": a.get("x", 0), "y": a.get("y", 0),
                "x2": a.get("destination_x", 0), "y2": a.get("destination_y", 0)}
    if name == "key_combination":
        # F1: map Gemini's key_combination (e.g. "Enter", "Return", "Tab", "Backspace") onto a
        # phone press_key. A modifier combo ("Control+A") has no Android press_key equivalent →
        # unsupported (the model re-plans). Enter/Return is the critical one (submit).
        return _key_combination_to_op(a.get("keys", ""))
    if name == "scroll_at":
        return {"op": "scroll", "direction": a.get("direction", "down")}
    if name == "scroll_down":
        return {"op": "scroll", "direction": "down"}
    if name == "scroll_up":
        return {"op": "scroll", "direction": "up"}
    if name == "open_app":
        return {"op": "open_app", "app": a.get("app_name") or a.get("package") or ""}
    if name == "go_home":
        return {"op": "home"}
    if name in ("go_back_android", "go_back"):
        return {"op": "back"}
    if name == "wait_5_seconds":
        return {"op": "wait", "seconds": 5}
    if name == "hover_at":
        return {"op": "wait", "seconds": 0}
    return {"op": "unsupported", "name": name}


# Gemini key_combination `keys` (single, unmodified) → press_key `key`. Only single keys with a
# phone equivalent map; a modifier combo has none (→ unsupported). Enter/Return → submit.
_KEY_COMBINATION_MAP = {
    "enter": "enter", "return": "enter", "\n": "enter",
    "tab": "tab",
    "backspace": "delete", "delete": "delete", "del": "delete",
    "escape": "back", "esc": "back", "back": "back",
    "home": "home",
}


def _key_combination_to_op(keys) -> Dict:
    """Map a Gemini ``key_combination`` keys string → a normalized op. A single mappable key
    (Enter/Return/Tab/Backspace/Delete/Escape/Back/Home) → ``press_key``; anything else
    (a modifier combo like ``Control+A``, or an unknown key) → ``unsupported`` so the loop
    feeds it back and the model re-plans rather than mis-actuating."""
    raw = str(keys or "").strip()
    key = _KEY_COMBINATION_MAP.get(raw.lower())
    if key is None:
        return {"op": "unsupported", "name": f"key_combination:{raw}"}
    return {"op": "press_key", "key": key}


def _android_fn_declarations(types, *, coordinate: bool = True):
    """Custom Android function declarations (mirrors gemini_cu/agent_loop._get_android_...).

    ``coordinate`` = whether the device supports coordinate gestures. When false (XR) the
    coordinate-ONLY custom function ``long_press_at`` is PRUNED — it can only be grounded to a
    coordinate_swipe, which the device refuses on a coordinate-less form factor, so offering it
    would just invite a guaranteed-to-fail call."""
    decls = [
        types.FunctionDeclaration(
            name="open_app",
            description="Open an Android app by its package name, e.g. 'com.google.android.apps.maps'.",
            parameters={"type": "object",
                        "properties": {"app_name": {"type": "string",
                                                    "description": "App package id to launch"}},
                        "required": ["app_name"]}),
        types.FunctionDeclaration(
            name="go_home", description="Go to the Android home screen.",
            parameters={"type": "object", "properties": {}}),
        types.FunctionDeclaration(
            name="go_back_android", description="Press the Android back button.",
            parameters={"type": "object", "properties": {}}),
        types.FunctionDeclaration(
            name="scroll_down", description="Scroll down (see content below).",
            parameters={"type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}),
        types.FunctionDeclaration(
            name="scroll_up", description="Scroll up (see content above).",
            parameters={"type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}),
    ]
    if coordinate:
        decls.insert(1, types.FunctionDeclaration(
            name="long_press_at",
            description="Long-press at a coordinate (0-999) on the screen.",
            parameters={"type": "object",
                        "properties": {"x": {"type": "integer", "description": "X (0-999)"},
                                       "y": {"type": "integer", "description": "Y (0-999)"}},
                        "required": ["x", "y"]}))
    return decls


def _build_mobile_tools(types, capability: Dict) -> list:
    """The CU tool config for the Android/mobile substitution (ENVIRONMENT_BROWSER + custom
    Android functions), mirroring the proven Android branch. When a real mobile environment
    lands in the SDK this is the single place to switch to ``ENVIRONMENT_MOBILE``.

    ``capability`` prunes coordinate-only functions on a device that can't actuate by
    coordinate (XR, ``supportsCoordinateGesture=false``): the predefined ``drag_and_drop`` is
    excluded and the custom ``long_press_at`` is dropped, so the model is never offered a
    gesture the grounder can only reject."""
    supports_coord = bool(capability.get("supportsCoordinateGesture", True))
    from Orchestrator.gemini_cu.config import BROWSER_ONLY_FUNCTIONS
    excluded = list(BROWSER_ONLY_FUNCTIONS)
    if not supports_coord and "drag_and_drop" not in excluded:
        excluded.append("drag_and_drop")
    tools = [types.Tool(computer_use=types.ComputerUse(
        environment=types.Environment.ENVIRONMENT_BROWSER,
        excluded_predefined_functions=excluded))]
    tools.append(types.Tool(
        function_declarations=_android_fn_declarations(types, coordinate=supports_coord)))
    return tools


def _tree_text(observation: Dict) -> str:
    cap = observation.get("device_capability") or {}
    ff = cap.get("formFactor", "phone")
    lines = [f"SCREEN ELEMENTS (accessibility tree — {ff}; coordinates are normalized 0-999):"]
    tree = observation.get("ui_tree") or []
    for n in tree:
        if not isinstance(n, dict):
            continue
        flags = []
        if n.get("clickable"):
            flags.append("clickable")
        if n.get("editable"):
            flags.append("editable")
        if n.get("is_password"):
            flags.append("password")
        rid = n.get("resource_id") or ""
        rid_s = f" id={rid}" if rid else ""
        flag_s = f" [{','.join(flags)}]" if flags else ""
        text = (n.get("text") or "").replace("\n", " ")
        lines.append(f"[{n.get('node_id')}] {n.get('role', 'View')} "
                     f"\"{text}\"{rid_s} bounds={n.get('bounds', '')}{flag_s}")
    if len(lines) == 1:
        lines.append("(no actionable elements detected)")
    return "\n".join(lines)


def _screenshot_bytes(observation: Dict) -> Optional[bytes]:
    shot = observation.get("screenshot")
    if not shot:
        return None
    import base64
    try:
        return base64.b64decode(shot, validate=False)
    except Exception:
        return None


def _summarize_result(last_result: Optional[Dict]) -> str:
    if not isinstance(last_result, dict):
        return "ok"
    if last_result.get("success"):
        return last_result.get("detail") or "ok"
    err = last_result.get("error")
    detail = last_result.get("detail") or "action failed"
    return f"{detail}" + (f" ({err})" if err else "")


def _mobile_system_prompt(capability: Dict) -> str:
    ff = capability.get("formFactor", "phone")
    has_shot = capability.get("hasScreenshot", True)
    vision = ("You see the screen as a screenshot AND a list of accessibility elements."
              if has_shot else
              "This device provides NO screenshot — reason ONLY from the accessibility "
              "element list. Do not request a screenshot.")
    return (
        f"You are the AI BlackBox device-control agent driving the user's own {ff} over a "
        "secure link. You complete the user's task one action at a time.\n\n"
        f"{vision} Coordinates are normalized 0-999 ((0,0)=top-left, (999,999)=bottom-right). "
        "The system snaps your coordinates onto the correct on-screen element automatically, "
        "so aim for the CENTER of the element you want.\n\n"
        "GUIDANCE:\n"
        "- Use open_app(app_name=<package id>) to launch apps (e.g. com.android.chrome).\n"
        "- Use go_home / go_back_android for navigation; scroll_down / scroll_up to reveal "
        "content.\n"
        "- Type into a field with type_text_at at the field's coordinate.\n"
        "- High-consequence actions (sending messages, payments, deletions) and passwords are "
        "gated ON THE DEVICE — the user confirms/enters them; proceed and let the device ask.\n"
        "- Work step by step and verify each result on the next screen. When the task is "
        "complete, STOP calling functions and reply with a brief natural-language summary of "
        "what you did.")


class GeminiMobileDriver:
    """Stateful Gemini CU driver for the mobile/Android substitution.

    Holds the multi-turn ``contents`` conversation. Each ``next_action`` sends the current
    screen (a11y tree text + optional screenshot) — as the first user turn, or as the
    function-response to the previous action — and returns the model's next :class:`Decision`.
    The google-genai SDK is imported lazily so this module imports without it (tests inject a
    fake driver via ``_make_driver``).
    """

    def __init__(self, model: str, task: str, operator: str, capability: Dict):
        from google import genai
        from google.genai import types
        from Orchestrator.config import GOOGLE_API_KEY

        self._types = types
        self.model = model
        self.task = task
        self.operator = operator
        self.capability = dict(capability or {})
        self._client = genai.Client(api_key=GOOGLE_API_KEY)
        self._contents: list = []
        self._pending_calls: List[str] = []
        self._config = types.GenerateContentConfig(
            tools=_build_mobile_tools(types, self.capability),
            system_instruction=_mobile_system_prompt(self.capability),
        )

    async def next_action(self, observation: Dict, last_result: Optional[Dict]) -> Decision:
        types = self._types
        parts = []
        if not self._contents:
            parts.append(types.Part.from_text(
                text=(f"TASK: {self.task}\n\nComplete this on the device, one action at a "
                      "time. When finished, reply with a brief summary and take no further "
                      "action.")))
        else:
            # Answer each pending function call (1 response per call — the provider protocol).
            summary = _summarize_result(last_result)
            for i, name in enumerate(self._pending_calls):
                resp = ({"url": f"android://{self.operator}", "result": summary} if i == 0
                        else {"url": f"android://{self.operator}",
                              "result": "skipped: one action at a time — re-plan from the screen"})
                parts.append(types.Part.from_function_response(name=name, response=resp))

        parts.append(types.Part.from_text(text=_tree_text(observation)))
        shot = _screenshot_bytes(observation)
        if shot:
            parts.append(types.Part.from_bytes(data=shot, mime_type="image/png"))

        self._contents.append(types.Content(role="user", parts=parts))
        self._pending_calls = []

        response = await self._client.aio.models.generate_content(
            model=self.model, contents=self._contents, config=self._config)

        if not response.candidates:
            return Decision(kind="done", text="The model returned no response.")
        candidate = response.candidates[0]
        content = getattr(candidate, "content", None)
        # MINOR 6: a safety-blocked / empty candidate has content=None (or no parts). Do NOT
        # append None to the conversation or dereference .parts (AttributeError) — end
        # gracefully with a clean done so the loop reports a benign completion, not a crash.
        if content is None or not getattr(content, "parts", None):
            fr = getattr(candidate, "finish_reason", None)
            return Decision(kind="done",
                            text="The model stopped without a further action"
                                 + (f" ({fr})." if fr else "."))
        self._contents.append(content)

        fn_calls = []
        texts = []
        for p in (content.parts or []):
            if getattr(p, "function_call", None):
                fn_calls.append(p.function_call)
            elif getattr(p, "text", None):
                texts.append(p.text)

        if not fn_calls:
            return Decision(kind="done", text="\n".join(texts).strip() or "Task complete.")

        self._pending_calls = [fc.name for fc in fn_calls]
        first = fn_calls[0]
        model_action = _normalize_gemini_call(first.name, dict(first.args or {}))
        return Decision(kind="action", model_action=model_action, text="\n".join(texts).strip())

    async def aclose(self) -> None:
        """MINOR 5: best-effort close of the genai client's underlying httpx async transport so
        a long-lived process doesn't leak sockets across many device-control runs. The SDK
        exposes no stable public close, so we probe the known private transports and swallow
        everything — never raises. Called from the loop's ``finally`` (via ``_aclose_driver``)."""
        client = getattr(self, "_client", None)
        if client is None:
            return
        for path in (("aio", "_api_client", "_async_httpx_client"),
                     ("_api_client", "_async_httpx_client")):
            obj = client
            try:
                for attr in path:
                    obj = getattr(obj, attr, None)
                    if obj is None:
                        break
                if obj is not None and hasattr(obj, "aclose"):
                    await obj.aclose()
                    return
            except Exception:
                pass
