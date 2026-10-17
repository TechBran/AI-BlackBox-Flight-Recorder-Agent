"""Chrome DevTools (CDP) exposure of the CU browsers (audit O13-computer-use-10).

Every Chrome used to launch with `--remote-debugging-port=9222` and
`--remote-allow-origins=*`:
  * the wildcard disables Chrome's CDP Origin check, so JavaScript on ANY page
    the agent visits could open ws://127.0.0.1:9222 and drive the profile
    (cookies, downloads, navigation);
  * with several CU desktops the second Chrome could not bind 9222, so CDP
    discovery (and the sandbox screenshot fallback) silently talked to the
    FIRST desktop's browser.
Now each desktop gets its own port and only the origin our own client sends
(websocket-client defaults Origin to http://<host>:<port>) is allowed.
"""
from __future__ import annotations

import io
import json

import pytest

from Orchestrator.browser import chrome as chrome_mod
from Orchestrator.browser import display as disp
from Orchestrator.browser.config import CDP_PORT


def _handle(slot: int):
    return disp.DisplayHandle(
        session_id=f"sess-{slot}", slot=slot, backend="anthropic", operator="op",
        width=1280, height=720, display_num=disp.DISPLAY_BASE + slot,
        vnc_port=disp.VNC_BASE_PORT + slot, ws_port=disp.WEBSOCKIFY_BASE_PORT + slot)


class _Proc:
    returncode = None

    def poll(self):
        return None


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """Run ChromeInstance.start with Popen/sleep/CDP discovery stubbed; return
    the argv Chrome would have been launched with."""
    launched = []

    def fake_popen(cmd, **kwargs):
        launched.append(cmd)
        return _Proc()

    monkeypatch.setattr(chrome_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chrome_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(chrome_mod, "PROFILE_BASE", tmp_path)
    monkeypatch.setattr(chrome_mod.ChromeInstance, "_discover_cdp_ws", lambda self: None)

    def _start(handle=None):
        inst = chrome_mod.ChromeInstance(operator="op")
        assert inst.start("about:blank", handle=handle) is True
        return inst, launched[-1]
    return _start


def _flag(argv, name):
    vals = [a.split("=", 1)[1] for a in argv if a.startswith(name + "=")]
    assert len(vals) == 1, f"{name} must appear exactly once: {argv}"
    return vals[0]


def test_no_wildcard_cdp_origin(launch):
    for handle in (None, _handle(0), _handle(2)):
        _, argv = launch(handle)
        assert "--remote-allow-origins=*" not in argv
        assert all(a != "--remote-allow-origins=*" for a in argv)


def test_default_display_chrome_keeps_the_legacy_port_and_only_its_own_origin(launch):
    inst, argv = launch(None)
    assert _flag(argv, "--remote-debugging-port") == str(CDP_PORT)
    assert _flag(argv, "--remote-allow-origins") == f"http://127.0.0.1:{CDP_PORT}"
    assert inst.cdp_port == CDP_PORT


def test_each_desktop_gets_its_own_cdp_port(launch):
    ports = set()
    for slot in range(disp.MAX_VIRTUAL_SESSIONS):
        h = _handle(slot)
        inst, argv = launch(h)
        port = int(_flag(argv, "--remote-debugging-port"))
        assert port == h.cdp_port == inst.cdp_port
        assert _flag(argv, "--remote-allow-origins") == f"http://127.0.0.1:{port}"
        ports.add(port)
    assert len(ports) == disp.MAX_VIRTUAL_SESSIONS, "desktops share a CDP port"
    assert CDP_PORT not in ports, "a desktop collides with the default-display Chrome"


def test_cdp_ports_do_not_collide_with_other_slot_ranges():
    for slot in range(disp.MAX_VIRTUAL_SESSIONS):
        h = _handle(slot)
        others = {disp.VNC_BASE_PORT + s for s in range(disp.MAX_VIRTUAL_SESSIONS)} | \
                 {disp.WEBSOCKIFY_BASE_PORT + s for s in range(disp.MAX_VIRTUAL_SESSIONS)}
        assert h.cdp_port not in others


def test_discovery_talks_to_the_instances_own_port(monkeypatch):
    """Two desktops' Chromes: each instance's CDP lookups must hit ITS port,
    never the shared 9222 (the cross-desktop screenshot bug)."""
    hit = []

    def fake_urlopen(url, timeout=None):
        hit.append(url)
        if url.endswith("/json/version"):
            body = {"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/x"}
        else:
            body = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/y"}]
        return io.BytesIO(json.dumps(body).encode())

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inst = chrome_mod.ChromeInstance(operator="op")
    inst.cdp_port = _handle(1).cdp_port
    inst._discover_cdp_ws()
    inst.get_page_ws_url()
    assert hit and all(f"127.0.0.1:{inst.cdp_port}/" in u for u in hit), hit


def test_screenshot_cdp_lookup_defaults_to_the_default_display_port(monkeypatch):
    from Orchestrator.browser import screenshot
    hit = []

    def fake_urlopen(url, timeout=None):
        hit.append(url)
        return io.BytesIO(b"[]")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    screenshot._get_page_ws_url()
    screenshot._get_page_ws_url(cdp_port=_handle(2).cdp_port)
    assert hit == [f"http://127.0.0.1:{CDP_PORT}/json",
                   f"http://127.0.0.1:{_handle(2).cdp_port}/json"]
