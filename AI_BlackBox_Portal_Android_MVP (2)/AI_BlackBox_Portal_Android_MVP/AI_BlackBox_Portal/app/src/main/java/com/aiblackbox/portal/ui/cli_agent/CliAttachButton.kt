package com.aiblackbox.portal.ui.cli_agent

// CliAttachButton — 📎 attach-file button for the CLI-agent terminal's
// ExtraKeysBar (Android twin of the Portal web terminal's attach control).
//
// Tap → system multi-file picker → each file is uploaded SEQUENTIALLY to
// POST /cli-agent/zellij/attach-file, which stores it under the session's
// terminal-uploads folder and bracketed-pastes the absolute path into the
// zellij pane (the paste echoes back into this terminal via the WS — no
// client-side insertion needed).
//
// CONSTRAINTS (read before "improving"):
//  • Transient status renders in a zero-layout-space Popup chip, NEVER a
//    visible sibling row: ANY layout-size change in the bottom chrome
//    triggers a heavyweight debounced zellij session reflow (the banner and
//    mic-chip precedents in ZellijTerminalScreen/CliMicButton).
//  • The session name is read AT UPLOAD TIME via getSessionName() — zellij
//    SwitchedSession can drift the live session away from the launch name
//    mid-batch, and a stale capture would paste into the wrong session.
//  • Chat's silent Log.e-and-skip upload handling is FORBIDDEN here: every
//    failure surfaces to the user (Toast), then the batch continues.

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntRect
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Popup
import androidx.compose.ui.window.PopupPositionProvider
import androidx.compose.ui.window.PopupProperties
import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.ui.chat.MAX_UPLOAD_SIZE
import com.aiblackbox.portal.ui.chat.rememberMultiFilePicker
import com.aiblackbox.portal.ui.feedback.clickFeedback
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.io.IOException
import java.net.URLEncoder

/** How long the "📎 name attached" success chip stays up. */
internal const val ATTACH_FLASH_MS = 1_500L

/**
 * Request path for the attach-file upload. The operator rides as a
 * URL-ENCODED `op` query parameter — the established convention for every
 * /cli-agent call (see CliAgentSessionRepository); hyphenated/spaced
 * operator names must round-trip. Pure + [internal] for unit tests.
 */
internal fun attachRequestPath(operator: String): String =
    "/cli-agent/zellij/attach-file?op=" + URLEncoder.encode(operator, "UTF-8")

/** Chip label while a file uploads; multi-file batches show (i/n). */
internal fun uploadingChipText(fileName: String, index: Int, total: Int): String =
    if (total > 1) "Uploading $fileName… (${index + 1}/$total)" else "Uploading $fileName…"

/**
 * Basename-sanitize a picker DISPLAY_NAME before it becomes the temp file's
 * name — which [BlackBoxApi.uploadFile] sends as the multipart filename, so
 * it IS the name the backend stores and pastes (web-terminal parity demands
 * the untouched original basename). A hostile ContentProvider can return
 * path separators in DISPLAY_NAME; keep only the last segment (`/` and
 * `\`), falling back to "attachment" when nothing usable remains. The
 * backend defends independently — this just prevents the client-side
 * IOException from File(dir, "a/b"). Pure + [internal] for unit tests.
 */
internal fun sanitizeAttachBaseName(displayName: String?): String {
    val base = displayName.orEmpty()
        .substringAfterLast('/')
        .substringAfterLast('\\')
    return base.ifBlank { "attachment" }
}

/**
 * The user-visible surface an upload attempt resolves to:
 *   [Chip]  — transient success flash in the Popup chip (~[ATTACH_FLASH_MS]).
 *   [Notice] — a LONG Toast the user must not miss.
 */
internal sealed class AttachOutcome {
    data class Chip(val text: String) : AttachOutcome()
    data class Notice(val text: String) : AttachOutcome()
}

/**
 * Map one upload attempt to its outcome message. Pure + [internal] so the
 * chip-vs-toast decision is unit-testable:
 *   - [ApiHttpException] → its message verbatim (the FastAPI `detail`).
 *   - any other error    → "Upload failed: <msg>".
 *   - injected=true      → success chip flash.
 *   - injected=false     → upload stored but the pane paste failed; the
 *     user needs the server path to paste it themselves.
 */
internal fun attachOutcomeMessage(
    fileName: String,
    injected: Boolean,
    serverPath: String?,
    error: Exception?,
): AttachOutcome = when {
    error is ApiHttpException -> AttachOutcome.Notice(error.message ?: "Upload failed")
    error != null -> AttachOutcome.Notice("Upload failed: ${error.message ?: "unknown error"}")
    injected -> AttachOutcome.Chip("📎 $fileName attached")
    else -> AttachOutcome.Notice("Uploaded — paste failed. Path: ${serverPath ?: "(unknown)"}")
}

/**
 * 📎 attach button for the terminal extra-keys bar. Disabled (dimmed, taps
 * ignored) while [getSessionName] is null/blank or an upload batch runs.
 *
 * [getSessionName] must return the DRIFT-TRACKED live session name (the
 * ZellijWebSocketClient's effective name, which flips on SwitchedSession),
 * NOT the static launch name — and it is re-read per file, at upload time.
 */
@Composable
fun CliAttachButton(
    operator: String,
    getSessionName: () -> String?,
    api: BlackBoxApi,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var uploading by remember { mutableStateOf(false) }
    // Non-null → the Popup chip is up ("Uploading …" or the success flash).
    var chipText by remember { mutableStateOf<String?>(null) }

    fun notice(msg: String) {
        Toast.makeText(context, msg, Toast.LENGTH_LONG).show()
    }

    val pickFiles = rememberMultiFilePicker { uris ->
        scope.launch {
            uploading = true
            try {
                uris.forEachIndexed { index, uri ->
                    // Resolve DISPLAY_NAME + SIZE (fallback "attachment").
                    val (name, size) = withContext(Dispatchers.IO) {
                        resolveDisplayNameAndSize(context.contentResolver, uri)
                    }
                    // 500MB cap (mirrors the backend's own 413 limit): toast + skip.
                    if (size > MAX_UPLOAD_SIZE) {
                        notice("$name is too large (max 500MB) — skipped")
                        return@forEachIndexed
                    }
                    // Session-name-at-upload-time drift rule (see file header):
                    // read fresh per file, never captured at tap time.
                    val sessionName = getSessionName()
                    if (sessionName.isNullOrBlank()) {
                        notice("No active session — $name not uploaded")
                        return@forEachIndexed
                    }
                    chipText = uploadingChipText(name, index, uris.size)
                    var tempDir: File? = null
                    val outcome = try {
                        val temp = withContext(Dispatchers.IO) {
                            // Unique SUBDIR + original basename: uploadFile
                            // sends file.name as the multipart filename, and
                            // the backend stores AND PASTES that name — a
                            // timestamp-prefixed name would break web-terminal
                            // paste parity.
                            val dir = File(context.cacheDir, "attach_${System.currentTimeMillis()}")
                                .apply { mkdirs() }
                            tempDir = dir
                            val f = File(dir, sanitizeAttachBaseName(name))
                            context.contentResolver.openInputStream(uri)?.use { input ->
                                f.outputStream().use { output -> input.copyTo(output) }
                            } ?: throw IOException("Could not read $name")
                            f
                        }
                        val response = withContext(Dispatchers.IO) {
                            api.uploadFile(
                                attachRequestPath(operator),
                                temp,
                                fields = mapOf("session_name" to sessionName),
                            )
                        }
                        // {url, path, filename, session_folder, provider, injected}
                        val obj = runCatching { api.json.parseToJsonElement(response).jsonObject }
                            .getOrNull()
                        attachOutcomeMessage(
                            fileName = name,
                            injected = (obj?.get("injected") as? JsonPrimitive)?.booleanOrNull == true,
                            serverPath = (obj?.get("path") as? JsonPrimitive)?.contentOrNull,
                            error = null,
                        )
                    } catch (e: CancellationException) {
                        throw e // composition left mid-upload — not a failure to report
                    } catch (e: Exception) {
                        // Forbidden-silent-drop rule: EVERY failure becomes a
                        // Toast via attachOutcomeMessage — never a bare log.
                        attachOutcomeMessage(name, injected = false, serverPath = null, error = e)
                    } finally {
                        // NonCancellable: the temp dir (copy included) must be
                        // deleted even when the batch is cancelled mid-flight.
                        withContext(NonCancellable + Dispatchers.IO) { tempDir?.deleteRecursively() }
                    }
                    when (outcome) {
                        is AttachOutcome.Chip -> {
                            chipText = outcome.text
                            // Intentionally serializes multi-file batches so
                            // each file's success flash is visible.
                            delay(ATTACH_FLASH_MS)
                            chipText = null
                        }
                        is AttachOutcome.Notice -> {
                            chipText = null
                            notice(outcome.text)
                        }
                    }
                }
            } finally {
                uploading = false
                chipText = null
            }
        }
    }

    // Popup-not-sibling reflow rule (see file header): the chip anchors
    // centered above the button and occupies ZERO layout space.
    val density = LocalDensity.current
    val gapPx = with(density) { 6.dp.roundToPx() }
    val chipPositionProvider = remember(gapPx) {
        object : PopupPositionProvider {
            override fun calculatePosition(
                anchorBounds: IntRect,
                windowSize: IntSize,
                layoutDirection: LayoutDirection,
                popupContentSize: IntSize,
            ): IntOffset {
                val x = anchorBounds.left + (anchorBounds.width - popupContentSize.width) / 2
                val y = anchorBounds.top - popupContentSize.height - gapPx
                val maxX = (windowSize.width - popupContentSize.width).coerceAtLeast(0)
                return IntOffset(x.coerceIn(0, maxX), y.coerceAtLeast(0))
            }
        }
    }

    val shape = RoundedCornerShape(6.dp)
    // getSessionName() is non-snapshot state: no recomposition when it changes
    // (safe today — the zellij client's name is always non-blank from launch).
    val enabled = !getSessionName().isNullOrBlank()

    Box(
        modifier = modifier
            .defaultMinSize(minWidth = 44.dp, minHeight = 36.dp)
            .height(36.dp)
            .clip(shape)
            .background(MaterialTheme.colorScheme.surface)
            .border(BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant), shape)
            .clickFeedback(enabled = enabled && !uploading, onClick = { pickFiles() })
            .padding(horizontal = 8.dp),
        contentAlignment = Alignment.Center,
    ) {
        val chip = chipText
        if (chip != null) {
            Popup(
                popupPositionProvider = chipPositionProvider,
                properties = PopupProperties(focusable = false),
            ) {
                AttachStatusChip(text = chip)
            }
        }
        if (uploading) {
            CircularProgressIndicator(
                modifier = Modifier.size(20.dp),
                strokeWidth = 2.dp,
                color = MaterialTheme.colorScheme.primary,
            )
        } else {
            Text(
                text = "📎",
                fontSize = 16.sp,
                color = if (enabled) MaterialTheme.colorScheme.onSurfaceVariant
                else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.38f),
            )
        }
    }
}

/**
 * DISPLAY_NAME + SIZE from the ContentResolver, with the "attachment"
 * fallback (same shape as NativeMainActivity's picker resolution — those
 * helpers are private to the activity, so replicated here).
 */
private fun resolveDisplayNameAndSize(
    resolver: ContentResolver,
    uri: Uri,
): Pair<String, Long> {
    var name = "attachment"
    var size = 0L
    resolver.query(uri, null, null, null, null)?.use { cursor ->
        if (cursor.moveToFirst()) {
            val nameIdx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val sizeIdx = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (nameIdx >= 0) name = cursor.getString(nameIdx) ?: "attachment"
            if (sizeIdx >= 0) size = cursor.getLong(sizeIdx)
        }
    }
    return name to size
}

/** Status chip body — mirrors CliMicButton's TranscriptPreviewChip styling. */
@Composable
private fun AttachStatusChip(text: String) {
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.onSurface,
        tonalElevation = 3.dp,
        shadowElevation = 6.dp,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        modifier = Modifier.widthIn(max = 280.dp),
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        )
    }
}
