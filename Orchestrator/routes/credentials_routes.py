"""Credentials file management for the onboarding wizard.

Manages JSON credential files (Google Cloud service accounts, future TLS
certs, etc.) that live in the credentials/ folder. Pattern:
- GET /onboarding/credentials → list all files with metadata (no contents)
- POST /onboarding/credentials/upload (multipart) → write to credentials/
- DELETE /onboarding/credentials/{filename} → remove + clear env if linked

Trust model: the Tailscale + LAN perimeter, like the rest of the wizard API.
There is NO loopback gate here (and loopback would prove nothing: tailscale
serve delivers every tailnet request from 127.0.0.1).

credentials/ also holds BlackBox's own registries (RESERVED_FILENAMES). They
are not credentials: they are hidden from the list and cannot be overwritten
by an upload or removed through these routes.

An upload must be a Google service-account key with Google's token endpoint
(onboarding/service_account.py), checked before anything is written: the file
becomes GOOGLE_APPLICATION_CREDENTIALS, and google-auth would otherwise act on
an external_account config that POSTs a local file (.env) to any URL.

NEVER returns or logs file contents — only filename, size, and an
is_google_service_account boolean derived from inspecting the type field.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from Orchestrator.onboarding.secrets_writer import remove_env_keys, update_env
from Orchestrator.onboarding.service_account import service_account_problem
from Orchestrator.utils import atomic_json
from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding/credentials", tags=["onboarding-credentials"])

CREDS_DIR = resolve("credentials")
# Filename hard-bound to alphanumeric + dot/dash/underscore + .json suffix.
# Defends against path traversal (../) and weird characters that could break
# the atomic-write rename.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")
# Registries other modules keep in credentials/ (custom_servers.REGISTRY_PATH,
# voice_agents.registry.REGISTRY_PATH, xai_phone.provisioning.STORE_PATH).
# An upload named like one would overwrite it AND, with the default
# set_as_google_app_creds, point GOOGLE_APPLICATION_CREDENTIALS at it; a DELETE
# would wipe it. A test keeps this set in step with those modules.
RESERVED_FILENAMES = frozenset({"custom_models.json", "voice_agents.json", "xai_phone.json"})


class CredentialFileMeta(BaseModel):
    filename: str
    size_bytes: int
    modified_at: float
    is_google_service_account: bool


class CredentialsListResponse(BaseModel):
    files: list[CredentialFileMeta]
    # Active GOOGLE_APPLICATION_CREDENTIALS env var path, if any. Frontend
    # uses this to mark which file in `files` is the live one.
    google_application_credentials: str | None


def _is_google_service_account(path: Path) -> bool:
    """Inspect file contents to confirm it's a Google service-account JSON.

    Doesn't return the content — just a boolean for UI hint. Service-account
    JSONs always carry type='service_account' + a client_email field.
    """
    try:
        with path.open() as f:
            data = json.load(f)
        # A JSON array/string/number/null is not a service account, and must
        # not raise: that 500'd every GET /onboarding/credentials.
        return (isinstance(data, dict) and data.get("type") == "service_account"
                and "client_email" in data)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False


def _file_meta(path: Path) -> CredentialFileMeta:
    stat = path.stat()
    return CredentialFileMeta(
        filename=path.name,
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        is_google_service_account=_is_google_service_account(path),
    )


@router.get("", response_model=CredentialsListResponse)
def list_credentials() -> CredentialsListResponse:
    """List all .json credential files in credentials/ + the currently-active
    GOOGLE_APPLICATION_CREDENTIALS path.
    """
    if not CREDS_DIR.exists():
        CREDS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CREDS_DIR, 0o700)
    files: list[CredentialFileMeta] = []
    for p in sorted(CREDS_DIR.glob("*.json")):
        if p.is_file() and p.name not in RESERVED_FILENAMES:
            files.append(_file_meta(p))
    return CredentialsListResponse(
        files=files,
        google_application_credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None,
    )


@router.post("/upload")
async def upload_credential(
    file: UploadFile = File(...),
    set_as_google_app_creds: bool = True,
) -> dict:
    """Upload a Google service-account key (.json). Validates the filename and
    that the JSON is a service-account key (service_account_problem), then
    optionally points GOOGLE_APPLICATION_CREDENTIALS at it.

    File is written atomically (Orchestrator/utils/atomic_json.py: 0600 temp
    file, fsync, os.replace). credentials/ dir is auto-created with chmod 0700
    if absent.
    """
    filename = (file.filename or "").strip()
    if not _FILENAME_RE.match(filename):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid filename {filename!r}. Must match {_FILENAME_RE.pattern}",
        )
    if filename in RESERVED_FILENAMES:
        raise HTTPException(
            status_code=400,
            detail=f"{filename!r} is reserved for BlackBox's own settings; rename the file",
        )
    contents = await file.read()
    if len(contents) > 64 * 1024:  # 64KB ceiling — service-account JSONs are ~2-3KB
        raise HTTPException(status_code=413, detail="File too large (64KB max)")
    try:
        parsed = json.loads(contents.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    problem = service_account_problem(parsed)
    if problem:
        raise HTTPException(status_code=400, detail=f"Invalid credential: {problem}")

    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDS_DIR, 0o700)
    target = CREDS_DIR / filename
    # Atomic write through a private, uniquely named temp sibling that is
    # 0600 from creation, fsync'ed, then os.replace()d (audit
    # O15-scheduler-admin-10): the old fixed '<name>.tmp' was briefly
    # umask-readable and followed whatever link sat at that name.
    atomic_json.write_bytes(target, contents, mode=0o600)

    is_sa = _is_google_service_account(target)
    result: dict = {
        "filename": filename,
        "size_bytes": len(contents),
        "is_service_account": is_sa,
    }

    if set_as_google_app_creds:
        update_env({"GOOGLE_APPLICATION_CREDENTIALS": str(target)})
        # Mirror the .env mutation into the running process env so callers
        # that rely on os.getenv (e.g. Google client libraries) pick it up
        # without a restart.
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(target)
        result["env_updated"] = "GOOGLE_APPLICATION_CREDENTIALS"

    # CRITICAL: never log contents — only filename, size, and the SA boolean.
    logger.info(
        "credentials uploaded: %s (%d bytes, sa=%s)",
        filename, len(contents), is_sa,
    )
    return {"ok": True, **result}


@router.delete("/{filename}")
def delete_credential(filename: str) -> dict:
    """Remove a credential file. If GOOGLE_APPLICATION_CREDENTIALS pointed
    at it, also clear the env var.
    """
    if not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail=f"Invalid filename {filename!r}")
    if filename in RESERVED_FILENAMES:
        raise HTTPException(
            status_code=400,
            detail=f"{filename!r} holds BlackBox's own settings, not a credential",
        )
    target = CREDS_DIR / filename
    if not target.exists():
        return {"ok": True, "removed": False, "reason": "not present"}
    target.unlink()
    # Clear env var if it pointed here
    current_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if current_creds == str(target):
        remove_env_keys(["GOOGLE_APPLICATION_CREDENTIALS"])
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        env_cleared = True
    else:
        env_cleared = False
    logger.info("credentials deleted: %s (env_cleared=%s)", filename, env_cleared)
    return {"ok": True, "removed": True, "env_cleared": env_cleared}
