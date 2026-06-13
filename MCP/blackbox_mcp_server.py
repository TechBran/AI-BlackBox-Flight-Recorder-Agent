#!/usr/bin/env python3
"""
BlackBox MCP Server - Exposes BlackBox Flight Recorder to Claude Code

This MCP server provides tools and resources for AI agents to:
- Search and retrieve snapshots from the BlackBox memory system
- Mint new snapshots (memories)
- Access the byte-offset manifest for efficient traversal
- Direct byte-offset seeking into the snapshot volume
- Get context-enriched responses through the BlackBox chat system

Run with: python blackbox_mcp_server.py
Configure in Claude Code: ~/.claude/mcp.json
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Dict, List
import httpx

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        Resource,
        ResourceContents,
        TextResourceContents,
    )
except ImportError:
    print("MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Configuration - paths relative to blackbox root
BLACKBOX_ROOT = Path(os.getenv("BLACKBOX_ROOT") or Path(__file__).resolve().parent.parent)
BLACKBOX_URL = os.getenv("BLACKBOX_URL", "http://localhost:9091")

# Operator resolution (pure decision logic lives in operator_resolution.py).
# Same-dir import: when this server runs as a bare script
# (MCP/venv/bin/python MCP/blackbox_mcp_server.py), the script's own directory
# is on sys.path[0], so the sibling module imports directly without a package root.
from operator_resolution import choose_operator

# Per-process cache of the install's operators (multi-tenant: never hard-coded).
_OPERATOR_CACHE = {"operators": None, "default": None}


async def _fetch_operators():
    """Fetch + cache the install's operators from GET /operators.

    Cached only on SUCCESS (per-process, lifetime = one MCP session). A failed
    fetch returns an empty result WITHOUT caching, so the next call retries —
    a transient API blip self-heals instead of degrading the whole session.
    """
    if _OPERATOR_CACHE["operators"] is not None:
        return _OPERATOR_CACHE["operators"], _OPERATOR_CACHE["default"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BLACKBOX_URL}/operators")
            data = r.json()
            operators = list(data.get("operators") or [])
            default = data.get("default") or ""
        _OPERATOR_CACHE["operators"] = operators
        _OPERATOR_CACHE["default"] = default
        return operators, default
    except Exception:
        return [], ""   # do NOT cache — retry on next call


async def resolve_operator(provided):
    """Server-side safety net: resolve the operator for a tool call. When omitted,
    single operator -> that; multiple -> system default. (Interactive dropdown for
    the multiple case is handled agent-side, not here.)"""
    operators, default = await _fetch_operators()
    resolved, _needs = choose_operator(provided, operators, default)
    return resolved

# Import web_tools directly (stdlib + requests + bs4 only)
sys.path.insert(0, str(BLACKBOX_ROOT / "Orchestrator"))
from web_tools import perform_web_search, perform_web_fetch

# E21 (2026-05-17): load tool_registry.py as a STANDALONE module to bypass
# Orchestrator/tools/__init__.py, which re-exports blackbox_tools — and
# blackbox_tools transitively pulls in aiohttp, Orchestrator.contacts, and
# the rest of the FastAPI request/response stack. The MCP server only needs
# get_mcp_tools (metadata generation, returns tool schemas); it never CALLS
# them locally (execution always hops through HTTP back to the BlackBox API).
# Loading the file directly via importlib lets MCP/venv stay lean — no
# aiohttp/fastapi/starlette needed, which sidesteps the mcp-vs-fastapi
# starlette version conflict that would otherwise force a heavier venv.
import importlib.util as _ilu
_tr_path = BLACKBOX_ROOT / "Orchestrator" / "tools" / "tool_registry.py"
_tr_spec = _ilu.spec_from_file_location("blackbox_tool_registry", str(_tr_path))
_tr_module = _ilu.module_from_spec(_tr_spec)
_tr_spec.loader.exec_module(_tr_module)
get_mcp_tools = _tr_module.get_mcp_tools

# Direct file paths for efficient byte-offset access
VOLUME_FILE = BLACKBOX_ROOT / "Volumes" / "SNAPSHOT_VOLUME.txt"
SNAPSHOT_INDEX = BLACKBOX_ROOT / "Manifest" / "snapshot_index.json"
MANIFEST_FILE = BLACKBOX_ROOT / "Manifest" / "manifest.json"

# Initialize MCP server
server = Server("blackbox-mcp")

# Cache for the snapshot index (loaded once, refreshed on demand)
_index_cache: Optional[Dict] = None
_index_cache_mtime: float = 0


def load_snapshot_index(force_refresh: bool = False) -> Dict:
    """Load snapshot index with caching for performance."""
    global _index_cache, _index_cache_mtime

    if not SNAPSHOT_INDEX.exists():
        return {}

    current_mtime = SNAPSHOT_INDEX.stat().st_mtime
    if _index_cache is None or force_refresh or current_mtime > _index_cache_mtime:
        with open(SNAPSHOT_INDEX, 'r') as f:
            _index_cache = json.load(f)
        _index_cache_mtime = current_mtime
        print(f"[MCP] Loaded snapshot index: {len(_index_cache)} entries", file=sys.stderr)

    return _index_cache


def seek_snapshot_by_offset(snap_id: str) -> Optional[str]:
    """
    Efficiently retrieve a snapshot using byte offsets from the index.
    This is O(1) seek + read, not O(n) scan of the entire volume.
    """
    index = load_snapshot_index()

    if snap_id not in index:
        return None

    entry = index[snap_id]
    byte_start = entry.get("byte_start")
    byte_end = entry.get("byte_end")

    if byte_start is None or byte_end is None:
        return None

    if not VOLUME_FILE.exists():
        return None

    # Seek directly to the byte offset and read only what we need
    with open(VOLUME_FILE, 'rb') as f:
        f.seek(byte_start)
        raw_bytes = f.read(byte_end - byte_start)

    return raw_bytes.decode('utf-8', errors='replace')


def get_snapshot_metadata(snap_id: str) -> Optional[Dict]:
    """Get metadata for a snapshot without reading its content."""
    index = load_snapshot_index()
    if snap_id not in index:
        return None

    entry = index[snap_id]
    return {
        "snap_id": snap_id,
        "operator": entry.get("operator", "unknown"),
        "timestamp": entry.get("timestamp", "unknown"),
        "type": entry.get("type", "normal"),
        "byte_start": entry.get("byte_start"),
        "byte_end": entry.get("byte_end"),
        "size_bytes": entry.get("byte_end", 0) - entry.get("byte_start", 0),
        "has_embedding": "embedding" in entry and len(entry.get("embedding", [])) > 0
    }


def list_snapshots_by_operator(operator: str, limit: int = 50) -> List[Dict]:
    """List snapshots for an operator using the index (no volume reads)."""
    index = load_snapshot_index()

    results = []
    for snap_id, entry in index.items():
        if entry.get("operator") == operator:
            results.append({
                "snap_id": snap_id,
                "timestamp": entry.get("timestamp", ""),
                "type": entry.get("type", "normal"),
                "size_bytes": entry.get("byte_end", 0) - entry.get("byte_start", 0)
            })

    # Sort by timestamp descending (most recent first)
    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results[:limit]


# =============================================================================
# TOOLS - Actions the agent can take
# =============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available BlackBox tools — generated from tool_registry.py."""
    return get_mcp_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a BlackBox tool."""

    try:
        # === LOCAL TOOLS (Direct file access - faster) ===

        if name == "seek_snapshot_direct":
            snap_id = arguments["snap_id"]
            content = seek_snapshot_by_offset(snap_id)

            if content is None:
                return [TextContent(type="text", text=f"Snapshot {snap_id} not found in index")]

            metadata = get_snapshot_metadata(snap_id)
            result = {
                "snap_id": snap_id,
                "metadata": metadata,
                "content": content
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_snapshot":
            snap_id = arguments["snap_id"]
            include_content = arguments.get("include_content", True)

            metadata = get_snapshot_metadata(snap_id)
            if metadata is None:
                return [TextContent(type="text", text=f"Snapshot {snap_id} not found")]

            result = {"metadata": metadata}
            if include_content:
                result["content"] = seek_snapshot_by_offset(snap_id)

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "list_recent_snapshots":
            operator = await resolve_operator(arguments.get("operator"))
            count = arguments.get("count", 10)

            snapshots = list_snapshots_by_operator(operator, count)
            return [TextContent(type="text", text=json.dumps(snapshots, indent=2))]

        elif name == "get_index_stats":
            index = load_snapshot_index()

            # Calculate stats
            operators = {}
            types = {"normal": 0, "checkpoint": 0, "summary": 0}
            total_bytes = 0

            for snap_id, entry in index.items():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1

                snap_type = entry.get("type", "normal")
                if snap_type in types:
                    types[snap_type] += 1

                size = entry.get("byte_end", 0) - entry.get("byte_start", 0)
                total_bytes += size

            stats = {
                "total_snapshots": len(index),
                "operators": operators,
                "types": types,
                "total_size_bytes": total_bytes,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2),
                "index_file": str(SNAPSHOT_INDEX),
                "volume_file": str(VOLUME_FILE),
                "volume_exists": VOLUME_FILE.exists()
            }
            return [TextContent(type="text", text=json.dumps(stats, indent=2))]

        elif name == "browse_index":
            # Read tool: omitting operator means ALL operators on this box (do NOT force-resolve).
            operator = arguments.get("operator", "")
            snap_type = arguments.get("snap_type")
            limit = arguments.get("limit", 20)
            offset = arguments.get("offset", 0)

            index = load_snapshot_index()

            # Filter and collect
            results = []
            for snap_id, entry in index.items():
                if operator and entry.get("operator") != operator:
                    continue
                if snap_type and entry.get("type") != snap_type:
                    continue

                results.append({
                    "snap_id": snap_id,
                    "operator": entry.get("operator"),
                    "timestamp": entry.get("timestamp"),
                    "type": entry.get("type", "normal"),
                    "size_bytes": entry.get("byte_end", 0) - entry.get("byte_start", 0)
                })

            # Sort by timestamp descending
            results.sort(key=lambda x: x["timestamp"], reverse=True)

            # Paginate
            paginated = results[offset:offset + limit]

            return [TextContent(type="text", text=json.dumps({
                "total_matching": len(results),
                "returned": len(paginated),
                "offset": offset,
                "snapshots": paginated
            }, indent=2))]

        elif name == "list_operators":
            index = load_snapshot_index()

            operators = {}
            for entry in index.values():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1

            # Sort by count descending
            sorted_ops = sorted(operators.items(), key=lambda x: x[1], reverse=True)

            return [TextContent(type="text", text=json.dumps({
                "operators": [{"name": op, "snapshot_count": count} for op, count in sorted_ops]
            }, indent=2))]

        elif name == "get_current_operator":
            operators, default = await _fetch_operators()
            resolved, needs_selection = choose_operator(None, operators, default)
            result = {
                "resolved": resolved,
                "operators": operators,
                "default": default,
                "count": len(operators),
                "needs_selection": needs_selection,
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "refresh_index":
            load_snapshot_index(force_refresh=True)
            index = load_snapshot_index()
            return [TextContent(type="text", text=f"Index refreshed. {len(index)} snapshots loaded.")]

        elif name == "web_search":
            query = arguments["query"]
            max_results = arguments.get("max_results", 5)
            recency = arguments.get("search_recency_filter", "month")
            result = perform_web_search(query, max_results, search_recency_filter=recency)
            return [TextContent(type="text", text=result)]

        elif name == "web_fetch":
            url = arguments["url"]
            max_chars = arguments.get("max_chars", 80000)
            result = perform_web_fetch(url, max_chars)
            return [TextContent(type="text", text=result)]

        elif name == "get_media":
            import base64
            import mimetypes

            url = arguments.get("url")
            task_id = arguments.get("task_id")
            include_base64 = arguments.get("include_base64", True)
            include_metadata = arguments.get("include_metadata", True)

            # If task_id provided, look up URL from task
            if task_id and not url:
                async with httpx.AsyncClient(timeout=30) as client:
                    task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                    if task_response.status_code == 200:
                        task_data = task_response.json()
                        url = task_data.get("result_url")
                        if not url:
                            return [TextContent(type="text", text=f"Error: Task {task_id} has no result_url")]
                    else:
                        return [TextContent(type="text", text=f"Error: Task {task_id} not found")]

            if not url:
                return [TextContent(type="text", text="Error: No URL or task_id provided")]

            # Clean URL - remove host prefix if present
            if url.startswith("http://") or url.startswith("https://"):
                from urllib.parse import urlparse
                parsed = urlparse(url)
                url = parsed.path

            # Resolve to file path
            # URL format: /ui/uploads/filename.ext -> Portal/uploads/filename.ext
            if url.startswith("/ui/"):
                relative_path = url.replace("/ui/", "")
                file_path = BLACKBOX_ROOT / "Portal" / relative_path
            else:
                return [TextContent(type="text", text=f"Error: Invalid URL format: {url}")]

            if not file_path.exists():
                return [TextContent(type="text", text=f"Error: File not found: {file_path}")]

            # Detect media type
            suffix = file_path.suffix.lower()
            if suffix in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                media_type = "image"
            elif suffix in ['.mp4', '.webm', '.mov']:
                media_type = "video"
            elif suffix in ['.wav', '.mp3', '.ogg', '.m4a']:
                media_type = "audio"
            else:
                media_type = "unknown"

            # Get MIME type
            mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

            result = {
                "url": url,
                "file_path": str(file_path),
                "file_size_bytes": file_path.stat().st_size,
                "media_type": media_type,
                "mime_type": mime_type
            }

            # Include base64 for images under 10MB
            if include_base64 and media_type == "image":
                if result["file_size_bytes"] < 10_000_000:
                    with open(file_path, "rb") as f:
                        result["base64"] = base64.b64encode(f.read()).decode()
                    print(f"[MCP] get_media: Included base64 for {file_path.name} ({result['file_size_bytes']} bytes)", file=sys.stderr)
                else:
                    result["base64_skipped"] = "File too large (>10MB)"

            # Include metadata from task if task_id provided
            if include_metadata and task_id:
                async with httpx.AsyncClient(timeout=30) as client:
                    task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                    if task_response.status_code == 200:
                        t = task_response.json()
                        result["metadata"] = {
                            "prompt": t.get("prompt"),
                            "task_type": t.get("task_type"),
                            "operator": t.get("operator"),
                            "created_at": t.get("created_at"),
                            "task_id": task_id
                        }
                        # Include artifact if present in result_data
                        if t.get("result_data") and "artifact" in t.get("result_data", {}):
                            result["artifact"] = t["result_data"]["artifact"]

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # === API TOOLS (Use BlackBox HTTP API) ===

        async with httpx.AsyncClient(timeout=60) as client:

            if name == "search_snapshots":
                query = arguments["query"]
                # Read tool: omitting operator means ALL operators on this box (do NOT force-resolve).
                operator = arguments.get("operator", "")
                limit = arguments.get("limit", 10)

                response = await client.get(
                    f"{BLACKBOX_URL}/fossil/hybrid",
                    params={"q": query, "operator": operator, "limit": limit}
                )
                response.raise_for_status()
                return [TextContent(type="text", text=json.dumps(response.json(), indent=2))]

            elif name == "mint_snapshot":
                content = arguments["content"]
                operator = await resolve_operator(arguments.get("operator"))
                snap_type = arguments.get("snapshot_type", "normal")

                # Route through /chat for AI processing, auto-mint will capture the result
                # This produces higher quality snapshots with proper headers and synthesis
                chat_response = await client.post(
                    f"{BLACKBOX_URL}/chat",
                    json={
                        "operator": operator,
                        "messages": [{"role": "user", "content": content}],
                        "provider": "google",
                        "model": "gemini-2.5-pro",
                        "streaming": False
                    },
                    timeout=60.0
                )
                chat_response.raise_for_status()
                chat_result = chat_response.json()

                # Refresh index after auto-mint captures the snapshot
                await asyncio.sleep(0.5)  # Brief wait for auto-mint to complete
                load_snapshot_index(force_refresh=True)

                # Get the latest snapshot ID for this operator
                recent = list_snapshots_by_operator(operator, 1)
                snap_id = recent[0]["snap_id"] if recent else "auto-minted"

                return [TextContent(type="text", text=f"Snapshot created via AI processing: {snap_id}\nAI acknowledged the content and auto-mint captured the conversation.")]

            elif name == "get_context":
                query = arguments["query"]
                operator = await resolve_operator(arguments.get("operator"))

                # Get hybrid search results
                search_response = await client.get(
                    f"{BLACKBOX_URL}/fossil/hybrid",
                    params={"q": query, "operator": operator, "limit": 5}
                )
                search_response.raise_for_status()
                relevant = search_response.json()

                # Get recent snapshots from local index
                recent = list_snapshots_by_operator(operator, 5)

                context = {
                    "query": query,
                    "operator": operator,
                    "relevant_snapshots": relevant,
                    "recent_snapshots": recent
                }

                return [TextContent(type="text", text=json.dumps(context, indent=2))]

            elif name == "chat_with_context":
                message = arguments["message"]
                operator = await resolve_operator(arguments.get("operator"))
                provider = arguments.get("provider", "anthropic")
                model = arguments.get("model")

                payload = {
                    "operator": operator,
                    "messages": [{"role": "user", "content": message}],
                    "provider": provider
                }
                if model:
                    payload["model"] = model

                response = await client.post(
                    f"{BLACKBOX_URL}/chat",
                    json=payload,
                    timeout=120
                )
                response.raise_for_status()
                result = response.json()

                # Poll for completion if async
                if "task_id" in result:
                    task_id = result["task_id"]
                    for _ in range(120):  # Wait up to 2 minutes
                        await asyncio.sleep(1)
                        task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                        task_data = task_response.json()
                        if task_data.get("status") == "completed":
                            # Response is in result_data.reply or result_data.ui_reply
                            result_data = task_data.get("result_data", {})
                            response_text = result_data.get("reply") or result_data.get("ui_reply") or result_data.get("text") or "No response"
                            return [TextContent(type="text", text=response_text)]
                        elif task_data.get("status") == "failed":
                            error_msg = task_data.get("error_message") or task_data.get("error") or "Unknown error"
                            return [TextContent(type="text", text=f"Chat failed: {error_msg}")]
                    return [TextContent(type="text", text="Chat request timed out")]

                return [TextContent(type="text", text=result.get("response", json.dumps(result)))]

            # ===========================================
            # MULTIMODAL TOOLS - Generation & Analysis
            # ===========================================

            elif name == "generate_image":
                prompt = arguments["prompt"]
                operator = await resolve_operator(arguments.get("operator"))

                response = await client.post(
                    f"{BLACKBOX_URL}/generate/image",
                    json={"prompt": prompt, "operator": operator},
                    timeout=120
                )
                response.raise_for_status()
                result = response.json()

                if "task_id" in result:
                    return [TextContent(type="text", text=json.dumps({
                        "status": "generating",
                        "task_id": result["task_id"],
                        "message": "Image generation started. Use get_task_status to check progress."
                    }, indent=2))]
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "generate_video":
                prompt = arguments["prompt"]
                operator = await resolve_operator(arguments.get("operator"))
                video_url = arguments.get("video_url")
                image_url = arguments.get("image_url")
                image_base64 = arguments.get("image_base64")
                image_mime_type = arguments.get("image_mime_type")

                # VIDEO EXTENSION: If video_url is provided, use /extend/video endpoint
                if video_url:
                    print(f"[MCP] generate_video: VIDEO EXTENSION mode with video_url={video_url}", file=sys.stderr)
                    response = await client.post(
                        f"{BLACKBOX_URL}/extend/video",
                        json={"video_url": video_url, "prompt": prompt, "operator": operator},
                        timeout=60
                    )
                    response.raise_for_status()
                    result = response.json()

                    return [TextContent(type="text", text=json.dumps({
                        "status": "generating",
                        "task_id": result.get("task_id"),
                        "message": "Video EXTENSION started (5-20 minutes). Use get_task_status to check progress.",
                        "mode": "video_extension",
                        "source_video": video_url,
                        "details": result
                    }, indent=2))]

                # IMAGE-TO-VIDEO or TEXT-TO-VIDEO
                payload = {"prompt": prompt, "operator": operator}

                # Option 1: Pass image_url directly to REST endpoint (preferred - server handles conversion)
                if image_url:
                    payload["image_url"] = image_url
                    print(f"[MCP] generate_video: IMAGE-TO-VIDEO mode with image_url={image_url}", file=sys.stderr)

                # Option 2: Pass base64 image data in the expected format
                elif image_base64:
                    payload["image"] = {
                        "data": image_base64,
                        "mime_type": image_mime_type or "image/jpeg"
                    }
                    print(f"[MCP] generate_video: IMAGE-TO-VIDEO mode with base64 ({len(image_base64)} chars)", file=sys.stderr)
                else:
                    print(f"[MCP] generate_video: TEXT-TO-VIDEO mode", file=sys.stderr)

                response = await client.post(
                    f"{BLACKBOX_URL}/generate/video",
                    json=payload,
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "generating",
                    "task_id": result.get("task_id"),
                    "message": "Video generation started (5-20 minutes). Use get_task_status to check progress.",
                    "mode": "image_to_video" if image_url or image_base64 else "text_to_video",
                    "source_image": image_url if image_url else None,
                    "details": result
                }, indent=2))]

            elif name == "extend_video":
                video_url = arguments["video_url"]
                operator = await resolve_operator(arguments.get("operator"))
                prompt = arguments.get("prompt", "")

                response = await client.post(
                    f"{BLACKBOX_URL}/extend/video",
                    json={"video_url": video_url, "prompt": prompt, "operator": operator},
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "extending",
                    "task_id": result.get("task_id"),
                    "message": "Video extension started (5-20 minutes). Use get_task_status to check progress.",
                    "details": result
                }, indent=2))]

            elif name == "lyria_music":
                prompt = arguments["prompt"]
                operator = await resolve_operator(arguments.get("operator"))

                response = await client.post(
                    f"{BLACKBOX_URL}/generate/lyria_music",
                    json={"prompt": prompt, "operator": operator},
                    timeout=120
                )
                response.raise_for_status()
                result = response.json()

                if "task_id" in result:
                    return [TextContent(type="text", text=json.dumps({
                        "status": "generating",
                        "task_id": result["task_id"],
                        "message": "Music generation started. Use get_music_status or get_task_status to check progress."
                    }, indent=2))]
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "text_to_speech":
                text = arguments["text"]
                voice = arguments.get("voice", "onyx")
                model = arguments.get("model", "tts-1-hd")

                response = await client.post(
                    f"{BLACKBOX_URL}/tts",
                    json={"text": text, "voice": voice, "model": model, "return_json": True},
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "audio_url": result.get("audio_url"),
                    "voice": voice,
                    "model": model,
                    "message": "Audio generated successfully. Access the audio_url to play."
                }, indent=2))]

            elif name == "gemini_pro_tts":
                text = arguments["text"]
                voice = arguments.get("voice", "Charon")
                operator = await resolve_operator(arguments.get("operator"))

                response = await client.post(
                    f"{BLACKBOX_URL}/generate/gemini_tts",
                    json={
                        "text": text,
                        "voice_name": voice,
                        "operator": operator,
                        "multi_speaker": False,
                        "model": "gemini-2.5-pro-tts"
                    },
                    timeout=120  # Gemini Pro TTS takes longer
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "generating",
                    "task_id": result.get("task_id"),
                    "voice": voice,
                    "model": "gemini-pro-tts",
                    "message": "Gemini Pro TTS generation started. Use get_task_status to check progress and get audio_url."
                }, indent=2))]

            elif name == "speech_to_text":
                audio_path = arguments["audio_path"]

                # Read the audio file and upload as multipart form data
                audio_file_path = Path(audio_path)
                if not audio_file_path.exists():
                    return [TextContent(type="text", text=json.dumps({
                        "status": "error",
                        "error": f"Audio file not found: {audio_path}"
                    }, indent=2))]

                # Determine content type from extension
                ext = audio_file_path.suffix.lower()
                content_types = {
                    ".wav": "audio/wav",
                    ".mp3": "audio/mpeg",
                    ".m4a": "audio/mp4",
                    ".ogg": "audio/ogg",
                    ".flac": "audio/flac",
                    ".webm": "audio/webm"
                }
                content_type = content_types.get(ext, "audio/wav")

                with open(audio_file_path, "rb") as f:
                    files = {"file": (audio_file_path.name, f, content_type)}
                    response = await client.post(
                        f"{BLACKBOX_URL}/stt",
                        files=files,
                        timeout=120
                    )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "transcription": result.get("text", result.get("transcription", "")),
                    "details": result
                }, indent=2))]

            elif name == "analyze_audio":
                file_path = arguments["file_path"]
                prompt = arguments.get("prompt", "Transcribe and describe this audio")
                operator = await resolve_operator(arguments.get("operator"))

                response = await client.post(
                    f"{BLACKBOX_URL}/analyze/audio",
                    json={"file_path": file_path, "prompt": prompt, "operator": operator},
                    timeout=120
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "analysis": result.get("response", result.get("analysis", "")),
                    "details": result
                }, indent=2))]

            elif name == "analyze_image":
                image_url = arguments["image_url"]
                prompt = arguments.get("prompt", "Describe this image in detail")
                operator = await resolve_operator(arguments.get("operator"))
                provider = arguments.get("provider", "gemini")

                # Use chat endpoint with image content for multimodal analysis
                response = await client.post(
                    f"{BLACKBOX_URL}/chat",
                    json={
                        "operator": operator,
                        "provider": provider,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": image_url}}
                            ]
                        }]
                    },
                    timeout=120
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "analysis": result.get("response", ""),
                    "provider": provider,
                    "details": result
                }, indent=2))]

            elif name == "analyze_video":
                video_url = arguments["video_url"]
                prompt = arguments.get("prompt", "Describe what happens in this video")
                operator = await resolve_operator(arguments.get("operator"))

                # Use Gemini for video analysis (best multimodal video support)
                response = await client.post(
                    f"{BLACKBOX_URL}/chat",
                    json={
                        "operator": operator,
                        "provider": "gemini",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "video_url", "video_url": {"url": video_url}}
                            ]
                        }]
                    },
                    timeout=180
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "analysis": result.get("response", ""),
                    "details": result
                }, indent=2))]

            elif name == "use_computer":
                prompt = arguments["prompt"]
                operator = await resolve_operator(arguments.get("operator"))
                url = arguments.get("url")
                device_id = arguments.get("device_id", "blackbox")

                payload = {"prompt": prompt, "operator": operator, "device_id": device_id}
                if url:
                    payload["url"] = url

                response = await client.post(
                    f"{BLACKBOX_URL}/browser/run",
                    json=payload,
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "started",
                    "task_id": result.get("task_id"),
                    "message": "Sovereign Browser task started. Use get_task_status to check progress and see screenshots when complete."
                }, indent=2))]

            elif name == "list_devices":
                from Orchestrator.device_registry import get_registry, DeviceType
                registry = get_registry()
                dtype = arguments.get("device_type")
                if dtype:
                    devices = registry.get_devices_by_type(DeviceType(dtype))
                else:
                    devices = registry.get_all_devices()
                device_list = [d.to_dict() for d in devices]
                return [TextContent(type="text", text=json.dumps({"devices": device_list}, indent=2))]

            elif name == "control_android_device":
                operator = await resolve_operator(arguments.get("operator"))
                response = await client.post(
                    f"{BLACKBOX_URL}/gemini-cu/run",
                    json={
                        "prompt": arguments["prompt"],
                        "device_id": arguments["device_id"],
                        "operator": operator
                    },
                    timeout=30
                )
                result = response.json()
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "get_task_status":
                task_id = arguments["task_id"]

                response = await client.get(
                    f"{BLACKBOX_URL}/tasks/{task_id}",
                    timeout=30
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "list_tts_voices":
                response = await client.get(
                    f"{BLACKBOX_URL}/tts/google/voices",
                    timeout=30
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "get_music_status":
                response = await client.get(
                    f"{BLACKBOX_URL}/music/status",
                    timeout=30
                )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name in ("gmail_search", "gmail_read", "gmail_send", "gmail_reply", "gmail_labels"):
                operator = await resolve_operator(arguments.get("operator"))
                params = {k: v for k, v in arguments.items() if k != "operator"}
                resp = await client.post(
                    f"{BLACKBOX_URL}/gmail/execute",
                    json={"tool": name, "params": params, "operator": operator},
                )
                data_g = resp.json()
                if data_g.get("success"):
                    return [TextContent(type="text", text=str(data_g.get("result", "")))]
                return [TextContent(type="text", text=f"Gmail error: {data_g.get('error') or data_g.get('result') or 'unknown'}")]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"HTTP Error: {str(e)}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# =============================================================================
# RESOURCES - Data the agent can read
# =============================================================================

@server.list_resources()
async def list_resources() -> list[Resource]:
    """List available BlackBox resources."""
    return [
        Resource(
            uri="blackbox://index/stats",
            name="Snapshot Index Statistics",
            description="Statistics about the snapshot index - counts, operators, sizes",
            mimeType="application/json"
        ),
        Resource(
            uri="blackbox://index/operators",
            name="Operator List",
            description="List of all operators with snapshot counts",
            mimeType="application/json"
        ),
        Resource(
            uri="blackbox://index/recent",
            name="Recent Snapshots",
            description="Most recent 20 snapshots across all operators",
            mimeType="application/json"
        ),
        Resource(
            uri="blackbox://volume/info",
            name="Volume Information",
            description="Information about the snapshot volume file",
            mimeType="application/json"
        )
    ]


@server.read_resource()
async def read_resource(uri: str) -> ResourceContents:
    """Read a BlackBox resource."""

    try:
        if uri == "blackbox://index/stats":
            index = load_snapshot_index()

            operators = {}
            types = {"normal": 0, "checkpoint": 0, "summary": 0}
            total_bytes = 0

            for entry in index.values():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1
                snap_type = entry.get("type", "normal")
                if snap_type in types:
                    types[snap_type] += 1
                total_bytes += entry.get("byte_end", 0) - entry.get("byte_start", 0)

            stats = {
                "total_snapshots": len(index),
                "operators": operators,
                "types": types,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2)
            }

            return TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(stats, indent=2))

        elif uri == "blackbox://index/operators":
            index = load_snapshot_index()
            operators = {}
            for entry in index.values():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1

            return TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=json.dumps({"operators": operators}, indent=2)
            )

        elif uri == "blackbox://index/recent":
            index = load_snapshot_index()

            entries = []
            for snap_id, entry in index.items():
                entries.append({
                    "snap_id": snap_id,
                    "operator": entry.get("operator"),
                    "timestamp": entry.get("timestamp"),
                    "type": entry.get("type", "normal")
                })

            entries.sort(key=lambda x: x["timestamp"], reverse=True)

            return TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=json.dumps({"recent_snapshots": entries[:20]}, indent=2)
            )

        elif uri == "blackbox://volume/info":
            info = {
                "volume_path": str(VOLUME_FILE),
                "exists": VOLUME_FILE.exists(),
                "size_bytes": VOLUME_FILE.stat().st_size if VOLUME_FILE.exists() else 0,
                "size_mb": round(VOLUME_FILE.stat().st_size / (1024 * 1024), 2) if VOLUME_FILE.exists() else 0,
                "index_path": str(SNAPSHOT_INDEX),
                "index_exists": SNAPSHOT_INDEX.exists()
            }
            return TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(info, indent=2))

        else:
            return TextResourceContents(uri=uri, mimeType="text/plain", text=f"Unknown resource: {uri}")

    except Exception as e:
        return TextResourceContents(uri=uri, mimeType="text/plain", text=f"Error: {str(e)}")


# =============================================================================
# MAIN
# =============================================================================

async def main():
    """Run the BlackBox MCP server."""
    print(f"BlackBox MCP Server starting...", file=sys.stderr)
    print(f"BlackBox Root: {BLACKBOX_ROOT}", file=sys.stderr)
    print(f"BlackBox API: {BLACKBOX_URL}", file=sys.stderr)
    print(f"Volume File: {VOLUME_FILE} (exists: {VOLUME_FILE.exists()})", file=sys.stderr)
    print(f"Index File: {SNAPSHOT_INDEX} (exists: {SNAPSHOT_INDEX.exists()})", file=sys.stderr)

    # Pre-load the index
    index = load_snapshot_index()
    print(f"Loaded {len(index)} snapshots from index", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
