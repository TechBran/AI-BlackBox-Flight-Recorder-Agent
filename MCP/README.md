# BlackBox MCP Server

Model Context Protocol (MCP) server that exposes BlackBox Flight Recorder functionality to Claude Code and other MCP-compatible AI agents.

## Overview

This MCP server provides AI agents with full access to:
- **Snapshot Search** - Semantic and keyword search across all memories
- **Snapshot Retrieval** - Get specific snapshots by ID using byte offsets
- **Memory Creation** - Mint new snapshots to remember information
- **Context Enrichment** - Get relevant historical context for queries
- **Manifest Access** - Read the byte-offset index for efficient traversal

## Installation

```bash
# Create virtual environment (optional but recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

## Configuration

### For Claude Code

Copy the example config to your Claude Code settings:

```bash
# Linux/Mac
mkdir -p ~/.claude
cp claude_mcp_config.example.json ~/.claude/mcp.json

# Edit paths if needed
nano ~/.claude/mcp.json
```

### Environment Variables

- `BLACKBOX_URL` - BlackBox API URL (default: `http://localhost:9091`)
- `BLACKBOX_DATA_DIR` - Path to BlackBox data directory

## Available Tools

### Memory & Search Tools

| Tool | Description |
|------|-------------|
| `search_snapshots` | Search memories using semantic + keyword search |
| `get_snapshot` | Retrieve a specific snapshot by ID |
| `seek_snapshot_direct` | Efficient byte-offset retrieval from volume |
| `list_recent_snapshots` | Get recent snapshots for an operator |
| `mint_snapshot` | Create a new memory/snapshot |
| `get_context` | Get enriched context for a query |
| `list_operators` | List all users in the system |
| `get_index_stats` | Get statistics about the snapshot store |
| `browse_index` | Browse/paginate through snapshots |
| `chat_with_context` | Send a message through BlackBox with full context |
| `refresh_index` | Force reload the snapshot index cache |

### Multimodal Generation Tools

| Tool | Description |
|------|-------------|
| `generate_image` | Generate images from text prompts |
| `generate_video` | Generate videos using Veo 3.1 (text-to-video or image-to-video) |
| `extend_video` | Extend existing videos with Veo 3.1 |
| `lyria_music` | Generate 30-second music with Google Lyria |
| `text_to_speech` | Convert text to speech (OpenAI TTS) |
| `speech_to_text` | Transcribe audio to text (OpenAI Whisper) |

### Multimodal Analysis Tools

| Tool | Description |
|------|-------------|
| `analyze_audio` | Analyze/transcribe audio with Gemini |
| `analyze_image` | Describe, extract text, identify objects in images |
| `analyze_video` | Describe and analyze video content |

### Status & Utility Tools

| Tool | Description |
|------|-------------|
| `get_task_status` | Check async generation task progress |
| `list_tts_voices` | List available Google Cloud TTS voices |
| `get_music_status` | Check Lyria music generation availability |

## Available Resources

| Resource URI | Description |
|--------------|-------------|
| `blackbox://manifest/stats` | Snapshot counts, sizes, operators |
| `blackbox://manifest/index` | Byte-offset index summary |
| `blackbox://operators` | List of all operators |

## Example Usage (from Claude Code)

Once configured, Claude Code can use these tools naturally:

```
User: "What did we discuss about the authentication system?"

Claude: [Uses search_snapshots("authentication system", "Brandon", 5)]
        Found relevant snapshots:
        - SNAP-20251115-xxx: Discussion about JWT tokens...

        Based on your previous conversations, you decided to...
```

## Deployment Notes

When creating a system image for new customers:

1. The MCP server is included in the image
2. Paths in `~/.claude/mcp.json` should use the standard location
3. New customers start with empty snapshot volume
4. MCP server auto-connects to local BlackBox instance

## Testing

```bash
# Test the server manually
python blackbox_mcp_server.py

# The server communicates via stdio (stdin/stdout)
# Claude Code handles this automatically
```

## Architecture

```
┌─────────────────────┐     stdio/MCP      ┌──────────────────────┐
│   Claude Code       │◄──────────────────►│  BlackBox MCP Server │
│   (AI Agent)        │                    │  (This server)       │
└─────────────────────┘                    └──────────┬───────────┘
                                                      │ HTTP
                                                      ▼
                                           ┌──────────────────────┐
                                           │  BlackBox Orchestrator│
                                           │  localhost:9091      │
                                           └──────────┬───────────┘
                                                      │
                                                      ▼
                                           ┌──────────────────────┐
                                           │  Snapshot Volume     │
                                           │  + Manifest Index    │
                                           │  + Embeddings        │
                                           └──────────────────────┘
```
