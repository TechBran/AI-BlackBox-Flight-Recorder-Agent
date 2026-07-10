"""OpenAI Computer Use Agent configuration.

2026-06-11: OpenAI deprecated the dedicated, access-gated computer-use-preview
model. Computer use is now a built-in `{"type": "computer"}` tool on gpt-5.5
(Responses API) — available to any org with gpt-5.5 access, no CUA waitlist.
The tool declaration carries NO display/environment fields; the model replies
with pixel coordinates in the space of the screenshot you send it.
Docs: https://developers.openai.com/api/docs/guides/tools-computer-use
"""

from Orchestrator.config import CU_MAX_ITERATIONS

# Model — the default must match Orchestrator.config CU_MODEL_FILTERS["openai"]
# so resolve_backend() routes it to this driver.
OPENAI_CU_MODEL_DEFAULT = "gpt-5.5"
OPENAI_CUA_MODEL = OPENAI_CU_MODEL_DEFAULT  # legacy alias (pre-Task-13 name)

# Screenshot size sent to the model — 1280x720 deliberately shares the
# Anthropic screenshot pipeline (capture_screenshot resizes to CU 1280x720)
# AND the ActionExecutor default anthropic-1280 coordinate space: the model
# sees screenshots at this size and replies with pixel coordinates in the
# same space, so no new coordinate conversion is needed. (The new computer
# tool no longer DECLARES a display — coordinates simply follow the
# screenshot's own pixel space.)
OPENAI_CU_WIDTH = 1280
OPENAI_CU_HEIGHT = 720

# Kept for reference: the legacy preview tool took an environment field; the
# new bare `computer` tool does not.
OPENAI_CU_ENVIRONMENT = "browser"

# Agent loop limits (wall-clock cap matches Gemini's 1800s). Single source:
# Orchestrator/config.py [computer_use] max_iterations, so all 3 CU backends share
# ONE cap (was a hardcoded 50 — the ceiling a GPT-fallback run actually hit).
MAX_ITERATIONS = CU_MAX_ITERATIONS
SESSION_TIMEOUT = 300
MAX_WALL_CLOCK = 1800

# Tool type — bare "computer" (the legacy "computer_use_preview" tool type
# remains only for orgs still on the deprecated preview model).
COMPUTER_USE_TOOL_TYPE = "computer"
