# AI BlackBox — Flight Recorder - AI Agent 

> **One Linux box. Every AI model. Total recall. Zero lock-in.**

The AI BlackBox is a self-hosted AI workstation that runs on customer-class
Linux hardware. It exposes the major frontier-model providers (Anthropic
Claude, OpenAI GPT, Google Gemini, xAI Grok, Perplexity Sonar) through a
single chat interface, records every conversation as an immutable
byte-offset snapshot, and exposes that memory back to the models as
searchable context.

Beyond chat, the BlackBox bundles a desktop Computer Use agent, three
terminal CLI agents (Claude Code, Gemini CLI, Codex), an MCP server that
lets those CLIs query the BlackBox's memory, scheduled tasks, multimodal
generation, a telephony bridge that puts an AI on a real phone line, a
cellular modem fallback for connectivity, and a paired Android remote.

The same machine your customers buy at install time keeps getting better
without re-flashing — the in-place update pipeline pulls new code from
GitHub on a button press while preserving every snapshot, every secret,
every paired device.

---

## Table of Contents

- [Quick install](#quick-install)
- [What you get](#what-you-get)
- [Architecture overview](#architecture-overview)
- [The snapshot ledger](#the-snapshot-ledger)
- [Chat + model providers](#chat--model-providers)
- [Voice agents](#voice-agents)
- [Computer Use](#computer-use)
- [CLI Agents (Claude Code / Gemini CLI / Codex)](#cli-agents)
- [MCP server](#mcp-server)
- [Telephony bridge](#telephony-bridge)
- [TG200 cellular gateway](#tg200-cellular-gateway)
- [SIM7600G-H cellular modem](#sim7600g-h-cellular-modem)
- [Android Portal app](#android-portal-app)
- [Apps platform](#apps-platform)
- [Scheduler](#scheduler)
- [Onboarding wizard](#onboarding-wizard)
- [Update pipeline](#update-pipeline)
- [Configuration reference](#configuration-reference)
- [Operating the BlackBox](#operating-the-blackbox)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)
- [Status and roadmap](#status-and-roadmap)

---

## Quick install

**Target hardware:** Ubuntu 24.04 LTS Desktop, x86_64, ≥8 GB RAM, ≥256 GB SSD.

**Tested on:** MSO2 Ultra mini-PC (Intel N100, 16 GB, 512 GB NVMe).

```bash
# 1. Clone the repo into your install location of choice
git clone https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git ~/Desktop/ai-blackbox-flight-recorder-agent
cd ~/Desktop/ai-blackbox-flight-recorder-agent

# 2. Run the installer — handles apt packages, Python venv, Node + CLI agents,
#    MCP server, systemd unit, sudoers, Tailscale, and the Tauri wizard wrapper.
sudo ./Scripts/install.sh

# 3. Reboot (or log out + back in) so the X11 session + group changes take effect.
sudo reboot
```

On first boot, the BlackBox Setup wizard launches automatically and walks
you through 8 onboarding steps (Tailscale, API keys, Gmail OAuth, phone
pairing, CLI agent sign-in, operator selection, done). Skip anything you
don't need and revisit later from the System Menu.

Once onboarding completes, the Portal is at:
- `http://localhost:9091` — local
- `https://<tailnet-hostname>` — anywhere on your tailnet (via Tailscale serve)

---

## What you get

| Capability | Detail |
|---|---|
| **Chat** | Streaming chat with 5+ model providers, per-message reasoning visibility, file uploads, image+video+audio attachments, snapshot-aware context |
| **Voice** | OpenAI Realtime / Gemini Live / Grok Live WebSocket sessions; STT (Whisper); TTS (OpenAI HD + Google Cloud 1,000+ voices + Gemini Pro TTS 30 voices) |
| **Computer Use** | Anthropic + Gemini desktop agents that screenshot, click, type, run shell commands on your real X11 desktop OR any remote machine on your tailnet |
| **CLI Agents** | `claude` / `gemini` / `codex` terminal CLIs accessible from the web Portal or Android app via tmux PTY bridge — each registered as MCP clients of the BlackBox |
| **MCP server** | Stdio MCP server registered with claude/gemini/codex on every session — exposes 62 BlackBox tools (snapshot search/get/mint, web search + fetch, image/video/music generation, Gmail, devices, robot control if paired) plus 4 read-only MCP resources for index stats and operator list |
| **Telephony** | Asterisk + PJSIP outbound/inbound calls via a TG200 SIP-to-GSM gateway (2 SIM cards / TG200-2 unit) plus Twilio integration for direct VoIP |
| **Cellular** | SIM7600G-H USB modem for failover internet AND a second voice channel (8 kHz PCM16 audio bridge to Asterisk) |
| **SMS** | Inbound/outbound SMS via the TG200 GSM gateway |
| **Multimodal generation** | Image (Gemini Imagen / Veo), Video (Veo 3.1), Music (Lyria), TTS variants — all callable from chat as tools |
| **Scheduled tasks** | APScheduler-backed cron with SQLite persistence — schedule any model to run on cadence and deliver via chat/SMS/email |
| **Memory** | Immutable byte-offset volume + 3072-dim embedding index across every conversation; semantic + recency retrieval injected into every prompt |
| **Checkpoints** | Auto-summarize every 50 turns into a checkpoint snapshot for long-running context |
| **Apps** | Reverse-proxy at `/app-proxy/<port>/` for any local web app — your customers can vibe-code their own sub-apps and have them appear in the Portal |
| **Tailnet remote** | Tailscale install + auth + HTTPS cert from the wizard; the Portal serves on port 443 over your tailnet |
| **Android remote** | Paired Android app re-uses your operator identity; chat / voice / CLI agent terminals over WebSocket via Tailscale |
| **Onboarding wizard** | 8-step first-run UI that handles install validation, Tailscale, all API keys, Gmail OAuth, phone pairing, CLI agent sign-in, operator setup |
| **In-place updates** | One-button GitHub pull → pip install → systemd regen → service restart, with rollback to any pre-update tag |

---

## Architecture overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Customer hardware (Ubuntu 24.04 — typically a mini-PC)              │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  blackbox.service (systemd, runs as the user who ran install.sh)│  │
│  │  ┌─────────────────────────────────────────────────────────┐   │  │
│  │  │  uvicorn + FastAPI (port 9091)                          │   │  │
│  │  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  │   │  │
│  │  │  │  chat    │  │ voice    │  │ computer │  │  cli-   │  │   │  │
│  │  │  │  routes  │  │ routes   │  │ use      │  │ agent   │  │   │  │
│  │  │  │ /chat    │  │ ws/      │  │ routes   │  │ routes  │  │   │  │
│  │  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬────┘  │   │  │
│  │  │       │ ─────────────┴─────────────┴────────────┘       │   │  │
│  │  │       ▼ (tool calls)                                    │   │  │
│  │  │  ┌──────────────────────────────────────────────────┐   │   │  │
│  │  │  │  72 tools registered (chat/voice/cu/CLI all      │   │   │  │
│  │  │  │  share the same registry)                        │   │   │  │
│  │  │  └────┬─────────────────────────────────────────────┘   │   │  │
│  │  │       ▼                                                 │   │  │
│  │  │  ┌──────────────────────────────────────────────────┐   │   │  │
│  │  │  │  Memory layer                                    │   │   │  │
│  │  │  │  • Volumes/SNAPSHOT_VOLUME.txt  (append-only)    │   │   │  │
│  │  │  │  • Manifest/snapshot_index.json (byte offsets +  │   │   │  │
│  │  │  │    3072-dim gemini-embedding-001 vectors)        │   │   │  │
│  │  │  │  • Fossils/                     (archived snaps) │   │   │  │
│  │  │  └──────────────────────────────────────────────────┘   │   │  │
│  │  └─────────────────────────────────────────────────────────┘   │  │
│  │                                                                │  │
│  │  ┌───────────────┐  ┌──────────────┐  ┌────────────────────┐   │  │
│  │  │  ydotoold     │  │ tailscaled   │  │  asterisk          │   │  │
│  │  │  (input)      │  │ (mesh net)   │  │  (telephony)       │   │  │
│  │  └───────────────┘  └──────────────┘  └────────────────────┘   │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌───────────────────────┐    ┌──────────────────────────────────┐  │
│  │  Portal (web UI)      │    │ Tauri wrapper (windowed)         │  │
│  │  HTML/CSS/JS modules  │    │ wraps the onboarding wizard at   │  │
│  │  served from /ui      │    │ /onboarding only — the main      │  │
│  │  in user's browser    │    │ Portal opens in the user's       │  │
│  │                       │    │ default browser after onboarding │  │
│  └───────────────────────┘    └──────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
        │                                              │
        │ Tailscale mesh (HTTPS, MagicDNS)             │ USB peripherals
        │                                              │
        ▼                                              ▼
  ┌─────────────┐                          ┌────────────────────────┐
  │ Android app │                          │  SIM7600G-H modem      │
  │ (Pixel etc) │                          │  TG200 SIP-GSM gateway │
  └─────────────┘                          │  Other tailnet devices │
                                           └────────────────────────┘
```

**Storage shape on disk** (everything under `$BLACKBOX_ROOT`):

```
Volumes/SNAPSHOT_VOLUME.txt   ← every snapshot, ever (append-only, byte-addressable)
Manifest/snapshot_index.json  ← {SNAP-ID: {byte_start, byte_end, embedding[3072], ...}}
Manifest/schema_version.json  ← version sentinel (rebuild trigger on mismatch)
Manifest/update_state.json    ← update pipeline state machine (crash recovery)
Manifest/update.lock          ← flock-based update mutex
Manifest/paired_devices.json  ← persistent registry of paired Android devices
Fossils/                      ← archived rotated volumes
Portal/uploads/               ← user-uploaded + AI-generated media files
Apps/                         ← customer sub-apps (gitignored)
Orchestrator/venv/            ← Python virtual environment (gitignored)
MCP/venv/                     ← separate venv for the MCP stdio server (gitignored)
.env                          ← API keys + per-machine secrets (gitignored)
config.ini                    ← operators, pairing, tailscale hostname (gitignored)
.onboarding_state.json        ← wizard progress (gitignored)
.onboarding_complete          ← sentinel: wizard done, /ui no longer redirects (gitignored)
```

Everything gitignored is, by design, ignored by `git pull` during updates — so
your snapshots, media, secrets, and onboarding state survive every update.

---

## The snapshot ledger

The BlackBox's defining feature is its memory model. Every assistant
response is appended as a SNAPSHOT to a single immutable file
(`Volumes/SNAPSHOT_VOLUME.txt`), and indexed in
`Manifest/snapshot_index.json` with:

- **byte_start / byte_end** — direct random-access into the volume file
- **embedding[3072]** — `gemini-embedding-001` vector for semantic search
- **operator, timestamp, type** — metadata

Three retrieval modes feed context back into every chat turn:
1. **Recent** — last N turns in this operator's conversation log
2. **Keyword** — string match over the volume
3. **Semantic** — cosine similarity against the 3072-dim embedding index

Snapshots arrive via three paths:
- **Auto-mint** after a configurable turn/token threshold per operator
- **Checkpoint** every 50 turns (configurable), summarizing prior context
- **Manual** via `POST /mint`, `POST /chat/save`, or the Portal's "Checkpoint" button

The system has been validated holding **6,600+ snapshots** in a single
plain-text volume file (~33 MB) without any retrieval slowdown — the
byte-offset index reads in milliseconds, and the embedding search uses
inline numpy cosine over the in-memory vectors.

---

## Chat + model providers

**Frontier streaming chat** with 6 providers; each implements the same
tool-call protocol so the BlackBox's 72 tools work identically across all:

| Provider | Default model | Notes |
|---|---|---|
| Anthropic | `claude-opus-4-7` | Extended thinking, 1M context, strict `display="summarized"` for reasoning |
| OpenAI | `gpt-5.1` | Reasoning blocks, structured output |
| Google | `gemini-3.1-pro-preview` | Thinking blocks, in-prompt cached system context |
| xAI | `grok-4-1-fast-reasoning` | Reasoning visibility |
| Perplexity | `sonar-pro` | Web-grounded answers with inline citations |
| Anthropic Computer Use | `claude-opus-4-6` | Special channel — see [Computer Use](#computer-use). Pinned to 4.6 because the `computer_20251124` tool type currently only ships on that model |

Per-message features:
- **Reasoning visibility** — turn-by-turn thinking blocks rendered in chat
- **File attachments** — drag-drop images, PDFs, audio, video, code; uploaded
  to `Portal/uploads/sessions/{id}/` and exposed to the model via tool calls
- **Per-message provenance** — every assistant response shows which
  snapshots/fossils contributed to the retrieved context
- **Streaming tool execution** — tool calls render mid-stream; the model
  can call multiple tools in parallel within one turn

All 72 tools are defined once in `Orchestrator/tools/tool_registry.py` and
formatted appropriately for each provider's schema (Anthropic
`input_schema`, OpenAI `parameters`, Gemini `parameters`, MCP standard).

---

## Voice agents

Three real-time voice WebSocket endpoints, each persistent across the
session and snapshot-aware:

- `WS /ws/realtime/{session_id}` — **OpenAI gpt-realtime** with 24kHz audio,
  function-calling, server-side VAD
- `WS /ws/gemini-live/{session_id}` — **Gemini 2.5 Flash native-audio
  preview** with on-device audio token streaming
- `WS /ws/grok-live/{session_id}` — **xAI Grok Live** (preview)

All three reuse the chat tool registry, so a voice session can call
`generate_image`, `search_snapshots`, `send_sms`, `make_phone_call`,
`use_computer`, etc. just like text chat.

**Speech I/O outside the live sessions:**
- **TTS** — OpenAI HD (6 voices) + Google Cloud (1,000+ voices) +
  Gemini Pro TTS (30 voices with emotional cues)
- **STT** — OpenAI Whisper-1, accessible via `POST /stt`
- **Per-operator preferences** — preferred TTS voice, model, mic state
  persisted in `config.ini`

---

## Computer Use

Two Computer Use channels, both bridged into the same SSE protocol as chat:

**Anthropic Computer Use** (`claude-opus-4-6`):
- Runs against the customer's real X11 desktop (`DISPLAY=:0`) by default
- Tools: `computer_20251124` (screenshot/click/type/scroll), `bash_20250124`,
  `str_replace_based_edit_tool` (file editor)
- 1280×720 codified as the screen-capture + click-translation resolution
  (the precision sweet spot for the model's training set; CU display autostart
  forces this at every login)
- Real input via **xdotool** (X11) — input events reach native apps
- Screenshots via **scrot** + XDG Desktop Portal as backup

**Gemini Computer Use** (`gemini-2.5-computer-use-preview-10-2025`):
- Same desktop, different model channel (Pro and Flash variants also
  available via `GEMINI_CU_MODEL_PRO` / `_FLASH` env vars)
- Renders at 1440×900 (Gemini-CU's native resolution; differs from
  Anthropic CU's 1280×720)
- Shares the same screenshot + input pipeline as Anthropic CU

**Multi-device** — A `device_id` parameter on the CU endpoints routes input
to any other tailnet machine running the BlackBox `remote_desktop` service
(VNC over Tailscale). One Portal can drive 10 mini-PCs.

**Why 1280×720 (Anthropic) and 1440×900 (Gemini)?** Both CU models drift
heavily when the model thinks the screen is bigger than its training-set
click-target resolution. Capping to each model's native resolution keeps
coordinate predictions accurate on ultrawide hardware (validated on a
3440×1440 customer monitor — drift dropped from 4-5 inches to pinpoint).

---

## CLI Agents

Three terminal-native AI coding agents launchable from the Portal or
Android app:

- **Claude Code** (`@anthropic-ai/claude-code` npm global)
- **Gemini CLI** (`@google/gemini-cli` npm global)
- **Codex** (`@openai/codex` npm global)

Each runs as a real tmux session on the BlackBox; the Portal/Android
client streams the PTY bytes over a WebSocket via the
`Orchestrator/cli_agent/` bridge. Architecturally:

```
Browser / Android Termux view
        │ WebSocket  ws://.../cli-agent/ws/<session>
        ▼
FastAPI handler in cli_agent_routes.py
        │ PtyBridge.spawn(["tmux", "attach", "-t", session])
        ▼
tmux session (per operator + provider + cwd)
        │ runs:
        ▼
/home/<user>/.nvm/versions/node/<ver>/bin/{claude|gemini|codex}
```

**Per-provider details:**
- Claude is auto-switched to its `/tui fullscreen` renderer 5 seconds
  after attach so PgUp/PgDn scroll works inside Termux
- Codex spawns with `--no-alt-screen` so the terminal's normal scrollback
  captures its history
- Gemini's default Ink-based renderer just works

**Sessions persist** across BlackBox service restarts (via
`KillMode=process` in the systemd unit), so disconnecting your Android
device doesn't lose your in-progress conversation.

**MCP integration** — All three CLIs get the BlackBox's MCP server
registered automatically (see next section), so `/mcp` inside any CLI
exposes `search_snapshots`, `get_snapshot`, etc.

---

## MCP server

The BlackBox runs its own **stdio MCP server** (`MCP/blackbox_mcp_server.py`)
and registers it with all three CLI agents at install time. The server
shares the same tool registry as the chat / voice / Computer Use code
paths — so 62 of the BlackBox's 72 tools are available from inside any
CLI session.

Highlights of what claude/gemini/codex can call from inside a tmux session:

| Category | Tools |
|---|---|
| **Snapshot memory** | `search_snapshots`, `get_snapshot`, `mint_snapshot`, `get_context`, `list_recent_snapshots`, `list_operators` |
| **Web** | `web_search` (Perplexity Sonar), `web_fetch` (html2text) |
| **Multimodal generation** | `generate_image` (Gemini Imagen), `generate_video` (Veo 3.1), `generate_music` (Lyria), `text_to_speech` (multiple voice providers) |
| **Communications** | `send_sms`, `make_phone_call`, `gmail_search` / `gmail_read` / `gmail_send` / `gmail_reply` / `gmail_labels` (Gmail OAuth) |
| **Devices + control** | `use_computer`, `list_devices`, `control_android_device`, plus 22 `ugv_*` tools for the optional UGV Beast robot |
| **System** | `get_current_time`, `create_cron_job` / `edit_cron_job` / `search_cron_jobs` for scheduling |

Plus 4 read-only MCP **resources**: `Snapshot Index Statistics`,
`Operator List`, `Recent Snapshots`, `Volume Information`.

Registration happens via each CLI's official `mcp add -s user` subcommand
in `Scripts/install.sh` Step 2b. The MCP server lives in its own venv
(`MCP/venv/`) — separate from the Orchestrator's venv because the `mcp`
package's transitive `starlette>=0.49` conflicts with FastAPI 0.118's
upper bound.

The CLI agents are themselves callable as tools from the Portal's chat —
any frontier model can ask a CLI agent to do focused codebase work, and
the CLI's MCP access lets it pull memory context the parent model didn't
have.

---

## Telephony bridge

`Orchestrator/asterisk/` runs an **Asterisk PBX** with PJSIP trunks for
both VoIP (Twilio) and GSM (TG200 — see below). The phone bridge
(`Orchestrator/phone/bridge.py`) connects the audio stream of an active
call to a Realtime AI:

```
PSTN / VoIP caller
        │ SIP/RTP
        ▼
Asterisk (8000 Hz μ-law/G.722)
        │ AudioSocket protocol → 16kHz PCM16
        ▼
PhoneAIBridge (phone/bridge.py)
        │ swaps providers per call:
        ├──▶ OpenAI gpt-realtime  WebSocket
        ├──▶ Gemini Live          WebSocket
        └──▶ xAI Grok Live        WebSocket
```

The AI talks back through the same socket. Inbound calls trigger
configurable IVR flows (`Orchestrator/asterisk/ivr.py`); outbound calls
are placed via `make_phone_call` tool with destination + first prompt.

Same tool registry as chat — the AI on the phone can search snapshots,
schedule cron jobs, send SMS, generate images, etc. Conversations are
minted as snapshots like any other.

---

## TG200 cellular gateway

The **TG200** is a hardware SIP-to-GSM gateway (Yeastar/OpenVox-class)
that plugs into your local network and exposes 2 GSM/4G SIM slots as
SIP trunks. The BlackBox `Orchestrator/asterisk/gateway_manager.py`:

- **Auto-discovers** TG200 units on the LAN via SIP-port scan
- **Persists** them to `Orchestrator/asterisk/gateways.json`
- **Queries** SIM status, signal strength, IMEI via the TG200's HTTP API
- **Generates** PJSIP trunk config so Asterisk can route calls through them
- **Sends SMS** via the TG200's `/cgi/WebCGI?1500103` HTTP endpoint

Default install ships one gateway entry in `gateways.json` pointed at
`192.168.1.200:5060` with HTTP `admin/password` — edit it to match your
actual unit's IP and credentials. The auto-discovery scanner separately
probes `192.168.5.150` (TG200's factory default), so units at either
address are found.

**Why TG200 over Twilio for outbound voice?** Cost (per-minute is
fractions vs. Twilio's per-minute), call quality (G.722 wideband vs.
Twilio's narrowband), and operating off the public internet (the AI
voice never leaves your tailnet for outbound calls placed via SIM).

---

## SIM7600G-H cellular modem

`Orchestrator/cellular/` drives a **SIM7600G-H USB modem** for two
purposes:

1. **Failover internet** — `internet_manager.py` enables the modem's
   built-in PPP+RNDIS connection when the Wi-Fi/Ethernet link drops
2. **Second voice channel** — `audio_stream.py` bridges 8 kHz PCM16
   audio between the modem's serial-AT audio interface and Asterisk for
   GSM calls that don't go through the TG200 (used when the TG200 is
   offline, or for backup mission-critical voice during a LAN outage)

**Hot-plug detection** (`cellular/hotplug.py`) polls every 3 seconds for
ttyUSB cluster appearance — the SIM7600 enumerates 5 ttyUSB ports but
doesn't guarantee consecutive numbering, so the detection is
cluster-based (4+ ports within range of 6, not strict consecutive).

Tested with US prepaid carriers (T-Mobile, AT&T MVNO). Modem must
have ModemManager **disabled** to avoid serial-port contention with the
audio bridge.

---

## Android Portal app

`AI_BlackBox_Portal_Android_MVP/` is a native Kotlin app that:

- **Pairs to the BlackBox** via QR code (the wizard's `pair_phone` step
  mints a token; Android scans it, calls `/pair/claim`, gets a persistent
  device record)
- **Reuses your operator identity** so chat history continues seamlessly
  across desktop ↔ phone
- **Hosts the full Portal in a WebView** with native bridges for: voice
  recording, file uploads, mic permissions, persistent notifications
- **Runs CLI agent terminals** via Termux's `TerminalView` widget +
  WebSocket → `cli-agent/ws/...` (so your `claude /init` session keeps
  going whether you're at your desk or on the bus)
- **Background services** for: keep-alive (so notifications + WebSockets
  survive Doze), media downloads, foreground voice recording
- **Foldable + XR support** — a dual-path UI rendering correctly on
  phones, foldables, and Android XR Home Space (Quest 3 / Vision Pro
  Android port). XR uses Compose-based bubble + expanded-panel views
  via the `XrOverlayActivity`

Communicates over Tailscale, so as long as both devices are on the same
tailnet, the Portal is reachable as `https://<tailnet-hostname>`.

---

## Apps platform

The BlackBox is also a **vibe-coding host** — any local web app can be
registered and the Portal will reverse-proxy it under `/app-proxy/<port>/`.
Use cases:

- Your customer's own internal tools written in any framework
- AI-generated mini-apps (the chat models can write + start a server +
  register the app with one tool call)
- Bundled examples (system monitor, generation showcases, etc.)

```
# From any chat session
"Build me a grocery list tracker as a single-page web app"
→ AI writes Apps/grocery-store/{index.html,server.py}
→ AI starts python3 Apps/grocery-store/server.py on port 8067
→ AI POSTs to /agent/apps/register with name+port+directory+operator
→ App now appears in Portal's System Menu under Running Apps
```

Customer-created apps live under `Apps/<name>/` which is gitignored — so
updates from GitHub never stomp on them (audit C1 in the update
pipeline).

---

## Scheduler

`Orchestrator/scheduler/` is an APScheduler-backed cron with SQLite
persistence. Schedule any model (Claude, GPT, Gemini, ...) to run a
prompt on cadence and deliver the result via:

- Chat (appended to your conversation log)
- SMS (via the TG200 GSM gateway)
- Email (Gmail OAuth)
- Phone call (`make_phone_call` tool)

Cron jobs survive service restarts and missed-fire catch-up is configurable.
Manage from the Portal's System Menu → Scheduler panel.

---

## Onboarding wizard

`Portal/onboarding/` is a fresh-install setup flow that runs on first
boot (and is reachable later via System Menu → Manage Setup). Eight steps:

1. **Welcome** — overview + system requirements
2. **Tailscale** — one-click install, auth, HTTPS cert request, MagicDNS check
3. **API keys** — validate keys for Anthropic / OpenAI / Google / xAI /
   Perplexity / Gmail OAuth (each does a single cheap API call to confirm)
4. **Optional integrations** — Gmail OAuth client setup, JSON service account
5. **Pair phone** — QR-code pairing with the Android app
6. **CLI agents** — install + interactive sign-in for claude/gemini/codex
   (each opens a terminal for OAuth)
7. **Operator** — name + voice + per-operator preferences
8. **Done** — summary + "Open Portal" + restart service + view logs +
   update-available badge

The wizard runs in a **Tauri-wrapped webview** during first-run install
(a normal windowed launcher — not a full-screen kiosk, which was tried
and dropped in favor of standard window decorations + customer control).
After onboarding completes, the main Portal opens in the user's default
browser; the wizard is reachable later via System Menu → Manage Setup.

---

## Update pipeline

The BlackBox keeps itself current by pulling from GitHub at customer
demand. Click **System Menu → Updates → Install update** and the
backend:

1. Acquires an `fcntl.flock` mutex on `Manifest/update.lock`
2. Tags the current commit as `pre-update-<unix-ts>` for rollback
3. Captures `pip freeze` of both venvs to `Manifest/pre_update_*_freeze.txt`
4. Fetches origin/main, categorizes changed files into buckets
   (apt, pip, mcp_pip, sudoers, helpers, systemd, code_only)
5. Atomic `git reset --hard origin/main` (.gitignore protects user data)
6. Re-runs each bucket's idempotent install.sh block as needed
7. Schedules detached `systemctl restart blackbox.service` via
   `asyncio.call_later(2.0, ...)` so the SSE "complete" event flushes
   to the browser before the service dies
8. Browser polls `/health` for up to 180s with progressive copy
   ("Restarting…" → "Rebuilding snapshot index (60-90s)…" → "Still warming…")
9. On `/health 200`: success toast + panel refresh

**What's preserved** (untouched by `git reset --hard`):
- `Volumes/`, `Manifest/`, `Fossils/` — every snapshot + embedding
- `Portal/uploads/` — every uploaded + generated media file
- `.env`, `config.ini` — every secret + per-operator preference
- `Orchestrator/device_registry/devices.json` — paired tailnet devices
- `Manifest/paired_devices.json` — paired Android devices
- `Apps/` — customer-created sub-apps
- `Orchestrator/venv/`, `MCP/venv/` — Python environments (rebuilt only if requirements.txt changed)
- `.onboarding_state.json`, `.onboarding_complete` — wizard state

**Security perimeter** — `apt` install + sudoers + systemd writes go
through two bounded helpers at `/usr/local/sbin/`:
- `blackbox-apt-install` — validates package name against the
  `system-packages.txt` allowlist before any `apt-get install` runs
- `blackbox-write-systemd` — accepts a whitelisted `target_kind`
  (`unit | override | cli-agent-overrides | sudoers-system`),
  hardcodes the destination path, runs `visudo -c` on sudoers writes
  before installing

A prompt-injection RCE through any AI tool can ONLY call these helpers
via the bounded NOPASSWD sudoers grant — and the helpers themselves are
the security policy.

**Manual update from SSH**: `./Scripts/update.sh` (interactive Y/N) or
`./Scripts/update.sh --yes` (CI/scripted).

---

## Configuration reference

**API keys** live in `$BLACKBOX_ROOT/.env`. Bootstrap from `.env.template`
(install.sh does this automatically). The wizard's API Keys step writes
them with masking + per-provider validation.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude chat + Computer Use |
| `OPENAI_API_KEY` | GPT chat + Realtime voice + TTS + Whisper |
| `GOOGLE_API_KEY` | Gemini chat + Live voice + Imagen + Veo + Lyria + Cloud TTS |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Gmail OAuth |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google Cloud service account JSON (for Veo + Lyria) |
| `XAI_API_KEY` | Grok chat + Live voice |
| `PERPLEXITY_API_KEY` | Sonar web-grounded chat |
| `TWILIO_*` | Optional Twilio voice/SMS |
| `BLACKBOX_ROOT` | Install path (auto-set by install.sh) |

**Per-customer state** lives in `config.ini` (also gitignored):

```ini
[users]
default = Brandon
list = Brandon,Anna,bbx1

[pairing]
tailnet_hostname = bbx-ms-02-ultra.tailABCDEF.ts.net

[checkpoint]
turns_to_compress = 50
auto_create_interval = 50
min_snapshots_required = 5

[audio]
model = tts-1
```

**Service tuning** via systemd drop-in at
`/etc/systemd/system/blackbox.service.d/override.conf`:

```ini
[Service]
# Custom port
# ExecStart=/path/to/venv/bin/python -m uvicorn Orchestrator.app:app --host 0.0.0.0 --port 8000

# Memory cap
# MemoryHigh=50%

# CPU priority
# Nice=-5
```

---

## Operating the BlackBox

**Service control:**

```bash
sudo systemctl restart blackbox.service
sudo systemctl status blackbox.service
sudo journalctl -u blackbox.service -f       # follow live logs
./blackbox-status.sh                          # quick health snapshot
```

**Updates:**
- From Portal: System Menu → Updates → Install update
- From SSH: `./Scripts/update.sh`
- Rollback: System Menu → Updates → Rollback (or `git reset --hard pre-update-<ts>`)

**Adding apps:**
```bash
curl -X POST http://localhost:9091/agent/apps/register \
  -H "Content-Type: application/json" \
  -d '{"name":"My App","port":8067,"directory":"/path","operator":"system"}'
```

**Switching operators** — Portal System Menu → Operator selector. Each
operator has independent conversation log, embedding context, and
preferences.

**Pairing a new Android device** — Portal System Menu → Pair Device. On
the Android side, install AI BlackBox, tap "Pair", scan QR.

---

## Troubleshooting

**Service won't start:**
```bash
sudo journalctl -u blackbox.service -n 100 --no-pager
# Look for: pip version conflicts, missing system packages, .env parse errors
```

**Portal renders but no chat response:**
- Check API keys in System Menu → Manage Setup → API Keys (each validates)
- Check `.env` is mode 0600 owned by your user
- Check the model provider's status page

**CLI agent terminal blank:**
- Verify the helper drop-in: `systemctl show blackbox.service --property=ReadWritePaths,PrivateTmp,KillMode`
- Should show `~/.claude /.gemini /.codex /.config /.cache /.npm /tmp` in
  ReadWritePaths, `PrivateTmp=no`, `KillMode=process`
- If not: re-run `sudo ./Scripts/install.sh`

**Computer Use clicks miss by inches:**
- Display is not at 1280×720. Check `xrandr --current`
- Fix: re-run install.sh (Step 6c installs the autostart .desktop) and
  log out + back in

**Tmux "Address already in use" after testing:**
```bash
tmux kill-server
rm -f /tmp/tmux-1000/default
sudo systemctl restart blackbox.service
```

**Update fails midway:**
```bash
# Check what happened
curl -s http://localhost:9091/update/state | jq
# Manual rollback to last pre-update tag
git -C $BLACKBOX_ROOT log --oneline --tags | grep pre-update- | head -3
git -C $BLACKBOX_ROOT reset --hard pre-update-<ts>
sudo systemctl restart blackbox.service
```

**SIM7600 audio not flowing:**
```bash
sudo systemctl disable ModemManager
sudo systemctl stop ModemManager
sudo systemctl restart blackbox.service
```

---

## Repository layout

```
blackbox-poc/
├── Orchestrator/            # FastAPI backend (Python)
│   ├── app.py               # Main entry point — registers all routes
│   ├── routes/              # 28 route modules (chat, voice, cu, cli-agent, ...)
│   ├── tools/               # 72-tool registry consumed by all providers
│   ├── update/              # In-place update pipeline (T6 modules)
│   ├── onboarding/          # Wizard backend + validators
│   ├── cli_agent/           # tmux PTY bridge + session manager
│   ├── browser/             # Computer Use screenshot + input + Chrome
│   ├── asterisk/            # Telephony bridge + TG200 gateway manager
│   ├── cellular/            # SIM7600 modem driver + audio stream
│   ├── phone/               # Realtime AI ↔ phone audio bridge
│   ├── scheduler/           # APScheduler cron + delivery
│   ├── gmail/               # Gmail OAuth + send/receive
│   ├── tests/               # pytest suite (85+ tests across modules)
│   └── utils/paths.py       # BLACKBOX_ROOT resolution
│
├── Portal/                  # Web frontend (vanilla JS + ES modules)
│   ├── index.html           # Single-page Portal
│   ├── modules/             # Per-feature JS modules (chat, voice, devices, ...)
│   ├── styles/              # Modular CSS (main.css imports features + generation)
│   ├── onboarding/          # 8-step wizard (separate from main Portal)
│   └── uploads/             # User-uploaded + AI-generated media
│
├── AI_BlackBox_Portal_Android_MVP/  # Native Kotlin Android app
│   └── app/src/main/        # PortalActivity (WebView host) + CLI terminal + voice
│
├── MCP/                     # Standalone MCP server (separate venv)
│   ├── blackbox_mcp_server.py   # Stdio server exposed to claude/gemini/codex
│   └── requirements.txt     # mcp + httpx + requests + beautifulsoup4
│
├── installer/               # Tauri 2.x wizard wrapper (Rust + WebKitGTK
│                            # with workarounds for select-rendering + dark bg)
│   ├── src-tauri/           # Rust app shell
│   └── templates/           # Sudoers + autostart .desktop + helper scripts
│
├── Scripts/                 # Operational shell scripts
│   ├── install.sh           # Idempotent installer (26 step blocks)
│   ├── update.sh            # Manual / SSH-rescue update path
│   ├── blackbox-status.sh   # Quick health snapshot
│   └── onboarding/system-packages.txt   # MUST_HAVE + SHOULD_HAVE apt allowlist
│
├── Volumes/                 # Snapshot volume (gitignored)
├── Manifest/                # Index + paired devices + update state (gitignored)
├── Fossils/                 # Archived snapshots (gitignored)
├── Apps/                    # Customer sub-apps (gitignored except .gitkeep + README)
├── docs/                    # Plans, audit notes, architecture writeups
└── tests/                   # Top-level integration tests
```

---

## Status and roadmap

**Current state (May 2026):** v1 customer-facing onboarding + update pipeline
shipped. Validated end-to-end on MSO2 Ultra customer-class hardware.

**Active development tracks:**
- Hardware product packaging (mini-PC + branded chassis + USB peripherals)
- Wireless charging dock for paired devices
- Asterisk multi-tenant operator isolation

**Optional paired hardware** (shipped, available when the corresponding
device is on the same tailnet):
- **UGV Beast (Waveshare tracked robot)** — 22 `ugv_*` tools exposed
  through chat + MCP (`ugv_camera_list`, `ugv_motion_move_forward`,
  `ugv_nav_goto_point`, `ugv_start_mission`, etc.). Pairs with Gemini
  Robotics-ER 1.6 running on the robot's Jetson for autonomous missions.

**Roadmap:**
- Stage 2 onboarding refinements: per-machine secrets storage, multi-tenant operator separation
- Hardware-attested attestation (TPM-based BlackBox identity)
- Update pipeline v2: APScheduler-managed update jobs, GitHub release-notes integration
- Auto-update opt-in for unattended deployments

---

## License

**Proprietary — All Rights Reserved.** See [`LICENSE`](./LICENSE) for
full terms.

This source is published for transparency, auditability, and the
portability of the AI BlackBox hardware product. It is **not** open
source. Personal viewing, security audit, and end-user evaluation are
permitted. Reproduction, redistribution, commercial use, SaaS hosting,
or reselling — in source or compiled form — requires prior written
permission from TechBran.

For commercial licensing or partnership inquiries, open an issue
titled "Licensing Inquiry" on this repository.

---

## Contact + Support

- GitHub: https://github.com/TechBran/ai-blackbox-flight-recorder-agent
- Issues: file at the repo's Issues tab
- Operator: Brandon (TechBran)

Built with `claude-opus-4-7`, `gemini-3.1-pro-preview`, `gpt-5.1`, and
several thousand snapshots of accumulated context.
