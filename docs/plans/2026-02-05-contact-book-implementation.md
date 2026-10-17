# Contact Book Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add search_contacts and save_contact tools to the AI BlackBox so any model (REST, WebSocket, phone, SMS) can look up and store contacts.

**Architecture:** Single `Contacts/contacts.json` file with per-operator scoping. New `Orchestrator/contacts.py` module for CRUD logic. Tool schemas added to all four backend formats (Anthropic, OpenAI, Gemini, Grok). System prompts updated with behavioral nudge.

**Tech Stack:** Python 3, JSON file I/O, UUID generation, FastAPI (existing)

---

### Task 1: Create contacts.py — Storage & Search Module

**Files:**
- Create: `Orchestrator/contacts.py`

**Step 1: Create the contacts module**

Write `Orchestrator/contacts.py` with the complete implementation:

```python
"""
contacts.py - Contact Book for AI BlackBox Flight Recorder

Per-operator contact storage with fuzzy search.
Storage: Contacts/contacts.json
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

# Path to contacts file
CONTACTS_DIR = Path(__file__).resolve().parent.parent / "Contacts"
CONTACTS_FILE = CONTACTS_DIR / "contacts.json"

# Seed contact added to every new operator's book
SEED_CONTACT = {
    "name": "AI BlackBox Flight Recorder",
    "phone": "+15555550100",
    "email": "brandon@aiblackboxfc.com",
    "relationship": "self",
    "notes": "This is your own phone number. The AI BlackBox system number. Use this as the caller identity.",
    "tags": ["system", "self"],
    "created_by": "system"
}


def load_contacts() -> Dict[str, Any]:
    """Read contacts.json. Creates file with {} if missing."""
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONTACTS_FILE.exists():
        CONTACTS_FILE.write_text("{}")
        return {}
    try:
        return json.loads(CONTACTS_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def save_contacts(data: Dict[str, Any]) -> None:
    """Write full contacts dict to disk."""
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTACTS_FILE.write_text(json.dumps(data, indent=2))


def _make_seed_contact() -> Dict[str, Any]:
    """Create a seed contact entry with generated ID and timestamps."""
    now = datetime.now(timezone.utc).isoformat()
    contact_id = str(uuid.uuid4())
    return {
        "id": contact_id,
        **SEED_CONTACT,
        "created_at": now,
        "updated_at": now
    }


def ensure_operator_book(data: Dict[str, Any], operator: str) -> bool:
    """
    Ensure operator has a phone book. Creates one with seed contact if missing.
    Returns True if a new book was created.
    """
    if operator not in data:
        seed = _make_seed_contact()
        data[operator] = {seed["id"]: seed}
        return True
    return False


def search_contacts(query: str, operator: str) -> List[Dict[str, Any]]:
    """
    Case-insensitive fuzzy match across all contact fields.
    Returns up to 10 matches, exact name matches ranked first.
    """
    data = load_contacts()
    if ensure_operator_book(data, operator):
        save_contacts(data)

    book = data.get(operator, {})
    query_lower = query.lower()
    results = []

    for contact in book.values():
        score = 0
        # Exact name match (highest priority)
        if contact.get("name", "").lower() == query_lower:
            score = 100
        # Partial name match
        elif query_lower in contact.get("name", "").lower():
            score = 80
        # Phone match
        elif query_lower in contact.get("phone", "").replace("+", "").replace("-", "").replace(" ", ""):
            score = 70
        # Email match
        elif query_lower in contact.get("email", "").lower():
            score = 60
        # Relationship match
        elif query_lower in contact.get("relationship", "").lower():
            score = 50
        # Tag match
        elif any(query_lower in tag.lower() for tag in contact.get("tags", [])):
            score = 40
        # Notes match
        elif query_lower in contact.get("notes", "").lower():
            score = 30

        if score > 0:
            results.append((score, contact))

    # Sort by score descending, return top 10
    results.sort(key=lambda x: x[0], reverse=True)
    return [contact for _, contact in results[:10]]


def upsert_contact(
    name: str,
    notes: str,
    tags: List[str],
    operator: str,
    created_by: str,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    relationship: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create or update a contact. Matches existing by name (case-insensitive).
    Returns the saved contact.
    """
    data = load_contacts()
    ensure_operator_book(data, operator)
    book = data[operator]
    now = datetime.now(timezone.utc).isoformat()

    # Check for existing contact with same name
    existing_id = None
    for cid, contact in book.items():
        if contact.get("name", "").lower() == name.lower():
            existing_id = cid
            break

    if existing_id:
        # Update existing
        contact = book[existing_id]
        contact["name"] = name
        contact["notes"] = notes
        contact["tags"] = tags
        if phone is not None:
            contact["phone"] = phone
        if email is not None:
            contact["email"] = email
        if relationship is not None:
            contact["relationship"] = relationship
        contact["updated_at"] = now
    else:
        # Create new
        contact_id = str(uuid.uuid4())
        contact = {
            "id": contact_id,
            "name": name,
            "phone": phone or "",
            "email": email or "",
            "relationship": relationship or "",
            "notes": notes,
            "tags": tags,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now
        }
        book[contact_id] = contact

    save_contacts(data)
    return contact
```

**Step 2: Verify module loads**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python3 -c "from Orchestrator.contacts import load_contacts, search_contacts, upsert_contact; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add Orchestrator/contacts.py
git commit -m "feat: add contacts.py module for contact book storage and search"
```

---

### Task 2: Create Contacts Directory with Seed Data

**Files:**
- Create: `Contacts/contacts.json`

**Step 1: Create the directory and seed file**

Create `Contacts/contacts.json` by calling the module:

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python3 -c "
from Orchestrator.contacts import load_contacts, save_contacts, ensure_operator_book

data = load_contacts()
# Create books for all operators from config.ini
operators = ['Brandon', 'Anna', 'Anna 2', 'B2 Tester', 'Brandon-DEV', 'Brandon-FA Tech', 'Brandon-Research', 'test', 'test 2']
for op in operators:
    ensure_operator_book(data, op)
save_contacts(data)
print('Contacts seeded for', len(operators), 'operators')
"`

Expected: `Contacts seeded for 9 operators`

**Step 2: Verify the file**

Run: `python3 -c "import json; data=json.load(open('Contacts/contacts.json')); print(len(data), 'operators'); print(list(data.keys()))"`
Expected: `9 operators` followed by the operator list

**Step 3: Commit**

```bash
git add Contacts/contacts.json
git commit -m "feat: seed contacts.json with all operators and BlackBox system contact"
```

---

### Task 3: Add Tool Schemas to blackbox_tools.py — Anthropic Format

**Files:**
- Modify: `Orchestrator/tools/blackbox_tools.py:369` (end of BLACKBOX_TOOLS_ANTHROPIC)

**Step 1: Add search_contacts and save_contact to BLACKBOX_TOOLS_ANTHROPIC**

Insert before the closing `]` at line 369. Add these two tool definitions after the last tool (make_phone_call) in the Anthropic list:

```python
    {
        "name": "search_contacts",
        "description": "Search the contact book for people by name, phone number, tag, or keyword. Use this before making calls or sending texts to find the person's number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, phone number, tag, or any search term to find contacts"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_contact",
        "description": "Save a new contact or update an existing one in the contact book. Use this when a user mentions someone new with contact info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Full name of the contact"
                },
                "notes": {
                    "type": "string",
                    "description": "Context about who this person is and any relevant details"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Categorization tags (e.g., family, work, doctor, vip)"
                },
                "phone": {
                    "type": "string",
                    "description": "Phone number in E.164 format (e.g., +15551234567)"
                },
                "email": {
                    "type": "string",
                    "description": "Email address"
                },
                "relationship": {
                    "type": "string",
                    "description": "Relationship to the user (e.g., friend, coworker, doctor)"
                }
            },
            "required": ["name", "notes", "tags"]
        }
    }
```

**Step 2: Verify syntax**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python3 -c "from Orchestrator.tools.blackbox_tools import BLACKBOX_TOOLS_ANTHROPIC; print(len(BLACKBOX_TOOLS_ANTHROPIC), 'tools')"`
Expected: Tool count increased by 2 from previous count

---

### Task 4: Add Tool Schemas to blackbox_tools.py — OpenAI Format

**Files:**
- Modify: `Orchestrator/tools/blackbox_tools.py:672` (end of BLACKBOX_TOOLS_OPENAI)

**Step 1: Add search_contacts and save_contact to BLACKBOX_TOOLS_OPENAI**

Insert before the closing `]` at line 672. OpenAI format uses `"type": "function"` wrapper with `"parameters"` instead of `"input_schema"`:

```python
    {
        "type": "function",
        "name": "search_contacts",
        "description": "Search the contact book for people by name, phone number, tag, or keyword. Use this before making calls or sending texts to find the person's number.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, phone number, tag, or any search term to find contacts"
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "save_contact",
        "description": "Save a new contact or update an existing one in the contact book. Use this when a user mentions someone new with contact info.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Full name of the contact"},
                "notes": {"type": "string", "description": "Context about who this person is"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Categorization tags"},
                "phone": {"type": "string", "description": "Phone number in E.164 format"},
                "email": {"type": "string", "description": "Email address"},
                "relationship": {"type": "string", "description": "Relationship to the user"}
            },
            "required": ["name", "notes", "tags"]
        }
    }
```

**Step 2: Verify syntax**

Run: `python3 -c "from Orchestrator.tools.blackbox_tools import BLACKBOX_TOOLS_OPENAI; print(len(BLACKBOX_TOOLS_OPENAI), 'tools')"`

---

### Task 5: Add Tool Schemas to blackbox_tools.py — Gemini Format

**Files:**
- Modify: `Orchestrator/tools/blackbox_tools.py:952` (end of BLACKBOX_TOOLS_GEMINI)

**Step 1: Add search_contacts and save_contact to BLACKBOX_TOOLS_GEMINI**

Gemini uses flat format (no `"type": "function"` wrapper), `"parameters"` directly:

```python
    {
        "name": "search_contacts",
        "description": "Search the contact book for people by name, phone number, tag, or keyword. Use this before making calls or sending texts to find the person's number.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, phone number, tag, or any search term to find contacts"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_contact",
        "description": "Save a new contact or update an existing one in the contact book. Use this when a user mentions someone new with contact info.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Full name of the contact"},
                "notes": {"type": "string", "description": "Context about who this person is"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Categorization tags"},
                "phone": {"type": "string", "description": "Phone number in E.164 format"},
                "email": {"type": "string", "description": "Email address"},
                "relationship": {"type": "string", "description": "Relationship to the user"}
            },
            "required": ["name", "notes", "tags"]
        }
    }
```

**Step 2: Verify syntax**

Run: `python3 -c "from Orchestrator.tools.blackbox_tools import BLACKBOX_TOOLS_GEMINI; print(len(BLACKBOX_TOOLS_GEMINI), 'tools')"`

**Step 3: Commit all tool schema changes**

```bash
git add Orchestrator/tools/blackbox_tools.py
git commit -m "feat: add search_contacts and save_contact tool schemas for Anthropic, OpenAI, Gemini"
```

---

### Task 6: Add Executor Methods to BlackBoxToolExecutor

**Files:**
- Modify: `Orchestrator/tools/blackbox_tools.py:1636` (after _execute_make_phone_call)

**Step 1: Add import at top of file**

Add after the existing imports (around line 28):

```python
from Orchestrator.contacts import search_contacts, upsert_contact
```

**Step 2: Add _execute_search_contacts method**

Insert after `_execute_make_phone_call` (after line 1636), before the Helper Functions section:

```python
    async def _execute_search_contacts(self, params: Dict[str, Any]) -> ToolResult:
        """Search the contact book."""
        query = params.get("query", "")
        if not query:
            return ToolResult(False, "Search query is required")

        try:
            results = search_contacts(query, self.operator)
            if not results:
                return ToolResult(
                    success=True,
                    result=f"No contacts found matching '{query}'.",
                    data={"contacts": []}
                )

            summary_lines = [f"Found {len(results)} contact(s):"]
            for c in results:
                line = f"- {c['name']}"
                if c.get('phone'):
                    line += f" | {c['phone']}"
                if c.get('email'):
                    line += f" | {c['email']}"
                if c.get('relationship'):
                    line += f" ({c['relationship']})"
                summary_lines.append(line)

            return ToolResult(
                success=True,
                result="\n".join(summary_lines),
                data={"contacts": results}
            )
        except Exception as e:
            return ToolResult(False, f"Contact search error: {str(e)}")
```

**Step 3: Add _execute_save_contact method**

Insert immediately after `_execute_search_contacts`:

```python
    async def _execute_save_contact(self, params: Dict[str, Any]) -> ToolResult:
        """Save or update a contact."""
        name = params.get("name", "")
        notes = params.get("notes", "")
        tags = params.get("tags", [])

        if not name:
            return ToolResult(False, "Contact name is required")
        if not notes:
            return ToolResult(False, "Contact notes are required")
        if not tags:
            return ToolResult(False, "At least one tag is required")

        try:
            contact = upsert_contact(
                name=name,
                notes=notes,
                tags=tags,
                operator=self.operator,
                created_by=self.operator,
                phone=params.get("phone"),
                email=params.get("email"),
                relationship=params.get("relationship")
            )

            return ToolResult(
                success=True,
                result=f"Contact saved: {contact['name']}" + (f" ({contact.get('phone', '')})" if contact.get('phone') else ""),
                data={"contact": contact}
            )
        except Exception as e:
            return ToolResult(False, f"Save contact error: {str(e)}")
```

**Step 4: Verify executor dispatch**

Run: `python3 -c "
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor
e = BlackBoxToolExecutor(operator='Brandon')
print(hasattr(e, '_execute_search_contacts'))
print(hasattr(e, '_execute_save_contact'))
"`
Expected: `True` and `True`

**Step 5: Commit**

```bash
git add Orchestrator/tools/blackbox_tools.py
git commit -m "feat: add search_contacts and save_contact executor methods"
```

---

### Task 7: Add Contact Tools to REALTIME_TOOLS (OpenAI Realtime)

**Files:**
- Modify: `Orchestrator/routes/realtime_routes.py:362` (before closing `]` of REALTIME_TOOLS)

**Step 1: Add search_contacts and save_contact**

Insert before the closing `]` at line 363. Uses OpenAI function format (same as BLACKBOX_TOOLS_OPENAI):

```python
    {
        "type": "function",
        "name": "search_contacts",
        "description": "Search the contact book for people by name, phone number, tag, or keyword. Use this before making calls or sending texts to find the person's number.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, phone number, tag, or any search term to find contacts"
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "save_contact",
        "description": "Save a new contact or update an existing one in the contact book. Use this when a user mentions someone new with contact info.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Full name of the contact"},
                "notes": {"type": "string", "description": "Context about who this person is"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Categorization tags"},
                "phone": {"type": "string", "description": "Phone number in E.164 format"},
                "email": {"type": "string", "description": "Email address"},
                "relationship": {"type": "string", "description": "Relationship to the user"}
            },
            "required": ["name", "notes", "tags"]
        }
    }
```

**Step 2: Update system prompt — custom_role section**

At `realtime_routes.py:598-600`, change the ESSENTIAL TOOLS section:

FROM:
```python
ESSENTIAL TOOLS:
You have access to search_snapshots and get_recent_snapshots for memory/context.
You can also generate images, videos, music, and send SMS or make phone calls.
```

TO:
```python
ESSENTIAL TOOLS:
You have access to search_snapshots and get_recent_snapshots for memory/context.
You can also generate images, videos, music, and send SMS or make phone calls.
You have search_contacts and save_contact for the contact book.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
```

**Step 3: Also update the default (non-custom_role) system prompt**

Search for the equivalent ESSENTIAL TOOLS section in the `else` branch (default prompt) around lines 619-680 and add the same contact nudge there.

**Step 4: Commit**

```bash
git add Orchestrator/routes/realtime_routes.py
git commit -m "feat: add contact tools to OpenAI Realtime (tools + prompt)"
```

---

### Task 8: Add Contact Tools to GEMINI_LIVE_TOOLS

**Files:**
- Modify: `Orchestrator/routes/gemini_live_routes.py:349` (before closing of GEMINI_LIVE_TOOLS)

**Step 1: Add search_contacts and save_contact**

Gemini uses `functionDeclarations` wrapper. Add inside the `functionDeclarations` array (before the closing `]` at line 349):

```python
            {
                "name": "search_contacts",
                "description": "Search the contact book for people by name, phone number, tag, or keyword. Use this before making calls or sending texts to find the person's number.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Name, phone number, tag, or any search term to find contacts"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "save_contact",
                "description": "Save a new contact or update an existing one in the contact book. Use this when a user mentions someone new with contact info.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Full name of the contact"},
                        "notes": {"type": "string", "description": "Context about who this person is"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Categorization tags"},
                        "phone": {"type": "string", "description": "Phone number in E.164 format"},
                        "email": {"type": "string", "description": "Email address"},
                        "relationship": {"type": "string", "description": "Relationship to the user"}
                    },
                    "required": ["name", "notes", "tags"]
                }
            }
```

**Step 2: Update system prompt**

At `gemini_live_routes.py:507-509`, change the ESSENTIAL TOOLS section (same change as realtime):

TO:
```python
ESSENTIAL TOOLS:
You have access to search_snapshots and get_recent_snapshots for memory/context.
You can also generate images, videos, music, and send SMS or make phone calls.
You have search_contacts and save_contact for the contact book.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
```

Also update the default prompt's equivalent section.

**Step 3: Commit**

```bash
git add Orchestrator/routes/gemini_live_routes.py
git commit -m "feat: add contact tools to Gemini Live (tools + prompt)"
```

---

### Task 9: Add Contact Tools to GROK_LIVE_TOOLS

**Files:**
- Modify: `Orchestrator/routes/grok_live_routes.py:365` (before closing `]` of GROK_LIVE_TOOLS)

**Step 1: Add search_contacts and save_contact**

Grok uses OpenAI-compatible format:

```python
    {
        "type": "function",
        "name": "search_contacts",
        "description": "Search the contact book for people by name, phone number, tag, or keyword. Use this before making calls or sending texts to find the person's number.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, phone number, tag, or any search term to find contacts"
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "save_contact",
        "description": "Save a new contact or update an existing one in the contact book. Use this when a user mentions someone new with contact info.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Full name of the contact"},
                "notes": {"type": "string", "description": "Context about who this person is"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Categorization tags"},
                "phone": {"type": "string", "description": "Phone number in E.164 format"},
                "email": {"type": "string", "description": "Email address"},
                "relationship": {"type": "string", "description": "Relationship to the user"}
            },
            "required": ["name", "notes", "tags"]
        }
    }
```

**Step 2: Update system prompt**

At `grok_live_routes.py:604-606`, change the ESSENTIAL TOOLS section:

TO:
```python
ESSENTIAL TOOLS:
You have access to search_snapshots and get_recent_snapshots for memory/context.
You can also generate images, videos, music, and send SMS or make phone calls.
You have search_contacts and save_contact for the contact book.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
```

Also update the default prompt's equivalent section.

**Step 3: Commit**

```bash
git add Orchestrator/routes/grok_live_routes.py
git commit -m "feat: add contact tools to Grok Live (tools + prompt)"
```

---

### Task 10: Update SMS System Prompt (twilio_routes.py)

**Files:**
- Modify: `Orchestrator/routes/twilio_routes.py:566-574`

**Step 1: Update the tool list and add behavioral nudge**

Change the system prompt at lines 566-574 from:

```python
You have access to these tools:
- send_sms: Send a text message to any phone number
- make_voice_call: Call someone and deliver a voice message (pre-generates TTS for instant playback)
- search_memory: Search BlackBox memory for past conversations or information
- get_current_time: Get the current date and time
- generate_image: Generate an AI image

If the user asks you to call them, use make_voice_call with their number ({From}).
If they want to text someone, use send_sms.
```

TO:

```python
You have access to these tools:
- send_sms: Send a text message to any phone number
- make_voice_call: Call someone and deliver a voice message (pre-generates TTS for instant playback)
- search_memory: Search BlackBox memory for past conversations or information
- search_contacts: Search the contact book for people by name, phone number, tag, or keyword
- save_contact: Save a new contact or update an existing one (requires name, notes, and tags)
- get_current_time: Get the current date and time
- generate_image: Generate an AI image

Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
If the user asks you to call them, use make_voice_call with their number ({From}).
If they want to text someone, use send_sms.
```

**Step 2: Commit**

```bash
git add Orchestrator/routes/twilio_routes.py
git commit -m "feat: add contact tools to SMS system prompt"
```

---

### Task 11: Update REST Chat System Prompt (config.py OUTPUT_SPEC)

**Files:**
- Modify: `Orchestrator/config.py:233-251` (after SNAPSHOT SEARCH TOOL section)

**Step 1: Add CONTACT BOOK section to OUTPUT_SPEC**

Insert after the SNAPSHOT SEARCH TOOL section (after the "When NOT to USE" block ending around line 255) and before the "INLINE IMAGE DISPLAY" section (line 256):

```python
    "CONTACT BOOK TOOLS:\n"
    "  Tool: search_contacts\n"
    "  Parameters:\n"
    "    - query (required): Name, phone number, tag, or any search term\n"
    "  Returns matching contacts with name, phone, email, relationship, notes, and tags.\n\n"
    "  Tool: save_contact\n"
    "  Parameters:\n"
    "    - name (required): Full name of the contact\n"
    "    - notes (required): Context about who this person is\n"
    "    - tags (required): Array of categorization tags\n"
    "    - phone (optional): Phone number in E.164 format\n"
    "    - email (optional): Email address\n"
    "    - relationship (optional): Relationship to the user\n\n"
    "  CONTACT BOOK BEHAVIOR:\n"
    "  Before making calls or sending texts, always search_contacts first to find the person's number.\n"
    "  When a user mentions someone new with contact info, save them to the contact book.\n\n"
```

**Step 2: Commit**

```bash
git add Orchestrator/config.py
git commit -m "feat: add contact tools to REST chat OUTPUT_SPEC"
```

---

### Task 12: Restart Service and Verify

**Files:**
- No file changes

**Step 1: Restart BlackBox service**

Run: `sudo systemctl restart blackbox.service`

**Step 2: Verify service is running**

Run: `sleep 3 && curl -s http://localhost:9091/health | python3 -m json.tool`
Expected: Health check returns OK

**Step 3: Verify contacts file exists and is populated**

Run: `python3 -c "import json; data=json.load(open('/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Contacts/contacts.json')); print(len(data), 'operators'); print(json.dumps(list(data['Brandon'].values())[0], indent=2))"`
Expected: Shows operators count and seed contact for Brandon

**Step 4: Test search via Python**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python3 -c "
from Orchestrator.contacts import search_contacts
results = search_contacts('BlackBox', 'Brandon')
for r in results:
    print(f'{r[\"name\"]} | {r[\"phone\"]} | {r[\"tags\"]}')
"`
Expected: Shows the seed contact

**Step 5: Test save via Python**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python3 -c "
from Orchestrator.contacts import upsert_contact, search_contacts
c = upsert_contact('Test Contact', 'Test notes for verification', ['test'], 'Brandon', 'system', phone='+15555550000')
print(f'Saved: {c[\"name\"]} | {c[\"id\"]}')
results = search_contacts('Test Contact', 'Brandon')
print(f'Found: {len(results)} result(s)')
"`
Expected: Shows saved contact and search finds it

**Step 6: Clean up test contact**

Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python3 -c "
import json
data = json.load(open('Contacts/contacts.json'))
book = data['Brandon']
to_remove = [k for k, v in book.items() if v.get('name') == 'Test Contact']
for k in to_remove:
    del book[k]
json.dump(data, open('Contacts/contacts.json', 'w'), indent=2)
print('Cleaned up test contact')
"`

**Step 7: Final commit**

```bash
git add -A
git commit -m "feat: contact book system complete - search_contacts and save_contact tools"
```
