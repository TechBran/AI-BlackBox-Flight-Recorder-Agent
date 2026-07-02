"""Curated source ANSWERS for the digest-vs-perspective A/B recall eval.

WHY CURATED (not historical extraction): The volume bodies in
Volumes/SNAPSHOT_VOLUME.txt use the OLD ceremonial snapshot format (BEACON +
VOLUME TRACKER + GAUGES + Kernel Index + Raw Session Log with [REASONING]
inline). The user-facing ANSWER (production's `reply_alias`) is NOT cleanly
separable from reasoning in those mixed bodies — there is no reliable close
tag, and reasoning often dominates. Extracting clean answer-only text is
therefore unreliable and would itself bias the comparison (whatever heuristic
splits answer-from-reasoning leaks into both arms unevenly).

The task explicitly permits curating 50 varied answer texts that mimic the real
corpus. We do that here. Each entry is a realistic assistant *reply* (the ANSWER
only — NO reasoning, NO ceremonial wrapper, NO keywords line) of the kind this
project's assistant actually produces, spanning the real topic distribution seen
in the BlackBox MEMORY.md: UGV robotics/Nav2/SLAM/IMU, embeddings & retrieval,
ToolVault, voice/TTS/STT, Computer Use, onboarding/Tauri, Android, snapshots,
Google Workspace, ElevenLabs, phone/SMS gateways, etc.

FAIRNESS: the queries are generated from THESE answer texts only (never from a
keyword line or a perspective), so neither arm is advantaged. Fixed order →
reproducible sampling.
"""

ANSWERS = [
    # --- UGV robotics / Nav2 / SLAM / IMU ---
    "The 'spin forever' symptom on the tracked robot was caused by a high GoalAlign.scale in the DWB controller conflicting with the RotationShim. Keeping GoalAlign at 0 fixes it. Do not add a BackUp recovery or costmap-clearing behavior to the Nav2 behavior tree — the design is rotate-in-place plus prevention-first, never reversing.",
    "Yaw stopped tracking during driving because the BNO085 IMU's -20 degree mount pitch was uncompensated. Declaring the tilt in the URDF imu_joint rpy as '0 -0.349 0' corrects the cos(20 degrees) = 6 percent yaw underestimation. After the fix, SLAM map-to-odom corrections dropped from 24cm and 5 degrees down to clean. two_d_mode does not absorb the tilt.",
    "The Nav2 planner was swapped from SmacPlanner2D to SmacPlannerHybrid using Reeds-Shepp motion with a 0.10m turn radius. This fixes the circle-versus-rectangle corner-clip mismatch. Footprint is 11 by 8 inches (0.140 by 0.102 meters), inflation 0.22 and 5.0, xy tolerance 0.20, yaw tolerance 0.35, min_vel_x = 0.",
    "Nav2 hesitation, collision, and freeze symptoms on the Jetson are caused by CPU saturation, not configuration. Check the load average and lifecycle responsiveness first. The Orin Nano maxes at 15W with no MAXN mode. We deployed jetson-performance.service to pin a persistent performance governor.",
    "The single zero Twist message is insufficient for e-stop because controller_server reasserts cmd_vel within about 50ms. The fix fans out: cancel the nav goal, call /explore/stop, pin zero velocity at 20Hz for 1.5 seconds, and send a firmware T=0 command. A fifth e-stop branch, er_cancel, was added later.",
    "ZUPT (zero-velocity update) preprocessing dropped the /odom yaw rate at rest from 0.08 down to 0.0000 degrees per second. The IMU is now silent when the robot is stationary, which stops phantom drift from accumulating in the EKF during pauses.",
    "The wake-word went silently dead after restart because of a stale /tmp/ugv_ears_muted flag left over from the previous session. The fix is to remove that mute flag on boot so the ears service starts unmuted.",
    "USB peripheral flapping on long autonomous sessions is a battery sag symptom — the charger cannot keep up with the draw. Check the battery voltage before chasing OAK-D camera or EMEET microphone cable bugs; it is almost always power, not the peripheral.",
    "The frontier explorer was sending Nav2 goals into inflation zones and getting controller-reject loops because it picked goals from /map without consulting the global costmap. We added a COSTMAP_REJECT_THRESHOLD of 90 that shifts the goal to a clear cell in the cluster or skips it, failing open if the costmap has not arrived yet.",
    "The X_LINK_ERROR cascades and dead camera streams are caused by the flat 4-pin cable, not a software regression. The OAK-D Lite is USB 2.0 by spec, so 480M is the correct enumeration speed. Waveshare bundled the correct cable in the box.",

    # --- Embeddings & retrieval ---
    "The 'search only returns old snapshots' complaint was root-caused — it was NOT a stale store. We unified one canonical recency-aware retriever across all surfaces using reciprocal rank fusion, a mild 0.05 tie-break, and MMR. We also added per-model threshold calibration, setting gemini-embedding-2 to 0.55, plus a model-switch gap-guard and a provider-down signal.",
    "Per-model semantic thresholds were calibrated because a single global threshold was wrong after the retrieval_query fix: gemini gets 0.60, qwen gets 0.54. This fixed a recall bug where 0.7 was too high after switching queries to the retrieval_query instruction across all three retrieval sites.",
    "The pluggable embeddings layer shrank the store from 408MB to 4MB and cut boot time to 11 seconds by moving to binary stores with a provider abstraction. After registering a successor model you MUST POST to /embeddings/health/check or the one-click Upgrade button never appears in the UI.",
    "Queries must be embedded with purpose='query' and documents with purpose='document' because the active provider applies a different query_instruction prefix for retrieval queries. Embedding a query as a document (the legacy bug) measurably hurt recall.",
    "The keyword index peak memory dropped from 127MB to 38MB. The embedding is generated from the full snapshot body but truncated at EMBEDDING_MAX_CHARS, which is 10000 characters, so very long bodies lose their tail before embedding.",

    # --- ToolVault ---
    "ToolVault v2 makes per-tool JSON-plus-Python modules the single source of truth. Each tool is a folder with a canonical schema.json and an optional executor.py. The chat injector, MCP server, and static fallback arrays all derive from these modules — the byte-offset volume and manifest monolith is deleted. embeddings.json is the only cache, keyed by description hash.",
    "After editing a tool schema, run python -m Orchestrator.toolvault.validate as the CI gate, then POST to /toolvault/reload to re-embed and bust caches without a restart. The live chat injector picks up schema edits automatically via the registry mtime cache, but the import-time fallback arrays are frozen snapshots that need a full restart.",
    "Models were sending complex array and object tool parameters as JSON strings, like requests='[...]', which made the executor's isinstance(list) check reject them with 'must be a list'. The fix is schema-aware coercion at the single dispatch chokepoint in BlackBoxToolExecutor.execute, not per-executor patches. Diagnose via the raw tool-input JSON in the logs, not theory.",

    # --- Voice / TTS / STT / ElevenLabs ---
    "The TTS picker voices — 71 total, 11 OpenAI plus 30 Gemini Flash plus 30 Gemini Pro — live in the backend config.py build_tts_catalog function and are served by GET /tts/catalog. Both Android and the Portal fetch that catalog. To change voices, edit config.py; do not re-add hardcoded voice lists in the frontends.",
    "Diarized transcription works through speech_to_text with provider='elevenlabs' and diarize=true, which adds speaker labels for up to 32 speakers. Setting mint=true saves the diarized transcript as a searchable snapshot, proving the diarization-as-memory pattern.",
    "The native SIGABRT in AudioRecord releaseBuffer 'on speaking' is a read racing release across threads. The fix is to release in the read loop's finally block (the same thread as read), read via a local reference, and have the stop function only signal. This applies to all AudioRecord usage — voice, STT, and future phone.",
    "Streaming STT runs behind a uniform /ws/stt websocket with a cumulative-delta protocol. OpenAI gpt-realtime-whisper streams live word-by-word at 24kHz; Google Cloud Speech v2 chirp_2 with a service account is final-only. Verified on web, Android, and the desktop Tauri app.",
    "WebKitGTK denies getUserMedia by default, so the STT microphone is dead in the packaged .deb desktop app even though it works in the browser. The fix is in the Tauri src-tauri lib.rs with_webview hook: enable_media_stream plus allow the permission-request, and pin webkit2gtk to exactly 2.0.2 to match wry.",

    # --- Computer Use ---
    "The Computer Use engine ships three drivers — Anthropic, Gemini, and OpenAI — behind one headless runner at browser/headless.py that serves /browser/run, the use_computer tool, and the scheduler. GET /models/computer-use returns the live capability-filtered catalog with a per-model backend field, and GET /cu/preflight runs machine-readiness checks with customer-facing remediation strings.",

    # --- Onboarding / Tauri / product ---
    "The onboarding architecture is locked: Tauri wraps the Portal, customers bring their own API keys, and it ships as a hardware product first. The build is estimated at 6 to 8 weeks. Keys are entered through the onboarding wizard, and GET /elevenlabs/status reports which capabilities the entered key unlocks.",

    # --- Snapshots / reply rendering ---
    "Phase A of reply rendering hardening makes the renderer auto-fence unfenced JSON blocks identically on Android and the Portal. The heuristic is parse-gated: a line that starts with JSON-like content, is either multiline or 80-plus characters, and actually parses as valid JSON gets fenced. Android uses MarkdownText.kt with kotlinx.serialization; the Portal fences before the markdown parser runs.",
    "A confabulated tool-failure report is detectable by four signs together: the error string does not exist anywhere in the code, the reported IDs are fabricated, there are no TOOLVAULT-EXEC log lines for the supposed call, and the narrative contradicts itself internally. The root cause was stale memory retrieval leading the model to narrate a fake tool transcript as prose.",
    "Post-cutover the snapshot body is composed deterministically: the HTML-and-URL-stripped answer first, then a server-side Keywords line with no LLM involved, and the model's native reasoning appended last. The ordering is deliberate so the 10K embedding truncation eats reasoning first, never the answer or the keywords.",

    # --- Google Workspace / Gmail ---
    "Google Workspace integration adds Docs, Sheets, Slides, Drive, and Calendar as 19 ToolVault tools riding on the existing Gmail OAuth. The raw batchUpdate passthrough gives full structural editing. Three live-only gotchas bit us: OAUTHLIB_RELAX_TOKEN_SCOPE must be set, a SERVICE_DISABLED error is not the same as a scope 403, and asyncio.run breaks test isolation.",
    "gmail_* MCP tools now work through a whitelisted POST /gmail/execute that routes through execute_tool(). This is the reusable pattern for exposing an in-process backend tool to the MCP without giving it a separate network surface.",

    # --- Phone / SMS / gateways ---
    "The NeoGate TG200 gateway has NO REST API — it is driven entirely through AMI and WebCGI. The SMS gate is a contact-book whitelist. The rebuild uses a schema v2 with encryption and a per-gateway AMIConnectionManager for multi-gateway and multi-SIM support.",
    "KillMode=process was orphaning the Asterisk audio_subprocess, which then squatted on port 9092 and caused Errno 98 on every restart. The fix is an ExecStartPre that SIGKILLs any leftover audio_subprocess.py — SIGTERM is caught and hangs when the process is orphaned, so it must be -9.",

    # --- systemd / sandboxing ---
    "ProtectHome=read-only, PrivateTmp=true, and ProtectSystem=strict silently break the claude, gemini, and codex tmux PTY bridge. The claude CLI specifically needs ProtectHome=no because it writes to too many paths at the home root to enumerate in a whitelist. Never share the /tmp socket with a user-shell tmux server.",
    "When a systemd-service child silently hangs with no error, no log, and no output, but the same binary works in your shell, compare /proc/<pid>/ns/mnt between the two. A different mount namespace means ProtectHome or ProtectSystem is silently blocking writes the app does no error handling for.",

    # --- Android ---
    "Every frontend change ships to three surfaces: the Portal web app, the Android Kotlin MVP that natively consumes /tts/catalog and /ws/stt, and the WebView wrappers. Keep the catalog contracts additive so older clients do not break.",

    # --- Gemini Live / robotics ER ---
    "The Android Voice Agent intentionally defaults Gemini Live to gemini-3.1-flash-live-preview. This is a deliberate exception to the GA-over-preview rule, made for the thinkingLevel support. Do not 'fix' it back to a GA model.",
    "ER 1.6 suppresses text output in agentic mode. The narration fix is a per-step prompt plus a tool-synthesis fallback, plus an ExecStopPost hook that kills the in-container zombie processes left behind on restart.",

    # --- model / config hygiene ---
    "gemini-3-pro-preview is retired and returns HTTP 404. It was hardcoded in the voice-session save path for realtime, grok, and gemini_live, which meant voice snapshots silently never minted, plus the task_routes file-analysis path. The fix points everything at GEMINI_MODEL_DEFAULT. Never hardcode model literals; use config.",
    "Never use git add -A or git add . in a commit — it sweeps pre-existing untracked local files like the typescript artifact and local scripts into the repo. That bit us once and got pushed to main. Always stage explicit paths, especially in subagent commit instructions.",

    # --- behavioral / architecture ---
    "behavioral_core.py is the single source of truth for persona, tone, and anti-sycophancy. It is injected into six prompt sites: the chat path and three voice routes. Computer Use, phone, and SMS are deliberately skipped.",
    "Prefill is TTFB-sensitive and has a per-provider cap, while the tool loop expands context unbounded. The per-turn caps live in PROVIDER_CAPS in context_builder.py: Anthropic is 75K and Google is 200K.",

    # --- snapshot mechanics ---
    "To mint a development snapshot, POST to /chat/save, not /chat — /chat wastes an LLM round-trip, while /chat/save is direct persistence and roughly 400 times cheaper. Auto-mint with turns_threshold=1 fires perform_mint immediately and generates the embedding inline before returning, so the snapshot is searchable the instant the curl returns. Do not call /mint afterward; that creates a duplicate.",

    # --- operator resolution ---
    "Never hardcode the operator. Resolve it dynamically via get_current_operator: with a single operator it is automatic, with multiple operators present an AskUserQuestion dropdown. The operator list comes from GET /operators. Brandon is only the seed operator on an unconfigured box — do not assume it.",

    # --- misc proven patterns ---
    "After two 'this should fix it' commits on the same symptom, the next commit must be telemetry or logging, not another guess. Logs that disprove the theory beat four more silent failures.",
    "pkill -f inside a docker exec bash -c matches the parent shell's argv and ends up SIGKILLing the SSH session. Use pgrep to resolve the PIDs and then kill them explicitly instead.",
    "GNOME xdg-mime defaults drift on Ubuntu Desktop, and the native GTK file dialog bypasses the xdg-open PATH shims entirely. The fix is a startup hook that re-asserts the xdg-mime defaults via the /onboarding/cli-agent/url-handlers endpoint.",
    "Default any media provider to its flagship model at maximum output quality. Cheaper or faster tiers are explicit user downgrades, never a silent default. Generation tools are named by their actual provider and model — elevenlabs_music, lyria_music — for quality traceability.",
    "The Madgwick filter outputs frame_id='odom', which breaks the EKF. You must subscribe to /oak/imu directly instead of the Madgwick output. This frame_id pitfall is easy to miss because the data looks correct otherwise.",
    "Swapping the Nav2 obstacle_layer for a static_layer to offload the streaming nvblox map cost MORE CPU, going from 39 percent to 58 percent. StaticLayer is the wrong abstraction for a streaming map. We reverted it; a custom Nav2 costmap plugin is the only real path to GPU offload.",
    "Tailscale plus the LAN is the BlackBox security boundary by design. The backend binds 0.0.0.0 and trusts the caller-asserted operator. Do not 'fix' this with app-layer auth — keep the blast-radius scoping via whitelists instead.",
]

assert len(ANSWERS) == 50, f"expected 50 answers, got {len(ANSWERS)}"
