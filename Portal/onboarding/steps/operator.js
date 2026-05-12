// Operator setup step — sixth screen of the onboarding wizard.
//
// LIVE-SYNCED (T2.7.2): on render, fetches /onboarding/current-config and
// rehydrates `state.existing` with the BlackBox's actual operator list.
// Existing operators render as read-only rows with [×] confirm-then-DELETE
// buttons. Editable rows below let the customer add NEW operators (POSTed
// to /operator/add on Save). Save button label adapts ("Continue →" when
// no pending changes, "Save & continue →" when adding).
//
// DEFAULT_OPERATOR is only POSTed when existing was empty AND ≥1 new name
// added — otherwise the existing default is preserved.
//
// Visual reference: extends design system. Two-row visual layout (existing
// readonly rows + pending editable rows) separated by section divider when
// both present.

const NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;

let busy = false;
let nextRowId = 1;  // monotonically increasing across re-renders

export async function render(container, { next, back, skip }) {
    // Fetch current operator list — try/catch so a network blip doesn't
    // hard-block setup. Empty existing list falls through to fresh-install UX.
    let existing = [];
    try {
        const r = await fetch("/onboarding/current-config");
        if (r.ok) {
            const data = await r.json();
            existing = (data.operators || []).map(name => ({ name }));
        }
    } catch (_e) {
        // Silent fallback — wizard still usable for fresh installs
    }

    const state = {
        existing,
        // Fresh install (existing empty) → start with one editable row to
        // preserve the original "type a name to begin" UX. Otherwise start
        // empty — customer clicks "+ Add another" if they want to add more.
        pending: existing.length === 0 ? [{ id: 0, name: "" }] : [],
    };

    container.innerHTML = renderShell();
    rerender(container, state);

    document.getElementById("ob-operator-add").addEventListener("click", () => addRow(container, state));
    document.getElementById("ob-operator-save").addEventListener("click", () => onSave(container, state, next));
    document.getElementById("ob-operator-back").addEventListener("click", back);

    // Focus the first editable input on mount (if any)
    const first = container.querySelector(".ob-operator-name");
    if (first) first.focus();
}

function renderShell() {
    return `
        <section class="ob-step ob-operator">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>06</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">OPERATOR</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Who uses this box
                </div>
                <h1 class="ob-step-title">
                    Name your <em>operators</em>.
                </h1>
                <p class="ob-step-lede">
                    Each operator gets their own conversation history,
                    preferences, and memory. Add one to start &mdash; you can
                    always add more later from the System Menu.
                </p>
                <div class="ob-operator-rows" id="ob-operator-rows"></div>
                <button type="button" class="ob-add-row" id="ob-operator-add">
                    <span aria-hidden="true">+</span> Add another operator
                </button>
                <div class="ob-operator-error" id="ob-operator-error" hidden></div>
                <div class="ob-cta-row">
                    <button type="button" class="ob-cta" id="ob-operator-save" disabled>
                        Save &amp; continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-operator-back">
                        <span aria-hidden="true">&larr;</span> Back to phone pairing
                    </button>
                </nav>
            </div>
        </section>
    `;
}

function rerender(container, state) {
    const rowsEl = container.querySelector("#ob-operator-rows");
    if (!rowsEl) return;

    let html = "";

    // Existing (read-only) rows
    for (const op of state.existing) {
        html += renderExistingRow(op);
    }

    // Section divider when both groups present
    if (state.existing.length > 0 && state.pending.length > 0) {
        html += `<div class="ob-operator-section-divider">+ Adding new</div>`;
    }

    // Pending (editable) rows
    state.pending.forEach((row, idx) => {
        html += renderPendingRow(row, idx, state.existing.length === 0);
    });

    rowsEl.innerHTML = html;
    wireRows(container, state);
    updateSaveButton(container, state);
}

function renderExistingRow(op) {
    const safeName = escapeHtml(op.name);
    return `
        <div class="ob-operator-row ob-operator-row-existing" data-name="${safeName}">
            <span class="ob-operator-name-existing">${safeName}</span>
            <button
                type="button"
                class="ob-row-remove ob-row-remove-existing"
                data-name="${safeName}"
                aria-label="Remove operator ${safeName}"
            >&times;</button>
        </div>
    `;
}

function renderPendingRow(row, idx, isFreshInstall) {
    // On fresh install, the very first pending row hides its [×] (matches
    // pre-T2.7.2 UX where the first row was anchored). Otherwise every
    // pending row gets its [×] since we may already have existing rows above.
    const showRemove = !isFreshInstall || idx > 0;
    return `
        <div class="ob-operator-row" data-row-id="${row.id}">
            <input
                type="text"
                class="ob-operator-name ob-provider-input"
                data-row-id="${row.id}"
                placeholder="e.g. Sarah"
                value="${escapeHtml(row.name)}"
                maxlength="32"
                pattern="[A-Za-z0-9_-]+"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
            />
            ${showRemove ? `
                <button
                    type="button"
                    class="ob-row-remove"
                    data-row-id="${row.id}"
                    aria-label="Remove operator row"
                >&times;</button>
            ` : `
                <span class="ob-row-remove-spacer" aria-hidden="true"></span>
            `}
        </div>
    `;
}

function wireRows(container, state) {
    const rowsEl = container.querySelector("#ob-operator-rows");

    // Editable inputs (pending rows)
    rowsEl.querySelectorAll(".ob-operator-name").forEach(input => {
        input.addEventListener("input", e => {
            const id = Number(e.target.dataset.rowId);
            const row = state.pending.find(r => r.id === id);
            if (row) {
                row.name = e.target.value.trim();
                clearError(container);
                updateSaveButton(container, state);
            }
        });
    });

    // Remove buttons — branch on whether it's an existing or pending row
    rowsEl.querySelectorAll(".ob-row-remove").forEach(btn => {
        btn.addEventListener("click", e => {
            const target = e.currentTarget;
            const existingName = target.dataset.name;
            if (existingName) {
                // Existing operator → confirm + DELETE
                removeExistingOperator(existingName, container, state);
            } else {
                // Pending row → just splice
                const id = Number(target.dataset.rowId);
                state.pending = state.pending.filter(r => r.id !== id);
                clearError(container);
                rerender(container, state);
            }
        });
    });
}

async function removeExistingOperator(name, container, state) {
    const message = `Remove operator "${name}"? Their conversation history stays in BlackBox memory but they won't appear in the operator dropdown.`;
    if (!confirm(message)) return;

    try {
        const r = await fetch(`/operator/${encodeURIComponent(name)}`, { method: "DELETE" });
        let result = {};
        try {
            result = await r.json();
        } catch (_e) {
            // empty body — ignore
        }
        if (r.ok && (result.status === "removed" || result.status === "not_present")) {
            state.existing = state.existing.filter(o => o.name !== name);
            // If existing is now empty AND no pending rows yet, give the
            // customer a default editable row so they can re-seed.
            if (state.existing.length === 0 && state.pending.length === 0) {
                state.pending = [{ id: nextRowId++, name: "" }];
            }
            clearError(container);
            rerender(container, state);
        } else {
            const detail = (result && (result.detail || result.error)) || `HTTP ${r.status}`;
            showError(container, `Couldn't remove "${name}": ${detail}`);
        }
    } catch (e) {
        showError(container, `Network error removing "${name}": ${e.message || e}`);
    }
}

function addRow(container, state) {
    const id = nextRowId++;
    state.pending.push({ id, name: "" });
    rerender(container, state);
    // Focus the newly-added input
    const newInput = container.querySelector(`.ob-operator-name[data-row-id="${id}"]`);
    if (newInput) newInput.focus();
}

function updateSaveButton(container, state) {
    const saveBtn = container.querySelector("#ob-operator-save");
    if (!saveBtn) return;

    const validPending = state.pending
        .map(r => r.name.trim())
        .filter(n => n && NAME_RE.test(n));

    // Always-enabled when existing is non-empty (can advance with no changes)
    const canAdvance = state.existing.length > 0 || validPending.length > 0;
    saveBtn.disabled = !canAdvance;

    // Adapt label: no pending changes → "Continue →"; otherwise "Save & continue →"
    const hasPendingChanges = validPending.length > 0;
    saveBtn.innerHTML = hasPendingChanges
        ? `Save &amp; continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>`
        : `Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>`;
}

function clearError(container) {
    const err = container.querySelector("#ob-operator-error");
    if (!err) return;
    err.textContent = "";
    err.hidden = true;
}

function showError(container, message) {
    const err = container.querySelector("#ob-operator-error");
    if (!err) return;
    err.textContent = message;
    err.hidden = false;
}

async function onSave(container, state, next) {
    if (busy) return;
    const saveBtn = container.querySelector("#ob-operator-save");

    // Validate pending names — collect trimmed valid names + reject on first error
    const validPending = [];
    for (const row of state.pending) {
        const name = row.name.trim();
        if (!name) continue;
        if (!NAME_RE.test(name)) {
            showError(container, `"${name}" is not a valid operator name. Use letters, numbers, _ or - (max 32 chars).`);
            return;
        }
        if (validPending.includes(name)) {
            showError(container, `"${name}" is duplicated. Each operator name must be unique.`);
            return;
        }
        if (state.existing.some(e => e.name === name)) {
            showError(container, `"${name}" already exists. Skip the duplicate or remove the existing one first.`);
            return;
        }
        validPending.push(name);
    }

    // Must have at least 1 operator total (existing + valid pending)
    if (state.existing.length === 0 && validPending.length === 0) {
        showError(container, "Add at least one operator (letters, numbers, _ or -).");
        return;
    }

    busy = true;
    saveBtn.disabled = true;
    const origLabel = saveBtn.innerHTML;
    saveBtn.innerHTML = validPending.length > 0 ? "Adding operators&hellip;" : "Continuing&hellip;";
    clearError(container);

    try {
        // Add each new operator (idempotent — re-add returns status:"exists")
        for (const name of validPending) {
            const r = await fetch("/operator/add", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            if (!r.ok) {
                const errBody = await r.text();
                throw new Error(`Couldn't add "${name}" (${r.status}): ${errBody.slice(0, 120)}`);
            }
        }

        // Persist DEFAULT_OPERATOR ONLY if existing was empty AND we added a new one.
        // Otherwise leave the existing default alone (Brandon's box already has one).
        if (state.existing.length === 0 && validPending.length > 0) {
            saveBtn.innerHTML = "Saving default&hellip;";
            const saveR = await fetch("/onboarding/save", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ secrets: { DEFAULT_OPERATOR: validPending[0] } }),
            });
            if (!saveR.ok) {
                throw new Error(`Couldn't persist DEFAULT_OPERATOR (${saveR.status})`);
            }
        }

        await next();
    } catch (e) {
        saveBtn.innerHTML = origLabel;
        saveBtn.disabled = false;
        showError(container, e.message || "Save failed. Try again.");
    } finally {
        busy = false;
    }
}

function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}
