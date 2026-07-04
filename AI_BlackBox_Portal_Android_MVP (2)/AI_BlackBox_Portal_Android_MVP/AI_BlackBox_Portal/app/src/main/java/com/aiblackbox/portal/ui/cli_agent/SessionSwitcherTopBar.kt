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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
// Codebase convention (CronManagerScreen, DeviceManagerScreen, MediaBrowserScreen):
// use `Icons.Default.*` — functionally identical to `Icons.Filled.*`.
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Close
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
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
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
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import com.aiblackbox.portal.ui.theme.CuWarning
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
 *   1. Live sessions (●/○ icon + provider · time · app-cwd + ⚡ YOLO badge),
 *      trailing ✕ → kill row (with confirm dialog).
 *   2. Divider.
 *   3. "+ Terminal" — starts a NEW zellij terminal session.
 *   4. "Shortcuts ▶" — nested submenu for Claude/Gemini/Codex/Antigravity/
 *      Grok; each row TAP starts a NEW session; its trailing amber ⚡
 *      launches a NEW session with permissions skipped (YOLO).
 *
 * Fresh-by-default (2026-07-03): EVERY launch mints a new concurrent
 * session — there is no tap-to-resume; reattaching goes through the
 * session rows above the divider.
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
    /**
     * Launch a NEW session for [provider] with permissions skipped (YOLO).
     * Wired to the trailing amber ⚡ button on each agent shortcut row —
     * the plain terminal entry deliberately has no YOLO affordance.
     * Default no-op keeps older call sites compiling.
     */
    onLaunchYolo: (provider: String) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val feedback = rememberPressFeedback()

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
                // T23 device QA: drop the 4dp vertical padding — the
                // IconButton/Box children already include their own
                // touch-target padding. Bar is now ~40dp tall instead of
                // ~56dp, giving the terminal more vertical room.
                .padding(horizontal = 4.dp)
                .height(40.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // ── Hamburger ──
            IconButton(
                onClick = { feedback(); onOpenNavDrawer() },
                modifier = Modifier.size(40.dp),
            ) {
                Icon(
                    imageVector = Icons.Default.Menu,
                    contentDescription = "Open navigation",
                )
            }

            // ── Label region (tappable) — flex grow ──
            Box(
                modifier = Modifier
                    .weight(1f)
                    .height(40.dp)
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
                IconButton(
                    onClick = { feedback(); dropdownExpanded = true },
                    modifier = Modifier.size(40.dp),
                ) {
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
                    // Wider than the Material default so the session label +
                    // X kill button both fit comfortably on phone portrait.
                    // Brandon's T23 UX ask (2026-05-26).
                    modifier = Modifier.widthIn(min = 280.dp),
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
                                        // dropdown auto-dismisses on slot
                                        // tap (Material 3 behavior).
                                        if (!isCurrent) onSelectSession(row)
                                    },
                                    onKillClick = {
                                        // X tap surfaces the same confirm
                                        // dialog as before (deliberate, kill
                                        // is destructive + no undo).
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

                    // 2) + Terminal (TAP = start a new terminal session; no
                    //    YOLO affordance for the plain terminal).
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
                            feedback()
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
                            onClick = { feedback(); shortcutsExpanded = true },
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
                                    // TAP the row = start a NEW session; tap the
                                    // trailing amber ⚡ = start a NEW session with
                                    // permissions skipped (YOLO). Mirrors the
                                    // proven tap-row-vs-trailing-icon idiom used
                                    // by the session list's kill (X) button — a
                                    // VISIBLE affordance, not a hidden long-press
                                    // (which T23 device QA flagged as
                                    // undiscoverable).
                                    trailingIcon = {
                                        YoloLaunchButton(
                                            enabled = !busy,
                                            contentDescription =
                                                yoloLaunchDescription(providerSlug),
                                            onClick = { onLaunchYolo(providerSlug) },
                                        )
                                    },
                                    enabled = !busy,
                                    onClick = {
                                        feedback()
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
                    feedback()
                    onKillSession(row)
                    pendingKill = null
                }) { Text("Kill") }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(onClick = { feedback(); pendingKill = null }) {
                    Text("Cancel")
                }
            },
        )
    }
}

/**
 * One row inside the session list.
 *
 * Layout: [●/○ indicator] [label] [✕ kill button]
 *
 * - Tap anywhere in the row's label area → [onClick] (switch session;
 *   Material 3 DropdownMenuItem auto-dismisses the dropdown).
 * - Tap the trailing ✕ → [onKillClick] (parent shows confirm dialog).
 *
 * T23 device QA pivot (2026-05-26): replaced the long-press → kill UX
 * with a visible X button. Three reasons:
 *   1. Brandon flagged that long-press was undiscoverable on the Z Fold 6.
 *   2. Switcher tap stopped working — the polish-pass `onClick = {} +
 *      combinedClickable` pattern silently swallowed tap events on real
 *      Android (the modifier-level handler wasn't reliably firing,
 *      possibly because DropdownMenuItem's internal clickable surface
 *      consumed the event first). Going back to slot `onClick` makes
 *      switching work AND re-enables Material 3 auto-dismiss.
 *   3. The original double-fire bug the polish was guarding against
 *      doesn't recur because we no longer layer combinedClickable on
 *      the same modifier — kill is a separate IconButton.
 */
@Composable
private fun SessionRowMenuItem(
    row: ZellijSessionRow,
    isCurrent: Boolean,
    nowMillis: Long,
    onClick: () -> Unit,
    onKillClick: () -> Unit,
) {
    val feedback = rememberPressFeedback()
    DropdownMenuItem(
        text = {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth(),
            ) {
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
                    modifier = Modifier.weight(1f),
                )
                // ⚡ YOLO badge — persistent (rebuilt from the server list's
                // `yolo` flag on every refresh; name-suffix fallback for
                // pre-field rows). A tinted vector, NOT the "⚡" emoji: U+26A1
                // has Emoji_Presentation=Yes, so a colored Text glyph renders
                // as the system's yellow color-emoji and ignores `color`.
                if (isYoloSession(row)) {
                    Spacer(Modifier.width(4.dp))
                    Icon(
                        imageVector = BoltIcon,
                        contentDescription = "YOLO session (permissions skipped)",
                        tint = CuWarning,
                        modifier = Modifier.size(16.dp),
                    )
                }
                Spacer(Modifier.width(4.dp))
                IconButton(
                    onClick = { feedback(); onKillClick() },
                    modifier = Modifier.size(36.dp),
                ) {
                    Icon(
                        imageVector = Icons.Default.Close,
                        contentDescription = "Kill ${row.name}",
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        },
        onClick = { feedback(); onClick() },
    )
}

/**
 * Trailing amber ⚡ YOLO button for an agent shortcut row. Tapping it
 * launches a NEW session of that provider with permissions skipped
 * ([onClick] → `onLaunchYolo`). The plain terminal entry never renders
 * this — YOLO only applies to agent CLIs.
 *
 * A VISIBLE affordance was chosen over a hidden long-press because T23
 * device QA found long-press undiscoverable on the Z Fold 6 (see the
 * SessionRowMenuItem kill-button note). This reuses the same proven
 * "tap row / trailing IconButton" idiom as the kill (X) button. Fires the
 * shared native press feedback so it feels identical to every other tappable.
 *
 * Uses the theme's [CuWarning] amber/orange (Color.kt) rather than a
 * hardcoded hex — the same warning tone the CU status surfaces use.
 *
 * The glyph is a tinted [BoltIcon] vector, NOT the "⚡" emoji: U+26A1 has
 * Emoji_Presentation=Yes, so `Text("⚡", color = …)` renders the system's
 * yellow color-emoji and silently drops the tint — which would make the
 * enabled and disabled states visually identical on a permission-bypass
 * control. The IconButton keeps a full 48dp interactive target (the glyph
 * itself is a compact 20dp) so this permission-bypass control clears the
 * 48dp minimum touch-target guidance on a phone.
 */
@Composable
internal fun YoloLaunchButton(
    enabled: Boolean,
    contentDescription: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val feedback = rememberPressFeedback()
    IconButton(
        onClick = { feedback(); onClick() },
        enabled = enabled,
        modifier = modifier
            .size(48.dp)
            .semantics { this.contentDescription = contentDescription },
    ) {
        Icon(
            imageVector = BoltIcon,
            contentDescription = null, // description is on the IconButton
            tint = if (enabled) CuWarning else MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(20.dp),
        )
    }
}

/**
 * A monochrome lightning-bolt vector (Material "Bolt" filled path) used for
 * the YOLO launch button and session badge. Built once and cached, mirroring
 * the material-icons builder idiom — we define it locally instead of pulling
 * in the whole `material-icons-extended` artifact for a single glyph, and it
 * is a template vector so [Icon]'s `tint` fully colors it (unlike the "⚡"
 * emoji, whose fixed color-emoji presentation ignores tint).
 */
private var _boltIcon: ImageVector? = null
private val BoltIcon: ImageVector
    get() {
        _boltIcon?.let { return it }
        return ImageVector.Builder(
            name = "Bolt",
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        ).apply {
            path(fill = SolidColor(Color.Black)) {
                moveTo(11f, 21f)
                horizontalLineToRelative(-1f)
                lineToRelative(1f, -7f)
                horizontalLineTo(7.5f)
                curveToRelative(-0.88f, 0f, -0.33f, -0.75f, -0.31f, -0.78f)
                curveTo(8.48f, 10.94f, 10.42f, 7.54f, 13f, 3f)
                horizontalLineToRelative(1f)
                lineToRelative(-1f, 7f)
                horizontalLineToRelative(3.5f)
                curveToRelative(0.49f, 0f, 0.56f, 0.33f, 0.47f, 0.51f)
                lineToRelative(-0.07f, 0.15f)
                curveTo(12.96f, 17.55f, 11f, 21f, 11f, 21f)
                close()
            }
        }.build().also { _boltIcon = it }
    }

// =========================================================================
// Pure helpers — exposed at package scope so SessionSwitcherTopBarTest.kt
// (plain JUnit, no Compose runtime) can exercise the time / casing logic.
// =========================================================================

/** Allowed provider shortcuts for the dropdown's nested menu. */
internal val PROVIDER_SHORTCUTS: List<String> =
    listOf("claude", "gemini", "codex", "antigravity", "grok")

/**
 * True when [row] is a YOLO (permissions-skipped) session and should show
 * the ⚡ badge. Primary signal is the server's `yolo` boolean from
 * GET /cli-agent/zellij/sessions (Task 2); the name-suffix check is the
 * fallback for rows synthesised before the field existed — YOLO session
 * names always end `_yolo` (which also matches the `__yolo` app-slug form).
 *
 * Known benign edge case: because the fallback is OR'd in, the server's
 * `yolo = false` cannot veto it, so a legacy resume-style session whose
 * app slug literally ends in `_yolo` (e.g. `op__claude__my_yolo`) would
 * badge ⚡ even though it isn't a YOLO session. This is cosmetic and
 * legacy-only: every session Android creates now is fork-named with a
 * `__{timestamp}` suffix (`…__{ts}` or `…__{ts}_yolo`), so a fresh row's
 * suffix reliably reflects its real YOLO state.
 */
internal fun isYoloSession(row: ZellijSessionRow): Boolean =
    row.yolo || row.name.endsWith("_yolo")

/** Accessibility/content description for a provider's ⚡ YOLO launch button. */
internal fun yoloLaunchDescription(providerSlug: String): String =
    "Launch ${titleCaseProvider(providerSlug)} with permissions skipped (YOLO)"

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
