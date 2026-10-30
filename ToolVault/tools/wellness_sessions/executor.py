"""Executor for wellness_sessions: sleep sessions or workouts, newest first
(spec §4.2).

The operator is the session's (ctx.operator), never a parameter. The gate runs
for sleep or activity first, and the answer is written to the operator's
delivered log for this turn (Orchestrator/wellness/access.run).
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.utils.tool_args import int_value
from Orchestrator.wellness import access, api

TOOL = "wellness_sessions"
CATEGORY_OF_KIND = {"sleep": "sleep", "exercise": "activity"}


def execute(params: dict, ctx: ToolContext) -> ToolResult:
    kind = params.get("kind")
    if kind not in CATEGORY_OF_KIND:
        return access.invalid(TOOL, "kind is sleep or exercise")
    start, end = params.get("start"), params.get("end")
    range_ = params.get("range") or (None if start or end else "last_30_days")
    exercise_type = params.get("exercise_type") or None
    limit = int_value(params.get("limit"), 10, 1, api.MAX_SESSIONS)

    def call(operator, categories, delivery, budget, now_ms):
        return api.sessions(operator, kind, range_, start=start, end=end,
                            exercise_type=exercise_type, limit=limit, categories=categories,
                            now_ms=now_ms, delivery=delivery, budget_tokens=budget)

    return access.run(TOOL, ctx, [CATEGORY_OF_KIND[kind]], call, explicit=True)
