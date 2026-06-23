package com.aiblackbox.portal.ui.cron

import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CheckboxDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.model.CronHistoryEntry
import com.aiblackbox.portal.data.model.CronJob
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBg
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral150
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral400
import com.aiblackbox.portal.ui.theme.Neutral50
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral600
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.Neutral900
import com.aiblackbox.portal.ui.theme.Neutral1000
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.RadiusXs
import com.aiblackbox.portal.ui.theme.SolidGreen

// Portal status colors
private val StatusActiveGreen = Color(0xFF4CAF50)
private val StatusActiveBg = Color(0x264CAF50)
private val StatusPausedAmber = Color(0xFFFFA726)
private val StatusPausedBg = Color(0x1AFFA726)
private val HistoryErrorRed = Color(0xFFEF5350)

@Composable
fun CronManagerScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: CronViewModel = viewModel()
) {
    val jobs by viewModel.filteredJobs.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val searchQuery by viewModel.searchQuery.collectAsState()
    val statusFilter by viewModel.statusFilter.collectAsState()
    val showEditDialog by viewModel.showEditDialog.collectAsState()
    val editingJob by viewModel.editingJob.collectAsState()
    val showHistoryDialog by viewModel.showHistoryDialog.collectAsState()
    val historyEntries by viewModel.historyEntries.collectAsState()
    val historyLoading by viewModel.historyLoading.collectAsState()
    val showDeleteConfirm by viewModel.showDeleteConfirm.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()

    val snackbarHostState = remember { SnackbarHostState() }
    val view = LocalView.current

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Show action messages via snackbar
    LaunchedEffect(actionMessage) {
        actionMessage?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearActionMessage()
        }
    }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = BbxBlack,
        snackbarHost = { SnackbarHost(snackbarHostState) },
        floatingActionButton = {
            FloatingActionButton(
                onClick = {
                    view.performPressFeedback()
                    viewModel.openCreateDialog()
                },
                containerColor = BbxAccent,
                contentColor = BbxWhite
            ) {
                Icon(Icons.Default.Add, contentDescription = "New Job")
            }
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(start = 16.dp, end = 16.dp, bottom = 12.dp, top = 100.dp)
        ) {
            // Title
            Text(
                "Cron Jobs",
                style = MaterialTheme.typography.headlineMedium,
                color = BbxWhite
            )
            Spacer(Modifier.height(12.dp))

            // Search + Filter bar (matching Portal .cron-top-bar)
            SearchFilterBar(
                searchQuery = searchQuery,
                onSearchChange = { viewModel.setSearchQuery(it) },
                statusFilter = statusFilter,
                onFilterChange = {
                    viewModel.setStatusFilter(it)
                }
            )
            Spacer(Modifier.height(12.dp))

            // Loading indicator
            AnimatedVisibility(visible = isLoading) {
                Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(
                        color = BbxAccent,
                        modifier = Modifier.size(24.dp),
                        strokeWidth = 2.dp
                    )
                }
            }

            // Job list or empty state
            if (jobs.isEmpty() && !isLoading) {
                EmptyState()
            } else {
                LazyColumn(
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.fillMaxSize()
                ) {
                    items(jobs, key = { it.id }) { job ->
                        CronJobCard(
                            job = job,
                            onRun = {
                                viewModel.runJob(job.id)
                            },
                            onToggle = {
                                if (job.status == "active") viewModel.pauseJob(job.id)
                                else viewModel.resumeJob(job.id)
                            },
                            onEdit = {
                                viewModel.openEditDialog(job.id)
                            },
                            onHistory = {
                                viewModel.openHistory(job.id)
                            },
                            onDelete = {
                                viewModel.requestDelete(job.id)
                            }
                        )
                    }
                }
            }
        }
    }

    // ---- Dialogs ----
    if (showEditDialog) {
        EditJobDialog(
            job = editingJob,
            isSaving = viewModel.isSaving.collectAsState().value,
            onDismiss = { viewModel.dismissEditDialog() },
            onSave = { name, prompt, schedule, hint, model, delivery, target, operator, oneShot ->
                viewModel.saveJob(name, prompt, schedule, hint, model, delivery, target, operator, oneShot)
            }
        )
    }

    if (showHistoryDialog) {
        HistoryDialog(
            entries = historyEntries,
            isLoading = historyLoading,
            onDismiss = { viewModel.dismissHistory() }
        )
    }

    showDeleteConfirm?.let { jobId ->
        val jobName = jobs.find { it.id == jobId }?.name ?: jobId
        AlertDialog(
            onDismissRequest = { viewModel.cancelDelete() },
            title = { Text("Delete Job", color = BbxWhite) },
            text = {
                Text(
                    "Delete \"$jobName\"? This cannot be undone.",
                    color = BbxDim
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    view.performPressFeedback()
                    viewModel.confirmDelete()
                }) {
                    Text("Delete", color = HistoryErrorRed)
                }
            },
            dismissButton = {
                TextButton(onClick = { view.performPressFeedback(); viewModel.cancelDelete() }) {
                    Text("Cancel", color = BbxDim)
                }
            },
            containerColor = Neutral100,
            tonalElevation = 0.dp
        )
    }
}

// =============================================================================
// Search + Filter Bar
// =============================================================================

@Composable
private fun SearchFilterBar(
    searchQuery: String,
    onSearchChange: (String) -> Unit,
    statusFilter: String,
    onFilterChange: (String) -> Unit
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Search field
        OutlinedTextField(
            value = searchQuery,
            onValueChange = onSearchChange,
            modifier = Modifier.weight(1f),
            placeholder = { Text("Search jobs...", color = Neutral500, fontSize = 13.sp) },
            leadingIcon = {
                Icon(Icons.Default.Search, contentDescription = null, tint = Neutral500, modifier = Modifier.size(18.dp))
            },
            singleLine = true,
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
            colors = glassTextFieldColors(),
            shape = RoundedCornerShape(RadiusSm)
        )

        // Status filter dropdown
        FilterDropdown(
            selected = statusFilter,
            options = listOf("all", "active", "paused"),
            labels = mapOf("all" to "All", "active" to "Active", "paused" to "Paused"),
            onSelect = onFilterChange
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FilterDropdown(
    selected: String,
    options: List<String>,
    labels: Map<String, String>,
    onSelect: (String) -> Unit
) {
    val feedback = rememberPressFeedback()
    var expanded by remember { mutableStateOf(false) }

    ExposedDropdownMenuBox(
        expanded = expanded,
        onExpandedChange = { expanded = it }
    ) {
        OutlinedTextField(
            value = labels[selected] ?: selected,
            onValueChange = {},
            readOnly = true,
            modifier = Modifier
                .width(110.dp)
                .menuAnchor(MenuAnchorType.PrimaryNotEditable),
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900, fontSize = 13.sp),
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            colors = glassTextFieldColors(),
            singleLine = true,
            shape = RoundedCornerShape(RadiusSm)
        )
        ExposedDropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false },
            containerColor = Neutral100
        ) {
            options.forEach { option ->
                DropdownMenuItem(
                    text = { Text(labels[option] ?: option, color = Neutral900) },
                    onClick = {
                        feedback()
                        onSelect(option)
                        expanded = false
                    }
                )
            }
        }
    }
}

// =============================================================================
// Job Card (matching Portal .cron-job-card)
// =============================================================================

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CronJobCard(
    job: CronJob,
    onRun: () -> Unit,
    onToggle: () -> Unit,
    onEdit: () -> Unit,
    onHistory: () -> Unit,
    onDelete: () -> Unit
) {
    val isActive = job.status == "active"
    val accentColor = if (isActive) StatusActiveGreen else StatusPausedAmber
    // Border uses accent color; card bg stays solid black (no tinted fill)
    val borderColor = accentColor.copy(alpha = 0.4f)

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(GlassBg)
            .border(1.dp, borderColor, RoundedCornerShape(RadiusMd))
    ) {
        // Left accent bar (Portal: border-left: 3px solid)
        Box(
            modifier = Modifier
                .width(3.dp)
                .matchParentSize()
                .background(accentColor)
        )

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(start = 14.dp, end = 12.dp, top = 12.dp, bottom = 12.dp)
        ) {
            // Header row: badge + name + schedule
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth()
            ) {
                // Status badge (Portal: .cron-job-status-badge)
                StatusBadge(
                    text = if (isActive) "ACTIVE" else "PAUSED",
                    color = if (isActive) StatusActiveGreen else StatusPausedAmber,
                    bgColor = if (isActive) StatusActiveBg else StatusPausedBg
                )
                Spacer(Modifier.width(10.dp))
                // Job name
                Text(
                    job.name,
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = Neutral1000,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f)
                )
                Spacer(Modifier.width(8.dp))
                // Schedule hint (Portal: .cron-job-schedule)
                val hint = job.frequencyHint ?: job.schedule
                if (hint.isNotBlank()) {
                    Text(
                        hint,
                        fontSize = 11.sp,
                        color = Neutral600,
                        fontFamily = FontFamily.Monospace,
                        modifier = Modifier
                            .clip(RoundedCornerShape(RadiusXs))
                            .background(Neutral50)
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    )
                }
            }

            Spacer(Modifier.height(6.dp))

            // Prompt preview (Portal: .cron-job-prompt)
            if (job.prompt.isNotBlank()) {
                Text(
                    job.prompt,
                    fontSize = 13.sp,
                    color = Neutral700,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    lineHeight = 18.sp
                )
                Spacer(Modifier.height(8.dp))
            }

            // Metadata tags (Portal: .cron-job-meta)
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                MetaTag(job.model.replaceFirstChar { it.uppercase() })
                if (job.operator.isNotBlank()) {
                    MetaTag(job.operator)
                }
                val deliveryLabel = when (job.delivery) {
                    "snapshot" -> "Snapshot"
                    "sms" -> "SMS"
                    "voice_call" -> "Voice"
                    "notification" -> "Alert"
                    else -> job.delivery
                }
                MetaTag(deliveryLabel)
                if (job.runCount > 0) {
                    MetaTag("${job.runCount} runs")
                }
                job.lastRunAt?.let { MetaTag("Last: ${formatRelativeTime(it)}") }
                job.nextRunAt?.let { MetaTag("Next: ${formatRelativeTime(it)}") }
            }

            Spacer(Modifier.height(10.dp))

            // Action buttons row (Portal: .cron-job-actions)
            Row(
                horizontalArrangement = Arrangement.End,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 4.dp)
            ) {
                // Run
                ActionIconButton(
                    icon = Icons.Default.PlayArrow,
                    contentDescription = "Run Now",
                    onClick = onRun,
                    tint = Color(0xFF64B5F6)
                )
                Spacer(Modifier.width(4.dp))
                // Pause/Resume
                ActionIconButton(
                    icon = if (isActive) Icons.Default.Info else Icons.Default.PlayArrow,
                    contentDescription = if (isActive) "Pause" else "Resume",
                    onClick = onToggle,
                    tint = StatusPausedAmber
                )
                Spacer(Modifier.width(4.dp))
                // Edit
                ActionIconButton(
                    icon = Icons.Default.Edit,
                    contentDescription = "Edit",
                    onClick = onEdit,
                    tint = Neutral600
                )
                Spacer(Modifier.width(4.dp))
                // History
                ActionIconButton(
                    icon = Icons.Default.Info,
                    contentDescription = "History",
                    onClick = onHistory,
                    tint = Color(0xFFBB86FC)
                )
                Spacer(Modifier.width(4.dp))
                // Delete
                ActionIconButton(
                    icon = Icons.Default.Delete,
                    contentDescription = "Delete",
                    onClick = onDelete,
                    tint = HistoryErrorRed
                )
            }
        }
    }
}

// =============================================================================
// Reusable sub-components
// =============================================================================

@Composable
private fun StatusBadge(text: String, color: Color, bgColor: Color) {
    Text(
        text,
        fontSize = 10.sp,
        fontWeight = FontWeight.SemiBold,
        color = color,
        letterSpacing = 0.6.sp,
        modifier = Modifier
            .clip(RoundedCornerShape(50))
            .background(bgColor)
            .padding(horizontal = 8.dp, vertical = 2.dp)
    )
}

@Composable
private fun MetaTag(text: String) {
    Text(
        text,
        fontSize = 11.sp,
        color = Neutral500,
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusXs))
            .background(Neutral50)
            .border(1.dp, Color(0x0AFFFFFF), RoundedCornerShape(RadiusXs))
            .padding(horizontal = 8.dp, vertical = 2.dp)
    )
}

@Composable
private fun ActionIconButton(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    contentDescription: String,
    onClick: () -> Unit,
    tint: Color
) {
    val feedback = rememberPressFeedback()
    IconButton(
        onClick = { feedback(); onClick() },
        modifier = Modifier.size(32.dp)
    ) {
        Icon(icon, contentDescription = contentDescription, tint = tint, modifier = Modifier.size(18.dp))
    }
}

@Composable
private fun EmptyState() {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                "No scheduled jobs yet",
                style = MaterialTheme.typography.bodyLarge,
                color = Neutral500
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "Create your first job to automate tasks on a schedule",
                fontSize = 12.sp,
                color = Neutral400,
                modifier = Modifier.padding(horizontal = 40.dp),
                lineHeight = 18.sp
            )
        }
    }
}

// =============================================================================
// Edit / Create Job Dialog
// =============================================================================

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun EditJobDialog(
    job: CronJob?,
    isSaving: Boolean,
    onDismiss: () -> Unit,
    onSave: (String, String, String, String, String, String, String, String, Boolean) -> Unit
) {
    var name by remember(job) { mutableStateOf(job?.name ?: "") }
    var prompt by remember(job) { mutableStateOf(job?.prompt ?: "") }
    var frequency by remember(job) { mutableStateOf("daily") }
    var timeHour by remember(job) { mutableStateOf("07") }
    var timeMinute by remember(job) { mutableStateOf("00") }
    var cronExpression by remember(job) { mutableStateOf(job?.schedule ?: "") }
    var useAdvanced by remember(job) { mutableStateOf(false) }
    var model by remember(job) { mutableStateOf(job?.model ?: "gemini") }
    var delivery by remember(job) { mutableStateOf(job?.delivery ?: "snapshot") }
    var deliveryTarget by remember(job) { mutableStateOf(job?.deliveryTarget ?: "") }
    var operator by remember(job) { mutableStateOf(job?.operator ?: "Brandon") }
    var oneShot by remember(job) { mutableStateOf(job?.oneShot ?: false) }
    var nameError by remember { mutableStateOf(false) }
    var promptError by remember { mutableStateOf(false) }
    val view = LocalView.current

    // Parse existing cron expression into simple mode
    LaunchedEffect(job) {
        if (job != null && job.schedule.isNotBlank()) {
            val parts = job.schedule.split(" ")
            if (parts.size == 5) {
                val (min, hour, dom, _, dow) = parts
                when {
                    min == "0" && hour == "*" -> {
                        frequency = "hourly"; useAdvanced = false
                    }
                    min.startsWith("*/") && hour == "*" -> {
                        frequency = "custom"; useAdvanced = true; cronExpression = job.schedule
                    }
                    dom == "*" && dow == "*" && min.all { it.isDigit() } && hour.all { it.isDigit() } -> {
                        frequency = "daily"
                        timeHour = hour.padStart(2, '0')
                        timeMinute = min.padStart(2, '0')
                        useAdvanced = false
                    }
                    dom == "*" && dow.all { it.isDigit() } && min.all { it.isDigit() } && hour.all { it.isDigit() } -> {
                        frequency = "weekly"; useAdvanced = true; cronExpression = job.schedule
                    }
                    else -> {
                        useAdvanced = true; cronExpression = job.schedule
                    }
                }
            }
        }
    }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .padding(vertical = 24.dp),
            shape = RoundedCornerShape(RadiusMd),
            color = Neutral100,
            border = androidx.compose.foundation.BorderStroke(1.dp, GlassBorder)
        ) {
            Column(
                modifier = Modifier
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                // Title
                Text(
                    if (job != null) "Edit Scheduled Job" else "New Scheduled Job",
                    style = MaterialTheme.typography.headlineMedium,
                    color = BbxWhite
                )

                // Job Name
                FormField("NAME") {
                    OutlinedTextField(
                        value = name,
                        onValueChange = { name = it; nameError = false },
                        modifier = Modifier.fillMaxWidth(),
                        isError = nameError,
                        placeholder = { Text("Job name", color = Neutral400) },
                        singleLine = true,
                        textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
                        colors = glassTextFieldColors(),
                        shape = RoundedCornerShape(RadiusSm)
                    )
                }

                // Prompt
                FormField("PROMPT") {
                    OutlinedTextField(
                        value = prompt,
                        onValueChange = { prompt = it; promptError = false },
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(100.dp),
                        isError = promptError,
                        placeholder = { Text("What should this job do?", color = Neutral400) },
                        maxLines = 5,
                        textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
                        colors = glassTextFieldColors(),
                        shape = RoundedCornerShape(RadiusSm)
                    )
                }

                // Schedule tabs (Simple / Advanced)
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(RadiusSm))
                        .background(Neutral50)
                        .padding(2.dp)
                ) {
                    ScheduleTab("Simple", !useAdvanced) {
                        useAdvanced = false
                    }
                    ScheduleTab("Advanced", useAdvanced) {
                        useAdvanced = true
                        // Sync cron from simple
                        if (cronExpression.isBlank()) {
                            cronExpression = buildSimpleCron(frequency, timeHour, timeMinute)
                        }
                    }
                }

                if (!useAdvanced) {
                    // Simple frequency selector
                    FormField("FREQUENCY") {
                        FrequencySelector(frequency) {
                            frequency = it
                        }
                    }
                    if (frequency == "daily" || frequency == "weekly") {
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("at", color = Neutral600, fontSize = 13.sp)
                            OutlinedTextField(
                                value = timeHour,
                                onValueChange = { if (it.length <= 2 && it.all { c -> c.isDigit() }) timeHour = it },
                                modifier = Modifier.width(60.dp),
                                singleLine = true,
                                textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
                                colors = glassTextFieldColors(),
                                shape = RoundedCornerShape(RadiusSm)
                            )
                            Text(":", color = Neutral600, fontSize = 16.sp)
                            OutlinedTextField(
                                value = timeMinute,
                                onValueChange = { if (it.length <= 2 && it.all { c -> c.isDigit() }) timeMinute = it },
                                modifier = Modifier.width(60.dp),
                                singleLine = true,
                                textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
                                colors = glassTextFieldColors(),
                                shape = RoundedCornerShape(RadiusSm)
                            )
                        }
                    }

                    // Schedule preview
                    val preview = describeSimpleSchedule(frequency, timeHour, timeMinute)
                    Text(
                        "Runs $preview",
                        fontSize = 12.sp,
                        color = Neutral500,
                        fontStyle = androidx.compose.ui.text.font.FontStyle.Italic,
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color(0x05FFFFFF))
                            .padding(6.dp)
                    )
                } else {
                    // Advanced cron input
                    FormField("CRON EXPRESSION") {
                        OutlinedTextField(
                            value = cronExpression,
                            onValueChange = { cronExpression = it },
                            modifier = Modifier.fillMaxWidth(),
                            placeholder = { Text("* * * * *", color = Neutral400) },
                            singleLine = true,
                            textStyle = MaterialTheme.typography.bodyMedium.copy(
                                color = Neutral900,
                                fontFamily = FontFamily.Monospace,
                                letterSpacing = 2.sp
                            ),
                            colors = glassTextFieldColors(),
                            shape = RoundedCornerShape(RadiusSm)
                        )
                    }
                    Text(
                        "Format: min hour day month weekday",
                        fontSize = 11.sp,
                        color = Neutral500
                    )
                }

                // Model
                FormField("MODEL") {
                    ModelSelector(model) {
                        model = it
                    }
                }

                // Delivery
                FormField("DELIVERY") {
                    DeliverySelector(delivery) {
                        delivery = it
                    }
                }

                // Delivery target (for SMS / voice)
                if (delivery == "sms" || delivery == "voice_call") {
                    FormField("PHONE NUMBER") {
                        OutlinedTextField(
                            value = deliveryTarget,
                            onValueChange = { deliveryTarget = it },
                            modifier = Modifier.fillMaxWidth(),
                            placeholder = { Text("+1 555 123 4567", color = Neutral400) },
                            singleLine = true,
                            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
                            colors = glassTextFieldColors(),
                            shape = RoundedCornerShape(RadiusSm)
                        )
                    }
                }

                // Operator
                FormField("OPERATOR") {
                    OutlinedTextField(
                        value = operator,
                        onValueChange = { operator = it },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
                        colors = glassTextFieldColors(),
                        shape = RoundedCornerShape(RadiusSm)
                    )
                }

                // One-shot toggle
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text("One-shot (run once then disable)", fontSize = 12.sp, color = Neutral600, modifier = Modifier.weight(1f))
                    Checkbox(
                        checked = oneShot,
                        onCheckedChange = {
                            view.performPressFeedback()
                            oneShot = it
                        },
                        colors = CheckboxDefaults.colors(
                            checkedColor = SolidGreen,
                            uncheckedColor = Neutral400
                        )
                    )
                }

                // Save / Cancel buttons
                Row(
                    horizontalArrangement = Arrangement.End,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    TextButton(onClick = { view.performPressFeedback(); onDismiss() }) {
                        Text("Cancel", color = BbxDim)
                    }
                    Spacer(Modifier.width(8.dp))
                    Button(
                        onClick = {
                            view.performPressFeedback()
                            nameError = name.isBlank()
                            promptError = prompt.isBlank()
                            if (nameError || promptError) return@Button

                            val schedule: String
                            val hint: String
                            if (useAdvanced) {
                                schedule = cronExpression
                                hint = describeCronExpression(cronExpression)
                            } else {
                                schedule = buildSimpleCron(frequency, timeHour, timeMinute)
                                hint = describeSimpleSchedule(frequency, timeHour, timeMinute)
                            }
                            onSave(name, prompt, schedule, hint, model, delivery, deliveryTarget, operator, oneShot)
                        },
                        enabled = !isSaving,
                        colors = ButtonDefaults.buttonColors(containerColor = BbxAccent)
                    ) {
                        if (isSaving) {
                            CircularProgressIndicator(color = BbxWhite, modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        } else {
                            Text(if (job != null) "Update" else "Create", color = BbxWhite)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun FormField(label: String, content: @Composable () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(
            label,
            fontSize = 11.sp,
            fontWeight = FontWeight.SemiBold,
            color = Neutral600,
            letterSpacing = 0.6.sp
        )
        content()
    }
}

@Composable
private fun RowScope.ScheduleTab(text: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .weight(1f)
            .clip(RoundedCornerShape(RadiusXs))
            .then(
                if (selected) Modifier
                    .background(Neutral200)
                    .border(1.dp, Color(0x1AFFFFFF), RoundedCornerShape(RadiusXs))
                else Modifier
            )
            .clickFeedback(onClick = onClick)
            .padding(vertical = 7.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text,
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
            color = if (selected) Neutral1000 else Neutral500
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FrequencySelector(selected: String, onSelect: (String) -> Unit) {
    val feedback = rememberPressFeedback()
    var expanded by remember { mutableStateOf(false) }
    val options = listOf("hourly" to "Every hour", "daily" to "Daily", "weekly" to "Weekly")
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = options.find { it.first == selected }?.second ?: selected,
            onValueChange = {},
            readOnly = true,
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor(MenuAnchorType.PrimaryNotEditable),
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
            colors = glassTextFieldColors(),
            singleLine = true,
            shape = RoundedCornerShape(RadiusSm)
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, containerColor = Neutral100) {
            options.forEach { (value, label) ->
                DropdownMenuItem(text = { Text(label, color = Neutral900) }, onClick = { feedback(); onSelect(value); expanded = false })
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ModelSelector(selected: String, onSelect: (String) -> Unit) {
    val feedback = rememberPressFeedback()
    var expanded by remember { mutableStateOf(false) }
    val options = listOf("gemini" to "Gemini", "openai" to "OpenAI", "claude" to "Claude", "grok" to "Grok", "computer-use" to "Computer Use")
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = options.find { it.first == selected }?.second ?: selected,
            onValueChange = {},
            readOnly = true,
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor(MenuAnchorType.PrimaryNotEditable),
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
            colors = glassTextFieldColors(),
            singleLine = true,
            shape = RoundedCornerShape(RadiusSm)
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, containerColor = Neutral100) {
            options.forEach { (value, label) ->
                DropdownMenuItem(text = { Text(label, color = Neutral900) }, onClick = { feedback(); onSelect(value); expanded = false })
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DeliverySelector(selected: String, onSelect: (String) -> Unit) {
    val feedback = rememberPressFeedback()
    var expanded by remember { mutableStateOf(false) }
    val options = listOf("snapshot" to "Snapshot", "sms" to "SMS", "voice_call" to "Voice Call", "notification" to "Notification")
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = options.find { it.first == selected }?.second ?: selected,
            onValueChange = {},
            readOnly = true,
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor(MenuAnchorType.PrimaryNotEditable),
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
            colors = glassTextFieldColors(),
            singleLine = true,
            shape = RoundedCornerShape(RadiusSm)
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, containerColor = Neutral100) {
            options.forEach { (value, label) ->
                DropdownMenuItem(text = { Text(label, color = Neutral900) }, onClick = { feedback(); onSelect(value); expanded = false })
            }
        }
    }
}

// =============================================================================
// History Dialog (matching Portal .cron-history-*)
// =============================================================================

@Composable
private fun HistoryDialog(
    entries: List<CronHistoryEntry>,
    isLoading: Boolean,
    onDismiss: () -> Unit
) {
    val feedback = rememberPressFeedback()
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .padding(vertical = 24.dp),
            shape = RoundedCornerShape(RadiusMd),
            color = Neutral100,
            border = androidx.compose.foundation.BorderStroke(1.dp, GlassBorder)
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        "Execution History",
                        style = MaterialTheme.typography.headlineMedium,
                        color = BbxWhite
                    )
                    TextButton(onClick = { feedback(); onDismiss() }) {
                        Text("Close", color = BbxDim)
                    }
                }
                Spacer(Modifier.height(12.dp))

                if (isLoading) {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .height(100.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(color = BbxAccent, modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                    }
                } else if (entries.isEmpty()) {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .height(100.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("No execution history yet", color = Neutral500)
                    }
                } else {
                    LazyColumn(
                        verticalArrangement = Arrangement.spacedBy(6.dp),
                        modifier = Modifier.height(400.dp)
                    ) {
                        items(entries.size) { i ->
                            HistoryItem(entries[i])
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun HistoryItem(entry: CronHistoryEntry) {
    val hasError = entry.error != null
    val accentColor = if (hasError) HistoryErrorRed else StatusActiveGreen

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(GlassBg)
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
    ) {
        // Left accent
        Box(
            modifier = Modifier
                .width(3.dp)
                .matchParentSize()
                .background(accentColor)
        )

        Column(modifier = Modifier.padding(start = 14.dp, end = 12.dp, top = 12.dp, bottom = 12.dp)) {
            // Time
            Text(
                formatRelativeTime(entry.runAt),
                fontSize = 12.sp,
                color = Neutral700,
                fontWeight = FontWeight.Medium
            )
            Spacer(Modifier.height(6.dp))

            // Meta row
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                MetaTag(entry.model)
                MetaTag("${entry.durationMs}ms")
                MetaTag(entry.deliveryStatus ?: "completed")
            }

            // Result preview
            entry.result?.let { result ->
                Spacer(Modifier.height(8.dp))
                Text(
                    result.take(300) + if (result.length > 300) "..." else "",
                    fontSize = 12.sp,
                    color = Neutral700,
                    lineHeight = 18.sp,
                    maxLines = 4,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(RadiusXs))
                        .background(Color(0x05FFFFFF))
                        .border(1.dp, Color(0x08FFFFFF), RoundedCornerShape(RadiusXs))
                        .padding(8.dp)
                )
            }

            // Error text
            entry.error?.let { error ->
                Spacer(Modifier.height(6.dp))
                Text(
                    error,
                    fontSize = 12.sp,
                    color = HistoryErrorRed,
                    lineHeight = 17.sp,
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(RadiusXs))
                        .background(Color(0x10F44336))
                        .border(1.dp, Color(0x1AF44336), RoundedCornerShape(RadiusXs))
                        .padding(6.dp)
                )
            }
        }
    }
}

// =============================================================================
// Helpers
// =============================================================================

@Composable
private fun glassTextFieldColors() = OutlinedTextFieldDefaults.colors(
    focusedBorderColor = Color(0x33FFFFFF),
    unfocusedBorderColor = Color(0x14FFFFFF),
    cursorColor = BbxWhite,
    focusedContainerColor = Neutral100,
    unfocusedContainerColor = Neutral100,
    errorBorderColor = HistoryErrorRed,
    focusedTextColor = Neutral900,
    unfocusedTextColor = Neutral900
)

private fun buildSimpleCron(frequency: String, hour: String, minute: String): String {
    val h = hour.toIntOrNull() ?: 7
    val m = minute.toIntOrNull() ?: 0
    return when (frequency) {
        "hourly" -> "0 * * * *"
        "daily" -> "$m $h * * *"
        "weekly" -> "$m $h * * 1"
        else -> "0 7 * * *"
    }
}

private fun describeSimpleSchedule(frequency: String, hour: String, minute: String): String {
    val h = hour.toIntOrNull() ?: 7
    val m = minute.toIntOrNull() ?: 0
    val ampm = if (h >= 12) "PM" else "AM"
    val h12 = if (h % 12 == 0) 12 else h % 12
    val timeStr = "$h12:${m.toString().padStart(2, '0')} $ampm"
    return when (frequency) {
        "hourly" -> "every hour"
        "daily" -> "daily at $timeStr"
        "weekly" -> "weekly at $timeStr"
        else -> "daily at $timeStr"
    }
}

private fun describeCronExpression(cron: String): String {
    val parts = cron.trim().split("\\s+".toRegex())
    if (parts.size != 5) return "Cron: $cron"
    val (min, hour) = parts
    if (min == "0" && hour == "*") return "Every hour"
    if (min.startsWith("*/")) return "Every ${min.drop(2)} minutes"
    if (hour.startsWith("*/")) return "Every ${hour.drop(2)} hours"
    return "Cron: $cron"
}

private fun formatRelativeTime(isoStr: String): String {
    if (isoStr.isBlank()) return ""
    return try {
        // Simple relative time -- just show the raw timestamp shortened
        if (isoStr.length > 16) isoStr.substring(5, 16).replace("T", " ") else isoStr
    } catch (_: Exception) {
        isoStr
    }
}
