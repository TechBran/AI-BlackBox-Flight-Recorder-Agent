# Google Workspace Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Build on `main` (staging-as-prod, NO worktrees). Stage explicit paths only, never `git add -A`. Commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` via `-F -` heredocs (no backticks). Edit tool is BLOCKED under this dir (`c./` false-positive) → edit via Python-in-Bash, verbatim-anchored; CHECK + PRESERVE line endings per file (`grep -c $'\r'`). Push = ship AFTER live validation (which here needs Brandon's one-time re-consent).

**Goal:** Add Google Docs/Sheets/Slides/Drive/Calendar as first-party ToolVault tools through the SAME Google OAuth flow Gmail uses, with full structural editing (raw `batchUpdate` passthrough).

**Architecture:** Extend `Orchestrator/gmail/service.py` `SCOPES` + factor a shared `_get_credentials(operator)` + add per-API service builders; new `Orchestrator/google_workspace/` helper modules per API; ToolVault tool modules (3 docs / 4 sheets / 3 slides / 4 drive / 5 calendar); a GENERIC MCP routing path (any mcp-group tool → `/local/tools/execute`); onboarding extends the Gmail step to "Google Workspace" (re-consent).

**Tech Stack:** `google-api-python-client` (`build("docs","v1")` etc. — already used by Gmail), `google-auth`, FastAPI, ToolVault v2 modules, pytest, vanilla-JS onboarding.

**Design:** `docs/plans/2026-06-20-google-workspace-design.md`. **Run tests:** `Orchestrator/venv/bin/python -m pytest <path> -v`. Full suite MUST include `Orchestrator/tools`.

**Migration note for ALL tasks:** existing operators' Gmail tokens lack the new scopes; the tools won't work live until Brandon RE-CONSENTS (Task 9). Until then, unit tests mock the Google services; that's expected.

---

## Task 1: OAuth scope extension + shared creds + per-API service builders

**Files:** Modify `Orchestrator/gmail/service.py`; Test `Orchestrator/tests/test_google_services.py` (create).

- Extend `SCOPES` (line 17) to add: `https://www.googleapis.com/auth/drive`, `.../auth/documents`, `.../auth/spreadsheets`, `.../auth/presentations`, `.../auth/calendar` (keep the 2 gmail scopes).
- Factor the creds-building out of `get_gmail_service` into `_get_credentials(operator) -> Credentials | None` (the lines that build `Credentials(...)`, refresh-if-expired, save refreshed token). `get_gmail_service` becomes `creds=_get_credentials(operator); return build("gmail","v1",...) if creds else None`.
- Add builders reusing `_get_credentials`: `get_docs_service` (`build("docs","v1")`), `get_sheets_service` (`build("sheets","v4")`), `get_slides_service` (`build("slides","v1")`), `get_drive_service` (`build("drive","v3")`), `get_calendar_service` (`build("calendar","v3")`). All `cache_discovery=False`.
- Add a sentinel for the auth-missing UX: a small helper `workspace_connected(operator) -> bool` (token present + has a refresh_token) the tools can check to return a clear "not connected" message.

**TDD:** monkeypatch `load_tokens` to return a fake token + monkeypatch `build` to record `(api, version)`; assert each builder calls `build` with the right api/version and returns its result; assert `_get_credentials` returns None when no token. Confirm `get_gmail_service` still works (back-compat). Confirm `build` is importable (the google client is in the venv — `Orchestrator/venv/bin/python -c "from googleapiclient.discovery import build; print('ok')"`).
**Commit:** `feat(google): workspace scopes + shared creds + docs/sheets/slides/drive/calendar service builders`.

---

## Task 2: Docs helpers + tools

**Files:** Create `Orchestrator/google_workspace/__init__.py`, `Orchestrator/google_workspace/docs.py`; Create `ToolVault/tools/{create_doc,read_doc,docs_batch_update}/{schema.json,executor.py}`; Test `Orchestrator/tests/test_google_docs.py`.

`docs.py` helpers (each takes `operator`, returns a dict; raise/return a clear error if `get_docs_service` is None):
- `create_doc(operator, title, text=None)` → `documents().create(body={"title":title})`; if `text`, follow with a `batchUpdate` insertText at index 1. Return `{document_id, title, url}`.
- `read_doc(operator, document_id)` → `documents().get(documentId=...)`; return a structured summary INCLUDING element IDs + start/end indices (so the model can target edits) + the plain text.
- `docs_batch_update(operator, document_id, requests)` → `documents().batchUpdate(documentId=..., body={"requests": requests})`; return the API reply. `requests` is the RAW Google Docs requests array (full structural).

Tool modules: `create_doc(title, text?)`, `read_doc(document_id)`, `docs_batch_update(document_id, requests)`. Schema `category:"google_workspace"`, groups `["chat","chat_cu","realtime","gemini_live","grok_live","phone","mcp"]`, tier 2. The `docs_batch_update` schema describes `requests` as "a Google Docs API batchUpdate requests array" with 1-2 worked examples (insertText, updateTextStyle) in `notes`. Executors mirror the gmail executor shape: resolve operator, call the `docs.py` helper, return `ToolResult(success, json.dumps(result))`; on not-connected return `ToolResult(False, "Google Workspace not connected for <operator> — connect in onboarding")`.

**TDD:** monkeypatch `get_docs_service` with a fake service whose `.documents().create()/get()/batchUpdate().execute()` return canned dicts; assert each helper calls the right method with the right body (esp. `docs_batch_update` forwards `requests` verbatim) + the not-connected path. Tool modules load + validate.
**Commit:** `feat(google): Docs helpers + create_doc/read_doc/docs_batch_update tools`.

---

## Task 3: Sheets helpers + tools
**Files:** `Orchestrator/google_workspace/sheets.py`; `ToolVault/tools/{create_spreadsheet,read_sheet,update_sheet_values,sheets_batch_update}/`; Test `test_google_sheets.py`.
Helpers: `create_spreadsheet(operator,title)`; `read_sheet(operator,spreadsheet_id,range=None)` (`spreadsheets().values().get`); `update_sheet_values(operator,spreadsheet_id,range,values)` (`values().update`, `valueInputOption="USER_ENTERED"`); `sheets_batch_update(operator,spreadsheet_id,requests)` (raw `spreadsheets().batchUpdate`). Tools mirror Task 2. TDD same pattern (fake sheets service). **Commit:** `feat(google): Sheets helpers + tools (incl. raw batchUpdate)`.

## Task 4: Slides helpers + tools
**Files:** `Orchestrator/google_workspace/slides.py`; `ToolVault/tools/{create_presentation,read_presentation,slides_batch_update}/`; Test `test_google_slides.py`.
Helpers: `create_presentation(operator,title)`; `read_presentation(operator,presentation_id)` (return slide + element/object IDs for edit targeting); `slides_batch_update(operator,presentation_id,requests)` (raw `presentations().batchUpdate`). Tools mirror. TDD. **Commit:** `feat(google): Slides helpers + tools (incl. raw batchUpdate)`.

## Task 5: Drive helpers + tools
**Files:** `Orchestrator/google_workspace/drive.py`; `ToolVault/tools/{search_drive_files,get_drive_file,create_drive_file,delete_drive_file}/`; Test `test_google_drive.py`.
Helpers: `search_drive_files(operator,query=None,page_size=20)` (`files().list(q=...)`); `get_drive_file(operator,file_id)` (metadata; for Google-native types use `files().export`, for binary `files().get_media` — return text where feasible, else metadata + note); `create_drive_file(operator,name,mime_type,content=None)` (`files().create` with media if content); `delete_drive_file(operator,file_id)`. Tools mirror. TDD. **Commit:** `feat(google): Drive helpers + tools (any file type)`.

## Task 6: Calendar helpers + tools
**Files:** `Orchestrator/google_workspace/calendar.py`; `ToolVault/tools/{create_event,list_events,update_event,delete_event,list_calendars}/`; Test `test_google_calendar.py`.
Helpers: `create_event(operator, summary, start, end, calendar_id="primary", description=None, attendees=None, location=None)` (`events().insert`; accept RFC3339 or date); `list_events(operator, time_min, time_max, calendar_id="primary")` (`events().list(singleEvents=True, orderBy="startTime")`); `update_event(operator, event_id, calendar_id="primary", **fields)` (`events().patch`); `delete_event(operator, event_id, calendar_id="primary")`; `list_calendars(operator)` (`calendarList().list`). Tools mirror. TDD. **Commit:** `feat(google): Calendar helpers + CRUD tools`.

---

## Task 7: Generic MCP routing (route any mcp-group ToolVault tool via /local/tools/execute)

**Files:** Modify `MCP/blackbox_mcp_server.py` (CRLF — PRESERVE); Test note in report.

Today `call_tool` is an explicit if/elif chain + a `WEB_SEARCH_TOOL_NAMES` set hopping to `/local/tools/execute`. Generalize: in `call_tool`, BEFORE the final "Unknown tool" else, add a GENERIC catch-all — if the requested `name` is in the live `get_mcp_tools()` set (i.e. it's a real ToolVault tool) and isn't already handled above, hop it to `/local/tools/execute` (same body shape: `{tool, params (minus operator), operator}`). This makes ALL ~18 google tools (and any future ToolVault tool) MCP-callable with real creds on the full backend, with NO per-tool MCP edits — Brandon's "full feature suite over MCP." Keep the existing web_search/gmail/web_fetch handling (or fold web_search into the generic path). Verify lean-venv import still works (the catch-all uses only httpx + the existing get_mcp_tools).
**Verify:** `cd MCP && BLACKBOX_ROOT=<root> venv/bin/python -c "import importlib; m=importlib.import_module('blackbox_mcp_server'); n=[t.name for t in m.get_mcp_tools()]; print('docs_batch_update' in n, 'create_event' in n)"` → True True (after Task 2/6 land + reload).
**Commit:** `feat(mcp): generic ToolVault-tool routing via /local/tools/execute`.

---

## Task 8: Onboarding — Gmail step → "Google Workspace"

**Files:** Modify the Gmail onboarding step (`Portal/onboarding/steps/<gmail step>.js` — locate it; it's the connect flow using `/auth/gmail/authorize`) + any backend label in `routes/onboarding_routes.py` `/current-config` (the `gmail` block). Mirror the EXISTING Gmail connect UX exactly — same authorize/launch/status — just relabel to "Google Workspace (Gmail + Docs + Sheets + Slides + Calendar)" and, when already connected, show a "Reconnect to grant Docs/Calendar" affordance (because the existing token lacks the new scopes). No new validator. Bump the Portal cache version if a main-app asset changed. (Onboarding is Portal-only; tools reach Android/voice via injection.)
**Verify:** the step still drives `/auth/gmail/authorize` → consent → `/callback`; manual checklist (connect → consent shows the new scopes → status connected). **Commit:** `feat(onboarding): Google Workspace step (Gmail + Docs/Sheets/Slides/Calendar, re-consent)`.

---

## Task 9: Integration — reload + auth-UX + live smoke (re-consent gate) + final review + push

1. `grep` sweep; `sudo systemctl restart blackbox.service`; `POST /toolvault/reload` (embed the new tools; expect tool_count +~18); confirm validate exit 0 + `test_validate_all_real_tree_ok` passes (add the tools to embeddings via reload).
2. Full suite incl. `Orchestrator/tools` → green except known pre-existing (`test_ollama_keep_alive_passthrough`).
3. Auth-missing UX: with an operator NOT re-consented, a google tool returns the clear "connect Google" message (not a 500) — verify.
4. Final whole-diff `superpowers:code-reviewer` (holistic: scope correctness, batch_update passthrough verbatim, per-operator creds, auth-missing UX, no key/token leak in errors, MCP generic routing, onboarding mirrors Gmail, lean-venv).
5. **LIVE VALIDATION GATES THE PUSH — needs Brandon's one-time re-consent.** Brandon re-runs the Google sign-in (grants the new scopes), then smoke: create_doc / update_sheet_values / create_presentation / create_event + list / create_drive_file — end-to-end + one via MCP. THEN push.
6. Memory (`project_google_workspace.md` + MEMORY.md) + `/snapshot-dev`.

---

## Post-review follow-ups (non-blocking)
- Rich `read_*` element-ID surfacing depth (enough for edit targeting in v1; refine from real model use).
- Drive binary download/export coverage by mime type.
- Token store rename gmail_tokens→google_tokens (cosmetic; back-compat now).
