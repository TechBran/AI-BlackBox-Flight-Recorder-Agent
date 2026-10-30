"""Executor for wellness_series: one metric over time, at most 120 points, up to
365 days per call, coarsened to fit (spec §4.2, §10).

The operator is the session's (ctx.operator), never a parameter. The gate runs
for the metric's category first, and the answer is written to the operator's
delivered log for this turn (Orchestrator/wellness/access.run).
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.wellness import access, api, catalog

TOOL = "wellness_series"


def execute(params: dict, ctx: ToolContext) -> ToolResult:
    metric = params.get("metric")
    category = catalog.category_of(metric) if isinstance(metric, str) else None
    if category is None or metric == "sleep_stages":
        return access.invalid(TOOL, "metric is one of " + ", ".join(
            m for m in catalog.METRICS if m != "sleep_stages"))
    start, end = params.get("start"), params.get("end")
    range_ = params.get("range") or (None if start or end else "last_7_days")
    granularity = params.get("granularity") or "auto"
    stat = params.get("stat") or "auto"

    def call(operator, categories, delivery, budget, now_ms):
        return api.series(operator, metric, range_, start=start, end=end,
                          granularity=granularity, stat=stat, categories=categories,
                          now_ms=now_ms, delivery=delivery, budget_tokens=budget)

    return access.run(TOOL, ctx, [category], call, explicit=True)
