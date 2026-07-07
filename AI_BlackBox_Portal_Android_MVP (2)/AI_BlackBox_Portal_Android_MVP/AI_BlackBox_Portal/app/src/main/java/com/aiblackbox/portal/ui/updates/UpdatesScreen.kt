@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.aiblackbox.portal.ui.updates

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import com.aiblackbox.portal.data.model.EmbeddingsJob
import com.aiblackbox.portal.data.model.EmbeddingsStatus
import com.aiblackbox.portal.data.model.RerankStatus
import com.aiblackbox.portal.data.model.UpdateCommit
import com.aiblackbox.portal.data.model.UpdateStatus
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxSurface
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import kotlinx.coroutines.delay

// State-specific accent colors mirroring the web Portal's _updates.css
private val OkGreen = Color(0xFF50C878)
private val WarnAmber = Color(0xFFF4A460)
private val ErrRed = Color(0xFFE57373)
private val InfoBlue = Color(0xFF6CA0DC)

@Composable
fun UpdatesScreen(
    origin: String,
    // B5: passed in from the activity-scoped instance (NativeMainActivity →
    // BlackBoxNavGraph) so the top-bar badge and this screen share ONE VM.
    // No `= viewModel()` default: a screen-scoped instance would diverge from
    // the badge's and re-fetch independently.
    viewModel: UpdatesViewModel,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsState()
    val logLines by viewModel.logLines.collectAsState()
    val logModalOpen by viewModel.logModalOpen.collectAsState()
    val restartLabel by viewModel.restartPollLabel.collectAsState()
    val embeddings by viewModel.embeddings.collectAsState()
    val embeddingsUpdateInFlight by viewModel.embeddingsUpdateInFlight.collectAsState()
    val embeddingsError by viewModel.embeddingsError.collectAsState()
    val rerankStatus by viewModel.rerankStatus.collectAsState()

    val snackbarHostState = remember { SnackbarHostState() }
    val view = LocalView.current
    val context = LocalContext.current

    // B5: initialize is now driven once at activity scope (NativeMainActivity),
    // so this screen does NOT re-init — the shared VM is already populated.

    // 5s job-progress poll while a migration runs and this screen is visible
    // (parity with the Portal card's interval). Keyed on the running flag:
    // disposal or the job finishing cancels/ends the loop automatically.
    val embeddingsJobRunning = embeddings?.job?.state == "running"
    LaunchedEffect(embeddingsJobRunning) {
        while (embeddingsJobRunning) {
            delay(5000)
            viewModel.refreshEmbeddings()
        }
    }

    // One-shot error surface for the [Update] POST (snackbar host already
    // lives in this Scaffold).
    LaunchedEffect(embeddingsError) {
        embeddingsError?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearEmbeddingsError()
        }
    }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = BbxBlack,
        snackbarHost = { SnackbarHost(snackbarHostState) },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(start = 16.dp, end = 16.dp, top = 100.dp, bottom = 16.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            // Title row + Re-check button
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("Updates", style = MaterialTheme.typography.headlineMedium, color = BbxWhite)
                Button(
                    onClick = {
                        view.performPressFeedback()
                        viewModel.refreshStatus(forceFresh = true)
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
                    shape = RoundedCornerShape(RadiusSm),
                ) {
                    Text("↻  Check for updates", color = BbxWhite)
                }
            }
            Spacer(Modifier.height(20.dp))

            // State-driven card
            when (val s = state) {
                is UpdatesUiState.Loading -> LoadingCard()
                is UpdatesUiState.Error -> StatusCard("Error", s.message, ErrRed)
                is UpdatesUiState.GitNotInitialized -> GitNotInitializedCard(
                    onInit = {
                        view.performPressFeedback()
                        viewModel.refreshStatus(forceFresh = true)
                    },
                )
                is UpdatesUiState.UpToDate -> UpToDateCard(s.status)
                is UpdatesUiState.LocalAhead -> StatusCard(
                    "Local ahead of origin",
                    "Your install has ${s.status.commitsAhead} unpushed commit(s). Push them to GitHub if you want them to ship.",
                    InfoBlue,
                )
                is UpdatesUiState.UpdatesAvailable -> UpdatesAvailableCard(
                    s.status,
                    onInstall = {
                        view.performPressFeedback()
                        viewModel.startUpdate()
                    },
                )
                is UpdatesUiState.InProgress -> StatusCard(
                    "Update in progress",
                    "The runner is updating now. This panel polls every few seconds.",
                    InfoBlue,
                )
                is UpdatesUiState.Failed -> FailedCard(
                    s.lastState,
                    onRollback = {
                        view.performPressFeedback()
                        viewModel.rollback()
                    },
                )
                is UpdatesUiState.Interrupted -> InterruptedCard(
                    s.lastState,
                    onRollback = {
                        view.performPressFeedback()
                        viewModel.rollback()
                    },
                )
            }

            // Embeddings notification card — parity with the Portal card in
            // updates-manager.js. Absent unless health is broken/superseded
            // or a migration job is running; a failed status fetch leaves
            // embeddings null and never breaks this screen.
            embeddings?.let { emb ->
                EmbeddingsCard(
                    status = emb,
                    updateInFlight = embeddingsUpdateInFlight,
                    onUpdate = { slug ->
                        view.performPressFeedback()
                        viewModel.startEmbeddingsMigration(slug)
                    },
                    onManage = {
                        view.performPressFeedback()
                        context.startActivity(
                            Intent(Intent.ACTION_VIEW, Uri.parse("$origin/onboarding/?step=embeddings"))
                        )
                    },
                )
            }

            // Read-only reranker status line — the selector moved to the
            // onboarding wizard; this only surfaces the current reranker state.
            // Rendered at the TOP LEVEL (a standalone card, NOT nested in
            // EmbeddingsCard — which renders nothing on a healthy box and would
            // hide a silent reranker preflight failure exactly when it needs
            // surfacing). A failed /rerank/status fetch leaves rerankStatus null
            // → the line is simply absent and never breaks this screen.
            rerankStatus?.let { rr ->
                RerankStatusLine(
                    status = rr,
                    onManage = {
                        view.performPressFeedback()
                        context.startActivity(
                            Intent(Intent.ACTION_VIEW, Uri.parse("$origin/onboarding/?step=embeddings"))
                        )
                    },
                )
            }

            Spacer(Modifier.height(16.dp))
            HintFooter()
        }

        if (logModalOpen) {
            LogModal(
                lines = logLines,
                restartLabel = restartLabel,
                onClose = { viewModel.closeLogModal() },
            )
        }
    }
}

// ── Cards per UI state ──────────────────────────────────────────────────

@Composable
private fun LoadingCard() {
    Box(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(BbxSurface)
            .border(1.dp, Neutral300, RoundedCornerShape(RadiusMd))
            .padding(20.dp),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(color = BbxAccent, strokeWidth = 2.dp, modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(12.dp))
            Text("Checking for updates…", color = BbxDim)
        }
    }
}

@Composable
private fun StatusCard(title: String, body: String, accent: Color) {
    Card(accent) {
        Text(title, color = accent, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        Spacer(Modifier.height(8.dp))
        Text(body, color = BbxDim, fontSize = 14.sp)
    }
}

@Composable
private fun UpToDateCard(status: UpdateStatus) {
    Card(OkGreen) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("✓ Up to date", color = OkGreen, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
            Spacer(Modifier.width(12.dp))
            ShaChip(status.currentShort)
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "Last fetched ${(status.lastFetchAgeS ?: 0)} s ago.",
            color = BbxDim,
            fontSize = 12.sp,
        )
    }
}

@Composable
private fun GitNotInitializedCard(onInit: () -> Unit) {
    Card(WarnAmber) {
        Text("Updates not yet initialized", color = WarnAmber, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        Spacer(Modifier.height(8.dp))
        Text(
            "Run this once to enable in-place updates. Clones the BlackBox repo into .git/ without touching your data.",
            color = BbxDim,
            fontSize = 13.sp,
        )
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = onInit,
            colors = ButtonDefaults.buttonColors(containerColor = WarnAmber, contentColor = BbxBlack),
            shape = RoundedCornerShape(RadiusSm),
        ) { Text("First-time setup") }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun UpdatesAvailableCard(status: UpdateStatus, onInstall: () -> Unit) {
    Card(BbxAccent) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "⬆ ${status.commitsBehind} update${if (status.commitsBehind == 1) "" else "s"} available",
                color = BbxAccent,
                fontWeight = FontWeight.SemiBold,
                fontSize = 16.sp,
            )
            Spacer(Modifier.width(12.dp))
            ShaChip("${status.currentShort} → ${status.latestShort}")
        }
        Spacer(Modifier.height(12.dp))

        // Category badges
        val active = status.categories.activeBuckets()
        if (active.isNotEmpty()) {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                active.forEach { CategoryBadge(it) }
            }
            Spacer(Modifier.height(12.dp))
        }

        // Commit list (up to 8)
        if (status.commits.isNotEmpty()) {
            Column {
                status.commits.take(8).forEach { c -> CommitRow(c) }
                if (status.commits.size > 8) {
                    Text(
                        "… and ${status.commits.size - 8} more",
                        color = Neutral500,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(top = 4.dp, start = 8.dp),
                    )
                }
            }
            Spacer(Modifier.height(16.dp))
        }

        if (status.activeCliSessions > 0) {
            Text(
                "⚠ ${status.activeCliSessions} active CLI session${if (status.activeCliSessions == 1) "" else "s"} will be disconnected on restart.",
                color = WarnAmber,
                fontSize = 12.sp,
            )
            Spacer(Modifier.height(12.dp))
        }

        Button(
            onClick = onInstall,
            colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
            shape = RoundedCornerShape(RadiusSm),
        ) { Text("Install update  →", color = BbxWhite, fontWeight = FontWeight.SemiBold) }
    }
}

@Composable
private fun FailedCard(lastState: com.aiblackbox.portal.data.model.UpdateState, onRollback: () -> Unit) {
    Card(ErrRed) {
        Text(
            "✕ Last update failed during ${lastState.failedPhase ?: lastState.phase}",
            color = ErrRed,
            fontWeight = FontWeight.SemiBold,
            fontSize = 16.sp,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            lastState.error ?: "(no error message recorded)",
            color = BbxDim,
            fontSize = 13.sp,
            fontFamily = FontFamily.Monospace,
        )
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = onRollback,
            colors = ButtonDefaults.buttonColors(containerColor = WarnAmber, contentColor = BbxBlack),
            shape = RoundedCornerShape(RadiusSm),
        ) { Text("Rollback to last good SHA") }
    }
}

@Composable
private fun InterruptedCard(lastState: com.aiblackbox.portal.data.model.UpdateState, onRollback: () -> Unit) {
    Card(WarnAmber) {
        Text("⚠ Update interrupted", color = WarnAmber, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        Spacer(Modifier.height(8.dp))
        Text(
            "Last phase: ${lastState.phase}\nTarget SHA: ${lastState.targetSha.take(7)}\nStarted: ${lastState.updatedIso}",
            color = BbxDim,
            fontSize = 12.sp,
            fontFamily = FontFamily.Monospace,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "Service was killed mid-update. Rollback to the pre-update tag is safe — code reverts to the last known-good SHA.",
            color = BbxDim,
            fontSize = 13.sp,
        )
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = onRollback,
            colors = ButtonDefaults.buttonColors(containerColor = WarnAmber, contentColor = BbxBlack),
            shape = RoundedCornerShape(RadiusSm),
        ) { Text("Rollback now") }
    }
}

// ── Embeddings notification card (pluggable embeddings) ────────────────
//
// Mirrors _renderEmbeddingsCard in Portal/modules/updates-manager.js,
// including precedence: broken (urgent, [Manage] only, progress shown when
// the watcher's auto-migration is underway) > job running (progress +
// [Manage]) > superseded ([Update] when successor_slug known + [Manage]) >
// hidden. A stalled job renders nothing — the wizard owns stalled.

@Composable
private fun EmbeddingsCard(
    status: EmbeddingsStatus,
    updateInFlight: Boolean,
    onUpdate: (String) -> Unit,
    onManage: () -> Unit,
) {
    val health = status.health
    val job = status.job
    val jobRunning = job != null && job.state == "running"

    when {
        health.state == "broken" -> {
            Spacer(Modifier.height(16.dp))
            Card(ErrRed) {
                Text(
                    "⚠ Search memory needs attention",
                    color = ErrRed,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 16.sp,
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    health.detail.ifBlank {
                        "The active embedding model stopped working. Automatic recovery " +
                            "is migrating your search memory to a working model."
                    },
                    color = BbxDim,
                    fontSize = 13.sp,
                )
                if (jobRunning && job != null) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        embeddingsProgressLine(job),
                        color = BbxDim,
                        fontSize = 13.sp,
                        fontFamily = FontFamily.Monospace,
                    )
                    Spacer(Modifier.height(6.dp))
                    LinearProgressIndicator(
                        progress = { if (job.total > 0) job.done.toFloat() / job.total else 0f },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                Spacer(Modifier.height(12.dp))
                EmbeddingsManageButton(onManage)
            }
        }
        jobRunning && job != null -> {
            Spacer(Modifier.height(16.dp))
            Card(InfoBlue) {
                Text(
                    "⟳ Search memory update in progress",
                    color = InfoBlue,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 16.sp,
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    embeddingsProgressLine(job),
                    color = BbxDim,
                    fontSize = 13.sp,
                    fontFamily = FontFamily.Monospace,
                )
                Spacer(Modifier.height(6.dp))
                LinearProgressIndicator(
                    progress = { if (job.total > 0) job.done.toFloat() / job.total else 0f },
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                EmbeddingsManageButton(onManage)
            }
        }
        health.state == "superseded" -> {
            val successorLabel = health.successor?.takeIf { it.isNotBlank() }
                ?: health.successorSlug?.takeIf { it.isNotBlank() }
                ?: "a newer model"
            Spacer(Modifier.height(16.dp))
            Card(InfoBlue) {
                Text(
                    "⬆ Search memory update available",
                    color = InfoBlue,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 16.sp,
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    "Your system will transfer embeddings to $successorLabel in the " +
                        "background. Search keeps working the whole time; the switch " +
                        "happens automatically when it finishes and survives restarts.",
                    color = BbxDim,
                    fontSize = 13.sp,
                )
                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    val slug = health.successorSlug
                    if (slug != null) {
                        Button(
                            onClick = { onUpdate(slug) },
                            enabled = !updateInFlight,
                            colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
                            shape = RoundedCornerShape(RadiusSm),
                        ) { Text("Update", color = BbxWhite, fontWeight = FontWeight.SemiBold) }
                    }
                    EmbeddingsManageButton(onManage)
                }
            }
        }
        // else: health ok + no running job → no card.
    }
}

@Composable
private fun EmbeddingsManageButton(onManage: () -> Unit) {
    Button(
        onClick = onManage,
        colors = ButtonDefaults.buttonColors(containerColor = Neutral200, contentColor = BbxWhite),
        shape = RoundedCornerShape(RadiusSm),
    ) { Text("Manage") }
}

private fun embeddingsProgressLine(job: EmbeddingsJob): String {
    val cancelling = if (job.cancelRequested) " (cancelling…)" else ""
    return "Re-embedding ${job.done}/${job.total}…$cancelling"
}

// ── Reranker status line (read-only) ───────────────────────────────────
//
// The tier/key-gated selector moved to the onboarding wizard; the updates
// panel now only *reports* the reranker's state. Keyed on `available` so a
// preflight-failed reranker reads "not in use" even while `enabled` — the
// whole reason this renders unconditionally at the top level (a healthy
// EmbeddingsCard shows no card, so nesting would swallow that signal).
// [Manage] reuses the same wizard hand-off EmbeddingsManageButton uses; M4
// converts these onboarding deep-links to an in-app WebView later.

@Composable
private fun RerankStatusLine(status: RerankStatus, onManage: () -> Unit) {
    val (text, accent) = when {
        status.enabled && status.available ->
            "Reranking: ON — ${status.provider}/${status.model}" to OkGreen
        status.enabled && !status.available ->
            "Reranking: ON — not in use" to WarnAmber
        else -> "Reranking: OFF" to Neutral500
    }
    Spacer(Modifier.height(16.dp))
    Card(accent) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text(
                text,
                color = BbxWhite,
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            TextButton(onClick = onManage) { Text("Manage", color = BbxAccent) }
        }
    }
}

// ── Card primitives ────────────────────────────────────────────────────

@Composable
private fun Card(accent: Color, content: @Composable ColumnScope.() -> Unit) {
    // Row layout: accent strip on left (fixed 3dp), content column on right.
    // intrinsic height isn't required here because Row sizes its children by
    // the tallest — the Column dictates height + the strip stretches via
    // .fillMaxHeight inside the Row.
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(BbxSurface)
            .border(width = 1.dp, color = Neutral300, shape = RoundedCornerShape(RadiusMd))
            .heightIn(min = 1.dp),
    ) {
        Box(
            Modifier
                .width(3.dp)
                .heightIn(min = 64.dp)
                .background(accent),
        )
        Column(
            Modifier
                .padding(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 16.dp),
            content = content,
        )
    }
}

@Composable
private fun ShaChip(text: String?) {
    if (text.isNullOrBlank()) return
    Box(
        Modifier
            .clip(RoundedCornerShape(3.dp))
            .background(Neutral200)
            .padding(horizontal = 6.dp, vertical = 2.dp),
    ) {
        Text(text, color = BbxDim, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
    }
}

@Composable
private fun CategoryBadge(name: String) {
    val (bg, fg) = when (name) {
        "apt" -> Color(0x29FFB432) to Color(0xE6FFDC96)
        "pip", "mcp_pip" -> Color(0x2950C864) to Color(0xE6B4F0C8)
        "systemd", "sudoers" -> Color(0x40CC0000) to Color(0xE6FFB4B4)
        "helpers" -> Color(0x406CA0DC) to Color(0xE6B4DCFF)
        else -> Neutral200 to BbxDim
    }
    Box(
        Modifier
            .clip(RoundedCornerShape(3.dp))
            .background(bg)
            .padding(horizontal = 8.dp, vertical = 3.dp),
    ) {
        Text(
            name.uppercase(),
            color = fg,
            fontSize = 10.sp,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.5.sp,
            fontFamily = FontFamily.Monospace,
        )
    }
}

@Composable
private fun CommitRow(c: UpdateCommit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.padding(vertical = 3.dp),
    ) {
        Text(
            c.short,
            color = Neutral700,
            fontSize = 11.sp,
            fontFamily = FontFamily.Monospace,
            modifier = Modifier.width(60.dp),
        )
        Text(
            c.subject,
            color = BbxWhite,
            fontSize = 13.sp,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(start = 8.dp),
        )
    }
}

@Composable
private fun HintFooter() {
    Text(
        "Updates preserve your snapshots, media, secrets, paired devices, and operator config. " +
            "Only application code and dependencies change. Service restarts; expect 60–90 s downtime.",
        color = Neutral500,
        fontSize = 12.sp,
        modifier = Modifier.padding(horizontal = 4.dp),
    )
}

// ── SSE log modal ──────────────────────────────────────────────────────

@Composable
private fun LogModal(
    lines: List<LogLine>,
    restartLabel: String?,
    onClose: () -> Unit,
) {
    val feedback = rememberPressFeedback()
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onClose,
        sheetState = sheetState,
        containerColor = Neutral100,
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp)
                .heightIn(min = 240.dp, max = 600.dp),
        ) {
            Text(
                restartLabel ?: "Update log",
                color = BbxWhite,
                fontWeight = FontWeight.SemiBold,
                fontSize = 16.sp,
                modifier = Modifier.padding(bottom = 8.dp),
            )
            // Lazy log list with auto-scroll-to-bottom via reverseLayout
            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Color(0xFF050505), RoundedCornerShape(RadiusSm))
                    .padding(12.dp),
                reverseLayout = true,
            ) {
                items(lines.reversed()) { line ->
                    val color = when (line.kind) {
                        LogLine.Kind.Phase -> InfoBlue
                        LogLine.Kind.Success -> OkGreen
                        LogLine.Kind.Error -> ErrRed
                        LogLine.Kind.System -> BbxDim
                        LogLine.Kind.Line -> Color(0xFFDCFFDC)
                    }
                    val weight = if (line.kind == LogLine.Kind.Phase) FontWeight.SemiBold else FontWeight.Normal
                    Text(
                        line.text,
                        color = color,
                        fontSize = 12.sp,
                        fontFamily = FontFamily.Monospace,
                        fontWeight = weight,
                        modifier = Modifier.padding(vertical = 1.dp),
                    )
                }
            }
            Spacer(Modifier.height(8.dp))
            TextButton(onClick = { feedback(); onClose() }, modifier = Modifier.align(Alignment.End)) {
                Text("Close", color = BbxAccent)
            }
        }
    }
}
