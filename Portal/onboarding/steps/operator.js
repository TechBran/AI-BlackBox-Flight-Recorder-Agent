// Operator setup step — sixth screen of the onboarding wizard.
// Customer enters one or more operator names. Each is POSTed to
// /operator/add (idempotent — re-add returns status:"exists"). The
// first valid name is persisted as DEFAULT_OPERATOR via /save so
// Portal opens with that operator selected.
//
// NO Brandon prefill — that's a code-level seed, not a customer-visible
// default. Customers see an empty form with placeholder "e.g. Sarah".
//
// Visual reference: extends design system. Multi-row input form with
// Add another / Remove (×) buttons.

const NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;

let busy = false;
let nextRowId = 1;  // monotonically increasing across re-renders

export async function render(container, { next, back, skip }) {
    // Per-render state — lost on re-render (which is fine for setup-mode)
    const state = {
        rows: [{ id: 0, name: "" }],
    };

    container.innerHTML = `
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
                <div class="ob-operator-rows" id="ob-operator-rows">
                    ${renderRow(state.rows[0], 0)}
                </div>
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

    wireRows(container, state);
    document.getElementById("ob-operator-add").addEventListener("click", () => addRow(container, state));
    document.getElementById("ob-operator-save").addEventListener("click", () => onSave(container, state, next));
    document.getElementById("ob-operator-back").addEventListener("click", back);
    // Focus the first input on mount
    const first = container.querySelector(".ob-operator-name");
    if (first) first.focus();
}

function renderRow(row, idx) {
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
            ${idx > 0 ? `
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
    rowsEl.querySelectorAll(".ob-operator-name").forEach(input => {
        input.addEventListener("input", e => {
            const id = Number(e.target.dataset.rowId);
            const row = state.rows.find(r => r.id === id);
            if (row) {
                row.name = e.target.value.trim();
                clearError(container);
                updateSaveButton(container, state);
            }
        });
    });
    rowsEl.querySelectorAll(".ob-row-remove").forEach(btn => {
        btn.addEventListener("click", e => {
            const id = Number(e.target.dataset.rowId);
            state.rows = state.rows.filter(r => r.id !== id);
            // Re-render rows
            rowsEl.innerHTML = state.rows.map((r, i) => renderRow(r, i)).join("");
            wireRows(container, state);
            updateSaveButton(container, state);
        });
    });
}

function addRow(container, state) {
    const id = nextRowId++;
    state.rows.push({ id, name: "" });
    const rowsEl = container.querySelector("#ob-operator-rows");
    rowsEl.innerHTML = state.rows.map((r, i) => renderRow(r, i)).join("");
    wireRows(container, state);
    updateSaveButton(container, state);
    // Focus the newly-added input
    const lastInput = rowsEl.querySelector(`.ob-operator-name[data-row-id="${id}"]`);
    if (lastInput) lastInput.focus();
}

function updateSaveButton(container, state) {
    const saveBtn = container.querySelector("#ob-operator-save");
    const validNames = state.rows
        .map(r => r.name.trim())
        .filter(n => n && NAME_RE.test(n));
    saveBtn.disabled = validNames.length === 0;
}

function clearError(container) {
    const err = container.querySelector("#ob-operator-error");
    err.textContent = "";
    err.hidden = true;
}

function showError(container, message) {
    const err = container.querySelector("#ob-operator-error");
    err.textContent = message;
    err.hidden = false;
}

async function onSave(container, state, next) {
    if (busy) return;
    const saveBtn = container.querySelector("#ob-operator-save");

    // Re-validate on submit (defense-in-depth)
    const validNames = [];
    for (const row of state.rows) {
        const name = row.name.trim();
        if (!name) continue;
        if (!NAME_RE.test(name)) {
            showError(container, `"${name}" is not a valid operator name. Use letters, numbers, _ or - (max 32 chars).`);
            return;
        }
        if (validNames.includes(name)) {
            showError(container, `"${name}" is duplicated. Each operator name must be unique.`);
            return;
        }
        validNames.push(name);
    }
    if (validNames.length === 0) {
        showError(container, "Enter at least one operator name (letters, numbers, _ or -).");
        return;
    }

    busy = true;
    saveBtn.disabled = true;
    const orig = saveBtn.innerHTML;
    saveBtn.innerHTML = "Adding operators&hellip;";
    clearError(container);

    try {
        // Add each operator
        for (const name of validNames) {
            const r = await fetch("/operator/add", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            if (!r.ok) {
                const errBody = await r.text();
                throw new Error(`Couldn't add "${name}" (${r.status}): ${errBody.slice(0, 120)}`);
            }
            // Status "exists" returns 200 — fine, idempotent re-add
        }

        // Persist DEFAULT_OPERATOR (first valid name)
        saveBtn.innerHTML = "Saving default&hellip;";
        const saveR = await fetch("/onboarding/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ secrets: { DEFAULT_OPERATOR: validNames[0] } }),
        });
        if (!saveR.ok) {
            throw new Error(`Couldn't persist DEFAULT_OPERATOR (${saveR.status})`);
        }

        await next();
    } catch (e) {
        saveBtn.innerHTML = orig;
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
