"""uvicorn owns SIGTERM/SIGINT, so a systemctl stop/restart drains gracefully.

startup.py used to call signal.signal(SIGTERM/SIGINT, shutdown_handler) at
import time. uvicorn installs its own handlers (capture_signals) BEFORE it
imports the app, so ours replaced them, and ours ended in sys.exit(0): on every
systemctl stop/restart the process died inside the running loop, with no
--timeout-graceful-shutdown drain, no lifespan shutdown (scheduler, Asterisk
audio subprocess, SMS AMI, cellular) and in-flight chats cut mid-stream (audit
O01-core-runtime-01, X05-architecture-04).
"""
from __future__ import annotations

import importlib
import signal
import sys

from fastapi import FastAPI


def _defined_in_orchestrator(handler) -> bool:
    module = getattr(handler, "__module__", "") or ""
    return module == "Orchestrator" or module.startswith("Orchestrator.")


def test_importing_startup_installs_no_signal_handler(monkeypatch):
    """Fake signal.signal and import startup.py afresh: it must not register a
    SIGTERM or SIGINT handler (the base code registered both)."""
    import Orchestrator
    import Orchestrator.checkpoint as checkpoint
    import Orchestrator.startup as original

    installed = []

    def fake_signal(signum, handler):
        installed.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    # A throwaway app, so the fresh import's hooks, middleware and mounts never
    # land on the shared one; the original module comes back afterwards.
    monkeypatch.setattr(checkpoint, "app", FastAPI())
    monkeypatch.delitem(sys.modules, "Orchestrator.startup")
    monkeypatch.setattr(Orchestrator, "startup", original, raising=False)

    fresh = importlib.import_module("Orchestrator.startup")

    assert fresh is not original
    assert [s for s, _ in installed if s in (signal.SIGTERM, signal.SIGINT)] == []
    assert not hasattr(fresh, "shutdown_handler")


def test_the_app_leaves_sigterm_and_sigint_to_the_server():
    """After the whole app is imported, neither handler is one of ours."""
    import Orchestrator.app  # noqa: F401

    for signum in (signal.SIGTERM, signal.SIGINT):
        handler = signal.getsignal(signum)
        assert not _defined_in_orchestrator(handler), (
            f"{signal.Signals(signum).name} is handled by {handler!r}, which "
            "replaces uvicorn's graceful-shutdown handler")
