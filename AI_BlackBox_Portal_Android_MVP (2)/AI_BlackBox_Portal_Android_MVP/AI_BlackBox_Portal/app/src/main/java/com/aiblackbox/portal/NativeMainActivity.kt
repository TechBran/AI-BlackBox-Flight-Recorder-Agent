package com.aiblackbox.portal

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.ui.graphics.toArgb
import com.aiblackbox.portal.navigation.BlackBoxApp
import com.aiblackbox.portal.ui.theme.BlackBoxTheme
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.util.normalizeApiOrigin
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Build a properly JSON-serialized TTS batch request body. No manual string escaping.
 * No model travels: /tts/batch synthesises each provider with its own model (the
 * `model` GET /tts/catalog publishes per group), so this path keeps no copy of a
 * provider's model ids to go stale (audit O07-tts-voice-06).
 */
internal fun buildTtsBatchBody(
    text: String, voice: String, format: String, provider: String, operator: String
): String = buildJsonObject {
    // §3.5: strip non-speakable content (artifacts/media-urls/fenced-code/
    // {ui_reply} envelope) before TTS -- mirrors Portal stripNonSpeakable.
    put("text", com.aiblackbox.portal.util.SpeakableText.stripNonSpeakable(text))
    put("voice", voice)
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

    override fun onDestroy() {
        super.onDestroy()
        com.aiblackbox.portal.data.voice.AudioPlaybackManager.release()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // BlackBox-black bars with light icons. Android 15+ draws every app edge-to-edge and
        // ignores bar colours (Window.statusBarColor/navigationBarColor are deprecated there);
        // below it, enableEdgeToEdge sets the colours the BlackBoxTheme SideEffect used to.
        // dark() also leaves nav-bar contrast unenforced, as Theme.AIBlackBox.NoBar asks.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(BbxBlack.toArgb()),
            navigationBarStyle = SystemBarStyle.dark(BbxBlack.toArgb()),
        )

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
                // The whole app: navigation, its chrome, the composer and the settings sheet
                // (rank 76). The mic grant stays here: it needs the activity-result launcher.
                BlackBoxApp(origin = origin, withMicPermission = ::withMicPermission)
            }
        }
    }
}
