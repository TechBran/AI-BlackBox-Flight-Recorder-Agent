package com.aiblackbox.portal

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BlackBoxTheme
import com.aiblackbox.portal.ui.wellness.WellnessScreen

/**
 * The native Settings, Health screen on its own, for the WebView Portal's "Manage on your
 * phone" (`AndroidBridge.wellnessOpenSettings()`, spec §6.3): health permissions and the
 * device key exist only natively, so the in-app Portal hands Health over to this screen and
 * Back returns to the Portal. Not exported: only this app starts it.
 */
class WellnessSettingsActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(BbxBlack.toArgb()),
            navigationBarStyle = SystemBarStyle.dark(BbxBlack.toArgb()),
        )
        setContent {
            BlackBoxTheme {
                Box(
                    Modifier
                        .fillMaxSize()
                        .background(BbxBlack)
                        .safeDrawingPadding(),
                ) {
                    WellnessScreen()
                }
            }
        }
    }
}
