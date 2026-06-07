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
