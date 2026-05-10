package com.aiblackbox.portal.ui.components

import androidx.compose.foundation.text.ClickableText
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.HighlightSnapshot

/**
 * Renders snapshot text with clickable SNAP-XXXXX IDs highlighted in purple.
 * Matches Portal makeSnapshotsClickable() — regex: \b(SNAP-(?:\d{8}-)?\d+)\b
 *
 * Used by TimelineScreen.SnapshotDetail and SnapshotPeekSheet so both surfaces
 * share one source of truth for SNAP-ID detection and styling.
 */
@Composable
fun ClickableSnapshotContent(
    text: String,
    onSnapIdClick: (String) -> Unit,
) {
    val snapPattern = remember { Regex("""\b(SNAP-(?:\d{8}-)?\d+)\b""", RegexOption.IGNORE_CASE) }
    val matches = remember(text) { snapPattern.findAll(text).toList() }

    if (matches.isEmpty()) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall.copy(
                fontFamily = FontFamily.Monospace,
                lineHeight = 20.sp,
                fontSize = 13.sp,
            ),
            color = BbxWhite,
        )
    } else {
        val annotated = remember(text) {
            buildAnnotatedString {
                var lastEnd = 0
                matches.forEach { match ->
                    if (match.range.first > lastEnd) {
                        append(text.substring(lastEnd, match.range.first))
                    }
                    val snapId = match.value
                    pushStringAnnotation(tag = "SNAP", annotation = snapId)
                    withStyle(
                        SpanStyle(
                            color = HighlightSnapshot,
                            fontWeight = FontWeight.Bold,
                        ),
                    ) {
                        append(snapId)
                    }
                    pop()
                    lastEnd = match.range.last + 1
                }
                if (lastEnd < text.length) {
                    append(text.substring(lastEnd))
                }
            }
        }

        @Suppress("DEPRECATION")
        ClickableText(
            text = annotated,
            style = MaterialTheme.typography.bodySmall.copy(
                fontFamily = FontFamily.Monospace,
                lineHeight = 20.sp,
                fontSize = 13.sp,
                color = BbxWhite,
            ),
            onClick = { offset ->
                annotated.getStringAnnotations(tag = "SNAP", start = offset, end = offset)
                    .firstOrNull()?.let { annotation ->
                        onSnapIdClick(annotation.item)
                    }
            },
        )
    }
}
