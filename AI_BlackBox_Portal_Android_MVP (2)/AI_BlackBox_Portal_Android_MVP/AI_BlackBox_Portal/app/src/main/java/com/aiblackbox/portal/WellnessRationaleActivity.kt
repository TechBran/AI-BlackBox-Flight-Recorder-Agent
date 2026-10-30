package com.aiblackbox.portal

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.MarkdownText
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BlackBoxTheme

/**
 * The Health privacy policy, as Health Connect requires every app that reads health data to
 * show (spec §3.2, "Rationale screen"). Health Connect opens it from its own permission
 * screens: on Android 13 and lower through `androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE`,
 * on Android 14+ through the `VIEW_PERMISSION_USAGE` / `HEALTH_PERMISSIONS` activity-alias
 * (both in the manifest). The setup flow's "Your privacy" step shows the same text.
 *
 * The text is the bundled `res/raw/health_privacy.md`; the box serves its own copy at
 * `GET /wellness/privacy-policy`, and a PolicyHashTest on each side (wave 3) holds the two to
 * the same SHA-256.
 */
class WellnessRationaleActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(BbxBlack.toArgb()),
            navigationBarStyle = SystemBarStyle.dark(BbxBlack.toArgb()),
        )
        setContent {
            BlackBoxTheme {
                val policy = remember { loadPolicy(this) }
                Column(
                    Modifier
                        .fillMaxSize()
                        .background(BbxBlack)
                        .safeDrawingPadding()
                        .verticalScroll(rememberScrollState())
                        .padding(horizontal = 20.dp, vertical = 16.dp),
                ) {
                    MarkdownText(content = policy)
                    Spacer(Modifier.height(16.dp))
                    TextButton(onClick = { finish() }) { Text("Close", color = BbxAccent) }
                }
            }
        }
    }

    companion object {
        /** The bundled policy text (UTF-8). */
        fun loadPolicy(context: Context): String =
            context.resources.openRawResource(R.raw.health_privacy).use { it.readBytes().toString(Charsets.UTF_8) }
    }
}
