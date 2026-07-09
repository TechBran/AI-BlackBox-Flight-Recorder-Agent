# Orchestrator/tests/test_custom_servers.py
import json, os, stat
import pytest
from Orchestrator.onboarding import custom_servers as cs


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "custom_models.json"
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(path))
    return path


def test_list_servers_absent_file_returns_empty(registry):
    assert cs.list_servers() == []


def test_list_servers_corrupt_file_returns_empty(registry):
    registry.write_text("{not json")
    assert cs.list_servers() == []  # fail-soft, never raises


def test_add_server_persists_and_generates_id(registry):
    srv = cs.add_server(alias="gemma-box", base_url="http://192.168.1.50:8080/v1",
                        api_key="sk-test", context_tokens=32768)
    assert srv["id"].startswith("srv-")
    on_disk = json.loads(registry.read_text())
    assert on_disk["version"] == 1
    assert on_disk["servers"][0]["alias"] == "gemma-box"
    assert on_disk["servers"][0]["enabled"] is True


def test_registry_file_is_0600(registry):
    cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    mode = stat.S_IMODE(os.stat(registry).st_mode)
    assert mode == 0o600


def test_base_url_normalized_no_trailing_slash(registry):
    srv = cs.add_server(alias="a", base_url="http://x:8080/v1/", api_key="k")
    assert srv["base_url"] == "http://x:8080/v1"


def test_alias_must_be_unique_and_separator_free(registry):
    cs.add_server(alias="box", base_url="http://x/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.add_server(alias="box", base_url="http://y/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.add_server(alias="bad::alias", base_url="http://z/v1", api_key="k")


def test_update_and_delete_server(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    cs.update_server(srv["id"], {"alias": "b", "last_models": ["m1"]})
    assert cs.get_server(srv["id"])["alias"] == "b"
    cs.delete_server(srv["id"])
    assert cs.list_servers() == []


def test_resolve_model_qualified_and_fallback(registry):
    s1 = cs.add_server(alias="one", base_url="http://x/v1", api_key="k")
    s2 = cs.add_server(alias="two", base_url="http://y/v1", api_key="k")
    cs.update_server(s2["id"], {"last_models": ["gemma-26b"]})
    srv, bare = cs.resolve_model("two::gemma-26b")
    assert srv["id"] == s2["id"] and bare == "gemma-26b"
    # unqualified: server that listed it wins
    srv, bare = cs.resolve_model("gemma-26b")
    assert srv["id"] == s2["id"]
    # unknown unqualified: first enabled server
    srv, bare = cs.resolve_model("mystery-model")
    assert srv["id"] == s1["id"] and bare == "mystery-model"


def test_resolve_model_no_servers_returns_none(registry):
    assert cs.resolve_model("anything") == (None, "anything")


def test_redacted_listing_masks_keys(registry):
    cs.add_server(alias="a", base_url="http://x/v1", api_key="sk-secret-1234")
    red = cs.list_servers_redacted()[0]
    assert "api_key" not in red
    assert red["key_last4"] == "1234"
    assert red["key_present"] is True


def test_redact_single_record():
    """redact() is the single source of truth for the API-safe shape —
    routes call it directly on created/patched records."""
    original = {"id": "srv-1", "alias": "a", "api_key": "sk-secret-1234"}
    red = cs.redact(original)
    assert "api_key" not in red
    assert red["key_present"] is True
    assert red["key_last4"] == "1234"
    assert original["api_key"] == "sk-secret-1234"  # input not mutated

    keyless = cs.redact({"id": "srv-2", "alias": "b", "api_key": ""})
    assert keyless["key_present"] is False and keyless["key_last4"] == ""
    no_field = cs.redact({"id": "srv-3", "alias": "c"})  # api_key absent entirely
    assert no_field["key_present"] is False and no_field["key_last4"] == ""


def test_update_server_unknown_field_raises(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"id": "srv-hijack"})
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"bogus_field": 1})


def test_update_and_delete_unknown_id_raise_keyerror(registry):
    with pytest.raises(KeyError):
        cs.update_server("srv-missing", {"alias": "z"})
    with pytest.raises(KeyError):
        cs.delete_server("srv-missing")


def test_list_servers_enabled_only_filters(registry):
    s1 = cs.add_server(alias="on", base_url="http://x/v1", api_key="k")
    s2 = cs.add_server(alias="off", base_url="http://y/v1", api_key="k")
    cs.update_server(s2["id"], {"enabled": False})
    assert [s["id"] for s in cs.list_servers(enabled_only=True)] == [s1["id"]]
    assert len(cs.list_servers()) == 2


def test_resolve_model_excludes_disabled_servers(registry):
    s1 = cs.add_server(alias="one", base_url="http://x/v1", api_key="k")
    s2 = cs.add_server(alias="two", base_url="http://y/v1", api_key="k")
    cs.update_server(s2["id"], {"last_models": ["gemma-26b"], "enabled": False})
    # qualified alias of a disabled server: fail fast, never reroute elsewhere
    assert cs.resolve_model("two::gemma-26b") == (None, "two::gemma-26b")
    # unqualified: disabled server's last_models must not match -> first enabled
    srv, bare = cs.resolve_model("gemma-26b")
    assert srv["id"] == s1["id"] and bare == "gemma-26b"
    # a prefix that can't be an alias is part of the model id, not a routing key
    srv, bare = cs.resolve_model("org/name::tag")
    assert srv["id"] == s1["id"] and bare == "org/name::tag"


def test_mutation_rejects_wrong_types(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"last_models": "gemma-26b"})  # string, not list
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"last_models": ["m1", 2]})  # non-str element
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"enabled": "no"})  # truthy string
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"context_tokens": "lots"})
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"validated_at": 12345})
    with pytest.raises(ValueError):
        cs.add_server(alias="b", base_url="http://y/v1", api_key="k", context_tokens=0)
    with pytest.raises(ValueError):
        cs.add_server(alias="c", base_url="http://z/v1", api_key=None)
    # nothing above should have persisted
    assert cs.get_server(srv["id"])["last_models"] == []
    assert len(cs.list_servers()) == 1


def test_alias_stripped_before_validation(registry):
    cs.add_server(alias="box", base_url="http://x/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.add_server(alias="box ", base_url="http://y/v1", api_key="k")
    srv = cs.add_server(alias="  edge  ", base_url="http://z/v1", api_key="k")
    assert srv["alias"] == "edge"


def test_corrupt_registry_quarantined_not_destroyed(registry):
    registry.write_text("{not json")
    assert cs.list_servers() == []
    quarantined = list(registry.parent.glob("custom_models.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{not json"
    assert not registry.exists()
    # subsequent writes start a fresh registry without touching the quarantine
    cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    assert len(cs.list_servers()) == 1
    assert quarantined[0].exists()


def test_wrong_shape_json_fail_soft(registry):
    registry.write_text(json.dumps([1, 2, 3]))
    assert cs.list_servers() == []


# ── per-model context map (model_context) ─────────────────────────────────
# A llama-swap box hosts models with DIFFERENT real windows behind ONE
# server record; model_context maps bare model id -> real context tokens
# (auto-learned from llama.cpp exceed_context_size_error 400s).


def test_add_server_defaults_model_context_empty(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    assert srv["model_context"] == {}
    on_disk = json.loads(registry.read_text())
    assert on_disk["servers"][0]["model_context"] == {}


def test_model_context_patchable_and_persisted(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    cs.update_server(srv["id"], {"model_context": {"gemma-31b": 16384, "gemma-26b": 32768}})
    assert cs.get_server(srv["id"])["model_context"] == {"gemma-31b": 16384, "gemma-26b": 32768}


def test_model_context_rejects_bad_shapes(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    cs.update_server(srv["id"], {"model_context": {"good": 16384}})
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": {"m": "16384"}})  # str value
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": {"m": True}})  # bool (int subclass)
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": {1: 16384}})  # int key
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": {"m": 0}})  # non-positive
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": {"m": -5}})
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": [("m", 16384)]})  # not a dict
    with pytest.raises(ValueError):
        cs.update_server(srv["id"], {"model_context": None})
    # nothing above persisted — the last good value survives every rejection
    assert cs.get_server(srv["id"])["model_context"] == {"good": 16384}


def test_model_context_kept_by_redact(registry):
    """model_context is NOT a secret — redact() keeps it for the wizard UI."""
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="sk-secret-1234")
    cs.update_server(srv["id"], {"model_context": {"m": 16384}})
    red = cs.list_servers_redacted()[0]
    assert "api_key" not in red
    assert red["model_context"] == {"m": 16384}


def test_window_guard_tokens_per_model_override():
    """A model present in model_context feeds ITS window into the guard
    formula; absent models (and model=None) use the server-wide value."""
    server = {"context_tokens": 131072, "model_context": {"gemma-31b": 16384}}
    assert cs.window_guard_tokens(server, "gemma-31b") == 9830   # int(0.6 x 16,384)
    assert cs.window_guard_tokens(server, "gemma-12b") == 78643  # int(0.6 x 131,072)
    assert cs.window_guard_tokens(server, None) == 78643
    assert cs.window_guard_tokens(server) == 78643               # backward compatible


def test_window_guard_tokens_per_model_legacy_and_junk_safe():
    """Records predating the field, non-dict junk, and junk values inside the
    map all fall back to context_tokens — never crash, never a junk budget."""
    assert cs.window_guard_tokens({"context_tokens": 32768}, "m") == 19660
    assert cs.window_guard_tokens({"context_tokens": 32768, "model_context": None}, "m") == 19660
    assert cs.window_guard_tokens({"context_tokens": 32768, "model_context": "x"}, "m") == 19660
    # hand-edited junk value inside the map: ignored, server value wins
    assert cs.window_guard_tokens({"context_tokens": 32768, "model_context": {"m": "big"}}, "m") == 19660
    assert cs.window_guard_tokens({"context_tokens": 32768, "model_context": {"m": True}}, "m") == 19660
    # the 4,000 floor still binds on a learned tiny window
    assert cs.window_guard_tokens({"context_tokens": 131072, "model_context": {"m": 2048}}, "m") == 4000
    assert cs.window_guard_tokens(None, "m") == 19660
