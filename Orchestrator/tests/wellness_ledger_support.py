"""Shared helpers for the ledger-rule tests (not a test module): a real
ledger, GoldMaster, manifest and snapshot index under a test's cwd, mints
through the real perform_mint, and the fixture operator's health results
delivered into its store.

    box = ledger_box(isolated_stores.root, monkeypatch)   # cwd is the root
    env = envelopes()["night"]                             # a fixture answer
    deliver(st, env, "wt_x")                               # a delivered row
    sid = mint_turn(OPERATOR, "how did I sleep?", "...", wellness=gauge)
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from Orchestrator.tests.wellness_support import OPERATOR, fixtures

ALL_ASK = {"sleep": "ask", "heart": "ask", "activity": "ask", "blood_pressure": "ask"}
FX = fixtures()


def ledger_box(root: Path, monkeypatch) -> SimpleNamespace:
    """The ledger files a mint needs, under `root` (the test's cwd: the
    ledger paths are cwd-relative), the snapshot index cache reset, fresh
    operator state, no background flight reports, and the embed seam
    recorded instead of called (`embedded` holds every text it was given)."""
    from Orchestrator import fossils, state, tasks
    from Orchestrator.embeddings import search as search_mod
    gm = root / "GoldMaster"
    gm.mkdir(parents=True, exist_ok=True)
    (gm / "v7.0.txt").write_bytes(b"gold")
    (gm / "v7.0.sha256").write_text(hashlib.sha256(b"gold").hexdigest())
    for d in ("Volumes", "Manifest", "Archive"):
        (root / d).mkdir(parents=True, exist_ok=True)
    manifest = root / "Manifest" / "manifest.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({"archive": []}))
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(state, "state_by_op", defaultdict(state.OpState))
    monkeypatch.setattr(tasks, "collect_pending_media_artifacts", lambda *a, **k: [])
    monkeypatch.setenv("BLACKBOX_SKIP_FR_SEED", "1")
    embedded = []

    def fake_embed(text):
        embedded.append(text)
        return {}

    monkeypatch.setattr(search_mod, "embed_snapshot_for_index", fake_embed)
    return SimpleNamespace(root=root, vol=root / "Volumes" / "SNAPSHOT_VOLUME.txt",
                           embedded=embedded)


def mint_turn(operator: str, user_text: str, assistant_text: str, *,
              wellness: Optional[dict] = None, snap_text: Optional[str] = None) -> str:
    """One turn recorded and minted through the real perform_mint."""
    from Orchestrator import checkpoint, state
    from Orchestrator.volume import now_utc_iso
    s = state.get_state(operator)
    utc = now_utc_iso()
    tag = {"wellness": wellness} if wellness else {}     # as turn_persistence tags a turn
    s.add_conversation_turn({"role": "user", "utc": utc, "text": user_text, **tag})
    s.add_conversation_turn({"role": "assistant", "utc": utc, "text": assistant_text,
                             "snap_text": snap_text or assistant_text, **tag})
    return checkpoint.perform_mint(operator, "TEST")["snap_id"]


def snapshot_text(vol: Path, snap_id: str) -> str:
    text = vol.read_text(encoding="utf-8")
    start = text.rfind("\n", 0, text.index(f"— {snap_id} (7.1.0) ===")) + 1
    end = text.index(f"=== END SNAPSHOT — {snap_id}")
    end = text.index("\n", end) + 1
    return text[start:end]


def envelopes(operator: str = OPERATOR) -> dict:
    """Fixture answers that carry the leak-scan sentinels (enroll with ALL_ASK
    for the blood pressure one)."""
    from Orchestrator.wellness import api
    now = FX.now_ms()
    return {
        "night": api.summary(operator, "custom", start=FX.day_of(-5), end=FX.day_of(-5),
                             now_ms=now),
        "steps": api.history(operator, FX.day_of(-3), FX.day_of(-3), now_ms=now),
        "bp": api.summary(operator, "custom", start=FX.day_of(-2), end=FX.day_of(-2),
                          categories=["blood_pressure"], now_ms=now),
        "week": api.summary(operator, "last_7_days", now_ms=now),
        "series": api.series(operator, "heart_rate", "last_24_hours", now_ms=now),
        "sleeps": api.sessions(operator, "sleep", "last_7_days", now_ms=now),
        "workouts": api.sessions(operator, "exercise", "last_7_days", now_ms=now),
    }


def categories_of(env: dict) -> list:
    from Orchestrator.wellness import catalog
    cats = set(env.get("categories") or [])
    cats |= {m.get("category") for m in env.get("metrics") or [] if m.get("category")}
    if env.get("category"):
        cats.add(env["category"])
    for col in env.get("columns") or []:
        if catalog.category_of(col):
            cats.add(catalog.category_of(col))
    if env.get("kind") == "sleep":
        cats.add("sleep")
    if env.get("kind") == "exercise":
        cats.add("activity")
    return sorted(c for c in cats if c)


def deliver(st, env: dict, turn_key: Optional[str], *, provider: str = "anthropic",
            ts: Optional[int] = None, surface: str = "android", categories=None) -> str:
    """One delivered row, as api.Delivery writes it (the wellness tool path).
    `categories` defaults to what the answer covers."""
    st.record_delivery(env["ref"], turn_key, surface, provider, "model-test", env["tool"], {},
                       categories_of(env) if categories is None else categories, env, ts=ts)
    return env["ref"]
