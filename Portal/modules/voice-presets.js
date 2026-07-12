/**
 * voice-presets.js
 * Shared Voice Agent preset support for the three voice panels
 * (gpt-realtime.js / gemini-live.js / grok-live.js).
 *
 * Fetches GET /voice-agents (P4 preset-registry contract:
 * [{id, name, provider, ...}]) and populates a per-panel <select>.
 * The endpoint may not exist yet (P4 ships after P3) — on ANY failure
 * (network error, non-200, empty list after provider filtering) the
 * preset row stays hidden and the panel behaves exactly as before.
 *
 * The selected preset id is sent as `agent` in the WS connect message;
 * backend precedence is explicit params > preset > defaults (design doc
 * workstream 3), so sending both is safe.
 */

/**
 * Filter presets to one panel's provider family.
 * Alias sets are deliberately generous (e.g. 'openai'|'realtime') so the
 * panels tolerate whichever canonical provider string P4 lands.
 * @param {Array} presets - raw /voice-agents list
 * @param {Array<string>} aliases - accepted provider strings
 * @returns {Array} presets whose .provider matches (case-insensitive)
 */
export function filterPresetsByProvider(presets, aliases) {
    if (!Array.isArray(presets)) return [];
    const accept = new Set(aliases.map(a => String(a).toLowerCase()));
    return presets.filter(p => p && p.id && accept.has(String(p.provider || '').toLowerCase()));
}

/**
 * Fetch the preset registry. Fresh fetch on every call — presets are
 * user-edited, so no sessionStorage cache. Returns [] on any failure.
 * @returns {Promise<Array>}
 */
export async function fetchVoicePresets() {
    try {
        const res = await fetch('/voice-agents');
        if (!res.ok) return [];
        const data = await res.json();
        if (Array.isArray(data)) return data;
        if (data && Array.isArray(data.agents)) return data.agents;
        if (data && Array.isArray(data.presets)) return data.presets;
        return [];
    } catch (err) {
        console.log('[VOICE-PRESETS] /voice-agents unavailable (pre-P4 is fine):', err.message);
        return [];
    }
}

/**
 * Populate a preset <select> and unhide its .va-row wrapper.
 * First option = "None (manual config)" (empty value → connect() omits
 * the agent field entirely). No-op on empty preset list: the row stays
 * hidden (rows ship with style="display:none;" in index.html).
 * @param {HTMLSelectElement|null} selectEl
 * @param {Array} presets - already provider-filtered
 */
export function populatePresetDropdown(selectEl, presets) {
    if (!selectEl || !Array.isArray(presets) || presets.length === 0) return;
    selectEl.innerHTML = '';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'None (manual config)';
    none.selected = true;
    selectEl.appendChild(none);
    presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name || p.id;
        selectEl.appendChild(opt);
    });
    const row = selectEl.closest('.va-row');
    if (row) row.style.display = '';
    console.log(`[VOICE-PRESETS] Preset dropdown populated with ${presets.length} presets`);
}

/**
 * Re-fetch the registry and repopulate every panel's preset dropdown.
 * Called on every Voice Agents modal open (and after P4.10 manage-UI
 * saves/deletes) so registry edits appear without a page reload.
 * Empty (or emptied) registry: the select is cleared and its va-row
 * re-hidden — the panel returns to the exact pre-P4 look.
 * Alias arrays MUST stay identical to the per-panel init hooks
 * (P3.25-27) — copy them from gpt-realtime.js / gemini-live.js /
 * grok-live.js if they differ from the ones below.
 * @returns {Promise<Array>} the fetched presets (P4.10 manage UI reuses them)
 */
export async function refreshAllPresetDropdowns() {
    const presets = await fetchVoicePresets();
    const panels = [
        ['vaRealtimePresetSelect', ['openai', 'realtime', 'gpt-realtime']],
        ['vaGeminiPresetSelect', ['google', 'gemini', 'gemini-live']],
        ['vaGrokPresetSelect', ['grok', 'xai', 'grok-live']],
    ];
    for (const [id, aliases] of panels) {
        const sel = document.getElementById(id);
        if (!sel) continue;
        const scoped = filterPresetsByProvider(presets, aliases);
        if (scoped.length === 0) {
            sel.innerHTML = '';
            const row = sel.closest('.va-row');
            if (row) row.style.display = 'none';
        } else {
            populatePresetDropdown(sel, scoped);
        }
    }
    return presets;
}

// ------------------------------------------------------------------ manage UI

const $id = (i) => document.getElementById(i);
let managePresets = [];

function fillForm(p) {
    $id('vaPresetName').value = p?.name || '';
    $id('vaPresetProvider').value = p?.provider || 'realtime';
    $id('vaPresetModel').value = p?.model || '';
    $id('vaPresetVoice').value = p?.voice || '';
    $id('vaPresetGreeting').value = p?.greeting || '';
    $id('vaPresetInstructions').value = p?.instructions || '';
    $id('vaPresetDelete').disabled = !p;
}

function renderManageList(presets) {
    managePresets = presets;
    const list = $id('vaPresetList');
    if (!list) return;
    const current = list.value;
    list.innerHTML = '<option value="">— new preset —</option>';
    presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} (${p.provider})`;
        list.appendChild(opt);
    });
    if ([...list.options].some(o => o.value === current)) list.value = current;
    const status = $id('vaPresetStatus');
    if (status) status.textContent = presets.length
        ? `${presets.length} preset(s)` : 'No presets yet — fill the form and Save.';
}

async function savePreset() {
    const id = $id('vaPresetList').value;
    const body = {
        name: $id('vaPresetName').value.trim(),
        provider: $id('vaPresetProvider').value,
        model: $id('vaPresetModel').value.trim(),
        voice: $id('vaPresetVoice').value.trim(),
        greeting: $id('vaPresetGreeting').value,
        instructions: $id('vaPresetInstructions').value,
    };
    const res = await fetch(id ? `/voice-agents/${id}` : '/voice-agents', {
        method: id ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const status = $id('vaPresetStatus');
    if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
        status.textContent = `Save failed: ${detail}`;   // fail LOUDLY, keep form
        return;
    }
    status.textContent = id ? 'Updated.' : 'Created.';
    renderManageList(await refreshAllPresetDropdowns());
}

async function deletePreset() {
    const id = $id('vaPresetList').value;
    if (!id) return;
    const res = await fetch(`/voice-agents/${id}`, { method: 'DELETE' });
    $id('vaPresetStatus').textContent = res.ok ? 'Deleted.' : `Delete failed: HTTP ${res.status}`;
    $id('vaPresetList').value = '';
    fillForm(null);
    renderManageList(await refreshAllPresetDropdowns());
}

export function initPresetManageUI() {
    if (!$id('vaPresetSave')) return;   // markup absent — degrade silently
    $id('vaPresetSave').addEventListener('click', savePreset);
    $id('vaPresetDelete').addEventListener('click', deletePreset);
    $id('vaPresetList').addEventListener('change', () => {
        fillForm(managePresets.find(p => p.id === $id('vaPresetList').value) || null);
    });
}

export async function refreshManageUI() {
    renderManageList(await refreshAllPresetDropdowns());
}
