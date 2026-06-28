package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update

/**
 * Process-wide bus carrying live on-device-download state so the ViewModel can
 * observe a download that actually runs inside [com.aiblackbox.portal.ModelDownloadService]
 * (a foreground Service) — which survives screen navigation that would otherwise
 * cancel the ViewModel's coroutine scope (Phase C, durable downloads).
 *
 * The Service is the sole producer (`update`/`clear`); the ViewModel is the sole
 * consumer (`flow`). Keyed by bundle slug so multiple downloads can coexist. Pure
 * Kotlin — no Android types — so it unit-tests with plain JUnit.
 */
object DownloadProgressBus {
    /** Terminal-or-running status for a single slug's download. */
    enum class Status { RUNNING, SUCCESS, FAILED }

    /**
     * A snapshot of one slug's download.
     *
     * @param slug the bundle slug being downloaded.
     * @param fraction progress in 0f..1f (or a negative sentinel for indeterminate).
     * @param status RUNNING while in flight, then SUCCESS or FAILED terminally.
     * @param error a user-facing failure message when [status] is FAILED, else null.
     */
    data class State(
        val slug: String,
        val fraction: Float,
        val status: Status,
        val error: String? = null,
    )

    private val _flow = MutableStateFlow<Map<String, State>>(emptyMap())

    /** Latest per-slug download state. The ViewModel collects this. */
    val flow: StateFlow<Map<String, State>> = _flow

    /** Publish (or overwrite) the state for [s.slug]. */
    fun update(s: State) = _flow.update { it + (s.slug to s) }

    /** Drop [slug] once the ViewModel has consumed its terminal state. */
    fun clear(slug: String) = _flow.update { it - slug }

    /** Drop everything (used to isolate tests). */
    fun clearAll() = _flow.update { emptyMap() }
}
