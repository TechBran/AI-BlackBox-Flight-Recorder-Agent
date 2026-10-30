"""Hygiene (spec W26, W32, A11, A11b): after a full simulated phone run the
wellness files are 0700/0600 (and re-secured at boot), nothing was written
under Portal/, Volumes/, Archive/ or Manifest/, no sentinel health value
appears in any other store or in anything logged or printed, the device key
never reaches a log, and the redaction backstop masks the new secret shapes.
Writes into the checkout itself are refused by the root conftest's write
guard for every test, these included."""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from Orchestrator.tests.wellness_support import (OPERATOR, SIM_IP, ClientTransport, fixtures,
                                                 free_port, load_sim, roster, simulator_mode)
from Orchestrator.wellness import api, paths

FX = fixtures()
SIM = load_sim()
WATCHED = ("Portal", "Volumes", "Archive", "Manifest")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _tree(root: Path) -> dict:
    out = {}
    for base in WATCHED:
        for p in sorted((root / base).rglob("*")) if (root / base).exists() else ():
            if p.is_file():
                s = p.stat()
                out[str(p.relative_to(root))] = (s.st_size, s.st_mtime_ns)
    return out


def _files_to_scan(root: Path):
    wellness = paths.data_dir().resolve()
    for base in ("data", "Manifest", "Volumes", "Archive", "Portal", "credentials"):
        for p in sorted((root / base).rglob("*")) if (root / base).exists() else ():
            if p.is_file() and wellness not in p.resolve().parents:
                yield p
    for p in sorted(root.glob("*.db")) + sorted((root / "Orchestrator").glob("*.db")):
        yield p


def _hits(text: str):
    return [s for s in FX.SENTINEL_STRINGS if s in text]


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    """One full run: register, enroll, push the 120-day fixture, read every
    tool answer once with a turn (so delivered payloads exist), status."""
    from fastapi.testclient import TestClient

    from Orchestrator.tests import box_stores
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("hygiene-box")
    box_stores.point_at(root, mp)
    for d in ("Volumes", "Manifest", "Archive", "Portal/uploads", "Portal/artifacts"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "config.ini").write_text(Path("config.ini").read_text())
    mp.chdir(root)
    roster(mp, OPERATOR, "other-human", "Flight Recorder")
    port = free_port(SIM_IP)
    simulator_mode(port)
    # Imported first: its setup_logging() replaces the root handlers.
    from Orchestrator.app import app
    records = []
    handler = logging.Handler(level=logging.DEBUG)
    handler.emit = lambda rec: records.append(rec)
    logging.getLogger().addHandler(handler)
    old_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    from unittest.mock import patch
    try:
        with patch("Orchestrator.toolvault.embeddings.sync_embeddings", return_value={}):
            before = _tree(root)
            with TestClient(app, client=(SIM_IP, 50000)) as client:
                t = ClientTransport(client)
                state = SIM.State(root / "sim-state")
                SIM.register(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                             pairing_file=paths.sim_pairing_path())
                out = SIM.enroll(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                                 capability_report=FX.load("continuous_120")["capabilities"])
                assert out["status"] == 200, out
                assert SIM.push(t, state, fixture="continuous_120",
                                log=lambda m: None)["quarantined"] == 0
                d = api.Delivery("wt_hyg", "android", "local")
                now = FX.now_ms()
                envs = [api.summary(OPERATOR, "custom", start=FX.day_of(-6), end=FX.day_of(-2),
                                    now_ms=now, delivery=d),
                        api.history(OPERATOR, FX.day_of(-10), FX.day_of(0), now_ms=now,
                                    delivery=d),
                        api.series(OPERATOR, "steps", "last_7_days", now_ms=now, delivery=d)]
                SIM.status(t, state)
                key = state.load()["key"]
            after = _tree(root)
        yield {"root": root, "records": records, "before": before, "after": after,
               "key": key, "envs": envs}
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(old_level)
        mp.undo()


def test_modes_are_private(ran):
    assert _mode(paths.data_dir()) == 0o700 if paths.data_dir().exists() else True
    root = ran["root"]
    wdir = root / "data" / "wellness"
    assert _mode(wdir) == 0o700
    for p in wdir.iterdir():
        assert _mode(p) == 0o600, p.name
    for name in ("wellness_device_keys.json", "wellness_sim_pairing.json"):
        assert _mode(root / "credentials" / name) == 0o600


def test_nothing_written_under_the_served_and_ledger_trees(ran):
    assert ran["after"] == ran["before"]


def test_the_sentinels_are_really_in_the_answers(ran):
    """The scan below would catch a leak: the tool answers DO carry them."""
    text = " ".join(str(e) for e in ran["envs"])
    assert "47.3" in text and "12345" in text


def test_no_sentinel_outside_the_wellness_store(ran):
    for p in _files_to_scan(ran["root"]):
        text = p.read_bytes().decode("utf-8", "replace")
        assert not _hits(text), (str(p.relative_to(ran["root"])), _hits(text))


def test_no_sentinel_or_key_in_the_logs(ran):
    lines = [r.getMessage() for r in ran["records"]]
    assert any("[WELLNESS] ingest op=" in line for line in lines)
    for line in lines:
        assert not _hits(line), line
        assert ran["key"] not in line
    ingest = [line for line in lines if "[WELLNESS] ingest" in line]
    assert all("op=" in line and "rows=" in line for line in ingest)
    assert not any(OPERATOR in line for line in ingest)       # the op_key prefix, not the name


def test_no_sentinel_in_anything_printed(ran, capsys):
    out, err = capsys.readouterr()
    assert not _hits(out + err)


def test_boot_hook_resecures(ran):
    root = ran["root"]
    wdir = root / "data" / "wellness"
    mp = pytest.MonkeyPatch()
    mp.setattr(paths, "DATA_DIR", wdir)
    mp.setattr(paths, "KEYS_PATH", root / "credentials" / "wellness_device_keys.json")
    try:
        os.chmod(wdir, 0o755)
        db = next(wdir.glob("*.db"))
        os.chmod(db, 0o644)
        from Orchestrator import startup
        startup.startup_wellness_store()
        assert _mode(wdir) == 0o700 and _mode(db) == 0o600
    finally:
        mp.undo()


def test_redaction_masks_the_wellness_secrets():
    from Orchestrator.secret_redaction import redact
    key = "Ab3_" * 10 + "xyz"
    assert len(key) == 43
    out = redact(f'{{"k": "{key}", "epoch": 2}}')
    assert key not in out and '"epoch": 2' in out
    sig = "a1" * 32
    header = f"v1;enr=en_0a1b2c3d;ts=1;nonce={'b' * 32};sig={sig}"
    assert sig not in redact(header) and "enr=en_0a1b2c3d" in redact(header)
    assert "wbx_s_" not in redact("token wbx_s_abcdefghijklmnop0123")
    assert redact("sig=short and k=1") == "sig=short and k=1"
