"""
Sovereign Browser — Anthropic Computer Use Integration
Autonomous browser agent using Claude Opus 4.6 with computer_20251124
"""

from Orchestrator.browser.config import (
    DISPLAY_NUMBER, DISPLAY_WIDTH, DISPLAY_HEIGHT,
    CHROME_PATH, PROFILE_BASE, MAX_ITERATIONS, SESSION_TIMEOUT,
    ANTHROPIC_BETA_HEADER, COMPUTER_TOOL_TYPE, CU_MODEL
)
from Orchestrator.browser.display import VirtualDisplay
from Orchestrator.browser.chrome import ChromeInstance
from Orchestrator.browser.screenshot import capture_screenshot, screenshot_to_base64, save_screenshot_to_uploads
from Orchestrator.browser.actions import ActionExecutor
from Orchestrator.browser.session_manager import (
    ComputerUseSession, get_or_create_session,
    destroy_session, cleanup_inactive_sessions, strip_screenshots_from_history
)

__all__ = [
    "VirtualDisplay", "ChromeInstance", "ActionExecutor",
    "ComputerUseSession", "get_or_create_session",
    "destroy_session", "cleanup_inactive_sessions", "strip_screenshots_from_history",
    "capture_screenshot", "screenshot_to_base64", "save_screenshot_to_uploads",
    "DISPLAY_NUMBER", "DISPLAY_WIDTH", "DISPLAY_HEIGHT",
    "CHROME_PATH", "PROFILE_BASE", "MAX_ITERATIONS", "SESSION_TIMEOUT",
    "ANTHROPIC_BETA_HEADER", "COMPUTER_TOOL_TYPE", "CU_MODEL"
]
