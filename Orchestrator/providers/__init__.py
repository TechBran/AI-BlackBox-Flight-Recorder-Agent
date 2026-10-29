"""Chat provider seams shared by every chat loop (audit rank 74, X05-architecture-11).

Orchestrator/routes/chat_routes.py used to hold one hand-cloned tool loop per
provider and path; a fix landed in one copy and not the others. The pieces the
loops share now live here, once:

  * tool_dispatch  - dispatch_tool(): the one tool-execution path (the native
                     media / snapshot / media-lookup branches, the executor
                     branches with their own progress labels, and the
                     BlackBoxToolExecutor catch-all), with each loop's legacy
                     differences kept as data in its LoopProfile.

The public chat_routes names (stream_<provider>_*, call_<provider>) stay where
other modules and the tests import them from.
"""
