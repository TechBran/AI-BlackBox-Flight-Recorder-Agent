package com.aiblackbox.portal.ui.sms

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.ModelCatalog
import com.aiblackbox.portal.data.repository.ModelCatalogRepository
import com.aiblackbox.portal.data.repository.ModelCatalogs
import kotlinx.coroutines.CancellationException
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.ModelSelection
import com.aiblackbox.portal.data.store.OperatorGeneration
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import com.aiblackbox.portal.util.Constants

data class SmsThread(
    val phoneNumber: String,
    val contactName: String,
    val lastMessage: String,
    val lastTimestamp: String,
    val unreadCount: Int,
    val direction: String,
    val lineNumber: String
)

data class SmsLine(
    val number: String,
    val gatewayId: String,
    val label: String
)

data class SmsMsg(
    val id: Int,
    val direction: String,
    val body: String,
    val timestamp: String,
    val contactName: String,
    val status: String
)

class SmsViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    // Every request reads the operator at that moment (SmsInboxApi), never a copy kept
    // from when the screen first opened (A14-ui-misc-07).
    private var sms: SmsInboxApi? = null
    private var api: BlackBoxApi? = null
    private var boundOrigin = ""
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    // Each load is tagged with the operator generation it started in: a reply that lands
    // after a switch (the 10 s poll, a load already in flight) was the previous
    // operator's, so it is dropped instead of replacing the new operator's inbox.
    private val generation = OperatorGeneration()

    private val _threads = MutableStateFlow<List<SmsThread>>(emptyList())
    val threads: StateFlow<List<SmsThread>> = _threads.asStateFlow()

    private val _messages = MutableStateFlow<List<SmsMsg>>(emptyList())
    val messages: StateFlow<List<SmsMsg>> = _messages.asStateFlow()

    private val _selectedPhone = MutableStateFlow<String?>(null)
    val selectedPhone: StateFlow<String?> = _selectedPhone.asStateFlow()

    private val _selectedContactName = MutableStateFlow("")
    val selectedContactName: StateFlow<String> = _selectedContactName.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _isSending = MutableStateFlow(false)
    val isSending: StateFlow<Boolean> = _isSending.asStateFlow()

    private val _unreadCount = MutableStateFlow(0)
    val unreadCount: StateFlow<Int> = _unreadCount.asStateFlow()

    // One-shot messages for the screen's snackbar ("Message sent", "Failed to send: …").
    // A Channel, not a nullable StateFlow: a second identical message still shows, and
    // none is lost while the screen is between views (A14-ui-misc-04).
    private val _actionMessages = Channel<String>(Channel.BUFFERED)
    val actionMessages: Flow<String> = _actionMessages.receiveAsFlow()

    // The compose box's text per thread; only a successful send clears it.
    private val drafts = SmsDrafts()
    val draftTexts: StateFlow<Map<String, String>> = drafts.drafts

    private val _connected = MutableStateFlow(false)
    val connected: StateFlow<Boolean> = _connected.asStateFlow()

    private val _smsProvider = MutableStateFlow(SmsPreference.DEFAULT.provider)
    val smsProvider: StateFlow<String> = _smsProvider.asStateFlow()

    // "" = Auto: the box's own default model for the provider (never a pinned id).
    private val _smsModel = MutableStateFlow(SmsPreference.DEFAULT.model)
    val smsModel: StateFlow<String> = _smsModel.asStateFlow()

    // The reply-model dropdown's rows for [smsProvider], from the catalog the chat
    // composer, Settings and cron share (ModelCatalogs, rank 76); see [loadSmsModels].
    private val _smsModelOptions = MutableStateFlow(Constants.AUTO_ONLY)
    val smsModelOptions: StateFlow<List<Pair<String, String>>> = _smsModelOptions.asStateFlow()

    // The operator generation whose saved preference [_smsModel] holds; null until the
    // box answered, so the DEFAULT placeholder is never "reconciled" onto the box.
    private var preferenceTag: Int? = null

    // Our sendable lines (enabled gateways' enabled ports with a phone number)
    private val _lines = MutableStateFlow<List<SmsLine>>(emptyList())
    val lines: StateFlow<List<SmsLine>> = _lines.asStateFlow()

    // Selected "from" line for outbound SMS; null = use backend default gateway
    private val _selectedFromNumber = MutableStateFlow<String?>(null)
    val selectedFromNumber: StateFlow<String?> = _selectedFromNumber.asStateFlow()

    private var pollJob: Job? = null

    init {
        // An operator switch while the inbox is open: the open thread, the list and the
        // badge were the previous operator's, so drop them and load the new operator's.
        viewModelScope.launch {
            store.operator.distinctUntilChanged().drop(1).collect {
                generation.advance()
                clearSelection()
                drafts.clear()
                _threads.value = emptyList()
                _unreadCount.value = 0
                loadAll()
            }
        }
    }

    /** Bind to [origin]; a different origin (a re-pair) rebinds and reloads. */
    fun initialize(origin: String) {
        if (origin.isBlank() || origin == boundOrigin) return
        boundOrigin = origin
        val client = BlackBoxApi(origin)
        api = client
        sms = SmsInboxApi(client) { store.operator.first() }
        clearSelection()
        loadAll()
    }

    private fun loadAll() {
        loadThreads()
        loadStatus()
        loadUnreadCount()
        loadSmsPreferences()
        loadLines()
    }

    fun loadThreads() {
        val sms = sms ?: return
        _isLoading.value = true
        val tag = generation.tag()
        viewModelScope.launch {
            try {
                val response = sms.threads()
                if (!generation.isCurrent(tag)) return@launch
                if (response == null) { _threads.value = emptyList(); return@launch }
                // One row per thread key: the list is keyed by it (A14-ui-misc-10).
                _threads.value = parseSmsThreads(response)
            } catch (_: Exception) {
                if (generation.isCurrent(tag)) _threads.value = emptyList()
            } finally {
                if (generation.isCurrent(tag)) _isLoading.value = false
            }
        }
    }

    fun loadLines() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val response = api.get("/asterisk/gateways")
                val root = json.parseToJsonElement(response).jsonObject
                val gatewaysArray = root["gateways"]?.jsonArray ?: return@launch
                val collected = mutableListOf<SmsLine>()
                gatewaysArray.forEach { gwEl ->
                    val gw = gwEl.jsonObject
                    val gwEnabled = gw["enabled"]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: false
                    if (!gwEnabled) return@forEach
                    val gwId = gw["id"]?.jsonPrimitive?.content ?: ""
                    val gwName = gw["name"]?.jsonPrimitive?.content ?: gwId
                    val ports = gw["ports"]?.jsonArray ?: return@forEach
                    ports.forEach { portEl ->
                        val port = portEl.jsonObject
                        val portEnabled = port["enabled"]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: false
                        val phoneNumber = port["phone_number"]?.jsonPrimitive?.content ?: ""
                        if (portEnabled && phoneNumber.isNotBlank()) {
                            collected.add(
                                SmsLine(
                                    number = phoneNumber,
                                    gatewayId = gwId,
                                    label = "$gwName · $phoneNumber"
                                )
                            )
                        }
                    }
                }
                _lines.value = collected
            } catch (_: Exception) {
                _lines.value = emptyList()
            }
        }
    }

    fun setFromNumber(n: String?) {
        _selectedFromNumber.value = n
    }

    fun loadMessages(phone: String) {
        val sms = sms ?: return
        val api = api ?: return
        _selectedPhone.value = phone
        _selectedContactName.value = _threads.value.find { it.phoneNumber == phone }?.contactName ?: ""
        val tag = generation.tag()
        viewModelScope.launch {
            try {
                val response = sms.messages(phone)
                if (!generation.isCurrent(tag)) return@launch
                if (response == null) { _messages.value = emptyList(); return@launch }
                val root = json.parseToJsonElement(response).jsonObject
                val msgsArray = root["messages"]?.jsonArray ?: return@launch
                _messages.value = msgsArray.map { el ->
                    val obj = el.jsonObject
                    SmsMsg(
                        id = obj["id"]?.jsonPrimitive?.intOrNull ?: 0,
                        direction = obj["direction"]?.jsonPrimitive?.content ?: "",
                        body = obj["body"]?.jsonPrimitive?.content ?: "",
                        timestamp = obj["timestamp"]?.jsonPrimitive?.content ?: "",
                        contactName = obj["contact_name"]?.jsonPrimitive?.content ?: "",
                        status = obj["status"]?.jsonPrimitive?.content ?: ""
                    )
                }
                markThreadRead(api, phone)
            } catch (_: Exception) {
                if (generation.isCurrent(tag)) _messages.value = emptyList()
            }
        }
    }

    // The thread is on screen: mark it read. GET /sms/messages is read-only
    // (a cross-site GET must not change state), so this is explicit.
    // Best-effort: a failure only leaves the unread badge up.
    private suspend fun markThreadRead(api: BlackBoxApi, phone: String) {
        try {
            api.post("/sms/mark-read", buildJsonObject {
                // Resolved per request (U16): never a captured or seeded name.
                put("operator", store.operator.first())
                put("phone", phone)
            }.toString())
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
        }
    }

    private fun say(message: String?) {
        if (!message.isNullOrBlank()) _actionMessages.trySend(message)
    }

    /** The open thread's compose text changed. */
    fun setDraft(phone: String, text: String) = drafts.set(phone, text)

    /**
     * Send the open thread's draft. The draft is cleared only once the box accepted the
     * SMS; a failure (no operator, the box unreachable, the gateway refusing it) leaves
     * it in the box to retry and says why (A14-ui-misc-04).
     */
    fun sendSms() {
        val sms = sms ?: return
        val phone = _selectedPhone.value ?: return
        if (_isSending.value || drafts.of(phone).isBlank()) return
        _isSending.value = true
        viewModelScope.launch {
            try {
                drafts.send(phone) { text ->
                    sms.send(to = phone, message = text, fromNumber = _selectedFromNumber.value)
                }
                say("Message sent")
                loadMessages(phone)
            } catch (e: CancellationException) {
                throw e
            } catch (e: OperatorUnresolvedException) {
                say(e.message)
            } catch (e: Exception) {
                say("Failed to send: ${e.message}")
            } finally {
                _isSending.value = false
            }
        }
    }

    fun sendNewSms(to: String, message: String) {
        val sms = sms ?: return
        if (_isSending.value) return
        _isSending.value = true
        viewModelScope.launch {
            try {
                sms.send(to = to, message = message, fromNumber = _selectedFromNumber.value)
                say("Message sent")
                loadThreads()
            } catch (e: CancellationException) {
                throw e
            } catch (e: OperatorUnresolvedException) {
                say(e.message)
            } catch (e: Exception) {
                say("Failed to send: ${e.message}")
            } finally {
                _isSending.value = false
            }
        }
    }

    fun loadStatus() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val response = api.get("/sms/status")
                val root = json.parseToJsonElement(response).jsonObject
                _connected.value = root["connected"]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: false
            } catch (_: Exception) {
                _connected.value = false
            }
        }
    }

    fun loadUnreadCount() {
        val sms = sms ?: return
        val tag = generation.tag()
        viewModelScope.launch {
            try {
                val response = sms.unread()
                if (!generation.isCurrent(tag)) return@launch
                if (response == null) { _unreadCount.value = 0; return@launch }
                val root = json.parseToJsonElement(response).jsonObject
                // GET /sms/unread answers {"unread": N} (A14-ui-misc-19).
                _unreadCount.value = root["unread"]?.jsonPrimitive?.intOrNull ?: 0
            } catch (_: Exception) {
                if (generation.isCurrent(tag)) _unreadCount.value = 0
            }
        }
    }

    fun loadSmsPreferences() {
        val sms = sms ?: return
        val tag = generation.tag()
        viewModelScope.launch {
            try {
                val pref = SmsPreference.parse(sms.preferences())
                if (!generation.isCurrent(tag)) return@launch
                _smsProvider.value = pref.provider
                _smsModel.value = pref.model
                preferenceTag = tag
                loadSmsModels(pref.provider)
            } catch (_: Exception) {}
        }
    }

    /**
     * Hydrate [smsModelOptions] for [provider]: the shared catalog's known copy at once,
     * then the box's. The picker used to read the hand-kept Constants.MODEL_CONFIG, which
     * went stale. A fresh live catalog that no longer lists the saved reply model replaces
     * it on the box ([reconcileSavedModel]): an id that 404s would fail every SMS reply.
     */
    fun loadSmsModels(provider: String) {
        val api = api ?: return
        val apiProvider = ModelCatalogRepository.apiProviderFor(provider)
        if (apiProvider == null) {
            _smsModelOptions.value = Constants.MODEL_CONFIG[provider].orEmpty()
            return
        }
        val catalogs = ModelCatalogs.forApi(api)
        _smsModelOptions.value = catalogs.cached(apiProvider)?.options()?.ifEmpty { null } ?: Constants.AUTO_ONLY
        viewModelScope.launch {
            val catalog = catalogs.refresh(apiProvider) ?: return@launch
            if (_smsProvider.value != provider) return@launch
            catalog.options().takeIf { it.isNotEmpty() }?.let { _smsModelOptions.value = it }
            reconcileSavedModel(provider, catalog)
        }
    }

    /**
     * Save the same-family successor (or Auto) for a saved reply model [catalog] no longer
     * lists (ModelSelection.replacementFor) — only for the preference the box returned for
     * the current operator, and only against an authoritative live catalog.
     */
    private fun reconcileSavedModel(provider: String, catalog: ModelCatalog) {
        val tag = preferenceTag ?: return
        if (!generation.isCurrent(tag) || !catalog.isAuthoritative || _smsProvider.value != provider) return
        val replacement = ModelSelection.replacementFor(_smsModel.value, catalog.ids) ?: return
        saveSmsPreferences(provider, replacement)
    }

    /** [model] "" = Auto (the provider switch in the screen always lands there). */
    fun saveSmsPreferences(provider: String, model: String) {
        val sms = sms ?: return
        _smsProvider.value = provider
        _smsModel.value = model
        viewModelScope.launch {
            try {
                sms.savePreferences(provider, model)
            } catch (e: OperatorUnresolvedException) {
                say(e.message)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                say("SMS model not saved: ${e.message}")
            }
        }
    }

    fun clearSelection() {
        _selectedPhone.value = null
        _messages.value = emptyList()
        _selectedContactName.value = ""
    }

    fun startPolling() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (isActive) {
                delay(10000)
                try {
                    loadThreads()
                    val phone = _selectedPhone.value
                    if (phone != null) {
                        loadMessages(phone)
                    }
                    loadUnreadCount()
                } catch (_: Exception) {
                    // Silent poll failure
                }
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    override fun onCleared() {
        super.onCleared()
        pollJob?.cancel()
    }
}
