"""
Tests for M4.2 — the cron tool schemas accept ANY provider/model id (free
text), no longer the coarse 5-value enum, AND the executor validates a chosen
specific model id against the live catalog at create/edit time so a typo fails
LOUDLY (not silently at fire time).

M4.2a (schema):
  * create/edit `model` is type:string with NO enum.
  * an optional `provider` string param exists on both schemas.
  * the description documents the interactive/device-bound providers that are
    OUT of cron scope (agents, gemini-agents, realtime, local).
  * the AI can create a job with a SPECIFIC id (e.g. "claude-opus-4-8") and it
    persists.

M4.2b (executor catalog validation):
  * a bogus model for a provider → ToolResult(success=False) with a clear msg.
  * a valid id present in the (mocked) catalog → success.
  * empty model (Auto) → success.
  * a bare provider word ("claude") → success.
  * catalog fetch raising → success (graceful allow — never block on outage).

The catalog fetch is mocked so these tests are deterministic and offline.
"""

import json
from pathlib import Path

import pytest

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager

import ToolVault.tools.create_cron_job.executor as create_exec
import ToolVault.tools.edit_cron_job.executor as edit_exec


_TOOLS = Path(__file__).resolve().parents[2] / "ToolVault" / "tools"


# ---------------------------------------------------------------------------
# M4.2a — schema shape
# ---------------------------------------------------------------------------

def _schema(name: str) -> dict:
    return json.loads((_TOOLS / name / "schema.json").read_text())


@pytest.mark.parametrize("tool", ["create_cron_job", "edit_cron_job"])
def test_model_is_free_text_no_enum(tool):
    model = _schema(tool)["parameters"]["properties"]["model"]
    assert model["type"] == "string"
    assert "enum" not in model, f"{tool} model must not constrain to a fixed enum"


@pytest.mark.parametrize("tool", ["create_cron_job", "edit_cron_job"])
def test_provider_param_present(tool):
    props = _schema(tool)["parameters"]["properties"]
    assert "provider" in props
    assert props["provider"]["type"] == "string"
    assert props["provider"].get("description")


@pytest.mark.parametrize("tool", ["create_cron_job", "edit_cron_job"])
def test_excluded_providers_documented(tool):
    """The interactive/device-bound providers out of cron scope are named."""
    blob = json.dumps(_schema(tool)).lower()
    for excluded in ("agents", "gemini-agents", "realtime", "local"):
        assert excluded in blob, f"{tool} should document '{excluded}' as out of scope"


# ---------------------------------------------------------------------------
# Shared fixtures for executor tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_freetext.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()
    # Make get_scheduler_manager() (used inside the executors) return our temp
    # manager so create/edit hit the isolated DB, not the real one.
    monkeypatch.setattr(
        "Orchestrator.scheduler.get_scheduler_manager", lambda: mgr
    )
    return mgr


@pytest.fixture()
def ctx():
    return ToolContext(operator="system")


def _mock_catalog(monkeypatch, *, models=None, raises=False):
    """Patch the catalog fetch the executors use. `models` is the list of
    valid ids the (single) provider catalog reports."""
    def fake_fetch(provider, operator=None):
        if raises:
            raise RuntimeError("upstream down")
        return {"models": [{"id": m, "name": m} for m in (models or [])]}

    # _validate_model (defined in create_exec, imported by edit_exec) resolves
    # _fetch_catalog_models from create_exec's module namespace, so patching the
    # single chokepoint there covers BOTH the create and edit validation paths.
    monkeypatch.setattr(create_exec, "_fetch_catalog_models", fake_fetch)


# ---------------------------------------------------------------------------
# M4.2a — a specific id persists through create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_persists_specific_model(temp_manager, ctx, monkeypatch):
    # No catalog mock: a specific id must persist through create. Once M4.2b's
    # validation lands it stays green via the graceful-allow path (the live
    # catalog is unreachable in the offline test env).
    res = await create_exec.execute(
        {
            "name": "opus job",
            "prompt": "hi",
            "schedule": "0 15 * * *",
            "provider": "claude",
            "model": "claude-opus-4-8",
        },
        ctx,
    )
    assert res.success, res.result
    job = res.data["job"]
    assert job["model"] == "claude-opus-4-8"
    # The explicit provider param threads through to the stored row.
    assert job["provider"] == "anthropic"
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["model"] == "claude-opus-4-8"
    assert refreshed["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# M4.2b — catalog validation at create time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_bogus_model_rejected(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch, models=["claude-opus-4-8", "claude-sonnet-4-8"])
    res = await create_exec.execute(
        {
            "name": "typo job",
            "prompt": "hi",
            "schedule": "0 15 * * *",
            "provider": "claude",
            "model": "totally-made-up",
        },
        ctx,
    )
    assert res.success is False
    assert "totally-made-up" in res.result
    assert "claude" in res.result.lower() or "anthropic" in res.result.lower()


@pytest.mark.asyncio
async def test_create_valid_model_accepted(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch, models=["claude-opus-4-8"])
    res = await create_exec.execute(
        {
            "name": "ok job",
            "prompt": "hi",
            "schedule": "0 15 * * *",
            "provider": "claude",
            "model": "claude-opus-4-8",
        },
        ctx,
    )
    assert res.success, res.result


@pytest.mark.asyncio
async def test_create_empty_model_skips_validation(temp_manager, ctx, monkeypatch):
    # Even with an empty catalog, Auto (empty model) must pass.
    _mock_catalog(monkeypatch, models=[])
    res = await create_exec.execute(
        {
            "name": "auto job",
            "prompt": "hi",
            "schedule": "0 15 * * *",
            "provider": "claude",
            "model": "",
        },
        ctx,
    )
    assert res.success, res.result


@pytest.mark.asyncio
async def test_create_bare_provider_word_skips_validation(
    temp_manager, ctx, monkeypatch
):
    # A bare provider word ("claude") is a default selector, not an id — never
    # validated against the catalog (so an empty catalog still passes).
    _mock_catalog(monkeypatch, models=[])
    res = await create_exec.execute(
        {
            "name": "bare job",
            "prompt": "hi",
            "schedule": "0 15 * * *",
            "provider": "claude",
            "model": "claude",
        },
        ctx,
    )
    assert res.success, res.result


@pytest.mark.asyncio
async def test_create_catalog_raises_graceful_allow(temp_manager, ctx, monkeypatch):
    # Catalog fetch raising (outage / missing key) must NOT block the create.
    _mock_catalog(monkeypatch, raises=True)
    res = await create_exec.execute(
        {
            "name": "outage job",
            "prompt": "hi",
            "schedule": "0 15 * * *",
            "provider": "claude",
            "model": "claude-brand-new-id",
        },
        ctx,
    )
    assert res.success, res.result


# ---------------------------------------------------------------------------
# M4.2b — catalog validation at edit time
# ---------------------------------------------------------------------------

@pytest.fixture()
def existing_job(temp_manager):
    return temp_manager.create_job(
        name="existing",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        provider="claude",
        model="claude-opus-4-8",
    )


@pytest.mark.asyncio
async def test_edit_bogus_model_rejected(
    temp_manager, ctx, monkeypatch, existing_job
):
    _mock_catalog(monkeypatch, models=["claude-opus-4-8"])
    res = await edit_exec.execute(
        {
            "job_id": existing_job["id"],
            "provider": "claude",
            "model": "nope-not-real",
        },
        ctx,
    )
    assert res.success is False
    assert "nope-not-real" in res.result


@pytest.mark.asyncio
async def test_edit_valid_model_accepted(
    temp_manager, ctx, monkeypatch, existing_job
):
    _mock_catalog(monkeypatch, models=["claude-opus-4-8", "claude-sonnet-4-8"])
    res = await edit_exec.execute(
        {
            "job_id": existing_job["id"],
            "provider": "claude",
            "model": "claude-sonnet-4-8",
        },
        ctx,
    )
    assert res.success, res.result
    assert temp_manager.get_job(existing_job["id"])["model"] == "claude-sonnet-4-8"


@pytest.mark.asyncio
async def test_edit_catalog_raises_graceful_allow(
    temp_manager, ctx, monkeypatch, existing_job
):
    _mock_catalog(monkeypatch, raises=True)
    res = await edit_exec.execute(
        {
            "job_id": existing_job["id"],
            "provider": "claude",
            "model": "claude-some-future-id",
        },
        ctx,
    )
    assert res.success, res.result


@pytest.mark.asyncio
async def test_edit_uses_existing_provider_when_only_model_changes(
    temp_manager, ctx, monkeypatch, existing_job
):
    """When the edit changes only `model` (no provider in the call), validation
    uses the job's STORED provider so the right catalog is consulted."""
    _mock_catalog(monkeypatch, models=["claude-opus-4-8"])
    res = await edit_exec.execute(
        {
            "job_id": existing_job["id"],
            "model": "definitely-not-a-claude-id",
        },
        ctx,
    )
    assert res.success is False
    assert "definitely-not-a-claude-id" in res.result
