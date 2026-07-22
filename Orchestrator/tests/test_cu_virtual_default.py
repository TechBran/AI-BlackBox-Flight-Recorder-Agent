"""M9: virtual is the default launch mode (per-session display, no native
arbiter claim); native is an explicit opt-in that still claims the shared display."""
import pytest
from Orchestrator.browser import session_manager as bsm
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.browser import display_arbiter as da


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ComputerUseSession, "is_alive", lambda self: True)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    for reg in (bsm._sessions, bsm._operator_sessions):
        reg.clear()
    da._reservations.clear()
    yield
    for reg in (bsm._sessions, bsm._operator_sessions):
        reg.clear()
    da._reservations.clear()


def test_default_session_is_virtual_not_native():
    s = bsm.get_or_create_session("op")
    assert s.native_mode is False


def test_virtual_launch_leaves_the_native_display_free(monkeypatch):
    # A virtual session running does NOT register as the native-display owner.
    s = bsm.get_or_create_session("op")
    s.native_mode = False
    s.status = "running"
    assert da.local_display_owner() is None  # native mutex untouched by virtual work


def test_native_launch_claims_the_native_display(monkeypatch):
    # An explicit-opt-in native session that is running DOES own the shared display.
    s = bsm.get_or_create_session("op")
    s.native_mode = True
    s.status = "running"
    owner = da.local_display_owner()
    assert owner is not None and owner.kind == "browser" and owner.session_id == s.session_id
