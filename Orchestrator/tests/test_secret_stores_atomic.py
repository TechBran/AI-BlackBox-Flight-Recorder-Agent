"""The token, device and secrets stores are atomic, locked and corrupt-safe
(audit rank 47: O15-scheduler-admin-07/-10, O19-local-stack-12,
O16-onboarding-update-14, X03-security-backend-14, O12-messaging-05)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _quarantined(path: Path):
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


# ── MCP bearer tokens (Manifest/mcp_tokens.json) ────────────────────────────

@pytest.fixture
def mcp(tmp_path, monkeypatch):
    from Orchestrator.routes import mcp_routes
    store = tmp_path / "mcp_tokens.json"
    monkeypatch.setattr(mcp_routes, "MCP_TOKENS_FILE", store)
    monkeypatch.setattr(mcp_routes, "MCP_RUNTIME_FILE", tmp_path / "mcp_runtime.json")
    monkeypatch.setattr(mcp_routes, "USERS_LIST", ["Alice", "Bob"])
    monkeypatch.setattr(mcp_routes, "_trigger_reload", lambda: False)
    app = FastAPI()
    app.include_router(mcp_routes.router)
    return TestClient(app), store


def test_corrupt_token_store_is_never_overwritten_by_a_mint(mcp):
    client, store = mcp
    store.write_text('{"bbmcp_alice_token": "Alice", "bbmcp_bob')
    r = client.post("/mcp/tokens", json={"operator": "Bob"})
    assert r.status_code == 200
    (moved,) = _quarantined(store)
    assert moved.read_text() == '{"bbmcp_alice_token": "Alice", "bbmcp_bob'
    assert list(json.loads(store.read_text()).values()) == ["Bob"]


def test_concurrent_mints_keep_every_token(mcp):
    client, store = mcp
    def worker():
        for _ in range(8):
            assert client.post("/mcp/tokens", json={"operator": "Alice"}).status_code == 200
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(store.read_text())) == 32
    assert store.stat().st_mode & 0o077 == 0


def test_cli_minter_never_overwrites_a_corrupt_store(tmp_path):
    """MCP/deploy/mint_token.py writes the same store from another process."""
    sys.path.insert(0, str(REPO_ROOT / "MCP" / "deploy"))
    try:
        import mint_token
    finally:
        sys.path.pop(0)
    store = tmp_path / "mcp_tokens.json"
    store.write_text('{"bbmcp_x": "Alice", ')
    mint_token._write_token(store, "bbmcp_new", "Bob")
    (moved,) = _quarantined(store)
    assert moved.read_text() == '{"bbmcp_x": "Alice", '
    assert json.loads(store.read_text()) == {"bbmcp_new": "Bob"}
    assert store.stat().st_mode & 0o077 == 0


def test_cli_minter_and_backend_mints_interleave_safely(mcp, tmp_path):
    client, store = mcp
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]);"
        "import mint_token, pathlib\n"
        "for i in range(30): mint_token._write_token(pathlib.Path(sys.argv[3]), f'bbmcp_cli{i}', 'Bob')\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script, str(REPO_ROOT / "MCP" / "deploy"),
                             str(REPO_ROOT), str(store)])
    for _ in range(30):
        client.post("/mcp/tokens", json={"operator": "Alice"})
    assert proc.wait(timeout=60) == 0
    values = list(json.loads(store.read_text()).values())
    assert values.count("Alice") == 30 and values.count("Bob") == 30


# ── on-device provider attestations (local_devices.json) ────────────────────

def test_corrupt_attestation_store_does_not_crash_and_is_kept(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    store = tmp_path / "local_devices.json"
    store.write_text('{"Ann": {"pixel": {"model_slug": ')
    monkeypatch.setattr(r, "STORE_FILE", store)
    reg = r.LocalProviderRegistry()          # used to raise JSONDecodeError
    assert reg.status("Ann")["available"] is False
    reg.attest(operator="Ann", device_id="fold", model_slug="m", version="1", sha256="s",
               delegate="gpu", autonomy_mode="permission")
    (moved,) = _quarantined(store)
    assert moved.read_text().startswith('{"Ann": {"pixel"')
    assert set(json.loads(store.read_text())["Ann"]) == {"fold"}
    assert store.stat().st_mode & 0o077 == 0


def test_failed_attestation_save_leaves_the_previous_store(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    store = tmp_path / "local_devices.json"
    monkeypatch.setattr(r, "STORE_FILE", store)
    reg = r.LocalProviderRegistry()
    reg.attest(operator="Ann", device_id="fold", model_slug="m", version="1", sha256="s",
               delegate="gpu", autonomy_mode="permission")
    good = store.read_text()
    with pytest.raises(TypeError):
        reg.attest(operator="Ann", device_id="tab", model_slug=object(), version="1",
                   sha256="s", delegate="gpu", autonomy_mode="permission")
    assert store.read_text() == good


# ── Gmail / Workspace OAuth tokens ──────────────────────────────────────────

def test_a_fresh_grant_moves_a_corrupt_gmail_token_aside(tmp_path, monkeypatch):
    """An unreadable token file still reports token_unreadable (the status
    route's needs_reauth state); the reconnect that follows saves over it,
    and the old bytes are kept instead of destroyed."""
    from Orchestrator.gmail import service
    monkeypatch.setattr(service, "TOKEN_DIR", tmp_path)
    (tmp_path / "Ann.json").write_text('{"refresh_token": "1//abc')
    with pytest.raises(ValueError):
        service.load_tokens("Ann")
    service.save_tokens("Ann", {"refresh_token": "1//fresh", "access_token": "ya29"})
    assert service.load_tokens("Ann")["refresh_token"] == "1//fresh"
    (moved,) = _quarantined(tmp_path / "Ann.json")
    assert moved.read_text() == '{"refresh_token": "1//abc'
    assert (tmp_path / "Ann.json").stat().st_mode & 0o077 == 0


# ── the wizard's .env ───────────────────────────────────────────────────────

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    from Orchestrator.onboarding import secrets_writer
    path = tmp_path / ".env"
    monkeypatch.setattr(secrets_writer, "ENV_FILE", path)
    return path


def _env_keys(path: Path):
    return {line.split("=", 1)[0] for line in path.read_text().splitlines()
            if "=" in line and not line.startswith("#")}


def test_concurrent_env_updates_lose_no_key(env_file):
    from Orchestrator.onboarding import secrets_writer

    def worker(n):
        for i in range(10):
            secrets_writer.update_env({f"KEY_{n}_{i}": "v"})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _env_keys(env_file) == {f"KEY_{n}_{i}" for n in range(5) for i in range(10)}


def test_concurrent_env_update_and_remove_stay_consistent(env_file):
    from Orchestrator.onboarding import secrets_writer
    secrets_writer.update_env({f"OLD_{i}": "v" for i in range(10)})

    def adder():
        for i in range(10):
            secrets_writer.update_env({f"NEW_{i}": "v"})

    def remover():
        for i in range(10):
            secrets_writer.remove_env_keys([f"OLD_{i}"])

    threads = [threading.Thread(target=adder), threading.Thread(target=remover)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _env_keys(env_file) == {f"NEW_{i}" for i in range(10)}


def test_env_writes_are_owner_only_and_leave_no_fixed_temp_file(env_file):
    from Orchestrator.onboarding import secrets_writer
    secrets_writer.update_env({"A_KEY": "1"})
    secrets_writer.remove_env_keys(["A_KEY"])
    assert env_file.stat().st_mode & 0o077 == 0
    assert not (env_file.parent / ".env.tmp").exists()
    leftovers = [p.name for p in env_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def _git_ignores(name: str) -> bool:
    proc = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", name],
                          capture_output=True)
    if proc.returncode not in (0, 1):
        pytest.skip(f"git check-ignore unavailable: {proc.stderr.decode(errors='replace')}")
    return proc.returncode == 0


def test_temp_files_of_the_root_env_and_config_ini_are_gitignored(env_file, tmp_path, monkeypatch):
    """A kill mid-write leaves the private temp sibling behind. At the
    serving tree's root it holds every API key, so git must ignore it (the
    updater refuses a dirty tree, and the repo is public). The names are the
    ones atomic_json really creates, not a copy of its naming rule."""
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    from Orchestrator.onboarding import secrets_writer
    from Orchestrator.utils import atomic_json, config_ini
    names = []
    real_mkstemp = atomic_json.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        names.append(os.path.basename(path))
        return fd, path

    monkeypatch.setattr(atomic_json.tempfile, "mkstemp", spy)
    secrets_writer.update_env({"A_KEY": "1"})
    secrets_writer.update_env({"B_KEY": "2"})            # backs the first file up
    ini = tmp_path / "config.ini"
    ini.write_text("[users]\nlist = Ann\n")
    config_ini.update_config_ini(lambda p: p.set("users", "list", "Ann, Bob"), path=ini)
    assert any(n.endswith(".tmp") and "backup" in n for n in names)
    assert any(".config.ini." in n for n in names)
    assert names and all(_git_ignores(n) for n in names), names
