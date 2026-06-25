"""Route-level test: POST/PUT /api/cron/jobs persist the `provider` field (M4 glue).

The M4.1 DB column + M4.2/M4.3/M4.4 surfaces all carry an explicit canonical
provider key, but the HTTP route's Pydantic models (CronJobCreate/Update) must
DECLARE `provider` or FastAPI silently drops it before it reaches
manager.create_job — which would break the model selector end-to-end for BOTH
the Portal and Android (they go through this route). This locks the field in.
"""

import pytest
from fastapi.testclient import TestClient

from Orchestrator.scheduler import manager as manager_mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from Orchestrator.checkpoint import app

    db = tmp_path / "cron_route_provider.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    monkeypatch.setattr(manager_mod, "_manager_instance", None, raising=False)
    return TestClient(app)


def test_post_persists_provider(client):
    """A POST carrying provider must persist it (not have Pydantic strip it)."""
    resp = client.post(
        "/api/cron/jobs",
        json={
            "name": "j", "prompt": "hi", "schedule": "0 15 * * *",
            "operator": "system", "provider": "anthropic", "model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["provider"] == "anthropic", (
        "POST dropped provider — the CronJobCreate model is missing the field"
    )


def test_put_persists_provider(client):
    """A PUT changing provider must persist the new canonical key."""
    created = client.post(
        "/api/cron/jobs",
        json={
            "name": "j", "prompt": "hi", "schedule": "0 15 * * *",
            "operator": "system", "provider": "google", "model": "",
        },
    ).json()["job"]
    resp = client.put(
        f"/api/cron/jobs/{created['id']}", json={"provider": "openai"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["provider"] == "openai"
