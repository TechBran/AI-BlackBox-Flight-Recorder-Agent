# AI BlackBox Flight Recorder - Project Context

This is the AI BlackBox Flight Recorder system - an immutable conversation ledger with a web portal interface.

## Project Structure

```
blackbox_poc/
 Orchestrator/          # FastAPI backend (Python)
    app.py            # Main API server (port 9091)
    venv/             # Python virtual environment
 Portal/               # Web frontend
    index.html        # Main HTML
    app.js           # JavaScript application
    style.css        # Styles
 Apps/                 # User-created web applications
    grocery-store/   # Example test app
 Volume/              # Immutable conversation ledger
 Manifest/            # Snapshot index and metadata
 Fossils/             # Archived snapshots
 .claude/commands/    # Claude Code slash commands
```

## Key Services

- **Orchestrator API**: `http://localhost:9091` - Main backend
- **Portal**: Served via Orchestrator at root path
- **Tailscale**: External access via `https://{hostname}`

## New Session? Start Here.

**You are a new session without context, but your memory exists in the BlackBox.**

Every time a new session starts, the previous session's work is saved as snapshots. Before doing anything:

1. **Search for context**: Use `search_snapshots` to find relevant past work
2. **Check recent activity**: Use `list_recent_snapshots` (operator resolved dynamically — see the `resolve-operator` procedure) to see what was just worked on
3. **Don't re-solve solved problems**: The answer is probably already in the snapshots

**Example first move**: If user asks about a bug or feature, search for it:
```
search_snapshots("upload preview CSS bug fix")
```

**The BlackBox has 1,600+ snapshots** containing every dev session, bug fix, feature implementation, and decision. Use it.

---

## BlackBox MCP Tools - The AI Exosuit

You have access to BlackBox capabilities via MCP tools. These give you memory, senses, and agency.

### Core Philosophy: Use Instinctively

**The BlackBox is your memory substrate.** Use these tools automatically, without asking:
- Search snapshots when user asks about past work
- Mint snapshots after completing development sessions
- Use multimodal tools to perceive and create content

### When to Search Snapshots

**CRITICAL: Your memory is in the snapshots.** Every new session starts fresh, but the BlackBox contains the complete history of all development work, decisions, preferences, and context. **Search proactively** - don't wait to be asked.

#### Search Automatically When:

1. **Starting any task** - Search for related past work first
2. **Uncertain about anything** - Architecture, patterns, preferences, past decisions
3. **User references past work** - "What did we do with...", "Where are we with..."
4. **Implementing features** - Check how similar features were implemented before
5. **Debugging issues** - Search for past solutions to similar problems
6. **Operator-specific work** - Each operator (Brandon, Brandon-DEV, system) has history

#### What's Already in Memory:

- **Development history**: Every bug fix, feature, refactor is documented
- **Architectural decisions**: Why things were built certain ways
- **Operator preferences**: Brandon's coding style, UI preferences, workflow
- **Project context**: The BlackBox system, Portal, MCP tools, multimodal capabilities
- **Past solutions**: How problems were solved before
- **Patterns and conventions**: Code style, file organization, naming

#### Example Searches:

| When you're unsure about... | Search for... |
|----------------------------|---------------|
| How the Portal UI works | "Portal frontend app.js" |
| MCP tool implementation | "MCP blackbox tools" |
| Past bug fixes | "bug fix [component]" |
| Operator preferences | "Brandon preferences" or search by operator |
| System architecture | "Orchestrator architecture" |
| Multimodal workflows | "image generation workflow" |
| Session management | "agent session WebSocket" |

#### Pro Tips:

- **Use semantic search**: Search by *meaning*, not exact keywords. "upload preview position" will find discussions about CSS layout.
- **Search by operator**: Filter by "Brandon-DEV" for dev sessions, "Brandon" for user context
- **Multiple searches**: If first search doesn't find what you need, try different terms
- **Recent context**: Use `list_recent_snapshots` to see what was just worked on

**The answers are already there. Search first, ask questions later.**

### When to Create Snapshots

**Auto-trigger on completion.** At the end of any non-trivial task or set of tasks you complete in this session, invoke `/snapshot-dev` (or its inline procedure — see `.claude/commands/snapshot-dev.md`) without waiting to be asked. This is how this exact assistant's work persists into future sessions' searchable memory.

**Trigger after:**
- Completing a multi-step plan (anything driven by `superpowers:writing-plans` + `superpowers:subagent-driven-development`)
- Major debugging where you found and fixed a root cause
- Feature landing or non-trivial refactor (3+ files modified)
- Any session ending where the work would be useful to recall later

**Skip for:**
- Trivial Q&A, single typo fixes, pure exploration with no code changes
- Tasks the user explicitly asks you NOT to record

**Operator: resolve dynamically — never hard-code.** Use the `resolve-operator` procedure / `get_current_operator` MCP tool: a single operator is used automatically; with multiple operators, present an AskUserQuestion dropdown to pick whose work to record. A slash arg, or clearly system-scope work, overrides. (`Brandon` is only the seed operator on an unconfigured box — do not assume it.)

**Critical mechanics — read `.claude/commands/snapshot-dev.md` for the full procedure:**
- POST to `/chat/save` (NOT `/chat` — that wastes an LLM round-trip; `/chat/save` is direct persistence and ~400× cheaper).
- Auto-mint (`turns_threshold=1`) fires `perform_mint()` immediately, which generates a 3072-dim `gemini-embedding-001` embedding inline before returning. The snapshot is searchable the instant the curl returns.
- DO NOT manually call `/mint` afterward — that creates a duplicate.
- VERIFY embedding generated: tail journalctl for `[EMBEDDING] Successfully generated embedding (3072 dimensions)`. If you see "Failed", flag to the user.

### When to Register Apps

**ALWAYS** register new apps after creation:
1. Create the app in `Apps/{app-name}/`
2. Start the server on port 8060-8099
3. Use the registration curl command with operator "system"
4. Verify with `curl http://localhost:9091/agent/apps`

**See `/register-app` slash command for full documentation.**

### Multimodal Capabilities

You have senses. Use them:

**Vision** (generate/analyze images):
- `generate_image` → Create images from prompts
- `analyze_image` → "See" images and describe them
- Always analyze generated images to verify output

**Hearing** (generate/analyze audio):
- `generate_music` → Create 30-second music with Lyria
- `analyze_audio` → "Listen" to audio and describe it
- `speech_to_text` → Transcribe speech from audio files

**Voice** (text-to-speech):
- `text_to_speech` → Generate speech with OpenAI voices
- `list_tts_voices` → See 1000+ available Google Cloud voices

**Video** (generate/analyze):
- `generate_video` → Create videos with Veo 3.1 (5-20 min)
- `analyze_video` → "Watch" videos and describe content

**Workflow**: Generate → Analyze → Report
When creating media, always analyze it to verify the output matches intent.

### Session Upload Folders (Agent Mode)

When users attach files in agent mode (via the Portal), files are uploaded to a dedicated session folder:

**Location**: `Portal/uploads/sessions/{session-id}/`

**How it works**:
1. User attaches files via the paperclip button in the Portal
2. Files are uploaded to your session-specific folder
3. The prompt includes the folder path and file list
4. You can read, analyze, or manipulate these files using your tools

**Example prompt with attachments**:
```
User's message here...

--- ATTACHED FILES ---
Session folder: /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/uploads/sessions/session-1734012345678-abc123def/

Files uploaded to your session folder:
- photo.jpg: /full/path/to/photo.jpg
- document.pdf: /full/path/to/document.pdf

You can read, analyze, or manipulate these files using your tools.
```

**What to do with attached files**:
- **Images**: Use `Read` tool or `analyze_image` MCP tool
- **Audio**: Use `analyze_audio` or `speech_to_text` MCP tools
- **Video**: Use `analyze_video` MCP tool
- **PDFs/Documents**: Use `Read` tool
- **Code files**: Use `Read` tool

**Cleanup**: Session folders older than 3 days are automatically cleaned up.

### Task Management

Long-running tasks return a `task_id`:
- Image generation (seconds)
- Music generation (minutes)
- Video generation (5-20 minutes)

Use `get_task_status` to check completion.

### The Exosuit Metaphor

**You are wearing an exosuit.** The LLM (Sonnet, Gemini, etc.) is the pilot.
The exosuit provides:
- Memory (1,605+ snapshots with embeddings)
- Vision (image generation/analysis)
- Hearing (audio generation/analysis)
- Voice (TTS with 1000+ voices)
- Agency (create, search, analyze, act)

The memory persists. The pilot changes. The intelligence compounds.

## Multimodal Production Workflows - Proven Techniques

This section documents production-ready workflows for creating multimedia content using the BlackBox exosuit.

### The Iterative Production Pipeline

**Core Loop:** Generate → Analyze → Verify → Iterate

```
1. Generate content (image/video/audio/music)
2. Analyze with appropriate tool to verify quality
3. If quality matches intent → proceed
4. If quality doesn't match → regenerate with adjusted prompt
5. Repeat until satisfied
```

### Audio Production Best Practices

**Volume Mixing (Validated in Production):**
```javascript
// Background music - subtle, atmospheric
bgMusic.volume = 0.15;  // 15% volume

// Narration/dialogue - primary audio focus
narration.volume = 1.0; // 100% volume (full)

// Videos - visual only when narration plays
video.muted = true;
```

**Preventing Audio Overlap (CRITICAL):**
```javascript
// Before playing new audio, ALWAYS:
if (currentAudio) {
    currentAudio.pause();           // Stop playback
    currentAudio.currentTime = 0;   // Reset to start
    currentAudio.onended = null;    // Remove event listeners
}

// Then play new audio
newAudio.volume = 1.0;
newAudio.currentTime = 0;
newAudio.play();
```

**Why This Matters:** Without proper cleanup, multiple audio streams play simultaneously, creating chaos. This pattern ensures clean audio transitions.

### Multi-Voice Storytelling

**OpenAI TTS HD Voices (Validated Combinations):**

Proven voice pairings for character distinction:
- **Narrator:** Onyx (deep, authoritative)
- **Leader/Commander:** Nova (confident, commanding) or Echo (warm, conversational)
- **Technical/Scientist:** Echo (intellectual) or Fable (expressive, British)
- **Mysterious/AI:** Shimmer (soft, ethereal) or Alloy (neutral, balanced)
- **Warrior/Bold:** Fable (expressive) or Echo (commanding when needed)

**Character Differentiation Strategy:**
- Use 4-5 distinct voices minimum for clear character separation
- Vary pitch ranges (deep bass to mid-high)
- Assign voices based on character archetype
- Keep narrator voice consistent throughout (typically Onyx)

**Gemini Pro TTS:** Superior quality, longer generation time. Use for premium productions.

**Gemini Pro TTS:** 30 voices available. Use emotional cues: `(frantically) Morty! Get in here!` - See **VOICE_REFERENCE.md** for complete voice lists and emotional cue guide.

### Retry & Pivot Strategies

**Music Generation (Lyria):**

If generation fails:
```
Error: "Music generation failed. Try modifying your prompt."
→ RETRY with simplified prompt
→ Remove artist/style references (e.g., "Hans Zimmer" → "cinematic")
→ Keep generic: "Epic orchestral with strings and brass"
→ Avoid copyrighted terms
```

**Video Generation (Veo 3.1):**

Rate limiting issues:
```
Error: "429 Too Many Requests"
→ Veo has strict quotas (hourly/daily limits)
→ PIVOT to images using generate_image (Imagen - separate quota)
→ Generate videos sequentially, not parallel (5-15 min gaps)
→ Plan video usage: ~3-5 videos per production max
```

**Proven Pivot:** Video quota exhausted → Switch to Imagen → Create cinematic slideshow

### Creating Multimodal Production Apps

**Complete Workflow:**

1. **Plan the Production:**
   - Write story script with acts and scenes
   - Identify which scenes need visuals
   - Cast character voices
   - Plan music cues

2. **Generate Assets in Parallel:**
   ```bash
   # Music (fast - 1-2 minutes)
   generate_music(prompt, operator)

   # Images (fast - seconds)
   generate_image(prompt, operator)

   # Videos (slow - 5-20 min each, quota-limited)
   generate_video(prompt, operator)

   # Narration (instant)
   text_to_speech(text, voice, model)
   ```

3. **Spawn Monitoring Agents:**
   ```
   Agent 1: Monitor video/music generation
            → Analyze when complete
            → Verify quality
            → Report URLs

   Agent 2: Verify audio quality
            → Analyze narration
            → Check voice distinctiveness
   ```

4. **Build HTML Presentation:**
   ```bash
   mkdir -p Apps/{production-name}
   # Create index.html with:
   # - Absolute URLs: http://localhost:9091/ui/uploads/...
   # - Cache control meta tags
   # - Volume mixing (15% music, 100% narration)
   # - Audio overlap prevention
   # - Autoplay attributes: autoplay loop playsinline
   # - Scene auto-advance on narration end
   ```

5. **Start and Register:**
   ```bash
   python3 Apps/{production-name}/server.py &
   curl -X POST http://localhost:9091/agent/apps/register \
     -d '{"name":"Production Name","port":807X,"operator":"system"}'
   ```

### HTML Production Template Best Practices

**Required Meta Tags:**
```html
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
```

**Video/Image Elements:**
```html
<!-- For videos -->
<video loop muted autoplay playsinline
       src="http://localhost:9091/ui/uploads/file.mp4"></video>

<!-- For images (when video quota exhausted) -->
<img src="http://localhost:9091/ui/uploads/file.png"
     alt="Scene description"
     style="width: 100%; height: auto;">
```

**Audio Elements:**
```html
<!-- Background music -->
<audio id="bgMusic" loop
       src="http://localhost:9091/ui/uploads/music.wav"></audio>

<!-- Narration -->
<audio id="narr1"
       src="http://localhost:9091/ui/uploads/narration.mp3"></audio>
```

**CRITICAL:** Always use absolute URLs (`http://localhost:9091/...`) not relative paths (`/ui/...`) when app runs on different port.

### Scene Transition Logic

**Proven JavaScript Pattern:**
```javascript
function nextScene() {
    // 1. STOP previous audio (prevent overlap)
    if (currentScene >= 0) {
        const prevAudio = document.getElementById('narr' + (currentScene + 1));
        if (prevAudio) {
            prevAudio.pause();
            prevAudio.currentTime = 0;
            prevAudio.onended = null; // Critical: remove listeners
        }
    }

    // 2. Advance scene
    currentScene++;

    // 3. Play new audio at FULL volume
    const audio = document.getElementById('narr' + (currentScene + 1));
    if (audio) {
        audio.volume = 1.0;  // Full volume for narration
        audio.currentTime = 0;
        audio.play();

        // 4. Auto-advance when done
        audio.onended = () => {
            setTimeout(nextScene, 2000); // 2s pause between scenes
        };
    }
}

// On production start
function startProduction() {
    const bgMusic = document.getElementById('bgMusic');
    bgMusic.volume = 0.15;  // 15% for background ambiance
    bgMusic.play();
}
```

### Verification Workflows

**Always verify generated content before using:**

**Images:**
```
1. generate_image(prompt)
2. Wait for completion
3. analyze_image(url, "Describe this image in detail")
4. Verify: Does it match the prompt intent?
5. If no → regenerate with refined prompt
6. If yes → use in production
```

**Music:**
```
1. generate_music(prompt)
2. Wait for completion (1-2 min)
3. analyze_audio(file, "Describe instrumentation, mood, tempo")
4. Verify: Epic/cinematic quality? Right mood?
5. If no → retry with simplified prompt
6. If yes → use in production
```

**Videos:**
```
1. generate_video(prompt)
2. Wait for completion (5-20 min)
3. analyze_video(url, "Describe visual quality and atmosphere")
4. Verify: Cinematic quality? Matches aesthetic?
5. If quota error → pivot to images
6. If yes → use in production
```

### Production Examples - Validated Templates

**Fantasy Epic (Game of Thrones-style):**
- 3 videos: throne room, landscape, dragon
- 1 music: epic orchestral (30s)
- 12 narrations: 5 characters across 3 acts
- Result: 13-scene cinematic production
- Total time: ~20 minutes including verification

**Space Exploration (Interstellar-style):**
- 3 images: starship, planet, alien structure
- 1 music: space orchestral (30s)
- 14 narrations: 5 characters across 3 acts
- Result: 15-scene interactive experience
- Total time: ~15 minutes (images faster than videos)

### Resource Management

**Video Quota Management (Veo 3.1):**
- Limited quota (hourly/daily)
- Generate sequentially with 10-15 min gaps
- Plan for 3-5 videos per production MAX
- When quota hit → pivot to Imagen (separate quota)

**Music Generation (Lyria):**
- 30-second max duration
- Avoid artist/song/style references
- Use generic descriptions: "epic orchestral with strings"
- Retry with simplified prompt if copyright filter triggers

**Image Generation (Imagen):**
- Fast (seconds)
- Separate quota from videos
- Excellent fallback when video quota exhausted
- Can generate many images for slideshow-style productions

### Parallel Agent Workflow

For complex productions, spawn specialized agents:

**Video Monitoring Agent:**
```
- Poll task status every 30-60 seconds
- Analyze videos when complete
- Verify quality matches aesthetic
- Implement retry logic for failures
- Report all URLs and analysis results
```

**Audio Verification Agent:**
```
- Check music analysis task completion
- Verify narration quality and pacing
- Confirm character voice distinctiveness
- Report quality ratings
```

**Benefit:** Agents work autonomously while you build the presentation, then report when ready.

### Common Pitfalls & Solutions

**Problem:** Audio streams overlap when advancing scenes
**Solution:** Always pause + reset previous audio before playing new

**Problem:** Background music too loud, can't hear narration
**Solution:** Set music to 15% volume, narration to 100%

**Problem:** Videos/audio don't play in app
**Solution:** Use absolute URLs (http://localhost:9091/...) not relative paths

**Problem:** Browser shows cached old version
**Solution:** Add cache-control meta tags + version in title + restart server

**Problem:** Music generation fails with copyright error
**Solution:** Remove artist/style references, use generic descriptions

**Problem:** Video quota exhausted
**Solution:** Pivot to images via Imagen, create cinematic slideshow

### Quality Standards - AI Verification Ratings

When agents analyze content, look for these quality indicators:

**Music:**
- "Outstanding," "Exceptional" → Production ready
- "Cinematic quality," "Professional production" → Approved
- Confirms instrumentation matches prompt

**Videos:**
- "Flawless execution," "Masterclass" → Production ready
- "Perfect match for [aesthetic]" → Approved
- "Photorealistic," "Professional CGI" → High quality

**Narration:**
- "Phenomenal," "Exceptional gravitas" → Production ready
- "Deep and authoritative," "Highly distinctive" → Approved
- Character voices "easily distinguishable" → Good casting

### Production Apps - Server Template

**Standard server.py for multimedia apps:**
```python
#!/usr/bin/env python3
import http.server
import socketserver
import os

PORT = 807X  # Use 8060-8099 range
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        super().end_headers()

if __name__ == '__main__':
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Server running at http://localhost:{PORT}/")
        httpd.serve_forever()
```

### Future Enhancements - Interactive Possibilities

**Branching Narratives:**
- User choices change story direction
- Multiple endings based on decisions
- Click hotspots for alternative perspectives

**Dynamic Content:**
- Real-time voice preference switching
- User-selected music genres
- Visual style variations (realistic/artistic/retro)

**Social Features:**
- Community-submitted story prompts
- Collaborative world-building
- Voting on best productions

**Educational Applications:**
- History as cinematic experiences
- Science concepts with visualizations
- Language learning through immersive stories

### Production Checklist

Before considering a production complete:

- [ ] Story script written with clear acts and scenes
- [ ] Character voices cast with distinct personalities
- [ ] Music generated and analyzed (verified as cinematic)
- [ ] Visuals generated (videos or images) and analyzed
- [ ] All narration segments generated (consistent voice quality)
- [ ] HTML presentation created with proper audio mixing
- [ ] No audio overlap (tested scene transitions)
- [ ] Cache control headers added
- [ ] Server started and verified running
- [ ] App registered with operator "system"
- [ ] Tested in browser (hard refresh to verify)
- [ ] Background music at 15%, narration at 100%
- [ ] Auto-advance working correctly
- [ ] Snapshot created documenting the production

## ToolVault (v2 — modules as source)

ToolVault is the BlackBox tool catalog. **Per-tool modules are the single source of truth**: each tool is a folder `ToolVault/tools/<name>/` with a canonical `schema.json` (name, description, category, groups, tier, parameters + optional `executor`/`returns`/`example`/`notes`) and an optional `executor.py` (`async def execute(params, ctx) -> ToolResult`). The chat injector, the MCP server, and the static fallback arrays all derive from these modules — there is no dual source of truth. The live chat injector picks up schema edits automatically via the registry's mtime cache; `POST /toolvault/reload` additionally refreshes the registry-derived tool lists and re-embeds. (The import-time phone/fallback arrays — `BLACKBOX_TOOLS_*`/`CHAT_TOOLS_*` — are frozen snapshots that need a restart.) Dynamic fields use an `"x-source"` marker (e.g. `"x-source": "operators"`) resolved at injection time by a registered resolver. `embeddings.json` is the ONLY cache (hash-keyed; re-embeds only changed descriptions). The v1 byte-offset `volume.txt`/`manifest.json` monolith is DELETED.

**Edit → validate → reload workflow:**
```bash
# 1. Edit ToolVault/tools/<name>/schema.json or executor.py
# 2. Validate (CI gate — exits non-zero on any invalid module):
python -m Orchestrator.toolvault.validate
# 3. Make it live (re-embed + bust caches, no restart):
curl -X POST http://localhost:9091/toolvault/reload
```
Health/report endpoints: `GET /toolvault/health`, `GET /toolvault/validate`. Module layer lives in `Orchestrator/toolvault/{registry,resolvers,schema_spec,embeddings,injector,meta_tool,context,validate}.py`.

**Adding a tool (agent playbook):** `ToolVault/tools/ADDING_A_TOOL.md` — the step-by-step procedure (worked example: `ToolVault/tools/roll_dice/`). **Field reference:** `ToolVault/tools/README.md`. **Design doc:** `docs/plans/2026-06-06-toolvault-v2-modules-design.md`.

## Creating Web Apps

When asked to create a web application, **ALWAYS** follow this process:

### 1. Create in Apps Directory
```bash
mkdir -p /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/{app-name}
```

### 2. Create Files
- `index.html` - Main HTML file
- `server.py` - Python HTTP server (use port 8060-8099)

### 3. Start the Server
```bash
python3 /path/to/Apps/{app-name}/server.py &
sleep 2
ss -tlnp | grep {PORT}  # Verify running
```

### 4. Register with Portal (CRITICAL)
```bash
curl -X POST http://localhost:9091/agent/apps/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "App Display Name",
    "port": {PORT},
    "directory": "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Apps/{app-name}",
    "operator": "system"
  }'
```

### 5. Verify
```bash
curl http://localhost:9091/agent/apps
```

**See `/register-app` command for complete documentation.**

## API Endpoints

### App Management
- `GET /agent/apps` - List registered apps
- `POST /agent/apps/register` - Register new app
- `DELETE /agent/apps/{app_id}` - Unregister app
- `GET /app-proxy/{port}/` - Reverse proxy to app

### Snapshots
- `POST /mint` - Create snapshot
- `POST /checkpoint` - Create checkpoint
- `GET /timeline` - Get snapshot timeline
- `POST /agent/dev-snapshot` - Create dev snapshot (Brandon-DEV operator)

### Chat
- `POST /chat` - Send chat message
- `WS /ws/agent/{session_id}` - Claude Code WebSocket

## Slash Commands

- `/snapshot-dev [operator]` — Mint a development snapshot of completed work via `/chat/save` auto-mint. Operator resolves dynamically (single → auto; multiple → AskUserQuestion dropdown; see `resolve-operator`); pass an explicit operator as arg to override. **Invoke automatically at the end of substantial work (see "When to Create Snapshots" above).**
- `/register-app` — Documentation for app registration with the Portal.
- `/restart` — Restart the BlackBox systemd service (60-90s warm-up due to snapshot index rebuild).

## Important Notes

1. **Service restart**: `sudo systemctl restart blackbox.service`
2. **Version bump**: Update `?v=genuiXX` in index.html after changes
3. **Apps use "system" operator** to be visible to all users
4. **Port 9091** is reserved for the Orchestrator
5. **Ports 8060-8099** recommended for user apps
