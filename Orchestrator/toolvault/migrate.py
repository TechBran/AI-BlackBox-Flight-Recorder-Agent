"""
ToolVault Migration - Mint all tools from tool_registry.py into the vault.

Reads the canonical TOOL_DEFINITIONS list and mints each tool into the
ToolVault with proper categories, embeddings, and metadata.

Usage:
    python -m Orchestrator.toolvault.migrate [--dry-run] [--no-embed]
"""

import sys
import time
from typing import Dict, List, Optional

# Import the source of truth
from Orchestrator.tools.tool_registry import TOOL_DEFINITIONS

# Import the vault
from Orchestrator.toolvault import mint_tool, get_vault_stats, read_tool
from Orchestrator.toolvault.manifest import load_manifest, get_tool


# ---------------------------------------------------------------------------
# Category mapping (derived from tool_registry.py section comments)
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    # Web Tools
    "web_search": "web",
    "web_fetch": "web",

    # Media Generation
    "generate_image": "media_generation",
    "generate_video": "media_generation",
    "generate_music": "media_generation",
    "extend_video": "media_generation",

    # Media Management
    "get_media": "media_management",
    "list_media": "media_management",
    "search_media": "media_management",

    # Memory / Snapshots
    "search_snapshots": "memory",
    "get_snapshot": "memory",
    "list_recent_snapshots": "memory",
    "get_current_time": "memory",

    # Communication
    "send_sms": "communication",
    "make_phone_call": "communication",
    "make_voice_call": "communication",

    # Contacts
    "search_contacts": "contacts",
    "save_contact": "contacts",

    # Cron Jobs
    "create_cron_job": "scheduling",
    "edit_cron_job": "scheduling",
    "search_cron_jobs": "scheduling",

    # Computer Control
    "use_computer": "computer_control",
    "list_devices": "computer_control",
    "control_android_device": "computer_control",

    # Task Status
    "get_task_status": "task_management",

    # Multimodal Analysis
    "analyze_image": "analysis",
    "analyze_audio": "analysis",
    "analyze_video": "analysis",

    # TTS / STT
    "speech_to_text": "audio",
    "text_to_speech": "audio",
    "list_tts_voices": "audio",
    "gemini_pro_tts": "audio",

    # Gmail
    "gmail_search": "email",
    "gmail_read": "email",
    "gmail_send": "email",
    "gmail_reply": "email",
    "gmail_labels": "email",

    # MCP-Only (BlackBox internals)
    "seek_snapshot_direct": "mcp_internal",
    "mint_snapshot": "mcp_internal",
    "get_context": "mcp_internal",
    "list_operators": "mcp_internal",
    "get_current_operator": "mcp_internal",
    "get_index_stats": "mcp_internal",
    "browse_index": "mcp_internal",
    "chat_with_context": "mcp_internal",
    "refresh_index": "mcp_internal",
    "get_music_status": "mcp_internal",
}


# ---------------------------------------------------------------------------
# Tier assignment
# ---------------------------------------------------------------------------

# Tier 1: Always loaded (core capabilities the model almost always needs)
TIER_1_TOOLS = {
    "search_snapshots",     # Memory - always needed
    "web_search",           # Web search - always useful
    "get_current_time",     # Time - trivial, always helpful
}

# Tier 3: MCP-only internals (not for REST/voice consumers)
TIER_3_TOOLS = {
    "seek_snapshot_direct",
    "mint_snapshot",
    "get_context",
    "list_operators",
    "get_current_operator",
    "get_index_stats",
    "browse_index",
    "chat_with_context",
    "refresh_index",
    "get_music_status",
}

# Everything else is Tier 2 (semantically retrieved on-demand)


def get_tier(name: str) -> int:
    if name in TIER_1_TOOLS:
        return 1
    if name in TIER_3_TOOLS:
        return 3
    return 2


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_all(dry_run: bool = False, generate_embeddings: bool = True) -> Dict:
    """Migrate all 41 tools from tool_registry.py into the ToolVault.

    Args:
        dry_run: If True, print what would happen without writing
        generate_embeddings: If False, skip embedding generation (faster for testing)

    Returns:
        Dict with migration results
    """
    results = {
        "total": len(TOOL_DEFINITIONS),
        "minted": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    print(f"\n{'='*60}")
    print(f"  ToolVault Migration — {len(TOOL_DEFINITIONS)} tools")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Embeddings: {'ON' if generate_embeddings else 'OFF'}")
    print(f"{'='*60}\n")

    for i, tool_def in enumerate(TOOL_DEFINITIONS):
        name = tool_def["name"]
        description = tool_def["description"]
        groups = tool_def.get("groups", [])
        parameters = tool_def.get("parameters", {"type": "object", "properties": {}, "required": []})
        category = CATEGORY_MAP.get(name, "uncategorized")
        tier = get_tier(name)

        # Check if already minted
        existing = get_tool(name)
        if existing:
            print(f"  [{i+1:2d}/{results['total']}] SKIP {name} (already in vault)")
            results["skipped"] += 1
            continue

        if dry_run:
            print(f"  [{i+1:2d}/{results['total']}] WOULD MINT {name} "
                  f"[cat={category}, tier={tier}, groups={len(groups)}]")
            results["minted"] += 1
            continue

        # Mint the tool
        try:
            result = mint_tool(
                name=name,
                description=description,
                category=category,
                groups=groups,
                parameters=parameters,
                returns=_infer_returns(name, description),
                example=_build_example(name, parameters),
                notes=_infer_notes(name, description),
                tier=tier,
                generate_embedding=generate_embeddings,
            )

            print(f"  [{i+1:2d}/{results['total']}] MINTED {name} "
                  f"→ TOOL {result['number']:03d} [{result['byte_start']}:{result['byte_end']}] "
                  f"({result['byte_end'] - result['byte_start']} bytes) "
                  f"{'✓ embedded' if result.get('embedding') else '○ no embedding'}")

            results["minted"] += 1

            # Rate limit for embedding API (avoid 429s)
            if generate_embeddings and i < len(TOOL_DEFINITIONS) - 1:
                time.sleep(0.3)

        except Exception as e:
            print(f"  [{i+1:2d}/{results['total']}] FAILED {name}: {e}")
            results["failed"] += 1
            results["errors"].append({"name": name, "error": str(e)})

    # Summary
    stats = get_vault_stats()
    print(f"\n{'='*60}")
    print(f"  Migration Complete")
    print(f"  Minted: {results['minted']}")
    print(f"  Skipped: {results['skipped']}")
    print(f"  Failed: {results['failed']}")
    print(f"  Vault: {stats['total_tools']} tools, {stats['embedded']} embedded")
    print(f"  Volume: {stats['volume_size_bytes']:,} bytes")
    print(f"{'='*60}\n")

    return results


# ---------------------------------------------------------------------------
# Metadata inference helpers
# ---------------------------------------------------------------------------

def _infer_returns(name: str, description: str) -> str:
    """Infer a returns string from the tool name and description."""
    desc_lower = description.lower()

    if "task_id" in desc_lower or "task id" in desc_lower:
        return "task_id (string) for async status tracking via get_task_status"
    if "returns" in desc_lower:
        # Try to extract the returns clause
        idx = desc_lower.index("returns")
        clause = description[idx:idx+100].split(".")[0]
        return clause
    if name.startswith("search_") or name.startswith("list_"):
        return "List of matching results"
    if name.startswith("get_"):
        return "Requested data object"
    if name.startswith("gmail_"):
        return "Gmail operation result"
    return ""


def _build_example(name: str, parameters: dict) -> str:
    """Build a simple example call from the tool name and required params."""
    required = parameters.get("required", [])
    props = parameters.get("properties", {})

    if not required:
        return f"{name}()"

    example_args = []
    for param_name in required:
        ptype = props.get(param_name, {}).get("type", "string")
        if ptype == "string":
            example_args.append(f'{param_name}="..."')
        elif ptype == "integer":
            example_args.append(f"{param_name}=1")
        elif ptype == "boolean":
            example_args.append(f"{param_name}=true")
        else:
            example_args.append(f'{param_name}="..."')

    return f"{name}({', '.join(example_args)})"


def _infer_notes(name: str, description: str) -> str:
    """Infer notes from tool characteristics."""
    notes_parts = []

    desc_lower = description.lower()
    if "async" in desc_lower or "task_id" in desc_lower:
        notes_parts.append("Async operation - use get_task_status to check progress.")
    if "use search_contacts" in desc_lower:
        notes_parts.append("Look up contact number with search_contacts first.")

    return " ".join(notes_parts)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_vault() -> Dict:
    """Verify all minted tools can be read back correctly.

    Returns dict with verification results.
    """
    from Orchestrator.toolvault.manifest import load_manifest
    from Orchestrator.toolvault.volume import read_tool_by_offset, parse_tool_block

    manifest = load_manifest()
    tools = manifest.get("tools", {})

    results = {
        "total": len(tools),
        "passed": 0,
        "failed": 0,
        "errors": [],
    }

    print(f"\nVerifying {len(tools)} tools in vault...\n")

    for name, entry in tools.items():
        try:
            # Read by byte offset
            block_text = read_tool_by_offset(entry["byte_start"], entry["byte_end"])
            assert block_text, f"Empty read for {name}"

            # Parse the block
            parsed = parse_tool_block(block_text)
            assert parsed.get("NAME") == name, f"Name mismatch: {parsed.get('NAME')} != {name}"
            assert parsed.get("DESCRIPTION"), f"Missing description for {name}"
            assert parsed.get("JSON_SCHEMA"), f"Missing JSON_SCHEMA for {name}"
            assert isinstance(parsed["JSON_SCHEMA"], dict), f"JSON_SCHEMA not dict for {name}"

            # Verify schema has expected structure
            schema = parsed["JSON_SCHEMA"]
            assert "type" in schema, f"Schema missing 'type' for {name}"
            assert "properties" in schema, f"Schema missing 'properties' for {name}"

            # Verify embedding exists
            has_emb = bool(entry.get("embedding"))

            print(f"  PASS {name} [{entry['byte_start']}:{entry['byte_end']}] "
                  f"{'✓' if has_emb else '○'} emb")
            results["passed"] += 1

        except Exception as e:
            print(f"  FAIL {name}: {e}")
            results["failed"] += 1
            results["errors"].append({"name": name, "error": str(e)})

    failed = results['failed']
    fail_msg = f", {failed} failed" if failed else ""
    print(f"\nVerification: {results['passed']}/{results['total']} passed{fail_msg}\n")

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    no_embed = "--no-embed" in sys.argv

    results = migrate_all(dry_run=dry_run, generate_embeddings=not no_embed)

    if not dry_run and results["minted"] > 0:
        print("Running verification...\n")
        verify_vault()
