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
