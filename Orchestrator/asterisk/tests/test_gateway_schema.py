from Orchestrator.asterisk import gateway_manager as gm


def test_new_gateway_v2_shape():
    gw = gm._new_gateway(name="Office", ip="10.0.0.5", model="TG400")
    assert gw["model"] == "TG400"
    assert gw["http"]["user"] and "password" in gw["http"]
    assert "ami" in gw and gw["ami"]["port"] == 5038
    assert len(gw["ports"]) == 4
    assert {p["span"] for p in gw["ports"]} == {2, 3, 4, 5}


def test_new_gateway_no_hardcoded_secret():
    gw = gm._new_gateway(name="x", ip="1.1.1.1")  # no AMI env set in tests
    assert gw["ami"]["secret"] == ""  # never a literal


def test_migrate_legacy_record():
    legacy = {"id": "x", "name": "old", "ip": "1.2.3.4", "http_user": "admin",
              "http_password": "password", "capacity": 2, "phone_numbers": ["+15551112222"],
              "trunk_name": "tg200", "enabled": True}
    new = gm.migrate_gateway(legacy)
    assert new["model"] == "TG200"
    assert new["http"]["user"] == "admin"
    assert new["ami"]["port"] == 5038
    assert len(new["ports"]) == 2
    assert new["ports"][0]["phone_number"] == "+15551112222"


def test_migrate_is_idempotent():
    legacy = {"id": "x", "name": "old", "ip": "1.2.3.4", "http_user": "admin",
              "http_password": "p", "capacity": 2, "phone_numbers": [], "trunk_name": "t", "enabled": True}
    once = gm.migrate_gateway(legacy)
    twice = gm.migrate_gateway(once)
    assert twice == once


def test_span_helpers():
    assert gm.spans_for_model("TG800") == [2, 3, 4, 5, 6, 7, 8, 9]
    assert gm.spans_for_model("TG100") == [2]
    assert gm.slot_to_span(0) == 2 and gm.slot_to_span(3) == 5
    assert gm.port_count("TG400") == 4


def test_new_gateway_has_http_port():
    gw = gm._new_gateway(name="x", ip="1.1.1.1", http_port=8080)
    assert gw["http_port"] == 8080
    gw2 = gm._new_gateway(name="y", ip="1.1.1.2")
    assert gw2["http_port"] == 80


def test_migrate_preserves_http_port():
    legacy = {"id":"x","name":"old","ip":"1.2.3.4","http_user":"admin","http_password":"p",
              "http_port":8443,"capacity":2,"phone_numbers":[],"trunk_name":"t","enabled":True}
    new = gm.migrate_gateway(legacy)
    assert new["http_port"] == 8443


def test_migrate_idempotent_keeps_http_port():
    legacy = {"id":"x","name":"old","ip":"1.2.3.4","http_user":"admin","http_password":"p",
              "http_port":8443,"capacity":2,"phone_numbers":[],"trunk_name":"t","enabled":True}
    once = gm.migrate_gateway(legacy)
    twice = gm.migrate_gateway(once)
    assert twice == once and twice["http_port"] == 8443
