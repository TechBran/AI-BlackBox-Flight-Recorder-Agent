"""config.ini runtime writers re-read the file fresh under a lock and replace
it atomically (audit O05-ledger-persistence-21, O06-frontier-agent-11)."""
from __future__ import annotations

import configparser
import io
import threading
from pathlib import Path

import pytest

from Orchestrator import config
from Orchestrator.utils.config_ini import ConfigIniError, update_config_ini


@pytest.fixture
def live_config(isolated_stores, monkeypatch):
    """config.ini in the test's box root, with the process-global CFG,
    USERS_LIST (mutated in place by the routes) and default restored after."""
    path = Path("config.ini")
    if not path.exists():
        path.write_text("[users]\nlist = Ann\ndefault = Ann\n")
    snapshot = io.StringIO()
    config.CFG.write(snapshot)
    users = list(config.USERS_LIST)
    monkeypatch.setattr(config, "USERS_DEFAULT", config.USERS_DEFAULT)
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section("users"):
        parser.add_section("users")
    parser.set("users", "list", "Ann, Bob")
    parser.set("users", "default", "Ann")
    with path.open("w") as f:
        parser.write(f)
    config.USERS_LIST[:] = ["Ann", "Bob"]
    yield path
    config.USERS_LIST[:] = users
    for section in config.CFG.sections():
        config.CFG.remove_section(section)
    config.CFG.read_string(snapshot.getvalue())


def _roster(path: Path):
    parser = configparser.ConfigParser()
    parser.read(path)
    return [u.strip() for u in parser.get("users", "list").split(",") if u.strip()]


def test_operator_add_keeps_what_changed_on_disk_since_boot(live_config, app_client):
    """The add wrote the boot-time CFG back over the file, reverting any key
    another writer (or a hand edit) had put there since."""
    with live_config.open("a") as f:
        f.write("\n[probe_u24]\nkeep = yes\n")
    r = app_client.post("/operator/add", json={"name": "Cleo"})
    assert r.json()["status"] == "success"
    parser = configparser.ConfigParser()
    parser.read(live_config)
    assert parser.get("probe_u24", "keep") == "yes"
    assert _roster(live_config) == ["Ann", "Bob", "Cleo"]
    assert "Cleo" in config.USERS_LIST


def test_operator_remove_keeps_what_changed_on_disk_since_boot(live_config, app_client):
    with live_config.open("a") as f:
        f.write("\n[probe_u24]\nkeep = yes\n")
    r = app_client.delete("/operator/Bob")
    assert r.json()["status"] == "removed"
    parser = configparser.ConfigParser()
    parser.read(live_config)
    assert parser.get("probe_u24", "keep") == "yes"
    assert _roster(live_config) == ["Ann"]


def test_concurrent_operator_adds_lose_none(live_config):
    from Orchestrator.routes.admin_routes import add_operator
    names = [f"Op{i}" for i in range(12)]
    threads = [threading.Thread(target=add_operator, args=({"name": n},)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(_roster(live_config)) == sorted(["Ann", "Bob"] + names)
    assert sorted(config.USERS_LIST) == sorted(["Ann", "Bob"] + names)


def test_an_unparseable_config_is_never_rewritten(live_config, app_client):
    broken = "no section header here\n[users]\nlist = Ann\n"
    live_config.write_text(broken)
    r = app_client.post("/operator/add", json={"name": "Cleo"})
    assert r.json()["status"] == "error"
    assert live_config.read_text() == broken


def test_update_writes_nothing_when_the_edit_fails(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[users]\nlist = Ann\n")
    path.chmod(0o640)

    def bad_edit(parser):
        parser.set("users", "list", "Ann, Bob")
        raise RuntimeError("caller bailed")

    with pytest.raises(RuntimeError):
        update_config_ini(bad_edit, path)
    assert path.read_text() == "[users]\nlist = Ann\n"
    update_config_ini(lambda p: p.set("users", "list", "Ann, Bob"), path)
    assert "Ann, Bob" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o640          # mode kept
    assert [p.name for p in tmp_path.iterdir()] == ["config.ini"]


def test_update_refuses_an_unparseable_file(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("garbage\n")
    with pytest.raises(ConfigIniError):
        update_config_ini(lambda p: p.add_section("x"), path)
    assert path.read_text() == "garbage\n"


def test_flight_recorder_seed_keeps_what_changed_on_disk(live_config, monkeypatch):
    from Orchestrator import oversight
    monkeypatch.delenv("BLACKBOX_SKIP_FR_SEED", raising=False)
    monkeypatch.setattr(oversight, "retire_flight_report_cron_job", lambda: None)
    monkeypatch.setattr(oversight, "_save_state", lambda: None)
    with live_config.open("a") as f:
        f.write("\n[probe_u24]\nkeep = yes\n")
    oversight.ensure_flight_recorder()
    parser = configparser.ConfigParser()
    parser.read(live_config)
    assert parser.get("probe_u24", "keep") == "yes"
    assert oversight.FLIGHT_RECORDER_OPERATOR in _roster(live_config)


def test_local_capability_flag_uses_the_locked_fresh_writer(tmp_path, monkeypatch):
    from Orchestrator.routes import local_stack_routes
    path = tmp_path / "config.ini"
    path.write_text("[users]\nlist = Ann\n")
    monkeypatch.setattr(local_stack_routes, "CONFIG_INI", path)
    local_stack_routes._set_local_flag("stt", True)
    parser = configparser.ConfigParser()
    parser.read(path)
    assert parser.getboolean("local_models", "stt") is True
    assert parser.get("users", "list") == "Ann"
    assert [p.name for p in tmp_path.iterdir()] == ["config.ini"]   # no fixed .tmp left
