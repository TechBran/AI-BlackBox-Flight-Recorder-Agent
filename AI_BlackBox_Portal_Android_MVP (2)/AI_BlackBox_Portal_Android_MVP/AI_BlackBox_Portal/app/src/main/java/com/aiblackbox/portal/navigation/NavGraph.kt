package com.aiblackbox.portal.navigation

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import android.net.Uri
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.navArgument
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.ui.chat.AgentChatScreen
import com.aiblackbox.portal.ui.chat.ChatScreen
import com.aiblackbox.portal.ui.chat.ChatViewModel
import com.aiblackbox.portal.ui.chat.BottomFocalGeometry
import com.aiblackbox.portal.ui.generation.ImageGenScreen
import com.aiblackbox.portal.ui.generation.VideoGenScreen
import com.aiblackbox.portal.ui.generation.MusicGenScreen
import com.aiblackbox.portal.ui.generation.GoogleSsmlScreen
import com.aiblackbox.portal.ui.generation.GeminiProTtsScreen
import com.aiblackbox.portal.ui.devices.DeviceManagerScreen
import com.aiblackbox.portal.ui.cron.CronManagerScreen
import com.aiblackbox.portal.ui.timeline.TimelineScreen
import com.aiblackbox.portal.ui.cellular.CellularScreen
import com.aiblackbox.portal.ui.cli_agent.CliAgentScreen
import com.aiblackbox.portal.ui.computeruse.CuScreen
import com.aiblackbox.portal.ui.robotics.RoboticsScreen
import com.aiblackbox.portal.ui.media.MediaBrowserScreen
import com.aiblackbox.portal.ui.telephony.TelephonyScreen
import com.aiblackbox.portal.ui.sms.SmsInboxScreen
import com.aiblackbox.portal.ui.contacts.ContactsScreen
import com.aiblackbox.portal.ui.voice.VoiceScreen
import com.aiblackbox.portal.ui.voicelab.VoiceLabScreen
import com.aiblackbox.portal.ui.updates.UpdatesScreen
import com.aiblackbox.portal.ui.updates.UpdatesViewModel
import com.aiblackbox.portal.ui.settings.LocalModelSettingsScreen
import com.aiblackbox.portal.ui.webview.WizardWebViewScreen

object Routes {
    const val CHAT = "chat"
    const val SETTINGS = "settings"
    const val TIMELINE = "timeline"
    const val MEDIA = "media"
    const val DEVICES = "devices"
    const val CRON = "cron"
    const val TELEPHONY = "telephony"
    const val CELLULAR = "cellular"
    const val IMAGE_GEN = "image_gen"
    const val VIDEO_GEN = "video_gen"
    const val MUSIC_GEN = "music_gen"
    const val TTS = "tts"
    const val GOOGLE_SSML = "google_ssml"
    const val GEMINI_PRO_TTS = "gemini_pro_tts"
    const val COMPUTER_USE = "computer_use"
    const val CLI_AGENT = "cli_agent"
    const val ROBOTICS = "robotics"
    const val AGENT = "agent"
    const val GEMINI_AGENT = "gemini_agent"
    const val VOICE = "voice"
    const val VOICE_LAB = "voice_lab"
    const val SMS_INBOX = "sms_inbox"
    const val CONTACTS = "contacts"
    const val UPDATES = "updates"
    const val LOCAL_MODEL_SETTINGS = "local_model_settings"
    // In-app onboarding-wizard WebView (M4). Optional `suffix` nav-arg carries
    // the wizard query string, e.g. "?step=embeddings" or "?mode=manage",
    // URL-encoded by the caller (Uri.encode). Replaces the external-browser
    // ACTION_VIEW hand-offs for manage/step deep-links.
    const val WIZARD = "wizard"
}

@Composable
fun BlackBoxNavGraph(
    navController: NavHostController,
    origin: String,
    operator: String,
    currentModel: String = "",
    chatViewModel: ChatViewModel? = null,
    /**
     * B5: the activity-scoped [UpdatesViewModel] shared with the top-bar
     * Updates badge (NativeMainActivity owns it). Threaded into the UPDATES
     * destination so the screen and the badge observe ONE instance. REQUIRED
     * (non-null) on purpose: a caller that forgets to thread it would otherwise
     * let the UPDATES composable mint a second, nav-scoped VM and desync the
     * badge from the screen with NO compile error — so this is a hard param.
     */
    updatesVm: UpdatesViewModel,
    onSpeak: (String) -> Unit = {},
    onSpeakWithId: (String, String) -> Unit = { _, _ -> },
    onModelChange: (String) -> Unit = {},
    bottomFocalGeometry: BottomFocalGeometry? = null,
    /**
     * Open the global SettingsSheet (the app's hamburger menu).
     *
     * Threaded through so destinations that own their own top bar — e.g.
     * [CliAgentScreen]'s [com.aiblackbox.portal.ui.cli_agent.SessionSwitcherTopBar] —
     * can surface a menu button without re-implementing the sheet locally.
     * Wired in [com.aiblackbox.portal.NativeMainActivity] to `showSettings = true`.
     */
    onOpenSettings: () -> Unit = {},
    /**
     * T23 device QA hook (2026-05-26): [CliAgentScreen] calls this with
     * `true` when its inner state transitions to a terminal branch and
     * `false` when it leaves. NativeMainActivity uses the flag to hide
     * its activity-scope floating chrome (operator pill + Layer 2.5 X
     * close button) so they don't overlap the SessionSwitcherTopBar.
     */
    onCliAgentTerminalActiveChange: (Boolean) -> Unit = {},
    /**
     * M4/M2: invoked when the in-app wizard WebView ([Routes.WIZARD]) closes
     * or is backed out of, so the activity can force-refresh update/embedding
     * status (a model/reranker change made inside the wizard reflects in the
     * badge + Updates screen). Wired in [com.aiblackbox.portal.NativeMainActivity]
     * to the SHARED activity-scoped `updatesVm.refreshStatus(forceFresh = true)`
     * — a nav-scoped destination can't reach the Updates screen's VM directly.
     */
    onWizardReturn: () -> Unit = {},
) {
    NavHost(
        navController = navController,
        startDestination = Routes.CHAT
    ) {
        composable(Routes.CHAT) {
            if (chatViewModel != null) {
                ChatScreen(origin = origin, operator = operator, viewModel = chatViewModel, onSpeak = onSpeak, onSpeakWithId = onSpeakWithId, bottomFocalGeometry = bottomFocalGeometry)
            } else {
                // Fallback: should not happen in normal flow
                ChatScreen(origin = origin, operator = operator, viewModel = viewModel(), onSpeak = onSpeak, onSpeakWithId = onSpeakWithId, bottomFocalGeometry = bottomFocalGeometry)
            }
        }
        composable(Routes.AGENT) {
            AgentChatScreen(
                origin = origin,
                operator = operator,
                provider = "agents",
                chatViewModel = chatViewModel,
                bottomFocalGeometry = bottomFocalGeometry,
            )
        }
        composable(Routes.GEMINI_AGENT) {
            AgentChatScreen(
                origin = origin,
                operator = operator,
                provider = "gemini-agents",
                chatViewModel = chatViewModel,
                bottomFocalGeometry = bottomFocalGeometry,
            )
        }
        composable(Routes.SETTINGS) { PlaceholderScreen("Settings") }
        composable(Routes.TIMELINE) { TimelineScreen(origin = origin, operator = operator) }
        composable(Routes.MEDIA) { MediaBrowserScreen(origin = origin) }
        composable(Routes.DEVICES) { DeviceManagerScreen(origin = origin) }
        composable(Routes.CRON) { CronManagerScreen(origin = origin) }
        composable(Routes.TELEPHONY) { TelephonyScreen(origin = origin) }
        composable(Routes.CELLULAR) { CellularScreen(origin = origin) }
        composable(Routes.IMAGE_GEN) { ImageGenScreen(origin = origin) }
        composable(Routes.VIDEO_GEN) { VideoGenScreen(origin = origin) }
        composable(Routes.MUSIC_GEN) { MusicGenScreen(origin = origin) }
        composable(Routes.TTS) { PlaceholderScreen("Text-to-Speech") }
        composable(Routes.GOOGLE_SSML) { GoogleSsmlScreen(origin = origin) }
        composable(Routes.GEMINI_PRO_TTS) { GeminiProTtsScreen(origin = origin) }
        composable(
            // Optional ?liveDevice= hand-off from the task pill's "Live" button:
            // arrive with that device preselected and the live screenshot stream
            // toggled ON. A plain navigate(Routes.COMPUTER_USE) (system menu)
            // still matches — the arg defaults to "".
            route = "${Routes.COMPUTER_USE}?liveDevice={liveDevice}",
            arguments = listOf(navArgument("liveDevice") {
                type = NavType.StringType
                defaultValue = ""
            })
        ) { backStackEntry ->
            val liveDevice =
                backStackEntry.arguments?.getString("liveDevice").orEmpty()
            val vm = chatViewModel ?: viewModel<ChatViewModel>()
            val cuStep by vm.cuStep.collectAsState()
            val cuStepTotal by vm.cuStepTotal.collectAsState()
            val cuStatus by vm.cuStatus.collectAsState()
            val cuActionLabel by vm.cuActionLabel.collectAsState()
            val messages by vm.messages.collectAsState()
            // CU model hydration (Task 17): live catalog + id→backend map from
            // GET /models/computer-use, fetched by ChatViewModel when the
            // provider flips to "computer-use" (NativeMainActivity route effect).
            val liveModels by vm.liveModels.collectAsState()
            val cuModelBackends by vm.cuModelBackends.collectAsState()

            CuScreen(
                origin = origin,
                model = currentModel,
                cuStep = cuStep,
                cuStepTotal = cuStepTotal,
                cuStatus = cuStatus,
                cuActionLabel = cuActionLabel,
                liveModels = liveModels,
                cuModelBackends = cuModelBackends,
                onModelChange = onModelChange,
                onDeviceChange = { deviceId -> vm.setCuDeviceId(deviceId) },
                onStopCu = { vm.stopCuTask() },
                onNewSession = { vm.resetCuSession() },
                messages = messages,
                onSpeak = onSpeak,
                onSpeakWithId = onSpeakWithId,
                initialLiveDeviceId = liveDevice.ifBlank { null },
            )
        }
        composable(
            Routes.CLI_AGENT,
        ) {
            // T23: clear the terminal-active flag when this composable leaves
            // composition (user navigated away from CLI Agents entirely).
            // Otherwise a stale `true` would keep the activity chrome hidden
            // on the next screen.
            androidx.compose.runtime.DisposableEffect(Unit) {
                onDispose { onCliAgentTerminalActiveChange(false) }
            }
            CliAgentScreen(
                origin = origin,
                operator = operator,
                onBackToTools = { navController.popBackStack() },
                // T22: SessionSwitcherTopBar's hamburger forwards to the
                // global SettingsSheet so the CLI Agents screen integrates
                // with the same menu the rest of the app uses.
                onOpenNavDrawer = onOpenSettings,
                // T23: propagate inner state-machine transitions up to the
                // activity so the chrome layers can hide.
                onTerminalActiveChange = onCliAgentTerminalActiveChange,
            )
        }
        composable(Routes.ROBOTICS) {
            val vm = chatViewModel ?: viewModel<ChatViewModel>()
            val erStatus by vm.erStatus.collectAsState()
            val erReasoning by vm.erReasoning.collectAsState()
            val erCameraFrame by vm.erCameraFrame.collectAsState()

            RoboticsScreen(
                origin = origin,
                model = currentModel,
                erStatus = erStatus,
                erReasoning = erReasoning,
                erCameraFrame = erCameraFrame,
                onModelChange = onModelChange,
                onCameraChange = { camera -> vm.setErCamera(camera) }
            )
        }
        composable(Routes.VOICE) { VoiceScreen(origin = origin) }
        composable(Routes.VOICE_LAB) { VoiceLabScreen(origin = origin) }
        composable(Routes.SMS_INBOX) { SmsInboxScreen(origin = origin, operator = operator) }
        composable(Routes.CONTACTS) { ContactsScreen(origin = origin, operator = operator) }
        composable(Routes.UPDATES) {
            // B5: use the activity-scoped instance so the top-bar badge and this
            // screen share ONE VM (updatesVm is a required param — no fallback,
            // so a second nav-scoped instance can never be minted here).
            UpdatesScreen(
                viewModel = updatesVm,
                origin = origin,
                // M4: manage/step hand-offs open the in-app wizard WebView
                // instead of ACTION_VIEW'ing an external browser. The suffix
                // (e.g. "?step=embeddings") is URL-encoded into the nav-arg.
                onOpenWizard = { suffix ->
                    navController.navigate(Routes.WIZARD + "?suffix=" + Uri.encode(suffix))
                },
            )
        }
        composable(
            route = Routes.WIZARD + "?suffix={suffix}",
            arguments = listOf(
                navArgument("suffix") {
                    type = NavType.StringType
                    defaultValue = ""
                }
            ),
        ) { backStackEntry ->
            val suffix = backStackEntry.arguments?.getString("suffix").orEmpty()
            WizardWebViewScreen(
                origin = origin,
                suffix = suffix,
                // On close/back-out: pop THIS destination, then let the activity
                // force-refresh the shared UpdatesViewModel (M2 return-refresh).
                onClose = {
                    navController.popBackStack()
                    onWizardReturn()
                },
            )
        }
        composable(Routes.LOCAL_MODEL_SETTINGS) {
            // On-device model settings: tune window/sampler, auto-warm, clear,
            // status -- wires to the existing ChatViewModel headless seams.
            val vm = chatViewModel ?: viewModel<ChatViewModel>()
            LocalModelSettingsScreen(viewModel = vm)
        }
    }
}

@Composable
private fun PlaceholderScreen(name: String) {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = name,
            style = MaterialTheme.typography.headlineMedium,
            color = MaterialTheme.colorScheme.onBackground
        )
    }
}
