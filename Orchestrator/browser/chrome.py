"""
Chrome browser process management with CDP (Chrome DevTools Protocol) support.
"""
import subprocess
import time
import signal
import json

from Orchestrator.browser.config import (
    CHROME_PATH, PROFILE_BASE, DISPLAY_NUMBER, DISPLAY_WIDTH, DISPLAY_HEIGHT,
    CDP_PORT
)
from Orchestrator.browser.display import get_display


class ChromeInstance:
    """Manages a Chrome browser instance on the virtual display with CDP."""

    def __init__(self, operator: str = "system"):
        self.operator = operator
        self.profile_dir = PROFILE_BASE / operator
        self.process = None
        self.cdp_ws_url = None  # WebSocket URL for CDP

    def start(self, url: str = "about:blank", handle=None) -> bool:
        """Launch Chrome on the session's virtual display with CDP.

        With a DisplayHandle it launches inside that session's :N at the
        backend resolution; handle=None preserves the legacy default-display
        path so nothing regresses until Task 9.4 threads handles in.
        """
        if self.is_running():
            print(f"[CHROME] Already running for operator {self.operator}")
            return True

        # Ensure profile directory exists
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        if handle is not None:
            env = handle.get_env()
            win_w, win_h = handle.width, handle.height
        else:
            display = get_display()
            env = display.get_env()
            win_w, win_h = DISPLAY_WIDTH, DISPLAY_HEIGHT

        cmd = [
            CHROME_PATH,
            f"--user-data-dir={self.profile_dir}",
            f"--window-size={win_w},{win_h}",
            "--window-position=0,0",
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--disable-features=TranslateUI",
            "--disable-background-networking",
            "--disable-sync",
            "--metrics-recording-only",
            "--disable-default-apps",
            # SECURITY NOTE: --no-sandbox is required when running Chrome as root or in
            # headless environments. The domain blocklist in config.py provides URL-level
            # protection against the CU agent visiting malicious sites.
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-plugins",
            "--disable-software-rasterizer",
            "--js-flags=--max-old-space-size=256",
            "--renderer-process-limit=2",
            "--force-device-scale-factor=1",
            url
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)
            )
            time.sleep(3)  # Give Chrome time to start, render, and open CDP

            if self.process.poll() is not None:
                print(f"[CHROME] Failed to start (exit code {self.process.returncode})")
                return False

            # Discover CDP WebSocket URL
            self._discover_cdp_ws()

            print(f"[CHROME] Started for operator '{self.operator}' on :{DISPLAY_NUMBER} → {url}")
            if self.cdp_ws_url:
                print(f"[CHROME] CDP WebSocket: {self.cdp_ws_url}")
            return True
        except FileNotFoundError:
            print(f"[CHROME] Chrome not found at {CHROME_PATH}")
            return False
        except Exception as e:
            print(f"[CHROME] Failed to start: {e}")
            return False

    def _discover_cdp_ws(self):
        """Discover the CDP WebSocket URL from Chrome's debug endpoint."""
        import urllib.request
        for attempt in range(5):
            try:
                req = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2)
                data = json.loads(req.read().decode())
                self.cdp_ws_url = data.get("webSocketDebuggerUrl")
                if self.cdp_ws_url:
                    return
            except Exception:
                time.sleep(0.5)
        print(f"[CHROME] Warning: Could not discover CDP WebSocket URL after 5 attempts")

    def get_page_ws_url(self) -> str:
        """Get the CDP WebSocket URL for the first browser page/tab."""
        import urllib.request
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
            pages = json.loads(req.read().decode())
            for page in pages:
                if page.get("type") == "page":
                    return page.get("webSocketDebuggerUrl", "")
        except Exception as e:
            print(f"[CHROME] Failed to get page WS URL: {e}")
        return ""

    def stop(self):
        """Terminate Chrome gracefully."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
            print(f"[CHROME] Stopped for operator '{self.operator}'")

    def is_running(self) -> bool:
        """Check if Chrome process is alive."""
        if self.process and self.process.poll() is None:
            return True
        return False

    def navigate(self, url: str):
        """Navigate Chrome to a new URL using xdotool."""
        display = get_display()
        env = display.get_env()
        # Ctrl+L to focus address bar, type URL, press Enter
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+l"],
                       env=env, capture_output=True)
        time.sleep(0.3)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+a"],
                       env=env, capture_output=True)
        time.sleep(0.1)
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "12", url],
                       env=env, capture_output=True)
        time.sleep(0.1)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"],
                       env=env, capture_output=True)
        time.sleep(1)  # Wait for navigation
