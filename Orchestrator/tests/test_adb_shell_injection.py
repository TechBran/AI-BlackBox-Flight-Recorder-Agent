"""ADB device-shell command injection (audit O14-cli-agents-devices-01) and the
`adb devices` header false-positive (O14-cli-agents-devices-06).

`adb -s <serial> shell <string>` hands the string to the PHONE's /system/bin/sh,
which evaluates it with full expansion. type_text / key_event / open_app are fed
by POST /adb/* and — more importantly — by the Gemini CU agent loop, where the
text / keys / app_name are model-generated, so on-screen prompt injection
became shell execution as the adb `shell` uid.

These tests capture the exact string sent to `adb shell` and then run it in a
REAL local POSIX shell with stub `input`/`am`/`monkey` functions (Android's
mksh honours POSIX single quotes the same way): every hostile value must arrive
as ONE literal argument, and no canary command may ever run.
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

import pytest

from Orchestrator.adb import commands as adb_commands
from Orchestrator.adb.commands import ADBCommands

_HOSTILE = [
    "'; touch {canary}; echo '",
    "$(touch {canary})",
    "`touch {canary}`",
    "hello\ntouch {canary}",
    '\\"; touch {canary}; echo \\"',
    '"; touch {canary}; echo "',
    "a && touch {canary}",
    "x | touch {canary}",
    "${{IFS}}touch${{IFS}}{canary}",
    # Space-free payloads: type_text rewrites spaces to %s, so a real attacker
    # uses ${IFS}. Each of these created the canary on the pre-fix code.
    "$(touch${{IFS}}{canary})",
    "`touch${{IFS}}{canary}`",
    '\\";touch${{IFS}}{canary};#',
]


class _FakeADB:
    def __init__(self):
        self.calls = []

    async def _run_adb(self, *args, timeout=10):
        self.calls.append(args)
        return 0, "", ""


@pytest.fixture
def cmds(monkeypatch):
    fake = _FakeADB()
    monkeypatch.setattr(adb_commands, "get_adb_manager", lambda: fake)
    c = ADBCommands("phone-1")
    monkeypatch.setattr(c, "_get_serial", lambda: "100.64.0.9:5555")
    return c, fake


def _shell_strings(fake: _FakeADB) -> list[str]:
    out = []
    for args in fake.calls:
        assert args[:3] == ("-s", "100.64.0.9:5555", "shell"), args
        assert len(args) == 4, "the device command must be ONE quoted string"
        out.append(args[3])
    return out


def _run_like_device(cmd: str, tmp_path: Path) -> list[str]:
    """Evaluate `cmd` the way the phone's sh would, with the Android tools
    stubbed to print one argument per line. Returns those arguments."""
    stubs = ('input() { printf "%s\\n" "$@"; }; am() { printf "%s\\n" "$@"; }; '
             'monkey() { printf "%s\\n" "$@"; }; ')
    res = subprocess.run(["sh", "-c", stubs + cmd], capture_output=True, text=True,
                         cwd=tmp_path, timeout=10)
    assert res.returncode == 0, res.stderr
    return res.stdout.split("\n")[:-1]


def _run(coro):
    return asyncio.run(coro)


# --- type_text -----------------------------------------------------------------


@pytest.mark.parametrize("template", _HOSTILE)
def test_type_text_hostile_value_is_one_literal_argument(cmds, tmp_path, template):
    c, fake = cmds
    canary = tmp_path / "pwned"
    text = template.format(canary=canary)
    result = _run(c.type_text(text))
    assert result["success"] is True
    (cmd,) = _shell_strings(fake)
    try:
        argv = _run_like_device(cmd, tmp_path)  # one printed line per argument
    finally:
        assert not canary.exists(), f"device-side shell executed: {cmd!r}"
    assert argv[0] == "text"
    assert "\n".join(argv[1:]) == text.replace(" ", "%s")
    assert shlex.split(cmd) == ["input", "text", text.replace(" ", "%s")]


def test_type_text_plain_text_still_types(cmds, tmp_path):
    c, fake = cmds
    _run(c.type_text("hello world it's 50% \"off\""))
    (cmd,) = _shell_strings(fake)
    assert shlex.split(cmd) == ["input", "text", "hello%sworld%sit's%s50%%s\"off\""]


# --- key_event ----------------------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("KEYCODE_HOME", "KEYCODE_HOME"),
    ("KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_UP"),
    ("66", "66"),
    (66, "66"),
    ("enter", "KEYCODE_ENTER"),
    ("Back", "KEYCODE_BACK"),
])
def test_key_event_accepts_keycodes(cmds, given, expected):
    c, fake = cmds
    result = _run(c.key_event(given))
    assert result["success"] is True
    (cmd,) = _shell_strings(fake)
    assert shlex.split(cmd) == ["input", "keyevent", expected]


@pytest.mark.parametrize("given", [
    "KEYCODE_HOME; reboot", "$(reboot)", "`reboot`", "3 && reboot", "KEYCODE_HOME\nreboot",
    "ctrl+a", "", "   ", "KEYCODE_", -1, True, None, 3.5, "KEYCODE_home'x",
])
def test_key_event_rejects_anything_else_without_touching_the_device(cmds, given):
    c, fake = cmds
    result = _run(c.key_event(given))
    assert result["success"] is False
    assert result["invalid_input"] is True
    assert "error" in result
    assert fake.calls == [], "a rejected keycode must never reach adb"


def test_go_home_and_back_still_work(cmds):
    c, fake = cmds
    _run(c.go_home())
    _run(c.go_back())
    assert [shlex.split(s) for s in _shell_strings(fake)] == [
        ["input", "keyevent", "KEYCODE_HOME"], ["input", "keyevent", "KEYCODE_BACK"]]


# --- open_app -----------------------------------------------------------------


def test_open_app_launcher_argv(cmds, tmp_path):
    c, fake = cmds
    result = _run(c.open_app("com.android.chrome"))
    assert result["success"] is True
    (cmd,) = _shell_strings(fake)
    assert shlex.split(cmd) == ["monkey", "-p", "com.android.chrome", "-c",
                                "android.intent.category.LAUNCHER", "1"]


@pytest.mark.parametrize("activity", [
    ".MainActivity", "com.google.android.apps.chrome.Main", "com.foo.Outer$Inner"])
def test_open_app_with_activity_argv(cmds, tmp_path, activity):
    c, fake = cmds
    result = _run(c.open_app("com.android.chrome", activity))
    assert result["success"] is True
    (cmd,) = _shell_strings(fake)
    assert shlex.split(cmd) == ["am", "start", "-n", f"com.android.chrome/{activity}"]
    # `$` in an inner-class name must reach `am` literally, not expand.
    assert _run_like_device(cmd, tmp_path) == ["start", "-n", f"com.android.chrome/{activity}"]


@pytest.mark.parametrize("package", [
    "com.x; reboot", "$(reboot)", "`reboot`", "com.android.chrome\nreboot", "chrome",
    "Google Maps", "com..double", ".com.lead", "com.9bad", "", None, 42,
    "com.android.chrome -c x",
])
def test_open_app_rejects_bad_package_without_touching_the_device(cmds, package):
    c, fake = cmds
    result = _run(c.open_app(package))
    assert result["success"] is False and result["invalid_input"] is True
    assert fake.calls == []


@pytest.mark.parametrize("activity", [
    "Main; reboot", "$(reboot)", ".Main\nreboot", "../x", "Main Activity", "a/b"])
def test_open_app_rejects_bad_activity(cmds, activity):
    c, fake = cmds
    result = _run(c.open_app("com.android.chrome", activity))
    assert result["success"] is False and result["invalid_input"] is True
    assert fake.calls == []


# --- coordinates / fixed commands are quoted too -------------------------------


def test_tap_swipe_long_press_are_quoted_argv(cmds):
    c, fake = cmds
    _run(c.tap(100, 200, normalized=False))
    _run(c.swipe(1, 2, 3, 4, duration_ms=300, normalized=False))
    _run(c.long_press(5, 6, duration_ms=900, normalized=False))
    assert [shlex.split(s) for s in _shell_strings(fake)] == [
        ["input", "tap", "100", "200"],
        ["input", "swipe", "1", "2", "3", "4", "300"],
        ["input", "swipe", "5", "6", "5", "6", "900"],
    ]


def test_hostile_coordinate_cannot_inject(cmds, tmp_path):
    """Route bodies are int-typed, but the agent loop forwards model args; a
    non-numeric coordinate must reach `input` as one literal, never run."""
    c, fake = cmds
    canary = tmp_path / "pwned"
    _run(c.tap(f"1; touch {canary}", 2, normalized=False))
    (cmd,) = _shell_strings(fake)
    _run_like_device(cmd, tmp_path)
    assert not canary.exists()


# --- routes: invalid input is a 400, not a device call --------------------------


@pytest.mark.parametrize("path,body", [
    ("/adb/key", {"device_id": "phone-1", "keycode": "KEYCODE_HOME; reboot"}),
    ("/adb/open-app", {"device_id": "phone-1", "package_name": "com.x; reboot"}),
    ("/adb/open-app", {"device_id": "phone-1", "package_name": "com.android.chrome",
                       "activity": "$(reboot)"}),
])
def test_routes_answer_400_for_invalid_input(monkeypatch, path, body):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from Orchestrator.routes import adb_routes

    fake = _FakeADB()
    monkeypatch.setattr(adb_commands, "get_adb_manager", lambda: fake)
    monkeypatch.setattr(ADBCommands, "_get_serial", lambda self: "100.64.0.9:5555")
    app = FastAPI()
    app.include_router(adb_routes.router)
    resp = TestClient(app).post(path, json=body)
    assert resp.status_code == 400, resp.text
    assert fake.calls == []


def test_type_route_still_sends_quoted_text(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from Orchestrator.routes import adb_routes

    fake = _FakeADB()
    monkeypatch.setattr(adb_commands, "get_adb_manager", lambda: fake)
    monkeypatch.setattr(ADBCommands, "_get_serial", lambda self: "100.64.0.9:5555")
    app = FastAPI()
    app.include_router(adb_routes.router)
    resp = TestClient(app).post("/adb/type", json={"device_id": "phone-1", "text": "$(reboot)"})
    assert resp.status_code == 200 and resp.json()["success"] is True
    assert shlex.split(fake.calls[0][3]) == ["input", "text", "$(reboot)"]


# --- O14-06: is_connected must parse the device state ---------------------------


class _Device:
    def connection_string(self):
        return "100.64.0.9:5555"


class _Registry:
    def get_device(self, device_id):
        return _Device() if device_id == "phone-1" else None


@pytest.mark.parametrize("devices_out,expected", [
    ("List of devices attached\n100.64.0.9:5555\tdevice\n", True),
    ("List of devices attached\n100.64.0.9:5555\toffline\n", False),
    ("List of devices attached\n100.64.0.9:5555\tunauthorized\n", False),
    ("List of devices attached\n100.64.0.99:5555\tdevice\n", False),
    ("List of devices attached\n\n", False),
])
def test_is_connected_requires_the_serial_in_device_state(monkeypatch, devices_out, expected):
    import Orchestrator.device_registry as device_registry
    from Orchestrator.adb.manager import ADBManager

    monkeypatch.setattr(device_registry, "get_registry", lambda: _Registry())
    mgr = ADBManager()

    async def fake_run_adb(*args, timeout=10):
        assert args == ("devices",)
        return 0, devices_out.strip(), ""

    monkeypatch.setattr(mgr, "_run_adb", fake_run_adb)
    assert _run(mgr.is_connected("phone-1")) is expected
