package com.aiblackbox.portal.ui.contacts

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import com.aiblackbox.portal.ui.theme.*

@Composable
fun ContactsScreen(
    origin: String,
    operator: String = "",
    modifier: Modifier = Modifier,
    viewModel: ContactsViewModel = viewModel()
) {
    val contacts by viewModel.filteredContacts.collectAsState()
    val searchQuery by viewModel.searchQuery.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()
    val showEditDialog by viewModel.showEditDialog.collectAsState()
    val editingContact by viewModel.editingContact.collectAsState()
    val isSaving by viewModel.isSaving.collectAsState()
    val showDeleteConfirm by viewModel.showDeleteConfirm.collectAsState()

    LaunchedEffect(origin) { viewModel.initialize(origin, operator) }

    val view = LocalView.current
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(actionMessage) {
        actionMessage?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearActionMessage()
        }
    }

    Box(Modifier.fillMaxSize()) {
        Column(
            modifier = modifier
                .fillMaxSize()
                .padding(start = 16.dp, end = 16.dp, bottom = 160.dp, top = 100.dp)
        ) {
            // Header
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "Contacts",
                    style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                    color = BbxWhite
                )
                TextButton(onClick = {
                    view.performPressFeedback()
                    viewModel.loadContacts()
                }) { Text("Refresh", color = BbxDim) }
            }

            Spacer(Modifier.height(8.dp))

            // Search bar
            OutlinedTextField(
                value = searchQuery,
                onValueChange = { viewModel.setSearchQuery(it) },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Search contacts...", color = Neutral500) },
                singleLine = true,
                shape = RoundedCornerShape(RadiusMd),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = BbxAccent,
                    unfocusedBorderColor = Neutral300,
                    cursorColor = BbxAccent,
                    focusedTextColor = BbxWhite,
                    unfocusedTextColor = BbxWhite
                )
            )

            Spacer(Modifier.height(12.dp))

            // Loading state
            if (isLoading && contacts.isEmpty()) {
                Box(
                    Modifier.fillMaxWidth().padding(32.dp),
                    contentAlignment = Alignment.Center
                ) {
                    CircularProgressIndicator(color = BbxAccent, strokeWidth = 2.dp)
                }
            } else if (contacts.isEmpty()) {
                Box(
                    Modifier.fillMaxWidth().padding(48.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("\uD83D\uDCCB", fontSize = 48.sp)
                        Spacer(Modifier.height(12.dp))
                        Text(
                            if (searchQuery.isNotBlank()) "No contacts match your search"
                            else "No contacts yet",
                            color = Neutral500,
                            style = MaterialTheme.typography.bodyLarge
                        )
                    }
                }
            } else {
                LazyColumn(
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.weight(1f)
                ) {
                    items(contacts, key = { it.id }) { contact ->
                        ContactCard(
                            contact = contact,
                            onEdit = {
                                viewModel.showEdit(contact)
                            },
                            onDelete = {
                                viewModel.showDelete(contact.id)
                            }
                        )
                    }
                }
            }
        }

        // FAB — Add new contact
        FloatingActionButton(
            onClick = {
                view.performPressFeedback()
                viewModel.showEdit(null)
            },
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .navigationBarsPadding()
                .padding(bottom = 24.dp, end = 24.dp),
            containerColor = BbxAccent,
            contentColor = BbxWhite,
            shape = CircleShape
        ) {
            Text("+", fontSize = 24.sp, fontWeight = FontWeight.Bold)
        }

        // Snackbar
        SnackbarHost(
            hostState = snackbarHostState,
            modifier = Modifier.align(Alignment.BottomCenter)
        )
    }

    // Edit / Create dialog
    if (showEditDialog) {
        EditContactDialog(
            contact = editingContact,
            isSaving = isSaving,
            onSave = { name, phone, email, relationship, notes, tags ->
                viewModel.saveContact(name, phone, email, relationship, notes, tags)
            },
            onDismiss = { viewModel.hideEdit() }
        )
    }

    // Delete confirmation dialog
    if (showDeleteConfirm != null) {
        DeleteConfirmDialog(
            onConfirm = { viewModel.deleteContact(showDeleteConfirm!!) },
            onDismiss = { viewModel.hideDelete() }
        )
    }
}

@Composable
private fun ContactCard(
    contact: Contact,
    onEdit: () -> Unit,
    onDelete: () -> Unit
) {
    val feedback = rememberPressFeedback()
    GlassCard(modifier = Modifier.fillMaxWidth()) {
        Row(modifier = Modifier.padding(14.dp)) {
            // Left accent bar
            Box(
                Modifier
                    .width(3.dp)
                    .height(80.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(BbxAccent)
            )
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                // Name
                Text(
                    contact.name,
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = BbxWhite,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Spacer(Modifier.height(4.dp))

                // Phone + Email
                if (contact.phone.isNotBlank()) {
                    Text(
                        contact.phone,
                        style = MaterialTheme.typography.bodySmall,
                        color = Neutral500
                    )
                }
                if (contact.email.isNotBlank()) {
                    Text(
                        contact.email,
                        style = MaterialTheme.typography.bodySmall,
                        color = Neutral500
                    )
                }

                // Relationship badge
                if (contact.relationship.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Box(
                        Modifier
                            .clip(RoundedCornerShape(RadiusSm))
                            .background(BbxAccent.copy(alpha = 0.15f))
                            .padding(horizontal = 8.dp, vertical = 2.dp)
                    ) {
                        Text(
                            contact.relationship,
                            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Medium),
                            color = BbxAccent
                        )
                    }
                }

                // Tags
                if (contact.tags.isNotEmpty()) {
                    Spacer(Modifier.height(6.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        contact.tags.take(5).forEach { tag ->
                            Box(
                                Modifier
                                    .clip(RoundedCornerShape(RadiusXs))
                                    .background(Neutral250)
                                    .padding(horizontal = 6.dp, vertical = 2.dp)
                            ) {
                                Text(
                                    tag,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = Neutral700
                                )
                            }
                        }
                    }
                }

                // Notes (truncated)
                if (contact.notes.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        contact.notes,
                        style = MaterialTheme.typography.bodySmall.copy(fontStyle = FontStyle.Italic),
                        color = Neutral500,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }

                // Action buttons
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    TextButton(onClick = { feedback(); onEdit() }, contentPadding = PaddingValues(0.dp)) {
                        Text("Edit", color = BbxAccent, style = MaterialTheme.typography.labelMedium)
                    }
                    TextButton(onClick = { feedback(); onDelete() }, contentPadding = PaddingValues(0.dp)) {
                        Text("Delete", color = BbxRed, style = MaterialTheme.typography.labelMedium)
                    }
                }
            }
        }
    }
}

@Composable
private fun EditContactDialog(
    contact: Contact?,
    isSaving: Boolean,
    onSave: (name: String, phone: String, email: String, relationship: String, notes: String, tags: String) -> Unit,
    onDismiss: () -> Unit
) {
    val feedback = rememberPressFeedback()
    var name by remember { mutableStateOf(contact?.name ?: "") }
    var phone by remember { mutableStateOf(contact?.phone ?: "") }
    var email by remember { mutableStateOf(contact?.email ?: "") }
    var relationship by remember { mutableStateOf(contact?.relationship ?: "") }
    var notes by remember { mutableStateOf(contact?.notes ?: "") }
    var tags by remember { mutableStateOf(contact?.tags?.joinToString(", ") ?: "") }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Neutral100,
        titleContentColor = BbxWhite,
        title = {
            Text(
                if (contact != null) "Edit Contact" else "New Contact",
                fontWeight = FontWeight.Bold
            )
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text("Name *", color = Neutral500) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent,
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedLabelColor = BbxAccent,
                        unfocusedLabelColor = Neutral500
                    )
                )
                OutlinedTextField(
                    value = phone,
                    onValueChange = { phone = it },
                    label = { Text("Phone", color = Neutral500) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent,
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedLabelColor = BbxAccent,
                        unfocusedLabelColor = Neutral500
                    )
                )
                OutlinedTextField(
                    value = email,
                    onValueChange = { email = it },
                    label = { Text("Email", color = Neutral500) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent,
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedLabelColor = BbxAccent,
                        unfocusedLabelColor = Neutral500
                    )
                )
                OutlinedTextField(
                    value = relationship,
                    onValueChange = { relationship = it },
                    label = { Text("Relationship", color = Neutral500) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent,
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedLabelColor = BbxAccent,
                        unfocusedLabelColor = Neutral500
                    )
                )
                OutlinedTextField(
                    value = notes,
                    onValueChange = { notes = it },
                    label = { Text("Notes", color = Neutral500) },
                    maxLines = 3,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent,
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedLabelColor = BbxAccent,
                        unfocusedLabelColor = Neutral500
                    )
                )
                OutlinedTextField(
                    value = tags,
                    onValueChange = { tags = it },
                    label = { Text("Tags (comma-separated)", color = Neutral500) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent,
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedLabelColor = BbxAccent,
                        unfocusedLabelColor = Neutral500
                    )
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { feedback(); onSave(name.trim(), phone.trim(), email.trim(), relationship.trim(), notes.trim(), tags.trim()) },
                enabled = name.isNotBlank() && !isSaving
            ) {
                if (isSaving) {
                    CircularProgressIndicator(
                        color = BbxAccent,
                        strokeWidth = 2.dp,
                        modifier = Modifier.size(16.dp)
                    )
                } else {
                    Text("Save", color = BbxAccent, fontWeight = FontWeight.Bold)
                }
            }
        },
        dismissButton = {
            TextButton(onClick = { feedback(); onDismiss() }) {
                Text("Cancel", color = Neutral500)
            }
        }
    )
}

@Composable
private fun DeleteConfirmDialog(
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    val feedback = rememberPressFeedback()
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Neutral100,
        titleContentColor = BbxWhite,
        title = { Text("Delete Contact", fontWeight = FontWeight.Bold) },
        text = { Text("Delete this contact? This action cannot be undone.", color = Neutral500) },
        confirmButton = {
            TextButton(onClick = { feedback(); onConfirm() }) {
                Text("Delete", color = BbxRed, fontWeight = FontWeight.Bold)
            }
        },
        dismissButton = {
            TextButton(onClick = { feedback(); onDismiss() }) {
                Text("Cancel", color = Neutral500)
            }
        }
    )
}
