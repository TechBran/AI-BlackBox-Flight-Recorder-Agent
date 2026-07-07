"""M1.5 — the DEFAULT operator is LIVE-READ (no restart lag).

``config.current_default()`` returns the live ``USERS_DEFAULT`` so a request that
OMITS an operator resolves to the CURRENT default, even after admin
``remove_operator``/``add_operator`` re-points ``config.USERS_DEFAULT`` in place.

Regression guard for the verified bug: several modules did a top-level
``from Orchestrator.config import USERS_DEFAULT`` which captured the value BY VALUE
at import — so once the default was re-pointed, those request-time fallbacks kept
naming the STALE (possibly phantom) operator until the service restarted. The
switched request-resolution paths now call ``current_default()`` instead.

Isolation: every mutation of ``config.USERS_DEFAULT`` goes through ``monkeypatch``,
which restores it on teardown — no config.ini is written by these tests.
"""
import Orchestrator.config as cfg
# A by-value snapshot, exactly like the stale top-level imports the fix replaced.
from Orchestrator.config import USERS_DEFAULT as _CAPTURED_AT_IMPORT

# Unique sentinel that no real box would ever use as its default operator.
_SENTINEL = "Zephyr-LIVE-DEFAULT-9137"


def test_current_default_returns_live_value():
    assert cfg.current_default() == cfg.USERS_DEFAULT


def test_current_default_reflects_a_changed_default(monkeypatch):
    # Simulate admin re-pointing the box default (as remove_operator does in place).
    monkeypatch.setattr(cfg, "USERS_DEFAULT", _SENTINEL)

    # The live read reflects the change immediately (no restart)...
    assert cfg.current_default() == _SENTINEL

    # ...while a plain top-level `from config import USERS_DEFAULT` copy stays stale:
    # this is exactly why the by-value consumers had to switch to current_default().
    assert _CAPTURED_AT_IMPORT != _SENTINEL
    # monkeypatch restores cfg.USERS_DEFAULT on teardown.


def test_switched_request_path_reflects_changed_default(monkeypatch):
    # agent_context.resolve_operator is one of the switched request-resolution paths:
    # an incoming turn with no operator falls back to the LIVE default.
    from Orchestrator import agent_context

    monkeypatch.setattr(cfg, "USERS_DEFAULT", _SENTINEL)

    # Empty / missing operator now resolves to the changed default WITHOUT a restart.
    assert agent_context.resolve_operator("", "[TEST]") == _SENTINEL
    assert agent_context.resolve_operator(None, "[TEST]") == _SENTINEL
    assert agent_context.resolve_operator("   ", "[TEST]") == _SENTINEL

    # A provided operator is passed through untouched (fallback only fires when empty).
    assert agent_context.resolve_operator("Anna", "[TEST]") == "Anna"


def test_switched_state_path_reflects_changed_default(monkeypatch):
    # state.get_state("") falls back to the live default operator's OpState.
    from Orchestrator import state

    monkeypatch.setattr(cfg, "USERS_DEFAULT", _SENTINEL)
    try:
        # Empty operator resolves to the changed default's state (same object as an
        # explicit lookup of the new default) — no restart needed.
        assert state.get_state("") is state.state_by_op[_SENTINEL]
    finally:
        # get_state() lazily creates a state_by_op entry via defaultdict; drop it so
        # the sentinel operator does not leak into other tests.
        state.state_by_op.pop(_SENTINEL, None)
