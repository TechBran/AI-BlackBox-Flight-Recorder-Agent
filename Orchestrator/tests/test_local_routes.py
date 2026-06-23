"""Tests for the on-device (local Gemma) tool-bridge endpoints (Task 0.2).

These exercise the HTTP contract of:
  POST /local/tools/search   — semantic tool discovery (≤ k schemas)
  POST /local/tools/execute  — execute a ToolVault tool

Hermetic: the live embedding backend (Gemini key is IP-restricted) is NEVER
hit. ``execute_tool`` and the meta-tool search are monkeypatched at module
level on ``local_routes`` so no network/credentials are required.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator.routes import local_routes
from Orchestrator.local_provider import registry as registry_module


@pytest.fixture
def client():
    """TestClient with the startup embedding-sync hook mocked (it spawns a
    daemon thread calling sync_embeddings, which would hit the network)."""
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_src:
        m_src.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def isolate_local_registry(monkeypatch, tmp_path):
    """Point the local-provider registry at a per-test tmp store AND reset the
    cached module-level singleton.

    HAZARD: ``get_local_registry()`` caches a singleton on first call, and that
    instance captures ``STORE_FILE`` at construction. Patching ``STORE_FILE``
    without also nulling ``_registry`` would leave a stale singleton bound to the
    REAL ``Orchestrator/local_provider/local_devices.json`` — polluting the repo.
    Resetting ``_registry`` forces a fresh instance reading the patched path.
    """
    monkeypatch.setattr(registry_module, "STORE_FILE", tmp_path / "local_devices.json")
    monkeypatch.setattr(registry_module, "_registry", None)


# ---------------------------------------------------------------------------
# /local/tools/execute
# ---------------------------------------------------------------------------

def test_tools_execute_routes_through_execute_tool(client, monkeypatch):
    """POST /local/tools/execute hands (tool, params+operator, operator) to
    execute_tool and surfaces {success, result}."""

    class _FakeResult:
        success = True

        def __init__(self, tool, operator):
            self.result = {"echo": tool, "op": operator}

    async def fake_execute_tool(tool, params, operator):
        # operator must be threaded both into params AND passed positionally
        assert params.get("operator") == operator
        return _FakeResult(tool, operator)

    monkeypatch.setattr(local_routes, "execute_tool", fake_execute_tool)

    resp = client.post(
        "/local/tools/execute",
        json={"tool": "search_snapshots", "params": {"query": "x"}, "operator": "Brandon"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "result": {"echo": "search_snapshots", "op": "Brandon"},
    }


def test_tools_execute_requires_tool(client):
    """Missing/blank tool → 400 with success False."""
    resp = client.post("/local/tools/execute", json={"params": {"query": "x"}})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


# ---------------------------------------------------------------------------
# /local/tools/search
# ---------------------------------------------------------------------------

def test_tools_search_returns_schemas(client, monkeypatch):
    """POST /local/tools/search returns ≤ k {name, description, parameters}
    dicts. The underlying meta_tool call is mocked (endpoint-contract test,
    not a semantic-search-quality test)."""

    class _FakeMetaResult:
        def __init__(self, success, result, data=None):
            self.success = success
            self.result = result
            self.data = data

    def fake_execute(action, **params):
        if action == "search":
            return _FakeMetaResult(
                True,
                "found",
                data={"matches": [
                    {"name": "tool_a", "score": 0.9},
                    {"name": "tool_b", "score": 0.8},
                    {"name": "tool_c", "score": 0.7},
                ]},
            )
        if action == "read":
            name = params.get("tool_name")
            return _FakeMetaResult(
                True,
                f"=== Tool: {name} ===",
                data={
                    "name": name,
                    "schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    "description": f"desc for {name}",
                },
            )
        return _FakeMetaResult(False, "unknown action")

    monkeypatch.setattr(local_routes.meta_tool, "execute", fake_execute)

    resp = client.post("/local/tools/search", json={"query": "search my memory", "k": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["tools"]) == 3
    for t in body["tools"]:
        assert "name" in t
        assert "parameters" in t
        assert "description" in t


def test_tools_search_description_from_real_read_shape(client, monkeypatch):
    """Regression: with `read` returning the REAL post-fix _action_read data
    shape (name/schema/groups/tier/description), the bridge surfaces the real
    description + schema. Would have caught the missing-`description` bug."""

    class _FakeMetaResult:
        def __init__(self, success, result, data=None):
            self.success = success
            self.result = result
            self.data = data

    def fake_execute(action, **params):
        if action == "search":
            return _FakeMetaResult(
                True,
                "found",
                data={"matches": [{"name": "roll_dice", "score": 0.9}]},
            )
        if action == "read":
            return _FakeMetaResult(
                True,
                "=== Tool: roll_dice ===",
                data={
                    "name": "roll_dice",
                    "schema": {"type": "object", "properties": {}},
                    "groups": [],
                    "tier": 1,
                    "description": "Roll dice",
                },
            )
        return _FakeMetaResult(False, "unknown action")

    monkeypatch.setattr(local_routes.meta_tool, "execute", fake_execute)

    resp = client.post("/local/tools/search", json={"query": "roll a die", "k": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["tools"]) == 1
    tool = body["tools"][0]
    assert tool["description"] == "Roll dice"
    assert tool["parameters"] == {"type": "object", "properties": {}}


def test_tools_search_skips_failed_read(client, monkeypatch):
    """A hit whose `read` fails (stale/renamed tool) is skipped, not 500'd and
    not appended as a garbage empty-schema entry."""

    class _FakeMetaResult:
        def __init__(self, success, result, data=None):
            self.success = success
            self.result = result
            self.data = data

    def fake_execute(action, **params):
        if action == "search":
            return _FakeMetaResult(
                True,
                "found",
                data={"matches": [
                    {"name": "good_tool", "score": 0.9},
                    {"name": "stale_tool", "score": 0.8},
                ]},
            )
        if action == "read":
            name = params.get("tool_name")
            if name == "good_tool":
                return _FakeMetaResult(
                    True,
                    "=== Tool: good_tool ===",
                    data={
                        "name": "good_tool",
                        "schema": {"type": "object", "properties": {}},
                        "groups": [],
                        "tier": 2,
                        "description": "A working tool",
                    },
                )
            return _FakeMetaResult(False, "not found", data=None)
        return _FakeMetaResult(False, "unknown action")

    monkeypatch.setattr(local_routes.meta_tool, "execute", fake_execute)

    resp = client.post("/local/tools/search", json={"query": "do something", "k": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["tools"]) == 1
    assert body["tools"][0]["name"] == "good_tool"


def test_tools_search_requires_query(client):
    """Empty query → 400 with success False."""
    resp = client.post("/local/tools/search", json={"query": "  "})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "query required"


# ---------------------------------------------------------------------------
# /local/device/attest + /local/device/status + /local/device/autonomy
# (Task 0.3 — backed by the real registry against an isolated tmp store via the
# autouse ``isolate_local_registry`` fixture above.)
# ---------------------------------------------------------------------------

def test_attest_then_status(client):
    """Attesting a device makes the local provider available for that operator;
    status reflects the attested model and the defaulted autonomy_mode."""
    resp = client.post(
        "/local/device/attest",
        json={
            "operator": "Brandon",
            "device_id": "pixel-9",
            "model_slug": "gemma-4-e4b",
            "version": "1.0",
            "sha256": "abc123",
            "delegate": "gpu",
            # autonomy_mode omitted → should default to "permission"
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["device"]["model_slug"] == "gemma-4-e4b"

    resp = client.get("/local/device/status", params={"operator": "Brandon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["models"][0]["model_slug"] == "gemma-4-e4b"
    assert body["models"][0]["autonomy_mode"] == "permission"


def test_status_unknown_operator(client):
    """An operator with no attestation → available False, empty models list."""
    resp = client.get("/local/device/status", params={"operator": "Nobody"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["models"] == []


def test_attest_requires_operator_and_device(client):
    """Missing device_id → 400."""
    resp = client.post(
        "/local/device/attest",
        json={
            "operator": "Brandon",
            "model_slug": "gemma-4-e4b",
            "version": "1.0",
            "sha256": "abc123",
            "delegate": "gpu",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


def test_attest_requires_operator(client):
    """device_id present but operator missing/blank → 400 with success False."""
    resp = client.post(
        "/local/device/attest",
        json={
            "operator": "  ",
            "device_id": "pixel-9",
            "model_slug": "gemma-4-e4b",
            "version": "1.0",
            "sha256": "abc123",
            "delegate": "gpu",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


def test_autonomy_flips_mode(client):
    """Attest (default permission), then flip to yolo; status reflects yolo."""
    client.post(
        "/local/device/attest",
        json={
            "operator": "Brandon",
            "device_id": "pixel-9",
            "model_slug": "gemma-4-e4b",
            "version": "1.0",
            "sha256": "abc123",
            "delegate": "gpu",
        },
    )

    resp = client.post(
        "/local/device/autonomy",
        json={"operator": "Brandon", "device_id": "pixel-9", "mode": "yolo"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["device"]["autonomy_mode"] == "yolo"

    resp = client.get("/local/device/status", params={"operator": "Brandon"})
    assert resp.json()["models"][0]["autonomy_mode"] == "yolo"


def test_autonomy_unknown_device_404(client):
    """Flipping autonomy on a never-attested device → 404, success False."""
    resp = client.post(
        "/local/device/autonomy",
        json={"operator": "Ghost", "device_id": "nonexistent", "mode": "yolo"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "device not found"


def test_autonomy_rejects_invalid_mode(client):
    """An autonomy mode outside {yolo, permission} → 400."""
    resp = client.post(
        "/local/device/autonomy",
        json={"operator": "Brandon", "device_id": "pixel-9", "mode": "banana"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


# ---------------------------------------------------------------------------
# GET /local/system-prompt — persona (behavioral core) for on-device parity
# (Task 0.4 — exposes the SAME behavioral_core text the cloud /chat path
# prepends, so the on-device Gemma agent loop shares persona/anti-sycophancy.)
# ---------------------------------------------------------------------------

def test_system_prompt_returns_prompt_and_version(client):
    """GET /local/system-prompt → 200 with a non-empty prompt + version.

    The version must be DERIVED from the prompt (12-char sha256 hex), not a
    hardcoded constant — proving a prompt edit changes the version.
    """
    import hashlib

    resp = client.get("/local/system-prompt", params={"operator": "Brandon"})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["prompt"], str)
    assert body["prompt"].strip()
    assert isinstance(body["version"], str)
    assert body["version"]
    assert body["version"] == hashlib.sha256(body["prompt"].encode("utf-8")).hexdigest()[:12]


def test_system_prompt_version_is_stable(client):
    """Two GETs with the same operator return the SAME prompt + version
    (the version is a deterministic hash of the prompt text)."""
    r1 = client.get("/local/system-prompt", params={"operator": "Brandon"})
    r2 = client.get("/local/system-prompt", params={"operator": "Brandon"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["prompt"] == r2.json()["prompt"]
    assert r1.json()["version"] == r2.json()["version"]


def test_system_prompt_is_operator_persona(client, monkeypatch):
    """Parity guard: the returned prompt is EXACTLY the operator's persona
    (the same text the cloud /chat path prepends via get_persona). An operator
    with no custom persona gets the lean default; setting a custom persona makes
    the endpoint return that custom text. If someone hand-rolls a divergent
    persona string, this fails."""
    from Orchestrator import state
    from Orchestrator.behavioral_core import DEFAULT_PERSONA_CHAT

    # No custom persona set -> lean default.
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {})
    resp = client.get("/local/system-prompt", params={"operator": "NoPersonaOp"})
    assert resp.status_code == 200
    assert resp.json()["prompt"] == DEFAULT_PERSONA_CHAT

    # Custom persona set -> that custom text is returned for that operator.
    monkeypatch.setattr(
        state, "OPERATOR_PREFERENCES", {"CustomOp": {"persona": "CUSTOM-PERSONA-XYZ"}}
    )
    resp2 = client.get("/local/system-prompt", params={"operator": "CustomOp"})
    assert resp2.status_code == 200
    assert resp2.json()["prompt"] == "CUSTOM-PERSONA-XYZ"
