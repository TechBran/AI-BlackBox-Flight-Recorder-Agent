import json
from Orchestrator.asterisk import gateway_manager as gm, secrets


def _sample():
    return {"id":"g1","name":"n","model":"TG200","ip":"1.1.1.1","enabled":True,
            "sip_port":5060,"http_port":80,"codec":"g722","trunk_name":"t","created_at":"x",
            "http":{"user":"admin","password":"pw123"},
            "ami":{"port":5038,"user":"blackbox","secret":"sek123"},
            "ports":[]}


def test_save_encrypts_on_disk_without_mutating_input(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "GATEWAYS_FILE", str(tmp_path/"gw.json"))
    gws = [_sample()]
    gm.save_gateways(gws)
    # input not mutated
    assert gws[0]["http"]["password"] == "pw123"
    assert gws[0]["ami"]["secret"] == "sek123"
    # on disk encrypted
    on_disk = json.loads((tmp_path/"gw.json").read_text())[0]
    assert on_disk["http"]["password"].startswith("enc:")
    assert on_disk["ami"]["secret"].startswith("enc:")


def test_get_gateway_decrypted_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "GATEWAYS_FILE", str(tmp_path/"gw.json"))
    gm.save_gateways([_sample()])
    dec = gm.get_gateway_decrypted("g1")
    assert dec["http"]["password"] == "pw123"
    assert dec["ami"]["secret"] == "sek123"
    assert gm.get_gateway_decrypted("nope") is None


def test_redact_hides_secrets():
    r = gm.redact_gateway(_sample())
    assert "password" not in r["http"] and r["http"]["has_password"] is True
    assert "secret" not in r["ami"] and r["ami"]["has_secret"] is True


def test_redact_empty_secret_false():
    g = _sample(); g["http"]["password"] = ""; g["ami"]["secret"] = ""
    r = gm.redact_gateway(g)
    assert r["http"]["has_password"] is False and r["ami"]["has_secret"] is False


def test_save_is_idempotent_on_encrypted(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "GATEWAYS_FILE", str(tmp_path/"gw.json"))
    gm.save_gateways([_sample()])
    first = (tmp_path/"gw.json").read_text()
    reloaded = gm.load_gateways()            # encrypted on disk
    gm.save_gateways(reloaded)               # must not double-encrypt
    dec = gm.get_gateway_decrypted("g1")
    assert dec["http"]["password"] == "pw123"  # still decrypts to original
