package com.aiblackbox.portal.ui.cli_agent

import androidx.activity.compose.BackHandler
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.CliAgentProvider
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.ui.insets.LocalShowAppChrome
import kotlinx.coroutines.launch

/**
 * Top-level CLI Agent flow: picker → terminal → back-to-picker.
 *
 * Owns the picker-vs-terminal state machine and reads the persisted
 * provider selection from [BlackBoxStore.cliAgentProviderFlow]. When
 * the user taps a chip in [AppFolderPicker], the new provider is
 * persisted via [BlackBoxStore.setCliAgentProvider]; on next visit
 * the chip starts in the user's last choice.
 *
 * System back from the picker pops to the Tools menu via [onBackToTools].
 * System back from the terminal returns to the picker, preserving the
 * operator's place. The TerminalScreen branch installs its own
 * BackHandler, so the picker's BackHandler is only active while the
 * picker is on screen.
 */
@Composable
fun CliAgentScreen(
    origin: String,
    operator: String,
    onBackToTools: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val store = remember { BlackBoxStore(context) }
    val scope = rememberCoroutineScope()

    val api: BlackBoxApi = remember(origin) { BlackBoxApi(origin) }
    val repository = remember(api) { CliAgentSessionRepository(api) }

    val providerSlug by store.cliAgentProviderFlow
        .collectAsState(initial = CliAgentProvider.CLAUDE.slug)
    val selectedProvider = remember(providerSlug) {
        CliAgentProvider.fromSlug(providerSlug)
    }

    var state by remember { mutableStateOf<CliAgentInternalState>(CliAgentInternalState.Picker) }

    // T20: hide the floating app chrome (operator pill / snapshot count /
    // connected indicator) while a terminal session is on screen. The
    // chrome stays visible on the Picker branch so users can still see
    // operator + health while choosing a workspace. T21 will replace the
    // Picker branch with the empty-state launch UI; this CompositionLocal
    // toggle continues to apply only to the Terminal branch.
    val terminalActive = state is CliAgentInternalState.Terminal
    CompositionLocalProvider(LocalShowAppChrome provides !terminalActive) {
        when (val s = state) {
            CliAgentInternalState.Picker -> {
                BackHandler(enabled = true) { onBackToTools() }
                AppFolderPicker(
                    repository = repository,
                    operator = operator,
                    selectedProvider = selectedProvider,
                    onProviderSelected = { p ->
                        scope.launch { store.setCliAgentProvider(p.slug) }
                    },
                    onAppSelected = { slug, name ->
                        state = CliAgentInternalState.Terminal(slug, name, selectedProvider.slug)
                    },
                    modifier = modifier,
                )
            }
            is CliAgentInternalState.Terminal -> {
                TerminalScreen(
                    api = api,
                    operator = operator,
                    appSlug = s.appSlug,
                    appName = s.appName,
                    provider = s.provider,
                    onBack = { state = CliAgentInternalState.Picker },
                    modifier = modifier,
                )
            }
        }
    }
}

private sealed class CliAgentInternalState {
    data object Picker : CliAgentInternalState()
    data class Terminal(
        val appSlug: String,
        val appName: String,
        val provider: String,
    ) : CliAgentInternalState()
}
