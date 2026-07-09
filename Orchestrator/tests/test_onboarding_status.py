"""Backend /onboarding/status rollup + SSE live re-validation (M1).

The rollup canonicalizes the provider->step join + attention-derivation rules
server-side so the hub frontend stays presentational. SECTIONS is the 10-section
catalog (welcome/done are the hub itself, NOT sections); it must never drift from
state.ALL_STEPS. Hermeticity mirrors test_onboarding_web_search.py's tmp_env.
"""
from Orchestrator.onboarding import status_rollup as sr
from Orchestrator.onboarding.state import ALL_STEPS


# Steps that are the hub itself, not status sections (design: welcome->hub
# basis, done->summary model).
_NON_SECTION_STEPS = {"welcome", "done"}


def test_sections_keys_are_all_steps_minus_welcome_and_done():
    section_keys = [s["key"] for s in sr.SECTIONS]
    expected = [s for s in ALL_STEPS if s not in _NON_SECTION_STEPS]
    assert section_keys == expected, (
        "status_rollup.SECTIONS drifted from state.ALL_STEPS:\n"
        f"  sections: {section_keys}\n"
        f"  expected: {expected}"
    )


def test_every_section_step_equals_its_key():
    # Contract: step == key for every section (the hub links ?step=<key>).
    for s in sr.SECTIONS:
        assert s["step"] == s["key"], f"{s['key']}: step != key"


def test_sections_have_required_shape():
    valid_groups = {"network", "keys", "capabilities", "identity"}
    for s in sr.SECTIONS:
        assert set(s) >= {"key", "group", "label", "required", "step"}
        assert s["group"] in valid_groups
        assert isinstance(s["required"], bool)


def _empty_inputs():
    """Minimal snapshot inputs for build_status (all unconfigured)."""
    return dict(
        env={},
        state={"completed_steps": [], "skipped_steps": [], "validated_at": {}},
        embeddings={"active": None, "health": {"state": "ok"}, "stores": [], "models": []},
        cli={"providers": {}, "ready": False},
        web_search={"enabled": [], "providers": {}, "default": ""},
        image={"enabled": [], "providers": {}, "default": ""},
        paired=[],
        operators=[],
        restart={"needs_restart": False, "drifted_keys": []},
        custom_servers=[],
    )


def _custom_server(**over):
    """A full registry record as custom_servers.list_servers() returns it —
    including api_key, which the rollup must NEVER emit."""
    srv = {
        "id": "srv-abc",
        "alias": "gemma-box",
        "base_url": "http://gemma.local:8000/v1",
        "api_key": "sk-SECRET-NEVER-EMIT",
        "context_tokens": 32768,
        "enabled": True,
        "added_at": "2026-07-08T00:00:00+00:00",
        "validated_at": "2026-07-08T01:00:00+00:00",
        "last_models": ["gemma-3-27b"],
    }
    srv.update(over)
    return srv


def _section(rollup, key):
    return next(s for s in rollup["sections"] if s["key"] == key)


def test_api_keys_attention_when_no_keys_present():
    rollup = sr.build_status(**_empty_inputs())
    sec = _section(rollup, "api_keys")
    # required + unsatisfied -> attention (NOT optional)
    assert sec["state"] == sr.ATTENTION
    assert sec["required"] is True


def test_api_keys_ready_when_a_key_present_and_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "api_keys")["state"] == sr.READY


def test_api_keys_attention_when_present_but_never_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}  # present, validated_at empty
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert any(a["section"] == "api_keys" and a["severity"] == "warn"
               for a in rollup["attention"])


def test_api_keys_reranker_providers_are_tracked_items(monkeypatch):
    """M10: Voyage/Cohere reranker keys live in the API-Keys step, so the rollup
    tracks them as items with a validated_at stamp (present-but-unvalidated
    nudges the same as any other key)."""
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx", "VOYAGE_API_KEY": "pa-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}  # voyage present, unvalidated
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    item_keys = {i["key"] for i in sec["items"]}
    assert {"voyage", "cohere"} <= item_keys
    # A present-but-unvalidated reranker key flags attention (validate nudge).
    assert sec["state"] == sr.ATTENTION
    assert any("voyage" in a["message"] for a in rollup["attention"]
               if a["section"] == "api_keys")


# ── Custom model servers in the api_keys rollup (custom-model-providers) ──

def test_api_keys_validated_enabled_custom_server_satisfies_llm_key():
    """A validated+enabled custom server with ZERO env keys is a valid
    production configuration — no 'No API keys' attention, section READY,
    server surfaced in the summary (the hub tile renders only state+summary)."""
    inp = _empty_inputs()
    inp["custom_servers"] = [_custom_server()]
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.READY
    assert not any(a["section"] == "api_keys" for a in rollup["attention"])
    # EQUALITY: pins no stray " · " prefix on a server-only box (Task 2.1's
    # Chrome contract asserts this tile text).
    assert sec["summary"] == "1 custom server"
    item = next(i for i in sec["items"] if i["key"] == "custom:srv-abc")
    assert item == {
        "key": "custom:srv-abc",
        "label": "Custom: gemma-box",
        "configured": True,
        "validated_at": "2026-07-08T01:00:00+00:00",
    }
    # The api_key must never leak into ANY emitted item.
    assert "sk-SECRET" not in repr(sec["items"])


def test_api_keys_unvalidated_custom_server_nudges_like_unvalidated_key():
    """Enabled-but-unvalidated custom server = present-but-unvalidated key
    semantics: escapes the 'No API keys' state but flags a validate nudge."""
    inp = _empty_inputs()
    inp["custom_servers"] = [_custom_server(validated_at=None)]
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert sec["summary"] != "No API keys configured"
    assert any(a["section"] == "api_keys" and a["severity"] == "warn"
               and "gemma-box" in a["message"] for a in rollup["attention"])


def test_api_keys_disabled_custom_server_does_not_satisfy_llm_key():
    """Disabled servers don't count toward the LLM-key requirement (still the
    'No API keys' attention) but remain visible as unconfigured items."""
    inp = _empty_inputs()
    inp["custom_servers"] = [_custom_server(enabled=False)]
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert sec["summary"] == "No API keys configured"
    assert any(a["section"] == "api_keys" and "No LLM API key" in a["message"]
               for a in rollup["attention"])
    item = next(i for i in sec["items"] if i["key"] == "custom:srv-abc")
    assert item["configured"] is False


def test_api_keys_plural_unvalidated_servers_summary():
    """Two enabled-but-unvalidated servers, no env keys → plural segment with
    the unvalidated count (equality pins the exact tile text)."""
    inp = _empty_inputs()
    inp["custom_servers"] = [
        _custom_server(validated_at=None),
        _custom_server(id="srv-def", alias="ollama-box", validated_at=None),
    ]
    sec = _section(sr.build_status(**inp), "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert sec["summary"] == "2 custom servers; 2 unvalidated"


def test_api_keys_validated_key_plus_unvalidated_server_summary_is_precise():
    """Env keys all validated + server unvalidated → the keys text must not
    claim '0 unvalidated'; the unvalidated count belongs to the server segment."""
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}
    inp["custom_servers"] = [_custom_server(validated_at=None)]
    sec = _section(sr.build_status(**inp), "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert sec["summary"] == "1 key(s) validated · 1 custom server; 1 unvalidated"


def test_api_keys_summary_composes_keys_and_servers_plural():
    """Summary composes the existing keys text with the server count,
    singular/plural correct."""
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx", "ANTHROPIC_API_KEY": "sk-ant"}
    inp["state"]["validated_at"] = {"openai": 1.0, "anthropic": 1.0}
    inp["custom_servers"] = [
        _custom_server(),
        _custom_server(id="srv-def", alias="ollama-box"),
    ]
    sec = _section(sr.build_status(**inp), "api_keys")
    assert sec["state"] == sr.READY
    assert "2 key(s) validated" in sec["summary"]
    assert "2 custom servers" in sec["summary"]


def test_operator_required_attention_when_none():
    rollup = sr.build_status(**_empty_inputs())
    assert _section(rollup, "operator")["state"] == sr.ATTENTION


def test_operator_ready_when_present():
    inp = _empty_inputs()
    inp["operators"] = ["Brandon"]
    rollup = sr.build_status(**inp)
    assert _section(rollup, "operator")["state"] == sr.READY


def test_rollup_top_level_shape():
    rollup = sr.build_status(**_empty_inputs())
    assert set(rollup) >= {"ready_count", "total", "is_complete", "sections", "attention"}
    assert rollup["total"] == len(sr.SECTIONS)
    assert isinstance(rollup["ready_count"], int)
    for sec in rollup["sections"]:
        assert set(sec) >= {"key", "group", "label", "state", "required",
                            "summary", "step", "skipped", "items"}


def test_embeddings_attention_when_no_active_model():
    inp = _empty_inputs()
    inp["embeddings"] = {"active": None, "health": {"state": "ok"},
                         "stores": [], "models": []}
    assert _section(sr.build_status(**inp), "embeddings")["state"] == sr.ATTENTION


def test_embeddings_ready_when_active_and_healthy_and_caught_up():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "ok", "successor": None},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}],
        "models": [],
    }
    assert _section(sr.build_status(**inp), "embeddings")["state"] == sr.READY


def test_embeddings_attention_when_index_behind():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b", "health": {"state": "ok"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 42}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "embeddings")["state"] == sr.ATTENTION
    assert any("behind" in a["message"].lower() for a in rollup["attention"]
               if a["section"] == "embeddings")


def test_embeddings_attention_when_health_superseded():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "superseded", "successor": "gemini-embedding-2"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "embeddings")["state"] == sr.ATTENTION


def test_embeddings_attention_when_health_broken_is_error_severity():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b", "health": {"state": "broken", "detail": "x"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert any(a["section"] == "embeddings" and a["severity"] == "error"
               for a in rollup["attention"])


def test_web_search_optional_when_nothing_enabled():
    inp = _empty_inputs()
    inp["web_search"] = {"enabled": [], "providers": {}, "default": ""}
    assert _section(sr.build_status(**inp), "web_search")["state"] == sr.OPTIONAL


def test_web_search_ready_when_enabled_with_keys():
    inp = _empty_inputs()
    inp["web_search"] = {
        "enabled": ["duckduckgo", "openai"],
        "providers": {"duckduckgo": {"key_present": True, "enabled": True},
                      "openai": {"key_present": True, "enabled": True}},
        "default": "openai",
    }
    assert _section(sr.build_status(**inp), "web_search")["state"] == sr.READY


def test_web_search_attention_when_enabled_provider_key_missing():
    inp = _empty_inputs()
    inp["web_search"] = {
        "enabled": ["openai"],
        "providers": {"openai": {"key_present": False, "enabled": True}},
        "default": "openai",
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "web_search")["state"] == sr.ATTENTION
    assert any(a["section"] == "web_search" and "key" in a["message"].lower()
               for a in rollup["attention"])


def test_image_attention_when_enabled_provider_key_missing():
    inp = _empty_inputs()
    inp["image"] = {
        "enabled": ["gemini"],
        "providers": {"gemini": {"key_present": False, "enabled": True}},
        "default": "gemini",
    }
    assert _section(sr.build_status(**inp), "image")["state"] == sr.ATTENTION


def test_cli_agents_attention_when_installed_not_authed():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": True, "authenticated": False}}, "ready": False}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "cli_agents")["state"] == sr.ATTENTION
    assert any("auth" in a["message"].lower() for a in rollup["attention"]
               if a["section"] == "cli_agents")


def test_cli_agents_ready_when_all_ready():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": True, "authenticated": True}}, "ready": True}
    assert _section(sr.build_status(**inp), "cli_agents")["state"] == sr.READY


def test_cli_agents_optional_when_none_installed():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": False, "authenticated": False}}, "ready": False}
    assert _section(sr.build_status(**inp), "cli_agents")["state"] == sr.OPTIONAL


def test_pair_phone_ready_when_devices_paired():
    inp = _empty_inputs()
    inp["paired"] = [{"name": "Pixel"}]
    assert _section(sr.build_status(**inp), "pair_phone")["state"] == sr.READY


def test_pair_phone_optional_when_none():
    assert _section(sr.build_status(**_empty_inputs()), "pair_phone")["state"] == sr.OPTIONAL


def test_global_restart_drift_emits_attention_row():
    inp = _empty_inputs()
    inp["restart"] = {"needs_restart": True, "drifted_keys": ["OPENAI_API_KEY"]}
    rollup = sr.build_status(**inp)
    assert any(a.get("section") is None and "restart" in a["message"].lower()
               for a in rollup["attention"])


def test_tailscale_optional_when_never_validated():
    assert _section(sr.build_status(**_empty_inputs()), "tailscale")["state"] == sr.OPTIONAL


def test_tailscale_attention_when_validated_but_serve_not_set():
    inp = _empty_inputs()
    inp["state"]["validated_at"] = {"tailscale": 1.0}
    # no serve hint in env -> serve-not-set attention
    rollup = sr.build_status(**inp)
    assert _section(rollup, "tailscale")["state"] == sr.ATTENTION
    assert any("pair" in a["message"].lower() or "serve" in a["message"].lower()
               for a in rollup["attention"] if a["section"] == "tailscale")


def test_tailscale_ready_when_validated_and_serve_hint_present():
    inp = _empty_inputs()
    inp["state"]["validated_at"] = {"tailscale": 1.0}
    inp["env"] = {"BLACKBOX_TAILNET_HOSTNAME": "box.tail1234.ts.net"}
    assert _section(sr.build_status(**inp), "tailscale")["state"] == sr.READY


def test_default_section_ready_when_completed_and_optional_when_skipped():
    inp = _empty_inputs()
    inp["state"]["completed_steps"] = ["optional_integrations"]
    inp["state"]["skipped_steps"] = ["transcription"]
    r = sr.build_status(**inp)
    assert _section(r, "optional_integrations")["state"] == sr.READY
    assert _section(r, "transcription")["state"] == sr.OPTIONAL
    assert _section(r, "transcription")["skipped"] is True


def test_ready_count_is_exact():
    """ready_count is the exact number of READY sections. Construct a scenario
    with exactly 3 READY (api_keys, operator, embeddings) and everything else
    optional/attention, then assert the count is unambiguously 3."""
    inp = _empty_inputs()
    # api_keys -> READY: one key present AND validated
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}
    # operator -> READY: one operator present
    inp["operators"] = ["Brandon"]
    # embeddings -> READY: active, healthy, caught up
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "ok", "successor": None},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}],
        "models": [],
    }
    # everything else left empty -> optional (tailscale/feature/pair_phone/cli/defaults)
    r = sr.build_status(**inp)
    ready = [s["key"] for s in r["sections"] if s["state"] == sr.READY]
    assert sorted(ready) == ["api_keys", "embeddings", "operator"]
    assert r["ready_count"] == 3


# ── Route tests (Task 13): GET /onboarding/status — fast read, zero probes ──
from unittest.mock import patch
import pytest


def _client():
    import Orchestrator.app  # noqa: F401 — registers onboarding routes
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


def test_status_route_returns_contract_shape():
    c = _client()
    r = c.get("/onboarding/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ready_count", "total", "is_complete", "sections", "attention"}
    assert body["total"] == len(sr.SECTIONS)
    assert {s["key"] for s in body["sections"]} == {s["key"] for s in sr.SECTIONS}


def test_status_route_does_no_tailscale_probe():
    """The FAST read must never shell out — patch the probe to raise; route
    must still 200 (proving it was never called)."""
    c = _client()
    with patch("Orchestrator.onboarding.validators.validate_tailscale",
               side_effect=AssertionError("FAST read must not probe tailscale")):
        r = c.get("/onboarding/status")
    assert r.status_code == 200


# ── M13: rerank block in the rollup (additive; fail-soft) ──


def test_rollup_carries_rerank_block_verbatim_and_defaults_to_none():
    """build_status passes the collected GET /rerank/status payload through
    as a top-level additive key; omitting it (older caller) yields None."""
    block = {"enabled": False, "gpu": True, "service_reachable": True,
             "configured": False, "available": False,
             "preflight": {"state": "skipped"}}
    inp = _empty_inputs()
    assert sr.build_status(**inp, rerank=block)["rerank"] == block
    assert sr.build_status(**inp)["rerank"] is None


def test_status_route_includes_rerank_block():
    """GET /onboarding/status carries the rerank status additively."""
    c = _client()
    block = {"enabled": True, "gpu": True, "service_reachable": True,
             "configured": True, "available": True,
             "preflight": {"state": "ok", "latency_ms": 42.0}}
    with patch("Orchestrator.rerank.status", return_value=block):
        r = c.get("/onboarding/status")
    assert r.status_code == 200
    assert r.json()["rerank"] == block


def test_status_route_fail_soft_when_rerank_status_raises():
    """A hanging/broken probe must never take the rollup down — rerank
    degrades to None, everything else intact (mirrors the embeddings
    fail-soft)."""
    c = _client()
    with patch("Orchestrator.rerank.status",
               side_effect=TimeoutError("probe hung")):
        r = c.get("/onboarding/status")
    assert r.status_code == 200
    body = r.json()
    assert body["rerank"] is None
    assert {s["key"] for s in body["sections"]} == {s["key"] for s in sr.SECTIONS}


# ── Custom servers flow through the route layer (fail-soft registry read) ──


def test_status_route_surfaces_custom_servers_and_never_leaks_api_key():
    """_collect_status_inputs reads the registry and the rollup surfaces the
    server count in the api_keys summary; the api_key never reaches the wire."""
    c = _client()
    with patch("Orchestrator.onboarding.custom_servers.list_servers",
               return_value=[_custom_server()]):
        r = c.get("/onboarding/status")
    assert r.status_code == 200
    sec = next(s for s in r.json()["sections"] if s["key"] == "api_keys")
    assert "1 custom server" in sec["summary"]
    assert "sk-SECRET" not in r.text


def test_status_route_fail_soft_when_custom_registry_read_raises():
    """A broken registry read must never take /onboarding/status down —
    custom servers degrade to [], everything else intact."""
    c = _client()
    with patch("Orchestrator.onboarding.custom_servers.list_servers",
               side_effect=OSError("registry unreadable")):
        r = c.get("/onboarding/status")
    assert r.status_code == 200
    body = r.json()
    assert {s["key"] for s in body["sections"]} == {s["key"] for s in sr.SECTIONS}


# ── SSE tests (Task 16): GET /onboarding/status/stream — live re-validation ──
def _parse_sse(text):
    """Minimal SSE parser -> list of (event, data_str)."""
    events, ev, data = [], None, []
    for line in text.splitlines():
        if line.startswith("event:"):
            ev = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())
        elif line == "":
            if ev or data:
                events.append((ev, "\n".join(data)))
            ev, data = None, []
    if ev or data:
        events.append((ev, "\n".join(data)))
    return events


def test_status_stream_emits_section_events_then_done():
    import json as _json
    c = _client()
    # Keep the live tailscale probe deterministic + cheap.
    with patch("Orchestrator.onboarding.validators.validate_tailscale") as m:
        from Orchestrator.onboarding.validators import ValidationResult
        m.return_value = ValidationResult(ok=False, latency_ms=1, error="not running")
        r = c.get("/onboarding/status/stream")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = _parse_sse(r.text)
    kinds = [e[0] for e in events]
    assert "section" in kinds
    assert kinds[-1] == "done"
    # every section event carries the contract keys
    for ev, data in events:
        if ev == "section":
            payload = _json.loads(data)
            assert set(payload) >= {"key", "state", "summary", "attention"}
    done_payload = _json.loads(events[-1][1])
    assert set(done_payload) >= {"ready_count", "total"}
    assert done_payload["total"] == len(sr.SECTIONS)


# ── Validate-stored-key (troubleshooting re-validate without re-entering) ──

def test_validate_uses_stored_env_key_when_no_credentials(tmp_path, monkeypatch):
    """POST /onboarding/validate {provider} with NO credentials validates the
    STORED .env key (the troubleshooting re-validate affordance) — the client
    never re-handles the secret. On success validated_at is stamped. Without
    the stored-key resolution this 400s on a missing credential field."""
    import Orchestrator.onboarding.secrets_writer as sw
    from Orchestrator.onboarding import validators
    from Orchestrator.onboarding.validators import ValidationResult
    from Orchestrator.routes import onboarding_routes

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-stored-abc123\n")
    monkeypatch.setattr(sw, "ENV_FILE", env_file)

    captured = {}
    def fake_validate_openai(api_key):
        captured["key"] = api_key
        return ValidationResult(ok=True, latency_ms=5, detail={"model_count": 1})
    monkeypatch.setattr(validators, "validate_openai", fake_validate_openai)

    recorded = []
    monkeypatch.setattr(onboarding_routes._state, "record_validation",
                        lambda prov: recorded.append(prov))

    c = _client()
    r = c.post("/onboarding/validate", json={"provider": "openai"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert captured["key"] == "sk-stored-abc123"   # read from stored .env, not the request
    assert recorded == ["openai"]                  # validated_at stamped on success


def test_validate_still_prefers_request_credentials_when_supplied(tmp_path, monkeypatch):
    """If the client DOES supply api_key, it wins over the stored value."""
    import Orchestrator.onboarding.secrets_writer as sw
    from Orchestrator.onboarding import validators
    from Orchestrator.onboarding.validators import ValidationResult
    from Orchestrator.routes import onboarding_routes

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-stored-OLD\n")
    monkeypatch.setattr(sw, "ENV_FILE", env_file)
    captured = {}
    def fake(api_key):
        captured["key"] = api_key
        return ValidationResult(ok=True, latency_ms=1, detail={})
    monkeypatch.setattr(validators, "validate_openai", fake)
    monkeypatch.setattr(onboarding_routes._state, "record_validation", lambda p: None)

    c = _client()
    r = c.post("/onboarding/validate",
               json={"provider": "openai", "credentials": {"api_key": "sk-NEW-from-request"}})
    assert r.status_code == 200
    assert captured["key"] == "sk-NEW-from-request"
