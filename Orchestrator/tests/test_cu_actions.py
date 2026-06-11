"""Coordinate-space scaling for ActionExecutor (anthropic-1280 vs gemini-999)."""
import pytest

from Orchestrator.browser import actions as A


@pytest.fixture
def fake_resolution(monkeypatch):
    def _set(w, h):
        # NATIVE_MODE pinned True so the tests don't depend on this box's
        # computer_use config — to_native reads both lazily from config.
        monkeypatch.setattr("Orchestrator.browser.config.NATIVE_MODE", True)
        monkeypatch.setattr("Orchestrator.browser.config.detect_native_resolution",
                            lambda force=False: (w, h))
    return _set


@pytest.mark.parametrize("native,cu_xy,expected", [
    ((1920, 1080), (640, 360), (960, 540)),    # 1080p: 1.5x
    ((3840, 2160), (640, 360), (1920, 1080)),  # 4K: 3x
    ((3440, 1440), (1280, 720), (3440, 1440)), # ultrawide bottom-right corner
])
def test_anthropic_space_scaling(fake_resolution, native, cu_xy, expected):
    fake_resolution(*native)
    ex = A.ActionExecutor()
    assert ex.to_native(*cu_xy) == expected


@pytest.mark.parametrize("native,gxy,expected", [
    ((1920, 1080), (999, 999), (1920, 1080)),
    ((1920, 1080), (0, 0), (0, 0)),
    ((3840, 2160), (500, 500), (1921, 1081)),  # int(500/999*3840)=1921
])
def test_gemini_space_scaling(fake_resolution, native, gxy, expected):
    fake_resolution(*native)
    ex = A.ActionExecutor(coord_space="gemini-999")
    assert ex.to_native(*gxy) == expected
