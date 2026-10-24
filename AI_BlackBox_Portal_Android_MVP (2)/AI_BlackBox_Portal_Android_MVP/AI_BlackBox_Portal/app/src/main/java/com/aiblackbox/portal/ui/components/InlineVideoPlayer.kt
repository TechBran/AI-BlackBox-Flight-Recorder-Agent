package com.aiblackbox.portal.ui.components

import android.net.Uri
import android.util.Log
import android.widget.MediaController
import android.widget.VideoView
import androidx.compose.runtime.Composable
import androidx.compose.runtime.key
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView

/**
 * An inline video player for an ABSOLUTE [url] (a box-relative "/ui/uploads/..." path
 * does not play: make it absolute first). The same VideoView + MediaController the media
 * browser uses; shows the first frame until played, and stops playback when it leaves
 * composition so a backgrounded screen does not keep streaming.
 */
@Composable
fun InlineVideoPlayer(url: String, modifier: Modifier = Modifier) {
    key(url) {
        AndroidView(
            modifier = modifier,
            factory = { ctx ->
                VideoView(ctx).apply {
                    setVideoURI(Uri.parse(url))
                    val controller = MediaController(ctx)
                    controller.setAnchorView(this)
                    setMediaController(controller)
                    setOnPreparedListener { mp ->
                        mp.isLooping = false
                        seekTo(100) // first frame as the thumbnail
                    }
                    setOnErrorListener { _, what, extra ->
                        Log.e("InlineVideoPlayer", "video error what=$what extra=$extra")
                        false
                    }
                    // Do NOT set a background color: it covers the video surface.
                }
            },
            onRelease = { it.stopPlayback() },
        )
    }
}
