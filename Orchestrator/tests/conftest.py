"""Shared fixtures for the Orchestrator suite.

app_client is THE way a test talks to the real app (audit O21-python-tests-02,
-09): a TestClient over Orchestrator.app with

  * BLACKBOX_TESTING=1, so the lifespan skips every startup/shutdown hook that
    reaches outside the process (Orchestrator/utils/testing.py; the root
    conftest sets it for the whole session too),
  * the ToolVault embedding sync mocked (it would embed through a provider),
  * every store a route can write pointed into tmp_path (isolated_stores).

    def test_health(app_client):
        assert app_client.get("/toolvault/health").status_code == 200

A test that also needs the embedding-sync mock asks for toolvault_embed_sync;
pytest hands both fixtures the same instance.
"""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# (module, attribute, path under the tmp box root). Each attribute is the
# module-level path a store reads at call time; a renamed attribute fails the
# fixture loudly instead of silently leaving the store on the box.
_STORE_PATHS = [
    ("Orchestrator.config", "BLACKBOX_DATA_DIR", "data"),
    ("Orchestrator.config", "TASKS_DB_PATH", "data/tasks.db"),
    ("Orchestrator.models", "TASKS_DB_PATH", "data/tasks.db"),
    ("Orchestrator.contacts", "CONTACTS_DIR", "Contacts"),
    ("Orchestrator.contacts", "CONTACTS_FILE", "Contacts/contacts.json"),
    ("Orchestrator.device_registry.registry", "DEVICES_FILE", "devices/devices.json"),
    ("Orchestrator.device_registry.registry", "_LEGACY_DEVICES_FILE", "devices/legacy-devices.json"),
    ("Orchestrator.device_registry.pairing", "PAIRING_PATH", "credentials/device_pairing.json"),
    ("Orchestrator.routes.pairing_routes", "PAIRED_DEVICES_FILE", "Manifest/paired_devices.json"),
    ("Orchestrator.local_provider.registry", "STORE_FILE", "local_provider/local_devices.json"),
    ("Orchestrator.scheduler.manager", "DB_PATH", "cron_jobs.db"),
    ("Orchestrator.sms.message_store", "DB_PATH", "Manifest/sms_messages.db"),
    ("Orchestrator.notifications.subscriptions", "DEFAULT_SUBS_FILE",
     "Manifest/device_notification_subs.json"),
    ("Orchestrator.voice_agents.registry", "REGISTRY_PATH", "credentials/voice_agents.json"),
    ("Orchestrator.onboarding.custom_servers", "REGISTRY_PATH", "credentials/custom_models.json"),
    ("Orchestrator.phone.pin_gate", "LOCKOUT_PATH", "credentials/phone_pin_lockout.json"),
]

# Singletons that captured a store path at construction: reset so the next
# get_*() builds a fresh one on the tmp path.
_SINGLETONS = [
    ("Orchestrator.device_registry.registry", "_registry"),
    ("Orchestrator.local_provider.registry", "_registry"),
    ("Orchestrator.scheduler.manager", "_manager_instance"),
]


# cwd-relative in config.py: Volumes/, Manifest/, Archive/, Portal/uploads,
# Portal/artifacts.
_RUNTIME_DIRS = ["Volumes", "Manifest", "Archive", "Portal/uploads", "Portal/artifacts"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _already_isolated(value) -> bool:
    """A store the test itself already pointed into a temp dir (its own
    autouse fixture ran first) is left alone. Every box default lives under the
    checkout, which may itself sit in a temp dir."""
    try:
        path = Path(str(value)).resolve()
    except (OSError, ValueError):
        return False
    if path.is_relative_to(_REPO_ROOT):
        return False
    return path.is_relative_to(Path(tempfile.gettempdir()).resolve())


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Point the box's runtime stores at a fresh tree under tmp_path.

    Covers the task DB (TASKS_DB_PATH and the open task_db singleton),
    contacts, the device registry and pairing stores, the local-provider
    registry, cron, SMS, notification subscriptions, voice-agent presets,
    custom model servers and the phone PIN lockout. The ledger paths
    (Volumes/, Manifest/, Portal/uploads, config.ini) are cwd-relative in
    config.py, so the test runs from the tmp root, which starts with a copy of
    this checkout's config.ini.
    """
    root = tmp_path / "box"
    root.mkdir()
    for module_name, attr, rel in _STORE_PATHS:
        module = importlib.import_module(module_name)
        if _already_isolated(getattr(module, attr)):
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, attr, target)
    for module_name, attr in _SINGLETONS:
        monkeypatch.setattr(importlib.import_module(module_name), attr, None)

    from Orchestrator import models
    if not _already_isolated(models.task_db.db_path):
        monkeypatch.setattr(models.task_db, "db_path", str(root / "data" / "tasks.db"))
        models.task_db._init_db()

    # The runtime dirs a box always has, so generated media and Manifest
    # writes land here instead of in the checkout.
    for rel in _RUNTIME_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    config_ini = Path(os.getcwd()) / "config.ini"
    if config_ini.is_file():
        shutil.copy2(config_ini, root / "config.ini")
    monkeypatch.chdir(root)
    return SimpleNamespace(root=root, tasks_db=Path(models.task_db.db_path))


@pytest.fixture
def toolvault_embed_sync():
    """The ToolVault embedding sync, mocked (it embeds through a provider)."""
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as mock:
        mock.return_value = {"x": {"vector": [0.1]}}
        yield mock


@pytest.fixture
def app_client(isolated_stores, toolvault_embed_sync, monkeypatch):
    """TestClient over the real app, lifespan entered, side-effect hooks off."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    from Orchestrator.app import app
    with TestClient(app) as client:
        yield client
