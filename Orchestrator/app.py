#!/usr/bin/env python3
"""
Orchestrator/app.py - BlackBox Orchestrator Entry Point

This is the modularized entry point that imports from extracted modules.
The original 7,878-line monolithic app.py has been refactored into:

Core Modules:
  - config.py          Configuration, paths, API keys, constants
  - models.py          Pydantic models, Task/AgentSession dataclasses
  - state.py           Operator state management, preferences, app registry
  - volume.py          Volume/file operations, snapshot utilities
  - monitoring.py      Health monitoring, drift detection, embeddings
  - artifacts.py       Artifact generation (PDF, DOCX, CSV)
  - fossils.py         Fossil retrieval, semantic search, hybrid retrieval
  - checkpoint.py      Checkpoint creation, minting, FastAPI app definition
  - startup.py         App initialization, static files mounting
  - tasks.py           Background task processing, media generation

Routes:
  - routes/task_routes.py   Task status endpoints
  - routes/admin_routes.py  Admin, health, cleanup endpoints
  - routes/tts_routes.py    Text-to-speech endpoints
  - routes/chat_routes.py   Chat, LLM interaction endpoints
  - routes/agent_routes.py  Claude Code agent endpoints

The FastAPI `app` instance is created in checkpoint.py and routes are
registered by each module via decorators.

Usage:
    uvicorn Orchestrator.app:app --host 0.0.0.0 --port 9091
"""

# =============================================================================
# Module Imports - Order matters for dependency resolution
# =============================================================================

# Configuration and constants (no dependencies)
from Orchestrator.config import *

# Centralized logging (initialize early, before other modules load)
from Orchestrator.logging_config import setup_logging
setup_logging()

# Data models (depends on config)
from Orchestrator.models import *

# State management (depends on config, models)
from Orchestrator.state import *

# Volume operations (depends on config)
from Orchestrator.volume import *

# Monitoring and health (depends on config, volume)
from Orchestrator.monitoring import *

# Artifact generation (depends on config)
from Orchestrator.artifacts import *

# Fossil retrieval (depends on config, volume, monitoring)
from Orchestrator.fossils import *

# Checkpoint and FastAPI app (depends on config, fossils, monitoring, state, volume)
# NOTE: This module defines `app = FastAPI()` which is exported here
from Orchestrator.checkpoint import *

# Startup initialization (depends on checkpoint.app)
from Orchestrator.startup import *

# Background tasks (depends on most modules)
from Orchestrator.tasks import *

# =============================================================================
# Route Imports - Register endpoints on the FastAPI app
# =============================================================================

from Orchestrator.routes.task_routes import *
from Orchestrator.routes.admin_routes import *
from Orchestrator.routes.tts_routes import *
from Orchestrator.routes.persona_routes import *
from Orchestrator.routes.chat_routes import *
from Orchestrator.routes.agent_routes import *
from Orchestrator.routes.realtime_routes import *
from Orchestrator.routes.gemini_live_routes import *
from Orchestrator.routes.stt_ws_routes import *
from Orchestrator.routes.gemini_agent_routes import *
from Orchestrator.routes.grok_live_routes import *
from Orchestrator.routes.phone_routes import *
from Orchestrator.routes.twilio_routes import *
from Orchestrator.routes.cellular_routes import *
from Orchestrator.routes.asterisk_routes import *
from Orchestrator.routes.cron_routes import *
from Orchestrator.routes.notification_routes import *
from Orchestrator.routes.browser_routes import *
from Orchestrator.routes.internet_routes import *
from Orchestrator.routes.gmail_routes import *
from Orchestrator.routes.local_routes import *
from Orchestrator.routes.toolvault_routes import *
from Orchestrator.routes.elevenlabs_routes import *
from Orchestrator.routes.xai_voice_routes import *

from Orchestrator.routes.device_routes import router as device_router
app.include_router(device_router)

from Orchestrator.routes.adb_routes import router as adb_router
app.include_router(adb_router)

from Orchestrator.routes.gemini_cu_routes import router as gemini_cu_router
app.include_router(gemini_cu_router)

from Orchestrator.routes.sms_routes import router as sms_router
app.include_router(sms_router)

from Orchestrator.routes.contacts_routes import router as contacts_router
app.include_router(contacts_router)

from Orchestrator.routes.cli_agent_routes import router as cli_agent_router
app.include_router(cli_agent_router)

from Orchestrator.routes.pairing_routes import router as pairing_router
app.include_router(pairing_router)

from Orchestrator.routes.update_routes import router as update_router
app.include_router(update_router)

from Orchestrator.routes.onboarding_routes import router as onboarding_router
app.include_router(onboarding_router)

from Orchestrator.routes.embeddings_routes import router as embeddings_router
app.include_router(embeddings_router)

from Orchestrator.routes.rerank_routes import router as rerank_router
app.include_router(rerank_router)

from Orchestrator.routes.local_models_routes import router as local_models_status_router
app.include_router(local_models_status_router)

from Orchestrator.routes.local_stack_routes import router as local_stack_router
app.include_router(local_stack_router)

from Orchestrator.routes.credentials_routes import router as credentials_router
app.include_router(credentials_router)

from Orchestrator.routes.mcp_routes import router as mcp_router
app.include_router(mcp_router)

from Orchestrator.routes.xai_phone_routes import router as xai_phone_router
app.include_router(xai_phone_router)

from Orchestrator.routes.voice_agent_routes import router as voice_agent_router
app.include_router(voice_agent_router)

# =============================================================================
# First-run middleware: redirect /ui index → /onboarding/ when wizard incomplete
# =============================================================================
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse
from Orchestrator.onboarding import get_state

_onboarding_state = get_state()


class FirstRunMiddleware(BaseHTTPMiddleware):
    """Redirect /ui (Portal index) to /onboarding/ when onboarding is incomplete.

    Only intercepts the three Portal-index paths (/ui, /ui/, /ui/index.html).
    All other paths — including /ui/uploads/*, /api/*, /onboarding/* itself,
    and websocket endpoints — pass through untouched.

    The .onboarding_complete sentinel (written by POST /onboarding/complete)
    is read on every request via OnboardingState.is_complete() — no caching,
    so completion takes effect immediately without restart.
    """
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Update this tuple if Portal's index document is renamed (e.g. main.html, app.html).
        if path in ("/ui", "/ui/", "/ui/index.html") and not _onboarding_state.is_complete():
            return RedirectResponse(
                url="/onboarding/",
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)


app.add_middleware(FirstRunMiddleware)


# =============================================================================
# Onboarding wizard static files mount
# =============================================================================
# CRITICAL: this mount MUST be registered AFTER app.include_router(onboarding_router)
# above. If StaticFiles is mounted first, it shadows the API routes — GET
# /onboarding/state would return index.html instead of JSON. The HARD-FAIL test
# in T2.1.1 Step 5 verifies this ordering. Don't reorder these blocks.
from fastapi.staticfiles import StaticFiles
from Orchestrator.utils.paths import resolve as _resolve_path


class _NoCacheStaticFiles(StaticFiles):
    """Forces no-cache on all responses. Caught 2026-05-16 on Brandon's
    MSO2 Ultra: Tauri's WebKitGTK aggressively caches the onboarding
    wizard's HTML/JS/CSS in ~/.local/share/com.blackbox.setup/WebKitCache/
    — survives PC reboots + Tauri shell restarts. Customer would see stale
    wizard UI after any update. no-store defeats the cache; multi-header
    belt-and-suspenders for WebKitGTK's historical aggressiveness."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount(
    "/onboarding",
    _NoCacheStaticFiles(directory=str(_resolve_path("Portal", "onboarding")), html=True),
    name="onboarding",
)

# noVNC live-view assets (M9 Task 9.5). Served from the system apt path so the
# per-session viewer page can `import RFB from '/cu/novnc/core/rfb.js'`. Mounted
# AFTER the router includes so it never shadows the /cu/view API routes; the
# whole mount is skipped when novnc isn't installed (SHOULD_HAVE, not MUST) and
# cu_view() then returns the install-notice page instead.
import os as _os
_novnc_dir = "/usr/share/novnc"
if _os.path.isdir(_novnc_dir):
    app.mount("/cu/novnc", StaticFiles(directory=_novnc_dir), name="cu-novnc")

# =============================================================================
# Entry point verification
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"Starting BlackBox Orchestrator with {len(app.routes)} routes...")
    uvicorn.run(app, host="0.0.0.0", port=9091)
