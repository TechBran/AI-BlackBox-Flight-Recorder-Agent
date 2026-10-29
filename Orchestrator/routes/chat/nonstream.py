"""POST /chat: queue a non-stream turn for the worker (tasks.process_chat_task).

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

import json

from Orchestrator.utils.operators import InvalidOperatorName
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from Orchestrator.checkpoint import app
from Orchestrator.startup import ChatIn
from Orchestrator.config import set_current_operator
from Orchestrator.models import TaskType
from Orchestrator.tasks import create_task

from Orchestrator.routes.chat.context import _checked_operator


# -----------------------------------------------------------------------------
# Chat Endpoint (with integrated fixes)
# -----------------------------------------------------------------------------
@app.post("/chat")
async def chat_async(request: Request):
    """Async chat endpoint - creates a task and returns task ID immediately"""
    try:
        # Parse request
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            inp_dict = await request.json()
        elif "multipart/form-data" in content_type:
            form_data = await request.form()
            inp_dict = {}
            for key, value in form_data.items():
                if key == "messages":
                    try:
                        inp_dict[key] = json.loads(value)
                    except json.JSONDecodeError:
                        raise HTTPException(status_code=400, detail=f"Invalid JSON in 'messages' field: {value}")
                else:
                    inp_dict[key] = value
        else:
            raise HTTPException(status_code=415, detail=f"Unsupported content type: {content_type}")
        
        # Validate input
        inp = ChatIn(**inp_dict)
        
        if not inp.messages:
            raise HTTPException(status_code=400, detail="The 'messages' field is required and cannot be empty.")

        try:
            active_operator = _checked_operator(inp.operator)
        except InvalidOperatorName as e:
            raise HTTPException(status_code=400, detail=f"Invalid operator: {e}")
        set_current_operator(active_operator)

        # M4 origin-aware device routing: do NOT call _set_origin_device_id here.
        # This route only QUEUES — the turn (and every tool call in it) runs later on
        # a worker thread, in a different context, so a contextvar stamped on this
        # request would be gone by then. inp_dict carries origin_device_id into
        # result_data and tasks.stamp_origin_device() stamps it on the worker,
        # unconditionally, right before the provider dispatch that reads it.

        # Create async task
        task = create_task(
            TaskType.CHAT,
            operator=active_operator,
            result_data=inp_dict  # Store the entire request for processing
        )

        # Return task ID immediately
        return {
            "task_id": task.task_id,
            "status": task.status,
            "message": "Chat request queued for processing"
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        error_msg = str(e)
        trace = traceback.format_exc()
        print(f"[CHAT] Error creating task: {error_msg}\n{trace}")
        return JSONResponse(status_code=500, content={
            "status": "error",
            "error": error_msg,
            "trace": trace
        })
