package com.aiblackbox.portal.ui.components

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withLink
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
    // The remembered AnnotatedString outlives recompositions; its links call the latest
    // callback rather than the one captured when the text was first built.
    val onSnapIdClickLatest by rememberUpdatedState(onSnapIdClick)
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
                    // A link span (ClickableText is deprecated): only the ID is the touch
                    // target, and accessibility services announce it as a link.
                    withLink(
                        LinkAnnotation.Clickable(
                            tag = "SNAP",
                            styles = TextLinkStyles(
                                style = SpanStyle(
                                    color = HighlightSnapshot,
                                    fontWeight = FontWeight.Bold,
                                ),
                            ),
                            linkInteractionListener = { onSnapIdClickLatest(snapId) },
                        ),
                    ) {
                        append(snapId)
                    }
                    lastEnd = match.range.last + 1
                }
                if (lastEnd < text.length) {
                    append(text.substring(lastEnd))
                }
            }
        }

        Text(
            text = annotated,
            style = MaterialTheme.typography.bodySmall.copy(
                fontFamily = FontFamily.Monospace,
                lineHeight = 20.sp,
                fontSize = 13.sp,
                color = BbxWhite,
            ),
        )
    }
}
