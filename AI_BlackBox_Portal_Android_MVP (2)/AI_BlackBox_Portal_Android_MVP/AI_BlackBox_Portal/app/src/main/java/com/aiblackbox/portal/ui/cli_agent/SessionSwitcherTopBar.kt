package com.aiblackbox.portal.ui.cli_agent

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
// Codebase convention (CronManagerScreen, DeviceManagerScreen, MediaBrowserScreen):
// use `Icons.Default.*` — functionally identical to `Icons.Filled.*`.
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onLongClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.data.model.ZellijSessionRow
import android.widget.Toast
import java.time.Instant
import java.time.format.DateTimeParseException
import java.util.Locale
import kotlin.math.abs

/**
 * Phase 4 / T20 — session switcher top bar for the CLI Agent flow.
 *
 * Stateless: the hosting screen owns which session is current, which
 * sessions exist, and which launches are in flight. This composable
 * renders + emits intents only. That separation makes T22 (nav
 * integration) and T23 (instrumented testing) both straightforward.
 *
 * Layout:
 *   [☰ hamburger] | Provider · time            | [▼ chevron]
 *
 * Tap the label region OR the chevron → opens a [DropdownMenu] anchored
 * to the bar. Long-press the label → toasts the full deterministic
 * session name (e.g. "Brandon__claude__root__1779750372") for debugging.
 *
 * Dropdown order (exact, per brief):
 *   1. Live sessions (●/○ icon + provider · time · app-cwd),
 *      long-press → kill row.
 *   2. Divider.
 *   3. "+ Terminal" — launches a zellij terminal session.
 *   4. "⚡ Shortcuts ▶" — nested submenu for Claude/Gemini/Codex/Antigravity.
 *
 * Per-provider [launchInFlight] (Set<String>, NOT Boolean) so the user can
 * fire "+ Terminal" and "Shortcuts → Claude" in rapid succession and see
 * independent spinners on each row.
 *
 * Brief invariants:
 *   • Stateless top bar — see above.
 *   • Per-provider launchInFlight set.
 *   • No hardcoded operator — passed in.
 *   • No network calls — emits intents only.
 *   • Does NOT modify TerminalScreen, ZellijWebSocketClient, repository, DTOs.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun SessionSwitcherTopBar(
    operator: String,
    currentSession: ZellijSessionRow?,
    sessions: List<ZellijSessionRow>,
    onSelectSession: (ZellijSessionRow) -> Unit,
    onLaunchProvider: (provider: String) -> Unit,
    onKillSession: (ZellijSessionRow) -> Unit,
    onOpenNavDrawer: () -> Unit,
    launchInFlight: Set<String> = emptySet(),
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current

    var dropdownExpanded by remember { mutableStateOf(false) }
    var shortcutsExpanded by remember { mutableStateOf(false) }
    var pendingKill by remember { mutableStateOf<ZellijSessionRow?>(null) }

    // Tick every 30s so relative timestamps refresh.
    //
    // We intentionally run this UNCONDITIONALLY (not gated on
    // [dropdownExpanded]) because the bar label itself is time-relative —
    // e.g. "Claude · 2m ago" — and would freeze stale if we only ticked
    // while the dropdown was open. Capturing System.currentTimeMillis()
    // lazily inside [labelFor] otherwise produces an indefinite stale
    // render. Labels are bucketed (just now / Xs / Xm / Xh / Xd) so 30s is
    // overkill-precise but cheap (1 LaunchedEffect, no per-frame work).
    var nowMillis by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            kotlinx.coroutines.delay(30_000L)
            nowMillis = System.currentTimeMillis()
        }
    }

    Surface(
        modifier = modifier.fillMaxWidth(),
        color = MaterialTheme.colorScheme.surface,
        tonalElevation = 2.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(horizontal = 4.dp, vertical = 4.dp)
                .height(48.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // ── Hamburger ──
            IconButton(onClick = onOpenNavDrawer) {
                Icon(
                    imageVector = Icons.Default.Menu,
                    contentDescription = "Open navigation",
                )
            }

            // ── Label region (tappable) — flex grow ──
            Box(
                modifier = Modifier
                    .weight(1f)
                    .height(48.dp)
                    .combinedClickable(
                        onClick = { dropdownExpanded = true },
                        onLongClick = {
                            val full = currentSession?.name ?: "(no session)"
                            Toast.makeText(context, full, Toast.LENGTH_LONG).show()
                        },
                    )
                    // a11y: combinedClickable doesn't expose long-press to TalkBack
                    // by default. Declare Role.Button and an explicit long-click
                    // action with a label so screen readers can announce/invoke it.
                    .semantics {
                        role = Role.Button
                        onLongClick(label = "Show full session name") {
                            val full = currentSession?.name ?: "(no session)"
                            Toast.makeText(context, full, Toast.LENGTH_LONG).show()
                            true
                        }
                    }
                    .padding(horizontal = 8.dp),
                contentAlignment = Alignment.CenterStart,
            ) {
                Text(
                    text = labelFor(currentSession, nowMillis),
                    style = MaterialTheme.typography.titleMedium.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }

            // ── Chevron ──
            Box {
                IconButton(onClick = { dropdownExpanded = true }) {
                    Icon(
                        imageVector = Icons.Default.ArrowDropDown,
                        contentDescription = "Open session switcher",
                    )
                }

                DropdownMenu(
                    expanded = dropdownExpanded,
                    onDismissRequest = {
                        dropdownExpanded = false
                        shortcutsExpanded = false
                    },
                ) {
                    // 1) Existing sessions
                    if (sessions.isEmpty()) {
                        DropdownMenuItem(
                            text = {
                                Text(
                                    "No sessions",
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            },
                            onClick = {},
                            enabled = false,
                        )
                    } else {
                        // key(row.name) gives each row stable identity so a
                        // reordered/extended sessions list from the orchestrator
                        // doesn't invalidate every row. row.name is the
                        // deterministic zellij session id ("Brandon__claude__…").
                        sessions.forEach { row ->
                            key(row.name) {
                                val isCurrent = row.name == currentSession?.name
                                SessionRowMenuItem(
                                    row = row,
                                    isCurrent = isCurrent,
                                    nowMillis = nowMillis,
                                    onClick = {
                                        dropdownExpanded = false
                                        if (!isCurrent) onSelectSession(row)
                                    },
                                    onLongClick = {
                                        pendingKill = row
                                    },
                                )
                            }
                        }
                    }

                    HorizontalDivider(
                        modifier = Modifier.padding(vertical = 4.dp),
                        color = MaterialTheme.colorScheme.outlineVariant,
                    )

                    // 2) + Terminal
                    val terminalInFlight = "terminal" in launchInFlight
                    DropdownMenuItem(
                        text = { Text("+ Terminal") },
                        leadingIcon = {
                            LeadingLaunchIcon(
                                isLoading = terminalInFlight,
                                icon = Icons.Default.Add,
                            )
                        },
                        enabled = !terminalInFlight,
                        onClick = {
                            // Keep menu open while in-flight so the spinner is visible.
                            onLaunchProvider("terminal")
                        },
                    )

                    // 3) Shortcuts (nested submenu)
                    Box {
                        DropdownMenuItem(
                            text = { Text("Shortcuts") },
                            leadingIcon = {
                                Icon(
                                    imageVector = Icons.Default.PlayArrow,
                                    contentDescription = null,
                                )
                            },
                            trailingIcon = {
                                Icon(
                                    imageVector = Icons.Default.ArrowDropDown,
                                    contentDescription = null,
                                )
                            },
                            onClick = { shortcutsExpanded = true },
                        )
                        DropdownMenu(
                            expanded = shortcutsExpanded,
                            onDismissRequest = { shortcutsExpanded = false },
                        ) {
                            PROVIDER_SHORTCUTS.forEach { providerSlug ->
                                val busy = providerSlug in launchInFlight
                                DropdownMenuItem(
                                    text = { Text(titleCaseProvider(providerSlug)) },
                                    leadingIcon = {
                                        LeadingLaunchIcon(
                                            isLoading = busy,
                                            icon = Icons.Default.PlayArrow,
                                        )
                                    },
                                    enabled = !busy,
                                    onClick = {
                                        onLaunchProvider(providerSlug)
                                    },
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    // Brandon's UX choice (2026-05-26): destructive kill action confirmed via
    // AlertDialog rather than fired immediately on long-press. Reason: accidental
    // long-press in a touch-dense dropdown is too easy a footgun; no undo path
    // exists post-kill (zellij sessions can't be resurrected with their PTY state).
    // Spec reviewer flagged this as a deviation from the original "long-press → kill"
    // brief; Brandon explicitly chose to keep the confirm dialog.
    //
    // Implementation: a single tap-anywhere surface keeps T20 self-contained;
    // a richer ModalBottomSheet can land in T22. When confirmed we forward to
    // the screen's onKillSession.
    pendingKill?.let { row ->
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { pendingKill = null },
            title = { Text("Kill session?") },
            text = {
                Text(
                    text = row.name,
                    style = MaterialTheme.typography.bodySmall,
                )
            },
            confirmButton = {
                androidx.compose.material3.TextButton(onClick = {
                    onKillSession(row)
                    pendingKill = null
                }) { Text("Kill") }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { pendingKill = null }) {
                    Text("Cancel")
                }
            },
        )
    }
}

/** One row inside the session list. Indicator + label + long-press → kill. */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SessionRowMenuItem(
    row: ZellijSessionRow,
    isCurrent: Boolean,
    nowMillis: Long,
    onClick: () -> Unit,
    onLongClick: () -> Unit,
) {
    DropdownMenuItem(
        text = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = if (isCurrent) "●" else "○",  // ● vs ○
                    color = if (isCurrent) {
                        MaterialTheme.colorScheme.primary
                    } else {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    },
                    // a11y: TalkBack would otherwise pronounce "black large
                    // circle / white circle." Override with a status string.
                    modifier = Modifier.semantics {
                        contentDescription = if (isCurrent) {
                            "Current session"
                        } else {
                            "Inactive session"
                        }
                    },
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    text = sessionRowLabel(row, nowMillis),
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = if (isCurrent) FontWeight.SemiBold else FontWeight.Normal,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        },
        // BLOCKING fix (A): neutralise the DropdownMenuItem slot, keep the
        // combinedClickable modifier. Previously both fired on tap, so
        // onSelectSession ran twice → visible flicker + race against
        // dropdownExpanded = false.
        onClick = {},
        modifier = Modifier.combinedClickable(
            onClick = onClick,
            onLongClick = onLongClick,
        ),
    )
}

// =========================================================================
// Pure helpers — exposed at package scope so SessionSwitcherTopBarTest.kt
// (plain JUnit, no Compose runtime) can exercise the time / casing logic.
// =========================================================================

/** Allowed provider shortcuts for the dropdown's nested menu. */
internal val PROVIDER_SHORTCUTS: List<String> =
    listOf("claude", "gemini", "codex", "antigravity")

/**
 * Title-case the provider slug for display: "claude" → "Claude",
 * "antigravity" → "Antigravity". Empty / blank → "Session".
 *
 * Deliberately single-word: provider slugs are short ASCII identifiers
 * (see ZELLIJ_PROVIDER_SLUGS), so we don't need full Unicode title-casing.
 */
internal fun titleCaseProvider(slug: String): String {
    if (slug.isBlank()) return "Session"
    // Locale.ROOT pins behaviour to ASCII-stable casing (avoids Turkish locale
    // dotless-i, etc.) and silences AndroidLint's DefaultLocale warning.
    return slug.replaceFirstChar {
        if (it.isLowerCase()) it.titlecase(Locale.ROOT) else it.toString()
    }
}

/**
 * "Provider · time" — the top-bar label.
 *
 * - null session → "No session" (T22 empty state placeholder; T21 owns the
 *   richer empty-state UI, but the bar can still mount here harmlessly).
 * - Time is relative from [nowMillis] against [ZellijSessionRow.createdAt]
 *   (ISO-8601 from the orchestrator). If the timestamp is unparseable or
 *   absent, the time half is dropped, leaving the bare provider name.
 */
internal fun labelFor(session: ZellijSessionRow?, nowMillis: Long): String {
    if (session == null) return "No session"
    val provider = titleCaseProvider(session.provider)
    val rel = relativeTime(session.createdAt, nowMillis)
    return if (rel == null) provider else "$provider · $rel"
}

/** "Provider · time · app" — the dropdown row label. */
internal fun sessionRowLabel(row: ZellijSessionRow, nowMillis: Long): String {
    val provider = titleCaseProvider(row.provider)
    val rel = relativeTime(row.createdAt, nowMillis)
    val sb = StringBuilder(provider)
    if (rel != null) sb.append(" · ").append(rel)
    val app = row.app?.takeIf { it.isNotBlank() }
    if (app != null) sb.append(" · ").append(app)
    return sb.toString()
}

/**
 * Convert an ISO-8601 timestamp to a relative string: "just now",
 * "Xs ago", "Xm ago", "Xh ago", "Xd ago".
 *
 * Returns null if the input is null/blank/unparseable so the label can
 * gracefully degrade to the bare provider name. We swallow
 * [DateTimeParseException] rather than throwing because the backend may
 * pre-date the field (see [ZellijSessionRow.createdAt] = null path) and
 * we don't want one drifty row to crash the top bar.
 */
internal fun relativeTime(isoTimestamp: String?, nowMillis: Long): String? {
    if (isoTimestamp.isNullOrBlank()) return null
    val createdMillis = try {
        Instant.parse(isoTimestamp).toEpochMilli()
    } catch (_: DateTimeParseException) {
        return null
    } catch (_: IllegalArgumentException) {
        return null
    }
    val deltaSec = abs(nowMillis - createdMillis) / 1000L
    return when {
        deltaSec < 30L -> "just now"
        deltaSec < 60L -> "${deltaSec}s ago"
        deltaSec < 3600L -> "${deltaSec / 60L}m ago"
        deltaSec < 86_400L -> "${deltaSec / 3600L}h ago"
        else -> "${deltaSec / 86_400L}d ago"
    }
}
