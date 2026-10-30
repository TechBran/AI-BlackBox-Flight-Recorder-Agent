package com.aiblackbox.portal.ui.timeline

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.SearchResponse
import com.aiblackbox.portal.data.model.SnapshotResult
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.wellness.WellnessLedgerRead
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private const val TAG = "TimelineVM"
private const val POLL_INTERVAL_MS = 10_000L  // Refresh every 10 seconds

/**
 * The peek sheet's body, TAGGED with the snapshot it was fetched for
 * (A07-components-rest-01): the sheet renders it only under that id, so snapshot A's
 * text can never sit under snapshot B's title while B loads or after B's fetch fails.
 */
data class SnapshotPeek(
    val snapId: String,
    val content: String? = null,
    val loading: Boolean = true,
    val error: String? = null,
)

/** What the peek sheet shows for the id on its title. */
sealed class PeekView {
    data object Loading : PeekView()
    data class Error(val message: String) : PeekView()
    data class Body(val text: String) : PeekView()
}

/** Pure: a body tagged with another id is never shown, a failed fetch shows its error. */
fun peekViewFor(peek: SnapshotPeek?, currentSnapId: String): PeekView = when {
    peek == null || peek.snapId != currentSnapId -> PeekView.Loading
    peek.content != null -> PeekView.Body(peek.content)
    peek.error != null -> PeekView.Error(peek.error)
    else -> PeekView.Loading
}

class TimelineViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null

    /**
     * (M7) The operator on the app's screen, read when a snapshot is fetched: the ledger read
     * is signed for it (WellnessLedgerRead), so the enrolled phone's own health turns come back
     * masked with their gauge and Show numbers can offer them. Tests replace it.
     */
    @androidx.annotation.VisibleForTesting
    internal var screenOperator: suspend () -> String = {
        BlackBoxStore(getApplication<Application>()).operator.first()
    }

    private suspend fun ledgerRead(api: BlackBoxApi, snapId: String): String =
        WellnessLedgerRead.fetch(api, snapId, runCatching { screenOperator() }.getOrNull())
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    private var pollJob: Job? = null
    private var currentOperator = ""

    private val _results = MutableStateFlow<List<SnapshotResult>>(emptyList())
    val results: StateFlow<List<SnapshotResult>> = _results.asStateFlow()

    private val _isSearching = MutableStateFlow(false)
    val isSearching: StateFlow<Boolean> = _isSearching.asStateFlow()

    // Tracks whether user is actively searching (disables auto-poll to avoid overwriting results)
    private var isUserSearching = false

    /** True while search results are on screen, so the live feed must not replace them. */
    @androidx.annotation.VisibleForTesting
    internal val inSearchMode: Boolean get() = isUserSearching

    private val _selectedSnapshot = MutableStateFlow<SnapshotResult?>(null)
    val selectedSnapshot: StateFlow<SnapshotResult?> = _selectedSnapshot.asStateFlow()

    // Full snapshot content (fetched on demand when user opens detail)
    private val _fullContent = MutableStateFlow<String?>(null)
    val fullContent: StateFlow<String?> = _fullContent.asStateFlow()

    private val _isLoadingContent = MutableStateFlow(false)
    val isLoadingContent: StateFlow<Boolean> = _isLoadingContent.asStateFlow()

    // The peek sheet's body, tagged with its snapshot id (see [SnapshotPeek]).
    private val _peek = MutableStateFlow<SnapshotPeek?>(null)
    val peek: StateFlow<SnapshotPeek?> = _peek.asStateFlow()

    // The one in-flight snapshot fetch; a newer request cancels it so a slow reply for
    // an earlier id can never land after the newer one.
    private var peekJob: Job? = null

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
    }

    fun search(query: String, operator: String? = null) {
        val api = api ?: return
        isUserSearching = query.isNotBlank()
        _isSearching.value = true
        viewModelScope.launch {
            try {
                val params = buildString {
                    append("?q=")
                    append(java.net.URLEncoder.encode(query, "UTF-8"))
                    append("&limit=10")
                    if (!operator.isNullOrBlank()) append("&operator=$operator")
                }
                val response = api.get("/fossil/hybrid$params")
                val parsed = json.decodeFromString(SearchResponse.serializer(), response)
                _results.value = parsed.results
            } catch (e: Exception) {
                Log.e(TAG, "search failed: ${e.message}")
                _results.value = emptyList()
            } finally {
                _isSearching.value = false
            }
        }
    }

    /** Load recent snapshots. Set silent=true for background refresh (no loading spinner). */
    /** Load recent snapshots chronologically (newest first) from dedicated endpoint. */
    fun loadRecent(operator: String, silent: Boolean = false) {
        val api = api ?: return
        currentOperator = operator
        if (!silent) _isSearching.value = true
        viewModelScope.launch {
            try {
                val params = buildString {
                    append("?limit=30")
                    if (operator.isNotBlank()) append("&operator=$operator")
                }
                val response = api.get("/api/snapshots/recent$params")
                val parsed = json.decodeFromString(SearchResponse.serializer(), response)
                _results.value = parsed.results
            } catch (e: Exception) {
                if (!silent) Log.e(TAG, "loadRecent failed: ${e.message}")
                if (!silent) _results.value = emptyList()
            } finally {
                if (!silent) _isSearching.value = false
            }
        }
    }

    /**
     * Start auto-polling for live updates. Call when timeline screen becomes visible. It
     * runs again on every resume, so it leaves search mode alone: search results stay
     * on screen across a lock or an app switch (only [clearSearch] ends search mode).
     */
    fun startPolling(operator: String) {
        currentOperator = operator
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (isActive) {
                delay(POLL_INTERVAL_MS)
                // Don't overwrite search results with poll results
                if (!isUserSearching) {
                    loadRecent(currentOperator, silent = true)
                }
            }
        }
    }

    /** Stop auto-polling. Call when timeline screen is no longer visible. */
    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    /** Clear search and resume live feed. */
    fun clearSearch(operator: String) {
        isUserSearching = false
        currentOperator = operator
        loadRecent(operator)
    }

    /**
     * Select a snapshot and fetch its full content from /fossil/snapshot/{id} (the ledger read, M7)
     */
    fun selectSnapshot(snapshot: SnapshotResult?) {
        _selectedSnapshot.value = snapshot
        _fullContent.value = null

        if (snapshot != null) {
            fetchFullContent(snapshot.snapId)
        }
    }

    /**
     * Navigate to a specific snapshot by ID (for clickable SNAP-XXXXX links)
     */
    fun navigateToSnapshot(snapId: String) {
        val api = api ?: return
        peekJob?.cancel()
        _peek.value = SnapshotPeek(snapId)
        _fullContent.value = null
        _isLoadingContent.value = true
        peekJob = viewModelScope.launch {
            try {
                val response = ledgerRead(api, snapId)
                val obj = json.parseToJsonElement(response).jsonObject
                val content = obj["content"]?.jsonPrimitive?.content ?: ""
                val meta = obj["metadata"]?.jsonObject
                val operator = meta?.get("operator")?.jsonPrimitive?.content ?: ""
                val timestamp = meta?.get("timestamp")?.jsonPrimitive?.content ?: ""
                val type = meta?.get("type")?.jsonPrimitive?.content ?: ""

                val snap = SnapshotResult(
                    snapId = snapId,
                    operator = operator,
                    timestamp = timestamp,
                    snippet = content,
                    type = type
                )
                _selectedSnapshot.value = snap
                _fullContent.value = content
                _peek.value = SnapshotPeek(snapId, content, loading = false)
            } catch (e: kotlinx.coroutines.CancellationException) {
                // Superseded by a newer peek: leave its state alone.
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "navigateToSnapshot failed for $snapId: ${e.message}")
                _peek.value = SnapshotPeek(snapId, loading = false, error = "Could not load $snapId. Check the connection and try again.")
            } finally {
                if (_peek.value?.snapId == snapId) _isLoadingContent.value = false
            }
        }
    }

    private fun fetchFullContent(snapId: String) {
        val api = api ?: return
        peekJob?.cancel()
        _isLoadingContent.value = true
        peekJob = viewModelScope.launch {
            try {
                val response = ledgerRead(api, snapId)
                val obj = json.parseToJsonElement(response).jsonObject
                if (_selectedSnapshot.value?.snapId == snapId) {
                    _fullContent.value = obj["content"]?.jsonPrimitive?.content ?: ""
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "fetchFullContent failed for $snapId: ${e.message}")
                // Fallback to the snippet of THIS snapshot, never another one's.
                val selected = _selectedSnapshot.value
                if (selected?.snapId == snapId) _fullContent.value = selected.snippet
            } finally {
                if (_selectedSnapshot.value?.snapId == snapId) _isLoadingContent.value = false
            }
        }
    }
}
