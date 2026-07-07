"""CURRENT_OPERATOR is a single canonical LIVE source (no cross-module staleness).

Regression guard for the verified bug: several modules used to do
``global CURRENT_OPERATOR; CURRENT_OPERATOR = <op>`` inside THEIR OWN module
namespace (``chat_routes``, ``tasks``) while OTHER modules read a by-value
top-level ``from config import CURRENT_OPERATOR`` copy (``monitoring``,
``admin_routes`` /mint & /checkpoint). Writers updated their own alias and
readers never saw it — a fragmented, permanently-stale fallback frozen at
import-time ``USERS_DEFAULT``.

The fix: one canonical writer ``config.set_current_operator`` and one live
reader ``config.current_operator`` on the config module. Every writer calls the
setter (mutates the config global) and every reader calls the getter (reads that
same global), so an update on any turn is seen everywhere with NO restart.

Isolation: snapshot+restore ``config.CURRENT_OPERATOR`` so the throwaway values
set here don't leak into other tests.
"""
import pytest

import Orchestrator.config as cfg
import Orchestrator.monitoring as mon
import Orchestrator.routes.admin_routes as ar
import Orchestrator.tasks as tasks_mod
import Orchestrator.routes.chat_routes as chat_mod


@pytest.fixture(autouse=True)
def _restore_current_operator():
    snap = cfg.CURRENT_OPERATOR
    yield
    cfg.set_current_operator(snap)


def test_setter_getter_roundtrip():
    cfg.set_current_operator("Xavier")
    assert cfg.current_operator() == "Xavier"
    # the getter reflects the config module global, not a frozen copy.
    assert cfg.CURRENT_OPERATOR == "Xavier"

    cfg.set_current_operator("Yara")
    assert cfg.current_operator() == "Yara"


def test_reader_modules_see_live_update_without_restart():
    """A module that previously held a by-value ``CURRENT_OPERATOR`` copy now
    imports the ``current_operator`` getter and sees writes live."""
    # monitoring + admin_routes are the readers; they must reference the ONE
    # canonical getter object, not a snapshot value bound at import.
    assert mon.current_operator is cfg.current_operator
    assert ar.current_operator is cfg.current_operator

    cfg.set_current_operator("Zed")
    # both reader modules reflect the change with no reload/restart.
    assert mon.current_operator() == "Zed"
    assert ar.current_operator() == "Zed"


def test_writer_modules_update_the_canonical_global():
    """Writers (chat_routes /chat, tasks process_chat_task) route through the ONE
    canonical setter, so their per-turn update lands on the config global that
    every reader observes."""
    assert tasks_mod.set_current_operator is cfg.set_current_operator
    assert chat_mod.set_current_operator is cfg.set_current_operator

    # simulate a writer's per-turn update and confirm a reader observes it.
    tasks_mod.set_current_operator("Wren")
    assert cfg.current_operator() == "Wren"
    assert mon.current_operator() == "Wren"
