"""Ensure ER runs exactly one mission per invocation and then sleeps.

Integration test — requires ugv-er.service running on the Jetson. Skipped
on machines without that service. Validates the supervisor's assumption
that ER is a stateless executor: one POST /mission → one completion →
return to idle.
"""
import os
import time

import pytest


ER_URL = os.environ.get("ER_URL", "http://localhost:8082")


def _er_reachable() -> bool:
    import httpx
    try:
        with httpx.Client(timeout=2.0) as c:
            r = c.get(f"{ER_URL}/health")
        return r.status_code == 200
    except Exception:
        return False


# UGV robot hardware (ugv-er.service on the Jetson): opt-in only.
pytestmark = [pytest.mark.ugv, pytest.mark.live_service]


@pytest.fixture(autouse=True)
def _require_er():
    # Skip if ER isn't reachable (e.g., running on a laptop). Probed per test,
    # not at import, so collecting this module opens no socket.
    if not _er_reachable():
        pytest.skip(f"ugv-er not reachable at {ER_URL}")


def test_single_shot_mission():
    """POST a trivial mission, wait for terminal state, assert exactly
    one terminal event.
    """
    import httpx
    with httpx.Client(timeout=5.0) as c:
        r = c.post(f"{ER_URL}/mission", json={
            "operator": "test",
            "mission": "say hello to the operator and stop",
        })
        assert r.status_code == 200, r.text
        mid = r.json()["mission_id"]

    deadline = time.time() + 120
    final = None
    while time.time() < deadline:
        with httpx.Client(timeout=5.0) as c:
            s = c.get(f"{ER_URL}/mission/{mid}").json()
        if s.get("status") in ("completed", "failed", "aborted"):
            final = s
            break
        time.sleep(2.0)

    assert final is not None, f"mission did not terminate within 120s; last state: {s}"
    events = final.get("events") or []
    terminal = [e for e in events if e.get("type") in ("mission_done", "mission_fail")]
    assert len(terminal) == 1, (
        f"expected exactly one terminal event for a single mission; "
        f"got {len(terminal)}: {terminal}"
    )
