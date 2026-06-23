package com.aiblackbox.portal.ui.devices

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.model.Device
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBg
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
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

// Portal device status colors
private val StatusOnlineGreen = Color(0xFF22C55E)
private val StatusOfflineRed = Color(0xFFEF4444)
private val StatusBusyAmber = Color(0xFFF59E0B)
private val StatusUnknownGray = Color(0xFF9CA3AF)
private val AdbBlue = Color(0xFF3B82F6)
private val PairPurple = Color(0xFFA78BFA)
private val RemoveRed = Color(0xFFF87171)

@Composable
fun DeviceManagerScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: DeviceViewModel = viewModel()
) {
    val devices by viewModel.filteredDevices.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val isSyncing by viewModel.isSyncing.collectAsState()
    val searchQuery by viewModel.searchQuery.collectAsState()
    val typeFilter by viewModel.typeFilter.collectAsState()
    val showDeleteConfirm by viewModel.showDeleteConfirm.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()

    val snackbarHostState = remember { SnackbarHostState() }
    val view = LocalView.current

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    LaunchedEffect(actionMessage) {
        actionMessage?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearActionMessage()
        }
    }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = BbxBlack,
        snackbarHost = { SnackbarHost(snackbarHostState) }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(start = 16.dp, end = 16.dp, bottom = 12.dp, top = 100.dp)
        ) {
            // Title row with Sync button
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "Devices",
                    style = MaterialTheme.typography.headlineMedium,
                    color = BbxWhite
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    // Sync Tailscale button
                    Button(
                        onClick = {
                            view.performPressFeedback()
                            viewModel.syncTailscale()
                        },
                        enabled = !isSyncing,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = BbxAccent,
                            disabledContainerColor = BbxAccent.copy(alpha = 0.5f)
                        ),
                        shape = RoundedCornerShape(RadiusSm)
                    ) {
                        if (isSyncing) {
                            CircularProgressIndicator(
                                color = BbxWhite,
                                modifier = Modifier.size(14.dp),
                                strokeWidth = 2.dp
                            )
                            Spacer(Modifier.width(6.dp))
                        }
                        Text(
                            if (isSyncing) "Syncing..." else "Sync Tailscale",
                            fontSize = 13.sp,
                            fontWeight = FontWeight.SemiBold
                        )
                    }
                    // Refresh button
                    IconButton(onClick = {
                        view.performPressFeedback()
                        viewModel.loadDevices()
                    }) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh", tint = BbxDim)
                    }
                }
            }
            Spacer(Modifier.height(12.dp))

            // Search + Type Filter bar (matching Portal .dm-top-bar)
            DeviceSearchFilterBar(
                searchQuery = searchQuery,
                onSearchChange = { viewModel.setSearchQuery(it) },
                typeFilter = typeFilter,
                onFilterChange = {
                    viewModel.setTypeFilter(it)
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

            // Device list or empty state
            if (devices.isEmpty() && !isLoading) {
                DeviceEmptyState()
            } else {
                LazyColumn(
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.fillMaxSize()
                ) {
                    items(devices, key = { it.id }) { device ->
                        DeviceCard(
                            device = device,
                            isAdbConnected = viewModel.isAdbConnected(device),
                            onConnect = {
                                viewModel.adbConnect(device.id)
                            },
                            onDisconnect = {
                                viewModel.adbDisconnect(device.id)
                            },
                            onHealth = {
                                viewModel.checkHealth(device.id)
                            },
                            onRemove = {
                                viewModel.requestRemove(device.id)
                            }
                        )
                    }
                }
            }
        }
    }

    // Delete confirmation dialog
    showDeleteConfirm?.let { deviceId ->
        AlertDialog(
            onDismissRequest = { viewModel.cancelRemove() },
            title = { Text("Remove Device", color = BbxWhite) },
            text = {
                Text(
                    "Remove device \"$deviceId\" from the registry?",
                    color = BbxDim
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    view.performPressFeedback()
                    viewModel.confirmRemove()
                }) {
                    Text("Remove", color = StatusOfflineRed)
                }
            },
            dismissButton = {
                TextButton(onClick = { view.performPressFeedback(); viewModel.cancelRemove() }) {
                    Text("Cancel", color = BbxDim)
                }
            },
            containerColor = Neutral100,
            tonalElevation = 0.dp
        )
    }
}

// =============================================================================
// Search + Type Filter Bar
// =============================================================================

@Composable
private fun DeviceSearchFilterBar(
    searchQuery: String,
    onSearchChange: (String) -> Unit,
    typeFilter: String,
    onFilterChange: (String) -> Unit
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        OutlinedTextField(
            value = searchQuery,
            onValueChange = onSearchChange,
            modifier = Modifier.weight(1f),
            placeholder = { Text("Search devices...", color = Neutral500, fontSize = 13.sp) },
            leadingIcon = {
                Icon(Icons.Default.Search, contentDescription = null, tint = Neutral500, modifier = Modifier.size(18.dp))
            },
            singleLine = true,
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900),
            colors = glassFieldColors(),
            shape = RoundedCornerShape(RadiusSm)
        )

        DeviceTypeDropdown(
            selected = typeFilter,
            onSelect = onFilterChange
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DeviceTypeDropdown(selected: String, onSelect: (String) -> Unit) {
    val feedback = rememberPressFeedback()
    var expanded by remember { mutableStateOf(false) }
    val options = listOf(
        "all" to "All Types",
        "android" to "Android",
        "linux" to "Linux",
        "windows" to "Windows",
        "macos" to "macOS"
    )

    ExposedDropdownMenuBox(
        expanded = expanded,
        onExpandedChange = { expanded = it }
    ) {
        OutlinedTextField(
            value = options.find { it.first == selected }?.second ?: "All Types",
            onValueChange = {},
            readOnly = true,
            modifier = Modifier
                .width(130.dp)
                .menuAnchor(MenuAnchorType.PrimaryNotEditable),
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = Neutral900, fontSize = 13.sp),
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            colors = glassFieldColors(),
            singleLine = true,
            shape = RoundedCornerShape(RadiusSm)
        )
        ExposedDropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false },
            containerColor = Neutral100
        ) {
            options.forEach { (value, label) ->
                DropdownMenuItem(
                    text = { Text(label, color = Neutral900) },
                    onClick = { feedback(); onSelect(value); expanded = false }
                )
            }
        }
    }
}

// =============================================================================
// Device Card (matching Portal .dm-device-card)
// =============================================================================

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun DeviceCard(
    device: Device,
    isAdbConnected: Boolean,
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    onHealth: () -> Unit,
    onRemove: () -> Unit
) {
    val status = device.status
    val isAndroid = device.protocol == "adb"

    // Portal accent colors by status
    val accentColor = when {
        isAdbConnected -> AdbBlue
        status == "online" -> StatusOnlineGreen
        status == "offline" -> StatusOfflineRed
        status == "busy" -> StatusBusyAmber
        else -> StatusUnknownGray
    }

    // Device type icon (Portal: getDeviceIcon)
    val typeIcon = when (device.deviceType) {
        "android" -> "\uD83D\uDCF1"
        "linux" -> "\uD83D\uDC27"
        "windows" -> "\uD83E\uDE9F"
        "macos" -> "\uD83C\uDF4E"
        else -> "\uD83D\uDCBB"
    }

    // Border uses accent color; card bg stays solid black
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
                .padding(start = 14.dp, end = 14.dp, top = 12.dp, bottom = 12.dp)
        ) {
            // Header: icon + name + ADB badge + status badge
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth()
            ) {
                // Device type icon
                Text(
                    typeIcon,
                    fontSize = 18.sp,
                    modifier = Modifier.width(24.dp)
                )
                Spacer(Modifier.width(8.dp))
                // Device name
                Text(
                    device.name,
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = Neutral900,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f)
                )
                // ADB connected badge
                if (isAdbConnected) {
                    Spacer(Modifier.width(6.dp))
                    Text(
                        "ADB",
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold,
                        color = AdbBlue,
                        modifier = Modifier
                            .clip(RoundedCornerShape(RadiusXs))
                            .background(AdbBlue.copy(alpha = 0.15f))
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    )
                }
                Spacer(Modifier.width(6.dp))
                // Status badge (Portal: .dm-status-badge)
                DeviceStatusBadge(status)
            }

            // Description
            device.description?.let { desc ->
                if (desc.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(desc, fontSize = 12.sp, color = Neutral500)
                }
            }

            Spacer(Modifier.height(8.dp))

            // Metadata grid (Portal: .dm-card-meta)
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(12.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                DeviceMetaItem("IP", device.tailscaleIp)
                if (device.deviceType.isNotBlank()) {
                    DeviceMetaItem("Type", device.deviceType)
                }
                if (device.protocol.isNotBlank()) {
                    DeviceMetaItem("Protocol", device.protocol.uppercase())
                }
                if (isAndroid && device.adbPort > 0) {
                    DeviceMetaItem("ADB Port", device.adbPort.toString())
                }
                device.metadata?.let { meta ->
                    meta.model?.let { DeviceMetaItem("Model", it) }
                    meta.androidVersion?.let { DeviceMetaItem("Android", it) }
                    meta.screenSize?.let { DeviceMetaItem("Screen", it) }
                }
                if (device.owner.isNotBlank()) {
                    DeviceMetaItem("Owner", device.owner)
                }
            }

            Spacer(Modifier.height(10.dp))

            // Action buttons (Portal: .dm-card-actions)
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                if (isAndroid) {
                    if (isAdbConnected) {
                        DeviceActionButton(
                            text = "Disconnect",
                            onClick = onDisconnect,
                            textColor = StatusOfflineRed,
                            bgColor = StatusOfflineRed.copy(alpha = 0.12f),
                            borderColor = StatusOfflineRed.copy(alpha = 0.2f)
                        )
                    } else {
                        DeviceActionButton(
                            text = "Connect",
                            onClick = onConnect,
                            textColor = StatusOnlineGreen,
                            bgColor = StatusOnlineGreen.copy(alpha = 0.12f),
                            borderColor = StatusOnlineGreen.copy(alpha = 0.2f)
                        )
                    }
                }
                DeviceActionButton(
                    text = "Health",
                    onClick = onHealth,
                    textColor = Neutral700,
                    bgColor = Neutral200,
                    borderColor = Color(0x10FFFFFF)
                )
                DeviceActionButton(
                    text = "Remove",
                    onClick = onRemove,
                    textColor = RemoveRed,
                    bgColor = StatusOfflineRed.copy(alpha = 0.08f),
                    borderColor = StatusOfflineRed.copy(alpha = 0.12f)
                )
            }
        }
    }
}

// =============================================================================
// Device sub-components
// =============================================================================

@Composable
private fun DeviceStatusBadge(status: String) {
    val (color, bg) = when (status) {
        "online" -> StatusOnlineGreen to StatusOnlineGreen.copy(alpha = 0.15f)
        "offline" -> StatusOfflineRed to StatusOfflineRed.copy(alpha = 0.15f)
        "busy" -> StatusBusyAmber to StatusBusyAmber.copy(alpha = 0.15f)
        else -> StatusUnknownGray to StatusUnknownGray.copy(alpha = 0.15f)
    }
    Text(
        status.uppercase(),
        fontSize = 11.sp,
        fontWeight = FontWeight.SemiBold,
        color = color,
        letterSpacing = 0.5.sp,
        modifier = Modifier
            .clip(RoundedCornerShape(50))
            .background(bg)
            .padding(horizontal = 8.dp, vertical = 2.dp)
    )
}

@Composable
private fun DeviceMetaItem(label: String, value: String) {
    Row {
        Text(
            "$label: ",
            fontSize = 12.sp,
            color = Neutral700,
            fontWeight = FontWeight.Medium
        )
        Text(value, fontSize = 12.sp, color = Neutral500)
    }
}

@Composable
private fun DeviceActionButton(
    text: String,
    onClick: () -> Unit,
    textColor: Color,
    bgColor: Color,
    borderColor: Color
) {
    val view = LocalView.current
    Button(
        onClick = {
            view.performPressFeedback()
            onClick()
        },
        colors = ButtonDefaults.buttonColors(
            containerColor = bgColor,
            contentColor = textColor
        ),
        shape = RoundedCornerShape(RadiusXs),
        border = androidx.compose.foundation.BorderStroke(1.dp, borderColor),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 10.dp, vertical = 4.dp),
        modifier = Modifier.height(30.dp)
    ) {
        Text(text, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun DeviceEmptyState() {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                "No devices found",
                style = MaterialTheme.typography.bodyLarge,
                color = Neutral500,
                fontWeight = FontWeight.Medium
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "Click \"Sync Tailscale\" to discover devices on your mesh network",
                fontSize = 13.sp,
                color = Neutral400,
                modifier = Modifier.padding(horizontal = 40.dp),
                lineHeight = 18.sp
            )
        }
    }
}

// =============================================================================
// Helpers
// =============================================================================

@Composable
private fun glassFieldColors() = OutlinedTextFieldDefaults.colors(
    focusedBorderColor = Color(0x33FFFFFF),
    unfocusedBorderColor = Color(0x14FFFFFF),
    cursorColor = BbxWhite,
    focusedContainerColor = Neutral100,
    unfocusedContainerColor = Neutral100,
    focusedTextColor = Neutral900,
    unfocusedTextColor = Neutral900
)
