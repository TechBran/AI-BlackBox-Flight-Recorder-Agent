package com.aiblackbox.portal.ui.chat

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.glassSurface

// =============================================================================
// FileAttachment — aligned with Portal file-upload.js
//
// Portal features:
//   MAX_UPLOAD_SIZE: 500MB
//   Preview: images (IMG), videos (VIDEO muted), audio (AUDIO), documents (📄)
//   Preview container: horizontal scroll, 72px thumbnails, ✕ remove button
//   Upload: POST /upload with XHR progress tracking
//
// Android enhancement: uses Coil for image loading, native file picker,
// glass surface styling for preview container
// =============================================================================

/** Maximum upload size in bytes (matches Portal MAX_UPLOAD_SIZE = 500MB) */
const val MAX_UPLOAD_SIZE = 500L * 1024 * 1024

/** File picker launcher — opens system file picker for any file type */
@Composable
fun rememberFilePicker(
    onFilePicked: (Uri) -> Unit
): () -> Unit {
    val launcher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        uri?.let { onFilePicked(it) }
    }
    return { launcher.launch("*/*") }
}

/** Multi-file picker launcher */
@Composable
fun rememberMultiFilePicker(
    onFilesPicked: (List<Uri>) -> Unit
): () -> Unit {
    val launcher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetMultipleContents()
    ) { uris: List<Uri> ->
        if (uris.isNotEmpty()) onFilesPicked(uris)
    }
    return { launcher.launch("*/*") }
}

/**
 * Horizontal preview strip for attached files, shown above the composer.
 * Matches Portal's preview container with thumbnails and remove buttons.
 */
@Composable
fun AttachmentPreview(
    attachments: List<AttachmentItem>,
    onRemove: (Int) -> Unit,
    modifier: Modifier = Modifier
) {
    if (attachments.isEmpty()) return

    LazyRow(
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
        contentPadding = PaddingValues(horizontal = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        items(attachments.size) { index ->
            val item = attachments[index]
            AttachmentThumbnail(item = item, onRemove = { onRemove(index) })
        }
    }
}

/** Single attachment thumbnail with type-specific preview */
@Composable
private fun AttachmentThumbnail(
    item: AttachmentItem,
    onRemove: () -> Unit
) {
    Box(modifier = Modifier.size(80.dp)) {
        when {
            item.isImage -> {
                // Image: thumbnail via Coil
                AsyncImage(
                    model = item.uri,
                    contentDescription = item.name,
                    modifier = Modifier
                        .size(80.dp)
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(Neutral200)
                        .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd)),
                    contentScale = ContentScale.Crop
                )
            }
            item.isVideo -> {
                // Video: icon with type label
                FileTypePreview(
                    icon = "\uD83C\uDFA5",
                    label = "Video",
                    name = item.name,
                    sizeText = item.sizeFormatted
                )
            }
            item.isAudio -> {
                // Audio: icon with type label
                FileTypePreview(
                    icon = "\uD83C\uDFB5",
                    label = "Audio",
                    name = item.name,
                    sizeText = item.sizeFormatted
                )
            }
            else -> {
                // Document: generic file icon
                FileTypePreview(
                    icon = "\uD83D\uDCC4",
                    label = item.extension.uppercase().ifEmpty { "File" },
                    name = item.name,
                    sizeText = item.sizeFormatted
                )
            }
        }

        // Remove button (matches Portal ✕ overlay)
        Box(
            modifier = Modifier
                .align(Alignment.TopEnd)
                .size(22.dp)
                .clip(CircleShape)
                .background(BbxAccent)
                .clickFeedback(onClick = onRemove),
            contentAlignment = Alignment.Center
        ) {
            Text(
                text = "\u00D7",
                color = BbxWhite,
                fontSize = 14.sp,
                fontWeight = FontWeight.Bold
            )
        }
    }
}

/** Generic file type preview card for non-image files */
@Composable
private fun FileTypePreview(
    icon: String,
    label: String,
    name: String,
    sizeText: String
) {
    Column(
        modifier = Modifier
            .size(80.dp)
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral100)
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
            .padding(6.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text(text = icon, fontSize = 22.sp)
        Spacer(Modifier.height(2.dp))
        Text(
            text = name,
            style = MaterialTheme.typography.labelSmall,
            color = BbxWhite,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            fontSize = 9.sp
        )
        Text(
            text = sizeText,
            style = MaterialTheme.typography.labelSmall,
            color = Neutral500,
            fontSize = 8.sp
        )
    }
}

// =============================================================================
// Data model
// =============================================================================

data class AttachmentItem(
    val uri: Uri,
    val name: String,
    val mimeType: String,
    val sizeBytes: Long = 0,
) {
    val isImage: Boolean get() = mimeType.startsWith("image/")
    val isVideo: Boolean get() = mimeType.startsWith("video/")
    val isAudio: Boolean get() = mimeType.startsWith("audio/")
    val isDocument: Boolean get() = !isImage && !isVideo && !isAudio

    val extension: String get() = name.substringAfterLast('.', "")

    val sizeFormatted: String get() = when {
        sizeBytes <= 0 -> ""
        sizeBytes < 1024 -> "${sizeBytes}B"
        sizeBytes < 1024 * 1024 -> "${sizeBytes / 1024}KB"
        else -> "${"%.1f".format(sizeBytes / (1024.0 * 1024.0))}MB"
    }

    val isTooLarge: Boolean get() = sizeBytes > MAX_UPLOAD_SIZE
}
