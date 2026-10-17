"""Executor for the toolvault meta-tool — delegates to meta_tool.execute."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.toolvault.meta_tool import execute as _meta_execute


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Run a toolvault meta-tool action (search/read/list).

    Replicates the param extraction + MetaToolResult→ToolResult conversion that
    blackbox_tools._execute_toolvault used to perform: pull ``action``, pass the
    remaining params through to meta_tool.execute, and map the result fields.
    The dispatcher's ctx goes along so 'read' resolves the operator enum for
    THIS session only (a remote MCP caller never sees the box's roster).
    """
    action = params.get("action", "")
    # Pass all params except 'action' to the meta-tool executor. 'ctx' is the
    # dispatcher's to set, never the model's.
    action_params = {k: v for k, v in params.items() if k not in ("action", "ctx")}
    result = _meta_execute(action, ctx=ctx, **action_params)
    return ToolResult(
        success=result.success,
        result=result.result,
        data=result.data if result.data else None,
    )
