# Contact Book / Phone Book System Design

**Date:** 2026-02-05
**Status:** Approved

## Overview

A contact book system for the AI BlackBox Flight Recorder that lets any AI model (REST, WebSocket, phone call, SMS) search for and save contacts. Enables models to look up phone numbers, emails, and context about people before making calls or sending texts.

## Storage

### Location
```
blackbox_poc/
   Contacts/
      contacts.json
```

Single JSON file. Per-operator scoping at the top level.

### Schema

```json
{
  "Brandon": {
    "a1b2c3d4-uuid-here": {
      "id": "a1b2c3d4-uuid-here",
      "name": "AI BlackBox Flight Recorder",
      "phone": "+15555550100",
      "email": "brandon@aiblackboxfc.com",
      "relationship": "self",
      "notes": "This is your own phone number. The AI BlackBox system number.",
      "tags": ["system", "self"],
      "created_by": "system",
      "created_at": "2026-02-05T00:00:00Z",
      "updated_at": "2026-02-05T00:00:00Z"
    }
  },
  "Anna": {},
  "Brandon-DEV": {}
}
```

### Fields

| Field | Required (save) | Type | Description |
|-------|-----------------|------|-------------|
| id | auto | string (UUID) | Auto-generated |
| name | yes | string | Full name, primary lookup field |
| notes | yes | string | Context about who this person is |
| tags | yes | array[string] | Categorization tags |
| phone | no | string | E.164 format (+15551234567) |
| email | no | string | Email address |
| relationship | no | string | Freeform (friend, doctor, coworker) |
| created_by | auto | string | Which model/backend created it |
| created_at | auto | string (ISO8601) | Creation timestamp |
| updated_at | auto | string (ISO8601) | Last update timestamp |

### Per-Operator Scoping

- Each operator gets their own namespace (phone book) in the file
- Operator resolved from context automatically (no parameter needed from model)
- New operators get an empty phone book on first access
- Seed contact (AI BlackBox: +15555550100, brandon@aiblackboxfc.com) added to every new operator's book automatically

## Backend Module

### New file: `Orchestrator/contacts.py`

Functions:
- **`load_contacts()`** — Read `Contacts/contacts.json`, create with `{}` if missing
- **`save_contacts(data)`** — Write full contacts dict to disk
- **`search_contacts(query, operator)`** — Case-insensitive fuzzy match across name, phone, email, relationship, notes, tags. Returns up to 10 matches ranked by relevance
- **`upsert_contact(name, notes, tags, operator, created_by, phone=None, email=None, relationship=None)`** — Update existing contact (matched by name, case-insensitive) or create new with UUID. Bumps `updated_at` on update
- **`ensure_operator_book(data, operator)`** — Create operator's phone book if missing, seed with BlackBox system contact

## Tool Definitions

### search_contacts

```
Parameters:
  - query (required, string): Name, phone number, tag, or any search term

Returns: Array of matching contacts with all fields, or empty array if none found.
```

### save_contact

```
Parameters:
  - name (required, string): Full name of the contact
  - notes (required, string): Context about who this person is
  - tags (required, array[string]): Categorization tags (at least one)
  - phone (optional, string): Phone number in E.164 format
  - email (optional, string): Email address
  - relationship (optional, string): Relationship to the user

Returns: The saved/updated contact object with ID.
```

Tool schemas defined in four formats:
1. Anthropic (Claude)
2. OpenAI (GPT)
3. Gemini (Google)
4. Grok (XAI)

`created_by` set automatically by executor based on calling backend.

## Tool Executor

Add to `BlackBoxToolExecutor` in `Orchestrator/tools/blackbox_tools.py`:

- **`_execute_search_contacts(params)`** — Extract operator from context, call `contacts.search_contacts(query, operator)`
- **`_execute_save_contact(params)`** — Extract operator from context, call `contacts.upsert_contact(...)` with `created_by` set to current backend name

## System Prompt Updates

### Tool list addition (all prompts)
```
- search_contacts: Search the contact book for people by name, phone number, tag, or keyword
- save_contact: Save a new contact or update an existing one (requires name, notes, and tags)
```

### Behavioral nudge (all prompts, near phone/SMS tools)
```
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
```

### Files to update

1. `Orchestrator/tools/blackbox_tools.py` — Tool schemas (4 formats) + executor methods
2. `Orchestrator/contacts.py` — New module (contact logic)
3. `Orchestrator/routes/twilio_routes.py` — SMS system prompt
4. `Orchestrator/config.py` — REST chat system prompt (OUTPUT_SPEC)
5. `Orchestrator/routes/realtime_routes.py` — OpenAI Realtime prompt + tools
6. `Orchestrator/routes/gemini_live_routes.py` — Gemini Live prompt + tools
7. `Orchestrator/routes/grok_live_routes.py` — Grok Live prompt + tools

## Seed Contact

Pre-populated for every operator:

```json
{
  "name": "AI BlackBox Flight Recorder",
  "phone": "+15555550100",
  "email": "brandon@aiblackboxfc.com",
  "relationship": "self",
  "notes": "This is your own phone number. The AI BlackBox system number. Use this as the caller identity.",
  "tags": ["system", "self"],
  "created_by": "system"
}
```
