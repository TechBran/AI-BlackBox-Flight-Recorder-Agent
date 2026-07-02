"""M0 wire-contract tests for the Android device-control JSON Schemas.

Covers `docs/schema/*.json` (the frontier-driven device-control wire contract). Two
layers, so this test is meaningful whether or not `jsonschema` is installed:

1. STRUCTURAL (always runs, no deps): every schema is well-formed JSON and encodes the
   load-bearing invariants (the `msg` message-kind discriminator, the password-redaction
   if/then, the XR no-screenshot rule, the element_ref anyOf, the enum floors, and the two
   new `open_app` / `scroll` action variants).
2. FULL VALIDATION (runs only when `jsonschema` is importable — e.g. system python3;
   the Orchestrator venv does NOT ship it, so these are SKIPPED there). Asserts a sample of
   EACH message + EACH action variant VALIDATES, and that the load-bearing NEGATIVES REJECT.

NOTE: full JSON-Schema (2020-12) validation of the positive/negative samples requires the
`jsonschema` package (with cross-file `$ref` resolution). It is intentionally NOT added to
the Orchestrator venv here (pip install is out of scope for M0). Run these under a
`jsonschema`-equipped interpreter (`python3 -m pytest ...`) to exercise the FULL path; the
STRUCTURAL layer still guards the contract in the venv.
"""

import glob
import json
import os

import pytest

SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "schema",
)

# ── load every schema, keyed by both filename and $id ────────────────────────────
_SCHEMA_PATHS = sorted(glob.glob(os.path.join(SCHEMA_DIR, "*.json")))
SCHEMAS = {os.path.basename(p): json.load(open(p, encoding="utf-8")) for p in _SCHEMA_PATHS}
BY_ID = {doc["$id"]: doc for doc in SCHEMAS.values() if "$id" in doc}

# Is a JSON-Schema validator available? (Absent in the Orchestrator venv → FULL layer skips.)
try:
    import jsonschema  # noqa: F401

    _HAS_VALIDATOR = True
except Exception:  # pragma: no cover - depends on interpreter
    _HAS_VALIDATOR = False


def _validator_for(schema_file):
    """A Draft2020-12 validator for `schema_file` with all sibling schemas resolvable by $id.

    Supports both the modern `referencing.Registry` API (jsonschema >= 4.18) and the legacy
    `RefResolver` (jsonschema < 4.18, e.g. 4.10.x), so it works across interpreters.
    """
    import jsonschema

    schema = SCHEMAS[schema_file]
    Validator = jsonschema.Draft202012Validator
    try:  # modern API
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        registry = Registry().with_resources(
            [(sid, Resource(contents=doc, specification=DRAFT202012)) for sid, doc in BY_ID.items()]
        )
        return Validator(schema, registry=registry)
    except Exception:  # legacy RefResolver path
        resolver = jsonschema.RefResolver(base_uri=schema.get("$id", ""), referrer=schema, store=dict(BY_ID))
        return Validator(schema, resolver=resolver)


def _valid(schema_file, instance):
    return not list(_validator_for(schema_file).iter_errors(instance))


# ── sample fixtures (positives) ──────────────────────────────────────────────────
NODE_OK = {
    "node_id": 0, "role": "Button", "text": "OK", "resource_id": "com.app:id/ok",
    "bounds": "0,0,100,50", "clickable": True, "editable": False, "is_password": False,
}
CAP_PHONE = {"formFactor": "phone", "hasScreenshot": True, "supportsCoordinateGesture": True, "displayId": 0}
CAP_XR = {"formFactor": "xr_headset", "hasScreenshot": False, "supportsCoordinateGesture": False, "displayId": 0}
# M5.5: a foldable advertises formFactor=foldable + a posture (FLAT / HALF_OPENED + orientation).
CAP_FOLDABLE = {
    "formFactor": "foldable", "hasScreenshot": True, "supportsCoordinateGesture": True, "displayId": 0,
    "posture": {"state": "half_opened", "orientation": "vertical"},
}
# M5.3: window-topology entries (an app window + a system bar).
WINDOW_APP = {"displayId": 0, "appPackage": "com.app", "bounds": "0,0,1080,2400", "isSystemBar": False}
WINDOW_SYSBAR = {"displayId": 0, "appPackage": "com.android.systemui", "bounds": "0,0,1080,96", "isSystemBar": True}

OBS_OK = {
    "msg": "observation", "schema_version": "1.3",
    "ui_tree": [NODE_OK], "device_capability": CAP_PHONE, "timestamp": 1234567890,
}
# M5: an observation carrying the new window_topology + posture_changed on a foldable.
OBS_M5 = {
    **OBS_OK, "device_capability": CAP_FOLDABLE,
    "window_topology": [WINDOW_APP, WINDOW_SYSBAR], "posture_changed": True,
}

# The 26 intent names the code ships today (ResidentTools.INTENT_ACTIONS) — the schema enum
# must match exactly (I2). Kept here as the single source the tests assert against.
INTENT_NAMES_26 = [
    "flashlight_on", "flashlight_off", "create_contact", "send_email", "show_map",
    "open_wifi_settings", "create_calendar_event", "open_url", "dial", "send_sms",
    "set_alarm", "set_timer", "share_text", "open_settings_panel", "take_photo",
    "capture_video", "show_alarms", "view_calendar", "pick_contact", "view_contacts",
    "pick_file", "create_document", "navigate", "play_media", "open_settings", "send_intent",
]

ACTIONS_OK = {
    "element_click": {"msg": "action", "type": "element_click", "resource_id": "com.app:id/ok"},
    "element_set_text": {"msg": "action", "type": "element_set_text", "resource_id": "com.app:id/f", "text": "hi"},
    "coordinate_tap": {"msg": "action", "type": "coordinate_tap", "x": 100, "y": 200},
    "coordinate_swipe": {"msg": "action", "type": "coordinate_swipe", "x": 10, "y": 20, "x2": 30, "y2": 40},
    "global_action": {"msg": "action", "type": "global_action", "action": "back"},
    "intent": {"msg": "action", "type": "intent", "name": "show_map", "params": {"query": "coffee"}},
    # I2: a decision-9 comprehensive intent + the guarded send_intent escape-hatch validate.
    "intent_navigate": {"msg": "action", "type": "intent", "name": "navigate", "params": {"destination": "SFO"}},
    "intent_open_settings": {"msg": "action", "type": "intent", "name": "open_settings", "params": {"panel": "wifi"}},
    "intent_send_intent": {
        "msg": "action", "type": "intent", "name": "send_intent",
        "params": {"action": "android.intent.action.VIEW", "uri": "https://example.com", "mime": "text/html",
                   "package": "com.android.chrome", "extras": {"k": "v"}},
    },
    "open_app": {"msg": "action", "type": "open_app", "package": "com.google.android.apps.maps"},
    "scroll": {"msg": "action", "type": "scroll", "direction": "down"},
    # M2 / v1.2: the press_key action variant (enter submits the focused field via IME_ENTER).
    "press_key": {"msg": "action", "type": "press_key", "key": "enter"},
}

RESULTS_OK = {
    "success": {"msg": "action_result", "success": True, "detail": "tapped node[0]"},
    "error": {"msg": "action_result", "success": False, "error": "node_not_found", "detail": "node 5 not found"},
    "not_wired": {"msg": "action_result", "success": False, "error": "not_wired", "detail": "scaffold"},
    "with_observation": {"msg": "action_result", "success": True, "detail": "ok", "observation": OBS_OK},
}

# ── negatives (MUST reject) ──────────────────────────────────────────────────────
PWD_NODE_RAW = {
    "node_id": 0, "role": "EditText", "text": "hunter2", "resource_id": "com.app:id/pw",
    "bounds": "0,0,10,10", "clickable": False, "editable": True, "is_password": True,
}
NEG_PASSWORD_RAW = {**OBS_OK, "ui_tree": [PWD_NODE_RAW]}
NEG_XR_SCREENSHOT = {**OBS_OK, "device_capability": CAP_XR, "screenshot": "iVBORw0KGgo="}
NEG_CLICK_NO_REF = {"msg": "action", "type": "element_click"}  # neither resource_id nor node_id
NEG_UNKNOWN_INTENT = {"msg": "action", "type": "intent", "name": "launch_missiles"}
# I1: a gesture/coordinate dispatch name smuggled as an intent must NOT be a valid intent
# (it is not in the intent.name enum) — the on-device parser rejects it too.
NEG_INTENT_COORD_SMUGGLE = {"msg": "action", "type": "intent", "name": "coordinate_tap", "params": {"x": 1, "y": 2}}
NEG_INTENT_READ_SCREEN = {"msg": "action", "type": "intent", "name": "read_screen"}
# I2: open_app's wire key is exactly `package` (additionalProperties:false) — no package_name alias.
NEG_OPEN_APP_PACKAGE_NAME = {"msg": "action", "type": "open_app", "package_name": "com.x"}
NEG_UNKNOWN_TYPE = {"msg": "action", "type": "frobnicate"}
NEG_UNKNOWN_GLOBAL = {"msg": "action", "type": "global_action", "action": "sideways"}
# M2 / v1.2: a press_key with a key outside the enum must reject (the on-device parser
# rejects it too with invalid_argument).
NEG_PRESS_KEY_UNKNOWN = {"msg": "action", "type": "press_key", "key": "f13"}
NEG_MISSING_MSG = {"type": "element_click", "resource_id": "com.app:id/ok"}  # no msg
NEG_BLANK_MSG = {"msg": "", "type": "element_click", "resource_id": "com.app:id/ok"}  # msg not const
# M5.5: a posture with an out-of-enum state must reject (device only emits flat / half_opened).
NEG_BAD_POSTURE_STATE = {
    **OBS_OK,
    "device_capability": {**CAP_FOLDABLE, "posture": {"state": "folded_shut"}},
}
# M5.3: a window-topology entry missing a required key must reject (additionalProperties:false + required).
NEG_WINDOW_MISSING_KEY = {**OBS_OK, "window_topology": [{"displayId": 0, "appPackage": "com.app", "bounds": "0,0,1,1"}]}


# ═════════════════════════ STRUCTURAL layer (always runs) ═════════════════════════

def test_every_schema_is_well_formed_json():
    # json.load already ran at import; re-assert every glob'd file parses.
    files = glob.glob(os.path.join(SCHEMA_DIR, "*.json"))
    assert files, "no schema files found"
    for f in files:
        with open(f, encoding="utf-8") as fh:
            json.load(fh)  # raises on malformed JSON


def test_msg_discriminator_is_required_and_const_per_message():
    assert "msg" in SCHEMAS["observation.json"]["required"]
    assert SCHEMAS["observation.json"]["properties"]["msg"]["const"] == "observation"
    assert "msg" in SCHEMAS["action.json"]["required"]
    assert SCHEMAS["action.json"]["$defs"]["msg"]["const"] == "action"
    assert "msg" in SCHEMAS["action_result.json"]["required"]
    assert SCHEMAS["action_result.json"]["properties"]["msg"]["const"] == "action_result"


def test_msg_is_a_distinct_key_from_action_variant_type():
    # In each action variant, `msg` (message kind) and `type` (variant) are separate keys.
    for name, variant in SCHEMAS["action.json"]["$defs"].items():
        if name in ("msg", "element_ref"):
            continue
        props = variant["properties"]
        assert "msg" in props and "type" in props, name
        assert props["type"]["const"] == name, name
        assert "msg" in variant["required"] and "type" in variant["required"], name


def test_open_app_and_scroll_variants_present_and_grounded():
    defs = SCHEMAS["action.json"]["$defs"]
    # open_app → {package required}; a PHONE_ACTUATOR, so NOT in intent.name.
    assert defs["open_app"]["properties"]["type"]["const"] == "open_app"
    assert "package" in defs["open_app"]["required"]
    assert "open_app" not in SCHEMAS["action.json"]["$defs"]["intent"]["properties"]["name"]["enum"]
    # scroll → direction enum (direction-based, XR-portable — matches Actuators.scroll).
    assert defs["scroll"]["properties"]["type"]["const"] == "scroll"
    assert "direction" in defs["scroll"]["required"]
    assert defs["scroll"]["properties"]["direction"]["enum"] == ["up", "down", "left", "right"]
    # both wired into the oneOf union.
    refs = {r["$ref"] for r in SCHEMAS["action.json"]["oneOf"]}
    assert "#/$defs/open_app" in refs and "#/$defs/scroll" in refs


def test_password_redaction_invariant_is_encoded():
    items = SCHEMAS["observation.json"]["properties"]["ui_tree"]["items"]["allOf"]
    gate = next(a for a in items if "if" in a)
    assert gate["if"]["properties"]["is_password"]["const"] is True
    assert gate["then"]["properties"]["text"]["const"] == "·····"


def test_xr_no_screenshot_rule_is_encoded():
    gate = SCHEMAS["observation.json"]["allOf"][0]
    assert gate["if"]["properties"]["device_capability"]["properties"]["hasScreenshot"]["const"] is False
    assert gate["then"]["not"]["required"] == ["screenshot"]


def test_element_ref_requires_one_addressing_key():
    anyof = SCHEMAS["action.json"]["$defs"]["element_ref"]["anyOf"]
    assert {"required": ["resource_id"]} in anyof
    assert {"required": ["node_id"]} in anyof


def test_action_result_error_enum_includes_not_wired():
    enum = SCHEMAS["action_result.json"]["properties"]["error"]["enum"]
    assert "not_wired" in enum  # M0 scaffold state alongside the structured errors
    for e in ("not_enabled", "node_not_found", "unknown_action", "invalid_argument", "dispatch_failed"):
        assert e in enum


def test_ui_node_requires_resource_id():
    assert "resource_id" in SCHEMAS["ui_node.json"]["required"]


def test_intent_name_enum_matches_the_26_intent_actions():
    # I2: the enum must match ResidentTools.INTENT_ACTIONS exactly (26 names, incl. the
    # decision-9 comprehensive intents + open_settings + the guarded send_intent).
    enum = SCHEMAS["action.json"]["$defs"]["intent"]["properties"]["name"]["enum"]
    assert len(enum) == 26
    assert set(enum) == set(INTENT_NAMES_26)
    # The new names + send_intent are present; open_app is NOT (it is a PHONE_ACTUATOR).
    for name in ("capture_video", "show_alarms", "view_calendar", "pick_contact", "view_contacts",
                 "pick_file", "create_document", "navigate", "play_media", "open_settings", "send_intent"):
        assert name in enum, name
    assert "open_app" not in enum
    # No gesture/global/coordinate dispatch name can be an intent (I1 collision-freedom).
    for gesture in ("read_screen", "tap", "type", "swipe", "coordinate_tap", "coordinate_swipe", "recents"):
        assert gesture not in enum, gesture


def test_schema_version_bumped_to_1_3_minor():
    # Additive minor bumps (const on observation, no /v2/ path change): 1.1 = 15->26 intents;
    # 1.2 = the press_key action variant; 1.3 = M5 window_topology + posture_changed + posture.
    assert SCHEMAS["observation.json"]["properties"]["schema_version"]["const"] == "1.3"
    assert "/v1/" in SCHEMAS["observation.json"]["$id"]  # major path unchanged


def test_window_topology_present_and_shaped():
    # M5.3: an optional (NOT required) array of window descriptors with the exact camelCase keys.
    obs = SCHEMAS["observation.json"]
    wt = obs["properties"]["window_topology"]
    assert wt["type"] == "array"
    assert "window_topology" not in obs["required"]  # additive/optional
    item = wt["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"displayId", "appPackage", "bounds", "isSystemBar"}
    assert item["properties"]["displayId"]["type"] == "integer"
    assert item["properties"]["isSystemBar"]["type"] == "boolean"


def test_posture_changed_flag_present_and_optional():
    # M5.5: an optional boolean re-observe flag on the observation.
    obs = SCHEMAS["observation.json"]
    assert obs["properties"]["posture_changed"]["type"] == "boolean"
    assert "posture_changed" not in obs["required"]


def test_device_capability_posture_is_optional_and_enumerated():
    # M5.5: device_capability.posture is an OPTIONAL object (not in required) with a state enum
    # (flat / half_opened) + an orientation enum (vertical / horizontal), state required.
    cap = SCHEMAS["device_capability.json"]
    assert "posture" not in cap["required"]  # foldable-only, additive
    posture = cap["properties"]["posture"]
    assert posture["type"] == "object"
    assert posture["additionalProperties"] is False
    assert posture["required"] == ["state"]
    assert posture["properties"]["state"]["enum"] == ["flat", "half_opened"]
    assert posture["properties"]["orientation"]["enum"] == ["vertical", "horizontal"]


def test_press_key_variant_present_and_grounded():
    # M2 / v1.2: the coordinate-free press_key variant — enter submits the focused field.
    defs = SCHEMAS["action.json"]["$defs"]
    assert defs["press_key"]["properties"]["type"]["const"] == "press_key"
    assert "key" in defs["press_key"]["required"]
    assert defs["press_key"]["properties"]["key"]["enum"] == \
        ["enter", "back", "home", "recents", "tab", "delete"]
    assert defs["press_key"]["additionalProperties"] is False
    # wired into the oneOf union.
    refs = {r["$ref"] for r in SCHEMAS["action.json"]["oneOf"]}
    assert "#/$defs/press_key" in refs
    # press_key is a top-level action variant, NOT an intent name (like open_app).
    assert "press_key" not in SCHEMAS["action.json"]["$defs"]["intent"]["properties"]["name"]["enum"]


def test_open_app_wire_key_is_exactly_package():
    # I2: open_app forbids extra keys (additionalProperties:false) and requires `package`,
    # so a `package_name` alias is NOT part of the wire contract.
    open_app = SCHEMAS["action.json"]["$defs"]["open_app"]
    assert open_app["additionalProperties"] is False
    assert "package" in open_app["required"]
    assert "package_name" not in open_app["properties"]


def test_samples_carry_the_expected_discriminators():
    assert OBS_OK["msg"] == "observation"
    variant_types = {k for k in SCHEMAS["action.json"]["$defs"] if k not in ("msg", "element_ref")}
    for name, a in ACTIONS_OK.items():
        assert a["msg"] == "action"
        assert a["type"] in variant_types, name
        # A key that names a variant directly must match it; the extra intent_* samples are intents.
        assert a["type"] == (name if name in variant_types else "intent"), name
    for r in RESULTS_OK.values():
        assert r["msg"] == "action_result"


# ═══════════════ FULL VALIDATION layer (jsonschema only; skipped in the venv) ═══════════════

_needs_validator = pytest.mark.skipif(
    not _HAS_VALIDATOR, reason="jsonschema not installed (Orchestrator venv); structural layer covers the contract"
)


@_needs_validator
def test_observation_sample_validates():
    assert _valid("observation.json", OBS_OK)


@_needs_validator
@pytest.mark.parametrize("name", list(ACTIONS_OK))
def test_each_action_variant_validates(name):
    assert _valid("action.json", ACTIONS_OK[name]), name


@_needs_validator
@pytest.mark.parametrize("name", list(RESULTS_OK))
def test_each_action_result_validates(name):
    assert _valid("action_result.json", RESULTS_OK[name]), name


@_needs_validator
def test_negative_password_node_with_raw_text_rejects():
    assert not _valid("observation.json", NEG_PASSWORD_RAW)


@_needs_validator
def test_negative_xr_capability_with_screenshot_rejects():
    assert not _valid("observation.json", NEG_XR_SCREENSHOT)


@_needs_validator
def test_negative_element_click_with_neither_ref_rejects():
    assert not _valid("action.json", NEG_CLICK_NO_REF)


@_needs_validator
def test_negative_unknown_intent_name_rejects():
    assert not _valid("action.json", NEG_UNKNOWN_INTENT)


@_needs_validator
@pytest.mark.parametrize("name", ["navigate", "open_settings", "send_intent"])
def test_new_intent_names_validate(name):
    # I2: the newly-added intent names are accepted by the schema.
    assert _valid("action.json", ACTIONS_OK[{"navigate": "intent_navigate",
                                              "open_settings": "intent_open_settings",
                                              "send_intent": "intent_send_intent"}[name]])


@_needs_validator
def test_negative_intent_coordinate_smuggle_rejects():
    # I1: a coordinate/gesture dispatch name is not a valid intent.name.
    assert not _valid("action.json", NEG_INTENT_COORD_SMUGGLE)
    assert not _valid("action.json", NEG_INTENT_READ_SCREEN)


@_needs_validator
def test_negative_open_app_package_name_alias_rejects():
    # I2: package_name is not on the wire (additionalProperties:false).
    assert not _valid("action.json", NEG_OPEN_APP_PACKAGE_NAME)


@_needs_validator
def test_negative_unknown_action_type_rejects():
    assert not _valid("action.json", NEG_UNKNOWN_TYPE)


@_needs_validator
def test_negative_unknown_global_action_rejects():
    assert not _valid("action.json", NEG_UNKNOWN_GLOBAL)


@_needs_validator
def test_press_key_sample_validates_and_unknown_key_rejects():
    # M2 / v1.2: the press_key sample validates; a key outside the enum rejects.
    assert _valid("action.json", ACTIONS_OK["press_key"])
    assert not _valid("action.json", NEG_PRESS_KEY_UNKNOWN)


@_needs_validator
def test_m5_observation_with_topology_and_posture_validates():
    # M5.3 + M5.5: an observation carrying window_topology + a foldable posture + posture_changed.
    assert _valid("observation.json", OBS_M5)


@_needs_validator
def test_m5_foldable_capability_with_posture_validates():
    # M5.5: a foldable device_capability with a posture object validates.
    assert _valid("device_capability.json", CAP_FOLDABLE)


@_needs_validator
def test_m5_negative_bad_posture_state_rejects():
    # M5.5: a posture.state outside {flat, half_opened} rejects.
    assert not _valid("observation.json", NEG_BAD_POSTURE_STATE)


@_needs_validator
def test_m5_negative_window_topology_missing_key_rejects():
    # M5.3: a window entry missing a required key rejects (required + additionalProperties:false).
    assert not _valid("observation.json", NEG_WINDOW_MISSING_KEY)


@_needs_validator
def test_negative_missing_msg_rejects():
    assert not _valid("action.json", NEG_MISSING_MSG)


@_needs_validator
def test_negative_blank_msg_rejects():
    assert not _valid("action.json", NEG_BLANK_MSG)
