"""Executor for the toolvault meta-tool — delegates to meta_tool.execute."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.toolvault.meta_tool import execute as _meta_execute


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Run a toolvault meta-tool action (search/read/list).

    Replicates the param extraction + MetaToolResult→ToolResult conversion that
    blackbox_tools._execute_toolvault used to perform: pull ``action``, pass the
    remaining params through to meta_tool.execute, and map the result fields.
    """
    action = params.get("action", "")
    # Pass all params except 'action' to the meta-tool executor.
    action_params = {k: v for k, v in params.items() if k != "action"}
    result = _meta_execute(action, **action_params)
    return ToolResult(
        success=result.success,
        result=result.result,
        data=result.data if result.data else None,
    )
