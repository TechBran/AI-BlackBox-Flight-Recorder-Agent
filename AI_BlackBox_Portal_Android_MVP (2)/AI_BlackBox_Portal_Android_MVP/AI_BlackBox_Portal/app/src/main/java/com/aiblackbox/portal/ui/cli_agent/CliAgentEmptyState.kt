package com.aiblackbox.portal.ui.cli_agent

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.onLongClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

/**
 * Phase 4 / T21 — empty-state launch UI for [CliAgentScreen].
 *
 * Rendered when there is no active terminal session selected. Offers two
 * primary entry points:
 *   - **+ Terminal** — quick-launches a no-app zellij terminal session via
 *     [onLaunchProvider]("terminal"). Long-press falls through to
 *     [onChooseFolderForTerminal] so the user can route through the
 *     existing [AppFolderPicker] flow when they want to pin a workspace.
 *   - **Shortcuts ▾** — toggles inline reveal of four provider buttons
 *     (Claude / Gemini / Codex / Antigravity) in [PROVIDER_SHORTCUTS] order.
 *     Tapping a provider button invokes [onLaunchProvider] with that slug.
 *
 * **Stateless except for the shortcuts-expanded toggle**, which is local
 * because the screen-level state holder doesn't care whether the panel
 * is currently visible — that's pure UI ephemera.
 *
 * The toggle uses [rememberSaveable] so an open shortcuts panel survives
 * configuration change (rotation, theme switch). All other state — which
 * launches are in flight, which sessions exist — is hoisted to the caller.
 *
 * Invariants (see T21 brief):
 *   - Uses [LaunchButton] for every button — no new button composable.
 *   - Reads [PROVIDER_SHORTCUTS] + [titleCaseProvider] from
 *     [SessionSwitcherTopBar] (`internal` package members; no duplication).
 *   - [launchInFlight] is per-provider so the user can fire multiple
 *     independent launches and see independent spinners on each row.
 *   - Empty-state and [SessionSwitcherTopBar] read the SAME hoisted
 *     [launchInFlight] set so a launch fired from one surface shows a
 *     spinner on the other.
 *
 * Caller wires:
 *   ```
 *   CliAgentEmptyState(
 *       launchInFlight = state.launchInFlight,
 *       onLaunchProvider = { provider -> screenState.launch(provider) },
 *       onChooseFolderForTerminal = { state = State.FolderPicker },
 *   )
 *   ```
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun CliAgentEmptyState(
    launchInFlight: Set<String>,
    onLaunchProvider: (provider: String) -> Unit,
    onChooseFolderForTerminal: () -> Unit,
    modifier: Modifier = Modifier,
) {
    // Local UI-only state: whether the shortcuts panel is expanded.
    // Saved across configuration change via rememberSaveable so a user
    // who opened the panel keeps it open after rotation.
    var shortcutsExpanded by rememberSaveable { mutableStateOf(false) }

    Box(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 24.dp),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .widthIn(max = 320.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                text = "No active terminal",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(24.dp))

            // ── + Terminal (primary) ──────────────────────────────────────
            // Tap: quick-launch a no-app terminal session.
            // Long-press: fall through to AppFolderPicker for workspace pick.
            //
            // LaunchButton itself doesn't expose long-press (it wraps a
            // Material3 Button whose onClick is single-tap only). We wrap
            // it in a Box with combinedClickable to add long-press while
            // preserving the visual + busy contract of LaunchButton.
            val terminalBusy = "terminal" in launchInFlight
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .combinedClickable(
                        // The Button inside LaunchButton swallows taps and
                        // handles its own click — these handlers are the
                        // fallback for the surrounding Box surface (the
                        // long-press route). The visible primary tap path
                        // is the Button.onClick wired in LaunchButton.
                        onClick = {},
                        onLongClick = {
                            if (!terminalBusy) onChooseFolderForTerminal()
                        },
                        enabled = !terminalBusy,
                    )
                    // a11y: combinedClickable doesn't expose long-press to
                    // TalkBack by default. Declare an explicit long-click
                    // action so screen readers can announce/invoke it.
                    .semantics {
                        role = Role.Button
                        onLongClick(label = "Pick a workspace folder") {
                            if (!terminalBusy) {
                                onChooseFolderForTerminal()
                                true
                            } else {
                                false
                            }
                        }
                    },
            ) {
                LaunchButton(
                    label = "+ Terminal",
                    icon = Icons.Default.Add,
                    isLoading = terminalBusy,
                    enabled = true,
                    onClick = { onLaunchProvider("terminal") },
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            Spacer(Modifier.height(12.dp))

            // ── Shortcuts ▾ (secondary) ───────────────────────────────────
            // Toggles inline visibility of the 4 provider buttons below.
            LaunchButton(
                label = if (shortcutsExpanded) "Shortcuts ▴" else "Shortcuts ▾",
                icon = if (shortcutsExpanded) Icons.Default.ArrowDropDown else Icons.Default.PlayArrow,
                isLoading = false,
                enabled = true,
                onClick = { shortcutsExpanded = !shortcutsExpanded },
                modifier = Modifier.fillMaxWidth(),
            )

            // ── Inline expansion: provider shortcut buttons ───────────────
            // Animate the reveal so the user sees a clear "opened panel"
            // affordance. PROVIDER_SHORTCUTS is the canonical order
            // (claude, gemini, codex, antigravity) defined in
            // SessionSwitcherTopBar — single source of truth.
            AnimatedVisibility(
                visible = shortcutsExpanded,
                enter = fadeIn() + expandVertically(),
                exit = fadeOut() + shrinkVertically(),
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    PROVIDER_SHORTCUTS.forEach { providerSlug ->
                        val busy = providerSlug in launchInFlight
                        LaunchButton(
                            label = titleCaseProvider(providerSlug),
                            icon = Icons.Default.PlayArrow,
                            isLoading = busy,
                            enabled = true,
                            onClick = { onLaunchProvider(providerSlug) },
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            }
        }
    }
}
