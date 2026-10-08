"""
Tests for the cron tools' provider/model pairing.

create_cron_job used to default an omitted model to "gemini" even when the
provider was anthropic/openai/xai/custom, and edit_cron_job kept the old
provider's model when only the provider changed. A bare provider word resolves
to ITS OWN provider's default at fire time (scheduler.executor
._resolve_model_name), so such a job sent a Gemini id to another provider and
every fire 404'd ("model: gemini-3.1-pro-preview").

Now:
  * create: an omitted model with a provider is Auto ('' -> that provider's
    default at fire time); with no provider the legacy "gemini" default stays.
  * create/edit: a model that obviously belongs to a different provider (a
    foreign bare word, or a foreign specific id the catalog can't vouch for)
    is rejected LOUDLY at create/edit time.
  * edit: a provider switch with no model resets the model to Auto; restating
    the provider repairs a row whose stored model is foreign to it.

The catalog fetch and the scheduler DB are isolated, so these tests are
deterministic and offline.
"""

import pytest

from Orchestrator.config import (
    ANTHROPIC_MODEL_DEFAULT,
    CU_MODEL_DEFAULT,
    GEMINI_MODEL_DEFAULT,
    OPENAI_MODEL_DEFAULT,
    XAI_MODEL_DEFAULT,
)
from Orchestrator.scheduler import executor as sched_exec
from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager
from Orchestrator.toolvault.context import ToolContext

import ToolVault.tools.create_cron_job.executor as create_exec
import ToolVault.tools.edit_cron_job.executor as edit_exec


_CUSTOM_DEFAULT = "lab::llama-3-8b"

# Per-provider catalogs the mocked fetch reports.
_CATALOG = {
    "google": ["gemini-3.1-pro-preview", "gemini-2.5-computer-use-preview-10-2025"],
    "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6"],
    "openai": ["gpt-5.1"],
    "xai": ["grok-4.3"],
}


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_provider_model.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()
    # The executors resolve the manager via get_scheduler_manager(); point it
    # at this isolated instance so create/edit never touch the real job store.
    monkeypatch.setattr(
        "Orchestrator.scheduler.get_scheduler_manager", lambda: mgr
    )
    # custom's Auto default is a live registry read; pin it for resolution.
    monkeypatch.setattr(sched_exec, "_custom_default_model", lambda: _CUSTOM_DEFAULT)
    return mgr


@pytest.fixture()
def ctx():
    return ToolContext(operator="system")


def _mock_catalog(monkeypatch, *, raises=False, empty=False):
    calls = []

    def fake_fetch(provider, operator=None):
        calls.append(provider)
        if raises:
            raise RuntimeError("upstream down")
        ids = [] if empty else _CATALOG.get(provider, [])
        return {"models": [{"id": m, "name": m} for m in ids]}

    # Single chokepoint shared by the create and edit validation paths.
    monkeypatch.setattr(create_exec, "_fetch_catalog_models", fake_fetch)
    return calls


def _fired_model(job):
    """The model id the scheduler would send to /chat for this job."""
    return sched_exec._resolve_model_name(job["model"], job["provider"])


async def _create(ctx, **params):
    base = {"name": "job", "prompt": "hi", "schedule": "0 15 * * *"}
    base.update(params)
    return await create_exec.execute(base, ctx)


# ---------------------------------------------------------------------------
# create -- model omitted follows the provider
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_word, key, fired",
    [
        ("gemini", "google", GEMINI_MODEL_DEFAULT),
        ("google", "google", GEMINI_MODEL_DEFAULT),
        ("claude", "anthropic", ANTHROPIC_MODEL_DEFAULT),
        ("anthropic", "anthropic", ANTHROPIC_MODEL_DEFAULT),
        ("openai", "openai", OPENAI_MODEL_DEFAULT),
        ("grok", "xai", XAI_MODEL_DEFAULT),
        ("xai", "xai", XAI_MODEL_DEFAULT),
        ("custom", "custom", _CUSTOM_DEFAULT),
        ("computer-use", "computer-use", CU_MODEL_DEFAULT),
    ],
)
async def test_create_model_omitted_is_auto_for_provider(
    temp_manager, ctx, monkeypatch, provider_word, key, fired
):
    _mock_catalog(monkeypatch)
    res = await _create(ctx, provider=provider_word)
    assert res.success, res.result
    job = temp_manager.get_job(res.data["job"]["id"])
    assert job["provider"] == key
    assert job["model"] == ""  # Auto, not "gemini"
    assert _fired_model(job) == fired


@pytest.mark.asyncio
async def test_create_model_none_treated_as_omitted(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch)
    res = await _create(ctx, provider="claude", model=None)
    assert res.success, res.result
    job = temp_manager.get_job(res.data["job"]["id"])
    assert job["model"] == ""
    assert _fired_model(job) == ANTHROPIC_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_create_no_provider_no_model_keeps_legacy_gemini(
    temp_manager, ctx, monkeypatch
):
    _mock_catalog(monkeypatch)
    res = await _create(ctx)
    assert res.success, res.result
    job = temp_manager.get_job(res.data["job"]["id"])
    assert job["model"] == "gemini"
    assert job["provider"] == "google"
    assert _fired_model(job) == GEMINI_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_create_model_only_derives_provider(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch)
    res = await _create(ctx, model="claude-sonnet-4-6")
    assert res.success, res.result
    job = temp_manager.get_job(res.data["job"]["id"])
    assert job["model"] == "claude-sonnet-4-6"
    assert job["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# create -- model given
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_word, model",
    [
        ("gemini", "gemini-3.1-pro-preview"),
        ("claude", "claude-opus-4-8"),
        ("openai", "gpt-5.1"),
        ("grok", "grok-4.3"),
    ],
)
async def test_create_same_provider_specific_id_persists(
    temp_manager, ctx, monkeypatch, provider_word, model
):
    _mock_catalog(monkeypatch)
    res = await _create(ctx, provider=provider_word, model=model)
    assert res.success, res.result
    job = temp_manager.get_job(res.data["job"]["id"])
    assert job["model"] == model
    assert _fired_model(job) == model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_word, model, fired",
    [
        ("claude", "claude", ANTHROPIC_MODEL_DEFAULT),
        ("anthropic", "Claude", ANTHROPIC_MODEL_DEFAULT),
        ("gemini", "google", GEMINI_MODEL_DEFAULT),
        ("openai", "gpt", OPENAI_MODEL_DEFAULT),
        ("xai", "grok", XAI_MODEL_DEFAULT),
        ("custom", "custom", _CUSTOM_DEFAULT),
    ],
)
async def test_create_same_provider_bare_word_accepted(
    temp_manager, ctx, monkeypatch, provider_word, model, fired
):
    _mock_catalog(monkeypatch)
    res = await _create(ctx, provider=provider_word, model=model)
    assert res.success, res.result
    assert _fired_model(temp_manager.get_job(res.data["job"]["id"])) == fired


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_word, model, owner",
    [
        ("claude", "gemini", "google"),
        ("anthropic", "google", "google"),
        ("openai", "claude", "anthropic"),
        ("grok", "gpt", "openai"),
        ("gemini", "grok", "xai"),
        ("claude", "computer-use", "computer-use"),
        ("custom", "gemini", "google"),
        ("gemini", "custom", "custom"),
    ],
)
async def test_create_foreign_bare_word_rejected(
    temp_manager, ctx, monkeypatch, provider_word, model, owner
):
    _mock_catalog(monkeypatch)
    res = await _create(ctx, provider=provider_word, model=model)
    assert res.success is False
    assert model in res.result
    assert f"belongs to provider {owner}" in res.result
    assert temp_manager.list_jobs() == []  # nothing persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("raises, empty", [(False, False), (True, False), (False, True)])
async def test_create_foreign_specific_id_rejected_even_when_catalog_unavailable(
    temp_manager, ctx, monkeypatch, raises, empty
):
    """The reported breakage: provider anthropic + a Gemini id. Rejected with
    the reachable catalog AND on the graceful-allow paths (fetch raises /
    empty catalog), where an unknown id would otherwise be let through."""
    _mock_catalog(monkeypatch, raises=raises, empty=empty)
    res = await _create(ctx, provider="claude", model="gemini-3.1-pro-preview")
    assert res.success is False
    assert "gemini-3.1-pro-preview" in res.result
    assert "belongs to provider google" in res.result
    assert temp_manager.list_jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_word, model",
    [
        ("openai", "claude-opus-4-8"),
        ("gemini", "gpt-5.1"),
        ("claude", "grok-4.3"),
        ("grok", "claude-sonnet-4-6"),
        ("claude", "lab::qwen-2"),
    ],
)
async def test_create_foreign_specific_id_rejected_on_catalog_outage(
    temp_manager, ctx, monkeypatch, provider_word, model
):
    _mock_catalog(monkeypatch, raises=True)
    res = await _create(ctx, provider=provider_word, model=model)
    assert res.success is False
    assert "belongs to provider" in res.result


@pytest.mark.asyncio
async def test_create_unrecognised_id_still_graceful_on_outage(
    temp_manager, ctx, monkeypatch
):
    """An id _model_to_provider can only place via its google catch-all is not
    obviously foreign -- the outage graceful-allow still applies."""
    _mock_catalog(monkeypatch, raises=True)
    res = await _create(ctx, provider="openai", model="codex-mini-latest")
    assert res.success, res.result
    assert temp_manager.get_job(res.data["job"]["id"])["model"] == "codex-mini-latest"


@pytest.mark.asyncio
async def test_create_catalog_confirmed_id_wins_over_heuristic(
    temp_manager, ctx, monkeypatch
):
    """An id the provider's own catalog lists is accepted even though the
    substring heuristic would place it elsewhere (a Gemini CU id -> 'computer-use')."""
    _mock_catalog(monkeypatch)
    res = await _create(
        ctx, provider="gemini", model="gemini-2.5-computer-use-preview-10-2025"
    )
    assert res.success, res.result


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-opus-4-6", "gemini", "gpt-5.1"])
async def test_create_computer_use_accepts_any_vendor_model(
    temp_manager, ctx, monkeypatch, model
):
    """computer-use legitimately runs other vendors' models -- never flagged."""
    _mock_catalog(monkeypatch, raises=True)
    res = await _create(ctx, provider="computer-use", model=model)
    assert res.success, res.result


@pytest.mark.asyncio
async def test_create_custom_accepts_qualified_id(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch, raises=True)
    res = await _create(ctx, provider="custom", model="lab::qwen-2")
    assert res.success, res.result
    job = temp_manager.get_job(res.data["job"]["id"])
    assert _fired_model(job) == "lab::qwen-2"


# ---------------------------------------------------------------------------
# edit -- provider switch with / without model
# ---------------------------------------------------------------------------

def _seed(mgr, provider, model):
    return mgr.create_job(
        name="seed",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        provider=provider,
        model=model,
    )


async def _edit(ctx, job_id, **params):
    return await edit_exec.execute({"job_id": job_id, **params}, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_provider, start_model, new_word, key, fired",
    [
        ("anthropic", "claude-opus-4-8", "gemini", "google", GEMINI_MODEL_DEFAULT),
        ("google", "gemini", "claude", "anthropic", ANTHROPIC_MODEL_DEFAULT),
        ("google", "gemini-3.1-pro-preview", "openai", "openai", OPENAI_MODEL_DEFAULT),
        ("openai", "gpt-5.1", "grok", "xai", XAI_MODEL_DEFAULT),
        ("xai", "grok-4.3", "custom", "custom", _CUSTOM_DEFAULT),
        ("anthropic", "claude-opus-4-8", "computer-use", "computer-use", CU_MODEL_DEFAULT),
        ("computer-use", "claude-opus-4-6", "claude", "anthropic", ANTHROPIC_MODEL_DEFAULT),
    ],
)
async def test_edit_provider_switch_without_model_resets_to_auto(
    temp_manager, ctx, monkeypatch, start_provider, start_model, new_word, key, fired
):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, start_provider, start_model)
    res = await _edit(ctx, job["id"], provider=new_word)
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["provider"] == key
    assert refreshed["model"] == ""
    assert _fired_model(refreshed) == fired


@pytest.mark.asyncio
async def test_edit_provider_switch_with_model_uses_given_model(
    temp_manager, ctx, monkeypatch
):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "google", "gemini")
    res = await _edit(ctx, job["id"], provider="claude", model="claude-sonnet-4-6")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["provider"] == "anthropic"
    assert refreshed["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_edit_provider_switch_with_explicit_auto(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "google", "gemini-3.1-pro-preview")
    res = await _edit(ctx, job["id"], provider="xai", model="")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["model"] == ""
    assert _fired_model(refreshed) == XAI_MODEL_DEFAULT


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini", "gemini-3.1-pro-preview"])
async def test_edit_provider_switch_with_foreign_model_rejected(
    temp_manager, ctx, monkeypatch, model
):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "google", "gemini")
    res = await _edit(ctx, job["id"], provider="claude", model=model)
    assert res.success is False
    assert "belongs to provider google" in res.result
    refreshed = temp_manager.get_job(job["id"])
    assert (refreshed["provider"], refreshed["model"]) == ("google", "gemini")


@pytest.mark.asyncio
async def test_edit_same_provider_keeps_matching_model(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "anthropic", "claude-sonnet-4-6")
    res = await _edit(ctx, job["id"], provider="claude", name="renamed")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["model"] == "claude-sonnet-4-6"
    assert refreshed["name"] == "renamed"


@pytest.mark.asyncio
async def test_edit_restating_provider_repairs_foreign_stored_model(
    temp_manager, ctx, monkeypatch
):
    """A row the old create default produced (anthropic + "gemini") is repaired
    by restating the provider, even though the provider doesn't change."""
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "anthropic", "gemini")
    assert _fired_model(job) == GEMINI_MODEL_DEFAULT  # the broken pairing
    res = await _edit(ctx, job["id"], provider="claude")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["model"] == ""
    assert _fired_model(refreshed) == ANTHROPIC_MODEL_DEFAULT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model, key, fired",
    [
        ("gemini", "google", GEMINI_MODEL_DEFAULT),
        ("gemini-3.1-pro-preview", "google", "gemini-3.1-pro-preview"),
    ],
)
async def test_edit_model_only_foreign_model_switches_provider(
    temp_manager, ctx, monkeypatch, model, key, fired
):
    """Schema: provider is derived from the model when omitted -- a model-only
    edit naming another vendor's model moves the job to that vendor instead of
    storing a pairing that 404s every fire."""
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "anthropic", "claude-opus-4-8")
    res = await _edit(ctx, job["id"], model=model)
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert (refreshed["provider"], refreshed["model"]) == (key, model)
    assert _fired_model(refreshed) == fired


@pytest.mark.asyncio
async def test_edit_model_only_switch_on_row_without_stored_provider(
    temp_manager, ctx, monkeypatch
):
    """Legacy rows (and create calls without a provider) store no provider;
    get_job reports one derived from the OLD model. A model-only vendor switch
    must still work there -- it did before the foreign-model check existed."""
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, None, "gemini")
    assert temp_manager.get_job(job["id"])["provider"] == "google"  # derived
    res = await _edit(ctx, job["id"], model="claude")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["provider"] == "anthropic"
    assert _fired_model(refreshed) == ANTHROPIC_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_edit_blank_provider_cannot_smuggle_foreign_model(
    temp_manager, ctx, monkeypatch
):
    """A whitespace provider counts as omitted, so the foreign model is caught
    (and the provider derived) instead of skipping the check."""
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "anthropic", "claude-opus-4-8")
    res = await _edit(ctx, job["id"], provider="  ", model="gemini")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["provider"] == "google"
    assert _fired_model(refreshed) == GEMINI_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_edit_restating_provider_keeps_catalog_valid_specific_id(
    temp_manager, ctx, monkeypatch
):
    """The restated-provider repair only touches stored bare provider words; a
    specific id the substring heuristic misplaces (a google CU id maps to
    computer-use) is left alone."""
    _mock_catalog(monkeypatch)
    cu_id = "gemini-2.5-computer-use-preview-10-2025"
    job = _seed(temp_manager, "google", cu_id)
    res = await _edit(ctx, job["id"], provider="gemini", name="renamed")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert (refreshed["provider"], refreshed["model"]) == ("google", cu_id)


@pytest.mark.asyncio
async def test_edit_without_provider_leaves_model_alone(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "openai", "gpt-5.1")
    res = await _edit(ctx, job["id"], prompt="new prompt")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert (refreshed["provider"], refreshed["model"]) == ("openai", "gpt-5.1")
    assert refreshed["prompt"] == "new prompt"


@pytest.mark.asyncio
async def test_edit_blank_provider_is_not_a_switch(temp_manager, ctx, monkeypatch):
    _mock_catalog(monkeypatch)
    job = _seed(temp_manager, "anthropic", "claude-opus-4-8")
    res = await _edit(ctx, job["id"], provider="  ")
    assert res.success, res.result
    refreshed = temp_manager.get_job(job["id"])
    assert (refreshed["provider"], refreshed["model"]) == ("anthropic", "claude-opus-4-8")
