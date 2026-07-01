package com.aiblackbox.portal.ui.devices

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.model.MESH_PROVIDER_CHOICES
import com.aiblackbox.portal.data.model.MeshDevice
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBg
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.Neutral900
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.RadiusXs
import com.aiblackbox.portal.ui.theme.SolidGreen

private val OnlineGreen = Color(0xFF22C55E)
private val OfflineGray = Color(0xFF9CA3AF)

/**
 * System-Menu "Devices" → Tailnet Mesh view (M3 task 3.8). Lists every tailnet device
 * from `GET /devices/mesh` and, per device, lets the user assign the owning operator,
 * mark the operator's primary, and pick the default frontier provider — mirroring the
 * Portal Devices view. Self-contained (owns its [MeshDeviceViewModel]); rendered as a
 * single item inside the DeviceManager scroll.
 */
@Composable
fun MeshDevicesSection(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: MeshDeviceViewModel = viewModel(),
) {
    val context = LocalContext.current
    val devices by viewModel.devices.collectAsState()
    val operators by viewModel.operators.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val loadedOnce by viewModel.loadedOnce.collectAsState()
    val error by viewModel.error.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Surface action feedback as a lightweight toast (the section may be embedded in a
    // scroll without a Scaffold snackbar host).
    LaunchedEffect(actionMessage) {
        actionMessage?.let {
            android.widget.Toast.makeText(context, it, android.widget.Toast.LENGTH_SHORT).show()
            viewModel.clearActionMessage()
        }
    }

    Column(modifier = modifier.fillMaxWidth()) {
        // Header: title + online count + refresh
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text(
                    "Tailnet Mesh",
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = BbxAccent,
                )
                Text(
                    "Assign owner, primary + default provider per device",
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                )
            }
            IconButton(onClick = { viewModel.refresh() }, enabled = !isLoading) {
                if (isLoading) {
                    CircularProgressIndicator(
                        color = BbxAccent, modifier = Modifier.size(18.dp), strokeWidth = 2.dp,
                    )
                } else {
                    Icon(Icons.Default.Refresh, contentDescription = "Refresh", tint = BbxDim)
                }
            }
        }

        Spacer(Modifier.height(10.dp))

        when {
            error != null -> MeshInlineNote("⚠️ ${error}", BbxAccent)
            !loadedOnce && isLoading -> MeshInlineNote("Loading tailnet…", Neutral500)
            devices.isEmpty() -> MeshInlineNote(
                "No tailnet devices found. Ensure Tailscale is up and this box can read “tailscale status”.",
                Neutral500,
            )
            else -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                devices.forEach { device ->
                    MeshDeviceCard(
                        device = device,
                        operators = operators,
                        onAssignOperator = { op -> viewModel.assignOperator(device.id, op) },
                        onSetPrimary = { device.owner?.let { viewModel.setPrimary(device.id, it) } },
                        onSetProvider = { p -> viewModel.setDefaultProvider(device.id, p, device.owner) },
                    )
                }
            }
        }
    }
}

@Composable
private fun MeshInlineNote(text: String, color: Color) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = color, lineHeight = 18.sp)
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MeshDeviceCard(
    device: MeshDevice,
    operators: List<String>,
    onAssignOperator: (String) -> Unit,
    onSetPrimary: () -> Unit,
    onSetProvider: (String?) -> Unit,
) {
    val typeIcon = when (device.type) {
        "android" -> "📱"
        "linux" -> "🐧"
        "windows" -> "🪟"
        "macos" -> "🍎"
        else -> "💻"
    }
    val accent = if (device.online) OnlineGreen else OfflineGray

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(GlassBg)
            .border(1.dp, accent.copy(alpha = 0.35f), RoundedCornerShape(RadiusMd)),
    ) {
        Column(Modifier.fillMaxWidth().padding(14.dp)) {
            // Header: online dot + icon + name + primary badge
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                Box(
                    Modifier.size(9.dp).clip(RoundedCornerShape(50)).background(accent),
                )
                Spacer(Modifier.width(8.dp))
                Text(typeIcon, fontSize = 18.sp, modifier = Modifier.width(24.dp))
                Spacer(Modifier.width(4.dp))
                Text(
                    device.name.ifBlank { device.id },
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = Neutral900,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                if (device.isPrimary) {
                    Text(
                        "★ PRIMARY",
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold,
                        color = SolidGreen,
                        modifier = Modifier
                            .clip(RoundedCornerShape(RadiusXs))
                            .background(SolidGreen.copy(alpha = 0.15f))
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                    )
                }
            }

            Spacer(Modifier.height(6.dp))

            // Meta: tailnet addr + type + online state
            FlowRow(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                device.tailnet?.let { MeshMeta("Tailnet", it, mono = true) }
                if (device.type.isNotBlank()) MeshMeta("Type", device.type)
                MeshMeta("State", if (device.online) "online" else "offline")
            }

            Spacer(Modifier.height(10.dp))

            // Owner picker
            MeshLabeledPicker(
                label = "Owner",
                value = device.owner?.takeIf { it.isNotBlank() } ?: "Unassigned",
                options = operators,
                onSelect = { onAssignOperator(it) },
                emptyHint = "No operators",
            )

            Spacer(Modifier.height(8.dp))

            // Provider picker (needs an owner — provider is operator-isolated)
            MeshLabeledPicker(
                label = "Provider",
                value = device.defaultProvider ?: "Default",
                options = listOf("Default") + MESH_PROVIDER_CHOICES,
                enabled = device.isClaimed,
                onSelect = { choice -> onSetProvider(if (choice == "Default") null else choice) },
                emptyHint = null,
            )

            Spacer(Modifier.height(10.dp))

            // Primary action (claimed + not already primary)
            if (device.isClaimed && !device.isPrimary) {
                MeshActionButton("Make Primary", onSetPrimary)
            } else if (!device.isClaimed) {
                Text(
                    "Assign an owner to enable primary + provider.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                )
            }
        }
    }
}

@Composable
private fun MeshMeta(label: String, value: String, mono: Boolean = false) {
    Row {
        Text("$label: ", fontSize = 12.sp, color = Neutral700, fontWeight = FontWeight.Medium)
        Text(
            value,
            fontSize = 12.sp,
            color = Neutral500,
            fontFamily = if (mono) FontFamily.Monospace else FontFamily.Default,
        )
    }
}

/** A labeled tap-to-open dropdown, styled to match the DeviceManager glass cards. */
@Composable
private fun MeshLabeledPicker(
    label: String,
    value: String,
    options: List<String>,
    onSelect: (String) -> Unit,
    enabled: Boolean = true,
    emptyHint: String?,
) {
    var expanded by remember { mutableStateOf(false) }
    Column {
        Text(label, fontSize = 11.sp, color = Neutral700, fontWeight = FontWeight.Medium)
        Spacer(Modifier.height(4.dp))
        Box {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(RadiusSm))
                    .background(Neutral100)
                    .border(1.dp, GlassBorder, RoundedCornerShape(RadiusSm))
                    .then(
                        if (enabled) Modifier.clickFeedback { expanded = true } else Modifier,
                    )
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    value,
                    style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Medium),
                    color = if (enabled) BbxWhite else Neutral500,
                    modifier = Modifier.weight(1f),
                    maxLines = 1,
                )
                Text("▾", color = Neutral500)
            }
            DropdownMenu(
                expanded = expanded,
                onDismissRequest = { expanded = false },
                modifier = Modifier.background(Neutral100),
            ) {
                if (options.isEmpty() && emptyHint != null) {
                    DropdownMenuItem(
                        text = { Text(emptyHint, color = Neutral500) },
                        onClick = {},
                        enabled = false,
                    )
                }
                options.forEach { option ->
                    val selected = option == value
                    DropdownMenuItem(
                        text = {
                            Text(
                                option,
                                color = if (selected) BbxAccent else BbxWhite,
                                fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
                            )
                        },
                        onClick = {
                            expanded = false
                            if (!selected) onSelect(option)
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun MeshActionButton(text: String, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusXs))
            .background(BbxAccent.copy(alpha = 0.14f))
            .border(1.dp, BbxAccent.copy(alpha = 0.35f), RoundedCornerShape(RadiusXs))
            .clickFeedback(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 8.dp),
    ) {
        Text(text, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = BbxAccent)
    }
}
