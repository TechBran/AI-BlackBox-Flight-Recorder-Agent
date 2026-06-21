#!/usr/bin/env python3
"""
sms_processor.py - Shared SMS AI Processing

Extracted from twilio_routes.py to be reusable by both Twilio webhooks
and the cellular SMS handler. Processes incoming SMS through Claude with
tool support (contacts, memory, calls, etc.).
"""

import aiohttp
from typing import Optional


async def process_incoming_sms(sender: str, body: str, operator: str) -> str:
    """
    Process an incoming SMS message through Claude Sonnet with tool support.

    Args:
        sender: Phone number of the sender (E.164 format)
        body: SMS message body
        operator: BlackBox operator name (e.g., "SMS-1234")

    Returns:
        AI response text (truncated to 1500 chars for SMS)
    """
    from Orchestrator.config import ANTHROPIC_API_KEY
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor

    if not body.strip():
        return "Hello! I'm the AI BlackBox assistant. Text me anything or ask me to call you back!"

    try:
        tool_executor = BlackBoxToolExecutor(operator=operator)

        # Import tool definitions from chat_routes (Anthropic format)
        from Orchestrator.tools import BLACKBOX_TOOLS_ANTHROPIC

        system_prompt = f"""You are the AI BlackBox assistant responding to SMS text messages.
Keep responses CONCISE - SMS has character limits (160 chars ideal, 320 max for multi-part).
The user is texting from phone number: {sender}
Be helpful but brief. No markdown, no formatting - plain text only.

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present before responding.

You have access to these tools:
- send_sms: Send a text message to any phone number
- make_voice_call: Call someone and deliver a voice message (pre-generates TTS for instant playback)
- search_memory: Your primary memory — search FIRST for past conversations, user preferences, decisions, and context. The BlackBox has 1,600+ snapshots of every interaction.
- search_contacts: Search the contact book for people by name, phone number, tag, or keyword
- save_contact: Save a new contact or update an existing one (requires name, notes, and tags)
- get_current_time: Get the current date and time
- gemini_image / openai_image / grok_image: Generate an AI image (pick a provider)
- create_cron_job: Schedule a recurring or one-time task (e.g., daily reminders, hourly checks)
- edit_cron_job: Modify an existing scheduled task (change schedule, prompt, delivery, or pause/resume)
- search_cron_jobs: List and search the user's scheduled tasks

Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
If the user asks you to call them, use make_voice_call with their number ({sender}).
If they want to text someone, use send_sms."""

        messages = [{"role": "user", "content": body}]

        async with aiohttp.ClientSession() as http_session:
            # First API call
            async with http_session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "system": system_prompt,
                    "tools": BLACKBOX_TOOLS_ANTHROPIC,
                    "messages": messages
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"[SMS-PROC] Anthropic API error: {resp.status} - {error_text[:200]}")
                    return "Sorry, I'm having trouble right now. Please try again later."

                result = await resp.json()
                stop_reason = result.get("stop_reason", "")
                content = result.get("content", [])

                if stop_reason == "tool_use":
                    # Execute tool calls
                    tool_results = []
                    text_parts = []

                    for block in content:
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})
                            tool_id = block.get("id")

                            print(f"[SMS-PROC] Executing tool: {tool_name} with {tool_input}")
                            result_obj = await tool_executor.execute(tool_name, tool_input)
                            tool_result = result_obj.result
                            print(f"[SMS-PROC] Tool result: {tool_result}")

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": tool_result
                            })
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))

                    # Follow-up call with tool results
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": tool_results})

                    async with http_session.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": ANTHROPIC_API_KEY,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json"
                        },
                        json={
                            "model": "claude-sonnet-4-20250514",
                            "max_tokens": 500,
                            "system": system_prompt,
                            "tools": BLACKBOX_TOOLS_ANTHROPIC,
                            "messages": messages
                        },
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp2:
                        if resp2.status == 200:
                            result2 = await resp2.json()
                            content2 = result2.get("content", [])
                            response_text = ""
                            for block in content2:
                                if block.get("type") == "text":
                                    response_text += block.get("text", "")
                            if not response_text:
                                response_text = "Done! " + (text_parts[0] if text_parts else "")
                        else:
                            response_text = "I tried to help but encountered an error."
                else:
                    # No tool use - extract text
                    response_text = ""
                    for block in content:
                        if block.get("type") == "text":
                            response_text += block.get("text", "")

                    if not response_text:
                        response_text = "I couldn't process that message. Please try again."

                # Truncate for SMS
                if len(response_text) > 1500:
                    response_text = response_text[:1497] + "..."

                return response_text

    except Exception as e:
        print(f"[SMS-PROC] Error processing message: {e}")
        import traceback
        traceback.print_exc()
        return "Sorry, I encountered an error. Please try again."
