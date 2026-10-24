"""
Provider/model coupling at the manager sink, reached over HTTP (audit
O15-scheduler-admin-05, -14, unfinished-work W18).

Commit 15c07145 taught the two ToolVault cron tools to pair a job's provider
and model: an omitted model is Auto, a provider switch without a model resets
the model to Auto, a model-only edit naming another vendor moves the job to
that vendor, and a model that obviously belongs to another provider is refused.
None of that lived in CronJobManager, so the HTTP API (the Portal, Android, any
script) still stored "gemini" for an omitted model and accepted pairings that
404 on every fire, and a JSON null model crashed the insert with a 500.

The rules now live in the manager, so every surface gets them; these tests go
through POST/PUT /api/cron/jobs. The live-catalog typo check stays in the tool
layer (it needs a provider round-trip).
"""

import sqlite3

import pytest

import Orchestrator.config as config
from Orchestrator.config import (
    ANTHROPIC_MODEL_DEFAULT,
    GEMINI_MODEL_DEFAULT,
    OPENAI_MODEL_DEFAULT,
)
from Orchestrator.scheduler import executor as sched_exec
from Orchestrator.scheduler import manager as manager_mod


@pytest.fixture()
def client(app_client, monkeypatch):
    # The route resolves the manager singleton; start it fresh on this test's
    # isolated cron DB (isolated_stores already pointed DB_PATH into tmp_path).
    monkeypatch.setattr(manager_mod, "_manager_instance", None)
    return app_client


def _post(client, **body):
    base = {"name": "j", "prompt": "hi", "schedule": "0 15 * * *", "operator": "system"}
    base.update(body)
    return client.post("/api/cron/jobs", json=base)


def _fired(job):
    return sched_exec._resolve_model_name(job["model"], job["provider"])


def test_omitted_model_and_provider_is_auto_on_the_box_default_provider(client, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["model"] == ""
    assert job["provider"] == "anthropic"
    assert _fired(job) == ANTHROPIC_MODEL_DEFAULT


def test_null_model_is_auto_not_a_500(client):
    resp = _post(client, provider="openai", model=None)
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert (job["provider"], job["model"]) == ("openai", "")
    assert _fired(job) == OPENAI_MODEL_DEFAULT


@pytest.mark.parametrize("word, key", [
    ("gemini", "google"), ("claude", "anthropic"), ("grok", "xai"), ("GPT", "openai"),
])
def test_provider_word_is_stored_as_its_catalog_key(client, word, key):
    resp = _post(client, provider=word, model="")
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["provider"] == key


@pytest.mark.parametrize("provider, model, owner", [
    ("anthropic", "gemini", "google"),
    ("anthropic", "gemini-3.1-pro-preview", "google"),
    ("openai", "claude-opus-4-8", "anthropic"),
    ("google", "grok-4.3", "xai"),
    ("anthropic", "lab::qwen-2", "custom"),
    ("custom", "gemini", "google"),
])
def test_foreign_model_is_refused_with_400(client, provider, model, owner):
    resp = _post(client, provider=provider, model=model)
    assert resp.status_code == 400, resp.text
    assert f"belongs to provider {owner}" in resp.json()["detail"]
    assert client.get("/api/cron/jobs").json()["count"] == 0


@pytest.mark.parametrize("provider, model", [
    ("google", "gemini-2.5-computer-use-preview-10-2025"),  # a Google catalog id
    ("computer-use", "claude-opus-4-6"),                   # CU runs any vendor's model
    ("custom", "lab::qwen-2"),
    ("openai", "codex-mini-latest"),                       # not obviously anyone else's
])
def test_legitimate_pairings_are_accepted(client, provider, model):
    resp = _post(client, provider=provider, model=model)
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["model"] == model


def test_put_provider_switch_without_model_resets_to_auto(client):
    job = _post(client, provider="anthropic", model="claude-opus-4-8").json()["job"]
    resp = client.put(f"/api/cron/jobs/{job['id']}", json={"provider": "openai"})
    assert resp.status_code == 200, resp.text
    updated = resp.json()["job"]
    assert (updated["provider"], updated["model"]) == ("openai", "")
    assert _fired(updated) == OPENAI_MODEL_DEFAULT


def test_put_model_only_naming_another_vendor_moves_the_job(client):
    job = _post(client, provider="anthropic", model="claude-opus-4-8").json()["job"]
    resp = client.put(f"/api/cron/jobs/{job['id']}", json={"model": "gemini-3.1-pro-preview"})
    assert resp.status_code == 200, resp.text
    updated = resp.json()["job"]
    assert (updated["provider"], updated["model"]) == ("google", "gemini-3.1-pro-preview")


def test_put_foreign_pair_is_refused_and_row_untouched(client):
    job = _post(client, provider="google", model="").json()["job"]
    resp = client.put(f"/api/cron/jobs/{job['id']}",
                      json={"provider": "anthropic", "model": "gemini"})
    assert resp.status_code == 400
    again = client.get(f"/api/cron/jobs/{job['id']}").json()["job"]
    assert (again["provider"], again["model"]) == ("google", "")


def test_put_restating_provider_repairs_a_stored_foreign_bare_word(client):
    """The shape the old "gemini" default wrote under another provider."""
    job = _post(client, provider="anthropic", model="").json()["job"]
    mgr = manager_mod.get_scheduler_manager()
    conn = sqlite3.connect(mgr.db_path)
    conn.execute("UPDATE cron_jobs SET model = 'gemini' WHERE id = ?", (job["id"],))
    conn.commit()
    conn.close()
    resp = client.put(f"/api/cron/jobs/{job['id']}", json={"provider": "anthropic", "name": "x"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["model"] == ""


def test_full_form_put_from_the_portal_keeps_a_matching_pair(client):
    """The Portal and Android PUT every field; a consistent pair round-trips."""
    job = _post(client, provider="anthropic", model="claude-sonnet-4-6").json()["job"]
    resp = client.put(f"/api/cron/jobs/{job['id']}", json={
        "name": "renamed", "prompt": "hi", "schedule": "0 15 * * *",
        "provider": "anthropic", "model": "claude-sonnet-4-6", "delivery": "snapshot",
        "operator": "system", "one_shot": False,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["model"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# fire time: a stored bare word foreign to the job's provider follows the provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model, provider, fired", [
    ("gemini", "anthropic", ANTHROPIC_MODEL_DEFAULT),
    ("claude", "openai", OPENAI_MODEL_DEFAULT),
    ("gemini", "google", GEMINI_MODEL_DEFAULT),
    ("claude", "anthropic", ANTHROPIC_MODEL_DEFAULT),
])
def test_bare_word_resolves_to_the_jobs_provider(model, provider, fired):
    """A row the old default wrote (provider anthropic, model "gemini") sent a
    Gemini id to Anthropic and 404'd every fire."""
    assert sched_exec._resolve_model_name(model, provider) == fired
