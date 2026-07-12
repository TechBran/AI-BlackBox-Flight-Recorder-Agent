"""Provisioning + credential store for the xAI sovereign line.

House conventions (custom_servers.py precedent): STORE_PATH monkeypatched to
tmp_path so no test touches credentials/xai_phone.json; _api_post monkeypatched
so no test hits api.x.ai.
"""
import json
import os
import stat

import pytest

from Orchestrator.xai_phone import provisioning as pv


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "xai_phone.json"
    monkeypatch.setattr(pv, "STORE_PATH", str(path))
    return path


@pytest.fixture
def fake_api(monkeypatch):
    calls = []

    async def _fake_post(path, payload):
        calls.append((path, payload))
        return {
            "id": "pn-1",
            "phone_number": "+15550100",
            "signing_secret": "whsec_c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0ISE=",
        }

    monkeypatch.setattr(pv, "_api_post", _fake_post)
    return calls


def test_status_unprovisioned(store):
    s = pv.get_status()
    assert s["provisioned"] is False
    assert s["phone_number"] is None


@pytest.mark.asyncio
async def test_provision_persists_number_and_secret(store, fake_api):
    status = await pv.provision_number("BlackBox line", "https://box.ts.net:10000/xai/voice/incoming")
    assert status["provisioned"] is True
    assert status["phone_number"] == "+15550100"
    assert status["has_signing_secret"] is True
    assert "signing_secret" not in status            # status NEVER leaks the secret
    assert "raw_response" not in status
    on_disk = json.loads(store.read_text())
    assert on_disk["phone_number"] == "+15550100"
    assert on_disk["signing_secret"].startswith("whsec_")
    assert on_disk["raw_response"]["id"] == "pn-1"   # secret returned ONCE: keep everything
    assert fake_api[0][0] == "/v2/phone-numbers"
    assert fake_api[0][1] == {
        "origin": pv.ORIGIN_PROVISIONED,
        "name": "BlackBox line",
        "webhook": "https://box.ts.net:10000/xai/voice/incoming",
    }


@pytest.mark.asyncio
async def test_store_file_is_0600(store, fake_api):
    await pv.provision_number("line", "https://x/hook")
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600


@pytest.mark.asyncio
async def test_provision_idempotent_refuses_second_call(store, fake_api):
    await pv.provision_number("line", "https://x/hook")
    with pytest.raises(pv.AlreadyProvisionedError):
        await pv.provision_number("line", "https://x/hook")
    assert len(fake_api) == 1                        # API NOT called again


@pytest.mark.asyncio
async def test_provision_force_reprovisions_and_keeps_preset(store, fake_api):
    await pv.provision_number("line", "https://x/hook")
    pv.set_default_preset_id("preset-abc")
    status = await pv.provision_number("line2", "https://y/hook", force=True)
    assert len(fake_api) == 2
    assert status["default_preset_id"] == "preset-abc"


@pytest.mark.asyncio
async def test_secret_extraction_fallback_field_names(store, monkeypatch):
    async def _fake_post(path, payload):
        return {"phone_number": "+1555", "webhook": {"secret": "nested-secret"}}
    monkeypatch.setattr(pv, "_api_post", _fake_post)
    await pv.provision_number("line", "https://x/hook")
    assert pv.get_signing_secret() == "nested-secret"


def test_corrupt_store_quarantined(store):
    store.write_text("{not json")
    assert pv.read_store() == {}
    assert not store.exists()                        # renamed to *.corrupt-<ts>
    assert any(p.name.startswith("xai_phone.json.corrupt-") for p in store.parent.iterdir())


def test_default_preset_roundtrip(store):
    assert pv.get_default_preset_id() is None
    pv.set_default_preset_id("preset-1")
    assert pv.get_default_preset_id() == "preset-1"
