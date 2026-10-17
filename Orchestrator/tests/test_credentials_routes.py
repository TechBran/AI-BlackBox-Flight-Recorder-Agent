"""/onboarding/credentials upload/list/delete (audit O15-scheduler-admin-06 and
its correction: credentials/ also hosts BlackBox's own registries).

- A JSON array/string/number/null upload is a 400, not a 500 that leaves a file
  behind which then wedges GET /onboarding/credentials on every call.
- A non-object file already on disk no longer wedges the list.
- custom_models.json / voice_agents.json / xai_phone.json are BlackBox
  registries, not credentials: they cannot be overwritten by an upload (which
  would also point GOOGLE_APPLICATION_CREDENTIALS at them), deleted through the
  credentials panel, or listed there.
- Only a real Google service-account key is accepted (U3 review). The upload
  points GOOGLE_APPLICATION_CREDENTIALS at the file, and google-auth honours
  far more than service accounts there: an `external_account` config names a
  local file (.env) as its subject token and POSTs it to a token_url of the
  uploader's choosing on the next refresh, and a service account's token_uri
  is POSTed to as well. So the type, the key material and the token endpoint
  are all checked BEFORE anything is written.

Hermetic: CREDS_DIR and secrets_writer.ENV_FILE point into tmp_path, and the
process env var the upload mirrors is restored by monkeypatch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.onboarding import secrets_writer
from Orchestrator.routes import credentials_routes as cr

SA = {"type": "service_account", "client_email": "svc@example.iam.gserviceaccount.com",
      "project_id": "example-project", "private_key_id": "0" * 8,
      # A placeholder: the route checks the shape, it never parses the key.
      "private_key": "test-placeholder-not-a-key",
      "token_uri": "https://oauth2.googleapis.com/token"}

# google-auth's external_account config: credential_source.file is read and
# POSTed (as subject_token) to token_url on every refresh.
EXTERNAL_ACCOUNT = {
    "type": "external_account",
    "audience": "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/x",
    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
    "token_url": "https://collector.example/token",
    "credential_source": {"file": "/srv/blackbox/.env"},
}


@pytest.fixture
def creds_dir(tmp_path, monkeypatch):
    d = tmp_path / "credentials"
    d.mkdir()
    monkeypatch.setattr(cr, "CREDS_DIR", d)
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    return d


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(cr.router)
    return TestClient(app)


def _upload(client, name, payload: bytes, **params):
    return client.post("/onboarding/credentials/upload", params=params,
                       files={"file": (name, payload, "application/json")})


@pytest.mark.parametrize("payload", [b"[1, 2]", b"\"a string\"", b"42", b"null", b"true"])
def test_non_object_json_upload_is_400_and_writes_nothing(client, creds_dir, payload):
    r = _upload(client, "weird.json", payload)
    assert r.status_code == 400, r.text
    assert list(creds_dir.iterdir()) == []
    assert secrets_writer.ENV_FILE.read_text() == ""


def test_list_survives_a_non_object_file_already_on_disk(client, creds_dir):
    (creds_dir / "legacy.json").write_text("[1, 2, 3]")
    (creds_dir / "sa.json").write_text(json.dumps(SA))
    r = client.get("/onboarding/credentials")
    assert r.status_code == 200, r.text
    files = {f["filename"]: f for f in r.json()["files"]}
    assert files["legacy.json"]["is_google_service_account"] is False
    assert files["sa.json"]["is_google_service_account"] is True


def test_service_account_upload_still_works(client, creds_dir):
    r = _upload(client, "my-sa.json", json.dumps(SA).encode())
    assert r.status_code == 200, r.text
    assert r.json()["is_service_account"] is True
    target = creds_dir / "my-sa.json"
    assert target.exists() and (target.stat().st_mode & 0o777) == 0o600
    assert f"GOOGLE_APPLICATION_CREDENTIALS={target}" in secrets_writer.ENV_FILE.read_text()


@pytest.mark.parametrize("name", sorted(cr.RESERVED_FILENAMES))
def test_registry_files_cannot_be_overwritten_by_upload(client, creds_dir, name):
    registry = creds_dir / name
    registry.write_text('{"servers": []}')
    r = _upload(client, name, json.dumps(SA).encode())
    assert r.status_code == 400, r.text
    assert registry.read_text() == '{"servers": []}'
    assert secrets_writer.ENV_FILE.read_text() == ""


@pytest.mark.parametrize("name", sorted(cr.RESERVED_FILENAMES))
def test_registry_files_cannot_be_deleted_from_the_credentials_panel(client, creds_dir, name):
    registry = creds_dir / name
    registry.write_text("{}")
    r = client.delete(f"/onboarding/credentials/{name}")
    assert r.status_code == 400, r.text
    assert registry.exists()


def test_registry_files_are_not_listed_as_credentials(client, creds_dir):
    for name in cr.RESERVED_FILENAMES:
        (creds_dir / name).write_text("{}")
    (creds_dir / "sa.json").write_text(json.dumps(SA))
    names = [f["filename"] for f in client.get("/onboarding/credentials").json()["files"]]
    assert names == ["sa.json"]


def test_reserved_names_cover_every_registry_under_credentials():
    """Every module that keeps its registry in credentials/ is reserved."""
    from Orchestrator.onboarding import custom_servers
    from Orchestrator.voice_agents import registry as voice_agents
    from Orchestrator.xai_phone import provisioning as xai_phone
    for path in (custom_servers.REGISTRY_PATH, voice_agents.REGISTRY_PATH,
                 xai_phone.STORE_PATH):
        assert Path(path).name in cr.RESERVED_FILENAMES, path


@pytest.mark.parametrize("name", ["../escape.json", "a/b.json", "x.txt", ".json", "a b.json"])
def test_bad_filenames_are_rejected(client, creds_dir, name):
    r = _upload(client, name, b"{}")
    assert r.status_code in (400, 422), r.text
    assert list(creds_dir.iterdir()) == []


def test_oversize_upload_is_413(client, creds_dir):
    r = _upload(client, "big.json", b"{\"k\": \"" + b"x" * (65 * 1024) + b"\"}")
    assert r.status_code == 413


def _assert_nothing_written(creds_dir):
    assert list(creds_dir.iterdir()) == []
    assert secrets_writer.ENV_FILE.read_text() == ""
    import os
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


@pytest.mark.parametrize("set_gac", ["true", "false"])
def test_external_account_config_is_refused_and_writes_nothing(client, creds_dir, set_gac):
    r = _upload(client, "wif.json", json.dumps(EXTERNAL_ACCOUNT).encode(),
                set_as_google_app_creds=set_gac)
    assert r.status_code == 400, r.text
    assert "service" in r.json()["detail"].lower()
    _assert_nothing_written(creds_dir)


@pytest.mark.parametrize("payload", [
    {},
    {"type": "authorized_user", "client_id": "x", "client_secret": "y", "refresh_token": "z"},
    {"type": "impersonated_service_account", "source_credentials": EXTERNAL_ACCOUNT,
     "service_account_impersonation_url": "https://collector.example/x"},
    {"type": "external_account_authorized_user", "token_url": "https://collector.example/t"},
    {**SA, "type": "Service_Account"},
    {k: v for k, v in SA.items() if k != "client_email"},
    {k: v for k, v in SA.items() if k != "private_key"},
    {**SA, "private_key": ""},
    {**SA, "private_key": 123},
    {**SA, "client_email": ["svc@example.iam.gserviceaccount.com"]},
])
def test_anything_but_a_service_account_key_is_refused(client, creds_dir, payload):
    r = _upload(client, "not-sa.json", json.dumps(payload).encode())
    assert r.status_code == 400, r.text
    _assert_nothing_written(creds_dir)


@pytest.mark.parametrize("token_uri", [
    "http://127.0.0.1:9091/onboarding/save",
    "https://collector.example/token",
    "https://oauth2.googleapis.com.collector.example/token",
    "http://oauth2.googleapis.com/token",
    "https://oauth2.googleapis.com/token?x=1",
    "",
    None,
])
def test_a_service_account_with_a_foreign_token_endpoint_is_refused(client, creds_dir, token_uri):
    """google-auth POSTs a signed assertion to token_uri on refresh; a key
    file must not be able to aim the box's requests anywhere else."""
    r = _upload(client, "sa.json", json.dumps({**SA, "token_uri": token_uri}).encode())
    assert r.status_code == 400, r.text
    _assert_nothing_written(creds_dir)


@pytest.mark.parametrize("token_uri", [
    "https://oauth2.googleapis.com/token",
    "https://accounts.google.com/o/oauth2/token",      # older key files
    "https://www.googleapis.com/oauth2/v4/token",
])
def test_google_token_endpoints_are_accepted(client, creds_dir, token_uri):
    r = _upload(client, "sa.json", json.dumps({**SA, "token_uri": token_uri}).encode())
    assert r.status_code == 200, r.text


def test_a_service_account_without_token_uri_is_refused(client, creds_dir):
    """google-auth itself requires token_uri, so such a file could never work."""
    payload = {k: v for k, v in SA.items() if k != "token_uri"}
    r = _upload(client, "sa.json", json.dumps(payload).encode())
    assert r.status_code == 400, r.text
    _assert_nothing_written(creds_dir)
