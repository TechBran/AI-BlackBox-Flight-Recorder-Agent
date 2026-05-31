"""
tool_registry.py - Single Source of Truth for all BlackBox tool definitions.

Define each tool ONCE in a canonical (provider-agnostic) format.
Format converters generate correct schemas for any AI provider:
  - Anthropic (input_schema)
  - OpenAI REST (type: function, function: {name, parameters})
  - OpenAI Realtime / Grok Live (type: function, name, parameters)
  - Gemini REST (function_declarations, snake_case)
  - Gemini Live (functionDeclarations, camelCase)
  - MCP (Tool objects with inputSchema)

Groups control which tools appear for each consumer:
  chat         - REST chat handlers (all providers, ~32 tools)
  chat_cu      - Computer Use agent (chat minus use_computer itself)
  realtime     - OpenAI Realtime voice WebSocket (~21 tools)
  gemini_live  - Gemini Live voice WebSocket (~21 tools)
  grok_live    - Grok Live voice WebSocket (~21 tools)
  phone        - Phone bridge / blackbox_tools.py (~24 tools)
  mcp          - MCP server for Claude Code (~30 tools)
"""

from typing import Dict, List, Optional, Any
import copy


# =============================================================================
# Group shorthand constants (for readability in definitions)
# =============================================================================

_ALL = ["chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"]
_ALL_NO_MCP = ["chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone"]
_CHAT_PHONE_MCP = ["chat", "chat_cu", "phone", "mcp"]
_CHAT_MCP = ["chat", "chat_cu", "mcp"]
_CHAT_PHONE = ["chat", "chat_cu", "phone"]
_MCP_ONLY = ["mcp"]


# =============================================================================
# Canonical Tool Definitions
# =============================================================================

TOOL_DEFINITIONS: List[Dict[str, Any]] = [

    # ── Web Tools ──────────────────────────────────────────────────────────

    {
        "name": "web_search",
        "description": "Search the web using Perplexity Sonar AI search. Returns a synthesized answer with source citations.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up on the web"
                },
                "search_recency_filter": {
                    "type": "string",
                    "description": "Filter results by recency: 'day', 'week', 'month', 'year' (default: 'month')",
                    "enum": ["hour", "day", "week", "month", "year"],
                    "default": "month"
                }
            },
            "required": ["query"]
        },
        "groups": _ALL,
    },
    {
        "name": "web_fetch",
        "description": "Fetch and extract clean content from a specific URL. Use this to read articles, documentation, blog posts, or web pages when you have the URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The complete URL to fetch (must start with http:// or https://)"
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 80000, optional)",
                    "default": 80000
                }
            },
            "required": ["url"]
        },
        "groups": _ALL,
    },

    # ── Media Generation Tools ─────────────────────────────────────────────

    {
        "name": "generate_image",
        "description": "Generate images using Gemini's Nano Banana Pro (gemini-3-pro-image-preview). Supports text-to-image AND image-to-image. For image-to-image, pass reference_images URLs from previous generations to guide style/content. Returns a task_id that completes asynchronously.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed description of the image to generate. Be specific about subjects, style, composition, lighting, colors, and mood."
                },
                "reference_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs of reference images for style/content guidance (e.g., ['/ui/uploads/sunset_abc123.png']). Use for image-to-image generation. Max 10 images recommended.",
                    "maxItems": 14
                },
                "aspectRatio": {
                    "type": "string",
                    "description": "Aspect ratio: '1:1' (square), '16:9' (landscape), '9:16' (portrait), '4:3', '3:4'. Default: '16:9'",
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                    "default": "16:9"
                },
                "resolution": {
                    "type": "string",
                    "description": "Image resolution: '1K' (1024px), '2K' (2048px), '4K' (4096px). Higher = more detail but slower. Default: '1K'",
                    "enum": ["1K", "2K", "4K"],
                    "default": "1K"
                },
                "numberOfImages": {
                    "type": "integer",
                    "description": "Number of images to generate (1-4). Default: 1",
                    "minimum": 1,
                    "maximum": 4,
                    "default": 1
                }
            },
            "required": ["prompt"]
        },
        "groups": _ALL,
    },
    {
        "name": "generate_video",
        "description": "Generate videos using Google's Veo 3.1. Supports THREE modes: (1) text-to-video (just prompt), (2) image-to-video (pass image_url to animate an image), (3) video-extension (pass video_url to extend/continue an existing video). Returns a task_id (takes 5-20 minutes).",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "For text-to-video: full video description. For image-to-video: describe how the image should animate. For video-extension: describe what happens NEXT in the video."
                },
                "image_url": {
                    "type": "string",
                    "description": "URL of an image to animate (e.g., /ui/uploads/sunset_abc123.png). If provided, creates image-to-video instead of text-to-video."
                },
                "video_url": {
                    "type": "string",
                    "description": "URL of a video to EXTEND. If provided, creates a continuation of that video guided by the prompt. Takes priority over image_url."
                },
                "aspectRatio": {
                    "type": "string",
                    "description": "Aspect ratio: '16:9' (landscape) or '9:16' (portrait). Default: '16:9'",
                    "enum": ["16:9", "9:16"],
                    "default": "16:9"
                },
                "duration": {
                    "type": "integer",
                    "description": "Video duration in seconds: 4, 6, or 8. Default: 8",
                    "enum": [4, 6, 8],
                    "default": 8
                },
                "resolution": {
                    "type": "string",
                    "description": "Video resolution: '720p' or '1080p'. IMPORTANT: Video extension (video_url) REQUIRES '720p'. Default: '720p'",
                    "enum": ["720p", "1080p"],
                    "default": "720p"
                },
                "negativePrompt": {
                    "type": "string",
                    "description": "Optional. Things to exclude from the video"
                }
            },
            "required": ["prompt"]
        },
        "groups": _ALL,
    },
    {
        "name": "generate_music",
        "description": "Generate 30-second instrumental music using Lyria-002. CRITICAL: Translate user requests into allowed vocabulary. The API REJECTS genre names, style words, and artist references. ONLY USE: instrument names + tempo + texture. Example: 'EDM' -> 'Synthesizer arpeggios with pulsing bass, fast driving tempo'.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "TRANSLATED description using ONLY: Instruments (piano, guitar, drums, bass, synth, strings, brass, etc.) + Tempo (fast, slow, moderate, driving, laid-back) + Texture (warm, bright, dark, smooth, aggressive, dense, sparse). FORBIDDEN: genre names, style words, artist names."
                },
                "negativePrompt": {
                    "type": "string",
                    "description": "Things to exclude (e.g., 'vocals, singing')"
                },
                "sampleCount": {
                    "type": "integer",
                    "description": "Variations to generate (1-4, default 1)"
                }
            },
            "required": ["prompt"]
        },
        "groups": _ALL,
    },
    {
        "name": "extend_video",
        "description": "Extend an existing video using Veo 3.1. Takes 5-20 minutes. Returns task_id (async). The new clip continues from where the original ended.",
        "parameters": {
            "type": "object",
            "properties": {
                "video_url": {
                    "type": "string",
                    "description": "URL of the video to extend (e.g. /ui/uploads/video.mp4)"
                },
                "prompt": {
                    "type": "string",
                    "description": "Optional prompt for how to extend the video"
                }
            },
            "required": ["video_url"]
        },
        "groups": _CHAT_MCP,
    },

    # ── Media Management Tools ─────────────────────────────────────────────

    {
        "name": "get_media",
        "description": "Retrieve a previously generated media file (image, video, music) by URL or task_id. Use to verify media exists before using it, or to get metadata about past generations.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Media URL (e.g., /ui/uploads/sunset_abc123.png from a previous generation)"
                },
                "task_id": {
                    "type": "string",
                    "description": "Alternative: Task ID from a previous generation (will look up the URL)"
                }
            },
            "required": []
        },
        "groups": _ALL,
    },
    {
        "name": "list_media",
        "description": "List media files (images, videos, audio) in the uploads folder. Use to see what media is available for image-to-video, video extension, or image-to-image.",
        "parameters": {
            "type": "object",
            "properties": {
                "media_type": {
                    "type": "string",
                    "description": "Filter by type: 'image', 'video', 'audio', or omit for all types",
                    "enum": ["image", "video", "audio"]
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default 20, max 50)",
                    "default": 20
                }
            },
            "required": []
        },
        "groups": _ALL_NO_MCP,
    },
    {
        "name": "search_media",
        "description": "Search for media files by description, prompt, or filename. Use when you need to find a specific image/video/audio that was previously generated or uploaded.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query - matches against prompts, descriptions, and filenames"
                },
                "media_type": {
                    "type": "string",
                    "description": "Filter by type: 'image', 'video', 'audio', or omit for all types",
                    "enum": ["image", "video", "audio"]
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default 10)",
                    "default": 10
                }
            },
            "required": ["query"]
        },
        "groups": _ALL_NO_MCP,
    },

    # ── Memory / Snapshot Tools ────────────────────────────────────────────

    {
        "name": "search_snapshots",
        "description": "Your primary memory tool. The BlackBox contains 1,600+ snapshots — an unlimited bucket of memory holding every past conversation, development session, bug fix, decision, user preference, and interaction. Search this FIRST and OFTEN. Everything about the user, their projects, past work, preferences, and recent activity lives here. When in doubt, search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query. Be descriptive — 'CSS design token migration' finds more than 'CSS'. Search multiple times with different queries if needed."
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. Omit to include ALL operators on this box; pass a name to scope results to one operator."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)",
                    "default": 10
                }
            },
            "required": ["query"]
        },
        "groups": _ALL,
    },
    {
        "name": "get_snapshot",
        "description": "Retrieve a specific snapshot by ID from the BlackBox memory. Use when you have a snapshot ID from search results and need the full content.",
        "parameters": {
            "type": "object",
            "properties": {
                "snap_id": {
                    "type": "string",
                    "description": "Snapshot ID (e.g., SNAP-20260208-1234)"
                },
                "include_content": {
                    "type": "boolean",
                    "description": "Include full content (default: true)",
                    "default": True
                }
            },
            "required": ["snap_id"]
        },
        "groups": _CHAT_PHONE_MCP,
    },
    {
        "name": "list_recent_snapshots",
        "description": "Get the most recent snapshots. IMPORTANT: Use this at the START of outbound calls to check for context from other models - there may be task details, order information, or instructions provided before this call was initiated.",
        "parameters": {
            "type": "object",
            "properties": {
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                },
                "count": {
                    "type": "integer",
                    "description": "Number of recent snapshots to retrieve (default: 3, max: 5)",
                    "default": 3
                }
            },
            "required": []
        },
        "groups": _ALL,
    },
    {
        "name": "get_current_time",
        "description": "Get the current date and time with timezone information.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "groups": _ALL,
    },

    # ── Communication Tools ────────────────────────────────────────────────

    {
        "name": "send_sms",
        "description": "Send an SMS text message via the TG200 cellular gateway. The message is delivered as a real cellular SMS. For long messages, the system automatically splits into multiple SMS segments (160 chars each, up to 10 segments). Use search_contacts first to find the recipient's number.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {
                    "type": "string",
                    "description": "Phone number in E.164 format (e.g., +15551234567) or 10-digit US format"
                },
                "message": {
                    "type": "string",
                    "description": "The text message to send. Plain text only, no markdown. Max ~1500 characters (auto-split into 160-char segments)."
                }
            },
            "required": ["phone_number", "message"]
        },
        "groups": _ALL_NO_MCP,
    },
    {
        "name": "make_phone_call",
        "description": "DELEGATE a real phone call to a separate AI voice agent via the TG200 cellular gateway. You are NOT making the call yourself — you are BRIEFING another AI agent who will have a real-time voice conversation on your behalf. Use 'role' to define WHO the agent IS (their persona/character), and 'greeting' for WHAT task to accomplish on this call. Use search_contacts first to find the number.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {
                    "type": "string",
                    "description": "Phone number in E.164 format (e.g., +15551234567) or 10-digit US format"
                },
                "role": {
                    "type": "string",
                    "description": "The PERSONA/CHARACTER for the AI voice agent — define WHO they are. This becomes the agent's system prompt before the call starts."
                },
                "greeting": {
                    "type": "string",
                    "description": "The TASK INSTRUCTIONS — WHAT to do on this specific call. The voice agent speaks this greeting first, then has a free-form conversation."
                },
                "backend": {
                    "type": "string",
                    "description": "AI voice backend for the real-time conversation",
                    "enum": ["openai_realtime", "gemini_live", "grok_live"]
                }
            },
            "required": ["phone_number"]
        },
        "groups": _ALL_NO_MCP,
    },
    {
        "name": "make_voice_call",
        "description": "Call a phone number and deliver a pre-recorded voice message using TTS. The system generates the speech audio first (via OpenAI TTS), then calls the number and plays it. For interactive two-way calls, use make_phone_call instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {
                    "type": "string",
                    "description": "Phone number in E.164 format (e.g., +15551234567) or 10-digit US format"
                },
                "message": {
                    "type": "string",
                    "description": "The message to speak. Will be converted to speech via TTS before calling."
                },
                "voice": {
                    "type": "string",
                    "description": "TTS voice to use for the message",
                    "enum": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
                }
            },
            "required": ["phone_number", "message"]
        },
        "groups": _ALL_NO_MCP,
    },

    # ── Contact Tools ──────────────────────────────────────────────────────

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
        },
        "groups": _ALL_NO_MCP,
    },
    {
        "name": "save_contact",
        "description": "Save a new contact or update an existing one in the contact book. Use when a user mentions someone new with contact info.",
        "parameters": {
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
        },
        "groups": _ALL_NO_MCP,
    },

    # ── Cron Job Tools ─────────────────────────────────────────────────────

    {
        "name": "create_cron_job",
        "description": "Create a scheduled task (cron job) that runs a prompt on a schedule. Use when the user wants recurring reminders, checks, or automated tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "A short, descriptive name for this task (e.g., 'Morning Server Check')"
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt/instruction that will be sent to the AI when the job fires"
                },
                "schedule": {
                    "type": "string",
                    "description": "Cron expression (e.g., '0 7 * * *' for daily at 7 AM, '*/30 * * * *' for every 30 minutes)"
                },
                "frequency_hint": {
                    "type": "string",
                    "description": "Human-readable description of the schedule (e.g., 'Every day at 7 AM')"
                },
                "model": {
                    "type": "string",
                    "description": "Which AI model should execute this job. Use 'computer-use' for tasks needing desktop/browser/GUI interaction (default: gemini)",
                    "enum": ["gemini", "openai", "claude", "grok", "computer-use"]
                },
                "delivery": {
                    "type": "string",
                    "description": "How to deliver the result (default: snapshot)",
                    "enum": ["snapshot", "sms", "voice_call", "notification"]
                },
                "delivery_target": {
                    "type": "string",
                    "description": "Phone number for SMS/voice delivery (E.164 format), not needed for snapshot/notification"
                },
                "one_shot": {
                    "type": "boolean",
                    "description": "If true, run once and auto-delete (for one-time reminders). Default: false"
                }
            },
            "required": ["name", "prompt", "schedule"]
        },
        "groups": _ALL_NO_MCP,
    },
    {
        "name": "edit_cron_job",
        "description": "Edit an existing scheduled task (cron job). Change the prompt, schedule, delivery method, or other settings.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the cron job to edit"
                },
                "name": {
                    "type": "string",
                    "description": "New name for the job"
                },
                "prompt": {
                    "type": "string",
                    "description": "New prompt/instruction for the job"
                },
                "schedule": {
                    "type": "string",
                    "description": "New cron expression"
                },
                "frequency_hint": {
                    "type": "string",
                    "description": "New human-readable schedule description"
                },
                "model": {
                    "type": "string",
                    "description": "Change which AI model executes this job",
                    "enum": ["gemini", "openai", "claude", "grok", "computer-use"]
                },
                "delivery": {
                    "type": "string",
                    "description": "Change how results are delivered",
                    "enum": ["snapshot", "sms", "voice_call", "notification"]
                },
                "delivery_target": {
                    "type": "string",
                    "description": "New phone number for SMS/voice delivery"
                },
                "pause": {
                    "type": "boolean",
                    "description": "Set to true to pause the job, false to resume it"
                }
            },
            "required": ["job_id"]
        },
        "groups": _ALL_NO_MCP,
    },
    {
        "name": "search_cron_jobs",
        "description": "Search and list scheduled tasks (cron jobs). Use when the user asks about their scheduled tasks, reminders, or cron jobs.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional search query to filter jobs by name or prompt content"
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status (default: all)",
                    "enum": ["active", "paused", "all"]
                }
            },
            "required": []
        },
        "groups": _ALL_NO_MCP,
    },

    # ── Computer Control Tools ─────────────────────────────────────────────

    {
        "name": "use_computer",
        "description": "Control the computer using an AI agent powered by Claude Opus 4.6. Can browse the web, use desktop apps, run terminal commands, manage files — full Linux desktop access. Returns a task ID - use get_task_status to check progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to do on the computer (e.g., 'Go to news.ycombinator.com and find the top 3 stories')"
                },
                "url": {
                    "type": "string",
                    "description": "Starting URL to navigate to (optional, defaults to about:blank)"
                },
                "device_id": {
                    "type": "string",
                    "description": "Target device ID from the Tailscale mesh (default: 'blackbox' for local machine). Use list_devices to see available devices."
                }
            },
            "required": ["prompt"]
        },
        # NOTE: Excluded from chat_cu (CU agent already has native computer control)
        "groups": ["chat", "realtime", "gemini_live", "grok_live", "phone", "mcp"],
    },
    {
        "name": "list_devices",
        "description": "List all devices on the Tailscale mesh network that can be controlled via Computer Use.",
        "parameters": {
            "type": "object",
            "properties": {
                "device_type": {
                    "type": "string",
                    "description": "Filter: 'android', 'linux', 'windows'"
                }
            },
            "required": []
        },
        "groups": _ALL,
    },
    {
        "name": "control_android_device",
        "description": "Control an Android device on the Tailscale mesh network using Gemini Computer Use. Can tap, type, swipe, open apps, and navigate the device UI autonomously. Returns a task ID - use get_task_status to check progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to do on the Android device (e.g., 'Open the Play Store and check StoryBox reviews')"
                },
                "device_id": {
                    "type": "string",
                    "description": "Device ID from the device registry (e.g., 'brandon-phone'). Use list_devices to see available devices."
                }
            },
            "required": ["prompt", "device_id"]
        },
        "groups": _ALL,
    },

    # ── Task Status Tool ───────────────────────────────────────────────────

    {
        "name": "get_task_status",
        "description": "Check the status of an async generation task (image, video, music). Returns status, progress, and result URL when complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID returned by generate_image/generate_video/generate_music"
                }
            },
            "required": ["task_id"]
        },
        "groups": _CHAT_PHONE_MCP,
    },

    # ── Multimodal Analysis Tools ──────────────────────────────────────────

    {
        "name": "analyze_image",
        "description": "Analyze an image using multimodal AI. Can describe, extract text, identify objects, answer questions about the image.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "URL or local path to the image (e.g. /ui/uploads/sunset.png or https://example.com/photo.jpg)"
                },
                "prompt": {
                    "type": "string",
                    "description": "Question or instruction about the image (default: 'Describe this image in detail')"
                }
            },
            "required": ["image_url"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "analyze_audio",
        "description": "Analyze audio content using Gemini. Can transcribe, describe sounds, identify music, etc. Returns task_id (async).",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the audio file to analyze"
                },
                "prompt": {
                    "type": "string",
                    "description": "What to analyze (e.g. 'Transcribe this audio', 'What sounds are in this?')"
                }
            },
            "required": ["file_path"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "analyze_video",
        "description": "Analyze a video using multimodal AI. Can describe content, identify actions, extract information.",
        "parameters": {
            "type": "object",
            "properties": {
                "video_url": {
                    "type": "string",
                    "description": "URL or local path to the video"
                },
                "prompt": {
                    "type": "string",
                    "description": "Question or instruction about the video (default: 'Describe what happens in this video')"
                }
            },
            "required": ["video_url"]
        },
        "groups": _CHAT_MCP,
    },

    # ── TTS / STT Tools ───────────────────────────────────────────────────

    {
        "name": "speech_to_text",
        "description": "Transcribe audio to text using OpenAI Whisper. Supports wav, mp3, m4a, ogg, flac, webm.",
        "parameters": {
            "type": "object",
            "properties": {
                "audio_path": {
                    "type": "string",
                    "description": "Path to audio file (local path)"
                }
            },
            "required": ["audio_path"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "text_to_speech",
        "description": "Convert text to speech using OpenAI TTS HD. Returns an audio URL. Voices: alloy, echo, fable, onyx, nova, shimmer.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to convert to speech"
                },
                "voice": {
                    "type": "string",
                    "description": "Voice: alloy, echo, fable, onyx, nova, shimmer (default: onyx)"
                },
                "model": {
                    "type": "string",
                    "description": "Model: tts-1 or tts-1-hd (default: tts-1-hd)"
                }
            },
            "required": ["text"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "list_tts_voices",
        "description": "List available text-to-speech voices from Google Cloud TTS. Returns a large list of voices with language and gender info.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "gemini_pro_tts",
        "description": "Generate high-quality speech using Gemini Pro TTS with 30 premium voices. Superior quality to OpenAI TTS. Supports emotional cues in text. Returns task_id (async).",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to convert to speech. Include emotional cues inline (e.g. 'said with determination', 'whispered nervously')."
                },
                "voice": {
                    "type": "string",
                    "description": "Voice name: Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede, and 22 more (default: Charon)"
                }
            },
            "required": ["text"]
        },
        "groups": _CHAT_MCP,
    },

    # ── Gmail Tools ─────────────────────────────────────────────────────────

    {
        "name": "gmail_search",
        "description": "Search or list emails in the operator's Gmail inbox. Uses Gmail search syntax (e.g., 'from:someone subject:hello', 'is:unread', 'newer_than:2d'). Leave query empty for recent emails.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query like 'from:someone subject:hello'. Leave empty for recent emails"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-20)",
                    "default": 10
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                }
            },
            "required": []
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "gmail_read",
        "description": "Read the full content of an email by its message ID (from gmail_search results). Returns subject, sender, body text, and labels.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID from gmail_search results"
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                }
            },
            "required": ["message_id"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "gmail_send",
        "description": "Compose and send a new email from the operator's Gmail account.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address"
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line"
                },
                "body": {
                    "type": "string",
                    "description": "Email body text"
                },
                "cc": {
                    "type": "string",
                    "description": "CC recipients (comma-separated email addresses)"
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                }
            },
            "required": ["to", "subject", "body"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "gmail_reply",
        "description": "Reply to an existing email thread. Automatically addresses the reply to the original sender and prefixes 'Re:' to the subject.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Original message ID to reply to"
                },
                "thread_id": {
                    "type": "string",
                    "description": "Thread ID from the original message"
                },
                "body": {
                    "type": "string",
                    "description": "Reply text"
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                }
            },
            "required": ["message_id", "thread_id", "body"]
        },
        "groups": _CHAT_MCP,
    },
    {
        "name": "gmail_labels",
        "description": "List all Gmail labels, or modify labels on a message (mark read/unread, archive, star/unstar).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to perform",
                    "enum": ["list", "mark_read", "mark_unread", "archive", "star", "unstar"]
                },
                "message_id": {
                    "type": "string",
                    "description": "Required for modify actions (mark_read, mark_unread, archive, star, unstar)"
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                }
            },
            "required": ["action"]
        },
        "groups": _CHAT_MCP,
    },

    # ── ToolVault Meta-Tool ──────────────────────────────────────────────
    # The tool that finds tools. Always Tier 1 (loaded into every context).
    # With TOOLVAULT_ENABLED, this is the model's gateway to all capabilities.

    {
        "name": "toolvault",
        "description": "Your tool discovery system. Use this to find and retrieve tools from the ToolVault. Actions: 'search' finds tools by what you need to do, 'read' gets the full spec for a specific tool, 'list' shows all available tools by category.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "read", "list"],
                    "description": "search=find tools by query, read=get full tool spec by name, list=show all tools by category"
                },
                "query": {
                    "type": "string",
                    "description": "For search: natural language description of what you need (e.g., 'send a text message', 'generate an image')"
                },
                "tool_name": {
                    "type": "string",
                    "description": "For read: exact tool name to retrieve (e.g., 'send_sms', 'generate_image')"
                },
                "category": {
                    "type": "string",
                    "description": "For list: filter by category (e.g., 'communication', 'media_generation', 'email'). Omit for all."
                },
            },
            "required": ["action"],
        },
        "groups": _ALL,
    },

    # ── MCP-Only Tools (BlackBox internals for Claude Code) ────────────────

    {
        "name": "seek_snapshot_direct",
        "description": "Directly seek and read a snapshot from the volume using byte offsets. Most efficient method for retrieving snapshot content.",
        "parameters": {
            "type": "object",
            "properties": {
                "snap_id": {
                    "type": "string",
                    "description": "Snapshot ID to seek"
                }
            },
            "required": ["snap_id"]
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "mint_snapshot",
        "description": "Create a new snapshot (memory) in the BlackBox via AI processing. Content is sent through /chat for synthesis, then auto-mint captures the result.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Content to save - will be processed by AI before minting"
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                },
                "snapshot_type": {
                    "type": "string",
                    "description": "Type hint (currently ignored - auto-mint determines type)",
                    "default": "normal"
                }
            },
            "required": ["content", "operator"]
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "get_context",
        "description": "Get enriched context for a query, including relevant snapshots and recent history. Use before answering questions that may benefit from historical context.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query or topic to get context for"
                },
                "operator": {
                    "type": "string",
                    "description": "Operator/user name"
                }
            },
            "required": ["query", "operator"]
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "list_operators",
        "description": "List all operators (users) that have snapshots in the BlackBox.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "get_current_operator",
        "description": "Resolve the BlackBox's current operator. Returns {resolved, operators, default, count, needs_selection}. When needs_selection is true (multiple operators, none specified), the agent should ask the user to pick one (default pre-selected). Use this instead of hard-coding any operator name.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "get_index_stats",
        "description": "Get statistics about the snapshot index - total count, operators, byte ranges, etc.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "browse_index",
        "description": "Browse the snapshot index to see available snapshots. Efficient - reads index only.",
        "parameters": {
            "type": "object",
            "properties": {
                "operator": {
                    "type": "string",
                    "description": "Optional. Omit to include ALL operators on this box; pass a name to scope results to one operator."
                },
                "snap_type": {
                    "type": "string",
                    "description": "Filter by type: 'normal', 'checkpoint', 'summary' (optional)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default: 20)",
                    "default": 20
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip first N results for pagination",
                    "default": 0
                }
            },
            "required": []
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "chat_with_context",
        "description": "Send a message through BlackBox chat with full context retrieval. Returns AI response enriched with relevant snapshots.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to send"
                },
                "operator": {
                    "type": "string",
                    "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
                },
                "provider": {
                    "type": "string",
                    "description": "AI provider: 'anthropic', 'openai', or 'gemini'",
                    "default": "anthropic"
                },
                "model": {
                    "type": "string",
                    "description": "Model name (optional - uses default for provider)"
                }
            },
            "required": ["message", "operator"]
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "refresh_index",
        "description": "Force refresh the snapshot index cache. Use after minting new snapshots.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "groups": _MCP_ONLY,
    },
    {
        "name": "get_music_status",
        "description": "Check the status of a music generation task. Alias for get_task_status with music-specific context.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID returned by generate_music"
                }
            },
            "required": ["task_id"]
        },
        "groups": _MCP_ONLY,
    },

    # ── UGV Beast (Waveshare tracked robot over Tailscale) ──────────────────
    # All 22 tools proxy through Orchestrator → http://ugv-beast:8080 via HTTP.
    # Semantic injection surfaces these only when the user's prompt is robot-related
    # (drive/look/snapshot/nav/etc.). Rich descriptions carry the persona — there
    # is no separate UGV system prompt.

    {"name": "ugv_motion_move_forward", "description": "Drive the UGV Beast — a physical Waveshare tracked robot you can control — straight forward for a duration. The robot has a JBL speaker for your voice and cameras for your sight. Safety-clamped server-side: max 0.15 m/s, max 10 seconds per call. Always check LiDAR with ugv_status_get_lidar_summary before moving if you haven't seen a recent reading. If any front sector is under 0.3 m, do NOT move forward.", "parameters": {"type": "object", "properties": {"duration_s": {"type": "number", "description": "Seconds to drive. Clamped at 10.", "minimum": 0.1, "maximum": 10.0}, "speed_m_s": {"type": "number", "description": "Linear speed in m/s (max 0.15).", "minimum": 0.02, "maximum": 0.15, "default": 0.1}}, "required": ["duration_s"]}, "groups": _ALL},

    {"name": "ugv_motion_move_backward", "description": "Drive the UGV Beast tracked robot straight backward for a duration. Safety-clamped: max 0.15 m/s, max 10 s. Blind reverse is riskier than forward — prefer short (<2s) pulses and confirm back-sector LiDAR with ugv_status_get_lidar_summary first.", "parameters": {"type": "object", "properties": {"duration_s": {"type": "number", "minimum": 0.1, "maximum": 10.0, "description": "Seconds to drive."}, "speed_m_s": {"type": "number", "minimum": 0.02, "maximum": 0.15, "default": 0.08, "description": "Linear speed in m/s (max 0.15)."}}, "required": ["duration_s"]}, "groups": _ALL},

    {"name": "ugv_motion_rotate_left", "description": "Rotate the UGV Beast tracked robot in place counter-clockwise (positive angular velocity). Useful for scanning the environment or aligning heading. Safety-clamped: max 0.8 rad/s, max 5 seconds. Check LiDAR with ugv_status_get_lidar_summary if the robot is close to obstacles — rotation sweeps the gimbal arm through nearby space.", "parameters": {"type": "object", "properties": {"duration_s": {"type": "number", "minimum": 0.1, "maximum": 5.0, "description": "Seconds to rotate."}, "rate_rad_s": {"type": "number", "minimum": 0.1, "maximum": 0.8, "default": 0.5, "description": "Angular rate in rad/s (max 0.8)."}}, "required": ["duration_s"]}, "groups": _ALL},

    {"name": "ugv_motion_rotate_right", "description": "Rotate the UGV Beast tracked robot in place clockwise (negative angular velocity). Useful for scanning the environment or aligning heading. Safety-clamped: max 0.8 rad/s, max 5 seconds. Check LiDAR with ugv_status_get_lidar_summary if the robot is close to obstacles.", "parameters": {"type": "object", "properties": {"duration_s": {"type": "number", "minimum": 0.1, "maximum": 5.0, "description": "Seconds to rotate."}, "rate_rad_s": {"type": "number", "minimum": 0.1, "maximum": 0.8, "default": 0.5, "description": "Angular rate in rad/s (max 0.8)."}}, "required": ["duration_s"]}, "groups": _ALL},

    {"name": "ugv_motion_stop", "description": "Immediately stop all UGV Beast motion by publishing a zero-velocity twist. Safe to call at any time; idempotent. Use this any time you want the robot to halt — don't wait for duration timers.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_gimbal_look_at", "description": "Point the UGV Beast's pan-tilt gimbal camera to absolute pan and tilt angles in degrees. The gimbal sits on top of the robot and carries the primary camera. Pan: -180 to +180 (negative=right, positive=left, zero=forward). Tilt: -45 to +90 (negative=down, positive=up). Servos are open-loop — commanded position is effectively actual after ~500ms settle.", "parameters": {"type": "object", "properties": {"pan_deg": {"type": "number", "minimum": -180, "maximum": 180, "description": "Pan angle in degrees. Negative=right, positive=left, zero=forward."}, "tilt_deg": {"type": "number", "minimum": -45, "maximum": 90, "description": "Tilt angle in degrees. Negative=down, positive=up."}, "speed": {"type": "integer", "minimum": 1, "maximum": 300, "default": 100, "description": "Servo speed 1-300. 100 is a gentle default."}}, "required": ["pan_deg", "tilt_deg"]}, "groups": _ALL},

    {"name": "ugv_gimbal_reset", "description": "Return the UGV Beast's pan-tilt gimbal to the forward-center home position (pan=0, tilt=0). Call this to \"look straight ahead again\" after scanning.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_gimbal_get_state", "description": "Get the current commanded pan and tilt angles of the UGV Beast's gimbal. Returns the last-commanded position (servos are open-loop PWM — no actual encoder feedback available on this hardware).", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_camera_list", "description": "List all cameras available on the UGV Beast — a pan-tilt mounted USB camera (\"pantilt\") and an OAK-D Lite depth camera on top (\"oakd\") — and report whether each is currently streaming fresh frames.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_camera_snapshot", "description": "Capture the latest JPEG frame from one of the UGV Beast's cameras so you can \"see\" what the robot sees. Set as_url=true for a fast URL response, or as_url=false to receive the image as base64 (useful when you need to analyze the image yourself via vision).", "parameters": {"type": "object", "properties": {"camera": {"type": "string", "enum": ["pantilt", "oakd"], "description": "Which camera: 'pantilt' for the pan-tilt camera, 'oakd' for the OAK-D Lite."}, "as_url": {"type": "boolean", "default": False, "description": "If true, return a URL like /snapshot/pantilt. If false, return base64 image bytes."}}, "required": ["camera"]}, "groups": _ALL},

    {"name": "ugv_status_get_pose", "description": "Get the UGV Beast's current (x, y, yaw) pose in the map frame. Yaw is in both radians and degrees. Use this to know where the robot is before planning navigation or describing location.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_status_get_odom", "description": "Get the UGV Beast's filtered odometry: position, heading, and current linear/angular velocity. Velocities are the actual observed values (non-zero while moving).", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_status_get_lidar_summary", "description": "Summarize the UGV Beast's 360° LiDAR scan as 8 directional sectors (front, front_left, left, back_left, back, back_right, right, front_right) with the minimum distance in meters per sector plus an overall minimum. Call this before any motion to check for obstacles — robot radius is ~0.2 m, so distances under 0.3 m are danger-close.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_status_list_nodes", "description": "List all running ROS2 nodes on the UGV Beast. Useful for diagnostics — healthy robot should have ~30+ nodes including bt_navigator, controller_server, planner_server, slam_toolbox.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_status_list_topics", "description": "List all active ROS2 topics on the UGV Beast with their message types. Useful for diagnostics when a subsystem seems offline.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_status_health", "description": "Overall UGV Beast health report: whether the ROS bridge is running plus freshness (in seconds since last message) for all key topics — /odom, /scan, /robot_pose, /map, camera streams, gimbal state. A healthy robot shows fresh under 0.5 s for all topics except /map (which updates sparingly).", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_nav_goto_point", "description": "Navigate the UGV Beast to an (x, y, yaw) pose in the map frame using Nav2 autonomous path planning. This is the preferred long-distance movement — Nav2 handles obstacle avoidance and path finding. Non-blocking: returns immediately after goal is accepted; poll ugv_nav_status for progress. Cancel with ugv_nav_cancel.", "parameters": {"type": "object", "properties": {"x": {"type": "number", "description": "Goal x coordinate in the map frame (meters)."}, "y": {"type": "number", "description": "Goal y coordinate in the map frame (meters)."}, "yaw_deg": {"type": "number", "minimum": -180, "maximum": 180, "default": 0.0, "description": "Desired goal heading in degrees (-180 to 180)."}}, "required": ["x", "y"]}, "groups": _ALL},

    {"name": "ugv_nav_cancel", "description": "Cancel the UGV Beast's currently active Nav2 navigation goal. Idempotent — returns canceled:false if there was no active goal.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_nav_status", "description": "Get the UGV Beast's current Nav2 goal status (idle, navigating, succeeded, aborted, canceled) and the distance remaining in meters to the goal.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_system_emergency_stop", "description": "Emergency stop the UGV Beast: publishes a zero-velocity twist AND sends an ESP32 all-motor cutoff command. Reserve for actual emergencies — for normal halts use ugv_motion_stop instead. This is the panic button to hit when you suspect runaway motion or an imminent collision.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_system_servo_center", "description": "Center the UGV Beast's servos to mid-point. Primarily a calibration operation — returns the gimbal to pan=0, tilt=0.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_system_servo_release", "description": "Release UGV Beast servo torque (limp mode). After this, the gimbal will flop under gravity — it won't hold its position. Only use when you specifically want free manipulation or storage.", "parameters": {"type": "object", "properties": {}, "required": []}, "groups": _ALL},

    {"name": "ugv_start_mission", "description": "Hand off a multi-step mission to the UGV Beast's on-device Gemini Robotics-ER 1.6 agent. Use this for goals that require perception + multi-step robot reasoning (patrol, find-and-approach, describe-surroundings, go-to-X). The robot autonomously loops observe → reason → act → speak, terminating when the model calls mission_done or mission_fail. Returns a mission_id — poll with ugv_mission_status. Prefer this over chaining individual motion/gimbal/nav tools for anything that isn't a single primitive action. Plain-language missions only: 'go check if the kitchen lights are on', 'follow me', 'find the red backpack'.", "parameters": {"type": "object", "properties": {"mission": {"type": "string", "description": "Plain-language mission instruction, as you would speak it to a robot. No code, no JSON, no tool names — just the goal."}, "operator": {"type": "string", "description": "Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."}}, "required": ["mission"]}, "groups": _ALL},

    {"name": "ugv_mission_status", "description": "Poll the status of a UGV Beast ER mission started by ugv_start_mission. Returns status (active/completed/failed/aborted), step_count, last_assistant_text (what the robot last said), end_reason when terminal, and a ring of recent events. Use this to follow along as the mission runs — the robot narrates via its own speaker, but this lets you see and summarize progress back to the user.", "parameters": {"type": "object", "properties": {"mission_id": {"type": "string", "description": "The mission_id returned by ugv_start_mission."}}, "required": ["mission_id"]}, "groups": _ALL},

    {"name": "ugv_mission_abort", "description": "Abort an in-progress UGV Beast ER mission. The robot will stop on the next loop iteration and the mission status becomes 'aborted'. Use this if the operator says to stop, or if you detect the mission has gone off-rails.", "parameters": {"type": "object", "properties": {"mission_id": {"type": "string", "description": "The mission_id returned by ugv_start_mission."}}, "required": ["mission_id"]}, "groups": _ALL},
]


# =============================================================================
# Indexes (built at module load)
# =============================================================================

# Name → tool definition (fast lookup)
_TOOL_INDEX: Dict[str, Dict] = {t["name"]: t for t in TOOL_DEFINITIONS}

# Alias → canonical name (backward compatibility)
# Maps old/variant tool names to the canonical name in the registry.
_ALIASES: Dict[str, str] = {
    "search_memory": "search_snapshots",
    "get_recent_snapshots": "list_recent_snapshots",
}

# Canonical name → executor method name (for BlackBoxToolExecutor dispatch)
# Only needed when the canonical name differs from the executor method.
_EXECUTOR_NAMES: Dict[str, str] = {
    "search_snapshots": "search_memory",
}


def get_tools_by_group(group: str) -> List[Dict]:
    """Return canonical tool definitions belonging to a group."""
    return [t for t in TOOL_DEFINITIONS if group in t.get("groups", [])]


def get_tool_by_name(name: str) -> Optional[Dict]:
    """Look up a tool by name or alias."""
    if name in _TOOL_INDEX:
        return _TOOL_INDEX[name]
    canonical = _ALIASES.get(name)
    return _TOOL_INDEX.get(canonical) if canonical else None


def resolve_alias(name: str) -> str:
    """Resolve a tool alias to its canonical name. Returns input if not an alias."""
    return _ALIASES.get(name, name)


def resolve_executor_name(name: str) -> str:
    """Resolve a canonical tool name to its executor method name.

    Most tools use the same name. Only a few need remapping
    (e.g., search_snapshots → search_memory for the executor).
    """
    canonical = resolve_alias(name)
    return _EXECUTOR_NAMES.get(canonical, canonical)


# =============================================================================
# Format Converters
# =============================================================================

def _clean_params(params: Dict) -> Dict:
    """Deep copy parameters, stripping registry-only fields."""
    return copy.deepcopy(params)


def _strip_for_gemini(params: Dict) -> Dict:
    """Clean params for Gemini compatibility.

    Gemini restrictions:
    - No 'default' keys in properties
    - Enum values must be strings
    - Enum is only allowed on STRING type properties
    - No 'minimum'/'maximum' constraints
    """
    params = copy.deepcopy(params)
    for prop in params.get("properties", {}).values():
        prop.pop("default", None)
        prop.pop("minimum", None)
        prop.pop("maximum", None)
        prop.pop("maxItems", None)
        # Gemini only allows enum on STRING type — convert if needed
        if "enum" in prop:
            prop["enum"] = [str(v) for v in prop["enum"]]
            prop["type"] = "string"
    return params


def to_anthropic(tool: Dict) -> Dict:
    """Canonical → Anthropic format (input_schema wrapper)."""
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": _clean_params(tool["parameters"]),
    }


def to_openai_rest(tool: Dict) -> Dict:
    """Canonical → OpenAI Chat Completions format (nested function wrapper)."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": _clean_params(tool["parameters"]),
        }
    }


def to_openai_realtime(tool: Dict) -> Dict:
    """Canonical → OpenAI Realtime / Grok Live format (flat, type=function)."""
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": _clean_params(tool["parameters"]),
    }


def to_gemini_rest(tools: List[Dict]) -> List[Dict]:
    """Canonical list → Gemini REST format: [{"function_declarations": [...]}]."""
    return [{
        "function_declarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": _strip_for_gemini(t["parameters"]),
            }
            for t in tools
        ]
    }]


def to_gemini_live(tools: List[Dict]) -> List[Dict]:
    """Canonical list → Gemini Live format: [{"functionDeclarations": [...]}]."""
    return [{
        "functionDeclarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": _strip_for_gemini(t["parameters"]),
            }
            for t in tools
        ]
    }]


def to_mcp(tool: Dict):
    """Canonical → MCP Tool() object. Lazy import to avoid MCP dependency."""
    from mcp.types import Tool
    return Tool(
        name=tool["name"],
        description=tool["description"],
        inputSchema=_clean_params(tool["parameters"]),
    )


# =============================================================================
# Convenience Getters (what consumers import)
# =============================================================================

def get_anthropic_tools(group: str = "chat") -> List[Dict]:
    """Get tools in Anthropic format for a group."""
    return [to_anthropic(t) for t in get_tools_by_group(group)]


def get_openai_rest_tools(group: str = "chat") -> List[Dict]:
    """Get tools in OpenAI Chat Completions format for a group."""
    return [to_openai_rest(t) for t in get_tools_by_group(group)]


def get_openai_realtime_tools(group: str = "realtime") -> List[Dict]:
    """Get tools in OpenAI Realtime (flat) format for a group."""
    return [to_openai_realtime(t) for t in get_tools_by_group(group)]


def get_gemini_rest_tools(group: str = "chat") -> List[Dict]:
    """Get tools in Gemini REST format for a group."""
    return to_gemini_rest(get_tools_by_group(group))


def get_gemini_live_tools(group: str = "gemini_live") -> List[Dict]:
    """Get tools in Gemini Live format for a group."""
    return to_gemini_live(get_tools_by_group(group))


def get_mcp_tools() -> list:
    """Get all MCP-group tools as MCP Tool objects."""
    return [to_mcp(t) for t in get_tools_by_group("mcp")]


# =============================================================================
# Utility
# =============================================================================

def get_all_tool_names() -> List[str]:
    """Return all canonical tool names."""
    return [t["name"] for t in TOOL_DEFINITIONS]


def get_group_tool_names(group: str) -> List[str]:
    """Return tool names for a specific group."""
    return [t["name"] for t in get_tools_by_group(group)]
