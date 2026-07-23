package com.aiblackbox.portal

import android.Manifest
import android.content.pm.PackageManager
import android.media.MediaPlayer
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.union
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.wrapContentHeight
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.ui.Alignment
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.material3.Text
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.TextFieldValue
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.repository.TtsQueue
import com.aiblackbox.portal.data.repository.TtsQueueFailedException
import com.aiblackbox.portal.data.repository.TtsQueueUnavailableException
import com.aiblackbox.portal.data.repository.TtsRepository
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.voice.AudioRecorderManager
import com.aiblackbox.portal.data.voice.SttEvent
import com.aiblackbox.portal.data.voice.SttStreamClient
import com.aiblackbox.portal.navigation.BlackBoxNavGraph
import com.aiblackbox.portal.ui.chat.AttachmentItem
import com.aiblackbox.portal.ui.chat.ChatState
import com.aiblackbox.portal.ui.chat.ChatViewModel
import com.aiblackbox.portal.ui.chat.Composer
import com.aiblackbox.portal.ui.chat.MAX_UPLOAD_SIZE
import com.aiblackbox.portal.ui.chat.FALLBACK_COMPOSER_HEIGHT
import com.aiblackbox.portal.ui.chat.LIVE_EDGE_GAP
import com.aiblackbox.portal.ui.chat.SIGNAL_RESIDENCE_HEIGHT
import com.aiblackbox.portal.ui.chat.calculateBottomFocalGeometry
import com.aiblackbox.portal.ui.chat.rememberFilePicker
import com.aiblackbox.portal.ui.chat.ReturnToLiveHost
import com.aiblackbox.portal.ui.theme.BlackBoxTheme
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.components.BlackBoxTopBar
import com.aiblackbox.portal.ui.insets.LocalShowAppChrome
import com.aiblackbox.portal.ui.settings.SettingsSheet
import com.aiblackbox.portal.ui.updates.UpdatesViewModel
import com.aiblackbox.portal.navigation.Routes
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.util.normalizeApiOrigin
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File

/** Build a properly JSON-serialized TTS batch request body. No manual string escaping. */
private fun buildTtsBatchBody(
    text: String, voice: String, model: String, format: String, provider: String, operator: String
): String = buildJsonObject {
    // §3.5: strip non-speakable content (artifacts/media-urls/fenced-code/
    // {ui_reply} envelope) before TTS -- mirrors Portal stripNonSpeakable.
    put("text", com.aiblackbox.portal.util.SpeakableText.stripNonSpeakable(text))
    put("voice", voice)
    put("model", model)
    put("format", format)
    put("provider", provider)
    put("operator", operator)
}.toString()

class NativeMainActivity : ComponentActivity() {

    private var pendingMicAction: (() -> Unit)? = null

    private val micPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            pendingMicAction?.invoke()
        } else {
            Toast.makeText(this, "Microphone permission required for recording", Toast.LENGTH_SHORT).show()
        }
        pendingMicAction = null
    }

    private fun withMicPermission(action: () -> Unit) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED) {
            action()
        } else {
            pendingMicAction = action
            micPermLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    // =========================================================================
    // B3 (2026-07-22): on-box (qwen) TTS via the async B1 server queue.
    // Submit (POST /tts/queue) -> poll (GET /tts/task/{id}, 1.5s cadence) ->
    // download the finished WAV. Poll requests are SHORT, so this path no
    // longer leans on the 300s ttsClient read-timeout stopgap. Cloud
    // providers never come through here — their direct paths are untouched.
    // =========================================================================

    /**
     * Queue an on-box clip for a message bubble, driving the live status chip
     * via chatViewModel.setMessageTtsGenerating(id, true, "Queued — N ahead" /
     * "Generating M/K…").
     *
     * Returns true when HANDLED — audio delivered (player appears) or a
     * terminal failure was toasted (speaker resets to idle = the retry
     * affordance) — so the caller must NOT run the direct /tts/batch path.
     * Returns false ONLY when the backend has no /tts/queue (404, pre-B1
     * build): the caller falls back to the existing direct call, byte-for-byte
     * unchanged.
     */
    private suspend fun tryQueueTtsForMessage(
        api: BlackBoxApi,
        chatViewModel: ChatViewModel,
        messageId: String,
        text: String,
        voice: String,
        operator: String,
    ): Boolean {
        chatViewModel.setMessageTtsGenerating(messageId, true, "Queueing audio…")
        return try {
            val bytes = TtsRepository(api).generateViaQueue(
                text = text, voice = voice, operator = operator,
            ) { st ->
                chatViewModel.setMessageTtsGenerating(messageId, true, TtsQueue.statusLine(st))
            }
            val tempFile = File(cacheDir, "tts_q_${messageId.take(8)}_${System.currentTimeMillis()}.wav")
            withContext(Dispatchers.IO) { tempFile.writeBytes(bytes) }
            // Sets the audio path AND clears ttsGenerating + the status chip.
            chatViewModel.setMessageTtsAudioUrl(messageId, tempFile.absolutePath)
            true
        } catch (e: TtsQueueUnavailableException) {
            // Pre-B1 backend: clear the chip but KEEP the spinner — the caller
            // continues straight into the direct /tts/batch path.
            chatViewModel.setMessageTtsGenerating(messageId, true, null)
            false
        } catch (e: kotlinx.coroutines.CancellationException) {
            chatViewModel.setMessageTtsGenerating(messageId, false)
            throw e
        } catch (e: Exception) {
            Log.e("TTS", "Queue TTS failed: ${e.message}", e)
            // Reset to idle — tapping the speaker again IS the retry action.
            chatViewModel.setMessageTtsGenerating(messageId, false)
            val retryable = (e as? TtsQueueFailedException)?.retryable ?: true
            val hint = if (retryable) " — tap the speaker to retry" else ""
            withContext(Dispatchers.Main) {
                Toast.makeText(
                    applicationContext,
                    "Audio failed: ${e.message?.take(80) ?: "unknown error"}$hint",
                    Toast.LENGTH_LONG
                ).show()
            }
            true
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        com.aiblackbox.portal.data.voice.AudioPlaybackManager.release()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        // Notification RECEIVER (MN.4): bring up the MODEL-FREE listener FGS that owns
        // the single control-port socket so the phone can receive a server push and post
        // a REAL system notification with no Gemma in the path, even backgrounded/closed.
        // Started from a foregrounded Activity so the FGS start is permitted. BootReceiver
        // (MN.5) re-arms it after reboot via a direct start in the exempt BOOT_COMPLETED
        // receiver context (connectedDevice is not boot-blocked).
        NotificationListenerFgs.start(this)

        // control_phone: also bring up LocalModelService's listener path, which now just
        // PUBLISHES the Gemma task handler into the shared holder (the FGS above owns the
        // socket). Listener-only (no engine warm) — control_phone wakes Gemma on demand.
        LocalModelService.startListener(this)

        // Normalize origin for API calls — handles Tailscale .ts.net domains
        // (forces HTTPS, strips port for Tailscale, removes /ui/ suffix)
        val rawOrigin = getSharedPreferences(Constants.PREFS_NAME, MODE_PRIVATE)
            .getString(Constants.KEY_ORIGIN, "") ?: ""
        val origin = normalizeApiOrigin(rawOrigin)

        setContent {
            BlackBoxTheme {
                val navController = rememberNavController()
                val store = remember { BlackBoxStore(applicationContext) }
                val scope = rememberCoroutineScope()
                val operator by store.operator.collectAsState(initial = Constants.DEFAULT_OPERATOR)
                val provider by store.provider.collectAsState(initial = Constants.DEFAULT_PROVIDER)
                val currentModel by store.model.collectAsState(initial = "")

                val chatViewModel: ChatViewModel = viewModel()
                // B5: ONE activity-owned UpdatesViewModel. In a ComponentActivity's
                // setContent, viewModel() resolves to the activity ViewModelStoreOwner,
                // so this instance survives navigation and stays alive on the Chat home
                // — letting the top-bar badge read update/embedding state without the
                // Updates screen ever being opened. The SAME instance is threaded into
                // the Updates screen via BlackBoxNavGraph(updatesVm = ...).
                val updatesVm: UpdatesViewModel = viewModel()
                val inputText by chatViewModel.inputText.collectAsState()
                val chatState by chatViewModel.chatState.collectAsState()
                val erMissionActive by chatViewModel.erMissionActive.collectAsState()
                val snapshotCount by chatViewModel.snapshotCount.collectAsState()
                val isHealthy by chatViewModel.isHealthy.collectAsState()
                val checkpointTurns by chatViewModel.checkpointTurns.collectAsState()
                val operators by chatViewModel.operators.collectAsState()
                var showSettings by remember { mutableStateOf(false) }
                // T23 device QA fix (2026-05-26): CliAgentScreen-in-Terminal-state
                // must hide BOTH the floating operator chrome (Layer 2) AND the
                // floating X close button (Layer 2.5). Both live as siblings to
                // BlackBoxNavGraph here, so the CompositionLocalProvider pattern
                // T20 used inside CliAgentScreen never reached them — that
                // provider only scopes to children of CliAgentScreen, not
                // activity-level overlays. Lifting the flag to Activity scope
                // and passing the setter down through NavGraph is the correct
                // mechanism.
                var cliAgentInTerminal by remember { mutableStateOf(false) }
                val autoTtsEnabled by store.autoTtsEnabled.collectAsState(initial = false)
                // Ember Backdrop mode (off / generating / always) — provided to the
                // tree below via LocalEmberMode so EmberOverlay can honor it.
                val emberMode by store.emberMode.collectAsState(initial = "always")
                // Particle FIELD style (stars / embers / matrix) — provided to the
                // tree below via LocalParticleMode so EmberOverlay picks the look.
                val particleMode by store.particleMode.collectAsState(initial = "stars")

                // Legacy audio recorder — still used by onRecordAudio (Gemini) and
                // the CLI CliMicButton. onWhisper no longer drives it.
                val audioRecorder = remember { AudioRecorderManager(applicationContext) }
                var isWhisperRecording by remember { mutableStateOf(false) }

                // Live streaming STT client for onWhisper (multi-provider /ws/stt).
                // Built once per origin, mirroring how VoiceScreen builds VoiceClient.
                val sttClient = remember(origin) {
                    val wsUrl = origin.replace("https://", "wss://").replace("http://", "ws://")
                    SttStreamClient(BlackBoxApi(origin).getClient(), wsUrl)
                }
                val isWhisperStreaming by sttClient.isStreaming.collectAsState()
                // Collect amplitude as Compose state so the waveform recomposes as
                // it changes. Reading sttClient.amplitude.value directly does NOT
                // subscribe → the ribbon would freeze at one value (caught 2026-06-05).
                val sttAmp by sttClient.amplitude.collectAsState()
                // Cumulative-delta applier holders: captured at stream start from the caret.
                var sttBaseBefore by remember { mutableStateOf("") }
                var sttBaseAfter by remember { mutableStateOf("") }

                // Tap-toggle STT (Brandon 2026-07-05): tap 1 starts + live-appends into
                // the input; tap 2 just STOPS — the transcript stays in the box for
                // editing, no auto-send. The mic writes text; the user sends explicitly.
                // Tapping send while the mic is live stops it and sends in one tap.
                // stop() flushes a graceful close that can emit one trailing stt_final;
                // on a mic-tap stop we want it (last words land), but right after a
                // send-while-live it would repopulate the just-cleared box, so
                // sttDiscardTrailingFinal drops exactly that one late final.
                var sttDiscardTrailingFinal by remember { mutableStateOf(false) }

                // Raw audio recorder for Gemini audio analysis
                val rawAudioRecorder = remember { AudioRecorderManager(applicationContext) }
                var isRawAudioRecording by remember { mutableStateOf(false) }

                // File attachments state
                val attachments = remember { mutableStateListOf<AttachmentItem>() }
                // Pre-uploaded media URLs (from raw audio record — already on server)
                val preUploadedUrls = remember { mutableStateListOf<String>() }
                val jsonParser = remember { Json { ignoreUnknownKeys = true; isLenient = true } }

                // File picker launcher
                val launchFilePicker = rememberFilePicker { uri ->
                    val contentResolver = applicationContext.contentResolver
                    val cursor = contentResolver.query(uri, null, null, null, null)
                    var fileName = "file"
                    var fileSize = 0L
                    cursor?.use {
                        if (it.moveToFirst()) {
                            val nameIdx = it.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                            val sizeIdx = it.getColumnIndex(android.provider.OpenableColumns.SIZE)
                            if (nameIdx >= 0) fileName = it.getString(nameIdx) ?: "file"
                            if (sizeIdx >= 0) fileSize = it.getLong(sizeIdx)
                        }
                    }
                    val mimeType = contentResolver.getType(uri) ?: "application/octet-stream"

                    if (fileSize > MAX_UPLOAD_SIZE) {
                        Toast.makeText(applicationContext, "File too large (max 500MB)", Toast.LENGTH_SHORT).show()
                    } else {
                        attachments.add(
                            AttachmentItem(
                                uri = uri,
                                name = fileName,
                                mimeType = mimeType,
                                sizeBytes = fileSize
                            )
                        )
                        Toast.makeText(applicationContext, "File attached", Toast.LENGTH_SHORT).show()
                    }
                }

                // Sync auto-TTS flag from store to viewmodel
                LaunchedEffect(autoTtsEnabled) {
                    chatViewModel.autoTtsEnabled = autoTtsEnabled
                }

                LaunchedEffect(origin) {
                    if (origin.isNotBlank()) {
                        chatViewModel.initialize(origin)
                    }
                }

                // B5: initialize the activity-scoped UpdatesViewModel once — this
                // creates its api/repo AND does the first status+embeddings fetch, so
                // the top-bar badge's `attention` flow is populated as soon as origin
                // resolves (no need to open the Updates screen first).
                LaunchedEffect(origin) {
                    if (origin.isNotBlank()) {
                        updatesVm.initialize(origin)
                    }
                }

                // M3: re-fetch update/embedding status whenever the app returns to the
                // foreground (e.g. back from the onboarding wizard) so the top-bar
                // badge stays fresh WITHOUT the user opening Updates. Low-frequency by
                // design — one fetch per ON_RESUME, no tight loop.
                val lifecycleOwner = androidx.lifecycle.compose.LocalLifecycleOwner.current
                DisposableEffect(lifecycleOwner, origin) {
                    val observer = androidx.lifecycle.LifecycleEventObserver { _, event ->
                        if (event == androidx.lifecycle.Lifecycle.Event.ON_RESUME && origin.isNotBlank()) {
                            updatesVm.refreshStatus(forceFresh = false)
                        }
                    }
                    lifecycleOwner.lifecycle.addObserver(observer)
                    onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
                }

                // Live STT — collect transcript events and apply the cumulative-delta
                // applier to the prompt (TextFieldValue). Delta.text is the full
                // interim so far; replace the interim region. Final.text commits.
                LaunchedEffect(sttClient) {
                    sttClient.events.collect { event ->
                        when (event) {
                            is SttEvent.Delta -> {
                                // Ignore any late interim after a send-while-live cleared
                                // the box (the trailing final is dropped separately).
                                if (sttDiscardTrailingFinal) return@collect
                                val newText = sttBaseBefore + event.text + sttBaseAfter
                                chatViewModel.onInputChange(
                                    TextFieldValue(
                                        newText,
                                        TextRange((sttBaseBefore + event.text).length)
                                    )
                                )
                            }
                            is SttEvent.Final -> {
                                // A send-while-live already consumed + cleared the box;
                                // drop the one trailing final stop()'s grace emits so it
                                // doesn't repopulate the now-empty input.
                                if (sttDiscardTrailingFinal) {
                                    sttDiscardTrailingFinal = false
                                    return@collect
                                }
                                val committed = if (event.text.isNotEmpty() && !event.text.last().isWhitespace())
                                    "${event.text} " else event.text
                                sttBaseBefore += committed
                                val merged = sttBaseBefore + sttBaseAfter
                                chatViewModel.onInputChange(
                                    TextFieldValue(merged, TextRange(sttBaseBefore.length))
                                )
                            }
                            is SttEvent.Error -> {
                                Toast.makeText(applicationContext, event.message, Toast.LENGTH_SHORT).show()
                                sttClient.stop()
                            }
                            is SttEvent.SessionEnded -> {
                                // Terminal marker from stop(): nothing further will
                                // arrive for this session. If a send-while-live armed
                                // the trailing-final discard but that final never came
                                // (server produced nothing), disarm it here so it can't
                                // eat the NEXT session's first final.
                                sttDiscardTrailingFinal = false
                            }
                        }
                    }
                }

                // Stop the streaming mic + WS when the composable leaves the tree.
                DisposableEffect(sttClient) {
                    onDispose { sttClient.stop() }
                }

                // Auto-TTS: observe events from ChatViewModel and speak
                // Saves audio to file and sets URL on last assistant message for inline player
                // Vibrate when chat response finishes generating
                val vibrator = remember {
                    if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
                        val mgr = getSystemService(android.os.VibratorManager::class.java)
                        mgr?.defaultVibrator
                    } else {
                        @Suppress("DEPRECATION")
                        getSystemService(android.content.Context.VIBRATOR_SERVICE) as? android.os.Vibrator
                    }
                }

                LaunchedEffect(chatState) {
                    // Vibrate when streaming transitions to IDLE (response complete)
                    if (chatState == com.aiblackbox.portal.ui.chat.ChatState.IDLE) {
                        val lastMsg = chatViewModel.messages.value.lastOrNull()
                        if (lastMsg != null && lastMsg.role == "assistant" && lastMsg.content.isNotBlank()) {
                            vibrator?.let { v ->
                                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                                    v.vibrate(android.os.VibrationEffect.createOneShot(100, android.os.VibrationEffect.DEFAULT_AMPLITUDE))
                                } else {
                                    @Suppress("DEPRECATION")
                                    v.vibrate(100)
                                }
                            }
                        }
                    }
                }

                // Auto-TTS: speak response when complete
                // Mirrors the manual onSpeakWithId flow — shows spinner, generates, shows player
                LaunchedEffect(Unit) {
                    chatViewModel.autoTtsEvent.collect { text ->
                        // Capture the target message BEFORE the async request
                        val targetMsg = chatViewModel.messages.value.lastOrNull { it.role == "assistant" }
                        val messageId = targetMsg?.id ?: return@collect

                        // Show generating spinner (same as manual TTS)
                        chatViewModel.setMessageTtsGenerating(messageId, true)

                        try {
                            val api = chatViewModel.getApi()
                            if (api == null) {
                                chatViewModel.setMessageTtsGenerating(messageId, false)
                                return@collect
                            }
                            val voiceValue = store.getOperatorVoice(operator).first()
                            val config = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceValue)
                            // B3: on-box voices go through the async queue when the
                            // backend has it (404 -> direct /tts/batch fallback below).
                            if (config.provider == TtsRepository.ON_BOX_PROVIDER &&
                                tryQueueTtsForMessage(api, chatViewModel, messageId, text, config.voice, operator)
                            ) {
                                return@collect
                            }
                            val body = buildTtsBatchBody(text, config.voice, config.model, "mp3", config.provider, operator)
                            val request = okhttp3.Request.Builder()
                                .url("${api.getBaseUrl()}/tts/batch")
                                .post(body.toRequestBody("application/json".toMediaType()))
                                .build()
                            val response = withContext(Dispatchers.IO) {
                                api.getTtsClient().newCall(request).execute()
                            }
                            if (response.isSuccessful) {
                                val bytes = withContext(Dispatchers.IO) { response.body?.bytes() }
                                if (bytes != null && bytes.isNotEmpty()) {
                                    val ext = if (config.provider == "gemini-pro" || config.provider == "gemini-flash") "wav" else "mp3"
                                    val tempFile = File(cacheDir, "auto_tts_${System.currentTimeMillis()}.$ext")
                                    withContext(Dispatchers.IO) { tempFile.writeBytes(bytes) }
                                    // Sets audio URL AND resets ttsGenerating = false
                                    chatViewModel.setMessageTtsAudioUrl(messageId, tempFile.absolutePath)
                                } else {
                                    chatViewModel.setMessageTtsGenerating(messageId, false)
                                }
                            } else {
                                chatViewModel.setMessageTtsGenerating(messageId, false)
                            }
                        } catch (e: Exception) {
                            Log.e("AutoTTS", "Failed: ${e.message}", e)
                            chatViewModel.setMessageTtsGenerating(messageId, false)
                        }
                    }
                }

                // Full-screen overlay layout — no Scaffold, no black bars
                // TopBar and Composer float over content with transparent backgrounds
                // Ember backdrop mode provided once here from the persisted setting;
                // read by EmberOverlay deep in the tree (call sites still pass "is generating").
                val returnToLiveHost = remember { com.aiblackbox.portal.ui.chat.ReturnToLiveHostState() }
                androidx.compose.runtime.CompositionLocalProvider(
                    com.aiblackbox.portal.ui.components.LocalEmberMode provides emberMode,
                    com.aiblackbox.portal.ui.components.LocalParticleMode provides particleMode,
                    com.aiblackbox.portal.ui.chat.LocalReturnToLiveHost provides returnToLiveHost,
                ) {
                val density = LocalDensity.current
                var windowBottomPx by remember { mutableStateOf(Float.NaN) }
                var composerTopPx by remember { mutableStateOf(Float.NaN) }
                var composerBottomPx by remember { mutableStateOf(Float.NaN) }
                val effectiveBottomInsetPx = WindowInsets.ime
                    .union(WindowInsets.navigationBars)
                    .getBottom(density)
                    .toFloat()
                val bottomFocalGeometry = calculateBottomFocalGeometry(
                    windowBottomPx = windowBottomPx,
                    composerTopPx = composerTopPx,
                    composerBottomPx = composerBottomPx,
                    residenceHeightPx = with(density) { SIGNAL_RESIDENCE_HEIGHT.toPx() },
                    breathingGapPx = with(density) { LIVE_EDGE_GAP.toPx() },
                    fallbackComposerHeightPx = with(density) { FALLBACK_COMPOSER_HEIGHT.toPx() },
                    effectiveBottomInsetPx = effectiveBottomInsetPx,
                )
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(BbxBlack)
                        .onGloballyPositioned {
                            windowBottomPx = it.boundsInWindow().bottom
                        }
                ) {
                    // Layer 1: Content (full screen, edge to edge)
                    BlackBoxNavGraph(
                        navController = navController,
                        origin = origin,
                        operator = operator,
                        currentModel = currentModel,
                        chatViewModel = chatViewModel,
                        updatesVm = updatesVm,
                        onModelChange = { scope.launch { store.setModel(it) } },
                        bottomFocalGeometry = bottomFocalGeometry,
                        onSpeak = { text ->
                            scope.launch {
                                try {
                                    val api = chatViewModel.getApi() ?: return@launch
                                    val voiceValue = store.getOperatorVoice(operator).first()
                                    val config = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceValue)
                                    // B3: on-box voices go through the async queue when the
                                    // backend has it (404 -> direct /tts/batch fallback below).
                                    // No message bubble here, so no status chip — but the poll
                                    // path survives arbitrarily long queue waits.
                                    if (config.provider == TtsRepository.ON_BOX_PROVIDER) {
                                        val queued: ByteArray? = try {
                                            TtsRepository(api).generateViaQueue(text, config.voice, operator)
                                        } catch (e: TtsQueueUnavailableException) {
                                            null // pre-B1 backend — fall through to /tts/batch
                                        } catch (e: TtsQueueFailedException) {
                                            Toast.makeText(
                                                applicationContext,
                                                "Audio failed: ${e.message?.take(80)}",
                                                Toast.LENGTH_LONG
                                            ).show()
                                            return@launch
                                        }
                                        if (queued != null) {
                                            val tempFile = File(cacheDir, "tts_${System.currentTimeMillis()}.wav")
                                            withContext(Dispatchers.IO) { tempFile.writeBytes(queued) }
                                            val player = MediaPlayer()
                                            player.setDataSource(tempFile.absolutePath)
                                            player.prepare()
                                            player.start()
                                            player.setOnCompletionListener { it.release(); tempFile.delete() }
                                            return@launch
                                        }
                                    }
                                    val body = buildTtsBatchBody(text, config.voice, config.model, "mp3", config.provider, operator)
                                    val request = okhttp3.Request.Builder()
                                        .url("${api.getBaseUrl()}/tts/batch")
                                        .post(body.toRequestBody("application/json".toMediaType()))
                                        .build()
                                    val response = withContext(Dispatchers.IO) {
                                        api.getTtsClient().newCall(request).execute()
                                    }
                                    if (response.isSuccessful) {
                                        val bytes = withContext(Dispatchers.IO) { response.body?.bytes() }
                                        if (bytes != null && bytes.isNotEmpty()) {
                                            val tempFile = File(cacheDir, "tts_${System.currentTimeMillis()}.mp3")
                                            tempFile.writeBytes(bytes)
                                            val player = MediaPlayer()
                                            player.setDataSource(tempFile.absolutePath)
                                            player.prepare()
                                            player.start()
                                            player.setOnCompletionListener { it.release(); tempFile.delete() }
                                        }
                                    }
                                } catch (e: Exception) {
                                    Log.e("TTS", "Playback failed: ${e.message}", e)
                                }
                            }
                        },
                        onSpeakWithId = { messageId, text ->
                            // Mark as generating (button turns red)
                            chatViewModel.setMessageTtsGenerating(messageId, true)
                            Toast.makeText(applicationContext, "Generating speech...", Toast.LENGTH_SHORT).show()
                            // Keep alive in background for TTS generation
                            try {
                                val svcIntent = android.content.Intent(applicationContext, BackgroundTaskService::class.java).apply {
                                    action = BackgroundTaskService.ACTION_START
                                    putExtra(BackgroundTaskService.EXTRA_TASK_LABEL, "Generating speech...")
                                }
                                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                                    startForegroundService(svcIntent)
                                } else {
                                    startService(svcIntent)
                                }
                            } catch (_: Exception) {}
                            scope.launch {
                                // D10 (Task 7.9): declared outside the try so the finally block
                                // can always cancel the slow-first-byte watchdog.
                                var slowToastJob: kotlinx.coroutines.Job? = null
                                try {
                                    val api = chatViewModel.getApi()
                                    if (api == null) {
                                        chatViewModel.setMessageTtsGenerating(messageId, false)
                                        return@launch
                                    }
                                    val voiceValue = store.getOperatorVoice(operator).first()
                                    val config = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceValue)
                                    // B3: on-box voices go through the async queue when the
                                    // backend has it — live chip on the bubble, short poll
                                    // requests. 404 (pre-B1 backend) returns false and falls
                                    // through to the direct path below; the outer finally still
                                    // stops the background service on every exit.
                                    if (config.provider == TtsRepository.ON_BOX_PROVIDER &&
                                        tryQueueTtsForMessage(api, chatViewModel, messageId, text, config.voice, operator)
                                    ) {
                                        return@launch
                                    }
                                    // D10 (Task 7.9): on-box (qwen) voices may wait on a GPU group
                                    // swap; show "loading models…" only if the first byte is slow.
                                    slowToastJob = if (config.provider == com.aiblackbox.portal.data.repository.TtsRepository.ON_BOX_PROVIDER) scope.launch {
                                        com.aiblackbox.portal.data.repository.TtsRepository.awaitSlowFirstByte(
                                            stillInFlight = { true },
                                            onSlow = { Toast.makeText(applicationContext, "Loading on-box voice models…", Toast.LENGTH_SHORT).show() },
                                        )
                                    } else null
                                    val body = buildTtsBatchBody(text, config.voice, config.model, "mp3", config.provider, operator)
                                    val request = okhttp3.Request.Builder()
                                        .url("${api.getBaseUrl()}/tts/batch")
                                        .post(body.toRequestBody("application/json".toMediaType()))
                                        .build()
                                    val response = withContext(Dispatchers.IO) {
                                        api.getTtsClient().newCall(request).execute()
                                    }
                                    if (response.isSuccessful) {
                                        val bytes = withContext(Dispatchers.IO) { response.body?.bytes() }
                                        if (bytes != null && bytes.isNotEmpty()) {
                                            val ext = if (config.provider == "gemini-pro" || config.provider == "gemini-flash") "wav" else "mp3"
                                            val tempFile = File(cacheDir, "tts_${messageId.take(8)}_${System.currentTimeMillis()}.$ext")
                                            withContext(Dispatchers.IO) { tempFile.writeBytes(bytes) }
                                            // Store file path — button turns green, AudioPlayerBar appears
                                            chatViewModel.setMessageTtsAudioUrl(messageId, tempFile.absolutePath)
                                        } else {
                                            // 200 but empty body — reset so button is clickable again
                                            chatViewModel.setMessageTtsGenerating(messageId, false)
                                            withContext(Dispatchers.Main) {
                                                Toast.makeText(applicationContext, "TTS: empty response", Toast.LENGTH_SHORT).show()
                                            }
                                        }
                                    } else {
                                        chatViewModel.setMessageTtsGenerating(messageId, false)
                                        withContext(Dispatchers.Main) {
                                            Toast.makeText(applicationContext, "TTS failed: ${response.code}", Toast.LENGTH_SHORT).show()
                                        }
                                    }
                                } catch (e: Exception) {
                                    Log.e("TTS", "TTS generation failed: ${e.message}", e)
                                    chatViewModel.setMessageTtsGenerating(messageId, false)
                                    withContext(Dispatchers.Main) {
                                        Toast.makeText(applicationContext, "TTS failed: ${e.message?.take(30)}", Toast.LENGTH_SHORT).show()
                                    }
                                } finally {
                                    // D10 (Task 7.9): cancel the slow-first-byte watchdog on
                                    // EVERY exit path (success/failure/throw) — the job is
                                    // launched with stillInFlight = { true }, so cancellation
                                    // is the only thing that suppresses a stale "loading
                                    // models…" toast after a failed/thrown request.
                                    slowToastJob?.cancel()
                                    // Stop background service when TTS completes
                                    try {
                                        val stopIntent = android.content.Intent(applicationContext, BackgroundTaskService::class.java).apply {
                                            action = BackgroundTaskService.ACTION_STOP
                                        }
                                        startService(stopIntent)
                                    } catch (_: Exception) {}
                                }
                            }
                        },
                        // T22: CLI Agents' SessionSwitcherTopBar hamburger
                        // routes through here to open the same SettingsSheet
                        // the global BlackBoxTopBar uses.
                        onOpenSettings = { showSettings = true },
                        // T23 device QA fix: lets CliAgentScreen tell the
                        // activity when its inner state is in Terminal mode,
                        // so the activity-level chrome layers (Layer 2
                        // BlackBoxTopBar + Layer 2.5 X close) can hide.
                        onCliAgentTerminalActiveChange = { active ->
                            cliAgentInTerminal = active
                        },
                        // M2 return-refresh: when the in-app wizard WebView
                        // (Routes.WIZARD) closes, force-refresh the SHARED
                        // activity-scoped UpdatesViewModel so a model/reranker
                        // change made inside the wizard reflects in the top-bar
                        // badge + Updates screen. This is the same `updatesVm`
                        // instance the Updates destination uses (a nav-scoped
                        // WIZARD destination can't reach that VM directly).
                        onWizardReturn = {
                            updatesVm.refreshStatus(forceFresh = true)
                        },
                    )

                    // Auto-set provider when on dedicated provider screens
                    LaunchedEffect(Unit) {
                        navController.currentBackStackEntryFlow.collect { entry ->
                            // destination.route is the registered PATTERN — for
                            // computer_use it now carries "?liveDevice={liveDevice}"
                            // (the pill hand-off), so compare on the path segment.
                            val route = entry.destination.route?.substringBefore('?')
                            if (route == Routes.COMPUTER_USE) {
                                val currentProv = store.provider.first()
                                if (currentProv != "computer-use") {
                                    store.setProvider("computer-use")
                                    val cuModels = Constants.MODEL_CONFIG["computer-use"] ?: emptyList()
                                    val currentMdl = store.model.first()
                                    if (cuModels.isNotEmpty() && cuModels.none { it.first == currentMdl }) {
                                        store.setModel(cuModels.first().first)
                                    }
                                }
                            }
                            if (route == Routes.ROBOTICS) {
                                val currentProv = store.provider.first()
                                if (currentProv != "robotics") {
                                    store.setProvider("robotics")
                                    val erModels = Constants.MODEL_CONFIG["robotics"] ?: emptyList()
                                    val currentMdl = store.model.first()
                                    if (erModels.isNotEmpty() && erModels.none { it.first == currentMdl }) {
                                        store.setModel(erModels.first().first)
                                    }
                                }
                            }
                        }
                    }

                    // Layer 2: TopBar floating on top (transparent bg, only bubbles visible)
                    // T20: respect LocalShowAppChrome so screens like CliAgentScreen
                    // (terminal-active branch) can hide the operator pill while
                    // SessionSwitcherTopBar owns the top region.
                    //
                    // [AppChromeLayer] scopes the LocalShowAppChrome.current read
                    // to a 1-line composable; without this extraction every
                    // sibling in this setContent body would invalidate on toggle.
                    // Brandon fit pass 2026-07-23: the CU live view hides the
                    // operator pill too — no operator switching happens on
                    // that screen (back out to switch) and the pill was the
                    // last overlay crowding the viewer's toolbar. The X close
                    // button (Layer 2.5) stays as the exit affordance.
                    val chromeRoute = navController
                        .currentBackStackEntryAsState().value?.destination?.route
                    val cuLiveViewActive =
                        chromeRoute?.startsWith(Routes.CU_LIVE_VIEW) == true
                    AppChromeLayer(forceHide = cliAgentInTerminal || cuLiveViewActive) {
                        BlackBoxTopBar(
                            operator = operator,
                            operators = operators,
                            snapshotCount = snapshotCount,
                            checkpointTurns = checkpointTurns,
                            isHealthy = isHealthy,
                            onMenuClick = { showSettings = true },
                            // D1: badged Updates icon — lights when the shared
                            // UpdatesViewModel says there's something to surface,
                            // taps straight to the Updates screen.
                            showUpdatesBadge = updatesVm.attention.collectAsState().value,
                            onUpdatesClick = { navController.navigate(Routes.UPDATES) },
                            onOperatorChange = { scope.launch { store.setOperator(it) } },
                            onAddOperator = { name ->
                                scope.launch {
                                    try {
                                        val api = chatViewModel.getApi() ?: return@launch
                                        val body = """{"name":"$name"}"""
                                        val response = api.post("/operator/add", body)
                                        val obj = jsonParser.parseToJsonElement(response).jsonObject
                                        val status = obj["status"]?.jsonPrimitive?.content
                                        if (status == "success" || status == "exists") {
                                            store.setOperator(name)
                                            chatViewModel.checkHealth() // Refreshes operator list
                                        }
                                    } catch (e: Exception) {
                                        android.util.Log.e("AddOperator", "Failed: ${e.message}", e)
                                    }
                                }
                            }
                        )
                    }

                    // Layer 2.5: Floating X close button on sub-screens (not chat)
                    // Essential for XR goggles where back gesture is difficult
                    val currentBackStackEntry by navController.currentBackStackEntryAsState()
                    val currentRoute = currentBackStackEntry?.destination?.route
                    // T23 device QA fix: also hide when CliAgentScreen is in
                    // Terminal state — the SessionSwitcherTopBar's own
                    // hamburger occupies this region, and two affordances at
                    // the same screen position overlap visually on Z Fold 6.
                    if (currentRoute != null && currentRoute != Routes.CHAT && !cliAgentInTerminal) {
                        Box(
                            modifier = Modifier
                                .align(Alignment.TopStart)
                                .statusBarsPadding()
                                .padding(start = 12.dp, top = 8.dp)
                                .size(36.dp)
                                .clip(androidx.compose.foundation.shape.CircleShape)
                                .background(androidx.compose.ui.graphics.Color(0xCC1C1C1E))
                                .border(
                                    1.dp,
                                    androidx.compose.ui.graphics.Color(0x33FFFFFF),
                                    androidx.compose.foundation.shape.CircleShape
                                )
                                .clickFeedback {
                                    navController.popBackStack(Routes.CHAT, inclusive = false)
                                },
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                "\u2715",
                                color = BbxWhite,
                                fontSize = 16.sp,
                                fontWeight = androidx.compose.ui.text.font.FontWeight.Medium
                            )
                        }
                    }

                    // Layer 3: TaskPanel floating above composer
                    val activeTasks by chatViewModel.activeTasks.collectAsState()
                    var showTaskPanel by remember { mutableStateOf(true) }
                    Box(modifier = Modifier.align(Alignment.BottomEnd).padding(bottom = 200.dp, end = 12.dp)) {
                        com.aiblackbox.portal.ui.components.TaskPanel(
                            tasks = activeTasks,
                            visible = showTaskPanel,
                            onDismiss = { showTaskPanel = false },
                            onStopTask = { taskId -> chatViewModel.cancelTask(taskId) },
                            // Pill "Live" → the NATIVE Computer Use screen (the same
                            // modal behind the system menu), device preselected and
                            // the live screenshot stream auto-toggled ON via the
                            // ?liveDevice hand-off. Replaces the old CU Live-view
                            // WebView overlay (it bounced users into a browser
                            // session — wrong surface for the Android MVP).
                            onLiveView = { devId ->
                                navController.navigate(
                                    Routes.COMPUTER_USE +
                                        "?liveDevice=" + android.net.Uri.encode(devId)
                                )
                            }
                        )
                    }
                    // Re-show panel when new tasks arrive
                    LaunchedEffect(activeTasks) {
                        if (activeTasks.isNotEmpty()) showTaskPanel = true
                    }

                    // Task completion notifications
                    val notificationMgr = remember { BlackBoxNotificationManager(applicationContext) }
                    LaunchedEffect(Unit) {
                        chatViewModel.taskCompletedEvent.collect { completedTask: com.aiblackbox.portal.data.model.TaskStatus ->
                            val isSuccess = completedTask.status.equals("completed", true)
                            val typeLabel = completedTask.taskType?.replace("_", " ")?.replaceFirstChar { it.uppercase() } ?: "Task"
                            notificationMgr.showTaskNotification(
                                title = if (isSuccess) "$typeLabel Complete" else "$typeLabel Failed",
                                body = if (isSuccess) "Your $typeLabel is ready" else (completedTask.error ?: "Generation failed"),
                                operator = operator,
                                taskType = completedTask.taskType,
                                isSuccess = isSuccess
                            )
                        }
                    }

                    // Layer 4: Composer floating at bottom (transparent bg, only pills visible)
                    // Hide on screens that have their own compose UI (SMS, Contacts, CLI Agent,
                    // Voice — the provider/model composer is irrelevant in voice-agent mode).
                    val hideComposerRoutes = setOf(Routes.SMS_INBOX, Routes.CONTACTS, Routes.CLI_AGENT, Routes.VOICE)
                    // Only the chat surfaces render the bottom Signal residence
                    // (MainChatContent / AgentLiveMessageContent). Reserving the
                    // 40dp band on every other route floated the composer over a
                    // dead black strip (image gen, timeline, ...).
                    val signalResidenceRoutes = setOf(Routes.CHAT, Routes.AGENT, Routes.GEMINI_AGENT)
                    if (currentRoute !in hideComposerRoutes)
                    Box(modifier = Modifier
                        .align(Alignment.BottomCenter)
                        // CRITICAL: wrapContentHeight prevents this Box from expanding
                        // to fill the parent and intercepting touches on chat content above.
                        // Without this, the Box measured to fill available height, creating
                        // an invisible touch-consuming overlay above the visible Composer.
                        .wrapContentHeight(Alignment.Bottom)
                        .padding(
                            bottom = (if (currentRoute in signalResidenceRoutes) SIGNAL_RESIDENCE_HEIGHT else 0.dp) +
                                with(density) { effectiveBottomInsetPx.toDp() },
                        )
                    ) {
                        Composer(
                            value = inputText,
                            onValueChange = { chatViewModel.onInputChange(it) },
                            applySystemBottomInsets = false,
                            onSend = {
                                // Send while the mic is live → stop it and send in one
                                // tap. Kill the stream, arm the trailing-final discard so
                                // stop()'s grace final doesn't repopulate the box after
                                // sendMessage() clears it, and drop the STT base anchors.
                                if (sttClient.isStreaming.value) {
                                    sttDiscardTrailingFinal = true
                                    sttClient.stop()
                                    sttBaseBefore = ""
                                    sttBaseAfter = ""
                                }
                                // Upload attachments first, then send message with URLs
                                val hasAttachments = attachments.isNotEmpty()
                                val hasPreUploaded = preUploadedUrls.isNotEmpty()
                                if (hasAttachments || hasPreUploaded) {
                                    val toUpload = attachments.toList()
                                    val alreadyUploaded = preUploadedUrls.toList()
                                    attachments.clear()
                                    preUploadedUrls.clear()
                                    scope.launch {
                                        val api = chatViewModel.getApi()
                                        if (api == null) {
                                            chatViewModel.sendMessage()
                                            return@launch
                                        }
                                        // Start with pre-uploaded URLs (from raw audio record)
                                        val uploadedUrls = alreadyUploaded.toMutableList()
                                        for (item in toUpload) {
                                            try {
                                                // Copy URI content to temp file
                                                val tempFile = withContext(Dispatchers.IO) {
                                                    val f = File(cacheDir, "upload_${System.currentTimeMillis()}_${item.name}")
                                                    applicationContext.contentResolver.openInputStream(item.uri)?.use { input ->
                                                        f.outputStream().use { output -> input.copyTo(output) }
                                                    }
                                                    f
                                                }
                                                // Upload to /upload endpoint
                                                val response = withContext(Dispatchers.IO) {
                                                    api.uploadFile("/upload", tempFile)
                                                }
                                                // Parse response for URL
                                                val obj = jsonParser.parseToJsonElement(response).jsonObject
                                                val url = obj["url"]?.jsonPrimitive?.content
                                                if (url != null) {
                                                    // Convert relative URL to absolute
                                                    val fullUrl = if (url.startsWith("http")) url
                                                                  else "${api.getBaseUrl()}$url"
                                                    uploadedUrls.add(fullUrl)
                                                }
                                                // Clean up temp file
                                                withContext(Dispatchers.IO) { tempFile.delete() }
                                            } catch (e: Exception) {
                                                Log.e("FileUpload", "Upload failed for ${item.name}: ${e.message}", e)
                                            }
                                        }
                                        chatViewModel.sendMessage(imageUrls = uploadedUrls)
                                    }
                                } else {
                                    chatViewModel.sendMessage()
                                }
                            },
                            onAttach = {
                                launchFilePicker()
                            },
                            onComputerUse = {
                                // Composer shortcut (Brandon 2026-07-23): straight
                                // into the CU live view. "auto" 302s server-side to
                                // the best surface — a running agent's desktop
                                // first, else the main desktop.
                                navController.navigate("${Routes.CU_LIVE_VIEW}/auto")
                            },
                            onWhisper = {
                                // Tap-toggle dictation via /ws/stt (Brandon 2026-07-05).
                                // Tap 1 = start + live-append; tap 2 = just STOP. The
                                // transcript stays in the box for editing — no auto-send.
                                // (Tapping send while live stops + sends; see onSend.)
                                if (sttClient.isStreaming.value) {
                                    sttClient.stop()
                                } else {
                                    withMicPermission {
                                        // Fresh dictation: don't drop the first final.
                                        sttDiscardTrailingFinal = false
                                        // Capture the caret-anchored base for the
                                        // cumulative-delta applier BEFORE starting.
                                        val cur = chatViewModel.inputText.value
                                        val insert = cur.selection.start.coerceIn(0, cur.text.length)
                                        sttBaseBefore = cur.text.substring(0, insert)
                                        sttBaseAfter = cur.text.substring(insert)
                                        sttClient.start()
                                    }
                                }
                            },
                            onRecordAudio = {
                                if (isRawAudioRecording) {
                                    // Stop recording — matches Portal gemini-recorder.js pipeline:
                                    // 1. Upload audio to /upload
                                    // 2. Transcribe via /stt (Whisper) → insert text in prompt
                                    // 3. Add as attachment so it's sent as audio_url to Gemini
                                    isRawAudioRecording = false
                                    Toast.makeText(applicationContext, "Processing audio...", Toast.LENGTH_SHORT).show()
                                    scope.launch {
                                        val file = rawAudioRecorder.stopRecording()
                                        if (file != null) {
                                            val api = chatViewModel.getApi()
                                            if (api != null) {
                                                try {
                                                    // Step 1: Upload audio file
                                                    val response = withContext(Dispatchers.IO) {
                                                        api.uploadFile("/upload", file)
                                                    }
                                                    val obj = jsonParser.parseToJsonElement(response).jsonObject
                                                    val url = obj["url"]?.jsonPrimitive?.content

                                                    if (url != null) {
                                                        val fullUrl = if (url.startsWith("http")) url
                                                                      else "${api.getBaseUrl()}$url"
                                                        // Add to pre-uploaded URLs (already on server, no re-upload needed)
                                                        preUploadedUrls.add(fullUrl)
                                                    }

                                                    // Step 2: Transcribe via Whisper STT
                                                    try {
                                                        val sttResponse = withContext(Dispatchers.IO) {
                                                            api.uploadFile("/stt", file)
                                                        }
                                                        val sttObj = jsonParser.parseToJsonElement(sttResponse).jsonObject
                                                        val transcript = sttObj["text"]?.jsonPrimitive?.content?.trim()
                                                        if (!transcript.isNullOrBlank()) {
                                                            // Insert transcription into prompt (matching Portal behavior)
                                                            val currentText = chatViewModel.inputText.value.text
                                                            val prefix = if (currentText.isBlank()) "" else "$currentText "
                                                            chatViewModel.onInputChange(
                                                                TextFieldValue("$prefix$transcript")
                                                            )
                                                            Toast.makeText(applicationContext, "Audio attached + transcribed", Toast.LENGTH_SHORT).show()
                                                        } else {
                                                            Toast.makeText(applicationContext, "Audio attached (no speech detected)", Toast.LENGTH_SHORT).show()
                                                        }
                                                    } catch (e: Exception) {
                                                        Log.w("RawAudio", "STT failed (audio still attached): ${e.message}")
                                                        Toast.makeText(applicationContext, "Audio attached (transcription unavailable)", Toast.LENGTH_SHORT).show()
                                                    }
                                                } catch (e: Exception) {
                                                    Log.e("RawAudio", "Upload failed: ${e.message}", e)
                                                    Toast.makeText(applicationContext, "Upload failed: ${e.message}", Toast.LENGTH_SHORT).show()
                                                } finally {
                                                    withContext(Dispatchers.IO) { file.delete() }
                                                }
                                            }
                                        }
                                    }
                                } else {
                                    withMicPermission {
                                        if (rawAudioRecorder.startRecording()) {
                                            isRawAudioRecording = true
                                            Toast.makeText(applicationContext, "Recording audio...", Toast.LENGTH_SHORT).show()
                                        }
                                    }
                                }
                            },
                            // Live voice-agent launcher (soundwave button in the input
                            // bubble) → the dedicated voice screen. Composer stays
                            // nav-agnostic; the host owns navigation. launchSingleTop
                            // mirrors the provider-select voice hand-off above.
                            onVoiceAgent = { navController.navigate(Routes.VOICE) { launchSingleTop = true } },
                            // Allow sends during robotics ER missions (prompt injection)
                            isStreaming = (chatState == ChatState.STREAMING || chatState == ChatState.THINKING)
                                && !(provider == "robotics" && erMissionActive),
                            isRecording = isWhisperStreaming,
                            isRecordingAudio = isRawAudioRecording,
                            recordingAmplitude = {
                                if (isWhisperStreaming) sttAmp
                                else rawAudioRecorder.getMaxAmplitude() / 32767f
                            },
                            provider = provider,
                            model = currentModel,
                            onProviderChange = { newProvider ->
                                scope.launch {
                                    store.setProvider(newProvider)
                                    // Reset model to Auto when switching providers
                                    // so we don't send e.g. claude-opus-4-6 to Gemini
                                    store.setModel("")
                                }
                                // Auto-navigate to dedicated screens for special providers
                                val targetRoute = when (newProvider) {
                                    "computer-use" -> Routes.COMPUTER_USE
                                    "robotics" -> Routes.ROBOTICS
                                    "gemini-live", "grok-live", "realtime" -> Routes.VOICE
                                    "agents" -> Routes.AGENT
                                    "gemini-agents" -> Routes.GEMINI_AGENT
                                    else -> null
                                }
                                if (targetRoute != null) {
                                    navController.navigate(targetRoute) {
                                        launchSingleTop = true
                                    }
                                }
                            },
                            onModelChange = { scope.launch { store.setModel(it) } },
                            autoTtsEnabled = autoTtsEnabled,
                            onAutoTtsToggle = { scope.launch { store.setAutoTtsEnabled(!autoTtsEnabled) } },
                            providerLabel = chatViewModel.getProviderLabel(),
                            // Task 1.6: offer the on-device LOCAL provider only when a
                            // verified model is installed; re-check (+ best-effort
                            // re-attest) each time the picker opens.
                            localAvailable = chatViewModel.localAvailable.collectAsState().value,
                            // Task W1: on-device engine readiness drives the pill's "loading…/ready"
                            // suffix so the model warm is visible before the first send.
                            localEngineState = chatViewModel.localEngineState.collectAsState().value,
                            onProviderMenuOpen = { chatViewModel.refreshLocalAvailability() },
                            liveModels = chatViewModel.liveModels.collectAsState().value,
                            // Task 7.1: custom-provider warm dots (id → load status
                            // from GET /models/custom; empty for other providers).
                            customModelStatus = chatViewModel.customModelStatus.collectAsState().value,
                            attachments = attachments,
                            onRemoveAttachment = { index ->
                                if (index in attachments.indices) attachments.removeAt(index)
                            },
                            modifier = Modifier.onGloballyPositioned { coordinates ->
                                coordinates.boundsInWindow().let { bounds ->
                                    composerTopPx = bounds.top
                                    composerBottomPx = bounds.bottom
                                }
                            },
                        )
                    }

                    // Layer 5: highest activity-owned chrome, permanently mounted.
                    ReturnToLiveHost(
                        state = returnToLiveHost,
                        composerTopPx = composerTopPx,
                    )

                }
                } // end CompositionLocalProvider(LocalEmberMode)

                // Settings sheet
                if (showSettings) {
                    SettingsSheet(
                        origin = origin,
                        operators = operators,
                        onDismiss = { showSettings = false },
                        onNavigate = { route ->
                            navController.navigate(route)
                        },
                        onClearHistory = { chatViewModel.clearHistory() }
                    )
                }
            }
        }
    }
}

/**
 * T20 polish: scope the [LocalShowAppChrome] read to a tiny composable so
 * the giant `setContent { ... }` body in [NativeMainActivity] doesn't
 * subscribe (and therefore invalidate) every sibling on chrome toggle.
 *
 * Pass the chrome ([content]) as a slot lambda — it only executes when
 * the local resolves to `true`. The CompositionLocal subscription stays
 * inside this 1-line composable; the rest of the activity content stays
 * insulated from chrome-visibility toggles.
 */
@Composable
private fun AppChromeLayer(
    forceHide: Boolean = false,
    content: @Composable () -> Unit,
) {
    // forceHide is the activity-scope override (T23 fix); LocalShowAppChrome
    // is the legacy CompositionLocal kept for any descendant-scoped consumers.
    if (!forceHide && LocalShowAppChrome.current) content()
}
