import pytest
from unittest.mock import AsyncMock, patch

from ugv_tools_api.supervisor.tool_handlers import (
    exec_get_robot_state,
    exec_dispatch_er_mission,
    exec_cancel_er_mission,
    exec_get_er_mission_status,
    exec_emergency_stop,
    exec_lights,
    exec_gimbal_look_at,
    MissionTracker,
)


# Patch at the import site (ugv_tools_api.supervisor.tool_handlers.httpx.AsyncClient)
# rather than module-level (httpx.AsyncClient). Narrower scope prevents ghost
# interference with other tests that might import httpx directly.
PATCH_TARGET = "ugv_tools_api.supervisor.tool_handlers.httpx.AsyncClient"


def _mock_ok(status: int = 200, payload=None):
    """Build an AsyncMock response with sync json() and requested status.

    Note: httpx response.json() is sync, so json= is a plain lambda, NOT
    an AsyncMock(return_value=...). Easy to misread.
    """
    m = AsyncMock()
    m.status_code = status
    m.json = lambda: (payload if payload is not None else {})
    m.text = ""
    return m


def _mock_err(status: int, body: str = "boom"):
    m = AsyncMock()
    m.status_code = status
    m.text = body
    m.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
    return m


@pytest.fixture
def cfg():
    from ugv_tools_api.supervisor.config import SupervisorConfig
    return SupervisorConfig(
        google_api_key="x", model="m", voice="v", language_code="en-US",
        mic_device="", spk_device="", tools_api_url="http://tools",
        er_url="http://er", camera_topic="", costmap_topic="",
        handle_store_path="",
    )


# ── Happy paths ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_robot_state_calls_both_endpoints(cfg):
    client = AsyncMock()
    client.post.side_effect = [
        _mock_ok(payload={"result": {"x": 1, "y": 2, "yaw_deg": 90}}),
        _mock_ok(payload={"result": {"sectors_m": {"front": 0.5}}}),
    ]
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_get_robot_state(cfg)
    assert res["odom"]["x"] == 1
    assert res["lidar"]["sectors_m"]["front"] == 0.5
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_dispatch_er_mission_returns_id(cfg):
    tracker = MissionTracker()
    client = AsyncMock()
    client.post.return_value = _mock_ok(payload={"mission_id": "m-123", "status": "pending"})
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_dispatch_er_mission(cfg, tracker, mission="drive forward")
    assert res["mission_id"] == "m-123"
    assert tracker.active_id == "m-123"


@pytest.mark.asyncio
async def test_dispatch_without_replace_fails_if_active(cfg):
    tracker = MissionTracker()
    tracker.active_id = "prior"
    # No patch needed — guard short-circuits before any HTTP call.
    res = await exec_dispatch_er_mission(cfg, tracker, mission="x")
    assert "error" in res
    assert "active" in res["error"].lower()


@pytest.mark.asyncio
async def test_cancel_mission_clears_tracker(cfg):
    tracker = MissionTracker()
    tracker.active_id = "m-7"
    client = AsyncMock()
    client.post.return_value = _mock_ok(payload={"status": "aborted"})
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_cancel_er_mission(cfg, tracker, reason="test")
    assert res["status"] == "aborted"
    assert tracker.active_id is None


@pytest.mark.asyncio
async def test_emergency_stop_posts_to_tools_api(cfg):
    client = AsyncMock()
    client.post.return_value = _mock_ok(payload={"result": "ok"})
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_emergency_stop(cfg)
    args, _ = client.post.call_args
    assert args[0].endswith("/tool/system_emergency_stop")
    assert res["result"] == "ok"


# ── Error paths (new in the quality fix pass) ────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_replace_current_aborts_then_dispatches(cfg):
    tracker = MissionTracker()
    tracker.active_id = "old-id"
    client = AsyncMock()
    # First call: abort prior (returns 200). Second call: create new.
    client.post.side_effect = [
        _mock_ok(payload={"status": "aborted"}),
        _mock_ok(payload={"mission_id": "new-id", "status": "pending"}),
    ]
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_dispatch_er_mission(
            cfg, tracker, mission="new mission", replace_current=True,
        )
    assert res["mission_id"] == "new-id"
    assert tracker.active_id == "new-id"
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_dispatch_replace_current_refuses_when_abort_fails(cfg):
    """If abort fails, do NOT dispatch a new mission — avoids two concurrent missions."""
    tracker = MissionTracker()
    tracker.active_id = "old-id"
    client = AsyncMock()
    client.post.side_effect = [_mock_err(503, body="ER unavailable")]
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_dispatch_er_mission(
            cfg, tracker, mission="new mission", replace_current=True,
        )
    assert "error" in res
    assert "abort" in res["error"].lower() or "old-id" in res["error"]
    # tracker retains old-id since we don't know if it's really dead
    assert tracker.active_id == "old-id"
    # Only one POST was made (the abort). No dispatch follow-up.
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_cancel_preserves_id_on_failure(cfg):
    """If cancel fails, active_id MUST stay so operator can retry/investigate."""
    tracker = MissionTracker()
    tracker.active_id = "m-7"
    client = AsyncMock()
    client.post.return_value = _mock_err(500, body="ER crashed")
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_cancel_er_mission(cfg, tracker, reason="test")
    assert "error" in res
    assert res.get("mission_id") == "m-7"
    # Tracker NOT cleared
    assert tracker.active_id == "m-7"


@pytest.mark.asyncio
async def test_get_robot_state_surfaces_downstream_http_error(cfg):
    """5xx on either gathered endpoint collapses to a single error dict."""
    client = AsyncMock()
    client.post.side_effect = [
        _mock_ok(payload={"result": {"x": 1}}),
        _mock_err(503, body="lidar service down"),
    ]
    with patch(PATCH_TARGET) as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_get_robot_state(cfg)
    assert "error" in res
    assert "lidar" in res["error"].lower()


@pytest.mark.asyncio
async def test_emergency_stop_transport_error_returns_error_dict(cfg):
    """Transport failure (connection refused) returns error dict, does NOT raise."""
    import httpx
    with patch(PATCH_TARGET) as client_cls:
        client = AsyncMock()
        client.post.side_effect = httpx.ConnectError("connection refused")
        client_cls.return_value.__aenter__.return_value = client
        res = await exec_emergency_stop(cfg)
    assert "error" in res
    assert "transport" in res["error"].lower()
