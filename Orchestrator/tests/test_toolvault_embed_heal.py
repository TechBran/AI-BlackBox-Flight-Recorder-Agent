"""Fresh-box guarantee: ToolVault tool embeddings self-heal the moment a box
gains an embedding provider — no restart, no model-switch required.

ToolVault/embeddings.json is gitignored (each box embeds its own tool vectors
under ITS chosen model). A box that keeps the DEFAULT embedding model never
triggers an /embeddings/migrate, so the onboarding /save endpoint kicks a
fire-and-forget ToolVault embed sync when API keys land — otherwise semantic
tool selection (use_computer, the CLI-agent tools, ...) would stay dark from
first boot until a manual restart. These tests pin that wiring + its safety
(never breaks /save, never raises out of the daemon thread).
"""
import pytest

from Orchestrator.routes import onboarding_routes as ob
import Orchestrator.toolvault.embeddings as tv_emb
import Orchestrator.toolvault.registry as tv_reg


# ── /save heal trigger ───────────────────────────────────────────────────────

def test_save_triggers_tool_embedding_heal(monkeypatch):
    """A successful /save fires the ToolVault embed heal exactly once, with the
    reason tag, AFTER the env write."""
    calls = {"env": None, "sync": []}
    monkeypatch.setattr(ob, "update_env",
                        lambda secrets: (calls.__setitem__("env", secrets), {"ok": True})[1])
    monkeypatch.setattr(tv_emb, "sync_tool_embeddings_bg",
                        lambda reason="": calls["sync"].append(reason))

    out = ob.save_secrets(ob.SaveRequest(secrets={"GOOGLE_API_KEY": "x"}))

    assert out == {"ok": True}
    assert calls["env"] == {"GOOGLE_API_KEY": "x"}   # keys were written
    assert calls["sync"] == ["onboarding key save"]  # heal fired once, tagged


def test_failed_save_does_not_trigger_heal(monkeypatch):
    """A rejected /save (update_env raises ValueError → 400) must NOT fire the
    heal — nothing was persisted, so there is nothing to re-embed for."""
    from fastapi import HTTPException
    fired = []

    def boom(secrets):
        raise ValueError("bad key")
    monkeypatch.setattr(ob, "update_env", boom)
    monkeypatch.setattr(tv_emb, "sync_tool_embeddings_bg",
                        lambda reason="": fired.append(reason))

    with pytest.raises(HTTPException) as ei:
        ob.save_secrets(ob.SaveRequest(secrets={"X": "y"}))
    assert ei.value.status_code == 400
    assert fired == []


def test_heal_trigger_never_breaks_save(monkeypatch):
    """If the heal trigger itself blows up (bad import, etc.), /save must still
    return the env-write result — the heal is best-effort."""
    monkeypatch.setattr(ob, "update_env", lambda secrets: {"ok": True})

    def boom(reason=""):
        raise RuntimeError("heal import exploded")
    monkeypatch.setattr(tv_emb, "sync_tool_embeddings_bg", boom)

    out = ob.save_secrets(ob.SaveRequest(secrets={"X": "y"}))
    assert out == {"ok": True}


# ── the background helper itself ─────────────────────────────────────────────

def test_sync_tool_embeddings_bg_calls_sync_with_canonical(monkeypatch):
    """The helper embeds the canonical tool list under the active model."""
    seen = {}
    monkeypatch.setattr(tv_reg, "load_canonical",
                        lambda: [{"name": "x", "description": "y"}])
    monkeypatch.setattr(tv_emb, "sync_embeddings",
                        lambda canonical: seen.setdefault("canon", canonical) or {"x": {}})

    t = tv_emb.sync_tool_embeddings_bg("test")
    t.join(timeout=5)

    assert not t.is_alive()
    assert seen["canon"] == [{"name": "x", "description": "y"}]


def test_sync_tool_embeddings_bg_is_nonfatal(monkeypatch):
    """A failing sync (e.g. no provider key yet) must be swallowed inside the
    daemon thread — the heal can never crash the caller."""
    monkeypatch.setattr(tv_reg, "load_canonical",
                        lambda: [{"name": "x", "description": "y"}])

    def boom(canonical):
        raise RuntimeError("no embedding provider reachable")
    monkeypatch.setattr(tv_emb, "sync_embeddings", boom)

    t = tv_emb.sync_tool_embeddings_bg("test")
    t.join(timeout=5)
    assert not t.is_alive()  # finished cleanly, no exception propagated
