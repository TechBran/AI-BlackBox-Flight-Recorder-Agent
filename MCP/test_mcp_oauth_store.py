#!/usr/bin/env python3
"""The OAuth client and access-token stores are atomic, locked and
corrupt-safe (audit O20-mcp-server-12): a torn write used to read back as {}
and the next /register or /token overwrote every connector's record.

RUN: pytest MCP/test_mcp_oauth_store.py (MCP/run_tests.sh runs it).
"""
import importlib.util as _ilu
import json
import os
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
os.environ.setdefault("BLACKBOX_ROOT", str(_HERE.parent))
sys.path.insert(0, str(_HERE))

_spec = _ilu.spec_from_file_location("bbmcp_oauth_store_under_test", str(_HERE / "blackbox_mcp_server.py"))
srv = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(srv)


def _quarantined(path: Path):
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def test_corrupt_client_store_is_kept_when_a_client_registers(tmp_path, monkeypatch):
    store = tmp_path / "mcp_oauth_clients.json"
    store.write_text('{"client-a": {"redirect_uris": ["https://claude.ai/cb"]}, "cli')
    monkeypatch.setattr(srv, "BLACKBOX_MCP_OAUTH_CLIENTS_FILE", str(store))
    assert srv._load_oauth_clients() == {}
    srv._persist_oauth_client("client-b", {"redirect_uris": ["https://claude.ai/cb"]})
    (moved,) = _quarantined(store)
    assert moved.read_text().startswith('{"client-a"')
    assert set(json.loads(store.read_text())) == {"client-b"}
    assert store.stat().st_mode & 0o077 == 0


def test_concurrent_registrations_keep_every_client(tmp_path, monkeypatch):
    store = tmp_path / "mcp_oauth_clients.json"
    monkeypatch.setattr(srv, "BLACKBOX_MCP_OAUTH_CLIENTS_FILE", str(store))

    def worker(n):
        for i in range(10):
            srv._persist_oauth_client(f"c{n}-{i}", {"n": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(store.read_text())) == 40


def test_access_token_store_prunes_and_survives_corruption(tmp_path, monkeypatch):
    store = tmp_path / "mcp_oauth_tokens.json"
    monkeypatch.setattr(srv, "BLACKBOX_MCP_OAUTH_TOKENS_FILE", str(store))
    store.write_text(json.dumps({"old": {"operator": "Ann", "expiry": time.time() - 5}}))
    srv._persist_access_token("tok-1", "Ann", time.time() + 60)
    assert set(json.loads(store.read_text())) == {"tok-1"}
    store.write_text('{"tok-1": {"operator": "Ann", "exp')
    srv._persist_access_token("tok-2", "Ann", time.time() + 60)
    assert set(json.loads(store.read_text())) == {"tok-2"}
    (moved,) = _quarantined(store)
    assert moved.read_text().startswith('{"tok-1"')
