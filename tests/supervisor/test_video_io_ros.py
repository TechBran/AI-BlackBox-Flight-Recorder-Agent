"""Integration test — requires ROS2, the pantilt camera publisher, and
ideally Nav2's global costmap. Runs only on the Jetson inside the
ugv_waveshare container.

If rclpy isn't importable (e.g., running on a laptop without ROS), the
whole module skips cleanly.
"""
import time

import pytest

rclpy = pytest.importorskip("rclpy", reason="ROS2 rclpy not available")

# UGV Beast sub-project on the Jetson (ROS2 + cameras): opt-in, -m ugv.
pytestmark = pytest.mark.ugv


def test_camera_subscriber_captures_frames():
    from ugv_tools_api.supervisor.video_io import RosCamera

    cam = RosCamera(
        camera_topic="/camera/image/compressed",
        costmap_topic="/global_costmap/costmap",
    )
    cam.start()
    try:
        # Poll with a 10s deadline rather than a fixed sleep. Fast-exit as
        # soon as the first frame arrives (typical <500 ms on a warm
        # Jetson); tolerate cold boots where DDS discovery takes longer.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if cam.get_camera_jpeg() is not None:
                break
            time.sleep(0.1)
        jpeg = cam.get_camera_jpeg()
        assert jpeg is not None, (
            f"camera topic produced no frames within 10s "
            f"(received count: {cam._camera_cache.received})"
        )
        assert len(jpeg) > 5000, f"camera frame suspiciously small: {len(jpeg)} bytes"
        # Camera frames are JPEG; first two bytes are FFD8.
        assert jpeg[:2] == b'\xff\xd8', "camera frame is not a JPEG"
        # Also check the counter is incrementing — guards against a stale
        # cached frame from a prior test process leaking through.
        assert cam._camera_cache.received >= 1, "subscription delivered no messages"
        png = cam.get_costmap_png()
        if png is not None:
            assert png[:8] == b'\x89PNG\r\n\x1a\n', "costmap bytes are not a PNG"
    finally:
        cam.stop()
