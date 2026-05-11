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
from Orchestrator.routes.chat_routes import *
from Orchestrator.routes.agent_routes import *
from Orchestrator.routes.realtime_routes import *
from Orchestrator.routes.gemini_live_routes import *
from Orchestrator.routes.gemini_agent_routes import *
from Orchestrator.routes.grok_live_routes import *
from Orchestrator.routes.phone_routes import *
from Orchestrator.routes.twilio_routes import *
from Orchestrator.routes.cellular_routes import *
from Orchestrator.routes.asterisk_routes import *
from Orchestrator.routes.cron_routes import *
from Orchestrator.routes.browser_routes import *
from Orchestrator.routes.internet_routes import *
from Orchestrator.routes.gmail_routes import *

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

from Orchestrator.routes.onboarding_routes import router as onboarding_router
app.include_router(onboarding_router)

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
        if path in ("/ui", "/ui/", "/ui/index.html") and not _onboarding_state.is_complete():
            return RedirectResponse(url="/onboarding/", status_code=307)
        return await call_next(request)


app.add_middleware(FirstRunMiddleware)

# =============================================================================
# Entry point verification
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"Starting BlackBox Orchestrator with {len(app.routes)} routes...")
    uvicorn.run(app, host="0.0.0.0", port=9091)
