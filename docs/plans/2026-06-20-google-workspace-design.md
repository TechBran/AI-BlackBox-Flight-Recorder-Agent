# Google Workspace Integration (Docs + Sheets + Slides + Drive + Calendar) — Design

**Date:** 2026-06-20
**Status:** Validated (brainstorm complete, approved by Brandon). Next: implementation plan.
**Author:** Claude (Opus 4.8) + Brandon

## Goal

Integrate Google **Docs, Sheets, Slides, Drive, and Calendar** through the **same Google OAuth
sign-in flow Gmail already uses**, exposed as first-party ToolVault tools the models use like any
other tool (cron, web search, image gen). **Full structural editing** (the complete Google
`batchUpdate` capability), full Drive file management (any type), and full Calendar CRUD. The MCP
surface is the **full feature suite** — nothing trimmed.

## Why / current state

Gmail is already first-party: `Orchestrator/gmail/service.py` holds `SCOPES`
(`gmail.readonly` + `gmail.send`), a per-operator token store (`Manifest/gmail_tokens/<op>.json`),
the OAuth flow (`/auth/gmail/authorize` → `/auth/gmail/callback`, `gmail_routes.py`), and
`get_gmail_service(operator)`. Gmail ToolVault tools (`gmail_send`/`read`/`search`/`reply`/`labels`)
dispatch via the ToolVault catch-all + a whitelisted `/gmail/execute` for MCP. Docs/Sheets/Slides/
Drive/Calendar extend exactly this. (First-party — distinct from the claude.ai-hosted Google
connectors.)

## Decisions locked (brainstorm)
1. **Full office suite** — Docs + Sheets + Slides + Drive + Calendar.
2. **Full structural editing** — expose the complete Google `batchUpdate` request model.
3. **Same OAuth flow** — add scopes to the existing Gmail flow; existing connections re-consent.
4. **MCP = full feature suite** — every Google tool is `mcp`-group; the raw `batch_update`
   passthrough means the entire API capability is reachable over MCP.
5. **Onboarding mirrors Gmail** — extend the existing Gmail onboarding step/flow to "Google
   Workspace"; do not reinvent the connect UX.

## Architecture

### 1. OAuth / scope extension (foundation)
Extend `Orchestrator/gmail/service.py` `SCOPES` with the five Workspace scopes:
`https://www.googleapis.com/auth/drive`, `.../auth/documents`, `.../auth/spreadsheets`,
`.../auth/presentations`, `.../auth/calendar`. The SAME `/auth/gmail/authorize`→`/callback` flow +
per-operator token store now grants all (`include_granted_scopes="true"` already set → incremental).
Add a shared `_get_credentials(operator)` (factor out of `get_gmail_service`) + per-API builders:
`get_docs_service` (docs v1), `get_sheets_service` (sheets v4), `get_slides_service` (slides v1),
`get_drive_service` (drive v3), `get_calendar_service` (calendar v3). The existing
`get_gmail_service` keeps working (reuses the shared creds). **Migration:** already-connected
operators' tokens carry only gmail scopes → they must **re-consent** (reconnect once) to grant the
new scopes; onboarding prompts this. Keep the `Manifest/gmail_tokens/` store as-is (the token now
holds all granted scopes; renaming is unnecessary churn / risk to the live path).

### 2. Full structural editing — raw `batchUpdate` passthrough (the key design)
Google Docs/Sheets/Slides `batchUpdate` APIs take a list of typed request objects. Rather than wrap
each as its own tool (50+), expose ONE passthrough per app:
- `docs_batch_update(document_id, requests: list)` → `docs.documents().batchUpdate(body={"requests": requests})`
- `sheets_batch_update(spreadsheet_id, requests: list)` → sheets batchUpdate
- `slides_batch_update(presentation_id, requests: list)` → slides batchUpdate
`requests` is the RAW Google API requests array — the entire structural surface (insertText,
updateTextStyle, insertTable, createParagraphBullets, updateCells, addSheet, repeatCell, createShape,
insertText, updateShapeProperties, …). Paired with `read_*` tools that return content WITH element
IDs/indices (so the model has edit targets), this gives full structural power. The tool descriptions
carry schema guidance + 1-2 worked request examples; frontier models know the Google API shape.

### 3. Tools (ToolVault, ~18)
- **Docs:** `create_doc(title, text?)`, `read_doc(document_id)` (returns structured content +
  element IDs/indices), `docs_batch_update(document_id, requests[])`.
- **Sheets:** `create_spreadsheet(title)`, `read_sheet(spreadsheet_id, range?)`,
  `update_sheet_values(spreadsheet_id, range, values)` (convenience for plain cell writes),
  `sheets_batch_update(spreadsheet_id, requests[])`.
- **Slides:** `create_presentation(title)`, `read_presentation(presentation_id)`,
  `slides_batch_update(presentation_id, requests[])`.
- **Drive:** `search_drive_files(query?, page_size?)`, `get_drive_file(file_id)` (metadata; download
  text/exported content where applicable), `create_drive_file(name, mime_type, content?)` (any type),
  `delete_drive_file(file_id)`.
- **Calendar:** `create_event(summary, start, end, …)`, `list_events(time_min, time_max, calendar_id?)`,
  `update_event(event_id, …)`, `delete_event(event_id)`, `list_calendars()`.
Each executor builds the right per-operator service + calls the API; structured, model-friendly
returns; errors mapped (auth-needed → a clear "connect Google" message). Groups:
`["chat","chat_cu","realtime","gemini_live","grok_live","phone","mcp"]` (full suite everywhere),
tier 2.

### 4. Dispatch / MCP
Directly-callable ToolVault tools → dispatch through the existing chat/voice ToolVault catch-all (no
per-tool branches needed). Over MCP: route through the generic `/local/tools/execute` (the path
web-search/image already use) so the FULL suite is MCP-callable with real creds on the full backend.
(Gmail keeps its `/gmail/execute`; the new tools use the generic executor.) After landing:
`POST /toolvault/reload` + `/mcp` reconnect.

### 5. Onboarding (mirror Gmail)
Extend the existing Gmail onboarding step to **"Google Workspace"**: same connect button + OAuth
launch + status display the Gmail step already uses, now communicating it grants Gmail + Docs +
Sheets + Slides + Calendar via one sign-in. Same validator (`validate_gmail_oauth`, client ID/secret).
Re-consent prompt for already-connected operators (their token lacks the new scopes). The consent
screen shows an "unverified app" warning + many restricted scopes — expected/fine for the personal
OAuth client (operator clicks through). Surfaces per the 3-surfaces rule: onboarding is the Portal
wizard; the tools reach Android/voice via server-side injection (no client UI needed).

### 6. Operator scoping & auth-state
Per-operator tokens (same as Gmail). Tools resolve the operator (default resolution) → load that
operator's creds. If the operator hasn't connected / lacks the new scopes, the tool returns a clear
"Google Workspace not connected for <operator> — connect in onboarding" (not a raw 500), so the
model can tell the user.

## Testing & validation
- Unit: mock each Google service per tool (no live calls); assert the right API method + body
  (esp. that `*_batch_update` forwards `requests` verbatim). Auth-missing path returns the clear
  message. Full suite incl. `Orchestrator/tools`.
- **Live validation requires a one-time re-consent** by Brandon (re-run the Google sign-in to grant
  the new scopes). After that: smoke create-a-doc, write-a-sheet-range, create-a-presentation,
  create/list a calendar event, create/list a Drive file — end-to-end + via MCP.

## Follow-ups / risks
- Restricted-scope unverified-app consent (personal client) — accepted.
- Token store name stays `gmail_tokens` (back-compat; the scopes within expand) — cosmetic.
- Full `batchUpdate` is powerful but model-reliability varies; mitigate with read-with-IDs + tool
  examples; convenience tools (create/read/update_values/find-replace requests) cover common cases.
- Google API client (`google-api-python-client`) must be available in the backend venv (it is —
  Gmail uses `build(...)`); confirm in the plan.
