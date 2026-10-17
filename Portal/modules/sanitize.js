/**
 * sanitize.js
 * The ONE choke point that turns assistant / tool / ledger text into markup.
 *
 * Every model-influenced string (chat replies, agent output, stored history,
 * task results) that reaches innerHTML goes through sanitizeAssistantHtml(), or
 * through renderMarkdown()/markdownToSafeHtml() in markdown-renderer.js, which
 * call it. Model output is attacker-steerable (prompt-injected web_fetch pages,
 * email, SMS, tool results), so this module FAILS CLOSED: when DOMPurify is
 * missing, unsupported or throws, the input comes back as escaped plain text —
 * never as raw HTML.
 *
 * DOMPurify is vendored at Portal/vendor/dompurify/ (see Portal/vendor/VENDORED.md)
 * and loaded by index.html as a classic script, so it is present on an offline
 * or LAN-only box. It is read lazily from globalThis on every call.
 *
 * Created: 2026-10-17 (audit P01-chat-core-web-01/03, X04-security-clients-infra-06/08)
 */

import { escapeHtml, toastError } from './core-utils.js';

// =============================================================================
// DOMPurify configuration
// =============================================================================

/**
 * Explicit allowlist additions on top of DOMPurify's safe defaults.
 * - Media the renderers emit (convertMediaUrlsToHtml, inline_media, generated
 *   image/video/music results) must survive: audio, video, source + their
 *   playback attributes. class and data-* (data-highlight, data-snap-id,
 *   data-task-id) are kept by DOMPurify's defaults.
 * - Event handlers, javascript:/vbscript: URLs, <script>, <iframe>, <object>
 *   and <embed> are removed by DOMPurify's defaults; <style> (page-wide
 *   restyling / overlay phishing) and <form> (credential-phishing posts to a
 *   foreign origin) are forbidden on top.
 */
const PURIFY_CONFIG = Object.freeze({
    ADD_TAGS: ['audio', 'video', 'source'],
    ADD_ATTR: ['controls', 'preload', 'poster', 'playsinline', 'loop', 'muted', 'src', 'type', 'download', 'loading'],
    FORBID_TAGS: ['style', 'form', 'iframe', 'frame', 'frameset', 'object', 'embed', 'base', 'link', 'meta', 'script'],
    FORBID_ATTR: ['formaction', 'action', 'srcdoc', 'ping'],
    ALLOW_UNKNOWN_PROTOCOLS: false,
});

let _warnedUnavailable = false;

/**
 * Resolve a usable DOMPurify instance, or null.
 * @returns {object|null}
 */
function getPurifier() {
    const purifier = globalThis.DOMPurify;
    if (!purifier || typeof purifier.sanitize !== 'function') return null;
    if (purifier.isSupported === false) return null;
    return purifier;
}

/**
 * Loudly report (once per page load) that rendering degraded to plain text.
 * @param {string} reason - Why the sanitizer could not be used
 * @param {*} [err] - Optional underlying error
 */
function warnUnavailable(reason, err) {
    if (_warnedUnavailable) return;
    _warnedUnavailable = true;
    console.error(`[SANITIZE] ${reason} — rendering assistant content as plain text (fail closed).`, err || '');
    try {
        if (typeof document !== 'undefined') {
            toastError('HTML sanitizer unavailable — replies shown as plain text');
        }
    } catch (_) {
        // toast is best-effort; the console error above is the record
    }
}

// =============================================================================
// Public API
// =============================================================================

/**
 * True when DOMPurify is loaded and usable.
 * @returns {boolean}
 */
export function isSanitizerAvailable() {
    return getPurifier() !== null;
}

/**
 * Escape text for display as HTML, keeping line breaks. This is the fail-closed
 * rendering: it can never produce an element other than <br>.
 * @param {*} text - Any value; null/undefined -> ''
 * @returns {string} Escaped HTML
 */
export function plainTextHtml(text) {
    return escapeHtml(text).replace(/\r?\n/g, '<br>');
}

/**
 * Sanitize assistant/model/tool/ledger HTML for innerHTML.
 * Fails closed: without a working DOMPurify the input is returned escaped.
 * @param {*} html - Untrusted HTML (null/undefined -> '')
 * @returns {string} Safe HTML
 */
export function sanitizeAssistantHtml(html) {
    const dirty = (html === null || html === undefined) ? '' : String(html);
    const purifier = getPurifier();
    if (!purifier) {
        warnUnavailable('DOMPurify is not loaded');
        return plainTextHtml(dirty);
    }
    try {
        const clean = purifier.sanitize(dirty, PURIFY_CONFIG);
        return typeof clean === 'string' ? clean : plainTextHtml(dirty);
    } catch (e) {
        warnUnavailable('DOMPurify.sanitize threw', e);
        return plainTextHtml(dirty);
    }
}

/**
 * Extract the visible text of an HTML string WITHOUT touching the live document.
 *
 * `div.innerHTML = html` on a detached element of the live document still
 * creates live elements — `<img src=x onerror=...>` fires. DOMParser builds an
 * inert document (no script, no handlers, no resource loads) instead.
 * @param {*} html - Untrusted HTML
 * @param {string} [removeSelector] - Elements to drop before reading the text
 * @returns {string} The text content (not whitespace-normalized)
 */
export function htmlToText(html, removeSelector = '') {
    const source = (html === null || html === undefined) ? '' : String(html);
    if (typeof DOMParser === 'undefined') {
        // Non-browser fallback: strip tags textually (never parses as markup).
        return source.replace(/<[^>]*>/g, '');
    }
    const doc = new DOMParser().parseFromString(source, 'text/html');
    const body = doc.body;
    if (!body) return '';
    const selector = ['script', 'style', 'template', 'noscript', removeSelector].filter(Boolean).join(', ');
    body.querySelectorAll(selector).forEach(el => el.remove());
    return body.textContent || '';
}
