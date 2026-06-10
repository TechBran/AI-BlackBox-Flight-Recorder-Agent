package com.aiblackbox.portal.ui.sms

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import android.view.HapticFeedbackConstants
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.util.Constants

@Composable
fun SmsInboxScreen(
    origin: String,
    operator: String = "",
    modifier: Modifier = Modifier,
    viewModel: SmsViewModel = viewModel()
) {
    val threads by viewModel.threads.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val selectedPhone by viewModel.selectedPhone.collectAsState()
    val messages by viewModel.messages.collectAsState()
    val isSending by viewModel.isSending.collectAsState()
    val connected by viewModel.connected.collectAsState()
    val selectedContactName by viewModel.selectedContactName.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()
    val smsProvider by viewModel.smsProvider.collectAsState()
    val smsModel by viewModel.smsModel.collectAsState()
    val lines by viewModel.lines.collectAsState()
    val selectedFromNumber by viewModel.selectedFromNumber.collectAsState()

    LaunchedEffect(origin) { viewModel.initialize(origin, operator) }
    DisposableEffect(Unit) {
        viewModel.startPolling()
        onDispose { viewModel.stopPolling() }
    }

    // Snackbar
    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(actionMessage) {
        actionMessage?.let { snackbarHostState.showSnackbar(it); viewModel.clearActionMessage() }
    }

    // If a thread is selected, show conversation view. Otherwise show thread list.
    if (selectedPhone != null) {
        SmsConversationView(
            phone = selectedPhone!!,
            contactName = selectedContactName,
            messages = messages,
            isSending = isSending,
            lines = lines,
            selectedFromNumber = selectedFromNumber,
            onSelectFromNumber = { n -> viewModel.setFromNumber(n) },
            onSend = { msg -> viewModel.sendSms(msg) },
            onBack = { viewModel.clearSelection() }
        )
    } else {
        SmsThreadListView(
            threads = threads,
            isLoading = isLoading,
            connected = connected,
            smsProvider = smsProvider,
            smsModel = smsModel,
            onProviderModelChange = { provider, model -> viewModel.saveSmsPreferences(provider, model) },
            onRefresh = { viewModel.loadThreads() },
            onSelectThread = { thread -> viewModel.loadMessages(thread.phoneNumber) },
            snackbarHostState = snackbarHostState
        )
    }
}

@Composable
private fun SmsThreadListView(
    threads: List<SmsThread>,
    isLoading: Boolean,
    connected: Boolean,
    smsProvider: String,
    smsModel: String,
    onProviderModelChange: (String, String) -> Unit,
    onRefresh: () -> Unit,
    onSelectThread: (SmsThread) -> Unit,
    snackbarHostState: SnackbarHostState
) {
    // SMS-capable providers (text chat only, no agents/voice/CU)
    val smsProviders = listOf(
        "gemini" to "Gemini",
        "anthropic" to "Anthropic",
        "openai" to "OpenAI",
        "xai" to "xAI"
    )

    Box(Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(start = 16.dp, end = 16.dp, bottom = 120.dp, top = 100.dp)
        ) {
            // Header row
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Text(
                        "SMS",
                        style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                        color = BbxWhite
                    )
                    // Connection dot
                    Box(
                        Modifier
                            .size(8.dp)
                            .clip(CircleShape)
                            .background(if (connected) SolidGreen else BbxAccent)
                    )
                }
                TextButton(onClick = onRefresh) { Text("Refresh", color = BbxDim) }
            }
            Spacer(Modifier.height(8.dp))

            // ── AI Response Model Selector ──
            SmsModelSelector(
                smsProvider = smsProvider,
                smsModel = smsModel,
                providers = smsProviders,
                onProviderModelChange = onProviderModelChange
            )

            Spacer(Modifier.height(12.dp))

            if (isLoading && threads.isEmpty()) {
                Box(
                    Modifier
                        .fillMaxWidth()
                        .padding(32.dp),
                    contentAlignment = Alignment.Center
                ) {
                    CircularProgressIndicator(color = BbxAccent, strokeWidth = 2.dp)
                }
            } else if (threads.isEmpty()) {
                Box(
                    Modifier
                        .fillMaxWidth()
                        .padding(48.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("\uD83D\uDCAC", fontSize = 48.sp)
                        Spacer(Modifier.height(12.dp))
                        Text(
                            "No conversations yet",
                            color = Neutral500,
                            style = MaterialTheme.typography.bodyLarge
                        )
                    }
                }
            } else {
                LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(threads, key = { it.phoneNumber }) { thread ->
                        SmsThreadCard(thread = thread, onClick = { onSelectThread(thread) })
                    }
                }
            }
        }
        SnackbarHost(
            hostState = snackbarHostState,
            modifier = Modifier.align(Alignment.BottomCenter)
        )
    }
}

@Composable
private fun SmsThreadCard(thread: SmsThread, onClick: () -> Unit) {
    val view = LocalView.current
    GlassCard(
        modifier = Modifier
            .fillMaxWidth()
            .clickable {
                view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                onClick()
            }
    ) {
        Row(modifier = Modifier.padding(14.dp)) {
            // Left accent bar - blue for outbound last, accent for inbound
            Box(
                Modifier
                    .width(3.dp)
                    .height(64.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(
                        if (thread.direction == "inbound") BbxAccent else Color(0xFF2979FF)
                    )
            )
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        thread.contactName.ifBlank { thread.phoneNumber },
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                        color = if (thread.unreadCount > 0) BbxWhite else BbxDim,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f)
                    )
                    if (thread.unreadCount > 0) {
                        Spacer(Modifier.width(8.dp))
                        Box(
                            modifier = Modifier
                                .clip(CircleShape)
                                .background(BbxAccent)
                                .padding(horizontal = 7.dp, vertical = 3.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                thread.unreadCount.toString(),
                                color = BbxWhite,
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    }
                }
                if (thread.contactName.isNotBlank()) {
                    Text(
                        thread.phoneNumber,
                        style = MaterialTheme.typography.bodySmall,
                        color = Neutral500
                    )
                    Spacer(Modifier.height(2.dp))
                }
                if (thread.lineNumber.isNotBlank()) {
                    Text(
                        "via ${thread.lineNumber}",
                        style = MaterialTheme.typography.labelSmall,
                        color = Neutral500
                    )
                    Spacer(Modifier.height(2.dp))
                }
                Spacer(Modifier.height(4.dp))
                Text(
                    (if (thread.direction == "outbound") "\u2197 " else "") + thread.lastMessage.take(60),
                    style = MaterialTheme.typography.bodySmall,
                    color = if (thread.unreadCount > 0) Neutral500 else Color(0xFF666666),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    thread.lastTimestamp.take(19).replace("T", " "),
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500
                )
            }
        }
    }
}

@Composable
private fun SmsConversationView(
    phone: String,
    contactName: String,
    messages: List<SmsMsg>,
    isSending: Boolean,
    lines: List<SmsLine>,
    selectedFromNumber: String?,
    onSelectFromNumber: (String?) -> Unit,
    onSend: (String) -> Unit,
    onBack: () -> Unit
) {
    var inputText by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    // Scroll to bottom when messages change
    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) listState.animateScrollToItem(messages.size - 1)
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(start = 0.dp, end = 0.dp, bottom = 0.dp, top = 90.dp)
    ) {
        // Header with back button
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(Neutral100)
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            TextButton(onClick = onBack) {
                Text("\u2190", color = BbxAccent, fontSize = 20.sp)
            }
            Spacer(Modifier.width(8.dp))
            Column {
                Text(
                    contactName.ifBlank { phone },
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = BbxWhite
                )
                if (contactName.isNotBlank()) {
                    Text(phone, style = MaterialTheme.typography.bodySmall, color = Neutral500)
                }
            }
        }

        // Messages
        LazyColumn(
            state = listState,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
            contentPadding = PaddingValues(top = 12.dp, bottom = 24.dp)
        ) {
            items(messages, key = { it.id }) { msg ->
                SmsBubble(msg)
            }
        }

        // Compose area: optional from-number selector + text input row
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(Neutral100)
                .navigationBarsPadding()
        ) {
        if (lines.isNotEmpty()) {
            SmsFromNumberSelector(
                lines = lines,
                selectedFromNumber = selectedFromNumber,
                onSelectFromNumber = onSelectFromNumber
            )
        }
        // Compose bar
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            OutlinedTextField(
                value = inputText,
                onValueChange = { inputText = it },
                modifier = Modifier.weight(1f),
                placeholder = { Text("Type a message...", color = Neutral500) },
                singleLine = false,
                maxLines = 3,
                shape = RoundedCornerShape(20.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = BbxAccent,
                    unfocusedBorderColor = Neutral200,
                    cursorColor = BbxAccent,
                    focusedTextColor = BbxWhite,
                    unfocusedTextColor = BbxWhite
                )
            )
            // Send button - circular blue
            Button(
                onClick = {
                    if (inputText.isNotBlank()) {
                        onSend(inputText.trim())
                        inputText = ""
                    }
                },
                enabled = inputText.isNotBlank() && !isSending,
                modifier = Modifier.size(48.dp),
                shape = CircleShape,
                contentPadding = PaddingValues(0.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Color(0xFF2979FF),
                    disabledContainerColor = Neutral200
                )
            ) {
                Text("\u2197", fontSize = 20.sp, color = BbxWhite)
            }
        }
        }
    }
}

@Composable
private fun SmsFromNumberSelector(
    lines: List<SmsLine>,
    selectedFromNumber: String?,
    onSelectFromNumber: (String?) -> Unit
) {
    var showMenu by remember { mutableStateOf(false) }

    val selectedLabel = if (selectedFromNumber.isNullOrBlank()) {
        "Default line"
    } else {
        lines.find { it.number == selectedFromNumber }?.label ?: selectedFromNumber
    }

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 12.dp, end = 12.dp, top = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Text(
            "From",
            style = MaterialTheme.typography.labelMedium,
            color = Neutral500
        )
        Box(modifier = Modifier.weight(1f)) {
            Button(
                onClick = { showMenu = true },
                modifier = Modifier.fillMaxWidth().height(36.dp),
                shape = RoundedCornerShape(8.dp),
                contentPadding = PaddingValues(horizontal = 12.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Neutral200,
                    contentColor = BbxWhite
                )
            ) {
                Text(
                    selectedLabel,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                    fontSize = 13.sp
                )
                Text(" ▾", color = BbxDim, fontSize = 12.sp)
            }
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false }
            ) {
                DropdownMenuItem(
                    text = {
                        Text(
                            "Default line",
                            color = if (selectedFromNumber.isNullOrBlank()) BbxAccent else BbxWhite,
                            fontWeight = if (selectedFromNumber.isNullOrBlank()) FontWeight.Bold else FontWeight.Normal
                        )
                    },
                    onClick = {
                        showMenu = false
                        onSelectFromNumber(null)
                    }
                )
                lines.forEach { line ->
                    DropdownMenuItem(
                        text = {
                            Text(
                                line.label,
                                color = if (line.number == selectedFromNumber) BbxAccent else BbxWhite,
                                fontWeight = if (line.number == selectedFromNumber) FontWeight.Bold else FontWeight.Normal
                            )
                        },
                        onClick = {
                            showMenu = false
                            onSelectFromNumber(line.number)
                        }
                    )
                }
            }
        }
    }
}

@Composable
private fun SmsModelSelector(
    smsProvider: String,
    smsModel: String,
    providers: List<Pair<String, String>>,
    onProviderModelChange: (String, String) -> Unit
) {
    var showProviderMenu by remember { mutableStateOf(false) }
    var showModelMenu by remember { mutableStateOf(false) }

    val providerDisplay = providers.find { it.first == smsProvider }?.second ?: smsProvider
    val models = Constants.MODEL_CONFIG[smsProvider] ?: emptyList()
    val modelDisplay = models.find { it.first == smsModel }?.second ?: "Auto"

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(Neutral200)
            .padding(12.dp)
    ) {
        Text(
            "AI Response Model",
            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
            color = Neutral500
        )
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            // Provider dropdown
            Box(modifier = Modifier.weight(1f)) {
                Button(
                    onClick = { showProviderMenu = true },
                    modifier = Modifier.fillMaxWidth().height(40.dp),
                    shape = RoundedCornerShape(8.dp),
                    contentPadding = PaddingValues(horizontal = 12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Neutral100,
                        contentColor = BbxWhite
                    )
                ) {
                    Text(
                        providerDisplay,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                        fontSize = 13.sp
                    )
                    Text(" \u25BE", color = BbxDim, fontSize = 12.sp)
                }
                DropdownMenu(
                    expanded = showProviderMenu,
                    onDismissRequest = { showProviderMenu = false }
                ) {
                    providers.forEach { (id, name) ->
                        DropdownMenuItem(
                            text = {
                                Text(
                                    name,
                                    color = if (id == smsProvider) BbxAccent else BbxWhite,
                                    fontWeight = if (id == smsProvider) FontWeight.Bold else FontWeight.Normal
                                )
                            },
                            onClick = {
                                showProviderMenu = false
                                // When provider changes, pick the first model for that provider
                                val newModels = Constants.MODEL_CONFIG[id] ?: emptyList()
                                val firstModel = newModels.firstOrNull()?.first ?: ""
                                onProviderModelChange(id, firstModel)
                            }
                        )
                    }
                }
            }

            // Model dropdown
            Box(modifier = Modifier.weight(1f)) {
                Button(
                    onClick = { showModelMenu = true },
                    modifier = Modifier.fillMaxWidth().height(40.dp),
                    shape = RoundedCornerShape(8.dp),
                    contentPadding = PaddingValues(horizontal = 12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Neutral100,
                        contentColor = BbxWhite
                    )
                ) {
                    Text(
                        modelDisplay,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                        fontSize = 13.sp
                    )
                    Text(" \u25BE", color = BbxDim, fontSize = 12.sp)
                }
                DropdownMenu(
                    expanded = showModelMenu,
                    onDismissRequest = { showModelMenu = false }
                ) {
                    models.forEach { (modelId, displayName) ->
                        DropdownMenuItem(
                            text = {
                                Text(
                                    displayName,
                                    color = if (modelId == smsModel) BbxAccent else BbxWhite,
                                    fontWeight = if (modelId == smsModel) FontWeight.Bold else FontWeight.Normal
                                )
                            },
                            onClick = {
                                showModelMenu = false
                                onProviderModelChange(smsProvider, modelId)
                            }
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun SmsBubble(msg: SmsMsg) {
    val isOutbound = msg.direction == "outbound"

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (isOutbound) Alignment.End else Alignment.Start
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 300.dp)
                .clip(
                    RoundedCornerShape(
                        topStart = if (isOutbound) 16.dp else 4.dp,
                        topEnd = if (isOutbound) 4.dp else 16.dp,
                        bottomStart = 16.dp,
                        bottomEnd = 16.dp
                    )
                )
                .background(if (isOutbound) Color(0xFF1A3F65) else Neutral200)
                .padding(horizontal = 14.dp, vertical = 10.dp)
        ) {
            Column {
                Text(
                    msg.body,
                    color = if (isOutbound) Color(0xFFE4ECF5) else BbxWhite,
                    style = MaterialTheme.typography.bodyMedium,
                    lineHeight = 20.sp
                )
                Spacer(Modifier.height(4.dp))
                Row(
                    horizontalArrangement = Arrangement.End,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        msg.timestamp.takeLast(8).take(5),
                        style = MaterialTheme.typography.labelSmall,
                        color = if (isOutbound) Color(0xFFA0C3E6).copy(alpha = 0.7f) else Neutral500
                    )
                    if (isOutbound && msg.status.isNotBlank()) {
                        Spacer(Modifier.width(4.dp))
                        Text(
                            msg.status,
                            style = MaterialTheme.typography.labelSmall,
                            color = if (msg.status == "delivered") SolidGreen else BbxAccent
                        )
                    }
                }
            }
        }
    }
}
