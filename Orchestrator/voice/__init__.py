"""Shared live-voice machinery (audit rank 74, X05-architecture-13).

The three realtime voice routes (Gemini Live, OpenAI Realtime, xAI Grok) and
the phone bridge used to carry their own copy of one session state machine,
one tool ladder, one prompt and one transcript save, and fixes landed in one
copy only (P1.7's terminal close reached Gemini alone). They now share:

  bridge.py      LiveVoiceBridge: the client WS endpoint loop, connect,
                 reconnect with backoff, keepalive, upstream listener and
                 teardown, driven through a per-provider VoiceAdapter that
                 speaks the provider's wire vocabulary.
  tools.py       the one voice tool ladder (and the tool-call dispatch both
                 wire formats use).
  transcript.py  the transcript a voice session saves to the ledger.

The provider adapters live with their routes (Orchestrator/routes/
gemini_live_routes.py, realtime_routes.py, grok_live_routes.py): connect,
session configuration and the message vocabulary are what differs, and the
routes' module-level functions stay the entry points other modules (the phone
bridge, the xAI SIP call bridge, the live-session reaper) call.
"""
