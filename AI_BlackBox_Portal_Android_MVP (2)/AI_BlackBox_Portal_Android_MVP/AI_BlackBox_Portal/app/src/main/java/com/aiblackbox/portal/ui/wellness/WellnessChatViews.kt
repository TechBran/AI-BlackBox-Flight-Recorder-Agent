package com.aiblackbox.portal.ui.wellness

import android.text.format.DateFormat
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.window.SecureFlagPolicy
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.wellness.ShowNumbersResult
import com.aiblackbox.portal.data.wellness.WellnessChatRules
import com.aiblackbox.portal.data.wellness.WellnessGraph
import com.aiblackbox.portal.data.wellness.WellnessRefClient
import com.aiblackbox.portal.data.wellness.WellnessRefSheet
import com.aiblackbox.portal.data.wellness.WellnessUsed
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral600
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusMd
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.ZoneId

/**
 * The chat's health surfaces (spec §6.1 "Chat"): the footer under a reply whose turn used the
 * person's health data, and Show numbers on a masked turn. Every decision is a
 * [WellnessChatRules] call; these only render.
 */

/** What a chat screen knows about Health for its bubbles: who the chat is for, who this phone is the source for. */
data class WellnessChatContext(
    val operator: String,
    val enrolledOperator: String?,
    val sourceLabel: String?,
)

/** [WellnessChatContext] for [operator], read off the main thread; re-read when [refreshKey] changes. */
@Composable
fun rememberWellnessChatContext(operator: String?, refreshKey: Any? = null): WellnessChatContext? {
    val app = LocalContext.current.applicationContext
    val state = produceState<WellnessChatContext?>(initialValue = null, operator, refreshKey) {
        value = withContext(Dispatchers.IO) {
            runCatching {
                WellnessChatContext(
                    operator = operator.orEmpty(),
                    enrolledOperator = WellnessGraph.binding(app).current()?.operator,
                    sourceLabel = WellnessGraph.state(app).load().sourceLabel,
                )
            }.getOrNull()
        }
    }
    return state.value
}

/** "Used your health data · <source> · as of 07:10 · Anthropic": small and calm, no numbers. */
@Composable
fun WellnessFooter(used: WellnessUsed, sourceLabel: String?, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val line = remember(used, sourceLabel) {
        WellnessChatRules.footerLine(used, sourceLabel, System.currentTimeMillis(), ZoneId.systemDefault(),
            DateFormat.is24HourFormat(context))
    }
    Text(
        text = line,
        style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp),
        color = Neutral700,
        modifier = modifier.padding(top = 2.dp),
    )
}

/**
 * The footer and Show numbers for one assistant bubble: the footer when its turn used health
 * data, Show numbers when its text is masked and this phone is the chat operator's source.
 */
@Composable
fun WellnessBubbleExtras(
    text: String,
    wellnessRef: String?,
    used: WellnessUsed?,
    wellness: WellnessChatContext?,
) {
    used?.let { WellnessFooter(it, wellness?.sourceLabel) }
    val refs = used?.refs ?: listOfNotNull(wellnessRef)
    if (wellness != null &&
        WellnessChatRules.offersShowNumbers(text, refs, wellness.operator, wellness.enrolledOperator)
    ) {
        ShowNumbersAction(WellnessChatRules.showNumbersRefs(text, refs), wellness.operator)
    }
}

/**
 * Show numbers on a ledger snapshot (Timeline and the SNAP peek sheet): a masked turn carries
 * the refs in its WELLNESS tag, and only its own operator's enrolled phone can read them.
 */
@Composable
fun SnapshotShowNumbers(text: String?, snapshotOperator: String?) {
    if (!WellnessChatRules.isMasked(text)) return
    val wellness = rememberWellnessChatContext(snapshotOperator) ?: return
    if (WellnessChatRules.offersShowNumbers(text, emptyList(), snapshotOperator, wellness.enrolledOperator)) {
        ShowNumbersAction(WellnessChatRules.showNumbersRefs(text), snapshotOperator.orEmpty().trim())
    }
}

/**
 * The "Show numbers" button and its private sheet. The numbers live only while the sheet is
 * open: dismissing it, or the app going to the background, drops them; nothing is persisted
 * or logged, and the sheet's window is FLAG_SECURE (no screenshots, a blank recents preview).
 */
@Composable
fun ShowNumbersAction(refs: List<String>, operator: String) {
    if (refs.isEmpty() || operator.isBlank()) return
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var open by remember { mutableStateOf(false) }
    var result by remember { mutableStateOf<ShowNumbersResult?>(null) }
    val close = {
        open = false
        result = null
    }
    TextButton(onClick = {
        open = true
        result = null
        scope.launch {
            val origin = withContext(Dispatchers.IO) { WellnessGraph.boxOrigin(context) }
            val fetched = if (origin == null) ShowNumbersResult.NotAvailable
            else WellnessRefClient.fetch(BlackBoxApi(origin), refs, operator)
            if (open) result = fetched
        }
    }) {
        Text("Show numbers", color = BbxAccent, style = MaterialTheme.typography.labelMedium)
    }
    if (open) {
        DismissOnBackground(onBackground = close)
        ShowNumbersDialog(result, onDismiss = close)
    }
}

/** Calls [onBackground] when the host lifecycle stops (the app left the screen). */
@Composable
private fun DismissOnBackground(onBackground: () -> Unit) {
    val owner = LocalLifecycleOwner.current
    val latest by rememberUpdatedState(onBackground)
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_STOP) latest() }
        owner.lifecycle.addObserver(observer)
        onDispose { owner.lifecycle.removeObserver(observer) }
    }
}

@Composable
private fun ShowNumbersDialog(result: ShowNumbersResult?, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val use24h = remember { DateFormat.is24HourFormat(context) }
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(securePolicy = SecureFlagPolicy.SecureOn, usePlatformDefaultWidth = false),
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp)
                .clip(RoundedCornerShape(RadiusMd))
                .background(Neutral100)
                .padding(16.dp)
                .heightIn(max = 560.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text("Your health numbers", color = BbxWhite,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold))
            Column(
                Modifier
                    .weight(1f, fill = false)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                when (result) {
                    null -> Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = BbxAccent, strokeWidth = 2.dp, modifier = Modifier.size(24.dp))
                    }
                    ShowNumbersResult.NotAvailable -> Text("Not available", color = BbxDim,
                        style = MaterialTheme.typography.bodyMedium)
                    is ShowNumbersResult.Loaded -> result.sheets.forEach { RefSection(it, use24h) }
                }
            }
            Text(
                "Shown on this phone only. BlackBox's memory keeps this conversation without the numbers.",
                color = Neutral600, style = MaterialTheme.typography.labelSmall,
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = onDismiss) { Text("Close", color = BbxAccent) }
            }
        }
    }
}

@Composable
private fun RefSection(sheet: WellnessRefSheet, use24h: Boolean) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(sheet.title, color = BbxWhite, style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.SemiBold))
        val asOf = WellnessChatRules.asOfLabel(sheet.asOf, System.currentTimeMillis(), ZoneId.systemDefault(), use24h)
        if (asOf != null) {
            Text("As of $asOf" + if (sheet.stale) " (not synced since)" else "", color = Neutral700,
                style = MaterialTheme.typography.labelSmall)
        }
        for (row in sheet.rows) {
            Column(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .background(Neutral300.copy(alpha = 0.25f))
                    .padding(horizontal = 10.dp, vertical = 6.dp),
            ) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(row.label, color = BbxDim, style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.weight(1f, fill = false))
                    Text(row.value, color = BbxWhite,
                        style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
                        modifier = Modifier.padding(start = 8.dp))
                }
                row.detail?.let { Text(it, color = Neutral700, style = MaterialTheme.typography.labelSmall) }
            }
        }
        for (line in sheet.coverage) Text(line, color = Neutral700, style = MaterialTheme.typography.labelSmall)
        for (line in sheet.notes) Text(line, color = Neutral700, style = MaterialTheme.typography.labelSmall)
    }
}
