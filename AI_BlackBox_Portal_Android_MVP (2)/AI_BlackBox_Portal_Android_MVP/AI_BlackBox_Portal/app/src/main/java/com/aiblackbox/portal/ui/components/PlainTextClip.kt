package com.aiblackbox.portal.ui.components

import android.content.ClipData
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.toClipEntry

/**
 * The clip a copy button hands to [androidx.compose.ui.platform.LocalClipboard], which
 * replaced the deprecated LocalClipboardManager. It carries the same label and plain text
 * that ClipboardManager.setText(AnnotatedString(text)) wrote, so what the user pastes is
 * unchanged by the migration.
 */
internal fun plainTextClipEntry(text: String): ClipEntry =
    ClipData.newPlainText(PLAIN_TEXT_CLIP_LABEL, text).toClipEntry()

/** The label Compose's Android ClipboardManager.setText always used. */
internal const val PLAIN_TEXT_CLIP_LABEL = "plain text"
