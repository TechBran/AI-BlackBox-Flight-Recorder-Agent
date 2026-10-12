# Terminal File Attach + Web Terminal Parity — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let users attach any file (image/PDF/anything) to a live CLI-agent terminal from a toolbar button on BOTH the web Portal and Android — the file uploads to a server folder and its absolute path is bracketed-pasted into the zellij pane (exactly like dragging a file onto a real terminal) — and bring the web Portal terminal UI to parity with Android: collapsible session rail, maximize control, and a mobile extra-keys bar.

**Architecture:** One new backend endpoint `POST /cli-agent/zellij/attach-file` does upload + store + provider-aware path injection via the probe-verified `zellij action paste --pane-id terminal_0` recipe (native bracketed paste, detached-safe). Both frontends call the same endpoint, so there is exactly ONE injection implementation. Web parity work is host-page chrome around the existing zellij iframe (toolbar, rail collapse, maximize class, extra-keys bar delivering bytes via the existing `/zellij/send-key` relay), plus a SwitchedSession drift fix so injection always targets the session actually on screen.

**Tech Stack:** FastAPI (Orchestrator), vanilla ES modules (Portal), Kotlin/Compose (Android), zellij 0.44.3 (patched local build), pytest, gradle.

---

## 0. Verified research facts (2026-07-20 workflow run, 10 agents)

Everything below was **live-verified on this box**, not inferred. Full findings: session scratchpad `research/` files.

### Injection (the risky part — proven working)
- `zellij action paste --pane-id terminal_0` delivers **native bracketed paste** (ESC[200~…ESC[201~) into a pane — multiline payloads land UNEXECUTED in bash until Enter; byte-perfect with spaces, quotes, UTF-8/CJK. Verified via throwaway session + `dump-screen` read-back.
- `--pane-id terminal_0` is **mandatory**: without it, actions on a detached session return exit 0 but the text is **silently lost** (goes to focused-pane resolution which needs an attached client). BlackBox KDL-launched sessions always contain exactly `plugin_0` + `terminal_0`.
- **Scrub `ZELLIJ*` env vars** before every zellij CLI call: backend processes on this box inherit `ZELLIJ_SESSION_NAME` pointing at a USER session.
- Enter is a separate `action write 13`; we deliberately do NOT send it (drag-drop semantics: user reviews, adds context, presses Enter).
- `POST /cli-agent/zellij/inject` (write-chars, 4096 cap) exists with **zero live consumers** (the launcher's INJECT_URL is defined but never fetched) — its internals can be hardened freely. `POST /cli-agent/zellij/send-key` (raw bytes ≤32 via `action write`) is live — it's the Android Esc relay.

### CLI file ingestion (docs + local CLI verification: claude 2.1.215, gemini 0.43.0, codex 0.144.5)
- **Claude Code:** quoted absolute path in a sentence is an officially documented method for text AND images ("Analyze this image: /path/to/image.png"). `@path` breaks on spaced paths.
- **Gemini CLI:** `@/abs/path` with backslash-escaped spaces = deterministic **multimodal** injection (image/PDF/audio/video). Quotes are NOT honored inside @-tokens.
- **Codex:** bracketed-pasting a bare **image** path auto-attaches it as `[Image #N]`; text files via quoted path in a sentence (view_image consent gate is satisfied because the user provides the path).
- All three have current-or-recent auto-submit-on-newline bugs outside bracketed paste → our payloads are single-line, no trailing newline, delivered as native bracketed paste anyway.

### Backend
- Zellij backend is active (`.env` `CLI_AGENT_BACKEND=zellij`); web terminal = zellij's own web client (xterm.js 5.5.0) served from the patched `~/.local/bin/zellij` binary at `127.0.0.1:9097`, iframed same-origin through `/app-proxy/9097/{session_name}` with a master-token cookie injected by the proxy.
- Session names `{op}__{provider}__{app|root}[__ms][_yolo]` are filesystem-safe; regex `^[A-Za-z0-9._\- ]+$` already enforced in `send_key`.
- Sessions' cwd is ALWAYS the repo root (launch passes no cwd) → **inject absolute paths only**.
- `POST /upload/session` (admin_routes.py:95) already proves the storage contract (multipart → folder → returns absolute path) BUT takes an **unsanitized `session_id`** (path-traversal write/rmtree primitive) — we build a properly-gated sibling and fix that hole.
- The documented "3-day auto-cleanup" of session upload folders is **dead code** (manual `POST /upload/cleanup` only — Dec 2025 folders still on disk). The zellij reaper (hourly, 7-day idle) has no upload hook → new terminal upload folders need explicit lifecycle hooks or they leak forever.
- Orchestrator + zellij sessions run as the same user in the SAME mount namespace (verified via `/proc/*/ns/mnt`) — files written to `Portal/uploads/**` are readable by CLI panes. `ProtectSystem=strict` applies inside panes; `Portal/uploads` is inside `ReadWritePaths`.
- All `/cli-agent/zellij/*` endpoints 503 when the backend degrades to tmux — feature is **zellij-only**; frontends already detect backend and must hide the button on tmux.

### Portal web
- CLI Agents surface = `#cliAgentsModal` → zellij shell built by `enterZellijMode()` (cli-agents-modal.js:477-564): launcher row + `.cli-agents-zellij-main` = fixed `flex: 0 0 220px` switcher rail (NO collapse affordance, NO shrink → terminal unusable at phone width) + iframe host. ZERO media queries, ZERO touch/keyboard handling in all four cli-agents modules.
- No toolbar exists on the terminal (only modal ✕, "+ Terminal", "Shortcuts ▾", rail ✕ deletes). No maximize anywhere in Portal (patterns: app-preview 95vw/90vh card, cu-interact fixed overlay — cu-interact ALSO ships a proven extra-keys typing bar + `visualViewport` soft-keyboard offset handler, cu-interact.js:185-213).
- The iframe is same-origin: `contentWindow.term` (xterm handle) and `contentDocument` are reachable. Mobile IME: tapping the terminal focuses xterm's editable helper-textarea (verified via CDP touch emulation) → soft keyboard WILL raise. Host toolbar buttons steal focus unless they `preventDefault()` on **`pointerdown`** (verified: keeps focus AND click fires; `touchstart`+preventDefault kills the click — do not use).
- **Keyboard-resize gap:** the iframe's inner visualViewport tracks the iframe ELEMENT, not the phone viewport → when the soft keyboard opens, the bottom terminal rows hide behind it unless the HOST resizes the iframe element (zellij-web then chain-refits automatically).
- **SwitchedSession drift (real hazard):** zellij's session-manager (Ctrl+o, w — default keybinds active) lets the user switch/kill/create sessions INSIDE the iframe; the client then navigates the iframe to `/app-proxy/9097/{new_name}` (same-origin, parent-readable) but the Portal's one-shot load listener never sees it → `getCurrentSessionName()` goes stale → attach/inject would target the WRONG session. Detection = persistent `load` listener + `contentWindow.location.pathname`.
- Esc cannot be sent over the terminal WS (zellij-web's parser holds a bare ESC forever) → web extra-keys must relay Esc via `POST /zellij/send-key` exactly like Android.
- Hardcoded `'Brandon'` operator fallbacks at cli-agents-modal.js:323 and :521 (violates operator-resolution rule — fix in passing).
- The launcher's cookie-login step (`POST /app-proxy/9097/command/login`) 422s and is dead since the Phase-5 master-token model — remove in passing.

### Android
- `ZellijTerminalScreen` is the live screen; `TerminalScreen` is legacy-tmux (long-press folder-picker path only) — don't touch it.
- `ExtraKeysBar` already has the exact extension point: **`micSlot`** (a composable slot hosting CliMicButton) — an `attachSlot` is the established idiom. Bar hides entirely when a hardware keyboard is attached (ExtraKeysBar.kt:119-123).
- Mic injection precedent: `client.sendBytes(buildBracketedPaste(text))` — but for attach we call the server endpoint instead so ALL surfaces share one injection implementation (server-side paste is detached-safe and provider-aware; bytes echo back to Android's WS like the Esc relay does).
- File picking: `ActivityResultContracts.GetContent`/`GetMultipleContents` pattern exists (ui/chat/FileAttachment.kt:62-85); upload pattern: copy content-URI to cacheDir temp then OkHttp multipart (`BlackBoxApi.uploadFile`, BlackBoxApi.kt:158-174 — cannot express extra form fields yet; needs a small extension).
- Transient chrome MUST be overlay/Popup, not a Column sibling — sibling height changes trigger a heavyweight debounced zellij session reflow (banner + mic-chip precedents).
- Chat's silent-drop error handling (Log.e + skip) must NOT be copied — attach failures are user-visible; toast loudly.

### Android parity note (scope honesty)
Android has NO collapsible side panel and NO maximize button — its session list is a dropdown from a 40dp top bar and the terminal is effectively always maximized. The web "collapsible rail + maximize" work therefore EXCEEDS Android; the extra-keys bar and attach button are the true parity items. Web sticky Ctrl/Alt is a deliberate near-no-op on Android today (no letter keys in the bar) — web ships sticky **Shift** only, matching real Android behavior.

---

## 1. Design decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | Upload+inject shape | ONE endpoint `POST /cli-agent/zellij/attach-file` (multipart `file` + `session_name` + optional `inject`, `?op=`). Both frontends call it. Server stores AND injects. |
| D2 | Storage location | `Portal/uploads/terminal/{session_name}/{filename}` — new root, sibling of `sessions/` (avoids the unsanitized-session_id namespace, the top-level screenshot sweeper, and the media-gallery auto-indexer, all non-recursive). |
| D3 | Injection mechanism | `zellij action paste --pane-id terminal_0` with `ZELLIJ*`-scrubbed env (probe-verified recipe). No trailing newline — user presses Enter. |
| D4 | Injected text | Provider-aware (provider parsed from session name): `terminal` → `"path" `; `gemini` → `@path` (backslash-escaped spaces); `codex`+image → bare path (auto `[Image #N]`); everything else (claude, grok, …) → `Read this file: "path" `. |
| D5 | Lifecycle | Delete the folder on session DELETE and on reaper kill; hourly reaper pass also sweeps orphan folders (no matching live session, mtime > idle window). |
| D6 | Scope | Zellij backend only (endpoints 503 on tmux; frontends hide the button in tmux mode). Legacy tmux path untouched. |
| D7 | Web extra-keys delivery | Everything via `POST /zellij/send-key` (server-side, sanctioned, ≤32 bytes covers every chord incl. 3× SGR wheel = 30 bytes). No iframe reach-in for input; best-effort `contentWindow.term.focus()` refocus only (try/catch). |
| D8 | Web key set (parity matrix) | Esc (relay), Tab, Enter ⌫, `/`, `@`, `-`, arrows (+Shift variants), sticky **Shift** (one-shot/locked), PgUp/PgDn (Shift → `ESC[5;2~`/`6;2~`, bare → 3× SGR wheel reports), Home/End. No sticky Ctrl/Alt (matches Android reality). |
| D9 | Android placement | `attachSlot` in ExtraKeysBar next to `micSlot` (the established slot idiom). Known trade-off: hidden when a hardware keyboard is attached. |
| D10 | Adjacent hardening | Sanitize `session_id` in the pre-existing `POST /upload/session` + `DELETE /upload/session/{id}` (path-traversal hole); scrub `ZELLIJ*` env in `zellij_client._run` for ALL zellij CLI calls. |

**Open questions for Brandon (recommendations embedded):** (a) D9 placement — also mirror the 📎 in SessionSwitcherTopBar so hardware-keyboard users keep it? Recommended: slot-only first, top-bar later if the gap bites. (b) Auto-submit after inject? Recommended NO (drag-drop semantics). (c) Web extra-keys default: auto-show on `(pointer: coarse)` + manual ⌨ toggle for everyone. Recommended yes.

---

## Milestone 1 — Backend: verified injection primitive + attach-file endpoint + lifecycle

### Task 1: Env-scrub all zellij CLI calls

**Files:**
- Modify: `Orchestrator/cli_agent/zellij_client.py` (`_run`, ~line 150)
- Test: `Orchestrator/tests/test_cli_agent/test_zellij_client.py`

**Step 1: Write the failing test**

```python
def test_run_scrubs_zellij_env(monkeypatch):
    """_run must never let the orchestrator's own ZELLIJ_* env leak into
    zellij CLI subprocesses — an inherited ZELLIJ_SESSION_NAME makes a bare
    action target the USER'S live session (probe 2026-07-20)."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "someones-live-session")
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(zellij_client.subprocess, "run", fake_run)
    zellij_client._run([zellij_client._ZELLIJ_BIN, "--session", "x", "action", "write", "13"])
    assert captured["env"] is not None
    assert not any(k.startswith("ZELLIJ") for k in captured["env"])
    assert "PATH" in captured["env"]
```

**Step 2: Run it — expect FAIL** (`env` is currently None/inherited)

Run: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && python -m pytest Orchestrator/tests/test_cli_agent/test_zellij_client.py -k scrubs_zellij_env -v`

**Step 3: Implement** — in `_run`, build `env = {k: v for k, v in os.environ.items() if not k.startswith("ZELLIJ")}` and pass `env=env` to `subprocess.run`. One comment: why (probe finding).

**Step 4: Re-run test — PASS. Run the whole client suite** (`python -m pytest Orchestrator/tests/test_cli_agent/ -q`) to confirm no regression.

**Step 5: Commit** `fix(cli-agent): scrub ZELLIJ* env from all zellij CLI subprocess calls`

### Task 2: `paste_into_pane` primitive (verified recipe)

**Files:**
- Modify: `Orchestrator/cli_agent/zellij_client.py` (below `send_key`, ~line 221)
- Test: `Orchestrator/tests/test_cli_agent/test_zellij_client.py`

**Step 1: Failing tests**

```python
def test_paste_into_pane_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(zellij_client, "_run", lambda argv, **kw: calls.append(argv))
    zellij_client.paste_into_pane("Brandon__claude__root", 'Read this file: "/a/b c.png" ')
    assert calls[0] == [
        zellij_client._ZELLIJ_BIN, "--session", "Brandon__claude__root",
        "action", "paste", "--pane-id", "terminal_0", 'Read this file: "/a/b c.png" ',
    ]

def test_paste_into_pane_rejects_bad_session():
    with pytest.raises(ValueError):
        zellij_client.paste_into_pane("bad;name", "x")

def test_paste_into_pane_rejects_empty_text():
    with pytest.raises(ValueError):
        zellij_client.paste_into_pane("Brandon__claude__root", "")
```

**Step 2: Run — FAIL (no such function).**

**Step 3: Implement**

```python
def paste_into_pane(session_name: str, text: str, pane_id: str = "terminal_0") -> None:
    """Bracketed-paste ``text`` into a specific pane of a session.

    ``zellij action paste`` wraps the payload in ESC[200~/ESC[201~ natively,
    so TUI agents (claude/gemini/codex) receive ONE paste event and bash
    leaves it unexecuted until the user presses Enter. ``--pane-id`` makes
    delivery detached-safe — focused-pane resolution needs an attached
    client and SILENTLY drops the action otherwise (probe 2026-07-20).
    BlackBox KDL-launched sessions always contain exactly plugin_0 +
    terminal_0, so terminal_0 deterministically addresses the CLI pane.

    SECURITY: caller enforces operator gates; this adapter only injects.
    """
    if not session_name or not _SESSION_NAME_RE.match(session_name):
        raise ValueError(f"invalid zellij session name: {session_name!r}")
    if not text:
        raise ValueError("paste text must be non-empty")
    logger.info("paste_into_pane: session=%s pane=%s text_len=%d", session_name, pane_id, len(text))
    _run([_ZELLIJ_BIN, "--session", session_name, "action", "paste", "--pane-id", pane_id, text])
```

**Step 4: Tests PASS. Step 5: Commit** `feat(cli-agent): add detached-safe bracketed-paste primitive (zellij action paste --pane-id)`

### Task 3: Provider-aware attach text builder

**Files:**
- Modify: `Orchestrator/routes/cli_agent_routes.py` (pure helpers near `_zellij_resume_name`)
- Test: `Orchestrator/tests/test_cli_agent/test_zellij_endpoints.py`

**Step 1: Failing tests** (pure functions — no HTTP plumbing)

```python
from Orchestrator.routes.cli_agent_routes import _provider_from_session_name, _build_attach_text

def test_provider_parse():
    assert _provider_from_session_name("Brandon__claude__root", "Brandon") == "claude"
    assert _provider_from_session_name("Brandon__gemini__myapp__1784542317224_yolo", "Brandon") == "gemini"
    assert _provider_from_session_name("weird", "Brandon") == "unknown"

def test_attach_text_terminal():
    assert _build_attach_text("terminal", "/a/b c.png") == '"/a/b c.png" '

def test_attach_text_gemini_escapes_spaces():
    assert _build_attach_text("gemini", "/a/b c.pdf") == "@/a/b\\ c.pdf "

def test_attach_text_codex_image_bare_path():
    assert _build_attach_text("codex", "/a/shot.png") == "/a/shot.png "

def test_attach_text_codex_textfile_sentence():
    assert _build_attach_text("codex", "/a/notes.txt") == 'Read this file: "/a/notes.txt" '

def test_attach_text_claude_sentence_no_newline():
    out = _build_attach_text("claude", "/a/b.png")
    assert out == 'Read this file: "/a/b.png" '
    assert "\n" not in out
```

**Step 2: FAIL. Step 3: Implement**

```python
_ATTACH_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def _provider_from_session_name(session_name_: str, operator: str) -> str:
    """Parse the provider segment out of '{op}__{provider}__...'.

    Pure function. Returns 'unknown' when the name doesn't carry the
    BlackBox shape (out-of-band sessions) — callers fall back to the
    universal sentence format.
    """
    prefix = f"{operator}__"
    if not session_name_.startswith(prefix):
        return "unknown"
    rest = session_name_[len(prefix):]
    return rest.split("__", 1)[0] if "__" in rest else rest or "unknown"


def _build_attach_text(provider: str, abs_path: str) -> str:
    """Compose the text pasted into the pane for an uploaded file.

    NEVER a trailing newline — the user reviews and presses Enter, the same
    semantics as dragging a file onto a real terminal. Trailing space so
    they can keep typing. Per-CLI formats are the documented deterministic
    ones (research 2026-07-20); anything unknown gets the universal
    quoted-path sentence that all three agents honor.
    """
    if provider == "terminal":
        return f'"{abs_path}" '
    if provider == "gemini":
        # @-token = deterministic multimodal injection; spaces must be
        # backslash-escaped inside the token (quotes are NOT honored there).
        return "@" + abs_path.replace(" ", "\\ ") + " "
    if provider == "codex" and Path(abs_path).suffix.lower() in _ATTACH_IMAGE_EXTS:
        # Codex auto-attaches a bracketed-pasted bare image path as [Image #N].
        return f"{abs_path} "
    return f'Read this file: "{abs_path}" '
```

**Step 4: PASS. Step 5: Commit** `feat(cli-agent): provider-aware attach text builder`

### Task 4: `POST /cli-agent/zellij/attach-file` endpoint

**Files:**
- Modify: `Orchestrator/routes/cli_agent_routes.py` (new endpoint after `/zellij/inject`; imports: `File`, `Form`, `UploadFile` from fastapi, `UPLOADS_DIR` from `Orchestrator.config`, reuse `MAX_UPLOAD_SIZE`/`UPLOAD_CHUNK_SIZE` constants — import from `admin_routes` or duplicate the two ints with a comment)
- Test: `Orchestrator/tests/test_cli_agent/test_zellij_endpoints.py`

**Step 1: Failing tests** (follow the existing endpoint-test style in that file — FastAPI TestClient with monkeypatched `zellij_client` + backend forced to zellij)

```python
def test_attach_file_stores_and_injects(client, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_agent_routes, "_TERMINAL_UPLOADS_DIR", tmp_path)
    pasted = {}
    monkeypatch.setattr(cli_agent_routes.zellij_client, "paste_into_pane",
                        lambda s, t: pasted.update(session=s, text=t))
    r = client.post(
        "/cli-agent/zellij/attach-file?op=Brandon",
        files={"file": ("my shot.png", b"\x89PNG fake", "image/png")},
        data={"session_name": "Brandon__claude__root"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["injected"] is True
    assert body["filename"] == "my shot.png"
    saved = tmp_path / "Brandon__claude__root" / "my shot.png"
    assert saved.read_bytes() == b"\x89PNG fake"
    assert body["path"] == str(saved.resolve())
    assert pasted["session"] == "Brandon__claude__root"
    assert pasted["text"] == f'Read this file: "{saved.resolve()}" '

def test_attach_file_cross_operator_403(client):
    r = client.post(
        "/cli-agent/zellij/attach-file?op=Anna",
        files={"file": ("x.txt", b"hi", "text/plain")},
        data={"session_name": "Brandon__claude__root"},
    )
    assert r.status_code == 403

def test_attach_file_bad_session_name_400(client):
    r = client.post(
        "/cli-agent/zellij/attach-file?op=Brandon",
        files={"file": ("x.txt", b"hi", "text/plain")},
        data={"session_name": "Brandon__claude__root; rm -rf /"},
    )
    assert r.status_code == 400

def test_attach_file_upload_survives_inject_failure(client, tmp_path, monkeypatch):
    """Honest degradation: file stored + path returned even when the paste
    fails (e.g. session died mid-upload) — injected:false, HTTP 200."""
    monkeypatch.setattr(cli_agent_routes, "_TERMINAL_UPLOADS_DIR", tmp_path)
    def boom(s, t): raise RuntimeError("session gone")
    monkeypatch.setattr(cli_agent_routes.zellij_client, "paste_into_pane", boom)
    r = client.post(
        "/cli-agent/zellij/attach-file?op=Brandon",
        files={"file": ("x.txt", b"hi", "text/plain")},
        data={"session_name": "Brandon__claude__root"},
    )
    assert r.status_code == 200
    assert r.json()["injected"] is False
```

Also: collision test (same filename twice → second gets `_{timestamp}` suffix), oversize test if the existing suite has a pattern for it (413).

**Step 2: FAIL. Step 3: Implement**

```python
_TERMINAL_UPLOADS_DIR = UPLOADS_DIR / "terminal"  # Portal/uploads/terminal/{session_name}/


@router.post("/zellij/attach-file")
async def zellij_attach_file(
    file: UploadFile = File(...),
    session_name: str = Form(...),
    inject: bool = Form(True),
    op: str = Query(...),
):
    """Upload a file for a terminal session and bracketed-paste its absolute
    path into the pane — the server-side twin of dragging a file onto a
    real terminal. One implementation shared by Portal web and Android.

    Security boundary mirrors /zellij/inject (audit I10): operator
    allowlist + '{op}__' prefix gate + session-name charset; storage is
    keyed by the (validated) session name so there is no client-chosen
    path component at all.
    """
    _check_operator_allowed(op)
    if not _validate_operator_prefix(session_name, op):
        raise HTTPException(403, "Cannot attach to a session belonging to another operator")
    if not zellij_client.is_valid_session_name(session_name):
        raise HTTPException(400, f"Invalid session name: {session_name!r}")
    _require_zellij_backend()

    folder = _TERMINAL_UPLOADS_DIR / session_name
    folder.mkdir(parents=True, exist_ok=True)
    original_name = Path(file.filename or "attachment").name
    save_path = folder / original_name
    if save_path.exists():
        save_path = folder / f"{Path(original_name).stem}_{int(time.time())}{Path(original_name).suffix}"

    total = 0
    with open(save_path, "wb") as f:
        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_UPLOAD_SIZE:
                f.close()
                save_path.unlink(missing_ok=True)
                raise HTTPException(413, f"File too large (max {MAX_UPLOAD_SIZE // (1024*1024)}MB)")
            f.write(chunk)

    abs_path = str(save_path.resolve())
    provider = _provider_from_session_name(session_name, op)
    text = _build_attach_text(provider, abs_path)

    injected = False
    if inject:
        try:
            await asyncio.to_thread(zellij_client.paste_into_pane, session_name, text)
            injected = True
        except Exception as exc:  # noqa: BLE001 — upload already succeeded; degrade honestly
            logger.error("attach-file: paste into %s failed: %s", session_name, exc, exc_info=True)

    logger.info("attach-file: operator=%s session=%s file=%s bytes=%d injected=%s",
                op, session_name, save_path.name, total, injected)
    return {
        "url": f"/ui/uploads/terminal/{session_name}/{save_path.name}",
        "path": abs_path,
        "filename": save_path.name,
        "session_folder": str(folder.resolve()),
        "provider": provider,
        "injected": injected,
    }
```

Add tiny `is_valid_session_name(name) -> bool` to `zellij_client` (wraps `_SESSION_NAME_RE`) so routes don't import the private regex.

**Step 4: PASS + full suite green. Step 5: Commit** `feat(cli-agent): attach-file endpoint — upload + provider-aware path paste into terminal`

### Task 5: Upload-folder lifecycle (delete + reaper hooks + orphan sweep)

**Files:**
- Modify: `Orchestrator/routes/cli_agent_routes.py` (DELETE `/zellij/sessions/{name}`, ~line 1001-1082)
- Modify: `Orchestrator/cli_agent/reaper.py` (after each reap kill; plus orphan sweep in the hourly pass)
- Test: `Orchestrator/tests/test_cli_agent/test_zellij_endpoints.py`, `Orchestrator/tests/test_cli_agent/test_reaper.py`

**Step 1: Failing tests** — (a) DELETE endpoint removes `_TERMINAL_UPLOADS_DIR/{name}`; (b) reaper reap removes the folder; (c) orphan sweep: folder with no live session and old mtime → removed; folder for a live session → kept; young orphan (< idle window) → kept (grace for EXITED-but-resurrectable sessions).

**Step 3: Implement** — a shared `remove_terminal_uploads(session_name)` helper (rmtree ignore_errors, log) called from both sites via `asyncio.to_thread`/directly in the reaper thread; orphan sweep iterates `_TERMINAL_UPLOADS_DIR.iterdir()` against the live `zellij list-sessions` set the reaper already fetches.

**Step 5: Commit** `feat(cli-agent): terminal upload folders die with their session (delete + reap + orphan sweep)`

### Task 6: Adjacent hardening — sanitize `/upload/session` session_id

**Files:**
- Modify: `Orchestrator/routes/admin_routes.py:95-201` (upload, list, delete)
- Test: `Orchestrator/tests/` (nearest admin-routes test module; create `test_admin_upload.py` if none)

**Step 1: Failing test** — `session_id="../../etc"` → 400 on all three endpoints; a normal `session-1734-abc` and a UUID still work.

**Step 3: Implement** — module-level `_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")` + a 400 guard at the top of the three handlers (matches both Portal id shapes: `crypto.randomUUID()` and `session-{ts}-{rand}`). One comment: this was a path-traversal write/rmtree primitive behind the Tailscale perimeter.

**Step 5: Commit** `fix(uploads): reject path-traversal session_id on /upload/session endpoints`

### Task 7: Live backend smoke (dev box)

**Step 1:** restart the service (pre-authorized): `sudo systemctl restart blackbox.service` (wait for warm-up), then:

```bash
OP=<first operator from GET /operators — do not hardcode>
# launch a scratch terminal session
curl -s -X POST "http://localhost:9091/cli-agent/zellij/launch?op=$OP" -H 'Content-Type: application/json' -d '{"provider":"terminal","fork":true}'
# attach a real file
echo "hello attach" > /tmp/attach-smoke.txt
curl -s -X POST "http://localhost:9091/cli-agent/zellij/attach-file?op=$OP" -F file=@/tmp/attach-smoke.txt -F session_name=<name from launch>
# verify the path landed on the pane's edit line (probe recipe)
env -u ZELLIJ -u ZELLIJ_SESSION_NAME -u ZELLIJ_PANE_ID zellij --session <name> action dump-screen --pane-id terminal_0
# expect: "/…/Portal/uploads/terminal/<name>/attach-smoke.txt" visible, NOT executed
# cleanup
curl -s -X DELETE "http://localhost:9091/cli-agent/zellij/sessions/<name>?op=$OP"
ls Portal/uploads/terminal/   # folder gone
```

**Step 2: Commit nothing — this is verification.** Record output in the session.

---

## Milestone 2 — Portal web: terminal toolbar, attach flow, session-drift fix

### Task 8: SwitchedSession drift fix (prerequisite for correct attach targeting)

**Files:**
- Modify: `Portal/modules/cli-agents-zellij-iframe.js` (persistent load listener; new `onSessionChanged` callback in mount options)
- Modify: `Portal/modules/cli-agents-modal.js` (wire callback → `markSessionActive(newName)` on the rail + toolbar label)

**Implementation sketch:** on iframe `load` (persistent listener, NOT one-shot), read `iframe.contentWindow.location.pathname` (try/catch — same-origin via app-proxy), strip the `/app-proxy/9097/` prefix, `decodeURIComponent` → if ≠ `currentSessionName`, update it and fire `onSessionChanged(name)`. Keep the existing one-shot loading-overlay logic untouched. Also fix the stale same-name guard (`loadSession` skip at :119-122) to compare against the tracked-live name.

**Verify:** open two sessions, switch inside the iframe via Ctrl+o → w, confirm rail highlight follows and `getCurrentSessionName()` returns the on-screen session.

**Commit** `fix(portal): track in-iframe zellij session switches (rail highlight + inject targeting)`

### Task 9: Terminal toolbar (attach 📎 + maximize ⛶ + rail toggle ☰ + ⌨ keys toggle)

**Files:**
- Create: `Portal/modules/cli-agents-terminal-bar.js`
- Modify: `Portal/modules/cli-agents-modal.js` (`enterZellijMode()` — wrap iframe host in a column: `.cli-agents-zellij-terminal-col` = toolbar + iframe-host)
- Modify: `Portal/styles/features/_cli_agents_modal.css`

**Toolbar contract:** `mountTerminalBar(containerEl, {getSessionName, operator, onToggleRail, onToggleMaximize, onToggleKeys, onError})`. Buttons are 36px targets; **every button uses `pointerdown → e.preventDefault()`** so terminal focus/IME survive taps (verified pattern; never `touchstart`). Attach + keys buttons disabled when `getSessionName()` is null. Session-name label (ellipsized, title=full name) doubles as the drift indicator.

**Attach flow (in the same module):** hidden `<input type="file" multiple>` → per file: `FormData {file, session_name}` → `XMLHttpRequest` POST `/cli-agent/zellij/attach-file?op=` with `upload.progress` → progress chip overlaid on the iframe host (absolute-positioned — mirrors the loading overlay, no layout shift) → on done: if `injected` toast `📎 name — path pasted into terminal`, else toast the returned path with a copy button (honest degradation). Errors: loud toast, never silent.

**Maximize:** toggle `.cli-agents-maximized` on `.modal-card.cli-agents-card`:

```css
.cli-agents-card.cli-agents-maximized {
  position: fixed; inset: 0; width: 100vw; max-width: 100vw;
  height: 100dvh; max-height: 100dvh; border-radius: 0; margin: 0;
}
.cli-agents-card.cli-agents-maximized .cli-agents-zellij-shell {
  height: auto; flex: 1 1 auto; max-height: none;
}
```

(zellij-web self-refits on iframe resize — no extra plumbing.)

**Rail collapse:** toggle `.rail-collapsed` on `.cli-agents-zellij-shell`; CSS collapses `.cli-agents-zellij-switcher-host` to `flex-basis: 0; width: 0; overflow: hidden;` with a width transition. Persist in `localStorage['cliAgentsRailCollapsed']`; re-apply on `enterZellijMode()` (state must survive the modal unmount/remount cycle).

**Commit** `feat(portal): terminal toolbar — attach, maximize, collapsible session rail`

### Task 10: Drag-and-drop attach onto the terminal

**Files:** `Portal/modules/cli-agents-terminal-bar.js` (+ modal wiring)

`dragenter/dragover` on the modal card (zellij mode + active session) → full-card overlay "Drop files to attach to this terminal" (pointer-events trick so the iframe doesn't eat the drop); `drop` → same upload pipeline as Task 9. This is the literal "drag a file into the terminal" parity the feature is named for.

**Commit** `feat(portal): drag-and-drop files onto the terminal to attach`

### Task 11: Operator + dead-login cleanup (in passing)

**Files:** `Portal/modules/cli-agents-modal.js:323,521`, `Portal/modules/cli-agents-zellij-launcher.js:151-181,230`

- Replace both hardcoded `'Brandon'` fallbacks with the Portal's canonical operator source (grep how chat modules resolve the active operator — reuse that getter; no literals).
- Delete the dead cookie-bridge login step (`POST /app-proxy/9097/command/login` 422s under the master-token model; launch returns `token:null`) and its token checks. The proxy injects the cookie — the iframe needs no login.

**Commit** `chore(portal): dynamic operator in CLI agents modal; remove dead zellij cookie-login step`

---

## Milestone 3 — Portal mobile: extra-keys bar + soft-keyboard handling + responsive layout

### Task 12: Extra-keys bar module

**Files:**
- Create: `Portal/modules/cli-agents-extra-keys.js`
- Modify: `Portal/modules/cli-agents-modal.js` (mount under the iframe host inside `.cli-agents-zellij-terminal-col`)
- Modify: `Portal/styles/features/_cli_agents_modal.css`

**Key table (byte arrays for `POST /cli-agent/zellij/send-key {session, bytes}`):**

| Key | bytes | Shift-armed bytes |
|---|---|---|
| Esc | `[27]` (relay — the endpoint exists for exactly this) | same |
| Tab | `[9]` | — |
| ↵ | `[13]` | — |
| ⌫ | `[127]` | — |
| `/` `@` `-` | `[47]` `[64]` `[45]` | — |
| ← ↓ ↑ → | `[27,91,68/66/65/67]` | `[27,91,49,59,50,68/66/65/67]` |
| PgUp | 3× SGR wheel-up `ESC[<64;1;1M` = `[27,91,60,54,52,59,49,59,49,77] ×3` (30 bytes, one call) | `[27,91,53,59,50,126]` (`ESC[5;2~`) |
| PgDn | 3× `ESC[<65;1;1M` ×3 | `[27,91,54,59,50,126]` |
| Home / End | `[27,91,72]` / `[27,91,70]` | — |
| Shift | sticky: tap = one-shot, double-tap/long-press = locked (visual states) | — |

Layout mirrors Android: Esc pinned left OUTSIDE the horizontally-scrollable strip (the "dead Esc" lesson), then one scrollable row. All buttons: `pointerdown preventDefault` + after send, best-effort `iframe.contentWindow.term?.focus()` in try/catch. Visibility: shown when `matchMedia('(pointer: coarse)')` matches, toggleable for everyone via the toolbar ⌨ button (persist in localStorage).

**Commit** `feat(portal): mobile extra-keys bar for the zellij terminal (Android parity)`

### Task 13: Soft-keyboard occlusion + phone-width layout

**Files:** `Portal/modules/cli-agents-modal.js`, `_cli_agents_modal.css`

- **visualViewport handler** (copy the cu-interact.js:185-213 pattern, attach on `enterZellijMode`, detach on close): when the soft keyboard overlaps, set the shell's height to fit `window.visualViewport.height` so the extra-keys bar and terminal bottom rows stay visible; the iframe element shrinks → zellij-web chain-refits and pushes new dimensions to the server (verified mechanism).
- **Media query ≤600px:** modal full-bleed (100vw/100dvh radius 0 — same treatment app-preview uses), rail default-collapsed, and when expanded it overlays the terminal as a drawer (`position: absolute; z-index` above iframe host) instead of squeezing the 220px basis into the row.

**Verify (Chrome DevTools mobile emulation + real Fold browser over Tailscale):** tap terminal → IME raises; type; toolbar/keys taps don't collapse the keyboard; bottom row never hidden; rail drawer opens/closes.

**Commit** `feat(portal): phone-width terminal layout + soft-keyboard viewport handling`

### Task 14: Version bump + web validation pass

- Bump `?v=genui318` in `Portal/index.html` (both the entry script and any touched asset params).
- Desktop pass: launch claude session → 📎 a PNG → path appears on claude's prompt → Enter → claude reads/views it. Repeat for gemini (`@path` lands), codex (image auto-`[Image #N]`), terminal (bare quoted path, NOT executed).
- Update `Portal/zellij-iframe-smoke.html` to the master-token contract (remove login step) or delete it (it's untracked and stale) — decide with Brandon at review.

**Commit** `chore(portal): version bump genui318`

---

## Milestone 4 — Android: attach button in the ExtraKeysBar

Base: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/` (quote the path — parens/spaces; repo has mixed CRLF/LF — match each file's existing endings).

### Task 15: `BlackBoxApi` multipart with extra form fields

**Files:**
- Modify: `app/src/main/java/com/aiblackbox/portal/data/api/BlackBoxApi.kt:158-174`
- Test: `app/src/test/java/com/aiblackbox/portal/data/api/` (new `BlackBoxApiUploadTest.kt` with OkHttp `MockWebServer` if the dep exists; otherwise a pure request-builder test)

Add optional `fields: Map<String, String> = emptyMap()` to `uploadFile` (each entry → `addFormDataPart(k, v)`); existing call sites unchanged. Also parse FastAPI `detail` on error for a real toast message (the current method returns raw body).

**Step: run the offline unit gate** — `./gradlew :app:testDebugUnitTest --offline` (~35s; local.properties present).

**Commit** `feat(android): multipart form fields on BlackBoxApi.uploadFile`

### Task 16: `CliAttachButton` composable + `attachSlot` in ExtraKeysBar

**Files:**
- Create: `app/src/main/java/com/aiblackbox/portal/ui/cli_agent/CliAttachButton.kt`
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/cli_agent/ExtraKeysBar.kt` (add `attachSlot: @Composable () -> Unit = {}` param, rendered as a LazyRow item adjacent to `micSlot`)
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/cli_agent/ZellijTerminalScreen.kt` (pass the slot, wired to the live client's `currentSessionName` — tracks SwitchedSession — and the resolved operator)

**CliAttachButton behavior (mirror CliMicButton's structure):**
- 📎 IconButton; `rememberLauncherForActivityResult(GetMultipleContents())` with `"*/*"`.
- Per picked URI (sequential): resolve DISPLAY_NAME + SIZE via ContentResolver (500MB gate — the NativeMainActivity.kt:244-272 pattern), copy to `cacheDir` temp, `api.uploadFile("/cli-agent/zellij/attach-file?op=$operator", tempFile, fields = mapOf("session_name" to sessionName))`, delete temp in `finally`.
- Progress: indeterminate spinner replacing the icon + a zero-layout-space `Popup` chip ("Uploading name…") — the mic-chip precedent; NEVER a Column sibling (reflow churn).
- Result: `injected:true` → chip flashes "📎 name attached"; `injected:false` → Toast with the returned path ("Uploaded — paste failed, path: …"); exception → loud Toast with the FastAPI detail. No silent drops.

**Unit tests:** pure helpers only (e.g. result-message selection from a parsed response) in `ExtraKeysBarTest`-style plain JUnit; UI behavior is device-QA (connected tests are connected-only on this box).

**Commit** `feat(android): attach-file button in the terminal extra-keys bar`

### Task 17: Android build + device validation (Fold)

- `./gradlew :app:testDebugUnitTest --offline` green, then assemble and sideload per the worktree-APK method (memory: on-device test method).
- Live QA on the Fold: claude session → 📎 → pick a photo → path pastes onto claude's prompt → Enter → claude views the image. Kill session → folder gone on the box. Hardware-keyboard case: bar hides (known trade-off, per D9).

---

## Milestone 5 — Cross-surface validation + close-out

### Task 18: End-to-end matrix

| Surface | claude | gemini | codex | terminal |
|---|---|---|---|---|
| Portal desktop | path sentence → Read/Image | `@path` multimodal | image → `[Image #N]`; txt → sentence | quoted path, unexecuted |
| Portal mobile (Fold browser) | ✓ + IME/keys bar usable | ✓ | ✓ | ✓ |
| Android app | ✓ | ✓ | ✓ | ✓ |

Plus: inject-after-in-iframe-switch targets the NEW session (Task 8 proof); attach disabled with no session; tmux-mode hides the button; upload folder dies with the session; spaced filenames work end-to-end on every provider.

### Task 19: Close-out
- Full backend suite: `python -m pytest Orchestrator/tests/test_cli_agent Orchestrator/tests/test_admin_upload.py -q`.
- Fresh-box check: no hardcoded operators/hosts introduced; `Portal/uploads/terminal/` auto-creates; feature degrades honestly when zellij backend is down (503 → toast).
- Update `CLAUDE.md` if the (false) "3-day auto-cleanup" claim should be corrected while we're here (flag to Brandon).
- `/snapshot-dev` the session; push per staging-box discipline (explicit paths in commits — never `git add -A`).

---

## Deferred / explicitly out of scope
- Camera capture on Android (TakePicture contract) — follow-up.
- Sticky Ctrl/Alt composing with typed letters on web — exceeds Android parity; revisit if asked.
- tmux-legacy attach support (`{type:'paste'}` WS frame exists if ever needed).
- Scheduling the dead `/upload/cleanup` sweep — separate decision (lifetime semantics differ from terminal uploads; raised to Brandon).
- In-iframe session-manager operator-isolation gap (cross-operator attach via zellij's own picker) — pre-existing, documented in research; Tailscale-perimeter scope decision for Brandon.
