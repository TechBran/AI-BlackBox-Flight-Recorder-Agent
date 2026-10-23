package com.aiblackbox.portal.ui.contacts

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.OperatorGeneration
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class Contact(
    val id: String,
    val name: String,
    val phone: String,
    val email: String,
    val relationship: String,
    val notes: String,
    val tags: List<String>,
    val createdAt: String,
    val inboundAllowed: Boolean = true,
    val isOperatorSelf: Boolean = false
)

class ContactsViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    // Every request reads the operator at that moment (ContactsBookApi), never a copy
    // kept from when the screen first opened (A14-ui-misc-07).
    private var book: ContactsBookApi? = null
    private var boundOrigin = ""
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    // A book that lands after an operator switch was the previous operator's: dropped.
    private val generation = OperatorGeneration()

    // -- Raw state --
    private val _allContacts = MutableStateFlow<List<Contact>>(emptyList())

    private val _searchQuery = MutableStateFlow("")
    val searchQuery: StateFlow<String> = _searchQuery.asStateFlow()

    private val _filteredContacts = MutableStateFlow<List<Contact>>(emptyList())
    val filteredContacts: StateFlow<List<Contact>> = _filteredContacts.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _actionMessage = MutableStateFlow<String?>(null)
    val actionMessage: StateFlow<String?> = _actionMessage.asStateFlow()

    private val _showEditDialog = MutableStateFlow(false)
    val showEditDialog: StateFlow<Boolean> = _showEditDialog.asStateFlow()

    private val _editingContact = MutableStateFlow<Contact?>(null)
    val editingContact: StateFlow<Contact?> = _editingContact.asStateFlow()

    private val _isSaving = MutableStateFlow(false)
    val isSaving: StateFlow<Boolean> = _isSaving.asStateFlow()

    private val _showDeleteConfirm = MutableStateFlow<String?>(null)
    val showDeleteConfirm: StateFlow<String?> = _showDeleteConfirm.asStateFlow()

    init {
        viewModelScope.launch {
            combine(_allContacts, _searchQuery) { contacts, query ->
                if (query.isBlank()) {
                    contacts
                } else {
                    val q = query.lowercase()
                    contacts.filter {
                        it.name.lowercase().contains(q) ||
                                it.phone.lowercase().contains(q) ||
                                it.email.lowercase().contains(q) ||
                                it.relationship.lowercase().contains(q) ||
                                it.tags.any { tag -> tag.lowercase().contains(q) }
                    }
                }
            }.collect { _filteredContacts.value = it }
        }
        // An operator switch while the screen is open: the list and any open edit or
        // delete were the previous operator's book, so close them and load the new one.
        viewModelScope.launch {
            store.operator.distinctUntilChanged().drop(1).collect {
                generation.advance()
                hideEdit()
                hideDelete()
                _allContacts.value = emptyList()
                loadContacts()
            }
        }
    }

    /** Bind to [origin]; a different origin (a re-pair) rebinds and reloads. */
    fun initialize(origin: String) {
        if (origin.isBlank() || origin == boundOrigin) return
        boundOrigin = origin
        book = ContactsBookApi(BlackBoxApi(origin)) { store.operator.first() }
        loadContacts()
    }

    fun loadContacts() {
        val book = book ?: return
        _isLoading.value = true
        val tag = generation.tag()
        viewModelScope.launch {
            try {
                val response = book.list()
                if (!generation.isCurrent(tag)) return@launch
                if (response == null) { _allContacts.value = emptyList(); return@launch }
                val root = json.parseToJsonElement(response).jsonObject
                val arr = root["contacts"]?.jsonArray ?: return@launch
                _allContacts.value = arr.map { el ->
                    val obj = el.jsonObject
                    Contact(
                        id = obj["id"]?.jsonPrimitive?.content ?: "",
                        name = obj["name"]?.jsonPrimitive?.content ?: "",
                        phone = obj["phone"]?.jsonPrimitive?.content ?: "",
                        email = obj["email"]?.jsonPrimitive?.content ?: "",
                        relationship = obj["relationship"]?.jsonPrimitive?.content ?: "",
                        notes = obj["notes"]?.jsonPrimitive?.content ?: "",
                        tags = obj["tags"]?.jsonArray?.mapNotNull { it.jsonPrimitive.content } ?: emptyList(),
                        createdAt = obj["created_at"]?.jsonPrimitive?.content ?: "",
                        // SMS flags; legacy/absent => inbound allowed, not operator-self
                        inboundAllowed = obj["inbound_allowed"]?.jsonPrimitive?.booleanOrNull ?: true,
                        isOperatorSelf = obj["is_operator_self"]?.jsonPrimitive?.booleanOrNull ?: false
                    )
                }
            } catch (_: Exception) {
                if (generation.isCurrent(tag)) _allContacts.value = emptyList()
            } finally {
                if (generation.isCurrent(tag)) _isLoading.value = false
            }
        }
    }

    fun setSearchQuery(query: String) {
        _searchQuery.value = query
    }

    fun saveContact(
        name: String,
        phone: String,
        email: String,
        relationship: String,
        notes: String,
        tags: String,
        inboundAllowed: Boolean,
        isOperatorSelf: Boolean
    ) {
        val book = book ?: return
        if (_isSaving.value) return
        _isSaving.value = true

        viewModelScope.launch {
            try {
                // Always a POST — backend upserts by name (case-insensitive match)
                val response = book.save(
                    name = name, phone = phone, email = email, relationship = relationship,
                    notes = notes, tags = tags, inboundAllowed = inboundAllowed, isOperatorSelf = isOperatorSelf,
                )
                val existingId = _editingContact.value?.id

                // Surface the write-time identity guard warning if present (save still succeeded)
                val warning = try {
                    json.parseToJsonElement(response).jsonObject["warning"]?.jsonPrimitive?.content
                } catch (_: Exception) {
                    null
                }
                _actionMessage.value = warning
                    ?: if (existingId != null) "Contact updated" else "Contact created"

                _showEditDialog.value = false
                _editingContact.value = null
                loadContacts()
            } catch (e: OperatorUnresolvedException) {
                _actionMessage.value = e.message
            } catch (e: Exception) {
                _actionMessage.value = "Failed to save: ${e.message}"
            } finally {
                _isSaving.value = false
            }
        }
    }

    fun deleteContact(id: String) {
        val book = book ?: return
        _showDeleteConfirm.value = null
        viewModelScope.launch {
            try {
                book.delete(id)
                _actionMessage.value = "Contact deleted"
                loadContacts()
            } catch (e: OperatorUnresolvedException) {
                _actionMessage.value = e.message
            } catch (e: Exception) {
                _actionMessage.value = "Failed to delete: ${e.message}"
            }
        }
    }

    fun showEdit(contact: Contact?) {
        _editingContact.value = contact
        _showEditDialog.value = true
    }

    fun hideEdit() {
        _editingContact.value = null
        _showEditDialog.value = false
    }

    fun showDelete(id: String) {
        _showDeleteConfirm.value = id
    }

    fun hideDelete() {
        _showDeleteConfirm.value = null
    }

    fun clearActionMessage() {
        _actionMessage.value = null
    }
}
