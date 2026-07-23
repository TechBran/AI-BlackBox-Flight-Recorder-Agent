"""CU virtual-desktop population (production-readiness plan 2026-07-23, M2).

The barren-desktop bug: _start_quartet spawned only Xvfb + openbox + x11vnc
(+ websockify), so an agent's "desktop" was a blank managed root with no way
to open an app or folder — Chrome (launched separately) was the only thing
that ever appeared. The populated desktop adds a lightweight DE on the
session display: tint2 (panel/taskbar) + pcmanfm --desktop (icons + file
manager), with an openbox right-click menu for launching apps (xterm etc.).

Spawn is mocked (no real Xvfb) — the allocator's own bookkeeping is real.
"""
import itertools
import os

import pytest

from Orchestrator.browser import display as disp


class _FakePopen:
    _ids = itertools.count(2000)
    spawned = []  # every instance, in spawn order (reset per test)

    def __init__(self, cmd, env=None, **kw):
        self.cmd = list(cmd)
        self.env = dict(env) if env else {}
        self.pid = next(self._ids)
        self._alive = True
        self.args = self.cmd
        _FakePopen.spawned.append(self)

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


@pytest.fixture
def allocator(monkeypatch):
    _FakePopen.spawned = []
    monkeypatch.setattr(disp.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(disp.time, "sleep", lambda s: None)
    monkeypatch.setattr(disp, "_xvfb_ready", lambda n: True)
    monkeypatch.setattr(disp, "_live_view_available", lambda: True)
    # All DE binaries "installed" by default; tests override per-case.
    monkeypatch.setattr(disp.shutil, "which", lambda name: f"/usr/bin/{name}")
    return disp.DisplayAllocator()


def test_virtual_desktop_spawns_panel_and_file_manager(allocator):
    allocator.allocate("sess-de", backend="anthropic", operator="op")
    roles = set(allocator._procs["sess-de"].keys())
    assert {"xvfb", "openbox", "x11vnc", "websockify", "tint2", "pcmanfm"} <= roles


def test_de_processes_target_the_session_display(allocator):
    h = allocator.allocate("sess-disp", backend="anthropic", operator="op")
    procs = allocator._procs["sess-disp"]
    for role in ("tint2", "pcmanfm"):
        assert procs[role].env.get("DISPLAY") == h.display


def test_de_processes_get_seeded_xdg_config(allocator):
    """openbox resolves the Applications menu and pcmanfm its cu-agent profile
    (show_wm_menu=1 — hands desktop right-clicks to the WM) through a seeded,
    WRITABLE per-slot XDG dir; the repo tree must never be written to."""
    allocator.allocate("sess-menu", backend="anthropic", operator="op")
    procs = allocator._procs["sess-menu"]
    for role in ("openbox", "pcmanfm", "tint2"):
        xdg = procs[role].env.get("XDG_CONFIG_HOME", "")
        assert xdg, f"{role} must receive XDG_CONFIG_HOME"
        assert "assets" not in xdg  # writable seed dir, never the repo tree
    xdg = procs["openbox"].env["XDG_CONFIG_HOME"]
    assert os.path.isfile(os.path.join(xdg, "openbox", "menu.xml"))
    conf = os.path.join(xdg, "pcmanfm", "cu-agent", "pcmanfm.conf")
    assert os.path.isfile(conf)
    with open(conf) as f:
        assert "show_wm_menu=1" in f.read()
    # openbox must run on the DISTRO rc (full stock bindings incl. the Root
    # right-click -> root-menu). A --config-file override silently loses the
    # mouse bindings a partial rc omits (smoke-proven 2026-07-23).
    assert "--config-file" not in procs["openbox"].cmd


def test_population_degrades_when_de_packages_missing(allocator, monkeypatch):
    """Fresh-box gate: a box without tint2/pcmanfm still gets a working display
    (the original quartet) — population is additive, never a hard dependency."""
    monkeypatch.setattr(
        disp.shutil, "which",
        lambda name: None if name in ("tint2", "pcmanfm") else f"/usr/bin/{name}")
    allocator.allocate("sess-bare", backend="anthropic", operator="op")
    roles = set(allocator._procs["sess-bare"].keys())
    assert {"xvfb", "openbox", "x11vnc"} <= roles
    assert "tint2" not in roles and "pcmanfm" not in roles


def test_release_tears_down_every_spawned_role(allocator):
    allocator.allocate("sess-td", backend="anthropic", operator="op")
    spawned = list(_FakePopen.spawned)
    assert len(spawned) >= 6  # quartet + tint2 + pcmanfm
    allocator.release("sess-td")
    still_alive = [p.cmd[0] for p in spawned if p._alive]
    assert still_alive == []
    assert allocator._slots == {}
    assert allocator._sessions == {}


def test_teardown_order_covers_every_spawnable_role(allocator):
    """The leak-class guard: any role _start_quartet can register must appear in
    the release teardown order, dependents before xvfb."""
    allocator.allocate("sess-order", backend="anthropic", operator="op")
    roles = set(allocator._procs["sess-order"].keys())
    order = list(disp._TEARDOWN_ORDER)
    assert roles <= set(order)
    assert order[-1] == "xvfb"  # the X server dies last


def test_de_assets_ship_in_the_repo():
    assets = os.path.join(os.path.dirname(disp.__file__), "assets")
    tint2rc = os.path.join(assets, "tint2rc")
    menu = os.path.join(assets, "xdg", "openbox", "menu.xml")
    pf = os.path.join(assets, "pcmanfm-cu-agent.conf")
    assert os.path.isfile(tint2rc)
    assert os.path.isfile(menu)
    with open(menu) as f:
        menu_text = f.read()
    assert "pcmanfm" in menu_text and "xterm" in menu_text
    # The menu must define the id the distro rc's Root right-click binding
    # shows — anything else renders the Applications menu unreachable.
    assert 'id="root-menu"' in menu_text
    with open(pf) as f:
        assert "show_wm_menu=1" in f.read()
    with open(tint2rc) as f:
        assert "launcher_item_app" in f.read()  # one-click app buttons
