"""Coordinate-space scaling for ActionExecutor (anthropic-1280 vs gemini-999)."""
import pytest

from Orchestrator.browser import actions as A


@pytest.fixture
def fake_resolution(monkeypatch):
    def _set(w, h):
        # NATIVE_MODE pinned True so the tests don't depend on this box's
        # computer_use config. to_native reads the resolution lazily from config,
        # but the native/virtual decision is now the ActionExecutor INSTANCE flag
        # (captured from actions.NATIVE_MODE at construction, M9), so pin that too.
        monkeypatch.setattr("Orchestrator.browser.config.NATIVE_MODE", True)
        monkeypatch.setattr("Orchestrator.browser.actions.NATIVE_MODE", True)
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
    # /1000 per Google's CU contract (0-999 = 1000 buckets): coordinate 999
    # lands ON the last pixel band, never one past it (the /999 divisor mapped
    # 999 -> width, out of bounds).
    ((1920, 1080), (999, 999), (1918, 1078)),
    ((1920, 1080), (0, 0), (0, 0)),
    ((3840, 2160), (500, 500), (1920, 1080)),  # int(500/1000*3840)=1920
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
    # native/virtual is the instance flag now (M9): pin the actions-module global
    # so the default-constructed executor captures native_mode=False.
    monkeypatch.setattr("Orchestrator.browser.config.NATIVE_MODE", False)
    monkeypatch.setattr("Orchestrator.browser.actions.NATIVE_MODE", False)
    # anthropic-1280 space: virtual coords ARE frame pixels — identity.
    ex = A.ActionExecutor()
    assert ex.to_native(640, 360) == (640, 360)
    # gemini-999 space: virtual coords are NORMALIZED — identity passthrough was
    # the pre-2026-07-23 click bug. They de-normalize against the session
    # resolution (or the gemini backend default when unbound), /1000 per
    # Google's contract.
    ex_g = A.ActionExecutor(coord_space=A.COORD_SPACE_GEMINI, resolution=(1440, 900))
    assert ex_g.to_native(999, 999) == (1438, 899)
    assert ex_g.to_native(0, 0) == (0, 0)
