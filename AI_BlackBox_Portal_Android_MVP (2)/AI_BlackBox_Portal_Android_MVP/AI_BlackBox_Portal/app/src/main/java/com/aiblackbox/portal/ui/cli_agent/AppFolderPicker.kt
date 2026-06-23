package com.aiblackbox.portal.ui.cli_agent

// AppFolderPicker — LazyColumn of registered apps with live-dot indicator.
//
// Phase 6.3 entry-point Composable for the CLI Agent flow:
//  - Special "✦ + New app workspace" row at top (maps to Apps/ root)
//  - Cross-references /agent/apps + /cli-agent/sessions for green/grey dots
//  - Long-press on a live row → action sheet (Reconnect / Kill session)
//  - Kill confirmation dialog with explicit warning text
//  - Loading + empty + refresh states
//
// Public API documented at the @Composable below. Caller (CliAgentScreen,
// Task 7.2) provides a CliAgentSessionRepository, the current operator,
// and an onAppSelected callback to navigate into TerminalScreen.

import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.data.model.CliAgentApp
import com.aiblackbox.portal.data.model.CliAgentSessionEntry
import com.aiblackbox.portal.data.model.CliAgentProvider
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import kotlinx.coroutines.launch

private val LiveDotColor = Color(0xFF27D980)   // Solid green — matches theme.SolidGreen
private val IdleDotColor = Color(0xFF6B7280)   // Neutral grey

/**
 * Extract the app slug from a CLI Agent session id.
 *
 * session_id = "cli-agent-<op>__<provider>__<slug>"
 * Returns "" for the Apps/ root marker ("_root"), null on malformed ids.
 */
internal fun extractAppSlug(sessionId: String): String? {
    if (!sessionId.startsWith("cli-agent-")) return null
    val parts = sessionId.removePrefix("cli-agent-").split("__")
    if (parts.size != 3) return null
    val slug = parts[2]
    return if (slug == "_root") "" else slug
}

/**
 * Derive an app slug from a CliAgentApp's directory.
 *
 * Slug is the basename of the absolute directory (e.g. "/Apps/grocery-store"
 * → "grocery-store"). Empty string means the Apps/ root row.
 */
internal fun appSlugFor(app: CliAgentApp): String {
    val dir = app.directory.trimEnd('/')
    if (dir.isEmpty()) return ""
    return dir.substringAfterLast('/')
}

/**
 * Internal state describing one row in the picker.
 */
private data class AppRowState(
    val app: CliAgentApp,
    val slug: String,
    val live: Boolean,
)

/**
 * Entry-point Composable for the CLI Agent flow.
 *
 * @param repository Source of /agent/apps and /cli-agent/sessions data, plus kill.
 * @param operator   Current operator name (used to scope sessions + build session ids).
 * @param onAppSelected Tap callback — navigate to TerminalScreen with (slug, displayName).
 *                      Slug is "" for the Apps root pick.
 * @param onAppsRootSelected Callback for the special "✦ + New app workspace" row.
 *                           Defaults to onAppSelected("", "Apps root").
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppFolderPicker(
    repository: CliAgentSessionRepository,
    operator: String,
    selectedProvider: CliAgentProvider,
    onProviderSelected: (CliAgentProvider) -> Unit,
    onAppSelected: (appSlug: String, appName: String) -> Unit,
    onAppsRootSelected: () -> Unit = { onAppSelected("", "Apps root") },
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val feedback = rememberPressFeedback()

    var loading by remember { mutableStateOf(true) }
    var rows by remember { mutableStateOf(emptyList<AppRowState>()) }
    var refreshTick by remember { mutableStateOf(0) }

    // Long-press action sheet target (null = sheet hidden)
    var actionTarget: AppRowState? by remember { mutableStateOf(null) }
    // Kill confirmation target (null = dialog hidden)
    var killTarget: AppRowState? by remember { mutableStateOf(null) }

    // Initial + on-operator-change + on-refresh-tick load.
    LaunchedEffect(operator, selectedProvider, refreshTick) {
        loading = true
        try {
            val apps = repository.listApps()
            val sessions: List<CliAgentSessionEntry> = try {
                repository.listSessions(operator)
            } catch (t: Throwable) {
                Toast.makeText(
                    context,
                    "Couldn't load CLI Agent sessions: ${t.message ?: "unknown error"}",
                    Toast.LENGTH_SHORT,
                ).show()
                emptyList()
            }
            val liveSlugs: Set<String> = sessions
                .filter { it.sessionId.contains("__${selectedProvider.slug}__") }
                .mapNotNull { extractAppSlug(it.sessionId) }
                .toSet()
            rows = apps.map { app ->
                val slug = appSlugFor(app)
                AppRowState(app = app, slug = slug, live = liveSlugs.contains(slug))
            }
        } catch (t: Throwable) {
            rows = emptyList()
            Toast.makeText(
                context,
                "Couldn't load apps: ${t.message ?: "unknown error"}",
                Toast.LENGTH_SHORT,
            ).show()
        } finally {
            loading = false
        }
    }

    Surface(
        modifier = modifier.fillMaxSize(),
        color = MaterialTheme.colorScheme.background,
    ) {
        Column(modifier = Modifier.fillMaxSize()) {
            AgentProviderChips(
                selected = selectedProvider,
                onSelect = onProviderSelected,
            )
            HorizontalDivider(
                color = MaterialTheme.colorScheme.outlineVariant,
                thickness = 0.5.dp,
            )
            when {
                loading && rows.isEmpty() -> LoadingState()
                else -> PickerList(
                    rows = rows,
                    onAppsRootSelected = onAppsRootSelected,
                    onAppSelected = onAppSelected,
                    onLongPressLive = { row -> actionTarget = row },
                )
            }
        }
    }

    // --- Long-press action sheet (Reconnect / Kill) ---
    val target = actionTarget
    if (target != null) {
        val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
        ModalBottomSheet(
            onDismissRequest = { actionTarget = null },
            sheetState = sheetState,
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 20.dp, vertical = 12.dp),
            ) {
                Text(
                    text = target.app.name,
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Text(
                    text = target.app.directory,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontFamily = FontFamily.Monospace,
                )
                Spacer(Modifier.height(16.dp))

                ActionRow(
                    label = "Reconnect",
                    color = MaterialTheme.colorScheme.onSurface,
                ) {
                    actionTarget = null
                    onAppSelected(target.slug, target.app.name)
                }
                ActionRow(
                    label = "Kill session",
                    color = MaterialTheme.colorScheme.error,
                ) {
                    actionTarget = null
                    killTarget = target
                }
                Spacer(Modifier.height(12.dp))
            }
        }
    }

    // --- Kill confirmation dialog ---
    val confirm = killTarget
    if (confirm != null) {
        AlertDialog(
            onDismissRequest = { killTarget = null },
            title = {
                Text(
                    text = "Kill ${confirm.app.name}?",
                    style = MaterialTheme.typography.titleMedium,
                )
            },
            text = {
                Text(
                    text = "Kill ${confirm.app.name}? In-flight commands will terminate.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    feedback()
                    val toKill = confirm
                    killTarget = null
                    val sessionId = cliAgentSessionId(
                        operator = operator,
                        provider = selectedProvider.slug,
                        appSlug = toKill.slug,
                    )
                    scope.launch {
                        val killed = try {
                            repository.killSession(sessionId)
                        } catch (t: Throwable) {
                            Toast.makeText(
                                context,
                                "Kill failed: ${t.message ?: "unknown error"}",
                                Toast.LENGTH_SHORT,
                            ).show()
                            false
                        }
                        if (killed) {
                            Toast.makeText(
                                context,
                                "${toKill.app.name} session terminated",
                                Toast.LENGTH_SHORT,
                            ).show()
                        } else {
                            Toast.makeText(
                                context,
                                "No active session to kill",
                                Toast.LENGTH_SHORT,
                            ).show()
                        }
                        // Refresh list either way (idempotent kill returns false on miss).
                        refreshTick++
                    }
                }) {
                    Text(
                        text = "Kill",
                        color = MaterialTheme.colorScheme.error,
                        fontWeight = FontWeight.SemiBold,
                    )
                }
            },
            dismissButton = {
                TextButton(onClick = { feedback(); killTarget = null }) {
                    Text("Cancel")
                }
            },
        )
    }
}

@Composable
private fun LoadingState() {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PickerList(
    rows: List<AppRowState>,
    onAppsRootSelected: () -> Unit,
    onAppSelected: (String, String) -> Unit,
    onLongPressLive: (AppRowState) -> Unit,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
    ) {
        // Always-visible Apps/ root row at the top.
        item(key = "__apps_root__") {
            AppsRootRow(onClick = onAppsRootSelected)
            HorizontalDivider(
                color = MaterialTheme.colorScheme.outlineVariant,
                thickness = 0.5.dp,
            )
        }

        if (rows.isEmpty()) {
            item(key = "__empty__") {
                EmptyStateCard()
            }
        } else {
            items(items = rows, key = { it.app.id.ifEmpty { it.slug } }) { row ->
                AppRow(
                    row = row,
                    onTap = { onAppSelected(row.slug, row.app.name) },
                    onLongPress = {
                        if (row.live) onLongPressLive(row)
                    },
                )
                HorizontalDivider(
                    color = MaterialTheme.colorScheme.outlineVariant,
                    thickness = 0.5.dp,
                )
            }
        }
    }
}

/**
 * "✦ + New app workspace" row, always at the top of the list.
 * Tapping it scaffolds a new app from the Apps/ root.
 */
@Composable
private fun AppsRootRow(onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .pointerInput(Unit) {
                detectTapGestures(onTap = { onClick() })
            }
            .padding(horizontal = 16.dp, vertical = 14.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = "✦",  // ✦
                style = MaterialTheme.typography.titleLarge,
                color = MaterialTheme.colorScheme.primary,
            )
            Spacer(Modifier.size(12.dp))
            Column(modifier = Modifier.weight(1f, fill = true)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = "+ New app workspace",
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSurface,
                        fontWeight = FontWeight.Medium,
                    )
                    Spacer(Modifier.weight(1f))
                    Text(
                        text = "/Apps",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontFamily = FontFamily.Monospace,
                    )
                }
                Text(
                    text = "scaffold a new app, work across apps",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/**
 * Standard registered-app row. Live dot on the left, name + directory in
 * the body, "active" label on the right when there's a live session.
 */
@Composable
private fun AppRow(
    row: AppRowState,
    onTap: () -> Unit,
    onLongPress: () -> Unit,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .pointerInput(row.app.id, row.live) {
                detectTapGestures(
                    onTap = { onTap() },
                    onLongPress = { onLongPress() },
                )
            }
            .padding(horizontal = 16.dp, vertical = 14.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // Live dot (green = live, grey = idle)
            Box(
                modifier = Modifier
                    .size(10.dp)
                    .clip(CircleShape)
                    .background(if (row.live) LiveDotColor else IdleDotColor),
            )
            Spacer(Modifier.size(14.dp))
            Column(modifier = Modifier.weight(1f, fill = true)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = row.app.name,
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSurface,
                        fontWeight = FontWeight.Medium,
                    )
                    Spacer(Modifier.weight(1f))
                    if (row.live) {
                        Text(
                            text = "active",
                            style = MaterialTheme.typography.labelSmall,
                            color = LiveDotColor,
                            fontWeight = FontWeight.SemiBold,
                            fontSize = 12.sp,
                        )
                    }
                }
                Text(
                    text = row.app.directory,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontFamily = FontFamily.Monospace,
                )
            }
        }
    }
}

@Composable
private fun EmptyStateCard() {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(24.dp),
        contentAlignment = Alignment.Center,
    ) {
        Surface(
            shape = RoundedCornerShape(12.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            tonalElevation = 1.dp,
        ) {
            Column(
                modifier = Modifier.padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    text = "No apps registered yet",
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Text(
                    text = "Use /register-app from chat or POST to /agent/apps/register.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/**
 * Single tap-target row inside the action sheet. Full-width, comfortable
 * touch target. `color` lets us tint the destructive "Kill session" entry.
 */
@Composable
private fun ActionRow(
    label: String,
    color: Color,
    onClick: () -> Unit,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(48.dp)
            .pointerInput(label) {
                detectTapGestures(onTap = { onClick() })
            },
        contentAlignment = Alignment.CenterStart,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyLarge,
            color = color,
            fontWeight = FontWeight.Medium,
        )
    }
}

/**
 * Horizontal segmented-control row of available CLI Agent providers.
 * Tap a chip to switch the active provider. Selection state is hoisted
 * to the caller (CliAgentScreen owns it via BlackBoxStore).
 */
@Composable
internal fun AgentProviderChips(
    selected: CliAgentProvider,
    onSelect: (CliAgentProvider) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(start = 12.dp, end = 12.dp, top = 96.dp, bottom = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        CliAgentProvider.values().forEach { provider ->
            val isSelected = provider == selected
            Surface(
                shape = RoundedCornerShape(20.dp),
                color = if (isSelected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.surfaceVariant,
                tonalElevation = if (isSelected) 4.dp else 0.dp,
                modifier = Modifier
                    .height(36.dp)
                    .pointerInput(provider) {
                        detectTapGestures(onTap = { onSelect(provider) })
                    },
            ) {
                Box(
                    modifier = Modifier.padding(horizontal = 16.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = provider.display,
                        color = if (isSelected) MaterialTheme.colorScheme.onPrimary
                                else MaterialTheme.colorScheme.onSurfaceVariant,
                        fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Medium,
                        style = MaterialTheme.typography.labelLarge,
                    )
                }
            }
        }
    }
}
