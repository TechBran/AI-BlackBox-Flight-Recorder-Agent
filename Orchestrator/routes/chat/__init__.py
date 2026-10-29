"""The chat routes, one module per concern (audit rank 74).

  context    - origin device, carried location, uploads, operator check, the
               media auto-route and the context builders
  media      - the media lookups and the media library routes
  providers  - stream_<provider>_* / call_<provider>: thin wrappers over the
               Orchestrator/providers adapters
  save       - POST /chat/save and the CU / on-device persist paths
  cu         - Computer Use as a chat provider and its session routes
  stream     - POST /chat/stream
  nonstream  - POST /chat

Import them through Orchestrator.routes.chat_routes, the one namespace (see its
docstring): it re-exports every name here, and a stub set on it reaches the
module that uses the name.
"""
