/**
 * core-utils.js
 * Foundation utilities used across all modules
 *
 * Source: Portal/app.js lines 3146-3175, 3706-3714
 * Refactored: 2025-12-20
 */

// =============================================================================
// DOM Helpers
// =============================================================================

/**
 * DOM element selector helper - shorthand for getElementById
 * @param {string} id - Element ID to find
 * @returns {HTMLElement|null} The found element or null
 */
export const $ = (id) => document.getElementById(id);

// =============================================================================
// HTML Escaping (the shared helpers — use these at every innerHTML sink)
// =============================================================================

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/**
 * Escape a value for use as HTML element content.
 *
 * Pure string work (no DOM), so it also escapes both quote characters — the
 * old per-module `div.textContent = x; return div.innerHTML` copies did NOT,
 * which made them unsafe inside attribute values. null/undefined -> ''.
 * @param {*} value - Any value; non-strings are stringified
 * @returns {string} Escaped text, safe between tags
 */
export function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

/**
 * Escape a value for use inside a QUOTED attribute value (`attr="${...}"`).
 *
 * Same escaping as escapeHtml (both quotes included); the separate name marks
 * the attribute context at the call site. Never use it for unquoted attributes
 * or inside inline JS handlers (onclick="...") — build those with
 * addEventListener instead.
 * @param {*} value - Any value; non-strings are stringified
 * @returns {string} Escaped text, safe inside "..." or '...'
 */
export function escapeAttr(value) {
    return escapeHtml(value);
}

// =============================================================================
// Toast Notifications
// =============================================================================

/**
 * Display a toast notification message
 * @param {string} msg - Message to display
 */
export function toast(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hide");
    setTimeout(() => t.classList.add("hide"), 2000);
}

/**
 * Display a success toast notification (green styling)
 * @param {string} msg - Success message to display
 */
export function toastSuccess(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hide", "error");
    t.classList.add("success");
    setTimeout(() => {
        t.classList.add("hide");
        t.classList.remove("success");
    }, 3000);
}

/**
 * Display an error toast notification (red styling)
 * @param {string} msg - Error message to display
 */
export function toastError(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hide", "success");
    t.classList.add("error");
    setTimeout(() => {
        t.classList.add("hide");
        t.classList.remove("error");
    }, 4000);
}

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Generate a simple hash for string caching purposes
 * @param {string} str - String to hash
 * @returns {string} Base36 hash string
 */
export function simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash; // Convert to 32bit integer
    }
    return hash.toString(36);
}

// =============================================================================
// Async Utilities
// =============================================================================

/**
 * Sleep/delay helper for async operations
 * @param {number} ms - Milliseconds to wait
 * @returns {Promise<void>} Promise that resolves after delay
 */
export const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// =============================================================================
// Debug Utilities
// =============================================================================

/**
 * Log with module prefix for debugging
 * @param {string} module - Module name
 * @param  {...any} args - Arguments to log
 */
export function debugLog(module, ...args) {
    console.log(`[${module}]`, ...args);
}
