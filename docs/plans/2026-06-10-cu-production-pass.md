# Computer Use Production Pass — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bring the Computer Use agent to production quality: dynamic model catalog for Anthropic/Google/OpenAI, one unified session engine with three backend drivers, customer-machine portability (preflight + config), screen-aware themed UI on Portal + Android, and the first CU test suite.

**Architecture:** A new `/models/computer-use` endpoint live-fetches and capability-filters vendor catalogs (reusing the existing `models_cache.py` layer); a single `ComputerUseSession` feeds three driver coroutines (Anthropic extracted from chat_routes, Gemini rebuilt through `ActionExecutor`, OpenAI CUA new); `/cu/preflight` reports machine readiness with remediation strings. Both frontends hydrate models from the server catalog. Design doc: `docs/plans/2026-06-10-cu-production-pass-design.md`.

**Tech Stack:** FastAPI, httpx, google-genai (async client), anthropic + openai SDKs, pytest + monkeypatch, vanilla-JS Portal modules, Jetpack Compose (Android).

**Critical context for the implementer:**
- **THIS IS A WORKING SYSTEM, NOT A GREENFIELD BUILD.** Both CU paths function in production today: (a) the `use_computer` ToolVault tool-call path (`/browser/run` → tasks.py), and (b) the chat-provider path driven from the UI model selector (`provider: "computer-use"` → `stream_computer_use` / `stream_gemini_computer_use`). Every task is a hardening/refactor of live behavior — preserving what works is the binding constraint from Task 1, not just at the Phase-4 golden test. If a change would alter observable behavior of either path beyond what the task explicitly specifies, stop and flag it instead of proceeding.
- Working dir is the worktree: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.worktrees/cu-production-pass`. `config.ini`, `.env`, `credentials` are symlinked from the main tree (already done).
- Tests run with the MAIN tree's venv (untracked, not present in worktree):
  `PY=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python` then `$PY -m pytest ...`
- Baseline: 222 tests pass in ~7s. Run the full suite before every commit.
- Prod runs LIVE from the MAIN working tree — never edit the main tree during this work; everything lands in the worktree and merges at the end.
- Stage explicit paths only. NEVER `git add -A` or `git add .`.
- Backend test style to imitate: `Orchestrator/tests/test_live_models.py` and `test_models_cache.py` (monkeypatch module-level names, no network).

---

## Phase 1 — CU model catalog + config migration (additive)

### Task 1: CU config block in `Orchestrator/config.py`

**Files:**
- Modify: `Orchestrator/config.py` (after the `[models]` block, ~line 150)
- Modify: `config.ini.template` (new section)
- Test: `Orchestrator/tests/test_cu_catalog.py` (create)

**Step 1: Write the failing test**

Create `Orchestrator/tests/test_cu_catalog.py`:

```python
"""CU production pass — model catalog config + filter rules.

Per docs/plans/2026-06-10-cu-production-pass-design.md §1.
"""
import re

import pytest

from Orchestrator.config import (
    CU_MODEL_DEFAULT,
    CU_GEMINI_MODEL_DEFAULT,
    CU_MODEL_FILTERS,
    CU_NATIVE_MODE,
    CU_CHROME_PATH,
    CU_MAX_ITERATIONS,
    CU_SESSION_TIMEOUT,
)


def test_cu_config_values_exist_and_typed():
    assert isinstance(CU_MODEL_DEFAULT, str) and CU_MODEL_DEFAULT.startswith("claude-")
    assert "computer-use" in CU_GEMINI_MODEL_DEFAULT
    assert isinstance(CU_NATIVE_MODE, bool)
    assert isinstance(CU_CHROME_PATH, str)
    assert CU_MAX_ITERATIONS > 0
    assert CU_SESSION_TIMEOUT > 0


@pytest.mark.parametrize("backend,model_id,expected", [
    # Anthropic: 4+-series opus/sonnet pass, haiku and 3.x fail
    ("anthropic", "claude-opus-4-6", True),
    ("anthropic", "claude-opus-4-8", True),
    ("anthropic", "claude-sonnet-4-6", True),
    ("anthropic", "claude-opus-5", True),            # future-shaped
    ("anthropic", "claude-sonnet-5-2", True),        # future-shaped
    ("anthropic", "claude-haiku-4-5-20251001", False),
    ("anthropic", "claude-3-5-sonnet-20241022", False),
    # Google: id must contain computer-use
    ("google", "gemini-2.5-computer-use-preview-10-2025", True),
    ("google", "gemini-3-computer-use-preview", True),  # future-shaped
    ("google", "gemini-2.5-flash", False),
    ("google", "gemini-3.1-pro-preview", False),
    # OpenAI: computer-use-preview family only
    ("openai", "computer-use-preview", True),
    ("openai", "computer-use-preview-2025-03-11", True),
    ("openai", "gpt-5.1", False),
])
def test_cu_filter_rules(backend, model_id, expected):
    pattern = CU_MODEL_FILTERS[backend]
    assert bool(re.match(pattern, model_id)) is expected, (
        f"{backend} filter {pattern!r} on {model_id!r}: expected {expected}"
    )
```

**Step 2: Run it to verify it fails**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.worktrees/cu-production-pass
PY=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python
$PY -m pytest Orchestrator/tests/test_cu_catalog.py -v
```
Expected: FAIL — `ImportError: cannot import name 'CU_MODEL_DEFAULT'`.

**Step 3: Implement**

In `Orchestrator/config.py`, after the existing `[models]` reads (~line 150), add:

```python
# ── Computer Use (CU) — production pass 2026-06-10 ──────────────────────────
# Single source of truth for CU defaults. Replaces the literals that used to
# live in browser/config.py, gemini_cu/config.py, and two chat_routes sites.
CU_MODEL_DEFAULT        = CFG.get("computer_use", "model_default", fallback="claude-opus-4-6").strip()
CU_GEMINI_MODEL_DEFAULT = CFG.get("computer_use", "gemini_model_default",
                                  fallback="gemini-2.5-computer-use-preview-10-2025").strip()
CU_NATIVE_MODE          = CFG.getboolean("computer_use", "native_mode", fallback=True)
CU_CHROME_PATH          = CFG.get("computer_use", "chrome_path", fallback="/opt/google/chrome/chrome").strip()
CU_MAX_ITERATIONS       = CFG.getint("computer_use", "max_iterations", fallback=100)
CU_SESSION_TIMEOUT      = CFG.getint("computer_use", "session_timeout_s", fallback=300)

# CU-capability filters: which model ids from each vendor's live catalog can
# drive the computer tool. Regex anchored at start (re.match). Data, not code —
# when a vendor ships a new CU-capable family, extend the pattern here.
#   anthropic: opus/sonnet at major version >= 4 (computer_20251124 tool)
#   google:    any Gemini id containing "computer-use"
#   openai:    the Responses-API CUA model family
CU_MODEL_FILTERS = {
    "anthropic": r"claude-(opus|sonnet)-([4-9]|\d{2,})",
    "google":    r"gemini-.*computer-use",
    "openai":    r"computer-use-preview",
}
```

In `config.ini.template`, append:

```ini
[computer_use]
# native_mode = true  -> CU drives the real desktop; false -> sandboxed Xvfb
native_mode = true
# chrome_path = /opt/google/chrome/chrome
model_default = claude-opus-4-6
gemini_model_default = gemini-2.5-computer-use-preview-10-2025
max_iterations = 100
session_timeout_s = 300
```

**Step 4: Run the test — expect PASS. Run full suite — expect 222+ pass.**

```bash
$PY -m pytest Orchestrator/tests/test_cu_catalog.py -v && $PY -m pytest -q
```

**Step 5: Commit**

```bash
git add Orchestrator/config.py config.ini.template Orchestrator/tests/test_cu_catalog.py
git commit -m "feat(cu): CU config block + capability filter rules in config.py"
```

---

### Task 2: `GET /models/computer-use` endpoint

**Files:**
- Modify: `Orchestrator/routes/admin_routes.py` (fetchers section, ~line 590–760)
- Test: `Orchestrator/tests/test_cu_catalog.py` (extend)

**Step 1: Write the failing tests** (append to `test_cu_catalog.py`)

```python
from unittest.mock import patch

from Orchestrator.utils import models_cache


@pytest.fixture(autouse=True)
def _clear_models_cache():
    models_cache.invalidate()
    yield
    models_cache.invalidate()


def _mk(provider, ids):
    """Vendor-fetcher stub result in the _wrap envelope."""
    from Orchestrator.routes.admin_routes import _wrap
    return _wrap(provider, [{"id": i, "name": i} for i in ids], "live")


def test_cu_catalog_merges_and_filters(monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-opus-4-8", "claude-haiku-4-5-20251001"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: _mk("google", ["gemini-2.5-computer-use-preview-10-2025", "gemini-2.5-flash"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai",
        lambda: _mk("openai", ["computer-use-preview", "gpt-5.1"]))

    out = admin_routes.get_available_models("computer-use")
    ids = {m["id"] for m in out["models"]}
    assert ids == {"claude-opus-4-8",
                   "gemini-2.5-computer-use-preview-10-2025",
                   "computer-use-preview"}
    # Locked contract + new backend field
    assert out["provider"] == "computer-use"
    assert out["source"] == "live"
    assert out["default_id"]
    for m in out["models"]:
        assert m["backend"] in ("anthropic", "google", "openai")


def test_cu_catalog_partial_vendor_failure(monkeypatch):
    """One vendor down -> still live, with the healthy vendors' models."""
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-sonnet-4-6"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: (_ for _ in ()).throw(RuntimeError("google down")))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai", lambda: None)

    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "live"
    assert [m["id"] for m in out["models"]] == ["claude-sonnet-4-6"]


def test_cu_catalog_all_down_falls_back(monkeypatch):
    from Orchestrator.routes import admin_routes
    for p in ("anthropic", "google", "openai"):
        monkeypatch.setitem(admin_routes._FETCHERS, p, lambda: None)
    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "fallback"
    assert out["models"], "static fallback must not be empty"
    assert all(m.get("backend") for m in out["models"])
```

**Step 2: Run — expect FAIL** (`get_available_models("computer-use")` → 404 HTTPException).

**Step 3: Implement** in `admin_routes.py`:

1. Import at top with the other config imports: `from Orchestrator.config import CU_MODEL_DEFAULT, CU_MODEL_FILTERS`.
2. Add to `_FALLBACK_MODELS` dict:

```python
    "computer-use": [
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "backend": "anthropic"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "backend": "anthropic"},
        {"id": "gemini-2.5-computer-use-preview-10-2025", "name": "Gemini CU Preview", "backend": "google"},
        {"id": "computer-use-preview", "name": "OpenAI Computer Use", "backend": "openai"},
    ],
```

3. Add to `_DEFAULT_MODEL`: `"computer-use": CU_MODEL_DEFAULT,`
4. After `_fetch_xai_models`, add:

```python
def _fetch_cu_models() -> dict | None:
    """Merge the three vendor catalogs, filtered to CU-capable models.

    Each vendor fetched independently (per-vendor failure tolerated). Returns
    None only when EVERY vendor fails, triggering the static fallback.
    Each model gains a `backend` field used by frontends for grouping and by
    the CU dispatcher for driver routing.
    """
    import re
    merged = []
    any_live = False
    for backend in ("anthropic", "google", "openai"):
        try:
            result = _FETCHERS[backend]()
        except Exception:
            result = None
        if not result:
            continue
        any_live = True
        pattern = CU_MODEL_FILTERS[backend]
        for m in result["models"]:
            if re.match(pattern, m["id"]):
                merged.append({**m, "backend": backend})
    return _wrap("computer-use", merged, "live") if any_live else None
```

> **AMENDMENT (post-review, 2026-06-10):** The original `_fetch_cu_models` above has a
> defect found by live probe during code review: `_fetch_openai_models` carries a chat
> prefix gate (`gpt-`/`o`) so `computer-use-preview` can NEVER reach the CU filter, and
> OpenAI's catalog may not list the CUA model at all (access-gated) — the OpenAI backend
> would be permanently absent from a "live" merge. Corrected semantics (implemented):
> - Per vendor: on live models passing the filter → `backends[vendor]="live"`, append them.
>   On live-but-zero-after-filter → `backends[vendor]="fallback"`, append that vendor's
>   entries from `_FALLBACK_MODELS["computer-use"]` (backfill — covers both the prefix
>   gate and filter rot). On fetch error/None → `backends[vendor]="error"`, append the
>   same backfill entries, and `logging.warning` the failure (no silent swallowing).
> - Envelope gains an additive `backends` field (vendor → live|fallback|error) so
>   frontends/operators can see partial degradation.
> - Return `None` (→ static fallback path) only when ALL vendors error.
> - Do NOT loosen `_fetch_openai_models` itself — that would leak CUA models into the
>   chat dropdown.
> Extra tests: cache engages on second call (call-counter, `cached: True`), 
> `default_id == CU_MODEL_DEFAULT`, openai backfill when live-but-filtered-empty,
> `backends` statuses on mixed failure.

5. Register: `_FETCHERS["computer-use"] = _fetch_cu_models` is WRONG (tests monkeypatch per-vendor entries in that dict) — instead, in `get_available_models`, route explicitly:

```python
    fetcher = _fetch_cu_models if provider == "computer-use" else _FETCHERS.get(provider)
```

(`provider in _FALLBACK_MODELS` already admits "computer-use" after step 2.)

**Step 4: Run tests + full suite — PASS. Also verify the existing 4 providers still work:**

```bash
$PY -m pytest Orchestrator/tests/test_cu_catalog.py Orchestrator/tests/test_models_cache.py -v && $PY -m pytest -q
```

**Step 5: Commit**

```bash
git add Orchestrator/routes/admin_routes.py Orchestrator/tests/test_cu_catalog.py
git commit -m "feat(cu): /models/computer-use merged live catalog with capability filters"
```

---

### Task 3: Kill scattered model defaults + retired Gemini constants

**Files:**
- Modify: `Orchestrator/browser/config.py` (~line 248–254: `MAX_ITERATIONS`, `SESSION_TIMEOUT`, `CU_MODEL`, `NATIVE_MODE` line 13, `CHROME_PATH` line 242)
- Modify: `Orchestrator/gemini_cu/config.py`
- Modify: `Orchestrator/routes/chat_routes.py` (two sites: ~5949 and ~6032 `model = "claude-opus-4-6"`)
- Test: `Orchestrator/tests/test_cu_catalog.py` (extend)

**Step 1: Failing test** (append):

```python
def test_no_scattered_cu_model_literals():
    """Defaults come from config; retired Gemini ids are gone."""
    from Orchestrator.browser import config as bconfig
    from Orchestrator.gemini_cu import config as gconfig
    from Orchestrator.config import CU_MODEL_DEFAULT, CU_GEMINI_MODEL_DEFAULT

    assert bconfig.CU_MODEL == CU_MODEL_DEFAULT
    assert gconfig.DEFAULT_CU_MODEL == CU_GEMINI_MODEL_DEFAULT
    assert not hasattr(gconfig, "GEMINI_CU_MODEL_PRO"), "retired gemini-3-pro-preview ref must be deleted"
    assert not hasattr(gconfig, "GEMINI_CU_MODEL_FLASH")

    import inspect
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(chat_routes)
    assert 'model = "claude-opus-4-6"' not in src, "chat_routes must use CU_MODEL_DEFAULT"
```

**Step 2: Run — FAIL.**

**Step 3: Implement.**

`Orchestrator/browser/config.py`:
- Line 13: `NATIVE_MODE = True` → move the `from Orchestrator.config import ...` up isn't possible (circular timing is fine — config.py has no browser imports). Replace with:
  ```python
  from Orchestrator.config import (
      ANTHROPIC_API_KEY, CU_NATIVE_MODE, CU_CHROME_PATH,
      CU_MODEL_DEFAULT, CU_MAX_ITERATIONS, CU_SESSION_TIMEOUT,
  )
  NATIVE_MODE = CU_NATIVE_MODE
  ```
  and DELETE the late `from Orchestrator.config import ANTHROPIC_API_KEY` at line 258.
- `CHROME_PATH = "/opt/google/chrome/chrome"` → `CHROME_PATH = CU_CHROME_PATH`
- `MAX_ITERATIONS = 100` → `MAX_ITERATIONS = CU_MAX_ITERATIONS`
- `SESSION_TIMEOUT = 300` → `SESSION_TIMEOUT = CU_SESSION_TIMEOUT`
- `CU_MODEL = "claude-opus-4-6"` → `CU_MODEL = CU_MODEL_DEFAULT`

`Orchestrator/gemini_cu/config.py` — replace the model block:

```python
from Orchestrator.config import CU_GEMINI_MODEL_DEFAULT

# Default model for CU tasks — single source: Orchestrator/config.py
DEFAULT_CU_MODEL = CU_GEMINI_MODEL_DEFAULT
```

DELETE `GEMINI_CU_MODEL`, `GEMINI_CU_MODEL_PRO`, `GEMINI_CU_MODEL_FLASH` lines entirely (grep the repo for usages first: `grep -rn "GEMINI_CU_MODEL" Orchestrator/ --include='*.py'` and fix any importer to use `DEFAULT_CU_MODEL`).

`chat_routes.py` both sites: `model = "claude-opus-4-6"  # ...` → `model = CU_MODEL_DEFAULT` (add `CU_MODEL_DEFAULT` to the existing `from Orchestrator.config import ...` block at the top of chat_routes.py).

**Step 4: Full suite — PASS.** Also smoke-import: `$PY -c "import Orchestrator.app"` (catches circular-import regressions; takes ~10s).

**Step 5: Commit**

```bash
git add Orchestrator/browser/config.py Orchestrator/gemini_cu/config.py Orchestrator/routes/chat_routes.py Orchestrator/tests/test_cu_catalog.py
git commit -m "feat(cu): single-source CU model defaults; delete retired gemini-3-*-preview refs"
```

---

## Phase 2 — Preflight diagnostics (additive)

### Task 4: `Orchestrator/browser/preflight.py`

**Files:**
- Create: `Orchestrator/browser/preflight.py`
- Test: `Orchestrator/tests/test_cu_preflight.py` (create)

**Step 1: Failing tests**

```python
"""CU preflight — machine-readiness checks with remediation strings."""
from unittest.mock import patch

from Orchestrator.browser import preflight


def test_check_shape():
    """Every check returns the locked shape."""
    report = preflight.run_preflight(skip_screenshot=True)
    assert isinstance(report["checks"], list) and report["checks"]
    for c in report["checks"]:
        assert set(c) >= {"id", "status", "detail", "remediation"}
        assert c["status"] in ("ok", "warn", "fail")
    assert report["status"] in ("ok", "warn", "fail")


def test_input_backend_wayland_no_ydotool(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight.shutil, "which",
                        lambda b: None if b == "ydotool" else f"/usr/bin/{b}")
    c = preflight.check_input_backend()
    assert c["status"] == "fail"
    assert "ydotool" in c["remediation"]


def test_input_backend_wayland_daemon_dead(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(preflight, "_ydotool_socket_alive", lambda: False)
    c = preflight.check_input_backend()
    assert c["status"] == "fail"
    assert "daemon" in c["remediation"].lower()


def test_x11_xdotool_ok(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: False)
    monkeypatch.setattr(preflight.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert preflight.check_input_backend()["status"] == "ok"


def test_api_keys_reported(monkeypatch):
    monkeypatch.setattr(preflight, "ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setattr(preflight, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(preflight, "OPENAI_API_KEY", "")
    c = preflight.check_api_keys()
    assert c["status"] == "warn"          # at least one backend usable
    assert "google" in c["detail"].lower()
```

**Step 2: Run — FAIL (module missing).**

**Step 3: Implement** `Orchestrator/browser/preflight.py`:

```python
"""CU preflight — is THIS machine ready to run Computer Use?

Each check returns {"id", "status": ok|warn|fail, "detail", "remediation"}.
Aggregate status = worst individual status. Secondary tooling (vnc/adb) can
only warn, never fail — remote devices are optional.
"""
import os
import shutil

from Orchestrator.config import ANTHROPIC_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY
from Orchestrator.browser.config import (
    NATIVE_MODE, ACTIVE_DISPLAY, CHROME_PATH,
    detect_native_resolution, get_scale_factors, get_native_env,
)
from Orchestrator.browser.actions import _is_wayland_session


def _is_wayland() -> bool:
    return _is_wayland_session()


def _ydotool_socket_alive() -> bool:
    sock = os.environ.get("YDOTOOL_SOCKET", "/run/user/%d/.ydotool_socket" % os.getuid())
    return os.path.exists(sock)


def _check(id_, status, detail, remediation=""):
    return {"id": id_, "status": status, "detail": detail, "remediation": remediation}


def check_display() -> dict:
    env = get_native_env()
    if not NATIVE_MODE:
        return _check("display", "ok", "Sandbox mode (Xvfb) — display managed internally")
    missing = [k for k in ("XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS") if k not in env]
    if missing and _is_wayland():
        return _check("display", "warn",
                      f"Display :{ACTIVE_DISPLAY}; session env missing {missing}",
                      "Log into the desktop session — the service picks it up automatically")
    return _check("display", "ok",
                  f"Display :{ACTIVE_DISPLAY} ({'Wayland' if _is_wayland() else 'X11'})")


def check_input_backend() -> dict:
    if _is_wayland():
        if not shutil.which("ydotool"):
            return _check("input", "fail", "Wayland session, ydotool not installed",
                          "Install ydotool: sudo apt install ydotool")
        if not _ydotool_socket_alive():
            return _check("input", "fail", "ydotool installed but daemon not running",
                          "Start the daemon: systemctl enable --now ydotool")
        return _check("input", "ok", "Wayland + ydotool daemon alive")
    if not shutil.which("xdotool"):
        return _check("input", "fail", "X11 session, xdotool not installed",
                      "Install xdotool: sudo apt install xdotool")
    return _check("input", "ok", "X11 + xdotool")


def check_screenshot() -> dict:
    from Orchestrator.browser.screenshot import capture_screenshot
    try:
        png = capture_screenshot()
        if len(png) < 1000:
            return _check("screenshot", "fail", f"Capture returned {len(png)} bytes",
                          "Check XDG Desktop Portal / install scrot: sudo apt install scrot")
        return _check("screenshot", "ok", f"Captured {len(png)} bytes")
    except Exception as e:
        return _check("screenshot", "fail", f"Capture failed: {e}",
                      "Install scrot (sudo apt install scrot) and verify the desktop session is active")


def check_resolution() -> dict:
    w, h = detect_native_resolution(force=True)
    sx, sy = get_scale_factors()
    return _check("resolution", "ok", f"{w}x{h} native; scale {sx:.2f}x{sy:.2f}")


def check_api_keys() -> dict:
    have = {"anthropic": bool(ANTHROPIC_API_KEY), "google": bool(GOOGLE_API_KEY),
            "openai": bool(OPENAI_API_KEY)}
    missing = [k for k, v in have.items() if not v]
    if not any(have.values()):
        return _check("api_keys", "fail", "No CU backend API keys configured",
                      "Set ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY in .env")
    if missing:
        return _check("api_keys", "warn", f"Missing keys: {', '.join(missing)}",
                      "Those backends will be hidden from the model selector")
    return _check("api_keys", "ok", "All three CU backend keys present")


def check_chrome() -> dict:
    if os.path.isfile(CHROME_PATH) or shutil.which("google-chrome"):
        return _check("chrome", "ok", f"Chrome at {CHROME_PATH}")
    return _check("chrome", "warn", f"Chrome not found at {CHROME_PATH}",
                  "Only needed for sandbox mode; set computer_use.chrome_path in config.ini")


def check_remote_tools() -> dict:
    missing = [b for b in ("vncdotool", "adb") if not shutil.which(b)]
    if missing:
        return _check("remote", "warn", f"Remote-device tools missing: {', '.join(missing)}",
                      "Optional — only needed for VNC/Android targets")
    return _check("remote", "ok", "vncdotool + adb present")


_RANK = {"ok": 0, "warn": 1, "fail": 2}


def run_preflight(skip_screenshot: bool = False) -> dict:
    checks = [check_display(), check_input_backend()]
    if not skip_screenshot:
        checks.append(check_screenshot())
    checks += [check_resolution(), check_api_keys(), check_chrome(), check_remote_tools()]
    worst = max(checks, key=lambda c: _RANK[c["status"]])["status"]
    return {"status": worst, "checks": checks}
```

**Step 4: Run tests + full suite — PASS.**

**Step 5: Commit**

```bash
git add Orchestrator/browser/preflight.py Orchestrator/tests/test_cu_preflight.py
git commit -m "feat(cu): preflight readiness checks with remediation strings"
```

> **AMENDMENT (post-review, 2026-06-10):** the original spec above shipped with plan
> defects found in quality review; the corrected semantics (implemented in a follow-up
> commit) are:
> 1. The ydotool check MUST reuse `actions.py`'s actual runtime gating — call
>    `actions._ydotool_available()` and report against `actions.YDOTOOL_BIN`
>    (hardcoded `/usr/local/bin/ydotool`; apt's `/usr/bin/ydotool` 0.1.8 lacks
>    `--absolute mousemove` and is deliberately NOT used) and `actions.YDOTOOL_SOCKET`
>    (hardcoded; `_run_ydotool` overrides the env var). `_ydotool_socket_alive` deleted —
>    a stale/exists-only socket check diverges from the S_ISSOCK+W_OK runtime check.
> 2. Remediation strings: daemon unit is **`ydotoold.service`** (`systemctl enable --now
>    ydotoold`); install remediation points at the BlackBox installer step
>    (`Scripts/install.sh` builds ydotool v1.0.4 from source), NEVER `apt install ydotool`.
> 3. `check_display` warns on missing XAUTHORITY/DBUS in native mode regardless of
>    Wayland/X11 (X11 needs them too).
> 4. `check_resolution` uses `force=not skip_screenshot` to avoid a second full Portal
>    capture when the caller asked for the cheap path.
> 5. `run_preflight` wraps each check in try/except → a check that raises degrades to
>    a `fail` entry instead of 500ing the whole report.
> 6. `check_chrome` detail names the path that actually matched.
> 7. Added tests: aggregation precedence (fail>warn>ok), ydotool gating consistency
>    (monkeypatch `actions._ydotool_available`), raising-check degradation.

---

### Task 5: `GET /cu/preflight` route + installer deps

**Files:**
- Modify: `Orchestrator/routes/browser_routes.py` (append)
- Modify: `Scripts/install.sh` (NOT `installer/install.sh` — that path was a plan error.
  AMENDED per Task 4 review: ydotool is ALREADY handled by the installer — it builds
  v1.0.4 from source and installs/enables `ydotoold.service` at ~lines 868–924. Do NOT
  add apt `ydotool` or `systemctl enable --now ydotool`. Only ensure `xdotool` and
  `scrot` are present in the apt package list; if they already are, no installer change
  at all — verify and report.)
- Test: `Orchestrator/tests/test_cu_preflight.py` (extend)

**Step 1: Failing test** (append):

```python
def test_preflight_route():
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    with patch.object(preflight, "run_preflight",
                      return_value={"status": "ok", "checks": []}) as m:
        client = TestClient(app)
        r = client.get("/cu/preflight")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

(If `TestClient(app)` is heavyweight in this codebase, mirror how `test_wizard_routes.py` instantiates it.)

**Step 2: Run — FAIL (404).**

**Step 3: Implement** — append to `browser_routes.py`:

```python
@app.get("/cu/preflight")
def cu_preflight(skip_screenshot: bool = False):
    """Machine-readiness report for Computer Use. Frontends render fails
    as banners with the remediation text."""
    from Orchestrator.browser import preflight
    return preflight.run_preflight(skip_screenshot=skip_screenshot)
```

In `Scripts/install.sh`, locate the existing apt package list and ensure `xdotool` and
`scrot` are present (add only if absent). ydotool/ydotoold is already fully handled by
the installer's source-build step (~lines 868–924) — make NO ydotool changes.

**Step 4: Tests + full suite — PASS. Manual route check is deferred to merge (prod tree).**

**Step 5: Commit**

```bash
git add Orchestrator/routes/browser_routes.py installer/install.sh Orchestrator/tests/test_cu_preflight.py
git commit -m "feat(cu): /cu/preflight route + installer CU deps (xdotool/ydotool/scrot)"
```

---

## Phase 3 — Gemini driver parity

### Task 6: Gemini coordinate space in `ActionExecutor`

**Files:**
- Modify: `Orchestrator/browser/actions.py` (`_scale_coord` ~line 142, `ActionExecutor.__init__` ~line 215)
- Test: `Orchestrator/tests/test_cu_actions.py` (create)

**Step 1: Failing tests**

```python
"""Coordinate-space scaling for ActionExecutor (anthropic-1280 vs gemini-999)."""
import pytest

from Orchestrator.browser import actions as A


@pytest.fixture
def fake_resolution(monkeypatch):
    def _set(w, h):
        monkeypatch.setattr("Orchestrator.browser.config.detect_native_resolution",
                            lambda force=False: (w, h))
    return _set


@pytest.mark.parametrize("native,cu_xy,expected", [
    ((1920, 1080), (640, 360), (960, 540)),    # 1080p: 1.5x
    ((3840, 2160), (640, 360), (1920, 1080)),  # 4K: 3x
    ((3440, 1440), (1280, 720), (3440, 1440)), # ultrawide bottom-right corner
])
def test_anthropic_space_scaling(fake_resolution, native, cu_xy, expected):
    fake_resolution(*native)
    ex = A.ActionExecutor()
    assert ex.to_native(*cu_xy) == expected


@pytest.mark.parametrize("native,gxy,expected", [
    ((1920, 1080), (999, 999), (1920, 1080)),
    ((1920, 1080), (0, 0), (0, 0)),
    ((3840, 2160), (500, 500), (1921, 1081)),  # int(500/999*3840)=1921
])
def test_gemini_space_scaling(fake_resolution, native, gxy, expected):
    fake_resolution(*native)
    ex = A.ActionExecutor(coord_space="gemini-999")
    assert ex.to_native(*gxy) == expected
```

**Step 2: Run — FAIL (`to_native` missing / no coord_space kwarg).**

**Step 3: Implement** in `actions.py`:

```python
class ActionExecutor:
    def __init__(self, display_number: int = DISPLAY_NUMBER, coord_space: str = "anthropic-1280"):
        self.display_number = display_number
        self.coord_space = coord_space   # "anthropic-1280" (1280x720) | "gemini-999" (0-999 normalized)
        self.use_ydotool = _use_ydotool()

    def to_native(self, x: int, y: int) -> tuple:
        """Convert model-space coordinates to native desktop pixels using the
        LIVE resolution (never the stale import-time constants)."""
        from Orchestrator.browser.config import detect_native_resolution, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT, NATIVE_MODE
        if not NATIVE_MODE:
            return int(x), int(y)
        w, h = detect_native_resolution()
        if self.coord_space == "gemini-999":
            return int(x / 999 * w), int(y / 999 * h)
        return int(x * w / CU_DISPLAY_WIDTH), int(y * h / CU_DISPLAY_HEIGHT)
```

Then update `_scale_coord` (and every action handler that uses it) to delegate to `self.to_native` so both spaces flow through one code path. Read the existing `_scale_coord` and handlers first — keep their jitter/sync behavior identical for the default space (the golden tests in Task 9 guard this).

**Step 4: Run new tests + full suite — PASS.**

**Step 5: Commit** — `git add Orchestrator/browser/actions.py Orchestrator/tests/test_cu_actions.py && git commit -m "feat(cu): coord_space-aware ActionExecutor (anthropic-1280 | gemini-999), live resolution"`

---

### Task 7: Rebuild Gemini desktop actions through `ActionExecutor` + async API

**Files:**
- Modify: `Orchestrator/gemini_cu/agent_loop.py` (`_execute_predefined_action` ~line 195–297; API call ~line 540; `_default_system_prompt` ~line 380)
- Modify: `Orchestrator/routes/gemini_cu_routes.py` (`_snapshot_cu_result` ~line 18)
- Test: `Orchestrator/tests/test_gemini_cu_actions.py` (create)

**Step 1: Failing tests** — mock `_run_xdotool`/`_run_ydotool` to capture argv; assert the browser/desktop branch uses `ActionExecutor(coord_space="gemini-999")` (assert on `executor.execute` calls via monkeypatch) and NEVER reads `NATIVE_WIDTH`:

```python
"""Gemini CU desktop actions must route through ActionExecutor (Wayland +
dynamic resolution) — direct _run_xdotool + stale NATIVE_WIDTH is the bug
this pass deletes."""
import inspect

import pytest

from Orchestrator.gemini_cu import agent_loop as G


def test_no_stale_resolution_constants():
    src = inspect.getsource(G)
    assert "NATIVE_WIDTH" not in src, "must use ActionExecutor.to_native (live resolution)"
    assert "_run_xdotool" not in src, "must route through ActionExecutor"


def test_no_hardcoded_display_in_prompt():
    class S:  # minimal session stub
        environment = "desktop"
    prompt = G._default_system_prompt(S())
    assert "1920x1080" not in prompt
    assert "display :0" not in prompt
    assert "get_current_time" not in prompt  # unsatisfiable instruction removed


@pytest.mark.asyncio
async def test_click_at_routes_through_executor(monkeypatch):
    calls = []
    class FakeExecutor:
        coord_space = "gemini-999"
        def __init__(self, *a, **k): assert k.get("coord_space") == "gemini-999"
        def execute(self, action, **params):
            calls.append((action, params)); return {"success": True}
        def to_native(self, x, y): return (x, y)
    monkeypatch.setattr(G, "ActionExecutor", FakeExecutor)
    class S:
        environment = "desktop"; device_id = "blackbox"
    result = await G._execute_predefined_action(S(), "click_at", {"x": 500, "y": 500})
    assert result["success"]
    assert calls and calls[0][0] == "left_click"
```

(Add `pytest-asyncio` config if not present: check `pytest.ini` for `asyncio_mode`; the repo already tests async routes — mirror whatever `test_stt_ws.py` uses.)

**Step 2: Run — FAIL.**

**Step 3: Implement.** Rewrite `_execute_predefined_action`'s browser/desktop branch as a dispatch table onto a module-level `ActionExecutor` import (top of file: `from Orchestrator.browser.actions import ActionExecutor`):

| Gemini function | ActionExecutor call |
|---|---|
| `click_at(x,y)` | `execute("left_click", coordinate=[x, y])` |
| `hover_at(x,y)` | `execute("mouse_move", coordinate=[x, y])` |
| `type_text_at(x,y,text,clear_before_typing)` | `execute("left_click", coordinate=[x,y])` then optional `execute("key", text="ctrl+a")` then `execute("type", text=text)` |
| `key_combination(keys)` | `execute("key", text=_map_gemini_keys(keys))` |
| `scroll_at(x,y,direction,magnitude)` | `execute("scroll", coordinate=[x,y], scroll_direction=direction, scroll_amount=max(1,int(magnitude)))` |
| `scroll_document(direction)` | same, at screen center `[500, 500]` |
| `drag_and_drop(x,y,destination_x,destination_y)` | `execute("left_click_drag", start_coordinate=[x,y], coordinate=[dx,dy])` |
| `navigate(url)` | `execute("key", text="ctrl+l")`, `execute("type", text=url)`, `execute("key", text="Return")` |
| `wait_5_seconds` | `await asyncio.sleep(5)` |

Construct once per call: `executor = ActionExecutor(coord_space="gemini-999")`. Read the actual `_action_*` handler signatures in `actions.py` first and match their kwarg names exactly (e.g. confirm whether scroll takes `scroll_direction`/`scroll_amount` or `direction`/`clicks`). The Android branch is untouched (ADB already handles 0–999 natively).

API call (line ~540): `response = client.models.generate_content(...)` → `response = await client.aio.models.generate_content(...)`.

`_default_system_prompt`: delete the TEMPORAL AWARENESS blocks in all three variants, replace with a line interpolated at composition time: `f"Current date/time: {datetime.now().isoformat(timespec='seconds')}"`. Delete "display :0, 1920x1080" — describe coordinates as "normalized 0-999 over the full screen" only.

`gemini_cu_routes.py` `_snapshot_cu_result`: change the POST target from `"http://localhost:9091/chat"` to `"http://localhost:9091/chat/save"` (same payload shape; `/chat/save` is direct persistence — see CLAUDE.md).

**Step 4: Run new tests + full suite — PASS.**

**Step 5: Commit**

```bash
git add Orchestrator/gemini_cu/agent_loop.py Orchestrator/routes/gemini_cu_routes.py Orchestrator/tests/test_gemini_cu_actions.py
git commit -m "feat(cu): Gemini driver parity — ActionExecutor routing, async API, /chat/save, prompt fixes"
```

---

### Task 8: Fix Anthropic prompts (`get_current_time`) + operator defaults

**Files:**
- Modify: `Orchestrator/browser/agent_loop.py` (`DEFAULT_SYSTEM_PROMPT` line 25)
- Modify: `Orchestrator/routes/chat_routes.py` (`stream_computer_use` line 3886, `stream_gemini_computer_use` line 4149: `operator: str = "Brandon"` → `operator: str`)
- Modify: `Orchestrator/scheduler/executor.py` (AMENDMENT from Task 3 review: lines ~159 `_execute_cu_job` payload and ~380 `_resolve_model_name` CU branch hardcode `"claude-opus-4-6"` — both → `CU_MODEL_DEFAULT`, matching how the sibling branches already use `*_MODEL_DEFAULT`.)
- AMENDMENT (Task 3 quality review) — harden `test_no_scattered_cu_model_literals`:
  the value-equality asserts (`bconfig.CU_MODEL == CU_MODEL_DEFAULT`) cannot catch a
  re-hardcoded literal while the config fallback is the same string. Replace/extend with
  source scans: for `browser/config.py`, `gemini_cu/config.py`, and `scheduler/executor.py`,
  `assert not re.search(r'=\s*["\\'](claude-|gemini-\d)', inspect.getsource(mod))`;
  for chat_routes use the formatting-robust `re.search(r'model\s*=\s*["\\']claude-', src)`
  instead of the exact-string check.
- Test: extend `Orchestrator/tests/test_cu_catalog.py`

**Steps:** same TDD rhythm — test that `DEFAULT_SYSTEM_PROMPT` has no `get_current_time` and that the two functions have no `"Brandon"` default (`inspect.signature(...).parameters["operator"].default is inspect.Parameter.empty`). Verify all call sites already pass `operator=` explicitly (grep `stream_computer_use(` and `stream_gemini_computer_use(`) — they do (lines ~5967–6054). Run suite, commit:

```bash
git add Orchestrator/browser/agent_loop.py Orchestrator/routes/chat_routes.py Orchestrator/tests/test_cu_catalog.py
git commit -m "fix(cu): drop unsatisfiable get_current_time instruction + hardcoded operator defaults"
```

---

## Phase 4 — Loop consolidation

### Task 9: Golden test for `/browser/run` result contract

**Files:**
- Test: `Orchestrator/tests/test_cu_golden_browser_run.py` (create)

Before touching the legacy loop, pin the externally observable contract of the task path: a `USE_COMPUTER` task ends `COMPLETED` with `result_data` containing `result_text`, `screenshots` (list), `final_screenshot`, `steps`, `tokens{input,output}` keys. Mock at the seam: monkeypatch the Anthropic HTTP call to return a canned no-tool-use response (`stop_reason: "end_turn"`) and `capture_screenshot` to return a tiny valid PNG (use `PIL.Image.new("RGB",(4,4))`). Drive `Orchestrator/tasks.py`'s USE_COMPUTER execution function directly (read `tasks.py:998` first for its name and call shape). Assert the keys above.

Run — must PASS against CURRENT code before any refactor. Commit: `git add Orchestrator/tests/test_cu_golden_browser_run.py && git commit -m "test(cu): golden contract for /browser/run task path pre-consolidation"`.

### Task 10: Extract `AnthropicDriver` from chat_routes

**Files:**
- Create: `Orchestrator/browser/driver_anthropic.py`
- Modify: `Orchestrator/routes/chat_routes.py` (~line 3339–3886)

**Steps:**
1. Cut `_cu_agent_loop` (entire function, chat_routes ~3339–3885) verbatim into `Orchestrator/browser/driver_anthropic.py` as `run_anthropic_cu_loop` (same signature — it is fully parameterized: `(session, history, system_prompt, tools, headers, model, operator, user_text)`; its imports are already lazy/inner).
2. In chat_routes, replace the definition with `from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop as _cu_agent_loop` (keeps both call sites at ~3868/4091 unchanged).
3. Watch the one internal self-reference: the queue-drain continuation at old line ~3868 calls `_cu_agent_loop` recursively — inside the new module it calls `run_anthropic_cu_loop` directly.
4. Run: `$PY -m pytest -q` AND `$PY -c "import Orchestrator.app"` (import-cycle check — memory: cutover import cycles bit ToolVault v2).
5. Commit: `git add Orchestrator/browser/driver_anthropic.py Orchestrator/routes/chat_routes.py && git commit -m "refactor(cu): extract Anthropic CU loop to browser/driver_anthropic.py"`

### Task 11: Backend dispatcher (kill `"gemini" in model` sniffing)

**Files:**
- Create: `Orchestrator/browser/dispatch.py`
- Modify: `Orchestrator/routes/chat_routes.py` (~5948–5972 and ~6031–6054)
- Test: `Orchestrator/tests/test_cu_catalog.py` (extend)

```python
# Orchestrator/browser/dispatch.py
"""Resolve a CU model id to its backend driver using the same filter rules
as the /models/computer-use catalog."""
import re

from Orchestrator.config import CU_MODEL_FILTERS, CU_MODEL_DEFAULT


def resolve_backend(model: str) -> str:
    model = (model or CU_MODEL_DEFAULT).strip()
    for backend, pattern in CU_MODEL_FILTERS.items():
        if re.match(pattern, model):
            return backend
    return "anthropic"  # unknown claude-adjacent ids and "" fall through here
```

Test: `resolve_backend("gemini-2.5-computer-use-preview-10-2025") == "google"`, `resolve_backend("") == "anthropic"`, `resolve_backend("computer-use-preview") == "openai"`. In chat_routes both branch sites, replace `if "gemini" in model:` with `backend = resolve_backend(model)` and branch on `backend == "google"` (the openai branch is wired in Task 13). Run suite; commit `feat(cu): catalog-rule backend dispatcher replaces model string sniffing`.

### Task 12: Headless runner + delete legacy loop

> **AMENDMENT (Task 7 quality review):** also in scope here — the Gemini loop body still
> blocks the event loop per step (`executor.execute(...)` runs subprocess + `time.sleep`
> jitter sync; `_capture_screenshot` wraps a blocking `subprocess.run` + PIL resize in an
> `async def`). While consolidating, wrap the sync action execution and screenshot capture
> in `asyncio.to_thread(...)` in the Gemini driver (and keep the Anthropic driver's
> existing threading semantics unchanged) so a running CU step no longer stalls other
> Orchestrator requests for 0.5–2s.

**Files:**
- Create: `Orchestrator/browser/headless.py` — `async def run_cu_task(task_id, operator, prompt, device_id="blackbox", model="", system_prompt=None, url=None) -> dict`
- Modify: `Orchestrator/tasks.py` (~line 284, 998: USE_COMPUTER execution)
- Delete: `Orchestrator/browser/agent_loop.py`
- Modify: `Orchestrator/browser/__init__.py` (drop BrowserSession export if present)

`run_cu_task` composes what `stream_computer_use` does minus SSE: `get_or_create_session(operator, device_id=device_id)`, build system prompt/tools/headers exactly as `stream_computer_use` does (import `COMPUTER_USE_SYSTEM_PROMPT` + `build_cu_context` from chat_routes lazily inside the function to avoid the import cycle), launch `run_anthropic_cu_loop` (or driver per `resolve_backend`), then drain `session.event_queue` until the `None` sentinel, accumulating `screenshots` from `cu_screenshot` events. Return the Task-9 golden contract dict (`success/result_text/screenshots/final_screenshot/steps/tokens`).

Refit `tasks.py` USE_COMPUTER branch to call it (it runs in the worker thread — read how tasks.py bridges async today for GEMINI_CU and reuse: `asyncio.run_coroutine_threadsafe` against the captured loop, per the APScheduler/uvloop lesson in MEMORY.md). Delete `browser/agent_loop.py`; grep for remaining importers: `grep -rn "browser.agent_loop\|BrowserSession" Orchestrator/ Portal/ --include='*.py'` and fix each.

**Verification:** Task-9 golden test still passes UNCHANGED. Full suite green. `$PY -c "import Orchestrator.app"`. Commit `refactor(cu): one CU loop — headless runner for tasks/scheduler, delete legacy BrowserSession`.

---

## Phase 5 — OpenAI CUA driver

### Task 13: Implement `openai_cu/agent_loop.py` + dispatch wiring

**Files:**
- Rewrite: `Orchestrator/openai_cu/agent_loop.py`
- Modify: `Orchestrator/openai_cu/config.py` (display 1280x720 to share the screenshot pipeline)
- Modify: `Orchestrator/routes/chat_routes.py` (openai branch at both dispatch sites → `stream_openai_computer_use`, mirroring the gemini one at ~4149)
- Test: `Orchestrator/tests/test_openai_cu.py` (create)

Loop contract (mirrors the docstring already in the stub): `client.responses.create(model=model, tools=[{"type":"computer_use_preview","display_width":1280,"display_height":720,"environment":"browser"}], input=..., reasoning={"summary":"concise"}, truncation="auto")`; iterate: collect `computer_call` items → execute via `ActionExecutor()` (anthropic-1280 space — screenshots resized to 1280x720 like the Anthropic path) → reply with `computer_call_output` containing a fresh screenshot + `previous_response_id`; pass back `pending_safety_checks` as `acknowledged_safety_checks` and emit a `cu_safety` event. Action map: `click(x,y,button)`→`left_click`/`right_click`; `double_click`→`double_click`; `scroll(x,y,scroll_x,scroll_y)`→`scroll` with direction from sign; `type(text)`→`type`; `keypress(keys:[...])`→`key` joined with `+`; `wait`→sleep 1; `screenshot`→capture only; `drag(path)`→`left_click_drag` first→last. Yield the same event vocabulary as the other drivers (`cu_step`, `cu_action`, `cu_screenshot`, `content`, `usage`, `done`, `error`).

Tests: mock the OpenAI client object entirely (no SDK network); feed a scripted two-step response sequence (one `computer_call` click, then a final text message); assert ActionExecutor received `left_click` with the right coordinates, the second `responses.create` call carried `previous_response_id`, and the loop yielded `done`. TDD steps as usual; full suite; commit `feat(cu): OpenAI CUA driver (Responses API) wired into dispatcher`.

---

## Phase 6 — Portal frontend

> No JS test runner exists in this repo — verification for Phase 6 is `curl` + browser. Keep changes surgical.

### Task 14: Hydrate the CU model dropdown

**Files:**
- Modify: `Portal/modules/state-management.js` (~line 386–462 `MODEL_CONFIG`, `updateModelDropdown`, `fetchAvailableModels` ~line 529)
- Modify: `Portal/index.html` (version bump `?v=genuiXX` — find current XX and increment)

**Steps:**
1. In `fetchAvailableModels`, widen the provider guard to include `"computer-use"`. CU responses carry `backend` per model — preserve it during hydration mapping (`{ id, name, backend }`). For CU, prepend `{ id: "", name: "(Auto - " + defaultName + ")", default: true }` where `defaultName` is the catalog's `default_id` — the backend resolves `""` → `CU_MODEL_DEFAULT` (Task 3).
2. Shrink `MODEL_CONFIG["computer-use"].models` to a 3-entry offline fallback (opus default, gemini CU preview with `backend` fields, plus Auto).
3. In `updateModelDropdown`, when models carry `backend`, group with `<optgroup label="Anthropic|Google|OpenAI">`.
4. Expose the lookup for the drawer: after hydration set `window.__cuModelBackends = Object.fromEntries(models.map(m => [m.id, m.backend]))`.
5. Verify: `curl -s localhost:9091/models/computer-use | python3 -m json.tool` from the MAIN tree (live service) shows the envelope; in the browser, switch provider to Computer Use → dropdown shows grouped live models; kill network → fallback still renders.
6. Commit: `git add Portal/modules/state-management.js Portal/index.html && git commit -m "feat(cu/portal): hydrate CU model dropdown from /models/computer-use with backend groups"`

### Task 15: Drawer — backend lookup, capability device filter, preflight banner

**Files:**
- Modify: `Portal/modules/cu-drawer.js` (`_isGeminiModel` line ~159, `_populateDevices` ~169, `_attachDrawer` ~229)
- Modify: `Portal/styles/features/_browser.css` (banner styles, reuse design tokens)

**Steps:**
1. Replace `_isGeminiModel()` with `_modelBackend()` reading `window.__cuModelBackends[window.__model] || 'anthropic'`; device filter: show ADB devices only when backend === 'google'.
2. On `_attachDrawer()`, `fetch('/cu/preflight?skip_screenshot=true')`; if `status !== 'ok'`, render a `.cu-drawer-banner` row listing each non-ok check's `detail` + `remediation` (warn = amber border, fail = red; tokens: `var(--neutral-*)` + existing radius/space vars only).
3. Verify in browser: with ydotool stopped (`systemctl stop ydotool`) banner shows the daemon remediation; restart it, reload, banner gone.
4. Commit: `git add Portal/modules/cu-drawer.js Portal/styles/features/_browser.css && git commit -m "feat(cu/portal): backend-driven device filter + preflight banner in CU drawer"`

### Task 16: Interactive viewer + responsive pass

**Files:**
- Modify: `Portal/modules/cu-interact.js` (DISPLAY_WIDTH/HEIGHT consts line 16–17)
- Modify: `Portal/styles/features/_browser.css`

**Steps:**
1. `cu-interact.js`: on `open()`, fetch `/browser/status` and use its `cu_resolution` (e.g. `"1280x720"`) for the click-coordinate math instead of the hardcoded consts (keep consts as fallback when fetch fails).
2. CSS: add a `@media (max-width: 640px)` block — `.cu-interact-modal` goes `inset: 0; border-radius: 0`; typing bar `position: sticky; bottom: 0`. Wire `visualViewport.addEventListener('resize', ...)` in cu-interact.js to keep the typing bar above the soft keyboard (translateY by `window.innerHeight - visualViewport.height` when focused). `.cu-drawer-row` gets `flex-wrap: wrap`.
3. Verify: Chrome devtools mobile emulation — open viewer, focus typing input, bar stays visible; drawer doesn't overflow the operator bubble at 360px width.
4. Commit: `git add Portal/modules/cu-interact.js Portal/styles/features/_browser.css && git commit -m "feat(cu/portal): responsive interactive viewer, resolution from /browser/status"`

---

## Phase 7 — Android frontend

> Build verification: `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew assembleDebug`. If gradle isn't runnable on this box, state that explicitly in the task report and flag for Brandon to build in Android Studio — do NOT claim verified.

### Task 17: CU model hydration in `ChatViewModel`

**Files:**
- Modify: `ui/chat/ChatViewModel.kt` (`mapProviderForApi` ~line 1240: add `"computer-use" -> "computer-use"`; `fetchLiveModels` ~1252: parse `backend` into a new `_cuModelBackends: StateFlow<Map<String,String>>`)
- Modify: `util/Constants.kt` (shrink `MODEL_CONFIG["computer-use"]` to 3 entries matching the Portal fallback)
- Modify: `ui/computeruse/CuScreen.kt` (`cuModelsForBackend` ~line 1031: partition by the backend map, fall back to the id-substring heuristic only when the map is empty)

Commit: `feat(cu/android): hydrate CU models from /models/computer-use with backend partition`.

### Task 18: Theme alignment + screen awareness in `CuScreen.kt`

**Files:**
- Modify: `ui/computeruse/CuScreen.kt` (private palette lines 117–127, `aspectRatio` line 541, paddings lines 708/723)
- Modify: the app theme file (locate: `grep -rn "object\|val BbxWhite" ui/theme/ --include='*.kt'`) — add the CU accent colors there once

**Steps:**
1. Move `CuPurple`/`CuPurpleDark`/`CuPurpleDim`/`CuPurpleBg`/`CuPurpleBorder`/`CuGreen`/`CuRed`/`CuOrange` into the theme file as `CuAccent*` tokens; CuScreen imports them (no behavior change, single source).
2. Live view sizing: fetch `/browser/status` once in `CuViewModel.initialize` → store `cuResolutionW/H`; `aspectRatio(cuW/cuH)` with 1280/720 fallback.
3. Replace `contentPadding = PaddingValues(bottom = 160.dp)` and `Spacer(140.dp)` with `WindowInsets.ime.union(WindowInsets.navigationBars)` derived padding (`Modifier.imePadding().navigationBarsPadding()` on the input column).
4. Layout: wrap content in `BoxWithConstraints`; when `maxWidth >= 840.dp` use a `Row` (live view weight 0.6, chat/controls weight 0.4) else current `Column`. (`BoxWithConstraints` avoids adding the material3-window-size-class dependency — YAGNI.)
5. Preflight banner: in `CuViewModel`, `fetchPreflight()` hitting `/cu/preflight?skip_screenshot=true`; a dismissible banner composable above the live view listing non-ok checks with remediation, shown only when `deviceId == "blackbox"`.
6. Build: `./gradlew assembleDebug` — expect BUILD SUCCESSFUL.
7. Commit: `feat(cu/android): theme-token palette, inset-aware paddings, adaptive layout, preflight banner`.

---

## Final phase — merge

### Task 19: Full verification + finish branch

1. Full suite in worktree: `$PY -m pytest -q` — all green.
2. `$PY -m Orchestrator.toolvault.validate` (CI gate, in case tool schemas were touched).
3. Use superpowers:requesting-code-review for the branch diff, fix findings.
4. Use superpowers:finishing-a-development-branch — merge `feat/cu-production-pass` to main, then on the MAIN tree: `sudo systemctl restart blackbox.service` (pre-authorized), wait 60–90s warm-up.
5. Live verify on the appliance: `curl -s localhost:9091/models/computer-use | python3 -m json.tool`; `curl -s localhost:9091/cu/preflight | python3 -m json.tool`; run one real Anthropic CU prompt and one Gemini CU prompt from the Portal on the Wayland desktop; confirm clicks land where expected.
6. `/snapshot-dev` to mint the session snapshot.
