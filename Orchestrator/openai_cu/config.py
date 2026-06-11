"""OpenAI Computer Use Agent configuration."""

# Model — the default must match Orchestrator.config CU_MODEL_FILTERS["openai"]
# (r"computer-use-preview") so resolve_backend() routes it to this driver.
OPENAI_CU_MODEL_DEFAULT = "computer-use-preview"
OPENAI_CUA_MODEL = OPENAI_CU_MODEL_DEFAULT  # legacy alias (pre-Task-13 name)

# Declared display — 1280x720 deliberately shares the Anthropic screenshot
# pipeline (capture_screenshot resizes to CU 1280x720) AND the ActionExecutor
# default anthropic-1280 coordinate space: the model sees screenshots at this
# size and replies with pixel coordinates in the same space, so no new
# coordinate conversion is needed.
OPENAI_CU_WIDTH = 1280
OPENAI_CU_HEIGHT = 720

# Supported environments per the Responses API; we only run "browser" today
# (local desktop driven through the shared browser/display stack).
OPENAI_CUA_ENVIRONMENTS = ["browser", "mac", "windows", "ubuntu"]
OPENAI_CU_ENVIRONMENT = "browser"

# Agent loop limits (wall-clock cap matches Gemini's 1800s)
MAX_ITERATIONS = 50
SESSION_TIMEOUT = 300
MAX_WALL_CLOCK = 1800

# Tool type version
COMPUTER_USE_TOOL_TYPE = "computer_use_preview"
