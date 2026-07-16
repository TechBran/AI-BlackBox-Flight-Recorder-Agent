package com.aiblackbox.portal.ui.chat

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Stable
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.BbxWhite
import kotlin.math.roundToInt

internal const val RETURN_TO_LIVE_GAP_DP = 8
internal const val RETURN_TO_LIVE_TARGET_DP = 48

@Stable
internal class ReturnToLiveHostState {
    private var generation = 0L
    private var activeGeneration: Long? = null
    private var action: (() -> Unit)? = null

    var visible by mutableStateOf(false)
        private set
    var returning by mutableStateOf(false)
        private set

    fun register(
        owner: String,
        visible: Boolean,
        returning: Boolean,
        onResume: () -> Unit,
    ): Registration {
        val token = ++generation
        activeGeneration = token
        publish(token, visible, returning, onResume)
        return Registration(this, token, owner, onResume)
    }

    fun resume() = action?.invoke()

    private fun publish(token: Long, visible: Boolean, returning: Boolean, onResume: () -> Unit) {
        if (activeGeneration != token) return
        this.visible = visible
        this.returning = returning
        action = onResume
    }

    private fun dispose(token: Long) {
        if (activeGeneration != token) return
        activeGeneration = null
        visible = false
        returning = false
        action = null
    }

    internal class Registration internal constructor(
        private val host: ReturnToLiveHostState,
        private val token: Long,
        @Suppress("unused") private val owner: String,
        private var onResume: () -> Unit,
    ) {
        fun publish(visible: Boolean, returning: Boolean, onResume: () -> Unit = this.onResume) {
            this.onResume = onResume
            host.publish(token, visible, returning, onResume)
        }

        fun dispose() = host.dispose(token)
    }
}

internal val LocalReturnToLiveHost = compositionLocalOf<ReturnToLiveHostState?> { null }

@Composable
internal fun BoxScope.ReturnToLiveHost(
    state: ReturnToLiveHostState,
    composerTopPx: Float,
) {
    if (!state.visible || !composerTopPx.isFinite()) return
    val density = androidx.compose.ui.platform.LocalDensity.current
    val targetBottomPx = composerTopPx - with(density) { RETURN_TO_LIVE_GAP_DP.dp.toPx() }
    IconButton(
        onClick = state::resume,
        // EXPLICIT colors — web-parity solid red circle + white glyph. A bare
        // IconButton here resolves its content color from LocalContentColor,
        // which defaults to Color.Black at this Surface-less activity layer:
        // a black arrow on a transparent button over the BbxBlack root, i.e.
        // composed and clickable but pixel-invisible in every state.
        colors = IconButtonDefaults.iconButtonColors(
            containerColor = BbxRed,
            contentColor = BbxWhite,
        ),
        modifier = Modifier
            .align(Alignment.TopEnd)
            .padding(end = 12.dp)
            .offset {
                IntOffset(0, (targetBottomPx - with(density) { RETURN_TO_LIVE_TARGET_DP.dp.toPx() }).roundToInt())
            }
            .size(RETURN_TO_LIVE_TARGET_DP.dp)
            .testTag("return-to-live"),
    ) {
        Icon(Icons.Default.KeyboardArrowDown, contentDescription = "Return to live")
    }
}
