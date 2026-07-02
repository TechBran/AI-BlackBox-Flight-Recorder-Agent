"""Unit tests for the M2 hybrid-grounding + coordinate-adapter module.

Pure (no network / model / device): proves the coordinate adapter denormalizes 0-999 → device
px correctly, that grounding snaps a coordinate to the correct a11y element (containment +
nearest), prefers a stable resource_id, falls back to a raw coordinate ONLY when no node
matches AND the device supports coordinate gestures (XR → element-only), and that device
dimensions are derived from a screenshot / tree bounds.
"""
import base64
import struct

from Orchestrator.frontier_grounding import (
    GeminiCoordinateAdapter,
    derive_device_dimensions,
    find_target_node,
    parse_bounds,
    snap_swipe_to_coordinate,
    snap_to_element,
)


# ── coordinate adapter ───────────────────────────────────────────────────────────────
def test_adapter_denormalizes_center():
    # 500,500 of 0-999 on a 1080×2400 screen → ~540,1201 (matches ADB denormalize math).
    assert GeminiCoordinateAdapter().to_device_px(500, 500, 1080, 2400) == (540, 1201)


def test_adapter_corners_and_clamp():
    a = GeminiCoordinateAdapter()
    assert a.to_device_px(0, 0, 1080, 2400) == (0, 0)
    assert a.to_device_px(999, 999, 1080, 2400) == (1080, 2400)
    # out-of-range coords are clamped to [0,999], never off-screen
    assert a.to_device_px(-50, 5000, 1080, 2400) == (0, 2400)


# ── bounds parsing ───────────────────────────────────────────────────────────────────
def test_parse_bounds_ok_and_malformed():
    assert parse_bounds("0,0,1080,2400") == (0, 0, 1080, 2400)
    assert parse_bounds("-5,-10,100,200") == (-5, -10, 100, 200)
    assert parse_bounds("nope") is None
    assert parse_bounds("1,2,3") is None
    assert parse_bounds(None) is None


# ── element targeting ────────────────────────────────────────────────────────────────
def _node(node_id, bounds, *, clickable=False, editable=False, resource_id="", role="View"):
    return {"node_id": node_id, "role": role, "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


def test_snap_hits_containing_element():
    tree = [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),        # full-screen
        _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
        _node(2, "400,1600,700,1750", clickable=True, resource_id="app:id/go"),
    ]
    # tap at 500,700 (0-999) → px (540,1681) which is inside node 2 (the submit button)
    g = snap_to_element((500, 700), tree, (1080, 2400))
    assert g.method == "element"
    assert g.frame == {"type": "element_click", "resource_id": "app:id/go"}


def test_snap_prefers_smallest_containing_over_fullscreen():
    tree = [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", clickable=True, resource_id="app:id/small"),
    ]
    # px (540,720) is inside BOTH — the smaller, more specific node wins
    g = snap_to_element((500, 300), tree, (1080, 2400))
    assert g.frame["resource_id"] == "app:id/small"


def test_snap_type_targets_editable_and_carries_text():
    tree = [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
    ]
    g = snap_to_element((500, 300), tree, (1080, 2400), editable=True, text="hello")
    assert g.frame == {"type": "element_set_text", "resource_id": "app:id/field", "text": "hello"}


def test_snap_nearest_when_between_elements():
    tree = [
        _node(1, "0,0,100,100", clickable=True, resource_id="near"),
        _node(2, "900,2200,1000,2300", clickable=True, resource_id="far"),
    ]
    # px for (30,30)=~(32,72) → closest to the top-left node even though contained by neither
    g = snap_to_element((30, 30), tree, (1080, 2400))
    assert g.method == "element"
    assert g.frame["resource_id"] == "near"


def test_snap_uses_node_id_when_no_resource_id():
    tree = [_node(7, "400,600,700,850", clickable=True)]  # no resource_id
    g = snap_to_element((500, 300), tree, (1080, 2400))
    assert g.frame == {"type": "element_click", "node_id": 7}


def test_snap_falls_back_to_coordinate_when_tree_empty():
    g = snap_to_element((500, 500), [], (1080, 2400))
    assert g.method == "coordinate"
    assert g.frame == {"type": "coordinate_tap", "x": 540, "y": 1201}


def test_snap_no_coordinate_fallback_on_coordinate_less_device():
    # XR: supportsCoordinateGesture=false → element-only; an empty tree is ungroundable.
    g = snap_to_element((500, 500), [], (1080, 2400), supports_coordinate=False)
    assert g.frame is None
    assert g.method == "none"


def test_snap_type_with_no_node_is_ungroundable():
    # There is no coordinate "type" on the wire → a text target with no node → frame None.
    g = snap_to_element((500, 500), [], (1080, 2400), editable=True, text="x")
    assert g.frame is None


def test_snap_type_onto_non_editable_target_is_ungroundable():
    # M7-M2: a type whose resolved node is NOT editable (e.g. the model typed with no prior click,
    # so (0,0) snapped to the root container) must NOT set text on the wrong element — ungroundable
    # so the loop feeds it back and the model clicks a field first (no accidental type into root).
    tree = [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),  # not editable
        _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
    ]
    g = snap_to_element((0, 0), tree, (1080, 2400), editable=True, text="hello")
    assert g.frame is None and g.method == "none"
    # a type aimed at the real editable field still grounds normally
    g2 = snap_to_element((510, 302), tree, (1080, 2400), editable=True, text="hello")
    assert g2.frame == {"type": "element_set_text", "resource_id": "app:id/field", "text": "hello"}


def test_find_target_node_prefers_wanted_kind():
    tree = [
        _node(0, "400,600,700,850", clickable=True, resource_id="clicky"),
        _node(1, "400,600,700,850", editable=True, resource_id="editable"),
    ]
    # same bounds, both contain the point; prefer_editable picks the editable one
    n = find_target_node(540, 720, tree, prefer_editable=True)
    assert n["resource_id"] == "editable"
    n2 = find_target_node(540, 720, tree, prefer_editable=False)
    assert n2["resource_id"] == "clicky"


# ── swipe / drag / long-press grounding ──────────────────────────────────────────────
def test_swipe_grounds_to_coordinate_swipe():
    g = snap_swipe_to_coordinate((100, 100), (100, 800), (1080, 2400), duration_ms=800)
    assert g.frame["type"] == "coordinate_swipe"
    assert g.frame["x"] == 108 and g.frame["duration_ms"] == 800
    assert g.frame["x2"] == 108 and g.frame["y2"] == 1921


def test_swipe_skipped_on_coordinate_less_device():
    g = snap_swipe_to_coordinate((1, 1), (1, 1), (1080, 2400), supports_coordinate=False)
    assert g.frame is None


# ── device dimension derivation ──────────────────────────────────────────────────────
def _fake_png(width, height):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return sig + ihdr


def test_dimensions_from_screenshot_png():
    obs = {"screenshot": base64.b64encode(_fake_png(1440, 3120)).decode(), "ui_tree": []}
    assert derive_device_dimensions(obs) == (1440, 3120)


def test_dimensions_from_tree_bounds_when_no_screenshot():
    obs = {"ui_tree": [_node(0, "0,0,1080,2400"), _node(1, "10,10,900,1000")]}
    assert derive_device_dimensions(obs) == (1080, 2400)


def test_dimensions_default_when_nothing_available():
    assert derive_device_dimensions({"ui_tree": []}) == (1080, 2400)
