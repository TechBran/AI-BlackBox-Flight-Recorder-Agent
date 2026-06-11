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
    ex = A.ActionExecutor(coord_space=A.COORD_SPACE_GEMINI)
    assert ex.to_native(*gxy) == expected


def test_unknown_coord_space_rejected():
    """A typo'd space must fail loudly at construction, not mis-scale clicks."""
    with pytest.raises(ValueError, match="gemini_999"):
        A.ActionExecutor(coord_space="gemini_999")


def test_scale_coord_none_passthrough(fake_resolution):
    fake_resolution(1920, 1080)
    assert A.ActionExecutor()._scale_coord(None) is None


def test_non_native_mode_identity(monkeypatch):
    monkeypatch.setattr("Orchestrator.browser.config.NATIVE_MODE", False)
    ex = A.ActionExecutor(coord_space=A.COORD_SPACE_GEMINI)
    assert ex.to_native(640, 360) == (640, 360)
