#!/usr/bin/env python3
"""
blackbox_tools.py - BlackBox Tool Executor + Legacy Schema Exports

Tool DEFINITIONS now live in tool_registry.py (single source of truth).
This file provides:
  - BlackBoxToolExecutor class (executes tools)
  - Legacy exports (BLACKBOX_TOOLS_ANTHROPIC/OPENAI/GEMINI) for backward compat
  - get_tools_for_backend() helper
  - execute_tool() convenience function
"""

import asyncio
import base64
import os
import aiohttp
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from Orchestrator.contacts import search_contacts as _search_contacts, upsert_contact

# Import from the unified registry (single source of truth)
from Orchestrator.tools.tool_registry import (
    get_anthropic_tools,
    get_openai_realtime_tools,
    get_gemini_live_tools,
    resolve_executor_name,
)

# =============================================================================
# Tool Definitions — Generated from tool_registry.py
# =============================================================================
# These are the "phone" group tools (used by phone bridge and live voice routes).
# chat_routes.py and other consumers import directly from tool_registry.

BLACKBOX_TOOLS_ANTHROPIC = get_anthropic_tools("phone")
BLACKBOX_TOOLS_OPENAI = get_openai_realtime_tools("phone")
BLACKBOX_TOOLS_GEMINI = get_gemini_live_tools("phone")

# =============================================================================
# Tool Executor
# =============================================================================

# ToolResult is defined canonically in toolvault.context and re-exported here so
# the toolvault package has no import-time dependency on this module (breaks the
# cycle now that tool_registry sources its definitions from the toolvault
# registry). Same class object — `blackbox_tools.ToolResult is context.ToolResult`.
from Orchestrator.toolvault.context import ToolResult  # noqa: E402


class BlackBoxToolExecutor:
    """
    Executes BlackBox tools with unified interface for all AI backends.

    Usage:
        executor = BlackBoxToolExecutor(operator="Brandon")
        result = await executor.execute("send_sms", {"phone_number": "+1555...", "message": "Hello"})
    """

    def __init__(self, operator: str = "system", base_url: str = "http://localhost:9091"):
        self.operator = operator
        self.base_url = base_url

    # ── UGV Beast HTTP proxy ──────────────────────────────────────────────────
    # UGV_BASE_URL is env-overridable so a developer on the LAN can point to
    # http://192.168.1.155:8080 when Tailscale MagicDNS is unavailable.
    UGV_BASE_URL = os.environ.get("UGV_BASE_URL", "http://ugv-beast:8080")
    UGV_ER_BASE_URL = os.environ.get("UGV_ER_URL", "http://ugv-beast:8082")

    async def _ugv_call(self, api_tool_name: str, args: Dict[str, Any]) -> ToolResult:
        """Proxy a call to the UGV Beast tool schema API over Tailscale.

        Inlines the response payload into ``.result`` because chat_routes.py
        consumers read only ``.result`` (not ``.rich_result()``); without this,
        the model sees "ok" instead of pose/sensor/camera data.
        """
        import json as _json
        url = f"{self.UGV_BASE_URL}/tool/{api_tool_name}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=args, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        txt = await r.text()
                        return ToolResult(success=False, result=f"UGV API {r.status}: {txt[:1000]}")
                    data = await r.json()
                    # Guard against HTTP 200 with server-side error payload.
                    if isinstance(data, dict) and ("error" in data or data.get("status") == "error"):
                        return ToolResult(success=False, result=f"UGV {api_tool_name} error: {str(data)[:1000]}")
                    payload = data.get("result", data)
                    payload_json = _json.dumps(payload, default=str)
                    return ToolResult(
                        success=True,
                        result=f"UGV {api_tool_name} returned: {payload_json}",
                        data=payload,
                    )
        except asyncio.TimeoutError:
            return ToolResult(success=False, result=f"UGV API timeout calling {api_tool_name}")
        except Exception as e:
            return ToolResult(success=False, result=f"UGV API error: {e}")

    # ── UGV Beast proxies (22 tools → http://ugv-beast:8080) ─────────────────

    async def _execute_ugv_motion_move_forward(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("motion_move_forward", {
            "duration_s": params.get("duration_s"),
            "speed_m_s": params.get("speed_m_s", 0.1),
        })

    async def _execute_ugv_motion_move_backward(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("motion_move_backward", {
            "duration_s": params.get("duration_s"),
            "speed_m_s": params.get("speed_m_s", 0.08),
        })

    async def _execute_ugv_motion_rotate_left(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("motion_rotate_left", {
            "duration_s": params.get("duration_s"),
            "rate_rad_s": params.get("rate_rad_s", 0.5),
        })

    async def _execute_ugv_motion_rotate_right(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("motion_rotate_right", {
            "duration_s": params.get("duration_s"),
            "rate_rad_s": params.get("rate_rad_s", 0.5),
        })

    async def _execute_ugv_motion_stop(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("motion_stop", {})

    async def _execute_ugv_gimbal_look_at(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("gimbal_look_at", {
            "pan_deg": params.get("pan_deg"),
            "tilt_deg": params.get("tilt_deg"),
            "speed": params.get("speed", 100),
        })

    async def _execute_ugv_gimbal_reset(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("gimbal_reset", {})

    async def _execute_ugv_gimbal_get_state(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("gimbal_get_state", {})

    async def _execute_ugv_camera_list(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("camera_list", {})

    async def _execute_ugv_camera_snapshot(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("camera_snapshot", {
            "camera": params.get("camera"),
            "as_url": params.get("as_url", False),
        })

    async def _execute_ugv_status_get_pose(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("status_get_pose", {})

    async def _execute_ugv_status_get_odom(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("status_get_odom", {})

    async def _execute_ugv_status_get_lidar_summary(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("status_get_lidar_summary", {})

    async def _execute_ugv_status_list_nodes(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("status_list_nodes", {})

    async def _execute_ugv_status_list_topics(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("status_list_topics", {})

    async def _execute_ugv_status_health(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("status_health", {})

    async def _execute_ugv_nav_goto_point(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("nav_goto_point", {
            "x": params.get("x"),
            "y": params.get("y"),
            "yaw_deg": params.get("yaw_deg", 0.0),
        })

    async def _execute_ugv_nav_cancel(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("nav_cancel", {})

    async def _execute_ugv_nav_status(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("nav_status", {})

    async def _execute_ugv_system_emergency_stop(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("system_emergency_stop", {})

    async def _execute_ugv_system_servo_center(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("system_servo_center", {})

    async def _execute_ugv_system_servo_release(self, params: Dict[str, Any]) -> ToolResult:
        return await self._ugv_call("system_servo_release", {})

    # ── UGV Beast on-device ER agent (port 8082) ─────────────────────────────

    async def _ugv_er_call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> ToolResult:
        import json as _json
        url = f"{self.UGV_ER_BASE_URL}{path}"
        try:
            async with aiohttp.ClientSession() as s:
                if method == "GET":
                    req = s.get(url, timeout=aiohttp.ClientTimeout(total=10))
                else:
                    req = s.request(method, url, json=body or {}, timeout=aiohttp.ClientTimeout(total=10))
                async with req as r:
                    data = await r.json() if r.content_type == "application/json" else {"text": await r.text()}
                    if r.status >= 400:
                        return ToolResult(success=False, result=f"UGV ER {path} HTTP {r.status}: {str(data)[:1000]}")
                    return ToolResult(success=True, result=f"UGV ER {path}: {_json.dumps(data, default=str)[:1500]}", data=data)
        except asyncio.TimeoutError:
            return ToolResult(success=False, result=f"UGV ER timeout on {path}")
        except Exception as e:
            return ToolResult(success=False, result=f"UGV ER error: {e}")

    async def _execute_ugv_start_mission(self, params: Dict[str, Any]) -> ToolResult:
        mission = (params.get("mission") or "").strip()
        if not mission:
            return ToolResult(success=False, result="ugv_start_mission: 'mission' is required")
        return await self._ugv_er_call("POST", "/mission", {
            "operator": params.get("operator", "Brandon"),
            "mission": mission,
        })

    async def _execute_ugv_mission_status(self, params: Dict[str, Any]) -> ToolResult:
        mid = (params.get("mission_id") or "").strip()
        if not mid:
            return ToolResult(success=False, result="ugv_mission_status: 'mission_id' is required")
        return await self._ugv_er_call("GET", f"/mission/{mid}")

    async def _execute_ugv_mission_abort(self, params: Dict[str, Any]) -> ToolResult:
        mid = (params.get("mission_id") or "").strip()
        if not mid:
            return ToolResult(success=False, result="ugv_mission_abort: 'mission_id' is required")
        return await self._ugv_er_call("POST", f"/mission/{mid}/abort")

    async def execute(self, tool_name: str, tool_input: Dict[str, Any]) -> ToolResult:
        """Execute a tool and return the result.

        MODULE-FIRST dispatch: ask the ToolVault registry for a per-tool
        ``executor.py`` (it resolves alias → canonical → ``executor.py``). If one
        exists, run it with a :class:`ToolContext`. Otherwise fall back to the
        legacy ``_execute_<name>`` method on this class.

        Until Task 6.2 migrates every executor into a module, NO ``executor.py``
        files ship, so ``get_executor`` always returns None and every call falls
        through to legacy — behavior is unchanged today; this just builds the rail.
        """
        from Orchestrator.toolvault import registry
        from Orchestrator.toolvault.context import ToolContext

        # Module-first: a per-tool executor.py wins (handles alias → canonical).
        ex = registry.get_executor(tool_name)
        if ex is not None:
            try:
                return await ex(
                    tool_input,
                    ToolContext(operator=self.operator, base_url=self.base_url),
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                return ToolResult(
                    success=False,
                    result=f"Error executing {tool_name}: {str(e)}"
                )

        # Legacy fallback (until 6.2 migrates all executors).
        # Resolve aliases (e.g., search_snapshots → search_memory for executor method)
        legacy = resolve_executor_name(tool_name)

        handler = getattr(self, f"_execute_{legacy}", None)
        if handler is None:
            return ToolResult(
                success=False,
                result=f"Unknown tool: {legacy}"
            )

        try:
            return await handler(tool_input)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ToolResult(
                success=False,
                result=f"Error executing {legacy}: {str(e)}"
            )

    async def _execute_send_sms(self, params: Dict[str, Any]) -> ToolResult:
        """Send an SMS message via cellular modem or Twilio."""
        phone_number = params.get("phone_number", "")
        message = params.get("message", "")

        if not phone_number or not message:
            return ToolResult(False, "Phone number and message are required")

        # Normalize phone number
        if not phone_number.startswith("+"):
            if phone_number.startswith("1") and len(phone_number) == 11:
                phone_number = f"+{phone_number}"
            elif len(phone_number) == 10:
                phone_number = f"+1{phone_number}"

        # Truncate message if too long
        if len(message) > 1600:
            message = message[:1597] + "..."

        # Route based on TELEPHONY_PROVIDER config
        from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED

        # Asterisk/TG200 path (preferred when enabled)
        if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
            try:
                from Orchestrator.sms import get_ami_client, get_message_store
                ami = get_ami_client()
                if not ami or not ami.connected:
                    return ToolResult(False, "SMS system not connected (AMI client down)")

                result = await ami.send_sms(phone_number, message, span=2)
                if result.get("success"):
                    # Store outbound message if we have the store
                    store = get_message_store()
                    if store and self.operator:
                        from datetime import datetime, timezone
                        store.store_message(
                            operator=self.operator,
                            direction="outbound",
                            phone_number=phone_number,
                            contact_name="",
                            body=message,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                    return ToolResult(True, f"SMS sent to {phone_number} via TG200.", data={"to": phone_number, "provider": "asterisk"})
                return ToolResult(False, f"TG200 SMS failed: {result.get('error', 'Unknown error')}")
            except Exception as e:
                return ToolResult(False, f"TG200 SMS error: {str(e)}")

        # Twilio path (fallback when Asterisk not available)
        try:
            from Orchestrator.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER

            if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
                return ToolResult(False, "Twilio not configured")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
                    auth=aiohttp.BasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                    data={
                        "To": phone_number,
                        "From": TWILIO_PHONE_NUMBER,
                        "Body": message
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = await resp.json()

                    if resp.status in (200, 201):
                        sid = result.get("sid", "")
                        return ToolResult(
                            success=True,
                            result=f"SMS sent to {phone_number}. Message SID: {sid}",
                            data={"sid": sid, "to": phone_number}
                        )
                    else:
                        error = result.get("message", str(result))
                        return ToolResult(False, f"Failed to send SMS: {error}")

        except Exception as e:
            return ToolResult(False, f"SMS error: {str(e)}")

    async def _execute_make_voice_call(self, params: Dict[str, Any]) -> ToolResult:
        """
        Make a voice call with a pre-generated TTS message.

        Flow:
        1. Generate TTS audio first (no delay on call connect)
        2. Save audio to a file
        3. Initiate call with audio injection
        """
        phone_number = params.get("phone_number", "")
        message = params.get("message", "")
        voice = params.get("voice", "onyx")

        if not phone_number or not message:
            return ToolResult(False, "Phone number and message are required")

        # Normalize phone number
        if not phone_number.startswith("+"):
            if phone_number.startswith("1") and len(phone_number) == 11:
                phone_number = f"+{phone_number}"
            elif len(phone_number) == 10:
                phone_number = f"+1{phone_number}"

        try:
            from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL

            # Step 1: Generate TTS audio FIRST
            print(f"[TOOL] Generating TTS for voice call: {message[:50]}...")

            async with aiohttp.ClientSession() as session:
                # Generate TTS
                async with session.post(
                    OPENAI_TTS_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "tts-1-hd",
                        "voice": voice,
                        "input": message,
                        "response_format": "pcm"  # Raw PCM for phone
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status != 200:
                        return ToolResult(False, f"TTS generation failed: {resp.status}")

                    pcm_audio = await resp.read()
                    print(f"[TOOL] TTS generated: {len(pcm_audio)} bytes")

                # Step 2: Initiate call with pre-generated greeting
                # The greeting will be converted to ULAW and played immediately
                # Route based on TELEPHONY_PROVIDER
                from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED
                if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
                    call_endpoint = f"{self.base_url}/asterisk/call"
                elif TELEPHONY_PROVIDER in ("cellular", "auto") and CELLULAR_ENABLED:
                    call_endpoint = f"{self.base_url}/cellular/call"
                else:
                    call_endpoint = f"{self.base_url}/twilio/call"
                async with session.post(
                    call_endpoint,
                    json={
                        "to": phone_number,
                        "backend": "openai_realtime",
                        "operator": self.operator,
                        "greeting": message  # Pass the message as greeting
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = await resp.json()

                    if result.get("status") == "initiated":
                        call_sid = result.get("call_sid", "")
                        return ToolResult(
                            success=True,
                            result=f"Voice call initiated to {phone_number}. The message will be delivered when they answer.",
                            data={"call_sid": call_sid, "to": phone_number}
                        )
                    else:
                        error = result.get("error", "Unknown error")
                        return ToolResult(False, f"Failed to initiate call: {error}")

        except Exception as e:
            return ToolResult(False, f"Voice call error: {str(e)}")

    async def _execute_search_memory(self, params: Dict[str, Any]) -> ToolResult:
        """Search BlackBox snapshots for relevant information using hybrid retrieval."""
        query = params.get("query", "")
        limit = params.get("limit", params.get("k", 5))

        if not query:
            return ToolResult(False, "Search query is required")

        try:
            from Orchestrator.fossils import hybrid_retrieve
            from Orchestrator.volume import read_text_safe
            from Orchestrator.config import VOL_PATH

            vol_txt = read_text_safe(VOL_PATH)

            # Use hybrid retrieval (keyword + semantic)
            results = hybrid_retrieve(vol_txt, query, k=limit, operator=self.operator)

            if not results:
                return ToolResult(
                    success=True,
                    result=f"No memories found matching: {query}"
                )

            # Format results
            output_parts = [f"Found {len(results)} relevant memory(ies) for: {query}\n"]
            for i, snap_text in enumerate(results, 1):
                # Truncate each result
                if len(snap_text) > 10000:
                    snap_text = snap_text[:10000] + "\n... [truncated]"
                output_parts.append(f"--- Result {i} ---\n{snap_text}")

            return ToolResult(
                success=True,
                result="\n\n".join(output_parts),
                data={"count": len(results)}
            )

        except Exception as e:
            return ToolResult(False, f"Search error: {str(e)}")

    async def _execute_get_snapshot(self, params: Dict[str, Any]) -> ToolResult:
        """Retrieve a specific snapshot by ID."""
        snap_id = params.get("snap_id", "")

        if not snap_id:
            return ToolResult(False, "Snapshot ID is required")

        try:
            from Orchestrator.fossils import get_snapshot_by_id

            result = get_snapshot_by_id(snap_id)

            if not result:
                return ToolResult(False, f"Snapshot not found: {snap_id}")

            content = result.get("content", "")
            metadata = result.get("metadata", {})

            # Format the response
            output = f"Snapshot {snap_id}:\n"
            output += f"Operator: {metadata.get('operator', 'unknown')}\n"
            output += f"Timestamp: {metadata.get('timestamp', 'unknown')}\n"
            output += f"Type: {metadata.get('type', 'normal')}\n"
            output += f"\n--- Content ---\n{content}"

            return ToolResult(
                success=True,
                result=output,
                data=result
            )

        except Exception as e:
            return ToolResult(False, f"Get snapshot error: {str(e)}")

    async def _execute_list_recent_snapshots(self, params: Dict[str, Any]) -> ToolResult:
        """Get the most recent snapshots for quick context catch-up."""
        count = min(params.get("count", 5), 10)  # Cap at 10

        try:
            from Orchestrator.fossils import get_recent_fossils_for_operator
            from Orchestrator.volume import read_text_safe
            from Orchestrator.config import VOL_PATH

            vol_txt = read_text_safe(VOL_PATH)

            # Get recent snapshots (cap each at 10000 chars for readability)
            snapshots = get_recent_fossils_for_operator(vol_txt, self.operator, count, cap_chars_each=10000)

            if not snapshots:
                return ToolResult(
                    success=True,
                    result=f"No recent snapshots found for operator: {self.operator}"
                )

            # Format output
            output = f"Recent {len(snapshots)} snapshot(s) for {self.operator}:\n\n"
            for i, snap in enumerate(snapshots, 1):
                output += f"--- Snapshot {i} ---\n{snap}\n\n"

            return ToolResult(
                success=True,
                result=output,
                data={"count": len(snapshots)}
            )

        except Exception as e:
            return ToolResult(False, f"List recent snapshots error: {str(e)}")

    async def _execute_get_current_time(self, params: Dict[str, Any]) -> ToolResult:
        """Get the current date and time."""
        from datetime import datetime
        now = datetime.now()

        return ToolResult(
            success=True,
            result=f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
            data={"iso": now.isoformat(), "unix": now.timestamp()}
        )

    async def _execute_generate_image(self, params: Dict[str, Any]) -> ToolResult:
        """Generate an image using the BlackBox image generation endpoint (Gemini 3 Pro Image)."""
        prompt = params.get("prompt", "")

        if not prompt:
            return ToolResult(False, "Image prompt is required")

        try:
            # Build payload with all supported parameters
            payload = {
                "prompt": prompt,
                "operator": self.operator
            }

            # Add optional parameters if provided
            if params.get("reference_images"):
                payload["reference_images"] = params["reference_images"]
            if params.get("aspectRatio"):
                payload["aspectRatio"] = params["aspectRatio"]
            if params.get("resolution"):
                payload["resolution"] = params["resolution"]
            if params.get("numberOfImages"):
                payload["numberOfImages"] = params["numberOfImages"]

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/generate/image",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        task_id = result.get("task_id", "")

                        if task_id:
                            return ToolResult(
                                success=True,
                                result=f"Image generation started. Task ID: {task_id}. The image will be available shortly.",
                                data={"task_id": task_id}
                            )
                        else:
                            url = result.get("url", "")
                            return ToolResult(
                                success=True,
                                result=f"Image generated: {url}",
                                data={"url": url}
                            )
                    else:
                        error_text = await resp.text()
                        return ToolResult(False, f"Image generation failed: {resp.status} - {error_text}")

        except Exception as e:
            return ToolResult(False, f"Image generation error: {str(e)}")

    async def _execute_generate_video(self, params: Dict[str, Any]) -> ToolResult:
        """Generate a video using Veo 3.1. Supports text-to-video, image-to-video, and video extension. Takes 5-20 minutes."""
        prompt = params.get("prompt", "")

        if not prompt:
            return ToolResult(False, "Video prompt is required")

        try:
            # Build payload with all supported parameters
            payload = {
                "prompt": prompt,
                "operator": self.operator
            }

            # Add optional parameters if provided
            if params.get("image_url"):
                payload["image_url"] = params["image_url"]
            if params.get("video_url"):
                payload["video_url"] = params["video_url"]
            if params.get("aspectRatio"):
                payload["aspectRatio"] = params["aspectRatio"]
            if params.get("duration"):
                payload["duration"] = params["duration"]
            if params.get("resolution"):
                payload["resolution"] = params["resolution"]
            if params.get("negativePrompt"):
                payload["negativePrompt"] = params["negativePrompt"]

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/generate/video",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        task_id = result.get("task_id", "")

                        # Determine mode for helpful message
                        mode = "Text-to-video"
                        if params.get("image_url"):
                            mode = "Image-to-video"
                        elif params.get("video_url"):
                            mode = "Video extension"

                        return ToolResult(
                            success=True,
                            result=f"{mode} generation started. Task ID: {task_id}. This will take 5-20 minutes. Use get_task_status to check progress.",
                            data={"task_id": task_id, "mode": mode}
                        )
                    else:
                        error_text = await resp.text()
                        return ToolResult(False, f"Video generation failed: {resp.status} - {error_text}")

        except Exception as e:
            return ToolResult(False, f"Video generation error: {str(e)}")

    async def _execute_generate_music(self, params: Dict[str, Any]) -> ToolResult:
        """Generate 30 seconds of music using Lyria."""
        prompt = params.get("prompt", "")

        if not prompt:
            return ToolResult(False, "Music prompt is required")

        try:
            # Build payload with all supported parameters
            payload = {
                "prompt": prompt,
                "operator": self.operator
            }

            # Add optional parameters if provided
            if params.get("negativePrompt"):
                payload["negativePrompt"] = params["negativePrompt"]
            if params.get("sampleCount"):
                payload["sampleCount"] = params["sampleCount"]

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/generate/lyria_music",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        task_id = result.get("task_id", "")

                        sample_info = ""
                        if params.get("sampleCount", 1) > 1:
                            sample_info = f" Generating {params['sampleCount']} samples."

                        return ToolResult(
                            success=True,
                            result=f"Music generation started. Task ID: {task_id}.{sample_info} Use get_task_status to check when it's ready.",
                            data={"task_id": task_id}
                        )
                    else:
                        error_text = await resp.text()
                        return ToolResult(False, f"Music generation failed: {resp.status} - {error_text}")

        except Exception as e:
            return ToolResult(False, f"Music generation error: {str(e)}")

    async def _execute_get_task_status(self, params: Dict[str, Any]) -> ToolResult:
        """Check the status of an async generation task."""
        task_id = params.get("task_id", "")

        if not task_id:
            return ToolResult(False, "Task ID is required")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/task/{task_id}",
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        status = result.get("status", "unknown")

                        if status == "completed":
                            url = result.get("url", result.get("result", {}).get("url", ""))
                            return ToolResult(
                                success=True,
                                result=f"Task completed! Result URL: {url}",
                                data={"status": status, "url": url, "result": result}
                            )
                        elif status == "failed":
                            error = result.get("error", "Unknown error")
                            return ToolResult(
                                success=False,
                                result=f"Task failed: {error}",
                                data={"status": status, "error": error}
                            )
                        else:
                            return ToolResult(
                                success=True,
                                result=f"Task status: {status}. Still in progress...",
                                data={"status": status}
                            )
                    elif resp.status == 404:
                        return ToolResult(False, f"Task not found: {task_id}")
                    else:
                        return ToolResult(False, f"Failed to check task status: {resp.status}")

        except Exception as e:
            return ToolResult(False, f"Task status error: {str(e)}")

    async def _execute_web_search(self, params: Dict[str, Any]) -> ToolResult:
        """Search the web using Perplexity Sonar (with DuckDuckGo fallback)."""
        query = params.get("query", "")
        max_results = params.get("max_results", 5)
        search_recency_filter = params.get("search_recency_filter", "month")

        if not query:
            return ToolResult(False, "Search query is required")

        try:
            from Orchestrator.web_tools import perform_web_search

            # perform_web_search returns formatted string
            result = perform_web_search(query, min(max_results, 10), search_recency_filter=search_recency_filter)

            if "❌" in result or "No results" in result:
                return ToolResult(
                    success=True,
                    result=result
                )

            return ToolResult(
                success=True,
                result=result
            )

        except Exception as e:
            return ToolResult(False, f"Web search error: {str(e)}")

    async def _execute_web_fetch(self, params: Dict[str, Any]) -> ToolResult:
        """Fetch and read content from a URL."""
        url = params.get("url", "")
        max_chars = params.get("max_chars", 80000)

        if not url:
            return ToolResult(False, "URL is required")

        try:
            from Orchestrator.web_tools import perform_web_fetch

            # perform_web_fetch returns formatted string
            result = perform_web_fetch(url, max_chars)

            if "❌" in result:
                return ToolResult(False, result)

            return ToolResult(
                success=True,
                result=result,
                data={"url": url}
            )

        except Exception as e:
            return ToolResult(False, f"Web fetch error: {str(e)}")

    async def _execute_get_media(self, params: Dict[str, Any]) -> ToolResult:
        """Retrieve media by URL or task_id."""
        url = params.get("url", "")
        task_id = params.get("task_id", "")

        if not url and not task_id:
            return ToolResult(False, "Either url or task_id is required")

        try:
            from Orchestrator.routes.chat_routes import execute_get_media
            result = execute_get_media(url, task_id)

            if result.get("error"):
                return ToolResult(False, result["error"])

            return ToolResult(
                success=True,
                result=f"Media found: {result.get('url', 'unknown')} ({result.get('type', 'unknown')})",
                data=result
            )
        except Exception as e:
            return ToolResult(False, f"Get media error: {str(e)}")

    async def _execute_list_media(self, params: Dict[str, Any]) -> ToolResult:
        """List media files in uploads folder."""
        media_type = params.get("media_type")
        limit = params.get("limit", 20)

        try:
            from Orchestrator.routes.chat_routes import execute_list_media
            result = execute_list_media(media_type, limit)

            # Function returns "media" not "files"
            media = result.get("media", [])
            if not media:
                return ToolResult(
                    success=True,
                    result="No media files found in uploads folder.",
                    data=result
                )

            # Format output for voice/text
            summary = f"Found {len(media)} media file(s):\n"
            for m in media[:15]:  # Show first 15
                desc = m.get('description', '')[:50] if m.get('description') else 'no description'
                summary += f"- {m.get('url', 'unknown')} ({m.get('type', 'unknown')}) - {desc}\n"
            if len(media) > 15:
                summary += f"... and {len(media) - 15} more"

            # Add usage hint
            summary += f"\n\n{result.get('usage_hint', '')}"

            return ToolResult(success=True, result=summary, data=result)
        except Exception as e:
            return ToolResult(False, f"List media error: {str(e)}")

    async def _execute_search_media(self, params: Dict[str, Any]) -> ToolResult:
        """Search media by description, prompt, or filename."""
        query = params.get("query", "")
        media_type = params.get("media_type")
        limit = params.get("limit", 10)

        if not query:
            return ToolResult(False, "Search query is required")

        try:
            from Orchestrator.routes.chat_routes import execute_search_media
            result = execute_search_media(query, media_type, limit)

            # Function returns "results" not "files"
            media = result.get("results", [])
            if not media:
                return ToolResult(
                    success=True,
                    result=f"No media found matching '{query}'",
                    data=result
                )

            # Format output
            summary = f"Found {len(media)} matching file(s) for '{query}':\n"
            for m in media[:15]:
                desc = m.get('description', '')[:50] if m.get('description') else 'no description'
                summary += f"- {m.get('url', 'unknown')} ({m.get('type', 'unknown')}) - {desc}\n"

            return ToolResult(success=True, result=summary, data=result)
        except Exception as e:
            return ToolResult(False, f"Search media error: {str(e)}")

    async def _execute_make_phone_call(self, params: Dict[str, Any]) -> ToolResult:
        """Initiate a phone call via cellular modem or Twilio."""
        phone_number = params.get("phone_number", "")
        greeting = params.get("greeting", "")
        role = params.get("role", "")
        backend = params.get("backend", "openai_realtime")

        if not phone_number:
            return ToolResult(False, "Phone number is required")

        # Normalize phone number
        if not phone_number.startswith("+"):
            if phone_number.startswith("1") and len(phone_number) == 11:
                phone_number = f"+{phone_number}"
            elif len(phone_number) == 10:
                phone_number = f"+1{phone_number}"

        # Determine call endpoint based on TELEPHONY_PROVIDER
        from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED
        if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
            call_endpoint = f"{self.base_url}/asterisk/call"
        elif TELEPHONY_PROVIDER in ("cellular", "auto") and CELLULAR_ENABLED:
            call_endpoint = f"{self.base_url}/cellular/call"
        else:
            call_endpoint = f"{self.base_url}/twilio/call"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    call_endpoint,
                    json={
                        "to": phone_number,
                        "backend": backend,
                        "operator": self.operator,
                        "greeting": greeting,
                        "role": role
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = await resp.json()

                    if result.get("status") == "initiated":
                        call_sid = result.get("call_sid", "")
                        return ToolResult(
                            success=True,
                            result=f"Phone call initiated to {phone_number}. Call SID: {call_sid}",
                            data={"call_sid": call_sid, "to": phone_number}
                        )
                    else:
                        error = result.get("error", "Unknown error")
                        return ToolResult(False, f"Failed to initiate call: {error}")

        except Exception as e:
            return ToolResult(False, f"Phone call error: {str(e)}")

    async def _execute_search_contacts(self, params: Dict[str, Any]) -> ToolResult:
        """Search the contact book."""
        query = params.get("query", "")
        if not query:
            return ToolResult(False, "Search query is required")

        try:
            results = _search_contacts(query, self.operator)
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

    # --- Cron Job Executors ---

    async def _execute_create_cron_job(self, params: Dict[str, Any]) -> ToolResult:
        """Create a new cron job."""
        try:
            from Orchestrator.scheduler import get_scheduler_manager
            manager = get_scheduler_manager()
            job = manager.create_job(
                name=params.get("name", "Unnamed Task"),
                prompt=params.get("prompt", ""),
                schedule=params.get("schedule", ""),
                operator=self.operator,
                frequency_hint=params.get("frequency_hint"),
                model=params.get("model", "gemini"),
                delivery=params.get("delivery", "snapshot"),
                delivery_target=params.get("delivery_target"),
                one_shot=params.get("one_shot", False)
            )
            hint = job.get("frequency_hint") or job["schedule"]
            return ToolResult(
                success=True,
                result=f"Cron job created: '{job['name']}' (ID: {job['id']}). Schedule: {hint}. Delivery: {job['delivery']}.",
                data={"job": job}
            )
        except ValueError as e:
            return ToolResult(False, f"Invalid cron job: {str(e)}")
        except Exception as e:
            return ToolResult(False, f"Create cron job error: {str(e)}")

    async def _execute_edit_cron_job(self, params: Dict[str, Any]) -> ToolResult:
        """Edit an existing cron job."""
        try:
            from Orchestrator.scheduler import get_scheduler_manager
            manager = get_scheduler_manager()
            job_id = params.pop("job_id", None)
            if not job_id:
                return ToolResult(False, "job_id is required")

            # Handle pause/resume
            if "pause" in params:
                if params.pop("pause"):
                    job = manager.pause_job(job_id)
                    if job:
                        return ToolResult(True, f"Cron job '{job['name']}' paused.", data={"job": job})
                else:
                    job = manager.resume_job(job_id)
                    if job:
                        return ToolResult(True, f"Cron job '{job['name']}' resumed.", data={"job": job})
                return ToolResult(False, "Job not found")

            # Update other fields
            updates = {k: v for k, v in params.items() if v is not None}
            job = manager.update_job(job_id, **updates)
            if not job:
                return ToolResult(False, f"Job not found: {job_id}")
            return ToolResult(
                success=True,
                result=f"Cron job '{job['name']}' updated.",
                data={"job": job}
            )
        except Exception as e:
            return ToolResult(False, f"Edit cron job error: {str(e)}")

    async def _execute_search_cron_jobs(self, params: Dict[str, Any]) -> ToolResult:
        """Search/list cron jobs."""
        try:
            from Orchestrator.scheduler import get_scheduler_manager
            manager = get_scheduler_manager()
            status_filter = params.get("status", "all")
            query = params.get("query", "")

            jobs = manager.list_jobs(
                operator=self.operator,
                status=None if status_filter == "all" else status_filter
            )

            # Filter by query if provided
            if query:
                query_lower = query.lower()
                jobs = [j for j in jobs if query_lower in j.get("name", "").lower()
                        or query_lower in j.get("prompt", "").lower()]

            if not jobs:
                return ToolResult(True, "No cron jobs found.", data={"jobs": []})

            # Format results
            lines = [f"Found {len(jobs)} cron job(s):\n"]
            for j in jobs:
                status_icon = {"active": "[ACTIVE]", "paused": "[PAUSED]"}.get(j["status"], "[?]")
                hint = j.get("frequency_hint") or j["schedule"]
                lines.append(f"{status_icon} {j['name']} (ID: {j['id']})")
                lines.append(f"   Schedule: {hint} | Delivery: {j['delivery']}")
                lines.append(f"   Prompt: {j['prompt'][:100]}{'...' if len(j.get('prompt','')) > 100 else ''}")
                if j.get("last_run_at"):
                    lines.append(f"   Last run: {j['last_run_at']}")
                lines.append("")

            return ToolResult(True, "\n".join(lines), data={"jobs": jobs})
        except Exception as e:
            return ToolResult(False, f"Search cron jobs error: {str(e)}")

    async def _execute_use_computer(self, params: Dict[str, Any]) -> ToolResult:
        """Launch Computer Use agent (Claude Opus CU on Linux desktop)."""
        prompt = params.get("prompt", "")
        url = params.get("url")
        device_id = params.get("device_id", "blackbox")

        if not prompt:
            return ToolResult(False, "Prompt is required for use_computer")

        try:
            from Orchestrator.tasks import create_task
            from Orchestrator.models import TaskType
            result_data = {"device_id": device_id}
            if url:
                result_data["url"] = url
            task = create_task(
                TaskType.USE_COMPUTER,
                operator=self.operator,
                prompt=prompt,
                result_data=result_data
            )
            return ToolResult(
                True,
                f"Computer Use task started. Task ID: {task.task_id}. Use get_task_status to check progress.",
                data={"task_id": task.task_id}
            )
        except Exception as e:
            return ToolResult(False, f"Computer use error: {str(e)}")

    async def _execute_list_devices(self, params: Dict[str, Any]) -> ToolResult:
        """List devices on the Tailscale mesh network."""
        from Orchestrator.device_registry import get_registry, DeviceType
        registry = get_registry()
        dtype = params.get("device_type")
        if dtype:
            try:
                devices = registry.get_devices_by_type(DeviceType(dtype))
            except ValueError:
                return ToolResult(False, f"Invalid device type: {dtype}. Use: android, linux, windows, macos")
        else:
            devices = registry.get_all_devices()
        if not devices:
            return ToolResult(True, "No devices registered. Add devices via POST /devices/")
        lines = []
        for d in devices:
            lines.append(f"  - {d.id}: {d.name} | {d.device_type.value} | "
                         f"{d.protocol.value} | {d.tailscale_ip} [{d.status.value}]")
            if d.description:
                lines.append(f"    {d.description}")
        return ToolResult(True, f"Devices ({len(devices)}):\n" + "\n".join(lines))

    async def _execute_control_android_device(self, params: Dict[str, Any]) -> ToolResult:
        """Control an Android device via Gemini Computer Use."""
        prompt = params.get("prompt", "")
        device_id = params.get("device_id", "")

        if not prompt:
            return ToolResult(False, "Prompt is required")
        if not device_id:
            return ToolResult(False, "device_id is required. Use list_devices to see available devices.")

        try:
            import asyncio
            from Orchestrator.device_registry import get_registry, DeviceProtocol
            from Orchestrator.tasks import create_task
            from Orchestrator.models import TaskType
            from Orchestrator.gemini_cu import get_or_create_session, run_gemini_cu_loop
            from Orchestrator.gemini_cu.config import DEFAULT_CU_MODEL
            from Orchestrator.routes.gemini_cu_routes import _run_task

            # Validate device exists and is ADB
            registry = get_registry()
            device = registry.get_device(device_id)
            if not device:
                return ToolResult(False, f"Device not found: {device_id}")
            if device.protocol != DeviceProtocol.ADB:
                return ToolResult(False, f"Device {device_id} is not an ADB device")

            # Ensure ADB connection
            from Orchestrator.adb import get_adb_manager
            conn_result = await get_adb_manager().ensure_connected(device_id)
            if not conn_result["success"]:
                return ToolResult(False, f"Cannot connect to device: {conn_result.get('error')}")

            # Create task
            task = create_task(
                TaskType.GEMINI_CU,
                operator=self.operator,
                prompt=prompt,
                result_data={
                    "device_id": device_id,
                    "environment": "android",
                    "model": DEFAULT_CU_MODEL,
                    "url": None,
                }
            )

            # Fire-and-forget the background task
            asyncio.create_task(_run_task(
                task.task_id, self.operator, device_id, "android",
                prompt, DEFAULT_CU_MODEL, None, None
            ))

            return ToolResult(
                True,
                f"Android CU task started on device '{device_id}'. Task ID: {task.task_id}. "
                f"Use get_task_status to check progress.",
                data={"task_id": task.task_id}
            )
        except Exception as e:
            return ToolResult(False, f"Error: {str(e)}")

    async def _execute_analyze_image(self, params: Dict[str, Any]) -> ToolResult:
        """Analyze an image using multimodal AI via /chat endpoint."""
        image_url = params.get("image_url", "")
        prompt = params.get("prompt", "Describe this image in detail")

        if not image_url:
            return ToolResult(False, "image_url is required")

        try:
            import aiohttp
            payload = {
                "operator": self.operator,
                "provider": "gemini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }]
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    result = await resp.json()
                    analysis = result.get("response", "")
                    return ToolResult(True, analysis, data={"provider": "gemini"})
        except Exception as e:
            return ToolResult(False, f"Analyze image error: {str(e)}")

    async def _execute_analyze_audio(self, params: Dict[str, Any]) -> ToolResult:
        """Analyze audio content via /analyze/audio (async task)."""
        file_path = params.get("file_path", "")
        prompt = params.get("prompt", "Transcribe and describe this audio")

        if not file_path:
            return ToolResult(False, "file_path is required")

        try:
            import aiohttp
            payload = {
                "file_path": file_path,
                "prompt": prompt,
                "operator": self.operator
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/analyze/audio",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    result = await resp.json()
                    task_id = result.get("task_id")
                    if task_id:
                        return ToolResult(
                            True,
                            f"Audio analysis queued. Task ID: {task_id}. Use get_task_status to check progress.",
                            data={"task_id": task_id}
                        )
                    return ToolResult(False, f"Failed to queue audio analysis: {result}")
        except Exception as e:
            return ToolResult(False, f"Analyze audio error: {str(e)}")

    async def _execute_analyze_video(self, params: Dict[str, Any]) -> ToolResult:
        """Analyze a video using multimodal AI via /chat endpoint."""
        video_url = params.get("video_url", "")
        prompt = params.get("prompt", "Describe what happens in this video")

        if not video_url:
            return ToolResult(False, "video_url is required")

        try:
            import aiohttp
            payload = {
                "operator": self.operator,
                "provider": "gemini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "video_url", "video_url": {"url": video_url}}
                    ]
                }]
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=180)
                ) as resp:
                    result = await resp.json()
                    analysis = result.get("response", "")
                    return ToolResult(True, analysis, data={"provider": "gemini"})
        except Exception as e:
            return ToolResult(False, f"Analyze video error: {str(e)}")

    async def _execute_speech_to_text(self, params: Dict[str, Any]) -> ToolResult:
        """Transcribe audio to text using OpenAI Whisper via /stt endpoint."""
        audio_path = params.get("audio_path", "")

        if not audio_path:
            return ToolResult(False, "audio_path is required")

        try:
            from pathlib import Path
            audio_file = Path(audio_path)
            if not audio_file.exists():
                return ToolResult(False, f"Audio file not found: {audio_path}")

            ext = audio_file.suffix.lower()
            content_types = {
                ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                ".ogg": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm"
            }
            content_type = content_types.get(ext, "audio/wav")

            import aiohttp
            data = aiohttp.FormData()
            data.add_field('file', open(audio_file, 'rb'),
                          filename=audio_file.name, content_type=content_type)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/stt",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    result = await resp.json()
                    text = result.get("text", "")
                    return ToolResult(True, f"Transcription: {text}", data={"text": text})
        except Exception as e:
            return ToolResult(False, f"Speech to text error: {str(e)}")

    async def _execute_text_to_speech(self, params: Dict[str, Any]) -> ToolResult:
        """Convert text to speech using OpenAI TTS via /tts endpoint."""
        text = params.get("text", "")
        voice = params.get("voice", "onyx")
        model = params.get("model", "tts-1-hd")

        if not text:
            return ToolResult(False, "text is required")

        try:
            import aiohttp
            payload = {
                "text": text,
                "voice": voice,
                "model": model,
                "return_json": True
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/tts",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    result = await resp.json()
                    audio_url = result.get("audio_url", "")
                    if audio_url:
                        return ToolResult(
                            True,
                            f"Speech generated. Audio URL: {audio_url} (voice: {voice}, model: {model})",
                            data={"audio_url": audio_url, "voice": voice, "model": model}
                        )
                    return ToolResult(False, f"TTS failed: {result}")
        except Exception as e:
            return ToolResult(False, f"Text to speech error: {str(e)}")

    async def _execute_list_tts_voices(self, params: Dict[str, Any]) -> ToolResult:
        """List available Google Cloud TTS voices via /tts/google/voices."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/tts/google/voices",
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = await resp.json()
                    voices = result.get("voices", [])
                    # Summarize to avoid overwhelming output
                    summary_lines = [f"Found {len(voices)} Google Cloud TTS voices.\n"]
                    # Group by language
                    lang_counts = {}
                    for v in voices:
                        for lc in v.get("languageCodes", []):
                            lang = lc.split("-")[0]
                            lang_counts[lang] = lang_counts.get(lang, 0) + 1
                    summary_lines.append("Languages: " + ", ".join(f"{k}({v})" for k, v in sorted(lang_counts.items())))
                    # Show first 20 English voices as examples
                    en_voices = [v["name"] for v in voices if any("en-" in lc for lc in v.get("languageCodes", []))][:20]
                    if en_voices:
                        summary_lines.append(f"\nSample English voices: {', '.join(en_voices)}")
                    return ToolResult(True, "\n".join(summary_lines), data={"voice_count": len(voices)})
        except Exception as e:
            return ToolResult(False, f"List TTS voices error: {str(e)}")

    async def _execute_gemini_pro_tts(self, params: Dict[str, Any]) -> ToolResult:
        """Generate speech using Gemini Pro TTS via /generate/gemini_tts (async task)."""
        text = params.get("text", "")
        voice = params.get("voice", "Charon")

        if not text:
            return ToolResult(False, "text is required")

        try:
            import aiohttp
            payload = {
                "text": text,
                "voice_name": voice,
                "operator": self.operator,
                "multi_speaker": False,
                "model": "gemini-2.5-pro-tts"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/generate/gemini_tts",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    result = await resp.json()
                    task_id = result.get("task_id")
                    if task_id:
                        return ToolResult(
                            True,
                            f"Gemini Pro TTS generation started. Task ID: {task_id}. Voice: {voice}. Use get_task_status to check progress.",
                            data={"task_id": task_id, "voice": voice}
                        )
                    return ToolResult(False, f"Failed to start Gemini Pro TTS: {result}")
        except Exception as e:
            return ToolResult(False, f"Gemini Pro TTS error: {str(e)}")

    # ── Gmail Tools ──────────────────────────────────────────────────────

    async def _execute_gmail_search(self, params: Dict[str, Any]) -> ToolResult:
        """Search/list emails in the operator's Gmail inbox."""
        import json
        from Orchestrator.gmail.service import list_messages
        operator = params.get("operator", self.operator or "Brandon")
        query = params.get("query", "")
        max_results = min(int(params.get("max_results", 10)), 20)
        results = list_messages(operator, query, max_results)
        return ToolResult(success=True, result=json.dumps(results, indent=2))

    async def _execute_gmail_read(self, params: Dict[str, Any]) -> ToolResult:
        """Read full email content by message ID."""
        import json
        from Orchestrator.gmail.service import get_message
        operator = params.get("operator", self.operator or "Brandon")
        message_id = params.get("message_id", "")
        if not message_id:
            return ToolResult(success=False, result="message_id is required")
        result = get_message(operator, message_id)
        return ToolResult(success=True, result=json.dumps(result, indent=2))

    async def _execute_gmail_send(self, params: Dict[str, Any]) -> ToolResult:
        """Compose and send a new email."""
        import json
        from Orchestrator.gmail.service import send_email
        operator = params.get("operator", self.operator or "Brandon")
        to = params.get("to", "")
        subject = params.get("subject", "")
        body = params.get("body", "")
        cc = params.get("cc", "")
        print(f"[GMAIL-SEND] operator={operator}, to={to}, subject={subject[:50]}, body_len={len(body)}")
        if not to or not subject or not body:
            return ToolResult(success=False, result=f"to, subject, and body are all required. Got: to='{to}', subject='{subject}', body_len={len(body)}")
        result = send_email(operator, to, subject, body, cc)
        print(f"[GMAIL-SEND] Result: {json.dumps(result)}")
        return ToolResult(success=True, result=json.dumps(result))

    async def _execute_gmail_reply(self, params: Dict[str, Any]) -> ToolResult:
        """Reply to an existing email thread."""
        import json
        from Orchestrator.gmail.service import send_email, get_message
        operator = params.get("operator", self.operator or "Brandon")
        message_id = params.get("message_id", "")
        thread_id = params.get("thread_id", "")
        body = params.get("body", "")
        if not message_id or not thread_id or not body:
            return ToolResult(success=False, result="message_id, thread_id, and body are all required")
        # Get original message to extract subject and sender for reply
        original = get_message(operator, message_id)
        to = original.get("from", "")
        subject = original.get("subject", "")
        if not subject.startswith("Re: "):
            subject = f"Re: {subject}"
        result = send_email(operator, to, subject, body, reply_to_message_id=message_id, thread_id=thread_id)
        return ToolResult(success=True, result=json.dumps(result))

    async def _execute_gmail_labels(self, params: Dict[str, Any]) -> ToolResult:
        """List labels or modify message labels."""
        import json
        from Orchestrator.gmail.service import get_labels, modify_message
        operator = params.get("operator", self.operator or "Brandon")
        action = params.get("action", "list")
        message_id = params.get("message_id", "")

        if action == "list":
            result = get_labels(operator)
            return ToolResult(success=True, result=json.dumps(result, indent=2))

        if not message_id:
            return ToolResult(success=False, result="message_id required for modify actions")

        label_map = {
            "mark_read": ([], ["UNREAD"]),
            "mark_unread": (["UNREAD"], []),
            "archive": ([], ["INBOX"]),
            "star": (["STARRED"], []),
            "unstar": ([], ["STARRED"]),
        }
        add_labels, remove_labels = label_map.get(action, ([], []))
        result = modify_message(operator, message_id, add_labels, remove_labels)
        return ToolResult(success=True, result=json.dumps(result))

    # ── Video Extension Tool ─────────────────────────────────────────────

    async def _execute_extend_video(self, params: Dict[str, Any]) -> ToolResult:
        """Extend an existing video using Veo 3.1 via /extend/video (async task)."""
        video_url = params.get("video_url", "")
        prompt = params.get("prompt", "")

        if not video_url:
            return ToolResult(False, "video_url is required")

        try:
            import aiohttp
            payload = {
                "video_url": video_url,
                "prompt": prompt,
                "operator": self.operator
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/extend/video",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    result = await resp.json()
                    task_id = result.get("task_id")
                    if task_id:
                        return ToolResult(
                            True,
                            f"Video extension started. Task ID: {task_id}. The new clip will continue from where the original ended. Use get_task_status to check progress (5-20 minutes).",
                            data={"task_id": task_id}
                        )
                    return ToolResult(False, f"Failed to start video extension: {result}")
        except Exception as e:
            return ToolResult(False, f"Extend video error: {str(e)}")


    async def _execute_toolvault(self, params: Dict[str, Any]) -> ToolResult:
        """Execute a ToolVault meta-tool action (search/read/list)."""
        from Orchestrator.toolvault.meta_tool import execute as tv_execute
        action = params.get("action", "")
        # Pass all params except 'action' to the executor
        action_params = {k: v for k, v in params.items() if k != "action"}
        result = tv_execute(action, **action_params)
        return ToolResult(
            success=result.success,
            result=result.result,
            data=result.data if result.data else None,
        )


# =============================================================================
# Helper Functions
# =============================================================================

def get_tools_for_backend(backend: str, group: str = "phone") -> List[Dict]:
    """Get tool definitions in the correct format for a backend.

    Uses the unified tool registry. The 'group' param controls which subset
    of tools to include (default: 'phone' for backward compat with voice routes).
    """
    from Orchestrator.tools.tool_registry import (
        get_anthropic_tools as _get_anthropic,
        get_openai_realtime_tools as _get_realtime,
        get_gemini_live_tools as _get_gemini_live,
    )
    if backend in ("openai", "openai_realtime", "grok", "grok_live"):
        return _get_realtime(group)
    elif backend in ("gemini", "gemini_live"):
        return _get_gemini_live(group)
    elif backend in ("anthropic", "claude", "sms"):
        return _get_anthropic(group)
    else:
        return _get_anthropic(group)  # Default


async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    operator: str = "system"
) -> ToolResult:
    """
    Convenience function to execute a tool.

    Usage:
        result = await execute_tool("send_sms", {"phone_number": "+1555...", "message": "Hello"}, "Brandon")
    """
    executor = BlackBoxToolExecutor(operator=operator)
    return await executor.execute(tool_name, tool_input)
