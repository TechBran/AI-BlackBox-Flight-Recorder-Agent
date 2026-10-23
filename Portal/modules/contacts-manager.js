/**
 * contacts-manager.js
 * Contact Book — browse, search, add, edit, delete contacts
 * Follows telephony-manager.js and sms-inbox.js patterns
 */
import { $, toast, toastSuccess, toastError, escapeHtml, escapeAttr } from './core-utils.js';
import { getOperator, OPERATOR_REQUIRED_MESSAGE } from './state-management.js';

// =============================================================================
// State
// =============================================================================

let allContacts = [];
let _searchTimer = null;
let _editingContact = null;  // null = new, object = editing existing
let _editForm = null;        // the container the open edit form was built in

// =============================================================================
// Helpers
// =============================================================================

function truncate(str, len = 80) {
    if (!str) return '';
    return str.length > len ? str.slice(0, len) + '\u2026' : str;
}

/**
 * A field of the open edit form, looked up INSIDE the form's container. Never
 * page-wide: getElementById returns the FIRST element with an id, and chat
 * history precedes this modal in index.html, so a reply carrying an element
 * with the same id would otherwise be filled and read instead of the field
 * the operator sees (is_operator_self decides how SMS treats a number).
 * @param {string} id - The field's id
 * @returns {HTMLElement|null}
 */
function formField(id) {
    return _editForm ? _editForm.querySelector(`[id="${id}"]`) : null;
}

// =============================================================================
// API Functions
// =============================================================================

// Every request names the operator whose book it is. With none resolved there is no
// book: reads show the guidance, writes refuse (never a contact book for "").
function renderOperatorRequired() {
    allContacts = [];
    const container = $('contactsList');
    if (!container) return;
    const empty = document.createElement('div');
    empty.className = 'contacts-empty';
    empty.textContent = `${OPERATOR_REQUIRED_MESSAGE}.`;
    container.replaceChildren(empty);
}

async function fetchContacts() {
    try {
        const op = getOperator();
        if (!op) {
            renderOperatorRequired();
            toastError(OPERATOR_REQUIRED_MESSAGE);
            return;
        }
        const res = await fetch(`/contacts?operator=${encodeURIComponent(op)}`);
        if (!res.ok) throw new Error('Failed to fetch contacts');
        const data = await res.json();
        allContacts = data.contacts || [];
        renderContactList(allContacts);
    } catch (e) {
        console.error('[Contacts] Failed to fetch contacts:', e);
        toastError('Failed to load contacts');
    }
}

async function searchContacts(query) {
    try {
        const op = getOperator();
        if (!op) { renderOperatorRequired(); return; }
        const res = await fetch(
            `/contacts/search?operator=${encodeURIComponent(op)}&query=${encodeURIComponent(query)}`
        );
        if (!res.ok) throw new Error('Search failed');
        const data = await res.json();
        const results = data.results || [];
        renderContactList(results);
    } catch (e) {
        console.error('[Contacts] Search failed:', e);
        toastError('Contact search failed');
    }
}

async function saveContact(data) {
    try {
        const op = getOperator();
        if (!op) {
            toastError(OPERATOR_REQUIRED_MESSAGE);
            return;
        }
        const body = { operator: op, ...data };
        const res = await fetch('/contacts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save contact');
        }
        const result = await res.json();
        if (result.success) {
            toastSuccess(_editingContact ? 'Contact updated' : 'Contact added');
        }
        // Surface the write-time identity guard warning (non-blocking — save succeeded)
        if (result.warning) {
            toast(result.warning);
        }
        hideEditForm();
        await fetchContacts();
    } catch (e) {
        console.error('[Contacts] Save failed:', e);
        toastError(e.message || 'Failed to save contact');
    }
}

async function deleteContact(id) {
    if (!getOperator()) {
        toastError(OPERATOR_REQUIRED_MESSAGE);
        return;
    }
    if (!confirm('Delete contact?')) return;
    try {
        const op = getOperator();
        const res = await fetch(
            `/contacts/${encodeURIComponent(id)}?operator=${encodeURIComponent(op)}`,
            { method: 'DELETE' }
        );
        if (!res.ok) throw new Error('Failed to delete contact');
        const result = await res.json();
        if (result.success) {
            toastSuccess('Contact deleted');
        }
        await fetchContacts();
    } catch (e) {
        console.error('[Contacts] Delete failed:', e);
        toastError('Failed to delete contact');
    }
}

// =============================================================================
// Rendering
// =============================================================================

function renderContactList(contacts) {
    const container = $('contactsList');
    if (!container) return;

    if (!contacts || contacts.length === 0) {
        container.innerHTML = `
            <div class="contacts-empty">
                No contacts yet. Click <strong>+ Add</strong> to create one.
            </div>`;
        return;
    }

    container.innerHTML = contacts.map(c => {
        const name = escapeHtml(c.name || 'Unnamed');
        const phone = c.phone ? escapeHtml(c.phone) : '';
        const email = c.email ? escapeHtml(c.email) : '';
        const relationship = c.relationship ? escapeHtml(c.relationship) : '';
        const notes = c.notes ? escapeHtml(truncate(c.notes)) : '';
        const tags = (c.tags || []).map(t => typeof t === 'string' ? t : '').filter(Boolean);

        let tagsHtml = '';
        if (tags.length > 0) {
            tagsHtml = `<div class="contact-card-tags">
                ${tags.map(t => `<span class="contact-tag-badge">${escapeHtml(t)}</span>`).join('')}
            </div>`;
        }

        let relationshipHtml = '';
        if (relationship) {
            relationshipHtml = `<span class="contact-card-relationship">${relationship}</span>`;
        }

        const idAttr = escapeAttr(String(c.id || ''));
        return `
        <div class="contact-card" data-id="${idAttr}">
            <div class="contact-card-header">
                <div class="contact-card-info">
                    <span class="contact-card-name">${name}</span>
                    ${relationshipHtml}
                </div>
                <div class="contact-card-actions">
                    <button class="btn btn-sm contact-edit-btn" data-id="${idAttr}" title="Edit">Edit</button>
                    <button class="btn btn-sm btn-danger contact-delete-btn" data-id="${idAttr}" title="Delete">Delete</button>
                </div>
            </div>
            ${phone ? `<div class="contact-card-phone">${phone}</div>` : ''}
            ${email ? `<div class="contact-card-email">${email}</div>` : ''}
            ${tagsHtml}
            ${notes ? `<div class="contact-card-notes">${notes}</div>` : ''}
        </div>`;
    }).join('');

    // Wire edit buttons
    container.querySelectorAll('.contact-edit-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const id = btn.dataset.id;
            const contact = allContacts.find(c => String(c.id) === id);
            if (contact) showEditForm(contact);
        });
    });

    // Wire delete buttons
    container.querySelectorAll('.contact-delete-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const id = btn.dataset.id;
            deleteContact(id);
        });
    });
}

// =============================================================================
// Edit Form
// =============================================================================

function showEditForm(contact) {
    _editingContact = contact || null;
    const container = $('contactsEditForm');
    if (!container) return;
    _editForm = container;

    // Raw values: they are assigned through the DOM (.value) after the static
    // form markup is built, never interpolated into attribute strings. This also
    // fixes notes being escaped twice and saved corrupted (P06-comms-web-07).
    const name = _editingContact ? (_editingContact.name || '') : '';
    const phone = _editingContact ? (_editingContact.phone || '') : '';
    const email = _editingContact ? (_editingContact.email || '') : '';
    const relationship = _editingContact ? (_editingContact.relationship || '') : '';
    const notes = _editingContact ? (_editingContact.notes || '') : '';
    const tags = _editingContact && Array.isArray(_editingContact.tags)
        ? _editingContact.tags.join(', ')
        : '';
    // Per-contact SMS flags (defaults: inbound allowed, not operator self)
    const inboundAllowed = _editingContact
        ? _editingContact.inbound_allowed !== false
        : true;
    const isOperatorSelf = _editingContact
        ? _editingContact.is_operator_self === true
        : false;
    const opName = escapeHtml(getOperator());

    container.innerHTML = `
        <div class="contacts-edit-form">
            <div class="form-row">
                <label class="form-label" for="contactName">Name *</label>
                <input type="text" id="contactName" placeholder="Full name" required />
            </div>
            <div class="form-row">
                <label class="form-label" for="contactPhone">Phone</label>
                <input type="tel" id="contactPhone" placeholder="+1 (555) 123-4567" />
            </div>
            <div class="form-row">
                <label class="form-label" for="contactEmail">Email</label>
                <input type="email" id="contactEmail" placeholder="name@example.com" />
            </div>
            <div class="form-row">
                <label class="form-label" for="contactRelationship">Relationship</label>
                <input type="text" id="contactRelationship" placeholder="Friend, Family, Work..." />
            </div>
            <div class="form-row">
                <label class="form-label" for="contactNotes">Notes</label>
                <textarea id="contactNotes" rows="3" placeholder="Optional notes..."></textarea>
            </div>
            <div class="form-row">
                <label class="form-label" for="contactTags">Tags (comma-separated)</label>
                <input type="text" id="contactTags" placeholder="vip, developer, nyc..." />
            </div>
            <div class="form-row">
                <label class="form-label" for="contactInboundAllowed" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
                    <input type="checkbox" id="contactInboundAllowed" ${inboundAllowed ? 'checked' : ''} />
                    Allow inbound texts
                </label>
            </div>
            <div class="form-row">
                <label class="form-label" for="contactIsOperatorSelf" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
                    <input type="checkbox" id="contactIsOperatorSelf" ${isOperatorSelf ? 'checked' : ''} />
                    This number IS operator ${opName}
                </label>
            </div>
            <div class="form-actions">
                <button id="btnContactCancel" class="btn">Cancel</button>
                <button id="btnContactSave" class="btn btn-confirm">${_editingContact ? 'Update' : 'Save'}</button>
            </div>
        </div>`;

    // Populate the fields through the DOM (no attribute-string interpolation)
    const fieldValues = {
        contactName: name,
        contactPhone: phone,
        contactEmail: email,
        contactRelationship: relationship,
        contactNotes: notes,
        contactTags: tags,
    };
    Object.entries(fieldValues).forEach(([id, value]) => {
        const field = formField(id);
        if (field) field.value = value;
    });

    container.style.display = 'block';

    // Wire cancel
    const btnCancel = formField('btnContactCancel');
    if (btnCancel) btnCancel.addEventListener('click', hideEditForm);

    // Wire save
    const btnSave = formField('btnContactSave');
    if (btnSave) btnSave.addEventListener('click', handleSave);

    // Focus name field
    const nameInput = formField('contactName');
    if (nameInput) nameInput.focus();
}

function hideEditForm() {
    _editingContact = null;
    _editForm = null;
    const container = $('contactsEditForm');
    if (container) {
        container.style.display = 'none';
        container.innerHTML = '';
    }
}

function handleSave() {
    const name = (formField('contactName')?.value || '').trim();
    const phone = (formField('contactPhone')?.value || '').trim();
    const email = (formField('contactEmail')?.value || '').trim();
    const relationship = (formField('contactRelationship')?.value || '').trim();
    const notes = (formField('contactNotes')?.value || '').trim();
    const tagsRaw = (formField('contactTags')?.value || '').trim();
    const inboundAllowed = !!formField('contactInboundAllowed')?.checked;
    const isOperatorSelf = !!formField('contactIsOperatorSelf')?.checked;

    if (!name) {
        toastError('Name is required');
        return;
    }

    const tags = tagsRaw
        ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean)
        : [];

    const data = {
        name, phone, email, relationship, notes, tags,
        inbound_allowed: inboundAllowed,
        is_operator_self: isOperatorSelf
    };

    // If editing, include the existing contact id
    if (_editingContact && _editingContact.id) {
        data.id = _editingContact.id;
    }

    saveContact(data);
}

// =============================================================================
// Init
// =============================================================================

export function initContactsManager() {
    // Open modal
    const btnOpen = $('btnContactsManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', () => {
            const modal = $('contactsManagerModal');
            if (modal) {
                modal.classList.remove('hide');
                fetchContacts();
            }
        });
    }

    // Close modal
    const btnClose = $('btnCloseContacts');
    if (btnClose) {
        btnClose.addEventListener('click', () => {
            const modal = $('contactsManagerModal');
            if (modal) modal.classList.add('hide');
            hideEditForm();
        });
    }

    // Close on backdrop click
    const modal = $('contactsManagerModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.classList.add('hide');
                hideEditForm();
            }
        });
    }

    // Add button
    const btnAdd = $('btnAddContact');
    if (btnAdd) {
        btnAdd.addEventListener('click', () => showEditForm(null));
    }

    // Search input with debounce
    const searchInput = $('contactsSearch');
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            clearTimeout(_searchTimer);
            const query = searchInput.value.trim();
            _searchTimer = setTimeout(() => {
                if (query.length > 1) {
                    searchContacts(query);
                } else {
                    fetchContacts();
                }
            }, 300);
        });
    }
}
