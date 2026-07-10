"""Shared voice-surface prompt fragments.

Home for prompt text that must be BYTE-IDENTICAL across the three real-time
voice routes (``realtime_routes`` / ``gemini_live_routes`` / ``grok_live_routes``).
Keeping the invariant text in ONE place makes drift structurally impossible —
there is a single copy to edit — and, because it is a plain string constant
rather than inline f-string text, it holds literal braces (e.g. the
``{"success": false, ...}`` failure shape) with NO f-string escaping. That
removes the unescaped-brace bug class entirely: an f-string carrying the block
inline could be mis-edited to a valid-but-broken replacement field that
``py_compile`` and ``ast.parse`` both accept and that only raises ``ValueError``
when the f-string is evaluated at a live voice session.

Only genuinely invariant, per-branch-context-free text belongs here. Prose that
varies by voice/persona/operator stays inline in each route.

This module is a dependency-free leaf (pure string data), so importing it from
the route modules cannot introduce an import cycle.
"""

# NOTE: this is a PLAIN string (not an f-string). Braces below are literal — do
# NOT double them. The routes interpolate it as ``{CU_CONTROL_BLOCK}``.
CU_CONTROL_BLOCK = """COMPUTER CONTROL:
use_computer drives a real computer — it can browse the web, use apps, and run commands. It starts an ASYNCHRONOUS background task and returns a task_id right away; it does NOT block or return the result. When you need it, ACTUALLY CALL IT — don't just say you're going to (never say "let me open the browser" and then stop). Once it's launched, tell the user it's running and that they can watch its progress on the live task pill (the result shows up there when it finishes), then END your turn — the pill surfaces completion, you do not. Do NOT poll get_task_status in a loop waiting for it to finish; that stalls the conversation and burns the turn. If the user later asks whether it's done, you may check get_task_status(task_id) once and report back — a single check, never a spin-loop.
- The optional model param names a model CLASS: opus (default, most capable), sonnet, fable, gemini, or gpt. Omit it unless the user asks for a specific provider; never name a concrete model id.
- Two kinds of failure. SYNCHRONOUS — the tool call itself returns {"success": false, ...} for an unresolvable model class: read its "available" list and retry with a class from it; when "retryable" is false, say what went wrong instead of retrying.
- ASYNCHRONOUS — the launch is always accepted and returns a task_id even when the machine can't actually run it. The local display is single-tenant, so if another Computer Use task holds it, the task comes back FAILED naming who holds the display (the task pill shows this, and a one-shot get_task_status check would too). If you see that, relay it and offer to retry — do not loop."""
