"""Executor for navigate_device — the ONE-SHOT navigation intent push (M2).

This is deliberately NOT control_device. control_device starts a server-side ReAct loop that
watches the screen and taps its way through Maps; this tool fires a SINGLE deterministic
Android intent and returns. No screenshot, no observation pull, no control session, no
on-device model — one HTTP POST to the device's Tailscale listener:

    POST http://<node>:8765/action
    {"msg": "action", "task_id": "<uuid>", "operator": "<op>",
     "type": "intent", "name": "navigate", "params": {"destination": ..., "mode"?, "avoid"?}}

On the device that routes RemoteActionChannel.parseAction -> AndroidPhoneController.dispatch
-> IntentActuator "navigate" -> a real ``google.navigation:q=`` deep link, i.e. turn-by-turn
guidance (not a map preview). ``destination`` is passed VERBATIM: Google Maps geocodes free
text itself, so a calendar address goes straight through and this box never needs a geocoder.

Device resolution is the SAME origin-aware rule control_device / stop_device_control use
(mesh.resolve_device): explicit ``device`` -> any tailnet node; else the ORIGIN device
(ctx.origin_device_id) but only if it belongs to this operator (never a silent retarget);
else the operator's PRIMARY device; else an error. The wire client is the frontier loop's
existing ``/action`` plumbing (frontier_agent_loop._wire_frame + _post_action) rather than a
second HTTP client.

DELIVERY (M3) — the honesty milestone. On targetSdk 36 Android SILENTLY DISCARDS an
activity launch made from the background: ``startActivity`` does not throw, so an M2-style
direct push to a pocketed phone came back ``success:true`` with Maps never opening. A
7:30am cron would then tell the operator "I've started navigation to your job site" while
the phone did nothing. The one launch Android still permits is one the USER TAPS, so a
notification is the only honest delivery for anything unattended. Hence ``delivery``:

  auto   (default) — the DEVICE decides: direct while it's foreground, notification otherwise
  direct           — open Maps now; only meaningful with the phone awake and in hand
  notify           — post a tappable notification; the right choice for scheduled/unattended

A direct launch and a queued notification are DIFFERENT OUTCOMES, so this executor reports
the delivery that ACTUALLY happened (``data["delivery"]``, never the one requested) plus
``data["navigating"]`` / ``data["awaiting_user_tap"]`` — a notification result can never be
read as a completed navigation. The device states the outcome in its success ``detail``
phrase (docs/schema/action_result.json pins ``additionalProperties:false`` and a CLOSED
error enum, so that phrase is the only channel it has today); an explicit ``delivery`` field
is honoured too if that schema is later widened.

Structured errors (data["error_kind"]) — each with a user-facing string the model can read
aloud verbatim:
  resolution : invalid_argument / no_device / no_primary_device / invalid_target / origin_mismatch
  delivery   : lost_contact / not_authorized / bad_response / unsupported_delivery
  device     : maps_missing / unsupported_device / invalid_destination / declined /
               background_blocked / notifications_blocked / device_error
"""
import re
import uuid

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider import mesh
from Orchestrator import frontier_agent_loop

REMOTE_CONTROL_PORT = 8765

# One intent fire — no model in the loop, so this is a short, hard budget.
_POST_TIMEOUT_SECS = 20.0

# Free text is fine (Maps geocodes it), but it must survive a JSON wire + a URI query and
# must not be an unbounded blob. Control characters are never part of a real address.
_MAX_DESTINATION_LEN = 512
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# "lat,lon" (optionally spaced). Normalized to the comma-literal form the on-device builder
# emits verbatim, and range-checked so a garbage pair fails HERE instead of on the phone.
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# Friendly model-facing words -> the google.navigation URI's own single-letter codes.
# These MIRROR IntentActions.NAVIGATION_MODES / NAVIGATION_AVOID_FLAGS exactly: mode is
# d|b|l|w and avoid is a duplicate-free subset of t/h/f. NOTE `l` is TWO-WHEELER, not
# transit — google.navigation has no transit mode, so "transit" is refused rather than
# silently routed as something else (see _TRANSIT_WORDS).
_MODE_CODES = {"driving": "d", "walking": "w", "bicycling": "b", "two_wheeler": "l"}
_MODE_ALIASES = {"drive": "d", "car": "d", "walk": "w", "on foot": "w", "bike": "b",
                 "cycling": "b", "bicycle": "b", "motorcycle": "l", "scooter": "l",
                 "two-wheeler": "l", "twowheeler": "l"}
_TRANSIT_WORDS = {"transit", "public transport", "public transit", "bus", "train", "subway"}
_AVOID_CODES = {"tolls": "t", "highways": "h", "ferries": "f"}
_AVOID_ORDER = ("t", "h", "f")

# Friendly navigation-app names -> Android package ids. "any" (and friends) tell the device
# to fire the implicit intent and let the phone's default handler take it
# (IntentActions.navigationTargetPackage). Omitting `app` entirely = Google Maps, the only
# app that honours the full mode/avoid contract.
_APP_PACKAGES = {
    "google maps": "com.google.android.apps.maps", "google map": "com.google.android.apps.maps",
    "maps": "com.google.android.apps.maps", "gmaps": "com.google.android.apps.maps",
    "waze": "com.waze",
    "osmand": "net.osmand", "osmand+": "net.osmand.plus",
    "organic maps": "app.organicmaps", "here": "com.here.app.maps",
    "here wego": "com.here.app.maps", "sygic": "com.sygic.aura",
}
_APP_ANY = {"any", "none", "default", "*", "whatever", "system default"}

# ── delivery (M3) ────────────────────────────────────────────────────────────────────
# How the navigation reaches the screen. "auto" is resolved ON THE DEVICE (only it knows
# whether it is foreground), so it goes on the wire verbatim rather than being decided here.
_DELIVERY_MODES = ("auto", "direct", "notify")
_DELIVERY_ALIASES = {"notification": "notify", "notifications": "notify", "push": "notify",
                     "background": "notify", "locked": "notify", "scheduled": "notify",
                     "now": "direct", "immediate": "direct", "immediately": "direct",
                     "foreground": "direct", "launch": "direct", "open": "direct",
                     "default": "auto", "any": "auto"}

# Phrases in a SUCCESS `detail` that tell us which delivery actually happened. The device
# has no structured field for this today (closed action_result schema), so the fixed phrase
# is the contract. Notify is tested first: a notification detail must never read as a launch.
_NOTIFY_DETAIL_WORDS = ("notif", "queued", "awaiting tap", "tap to")
_DIRECT_DETAIL_WORDS = ("started navigation", "launched", "opened", "foreground")

# The device's own wire tokens for the M3 refusals (NavigationDelivery.kt consts, lifted
# verbatim into action_result.error by classifyActuatorError, and also the leading token of
# the detail sentence). Matched FIRST and exactly, so classification never depends on prose.
_BACKGROUND_TOKENS = ("background_launch_blocked", "background_blocked")
_NOTIF_BLOCKED_TOKENS = ("notification_permission_missing", "notifications_blocked")
# "could not post the notification at all" is a delivery that did not happen, not a
# permission the operator can go and grant — its own detail explains itself.
_DELIVERY_TOKENS = ("notification_delivery_failed", "unsupported_delivery")

# Loose phrase fallbacks, for a device build that reports prose without a token.
_NOTIF_DENIED_WORDS = ("permission", "denied", "not permitted", "not allowed", "disabled",
                       "blocked", "turned off", "not enabled")
_BACKGROUND_WORDS = ("block", "launch", "restrict", "discard")
_DELIVERY_REJECT_WORDS = ("unsupported", "not supported", "unknown", "invalid",
                          "unrecognized", "reject")

# Resolution failures get a navigation-specific sentence; mesh's own (more technical) message
# is preserved in data["resolution_detail"] so nothing is lost.
_RESOLUTION_MESSAGES = {
    "no_device": ("I couldn't start navigation — none of your devices is reachable on the "
                  "tailnet right now. Wake the phone (or reconnect it to Tailscale) and ask "
                  "again."),
    "no_primary_device": ("I couldn't start navigation — your primary device isn't reachable "
                          "on the tailnet right now."),
    "invalid_target": ("I couldn't start navigation — there's no reachable device by that name "
                       "on the tailnet. It may be offline, or the name is wrong."),
    "origin_mismatch": ("I won't send navigation to that device — it isn't registered to this "
                        "operator. Name the device explicitly if you really meant it."),
}


def _control_port() -> int:
    """Device listener port — [control_phone] port (shared with control_device), default 8765."""
    try:
        from Orchestrator.config import CFG
        return CFG.getint("control_phone", "port", fallback=REMOTE_CONTROL_PORT)
    except Exception:
        return REMOTE_CONTROL_PORT


def _phone_base_url(node: mesh.Node) -> str:
    """Build the device listener's base URL from its tailnet address (dns_name preferred)."""
    host = node.dns_name or node.ip
    return f"http://{host}:{_control_port()}"


def _clip(value, limit: int = 200) -> str:
    s = str(value)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def normalize_destination(raw) -> str:
    """Clean a free-text destination, or return "" if it is unusable.

    PURE (unit-tested). Collapses whitespace, normalizes a 'lat, lon' pair to the
    comma-literal 'lat,lon' form the on-device URI builder expects, and rejects blanks,
    control characters, over-long blobs and out-of-range coordinates.
    """
    if not isinstance(raw, str):
        return ""
    dest = " ".join(raw.split())  # collapse newlines/tabs/runs of spaces
    if not dest or len(dest) > _MAX_DESTINATION_LEN or _CONTROL_CHARS.search(dest):
        return ""
    m = _LATLON.match(dest)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return ""
        return f"{m.group(1)},{m.group(2)}"
    return dest


def normalize_avoid(raw) -> str:
    """'tolls, ferries' -> 'tf' (stable t/h/f order). "" when absent; None when invalid.

    PURE (unit-tested). Accepts the friendly words, the letter codes, or a list of either.
    """
    if raw is None:
        return ""
    parts = raw if isinstance(raw, list) else str(raw).replace("|", ",").split(",")
    codes = set()
    for part in parts:
        token = str(part).strip().lower()
        if not token:
            continue
        code = _AVOID_CODES.get(token) or (token if token in _AVOID_ORDER else None)
        if code is None:
            return None
        codes.add(code)
    return "".join(c for c in _AVOID_ORDER if c in codes)


def normalize_app(raw) -> str:
    """A friendly app name -> its Android package id. "" when absent; None when unusable.

    PURE (unit-tested). 'any'/'default'/'none' -> "any" (the device's implicit-intent
    sentinel); a dotted package id passes through verbatim (never lock a Waze/OsmAnd user
    out); anything else that isn't a known name is refused HERE rather than shipped to the
    phone as a package that cannot resolve.
    """
    if raw is None:
        return ""
    name = " ".join(str(raw).split()).lower()
    if not name:
        return ""
    if name in _APP_ANY:
        return "any"
    if name in _APP_PACKAGES:
        return _APP_PACKAGES[name]
    # A real package id: dotted, no spaces, plain identifier characters.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", str(raw).strip()):
        return str(raw).strip()
    return None


def normalize_delivery(raw) -> str:
    """A delivery request -> 'auto' | 'direct' | 'notify'. None when unusable.

    PURE (unit-tested). Absent/blank is "auto" — the default has to be the SAFE one, because
    an unattended caller that never thought about delivery is exactly the case that broke.
    """
    if raw is None:
        return "auto"
    word = " ".join(str(raw).split()).lower()
    if not word:
        return "auto"
    if word in _DELIVERY_MODES:
        return word
    return _DELIVERY_ALIASES.get(word)


def resolve_delivery(result: dict) -> tuple:
    """(what the device ACTUALLY did, whether it said so KNOWINGLY).

    PURE (unit-tested). First element is 'direct' / 'notify' / "" (it never said). Second is
    the part that keeps us honest: True only when the answer came from a device that
    demonstrably understands ``delivery``.

      explicit result["delivery"]        -> stated. The device says the outcome outright.
      a notification/queued detail       -> stated. Only an M3 build can post a notification.
      a detail naming the foreground     -> stated. An M3 build explaining WHY it went direct.
      the legacy 'started navigation'    -> NOT stated. Indistinguishable between an M3 build
                                            launching from the foreground and a pre-M3 build
                                            that ignored `delivery` entirely — and the
                                            pre-M3 launch is the one Android drops in silence.
      nothing at all                     -> NOT stated.

    docs/schema/action_result.json pins additionalProperties:false with a CLOSED error enum,
    so the detail phrase is the only channel the device has today; the explicit field is
    honoured for when that schema is widened.
    """
    if not isinstance(result, dict):
        return "", False
    explicit = str(result.get("delivery") or "").strip().lower()
    if explicit in ("direct", "notify"):
        return explicit, True
    detail = str(result.get("detail") or "").lower()
    if any(w in detail for w in _NOTIFY_DETAIL_WORDS):
        return "notify", True
    if "foreground" in detail:
        return "direct", True
    if any(w in detail for w in _DIRECT_DETAIL_WORDS):
        return "direct", False
    return "", False


async def _post_intent(base_url: str, frame: dict) -> dict:
    """POST the one action frame to the device. Test seam — monkeypatched in unit tests so no
    socket is touched. Delegates to the frontier loop's existing /action client (httpx)."""
    return await frontier_agent_loop._post_action(base_url, frame, _POST_TIMEOUT_SECS)


def _http_status(exc) -> int:
    """The HTTP status carried by a client error (httpx.HTTPStatusError / aiohttp), or 0."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) or getattr(exc, "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _device_failure(result: dict, device_name: str, destination: str) -> ToolResult:
    """Map a device-reported action_result{success:false} to a structured, speakable error."""
    error = str(result.get("error") or "").strip().lower()
    detail = str(result.get("detail") or "").strip()
    low = detail.lower()
    data = {"device": device_name, "destination": destination, "device_detail": detail}

    # ── M3 delivery refusals — CHECKED FIRST ──────────────────────────────────────────
    # A background-blocked launch is ALSO error='dispatch_failed', so if these ran after the
    # branch below we would tell the operator "Google Maps isn't installed" when Maps is
    # installed and fine. Each matches the device's own wire token exactly, falling back to
    # a prose phrase for a build that doesn't send one.
    #
    # ORDER MATTERS: background BEFORE notifications. The device's background-blocked detail
    # names the notification as the WAY OUT ("or use delivery=notify …") and contains the
    # word "blocked", so a loose notification test would swallow it and send the operator to
    # go and enable a permission that was never the problem.
    if error in _BACKGROUND_TOKENS or any(t in low for t in _BACKGROUND_TOKENS) or (
            "background" in low and any(w in low for w in _BACKGROUND_WORDS)):
        data["error_kind"] = "background_blocked"
        return ToolResult(
            False,
            f"Android blocked the navigation launch on {device_name} — it won't let an app "
            "open Maps on its own while the phone is locked or the BlackBox app is in the "
            "background. Nothing is navigating. Unlock the phone and ask me again, or have "
            "me send it as a notification you can tap.",
            data=data)

    if error in _NOTIF_BLOCKED_TOKENS or any(t in low for t in _NOTIF_BLOCKED_TOKENS) or (
            "notif" in low and any(w in low for w in _NOTIF_DENIED_WORDS)):
        data["error_kind"] = "notifications_blocked"
        return ToolResult(
            False,
            f"I couldn't put the navigation on {device_name} as a notification — "
            "notifications aren't permitted for the BlackBox app on that device, so there "
            "is nothing there to tap. Enable notifications for it in the phone's settings "
            "and ask me again.",
            data=data)

    if error in _DELIVERY_TOKENS or any(t in low for t in _DELIVERY_TOKENS) or (
            "delivery" in low and any(w in low for w in _DELIVERY_REJECT_WORDS)):
        data["error_kind"] = "unsupported_delivery"
        return ToolResult(
            False,
            f"{device_name} wouldn't deliver the navigation that way"
            f"{' (' + detail + ')' if detail else ''} — nothing is navigating there. Its "
            "BlackBox app may be an older build; update it, or ask me again with the phone "
            "unlocked in front of you.",
            data=data)

    # An intent that could not be launched = nothing on the device handled
    # google.navigation: — i.e. Google Maps is missing/disabled (the resolveActivity preflight).
    maps_words = ("not installed", "no app", "resolve", "activity not found", "maps")
    if error == "dispatch_failed" or (not error and any(w in low for w in maps_words)):
        data["error_kind"] = "maps_missing"
        return ToolResult(
            False,
            f"{device_name} couldn't open turn-by-turn navigation — it doesn't look like "
            "Google Maps (or any navigation app) is installed and enabled on that device.",
            data=data)

    if error == "unknown_action":
        data["error_kind"] = "unsupported_device"
        return ToolResult(
            False,
            f"{device_name} doesn't support the navigate action — its BlackBox app is likely "
            "an older build. Update the app on that device and try again.",
            data=data)

    if error == "invalid_argument":
        # The device's own argument envelope refused something. Its detail names the valid
        # set (IntentActions.navigationRejectionReason) — pass that straight to the model.
        if "invalid navigation" in low or "not installed" in low:
            data["error_kind"] = "invalid_argument"
            return ToolResult(
                False, f"{device_name} rejected the navigation request: {detail}.", data=data)
        data["error_kind"] = "invalid_destination"
        return ToolResult(
            False,
            f"{device_name} rejected that destination"
            f"{' (' + detail + ')' if detail else ''}. Try a full street address or a "
            "'lat,lon' pair.",
            data=data)

    if not error:
        # success=false with no error kind is a benign decline — the on-device consent gate
        # for a cloud-originated push, or the user dismissing the confirmation.
        data["error_kind"] = "declined"
        return ToolResult(
            False,
            f"The navigation wasn't approved on {device_name} — the device asked for "
            "confirmation and it was declined or dismissed.",
            data=data)

    data["error_kind"] = "device_error"
    return ToolResult(
        False,
        f"{device_name} couldn't start navigation ({error}{': ' + detail if detail else ''}).",
        data=data)


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    # ── 1. Validate the caller's arguments BEFORE touching the tailnet ────────────────
    destination = normalize_destination(params.get("destination"))
    if not destination:
        return ToolResult(
            False,
            "I need a destination to navigate to — a street address, a place name, or "
            "'lat,lon' coordinates.",
            data={"error_kind": "invalid_argument", "field": "destination"})

    mode_word = " ".join(str(params.get("mode") or "").split()).lower()
    mode_code = ""
    if mode_word:
        if mode_word in _TRANSIT_WORDS:
            # google.navigation has no transit mode. Say so instead of quietly navigating
            # them by two-wheeler (the 'l' letter) or by car.
            return ToolResult(
                False,
                "Turn-by-turn navigation on the phone doesn't support public-transit "
                "directions — I can start driving, walking, bicycling or two-wheeler "
                "navigation instead.",
                data={"error_kind": "invalid_argument", "field": "mode", "mode": mode_word})
        mode_code = (_MODE_CODES.get(mode_word) or _MODE_ALIASES.get(mode_word)
                     or (mode_word if mode_word in _MODE_CODES.values() else ""))
        if not mode_code:
            return ToolResult(
                False,
                f"'{mode_word}' isn't a travel mode I can use. Pick driving, walking, "
                "bicycling or two-wheeler.",
                data={"error_kind": "invalid_argument", "field": "mode", "mode": mode_word})

    avoid_code = normalize_avoid(params.get("avoid"))
    if avoid_code is None:
        return ToolResult(
            False,
            f"I can only avoid tolls, highways or ferries — '{_clip(params.get('avoid'), 60)}' "
            "isn't one of those.",
            data={"error_kind": "invalid_argument", "field": "avoid"})

    app_package = normalize_app(params.get("app"))
    if app_package is None:
        return ToolResult(
            False,
            f"I don't recognize '{_clip(params.get('app'), 60)}' as a navigation app. Name "
            "Google Maps, Waze, OsmAnd, Organic Maps or HERE — or say 'any' to use the "
            "phone's default.",
            data={"error_kind": "invalid_argument", "field": "app"})

    delivery = normalize_delivery(params.get("delivery"))
    if delivery is None:
        return ToolResult(
            False,
            f"'{_clip(params.get('delivery'), 60)}' isn't a delivery mode I can use. Pick "
            "'auto' (let the phone decide), 'direct' (open Maps right now — only works with "
            "the phone awake), or 'notify' (put a tappable notification on the phone).",
            data={"error_kind": "invalid_argument", "field": "delivery"})

    # ── 2. Resolve the target device (same rule as control_device) ────────────────────
    device = str(params.get("device") or "").strip()
    try:
        node = mesh.resolve_device(
            operator=ctx.operator,
            origin_device_id=ctx.origin_device_id,
            target_device_id=device or None,
        )
    except mesh.DeviceResolutionError as e:
        data = {"error_kind": e.kind, "resolution_detail": e.message, "destination": destination}
        data.update(e.detail)
        return ToolResult(False, _RESOLUTION_MESSAGES.get(e.kind, e.message), data=data)

    base_url = _phone_base_url(node)
    device_name = node.dns_name or node.ip

    # ── 3. ONE action frame. No loop, no session, no screenshot. ──────────────────────
    intent_params = {"destination": destination}
    if mode_code:
        intent_params["mode"] = mode_code
    if avoid_code:
        intent_params["avoid"] = avoid_code
    if app_package:
        # "any" is the device's own sentinel for "no package — implicit intent".
        intent_params["package"] = app_package
    # ALWAYS on the wire, "auto" included — unlike mode/avoid/package this is never omitted.
    # An absent key would leave a device build free to read it as "legacy caller, launch
    # directly", and that guess is precisely the silent-false-success this milestone kills.
    intent_params["delivery"] = delivery
    task_id = uuid.uuid4().hex
    frame = frontier_agent_loop._wire_frame(
        task_id, ctx.operator,
        {"type": "intent", "name": "navigate", "params": intent_params})

    base_data = {"device": device_name, "destination": destination, "task_id": task_id,
                 "mode": mode_word or None, "avoid": avoid_code or None,
                 "app": app_package or None, "delivery_requested": delivery}

    try:
        result = await _post_intent(base_url, frame)
    except Exception as e:  # transport/status errors (CancelledError is a BaseException)
        status = _http_status(e)
        if status == 403:
            return ToolResult(
                False,
                f"{device_name} refused the navigation request — that device isn't authorized "
                f"for operator '{ctx.operator}'.",
                data={**base_data, "error_kind": "not_authorized", "http_status": status})
        if 400 <= status < 600:
            return ToolResult(
                False,
                f"{device_name} couldn't accept the navigation request (HTTP {status}).",
                data={**base_data, "error_kind": "bad_response", "http_status": status})
        return ToolResult(
            False,
            f"I couldn't reach {device_name} to start navigation — it may be asleep, off "
            f"Tailscale, or the BlackBox app isn't running ({_clip(e)}).",
            data={**base_data, "error_kind": "lost_contact"})

    if not isinstance(result, dict):
        return ToolResult(
            False,
            f"{device_name} returned an unreadable response to the navigation request.",
            data={**base_data, "error_kind": "bad_response"})

    if not result.get("success"):
        failure = _device_failure(result, device_name, destination)
        failure.data.update({k: v for k, v in base_data.items() if k not in failure.data})
        return failure

    # ── 4. Fired — report the delivery that ACTUALLY happened, not the one requested ──
    extras = []
    if mode_word:
        extras.append(mode_word)
    if avoid_code:
        extras.append("avoiding " + ", ".join(
            w for w, c in _AVOID_CODES.items() if c in avoid_code))
    suffix = f" ({', '.join(extras)})" if extras else ""
    device_detail = str(result.get("detail") or "")

    actual, stated = resolve_delivery(result)
    if delivery == "notify" and not stated:
        # We asked for the notification BECAUSE a direct launch is unreliable here, and the
        # device gave us nothing that proves it honoured that. Its success:true is exactly
        # the M2 false success — a launch Android may have discarded without a word. There
        # is no outcome we can claim: not a notification (it may not exist) and not a
        # navigation (it may never have opened). Refuse rather than invent one.
        return ToolResult(
            False,
            f"I can't confirm the navigation actually reached {device_name}. Its BlackBox "
            "app looks like an older build that only opens Maps directly, and Android "
            "throws that away without a word when the phone is locked or in a pocket — so "
            "there may be nothing on the phone at all. Update the app on that device, or "
            "ask me again with the phone unlocked in front of you.",
            data={**base_data, "error_kind": "unsupported_delivery", "delivery": None,
                  "delivery_confirmed": False, "navigating": False,
                  "awaiting_user_tap": False, "device_detail": device_detail})
    if not actual:
        # Nothing said, and 'auto'/'direct' asked for the direct launch an older build
        # performs anyway — the device-proven M2 outcome stands, flagged unconfirmed rather
        # than silently assumed.
        actual = "direct"

    delivered = {**base_data, "delivery": actual,
                 "delivery_confirmed": stated,
                 "navigating": actual == "direct",
                 "awaiting_user_tap": actual == "notify",
                 "device_detail": device_detail}

    if actual == "notify":
        # NOT a started navigation. Nothing moves until a human taps, and the model must
        # have no way to read this as "Maps is open and navigating".
        return ToolResult(
            True,
            f"I've sent the navigation to {destination} to {device_name}{suffix} — it's "
            "waiting there as a notification. Tap it on the phone to start turn-by-turn. "
            "Nothing is navigating yet: Android only lets Maps open from your tap while the "
            "phone is locked or the app is in the background.",
            data=delivered)

    return ToolResult(
        True,
        f"Turn-by-turn navigation to {destination} is starting on {device_name}{suffix} — "
        "Google Maps is opening there now.",
        data=delivered)
