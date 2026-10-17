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
 *   restyling) and <form> (credential-phishing posts to a foreign origin) are
 *   forbidden on top, and so is <dialog>, which the browser positions above
 *   the page.
 * - Model output never owns a Portal id or name: SANITIZE_NAMED_PROPS rewrites
 *   them to "user-content-<value>". The Portal looks its own fields up by id
 *   page-wide, and #history comes before every modal in index.html, so a
 *   planted <input id="contactIsOperatorSelf" checked> would otherwise win the
 *   lookup (DOM clobbering). Attributes that ACT on another element by id
 *   (label for, popovertarget, commandfor/command, list) are dropped as well.
 * - Inline style is kept (server placeholders rely on it) but may not lift
 *   content out of its bubble: see keepStyleInFlow below. Class names are
 *   kept too (the renderer and stored history rely on many), so a reply can
 *   still borrow a Portal class's look; bounding THAT needs CSS containment
 *   on the bubble, not the sanitizer.
 */
const PURIFY_CONFIG = Object.freeze({
    ADD_TAGS: ['audio', 'video', 'source'],
    ADD_ATTR: ['controls', 'preload', 'poster', 'playsinline', 'loop', 'muted', 'src', 'type', 'download', 'loading'],
    FORBID_TAGS: ['style', 'form', 'iframe', 'frame', 'frameset', 'object', 'embed', 'base', 'link', 'meta', 'script', 'dialog'],
    FORBID_ATTR: ['formaction', 'action', 'srcdoc', 'ping',
        'for', 'list', 'popover', 'popovertarget', 'popovertargetaction', 'commandfor', 'command'],
    SANITIZE_NAMED_PROPS: true,
    ALLOW_UNKNOWN_PROTOCOLS: false,
});

// Declarations that would let model content draw over the Portal's own UI
// (a fake "session expired" sheet over the composer): any non-static
// positioning, stacking (z-index), transforms, and negative margins.
const LIFTING_PROPERTIES = ['z-index', 'transform', 'translate', 'scale', 'rotate', 'offset-path'];
const MARGIN_PROPERTIES = ['margin-top', 'margin-right', 'margin-bottom', 'margin-left',
    'margin-block-start', 'margin-block-end', 'margin-inline-start', 'margin-inline-end'];

let _styleProbe = null;

/**
 * True when an inline style value would take content out of normal flow.
 * Parsed by the browser's own CSS parser (in an inert document), so escapes,
 * !important and var() are seen exactly as they would render; an unresolved
 * var() counts as lifting.
 * @param {string} value - A style attribute value
 * @returns {boolean}
 */
function styleLiftsContent(value) {
    if (!_styleProbe) {
        _styleProbe = document.implementation.createHTMLDocument('').createElement('div');
    }
    _styleProbe.setAttribute('style', value);
    const style = _styleProbe.style;
    const position = style.getPropertyValue('position').trim().toLowerCase();
    if (position && position !== 'static') return true;
    if (LIFTING_PROPERTIES.some(prop => style.getPropertyValue(prop).trim() !== '')) return true;
    return MARGIN_PROPERTIES.some(prop => style.getPropertyValue(prop).includes('-'));
}

/**
 * DOMPurify uponSanitizeAttribute hook: drop a style attribute that would lift
 * model content out of its bubble. Ordinary styles are kept byte-identical.
 */
function keepStyleInFlow(node, data) {
    if (data.attrName !== 'style' || !data.attrValue) return;
    try {
        if (styleLiftsContent(data.attrValue)) data.keepAttr = false;
    } catch (_) {
        data.keepAttr = false;  // unparseable here: drop it rather than guess
    }
}

// Degradation warnings already shown this page load, by component.
const _warned = new Set();

const DEGRADED = {
    sanitizer: {
        reason: 'DOMPurify is not loaded',
        effect: 'rendering assistant content as plain text (fail closed)',
        toast: 'HTML sanitizer unavailable — replies shown as plain text',
    },
    markdown: {
        reason: 'marked is not loaded',
        effect: 'rendering assistant content without markdown formatting',
        toast: 'Markdown renderer unavailable — replies shown without formatting',
    },
};

// One private DOMPurify instance per loaded library, carrying this module's
// hook, so the shared global instance is never mutated.
const _purifiers = new WeakMap();

/**
 * Build this module's DOMPurify instance from the loaded library, or null.
 * @param {Function|object} shared - globalThis.DOMPurify
 * @returns {object|null}
 */
function buildPurifier(shared) {
    try {
        const own = (typeof shared === 'function' && typeof window !== 'undefined') ? shared(window) : shared;
        if (!own || typeof own.sanitize !== 'function' || own.isSupported === false) return null;
        if (typeof own.addHook === 'function') own.addHook('uponSanitizeAttribute', keepStyleInFlow);
        return own;
    } catch (e) {
        warnDegraded('sanitizer', 'DOMPurify instance could not be created', e);
        return null;
    }
}

/**
 * Resolve a usable DOMPurify instance, or null.
 * @returns {object|null}
 */
function getPurifier() {
    const shared = globalThis.DOMPurify;
    if (!shared || typeof shared.sanitize !== 'function') return null;
    if (shared.isSupported === false) return null;
    if (!_purifiers.has(shared)) _purifiers.set(shared, buildPurifier(shared));
    return _purifiers.get(shared);
}

/**
 * Loudly report (once per page load per component) that rendering degraded.
 * @param {'sanitizer'|'markdown'} component - What is missing or failing
 * @param {string} [reason] - Why, for the console record (default: not loaded)
 * @param {*} [err] - Optional underlying error
 */
export function warnDegraded(component, reason, err) {
    const info = DEGRADED[component] || DEGRADED.sanitizer;
    if (_warned.has(component)) return;
    _warned.add(component);
    console.error(`[SANITIZE] ${reason || info.reason} — ${info.effect}.`, err || '');
    try {
        if (typeof document !== 'undefined') {
            toastError(info.toast);
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
        warnDegraded('sanitizer');
        return plainTextHtml(dirty);
    }
    try {
        const clean = purifier.sanitize(dirty, PURIFY_CONFIG);
        return typeof clean === 'string' ? clean : plainTextHtml(dirty);
    } catch (e) {
        warnDegraded('sanitizer', 'DOMPurify.sanitize threw', e);
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
