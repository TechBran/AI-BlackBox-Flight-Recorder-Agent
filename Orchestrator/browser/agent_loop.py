"""
Sovereign Browser agent loop — core brain for computer use.
Sends screenshots to Claude Opus 4.6, receives actions, executes them, loops.
"""
import asyncio
import time
import io
import httpx
from datetime import datetime
from typing import Optional

from Orchestrator.browser.config import (
    ANTHROPIC_API_KEY, ANTHROPIC_API_URL, ANTHROPIC_BETA_HEADER,
    COMPUTER_TOOL_TYPE, CU_MODEL, MAX_ITERATIONS, SESSION_TIMEOUT,
    DISPLAY_WIDTH, DISPLAY_HEIGHT, is_domain_allowed
)
from Orchestrator.browser.display import ensure_display_running, get_display
from Orchestrator.browser.chrome import ChromeInstance
from Orchestrator.browser.screenshot import (
    capture_screenshot, screenshot_to_base64, resize_screenshot,
    save_screenshot_to_uploads
)
from Orchestrator.browser.actions import ActionExecutor


DEFAULT_SYSTEM_PROMPT = """You are a browser automation agent. You control a Chrome browser to accomplish tasks.
You can see the browser through screenshots and interact using mouse clicks, keyboard input, and scrolling.
Be methodical: observe the screen, plan your action, execute it, then observe the result.
When the task is complete, respond with a clear summary of what was accomplished."""


class BrowserSession:
    """A single browser automation session using Anthropic Computer Use."""

    def __init__(self, task_id: str, operator: str = "system"):
        self.task_id = task_id
        self.operator = operator
        self.chrome = ChromeInstance(operator=operator)
        self.actions = ActionExecutor()
        self.screenshots = []  # List of saved screenshot URLs
        self.step_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def start(self, url: str = "about:blank") -> bool:
        """Start virtual display and Chrome."""
        if not ensure_display_running():
            return False
        return self.chrome.start(url)

    def stop(self):
        """Clean up Chrome (display persists for reuse)."""
        self.chrome.stop()

    async def run(self, prompt: str, url: Optional[str] = None,
                  system_prompt: Optional[str] = None) -> dict:
        """
        Run the agent loop: screenshot → API → actions → repeat.
        Returns {success, result_text, screenshots, final_screenshot, steps, tokens}.
        """
        system = system_prompt or DEFAULT_SYSTEM_PROMPT
        # Inject at run time (not import time) — the loop exposes only the
        # computer tool, so the model cannot fetch the time itself.
        system = f"{system}\n\nCurrent date/time: {datetime.now().isoformat(timespec='seconds')}"

        # Start browser
        start_url = url or "about:blank"
        if url and not is_domain_allowed(url):
            return {
                "success": False,
                "result_text": f"Domain blocked by security policy: {url}",
                "screenshots": [],
                "final_screenshot": None,
                "steps": 0,
                "tokens": {"input": 0, "output": 0}
            }

        if not self.start(start_url):
            return {
                "success": False,
                "result_text": "Failed to start browser session",
                "screenshots": [],
                "final_screenshot": None,
                "steps": 0,
                "tokens": {"input": 0, "output": 0}
            }

        try:
            # Wait for initial page load
            await asyncio.sleep(2)

            # Capture initial screenshot
            initial_png = capture_screenshot()
            initial_b64 = screenshot_to_base64(initial_png)
            initial_url = save_screenshot_to_uploads(initial_png, self.task_id, 0)
            self.screenshots.append(initial_url)

            # Build initial messages
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": initial_b64
                            }
                        }
                    ]
                }
            ]

            # Tool definition for computer use
            tools = [
                {
                    "type": self._tool_type(),
                    "name": "computer",
                    "display_width_px": DISPLAY_WIDTH,
                    "display_height_px": DISPLAY_HEIGHT,
                }
            ]

            result_text = ""

            # Agent loop
            for iteration in range(MAX_ITERATIONS):
                self.step_count = iteration + 1
                print(f"[BROWSER] Step {self.step_count}/{MAX_ITERATIONS} for task {self.task_id}")

                # Call Anthropic API
                response = await self._call_api(system, messages, tools)

                if not response:
                    result_text = "API call failed"
                    break

                # Track tokens
                usage = response.get("usage", {})
                self.total_input_tokens += usage.get("input_tokens", 0)
                self.total_output_tokens += usage.get("output_tokens", 0)

                # Get assistant message
                assistant_content = response.get("content", [])
                stop_reason = response.get("stop_reason", "")

                # Add assistant message to conversation
                messages.append({"role": "assistant", "content": assistant_content})

                # Check if done
                if stop_reason != "tool_use":
                    # Extract final text
                    for block in assistant_content:
                        if block.get("type") == "text":
                            result_text = block.get("text", "")
                    break

                # Process tool calls
                tool_results = []
                for block in assistant_content:
                    if block.get("type") == "tool_use" and block.get("name") == "computer":
                        tool_result = await self._execute_tool_call(block)
                        tool_results.append(tool_result)

                # Add tool results to messages
                if tool_results:
                    messages.append({
                        "role": "user",
                        "content": tool_results
                    })

            final_screenshot = self.screenshots[-1] if self.screenshots else None

            return {
                "success": True,
                "result_text": result_text,
                "screenshots": self.screenshots,
                "final_screenshot": final_screenshot,
                "steps": self.step_count,
                "tokens": {
                    "input": self.total_input_tokens,
                    "output": self.total_output_tokens
                }
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "result_text": f"Session timed out after {SESSION_TIMEOUT}s",
                "screenshots": self.screenshots,
                "final_screenshot": self.screenshots[-1] if self.screenshots else None,
                "steps": self.step_count,
                "tokens": {"input": self.total_input_tokens, "output": self.total_output_tokens}
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "result_text": f"Session error: {e}",
                "screenshots": self.screenshots,
                "final_screenshot": self.screenshots[-1] if self.screenshots else None,
                "steps": self.step_count,
                "tokens": {"input": self.total_input_tokens, "output": self.total_output_tokens}
            }
        finally:
            self.stop()

    def _tool_type(self) -> str:
        """Return the correct tool type string."""
        return COMPUTER_TOOL_TYPE

    async def _call_api(self, system: str, messages: list, tools: list) -> dict:
        """Make a raw httpx call to the Anthropic messages API."""
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": ANTHROPIC_BETA_HEADER,
            "content-type": "application/json"
        }

        body = {
            "model": CU_MODEL,
            "max_tokens": 4096,
            "system": system,
            "tools": tools,
            "messages": messages
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)

            if resp.status_code != 200:
                print(f"[BROWSER] API error {resp.status_code}: {resp.text[:500]}")
                return None

            return resp.json()
        except Exception as e:
            print(f"[BROWSER] API call failed: {e}")
            return None

    async def _execute_tool_call(self, tool_block: dict) -> dict:
        """Execute a computer use tool call and return the tool_result with screenshot."""
        tool_use_id = tool_block.get("id")
        input_data = tool_block.get("input", {})
        action = input_data.get("action", "")

        print(f"[BROWSER]   Action: {action} | Params: {_safe_params(input_data)}")

        # Execute the action — strip 'action' key to avoid duplicate kwarg
        params = {k: v for k, v in input_data.items() if k != "action"}
        result = self.actions.execute(action, **params)

        # Wait briefly for UI to update after action
        if action not in ("screenshot", "wait", "zoom"):
            await asyncio.sleep(0.5)

        # Capture screenshot after action
        try:
            png_bytes = capture_screenshot()

            # Handle zoom: crop the region
            if action == "zoom":
                png_bytes = self._crop_region(png_bytes, input_data.get("region"))

            png_b64 = screenshot_to_base64(png_bytes)
            screenshot_url = save_screenshot_to_uploads(png_bytes, self.task_id, self.step_count)
            self.screenshots.append(screenshot_url)

            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": png_b64
                        }
                    }
                ]
            }
        except Exception as e:
            print(f"[BROWSER]   Screenshot failed: {e}")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [{"type": "text", "text": f"Screenshot failed: {e}"}],
                "is_error": True
            }

    def _crop_region(self, png_bytes: bytes, region) -> bytes:
        """Crop a region from the screenshot for zoom."""
        if not region or len(region) != 4:
            return png_bytes
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            x0, y0, x1, y1 = [int(v) for v in region]
            cropped = img.crop((x0, y0, x1, y1))
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            return png_bytes


def _safe_params(data: dict) -> str:
    """Format params for logging without flooding with text content."""
    safe = {}
    for k, v in data.items():
        if k == "action":
            continue
        if k == "text" and isinstance(v, str) and len(v) > 50:
            safe[k] = v[:50] + "..."
        else:
            safe[k] = v
    return str(safe) if safe else ""
