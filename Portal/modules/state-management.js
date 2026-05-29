/**
 * state-management.js
 * Centralized application state and localStorage operations
 *
 * Source: Portal/app.js lines 3588-3760, 3799-3845, 5200
 * Refactored: 2025-12-20
 */

// =============================================================================
// Storage Keys
// =============================================================================

export const OP_STORE = "bbx_operator";
export const CUSTOM_OPS_STORE = "bbx_custom_operators";
export const AGENT_SESSIONS_STORE = "bbx_agent_sessions";
export const STORE_KEY = "bbx_history_v2";
export const AUDIO_CACHE_KEY = "bbx_audio_cache";

// =============================================================================
// State Variables
// =============================================================================

/** Conversation history array */
export let historyData = [];

/** Audio cache for TTS playback - maps hash to data URL */
export let audioCache = {};

// Note: attachedFiles is managed by file-upload.js module

// =============================================================================
// Operator Management
// =============================================================================

/**
 * Get current operator name from localStorage
 * @returns {string} Operator name or empty string
 */
export function getOperator() {
    return (localStorage.getItem(OP_STORE) || "").trim();
}

/**
 * Set current operator name in localStorage
 * @param {string} name - Operator name
 */
export function setOperator(name) {
    localStorage.setItem(OP_STORE, (name || "").trim());
}

/**
 * Get all custom operators from localStorage
 * @returns {string[]} Array of custom operator names
 */
export function getCustomOperators() {
    try {
        const raw = localStorage.getItem(CUSTOM_OPS_STORE);
        const ops = raw ? JSON.parse(raw) : [];
        return Array.isArray(ops) ? ops : [];
    } catch {
        return [];
    }
}

/**
 * Save custom operators to localStorage
 * @param {string[]} ops - Array of operator names
 */
export function saveCustomOperators(ops) {
    try {
        localStorage.setItem(CUSTOM_OPS_STORE, JSON.stringify(ops));
    } catch {}
}

/**
 * Add a custom operator via API
 * @param {string} name - Operator name to add
 * @returns {Promise<boolean>} True if added successfully
 */
export async function addCustomOperator(name) {
    const trimmed = name.trim();
    if (!trimmed) return false;

    try {
        const response = await fetch('/operator/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: trimmed })
        });

        const result = await response.json();

        if (result.status === 'success') {
            return true;
        } else if (result.status === 'exists') {
            return false;
        } else {
            console.error('Failed to add operator:', result);
            return false;
        }
    } catch (error) {
        console.error('Error adding operator:', error);
        return false;
    }
}

/**
 * Remove a custom operator from localStorage
 * @param {string} name - Operator name to remove
 */
export function removeCustomOperator(name) {
    const ops = getCustomOperators();
    const filtered = ops.filter(op => op !== name);
    saveCustomOperators(filtered);
}

// =============================================================================
// Agent Session Persistence
// =============================================================================

/**
 * Get all operator sessions from localStorage
 * @returns {Object} Map of operator to session ID
 */
export function getOperatorSessions() {
    try {
        const raw = localStorage.getItem(AGENT_SESSIONS_STORE);
        return raw ? JSON.parse(raw) : {};
    } catch {
        return {};
    }
}

/**
 * Save an operator's session ID
 * @param {string} operator - Operator name
 * @param {string} sessionId - Session ID to save
 */
export function saveOperatorSession(operator, sessionId) {
    try {
        const sessions = getOperatorSessions();
        sessions[operator] = sessionId;
        localStorage.setItem(AGENT_SESSIONS_STORE, JSON.stringify(sessions));
        console.log('[Session] Saved session for operator:', operator, sessionId);
    } catch (e) {
        console.error('[Session] Failed to save session:', e);
    }
}

/**
 * Get session ID for a specific operator
 * @param {string} operator - Operator name
 * @returns {string|null} Session ID or null
 */
export function getOperatorSession(operator) {
    const sessions = getOperatorSessions();
    return sessions[operator] || null;
}

/**
 * Clear session for a specific operator
 * @param {string} operator - Operator name
 */
export function clearOperatorSession(operator) {
    try {
        const sessions = getOperatorSessions();
        delete sessions[operator];
        localStorage.setItem(AGENT_SESSIONS_STORE, JSON.stringify(sessions));
        console.log('[Session] Cleared session for operator:', operator);
    } catch (e) {
        console.error('[Session] Failed to clear session:', e);
    }
}

// =============================================================================
// History Management
// =============================================================================

/**
 * Load conversation history from localStorage
 */
export function loadHistory() {
    try {
        const raw = localStorage.getItem(STORE_KEY);
        historyData = raw ? JSON.parse(raw) : [];
        if (!Array.isArray(historyData)) historyData = [];
    } catch {
        historyData = [];
    }
}

/**
 * Get current history data array
 * @returns {Array} History data array
 */
export function getHistoryData() {
    return historyData;
}

/**
 * Save conversation history to localStorage with quota handling
 * @param {Function} [toastFn] - Optional toast function for notifications
 */
export function saveHistory(toastFn) {
    try {
        localStorage.setItem(STORE_KEY, JSON.stringify(historyData));
    } catch (e) {
        // Handle storage quota exceeded - delete oldest items first (FIFO)
        if (e.name === 'QuotaExceededError' || e.code === 22) {
            console.warn('[History] Storage quota exceeded - trimming old items');

            const maxAttempts = 5;
            let itemsToKeep = Math.floor(historyData.length * 0.7);

            for (let attempt = 0; attempt < maxAttempts; attempt++) {
                if (itemsToKeep < 10) itemsToKeep = 10;

                const trimmedHistory = historyData.slice(-itemsToKeep);

                try {
                    localStorage.setItem(STORE_KEY, JSON.stringify(trimmedHistory));
                    const removedCount = historyData.length - trimmedHistory.length;
                    historyData = trimmedHistory;
                    console.log(`[History] Trimmed ${removedCount} old items, keeping ${itemsToKeep} newest items`);
                    if (toastFn) toastFn(`Storage full - removed ${removedCount} oldest messages`);
                    return;
                } catch (retryError) {
                    itemsToKeep = Math.floor(itemsToKeep * 0.7);
                    console.warn(`[History] Still too large, trying ${itemsToKeep} items...`);
                }
            }

            // If we get here, even a small history failed - clear everything
            console.error('[History] Could not save even after trimming - clearing history');
            historyData = [];
            try {
                localStorage.setItem(STORE_KEY, JSON.stringify(historyData));
            } catch (finalError) {
                console.error('[History] Complete storage failure:', finalError);
            }
            if (toastFn) toastFn('Storage critical - history cleared');
        } else {
            console.error('[History] Save failed:', e);
        }
    }
}

/**
 * Clear conversation history
 */
export function clearHistory() {
    historyData = [];
    try {
        localStorage.removeItem(STORE_KEY);
    } catch {}
}

/**
 * Add item to history
 * @param {Object} item - History item to add
 */
export function addToHistory(item) {
    historyData.push(item);
}

/**
 * Set entire history data
 * @param {Array} data - New history array
 */
export function setHistoryData(data) {
    historyData = Array.isArray(data) ? data : [];
}

// =============================================================================
// Audio Cache Management
// =============================================================================

/**
 * Load audio cache from localStorage
 */
export function loadAudioCache() {
    try {
        const raw = localStorage.getItem(AUDIO_CACHE_KEY);
        audioCache = raw ? JSON.parse(raw) : {};
        console.log('[AudioCache] Loaded cache with', Object.keys(audioCache).length, 'items');
        if (raw) {
            console.log('[AudioCache] Cache size:', (raw.length / 1024).toFixed(2), 'KB');
        }
    } catch (e) {
        console.error('[AudioCache] Load failed:', e);
        audioCache = {};
    }
}

/**
 * Save audio cache to localStorage with quota handling
 */
export function saveAudioCache() {
    try {
        const serialized = JSON.stringify(audioCache);
        console.log('[AudioCache] Saving', Object.keys(audioCache).length, 'items, size:', (serialized.length / 1024).toFixed(2), 'KB');
        localStorage.setItem(AUDIO_CACHE_KEY, serialized);
        console.log('[AudioCache] Save successful');
    } catch (e) {
        console.error('[AudioCache] Save failed:', e);
        if (e.name === 'QuotaExceededError') {
            console.warn('[AudioCache] Storage quota exceeded - clearing old audio');
            const keys = Object.keys(audioCache);
            if (keys.length > 3) {
                const keysToRemove = keys.slice(0, keys.length - 3);
                keysToRemove.forEach(k => delete audioCache[k]);
                try {
                    localStorage.setItem(AUDIO_CACHE_KEY, JSON.stringify(audioCache));
                    console.log('[AudioCache] Cleared old audio, saved', Object.keys(audioCache).length, 'items');
                } catch (e2) {
                    console.error('[AudioCache] Still failed after cleanup:', e2);
                }
            }
        }
    }
}

/**
 * Clear audio cache
 */
export function clearAudioCache() {
    audioCache = {};
    try {
        localStorage.removeItem(AUDIO_CACHE_KEY);
    } catch {}
}

/**
 * Get the entire audio cache object
 * @returns {Object} The audio cache object
 */
export function getAudioCache() {
    return audioCache;
}

/**
 * Get cached audio by hash
 * @param {string} hash - Audio hash key
 * @returns {string|null} Cached data URL or null
 */
export function getCachedAudio(hash) {
    return audioCache[hash] || null;
}

/**
 * Set cached audio
 * @param {string} hash - Audio hash key
 * @param {string} dataUrl - Audio data URL
 */
export function setCachedAudio(hash, dataUrl) {
    audioCache[hash] = dataUrl;
}

// =============================================================================
// Utility Functions
// =============================================================================
// Note: Attached file functions are in file-upload.js module

/**
 * Convert blob to base64 data URL
 * @param {Blob} blob - Blob to convert
 * @returns {Promise<string>} Base64 data URL
 */
export async function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
    });
}

// =============================================================================
// Model Configuration
// =============================================================================

/**
 * Model configuration for each provider.
 *
 * IMPORTANT (T2 / 2026-05-18): The 4 chat providers' `models[]` arrays are
 * now OFFLINE-BOOTSTRAP FALLBACKS only — minimal 3-5 entries shown until
 * fetchAvailableModels() hydrates from the backend's /models/{provider}
 * endpoint (the single source of truth — see
 * Orchestrator/routes/admin_routes.py). Once hydrated, these arrays are
 * REPLACED with the live catalog.
 *
 * Non-chat entries (computer-use, agents, gemini-agents, realtime) are
 * NOT centralized — they're subsystem-pinned model lists with different
 * lifecycles than chat models, so they remain hardcoded here.
 */
const MODEL_CONFIG = {
    google: {
        name: "Google",
        models: [  // Offline fallback — replaced by /models/google fetch
            { id: "", name: "(Auto - Latest)", default: true },
            { id: "gemini-3.1-pro-preview", name: "Gemini 3.1 Pro" },
            { id: "gemini-2.5-pro", name: "Gemini 2.5 Pro" },
            { id: "gemini-2.5-flash", name: "Gemini 2.5 Flash" }
        ]
    },
    anthropic: {
        name: "Anthropic",
        models: [  // Offline fallback — replaced by /models/anthropic fetch
            { id: "", name: "(Auto - Latest)", default: true },
            { id: "claude-opus-4-8", name: "Claude Opus 4.8" },
            { id: "claude-opus-4-7", name: "Claude Opus 4.7" },
            { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6" },
            { id: "claude-haiku-4-5-20251001", name: "Claude Haiku 4.5" }
        ]
    },
    openai: {
        name: "OpenAI",
        models: [  // Offline fallback — replaced by /models/openai fetch
            { id: "", name: "(Auto - Latest)", default: true },
            { id: "gpt-5.1", name: "GPT-5.1" },
            { id: "gpt-5-mini-2025-08-07", name: "GPT-5 Mini" },
            { id: "o3-2025-04-16", name: "o3 (Reasoning)" }
        ]
    },
    xai: {
        name: "xAI (Grok)",
        models: [  // Offline fallback — replaced by /models/xai fetch
            { id: "", name: "(Auto - Latest)", default: true },
            { id: "grok-4.3", name: "Grok 4.3" },
            { id: "grok-4.20-multi-agent-0309", name: "Grok 4.20 Multi-Agent" },
            { id: "grok-3-mini-beta", name: "Grok 3 Mini (legacy, cheap)" }
        ]
    },
    "computer-use": {
        name: "Computer Use",
        models: [
            { id: "claude-opus-4-7", name: "Claude Opus 4.7 (Sovereign)", default: true },
            { id: "claude-opus-4-6", name: "Claude Opus 4.6" },
            { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6" },
            { id: "gemini-2.5-computer-use-preview-10-2025", name: "Gemini CU Preview" }
        ]
    },
    agents: {
        name: "Claude Code CLI",
        models: [
            { id: "claude-code", name: "Claude Code CLI", default: true }
        ]
    },
    "gemini-agents": {
        name: "Gemini CLI",
        models: [
            { id: "gemini-cli", name: "Gemini CLI", default: true }
        ]
    },
    realtime: {
        name: "Realtime",
        models: [
            { id: "gpt-realtime", name: "GPT Realtime (GA)", default: true },
            { id: "gpt-4o-realtime-preview-2025-06-03", name: "GPT-4o Realtime Preview" }
        ]
    }
};

/**
 * Update model dropdown based on selected provider
 * @param {string} provider - Provider name
 */
export function updateModelDropdown(provider) {
    console.log(`[ModelSelect] updateModelDropdown called for provider: ${provider}`);

    const modelEl = document.getElementById("modelSelect");
    if (!modelEl) {
        console.error('[ModelSelect] modelSelect element not found!');
        return;
    }

    const config = MODEL_CONFIG[provider];
    if (!config) {
        console.warn(`[ModelSelect] Unknown provider: ${provider}`);
        return;
    }

    console.log(`[ModelSelect] Config found with ${config.models.length} models`);

    // Save current selection
    const currentModel = modelEl.value;

    // Clear and rebuild dropdown
    modelEl.innerHTML = "";

    config.models.forEach((model, index) => {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = model.name;
        modelEl.appendChild(option);
        if (index === 0) {
            console.log(`[ModelSelect] Added first option: ${model.name} (id: ${model.id})`);
        }
    });

    console.log(`[ModelSelect] Dropdown now has ${modelEl.options.length} options`);

    // Restore selection if it exists for this provider, otherwise use default (Auto)
    const savedModel = localStorage.getItem(`bb_model_${provider}`);
    if (savedModel && config.models.some(m => m.id === savedModel)) {
        modelEl.value = savedModel;
    } else {
        modelEl.value = ""; // Auto
    }

    window.__model = modelEl.value || "";
    console.log(`[ModelSelect] Updated for provider ${provider}, model: ${window.__model || "(auto)"}, options: ${modelEl.options.length}`);
}

/**
 * Fetch available models from backend /models/{provider} and hydrate
 * MODEL_CONFIG[provider].models in place. Always prepends the
 * `(Auto - Latest)` placeholder so the dropdown's default option is
 * preserved across hydration.
 *
 * Caches successful fetches in sessionStorage for 5 minutes to avoid
 * hammering the backend (which has its own 10-minute upstream cache,
 * but sessionStorage lets us skip the round-trip entirely on
 * provider-switch within a tab).
 *
 * @param {string} provider - one of "google" | "anthropic" | "openai" | "xai"
 * @returns {Promise<boolean>} true if MODEL_CONFIG was updated (from cache or fetch)
 */
export async function fetchAvailableModels(provider) {
    const CACHE_TTL_MS = 5 * 60 * 1000;  // 5 minutes
    const cacheKey = `bb_models:${provider}`;

    // Try sessionStorage cache first
    try {
        const cached = JSON.parse(sessionStorage.getItem(cacheKey) || "null");
        if (cached && Date.now() - cached.ts < CACHE_TTL_MS && Array.isArray(cached.models)) {
            MODEL_CONFIG[provider].models = [
                { id: "", name: "(Auto - Latest)", default: true },
                ...cached.models.map(m => ({ id: m.id, name: m.name || m.id }))
            ];
            console.log(`[ModelSelect] Cache hit for ${provider} (${cached.models.length} models, age ${Math.round((Date.now() - cached.ts) / 1000)}s)`);
            return true;
        }
    } catch (_) { /* corrupted cache — fall through to live fetch */ }

    // Live fetch via backend (which itself has 10-min upstream cache)
    try {
        const response = await fetch(`/models/${provider}`);
        if (response.ok) {
            const data = await response.json();
            if (data.models && data.models.length > 0) {
                MODEL_CONFIG[provider].models = [
                    { id: "", name: "(Auto - Latest)", default: true },
                    ...data.models.map(m => ({ id: m.id, name: m.name || m.id }))
                ];
                // Cache the live result for 5 min
                try {
                    sessionStorage.setItem(cacheKey, JSON.stringify({
                        ts: Date.now(),
                        models: data.models,
                        source: data.source || "live"
                    }));
                } catch (_) { /* sessionStorage full or disabled — ignore */ }
                console.log(`[ModelSelect] Fetched ${data.models.length} models for ${provider} (source=${data.source || "unknown"})`);
                return true;
            }
        }
    } catch (e) {
        console.log(`[ModelSelect] Could not fetch models for ${provider}, using built-in fallback:`, e.message);
    }
    return false;
}

/**
 * Abbreviate operator name for compact display
 * - No spaces in abbreviated form
 * - Full name shown in dropdown and as title
 * @param {string} name - Full operator name
 * @returns {string} Abbreviated name
 */
function abbreviateOperator(name) {
    if (!name || name.length <= 10) return name;
    // If no spaces, return as-is (e.g., "Brandon-DEV")
    if (!name.includes(' ')) return name;
    // Split by spaces, take first word + initials of rest
    const parts = name.split(' ');
    if (parts.length === 1) return name;
    // First word + first letter of each subsequent word
    const abbrev = parts[0] + parts.slice(1).map(p => p[0]?.toUpperCase() || '').join('');
    return abbrev.length <= 12 ? abbrev : abbrev.substring(0, 12);
}

/**
 * Initialize operator selector dropdown
 * Note: This function doesn't handle session switching to avoid circular dependencies.
 * The onchange handler should be extended by app-init.js if needed.
 */
export async function initOperatorSelector() {
    try {
        const r = await fetch("/health");
        const j = await r.json();
        const serverOps = (j.users && Array.isArray(j.users.list)) ? j.users.list : [];

        // All operators now come from server (config.ini)
        const allOps = serverOps;

        const def = (j.users && j.users.default) ? j.users.default : (allOps[0] || "Operator");
        const sel = document.getElementById("operatorSelect");
        if (!sel) return;
        sel.innerHTML = "";

        // Add all operators with abbreviated display text
        allOps.forEach(name => {
            const opt = document.createElement("option");
            opt.value = name;
            opt.textContent = abbreviateOperator(name);
            opt.title = name; // Full name on hover
            sel.appendChild(opt);
        });

        // Add "Add..." option (compact)
        const addOpt = document.createElement("option");
        addOpt.value = "__add__";
        addOpt.textContent = "+ Add...";
        addOpt.title = "Add new operator";
        addOpt.style.fontStyle = "italic";
        addOpt.style.color = "var(--accent)";
        sel.appendChild(addOpt);

        const persisted = getOperator();
        const active = (persisted && allOps.includes(persisted)) ? persisted : def;
        sel.value = active;
        setOperator(active);

        sel.onchange = () => {
            if (sel.value === "__add__") {
                // Open add operator modal
                const modal = document.getElementById("addOperatorModal");
                if (modal) modal.classList.remove("hide");
                // Reset to previous operator
                sel.value = active;
            } else {
                const newOperator = sel.value;
                setOperator(newOperator);
                window.__operator = newOperator;
                // Refresh health to update checkpoint counter for new operator
                import('./ui-setup.js').then(({ refreshHealth }) => refreshHealth());
                // Sync voice dropdown to new operator's preference
                import('./tts-stt.js').then(({ syncVoiceDropdown }) => syncVoiceDropdown());
            }
        };
    } catch (e) {
        console.warn("initOperatorSelector failed:", e);
    }
}
