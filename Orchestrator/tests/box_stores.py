"""The box's runtime stores, and how a test session points them off the checkout.

Every store below is a module-level path the code reads at call time (or a
singleton that captured one), and its default lives in the checkout. Two layers
use the table (audit O21-python-tests-01/-02):

  * the root conftest.py points them all at one sandbox for the whole session
    (session_root), so a worker, route or tool call in ANY test writes there;
  * isolated_stores (Orchestrator/tests/conftest.py) gives a test a fresh tree
    of its own on top, under its tmp_path.

A renamed attribute fails loudly here instead of silently leaving a store on
the box. Imported by the root conftest only once the session has loaded the
Orchestrator, so this module stays stdlib-only.
"""
from __future__ import annotations

import importlib
from pathlib import Path

# The production path definitions in config.py. Nothing reads them at call
# time (readers ask models.active_tasks_db_path()), and the tasks-DB layout
# tests check them, so only isolated_stores repoints them, for its own test.
CONFIG_DEFINITIONS = [
    ("Orchestrator.config", "BLACKBOX_DATA_DIR", "data"),
    ("Orchestrator.config", "TASKS_DB_PATH", "data/tasks.db"),
]

# (module, attribute, path under the sandbox root)
STORE_PATHS = [
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
    # The wizard's .env writer: the telephony apply route generates a phone PIN
    # into it when none is configured, and the suite strips the box's PIN.
    ("Orchestrator.onboarding.secrets_writer", "ENV_FILE", ".env"),
    ("Orchestrator.onboarding.state", "STATE_FILE", ".onboarding_state.json"),
    ("Orchestrator.onboarding.state", "COMPLETE_SENTINEL", ".onboarding_complete"),
    ("Orchestrator.local_stack", "DOWNLOAD_STATE_PATH", "Manifest/local_models/downloads.json"),
    # cwd-relative in config.py. save_operator_state() writes every operator
    # held in memory, so from the serving tree a test replaced the box's file.
    ("Orchestrator.state", "OPERATOR_STATE_FILE", "Manifest/operator_state.json"),
    ("Orchestrator.state", "OPERATOR_PREFS_FILE", "Manifest/operator_preferences.json"),
    ("Orchestrator.state", "APPS_REGISTRY_FILE", "Manifest/apps_registry.json"),
    ("Orchestrator.media_index", "MEDIA_INDEX_FILE", "Manifest/media_index.json"),
    # Failed voice-transcript saves spool here (under the ledger root, cwd-relative).
    ("Orchestrator.voice_transcript_spool", "SPOOL_DIR", "Volumes/pending_transcripts"),
]

# Singletons that captured a store path (or a store's contents) at
# construction: reset so the next get_*() builds a fresh one on the new path.
SINGLETONS = [
    ("Orchestrator.device_registry.registry", "_registry"),
    ("Orchestrator.local_provider.registry", "_registry"),
    ("Orchestrator.scheduler.manager", "_manager_instance"),
    ("Orchestrator.onboarding.state", "_instance"),
]

# Set by the root conftest to the session sandbox, once it is in place.
session_root: Path | None = None


def point_at(root: Path, patch, keep=lambda value: False, definitions: bool = True) -> None:
    """Point every store at `root`, through `patch` (a pytest MonkeyPatch, so
    it is undone with it), and CONFIG_DEFINITIONS too when `definitions`. A
    store whose current value `keep` accepts (a test that already pointed it
    at a temp dir of its own) is left alone."""
    for module_name, attr, rel in (CONFIG_DEFINITIONS if definitions else []) + STORE_PATHS:
        module = importlib.import_module(module_name)
        if keep(getattr(module, attr)):
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        patch.setattr(module, attr, target)
    for module_name, attr in SINGLETONS:
        patch.setattr(importlib.import_module(module_name), attr, None)

    # The task DB is an object every importer shares (tasks, media, routes).
    from Orchestrator import models
    if not keep(models.task_db.db_path):
        patch.setattr(models.task_db, "db_path", str(root / "data" / "tasks.db"))
        models.task_db._init_db()
