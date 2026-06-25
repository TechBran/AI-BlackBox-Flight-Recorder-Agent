"""MN.7 — embeddings watcher notify wiring tests.

On a STATE TRANSITION (the previous health.json state differs from this run's
state), the watcher fires a notify(operator="system", category="index", ...) so
the operator learns the embedding index went ok→broken / ok→superseded (or
recovered). The watcher's run_health_check is already async (on the event loop),
so it awaits notify directly — no sync bridge needed here.

Invariants asserted:
  * ok→broken transition fires one index notify.
  * A STEADY state (previous == current, e.g. broken→broken on the hourly
    recheck) does NOT re-notify — no spam.
  * notify is awaited with operator="system" + category="index".
  * A notify failure NEVER breaks run_health_check (it still returns the health
    dict and writes health.json).

The probe / catalog / migration seams are mocked so nothing touches a provider,
and notify itself is mocked so nothing touches the real bus.
"""

import pytest

import Orchestrator.embeddings.watcher as watcher_mod


@pytest.fixture
def notify_calls(monkeypatch):
    """Capture notify(...) awaits inside the watcher; no real bus."""
    calls = []

    async def fake_notify(operator, title, body, category="general", **k):
        calls.append(
            {"operator": operator, "title": title, "body": body, "category": category}
        )

    monkeypatch.setattr(watcher_mod, "notify", fake_notify)
    return calls


@pytest.fixture
def stub_watcher(monkeypatch):
    """Stub the heavy seams so run_health_check is offline + deterministic.

    Returns a small control object whose attributes the individual tests set:
      .probe_ok, .listed, .successor, .prev_state
    plus capture of whether _write_health was called.
    """
    class Ctl:
        probe_ok = True
        listed = True
        successor = None
        prev_state = "ok"
        written = []

    ctl = Ctl()

    # The probe-failure path sleeps RETRY_PROBE_DELAY_S (60s real-time) before
    # re-probing. Collapse it to 0 so the broken-transition tests stay fast.
    monkeypatch.setattr(watcher_mod, "RETRY_PROBE_DELAY_S", 0)

    monkeypatch.setattr(watcher_mod, "get_active_slug", lambda: "gemini-embedding-001")
    monkeypatch.setattr(
        watcher_mod, "EMBEDDING_MODELS",
        {"gemini-embedding-001": {"provider": "gemini", "model_id": "models/gemini-embedding-001"}},
    )

    async def fake_probe(active):
        return ctl.probe_ok, (None if ctl.probe_ok else "ProbeError: down")

    async def fake_catalog(entry):
        return ctl.listed, ctl.successor, None

    monkeypatch.setattr(watcher_mod, "_probe_active", fake_probe)
    monkeypatch.setattr(watcher_mod, "_catalog_check", fake_catalog)
    monkeypatch.setattr(watcher_mod, "_registry_slug_for", lambda p, v: None)
    monkeypatch.setattr(watcher_mod, "_previous_state", lambda: ctl.prev_state)
    monkeypatch.setattr(watcher_mod, "_write_health", lambda h: ctl.written.append(h))

    # broken-path helpers (only reached when state == broken AND prev == broken)
    async def fake_pick(active, succ):
        return None, "no target"

    async def fake_heal(active):
        return 0

    monkeypatch.setattr(watcher_mod, "_pick_migration_target", fake_pick)
    monkeypatch.setattr(watcher_mod, "_gap_heal", fake_heal)
    return ctl


@pytest.mark.asyncio
async def test_ok_to_broken_transition_fires_index_notify(stub_watcher, notify_calls):
    stub_watcher.prev_state = "ok"
    stub_watcher.probe_ok = False  # → state becomes "broken"

    health = await watcher_mod.run_health_check()

    assert health["state"] == "broken"
    assert len(notify_calls) == 1
    assert notify_calls[0]["operator"] == "system"
    assert notify_calls[0]["category"] == "index"


@pytest.mark.asyncio
async def test_steady_broken_does_not_renotify(stub_watcher, notify_calls):
    """broken→broken (the hourly recheck) must NOT re-notify — no spam."""
    stub_watcher.prev_state = "broken"
    stub_watcher.probe_ok = False  # stays broken

    health = await watcher_mod.run_health_check()

    assert health["state"] == "broken"
    assert notify_calls == []


@pytest.mark.asyncio
async def test_ok_to_superseded_transition_fires_notify(stub_watcher, notify_calls):
    stub_watcher.prev_state = "ok"
    stub_watcher.probe_ok = True
    stub_watcher.listed = False  # → superseded

    health = await watcher_mod.run_health_check()

    assert health["state"] == "superseded"
    assert len(notify_calls) == 1
    assert notify_calls[0]["category"] == "index"


@pytest.mark.asyncio
async def test_steady_ok_does_not_notify(stub_watcher, notify_calls):
    stub_watcher.prev_state = "ok"
    stub_watcher.probe_ok = True
    stub_watcher.listed = True

    health = await watcher_mod.run_health_check()

    assert health["state"] == "ok"
    assert notify_calls == []


@pytest.mark.asyncio
async def test_notify_failure_does_not_break_health_check(stub_watcher, monkeypatch):
    """A notify that raises must not stop run_health_check returning health."""
    stub_watcher.prev_state = "ok"
    stub_watcher.probe_ok = False  # → broken transition

    async def boom(*a, **k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(watcher_mod, "notify", boom)

    health = await watcher_mod.run_health_check()

    assert health["state"] == "broken"
    # health.json still written despite the notify failure.
    assert stub_watcher.written
