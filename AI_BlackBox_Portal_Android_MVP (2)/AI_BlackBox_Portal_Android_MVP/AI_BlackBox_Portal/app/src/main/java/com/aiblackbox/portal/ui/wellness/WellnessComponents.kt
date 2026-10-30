package com.aiblackbox.portal.ui.wellness

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import androidx.core.net.toUri
import android.provider.Settings
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.health.connect.client.HealthConnectClient
import com.aiblackbox.portal.WellnessRationaleActivity
import com.aiblackbox.portal.data.wellness.WellnessCatalog
import com.aiblackbox.portal.data.wellness.WellnessConsent
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBg
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen

internal val HealthAmber = Color(0xFFF5A524)

/** A glass card with a title, the house style of the Devices and Settings screens. */
@Composable
internal fun HealthCard(title: String, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(GlassBg)
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
            .padding(14.dp),
    ) {
        Text(title, style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.SemiBold), color = BbxAccent)
        Spacer(Modifier.height(8.dp))
        content()
    }
}

@Composable
internal fun HealthNote(text: String, color: Color = BbxDim) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = color, lineHeight = 18.sp)
}

@Composable
internal fun StatusChipView(chip: WellnessRules.StatusChip) {
    val color = when (chip) {
        WellnessRules.StatusChip.ACTIVE -> SolidGreen
        WellnessRules.StatusChip.NEEDS_ATTENTION -> HealthAmber
        WellnessRules.StatusChip.PAUSED -> Neutral700
        WellnessRules.StatusChip.NOT_SET_UP -> Neutral500
    }
    Text(
        chip.label,
        fontSize = 12.sp,
        fontWeight = FontWeight.Bold,
        color = color,
        modifier = Modifier
            .clip(RoundedCornerShape(50))
            .background(color.copy(alpha = 0.15f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
    )
}

@Composable
internal fun HealthSwitchRow(label: String, checked: Boolean, enabled: Boolean = true, note: String? = null,
                             onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(label, style = MaterialTheme.typography.bodyMedium, color = if (enabled) BbxWhite else Neutral500)
            note?.let { HealthNote(it, Neutral700) }
        }
        Spacer(Modifier.width(8.dp))
        Switch(
            checked = checked, onCheckedChange = onChange, enabled = enabled,
            colors = SwitchDefaults.colors(checkedTrackColor = BbxAccent),
        )
    }
}

/** Off / Ask / Proactive for one category. */
@Composable
internal fun ConsentSelector(value: WellnessConsent, onChange: (WellnessConsent) -> Unit) {
    val options = WellnessConsent.entries
    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
        options.forEachIndexed { i, option ->
            SegmentedButton(
                selected = value == option,
                onClick = { onChange(option) },
                shape = SegmentedButtonDefaults.itemShape(index = i, count = options.size),
                colors = SegmentedButtonDefaults.colors(
                    activeContainerColor = BbxAccent.copy(alpha = 0.15f),
                    activeContentColor = BbxAccent,
                    activeBorderColor = BbxAccent.copy(alpha = 0.4f),
                    inactiveContainerColor = Neutral100,
                    inactiveContentColor = Neutral700,
                    inactiveBorderColor = Neutral300,
                ),
                label = { Text(option.label, style = MaterialTheme.typography.bodySmall) },
            )
        }
    }
}

@Composable
internal fun HealthButton(label: String, enabled: Boolean = true, color: Color = BbxAccent, onClick: () -> Unit) {
    TextButton(onClick = onClick, enabled = enabled) { Text(label, color = if (enabled) color else Neutral500) }
}

@Composable
internal fun TroubleCardView(card: WellnessRules.TroubleCard, onFix: (WellnessRules.TroubleFix) -> Unit) {
    Box(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(HealthAmber.copy(alpha = 0.08f))
            .border(1.dp, HealthAmber.copy(alpha = 0.35f), RoundedCornerShape(RadiusMd))
            .padding(12.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(card.title, style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = BbxWhite)
            HealthNote(card.body)
            if (card.fix != WellnessRules.TroubleFix.NONE) HealthButton(card.fix.label) { onFix(card.fix) }
        }
    }
}

/** The intents behind trouble-card fixes and setup links. Each one degrades to a toast. */
internal object HealthIntents {

    private fun start(context: Context, intent: Intent, fallback: String) {
        try {
            context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(context, fallback, Toast.LENGTH_LONG).show()
        } catch (e: SecurityException) {
            Toast.makeText(context, fallback, Toast.LENGTH_LONG).show()
        }
    }

    fun openHealthConnect(context: Context) =
        start(context, Intent(HealthConnectClient.ACTION_HEALTH_CONNECT_SETTINGS), "Open Health Connect from your phone's Settings.")

    fun getHealthConnect(context: Context) = start(
        context,
        Intent(Intent.ACTION_VIEW, "market://details?id=${WellnessCatalog.HEALTH_CONNECT_PACKAGE}".toUri()),
        "Install or update Health Connect from your app store.",
    )

    fun openSamsungHealth(context: Context) {
        val launch = context.packageManager.getLaunchIntentForPackage(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE)
        if (launch == null) Toast.makeText(context, "Samsung Health isn't installed.", Toast.LENGTH_LONG).show()
        else start(context, launch, "Open Samsung Health.")
    }

    /** The battery-optimisation LIST (no special permission needed to open it). */
    fun openBatterySettings(context: Context) =
        start(context, Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS), "Open Settings, Battery, for BlackBox.")

    fun openPrivacyPolicy(context: Context) =
        start(context, Intent(context, WellnessRationaleActivity::class.java), "The privacy policy couldn't open.")

    /** Share the probe report as text (counts and dates only; nothing is sent to the box). */
    fun shareText(context: Context, subject: String, text: String) {
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, subject)
            putExtra(Intent.EXTRA_TEXT, text)
        }
        start(context, Intent.createChooser(send, subject), "No app can share this.")
    }
}
