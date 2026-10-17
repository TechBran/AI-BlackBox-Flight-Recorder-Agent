import pytest

from ugv_tools_api.supervisor.tool_declarations import ALL_TOOLS, tool_names

# UGV Beast sub-project (tests/supervisor/conftest.py): opt-in, -m ugv.
pytestmark = pytest.mark.ugv


EXPECTED = {
    "get_robot_state", "get_camera_view", "get_slam_map_view",
    "get_costmap_view", "set_watch_mode",
    "dispatch_er_mission", "cancel_er_mission", "get_er_mission_status",
    "emergency_stop", "lights_on", "lights_off", "gimbal_look_at",
}


def test_all_tools_present():
    assert set(tool_names()) == EXPECTED


def test_tool_names_ordering_matches_all_tools():
    assert tool_names() == tuple(t.name for t in ALL_TOOLS)


def test_dispatch_er_mission_schema_has_required_mission_param():
    d = next(t for t in ALL_TOOLS if t.name == "dispatch_er_mission")
    props = d.parameters.properties
    assert "mission" in props
    assert "mission" in (d.parameters.required or [])


def test_dispatch_er_mission_replace_current_defaults_true():
    d = next(t for t in ALL_TOOLS if t.name == "dispatch_er_mission")
    rc = d.parameters.properties["replace_current"]
    assert rc.default is True


def test_cancel_er_mission_reason_is_required():
    d = next(t for t in ALL_TOOLS if t.name == "cancel_er_mission")
    assert "reason" in (d.parameters.required or [])


def test_lights_on_which_defaults_both():
    d = next(t for t in ALL_TOOLS if t.name == "lights_on")
    which = d.parameters.properties["which"]
    assert which.default == "both"


def test_lights_off_which_defaults_both():
    d = next(t for t in ALL_TOOLS if t.name == "lights_off")
    which = d.parameters.properties["which"]
    assert which.default == "both"


def test_gimbal_pan_tilt_have_enforced_ranges():
    d = next(t for t in ALL_TOOLS if t.name == "gimbal_look_at")
    pan = d.parameters.properties["pan_deg"]
    tilt = d.parameters.properties["tilt_deg"]
    assert pan.minimum == -180 and pan.maximum == 180
    assert tilt.minimum == -45 and tilt.maximum == 90
