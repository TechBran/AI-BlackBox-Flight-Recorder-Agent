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

OBS_OK = {
    "msg": "observation", "schema_version": "1.0",
    "ui_tree": [NODE_OK], "device_capability": CAP_PHONE, "timestamp": 1234567890,
}

ACTIONS_OK = {
    "element_click": {"msg": "action", "type": "element_click", "resource_id": "com.app:id/ok"},
    "element_set_text": {"msg": "action", "type": "element_set_text", "resource_id": "com.app:id/f", "text": "hi"},
    "coordinate_tap": {"msg": "action", "type": "coordinate_tap", "x": 100, "y": 200},
    "coordinate_swipe": {"msg": "action", "type": "coordinate_swipe", "x": 10, "y": 20, "x2": 30, "y2": 40},
    "global_action": {"msg": "action", "type": "global_action", "action": "back"},
    "intent": {"msg": "action", "type": "intent", "name": "show_map", "params": {"query": "coffee"}},
    "open_app": {"msg": "action", "type": "open_app", "package": "com.google.android.apps.maps"},
    "scroll": {"msg": "action", "type": "scroll", "direction": "down"},
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
NEG_UNKNOWN_TYPE = {"msg": "action", "type": "frobnicate"}
NEG_UNKNOWN_GLOBAL = {"msg": "action", "type": "global_action", "action": "sideways"}
NEG_MISSING_MSG = {"type": "element_click", "resource_id": "com.app:id/ok"}  # no msg
NEG_BLANK_MSG = {"msg": "", "type": "element_click", "resource_id": "com.app:id/ok"}  # msg not const


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


def test_intent_name_enum_has_the_15_intents():
    enum = SCHEMAS["action.json"]["$defs"]["intent"]["properties"]["name"]["enum"]
    assert len(enum) == 15
    assert "open_app" not in enum  # open_app is a PHONE_ACTUATOR, not an intent


def test_samples_carry_the_expected_discriminators():
    assert OBS_OK["msg"] == "observation"
    for name, a in ACTIONS_OK.items():
        assert a["msg"] == "action" and a["type"] == name
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
def test_negative_unknown_action_type_rejects():
    assert not _valid("action.json", NEG_UNKNOWN_TYPE)


@_needs_validator
def test_negative_unknown_global_action_rejects():
    assert not _valid("action.json", NEG_UNKNOWN_GLOBAL)


@_needs_validator
def test_negative_missing_msg_rejects():
    assert not _valid("action.json", NEG_MISSING_MSG)


@_needs_validator
def test_negative_blank_msg_rejects():
    assert not _valid("action.json", NEG_BLANK_MSG)
