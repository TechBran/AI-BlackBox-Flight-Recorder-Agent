/**
 * markdown-renderer.js
 * Markdown parsing, HTML rendering, syntax highlighting, and code utilities
 *
 * Source: Portal/app.js lines 3507-3594, 3895-3994
 * Refactored: 2025-12-20
 *
 * Every function here that yields HTML for innerHTML returns SANITIZED markup
 * (via sanitize.js) and fails closed to escaped text when DOMPurify is missing
 * or a library throws; without marked the text is still sanitized, never raw
 * (audit P01-chat-core-web-02/03, X05-architecture-02).
 */

import { sanitizeAssistantHtml, isSanitizerAvailable, plainTextHtml, warnDegraded } from './sanitize.js';

// =============================================================================
// Markdown Configuration
// =============================================================================
//
// marked, marked-highlight and highlight.js are vendored (Portal/vendor/,
// loaded as classic scripts by index.html) and read lazily from globalThis.
// Every render goes through one of two PRIVATE marked instances over the
// loaded library, both with MARKED_OPTIONS:
//   - plain: the streaming renders. appendResponseContent and the agent
//     handlers re-render the whole reply once per animation frame while tokens
//     arrive, so they never run highlight.js;
//   - highlighted: every other (final) render. marked-highlight runs
//     highlight.js at PARSE time, once per code block.
// This replaces the old post-render hljs.highlightElement sweeps on timers
// (audit P01-chat-core-web-13). Sanitizing stays sanitize.js's job: the
// highlighted HTML still goes through sanitizeAssistantHtml.

/** marked options for every render: GFM, single newlines as <br>. */
const MARKED_OPTIONS = Object.freeze({ breaks: true, gfm: true });

/**
 * Configure the global marked instance (GFM, single newlines as <br>), for
 * any caller outside this module; the Portal's own renders use the private
 * instances below, which carry the same options.
 *
 * Code highlighting is NOT a marked option: marked removed `highlight`,
 * `headerIds`, `mangle` and `sanitize` in v8 and silently ignores them, so they
 * are not passed (audit X04-security-clients-infra-08).
 */
export function configureMarked() {
    if (!isMarkedAvailable()) return;
    marked.setOptions({ ...MARKED_OPTIONS });
}

/**
 * highlight.js callback for marked-highlight. A fence labeled with a language
 * highlight.js knows is highlighted as that language; an unlabeled fence is
 * auto-detected (what hljs.highlightElement did); `nohighlight` or a label
 * highlight.js lacks comes back unchanged, which marked then escapes.
 * @param {string} code - The fence's text (raw)
 * @param {string} lang - The first word of its info string ('' when none)
 * @returns {string} Highlighted HTML, or `code` unchanged
 */
function highlightCode(code, lang) {
    const hl = typeof hljs !== 'undefined' ? hljs : null;
    if (!hl || typeof hl.highlight !== 'function') return code;
    try {
        if (!lang) return hl.highlightAuto(code).value;
        if (hl.getLanguage(lang)) return hl.highlight(code, { language: lang, ignoreIllegals: true }).value;
    } catch (e) {
        console.warn('Code highlighting failed:', e);
    }
    return code;
}

// One {plain, highlighted} pair per loaded marked library.
const _markedInstances = new WeakMap();

/**
 * The marked instance for a render, or null when marked is not loaded.
 * Falls back to the global object itself when the loaded library has no
 * Marked constructor (and to the plain instance without marked-highlight).
 * @param {boolean} streaming - True for a mid-stream render
 * @returns {{parse: Function}|null}
 */
function markedFor(streaming) {
    if (!isMarkedAvailable()) return null;
    const lib = marked;
    let pair = _markedInstances.get(lib);
    if (!pair) {
        pair = { plain: lib, highlighted: lib };
        try {
            if (typeof lib.Marked === 'function') {
                pair.plain = new lib.Marked({ ...MARKED_OPTIONS });
                pair.highlighted = pair.plain;
                const ext = typeof markedHighlight !== 'undefined' && markedHighlight
                    ? markedHighlight.markedHighlight : null;
                if (typeof ext === 'function') {
                    pair.highlighted = new lib.Marked({ ...MARKED_OPTIONS }, ext({
                        langPrefix: 'hljs language-',
                        emptyLangClass: 'hljs',
                        highlight: highlightCode,
                    }));
                }
            }
        } catch (e) {
            console.warn('marked instances could not be created:', e);
        }
        _markedInstances.set(lib, pair);
    }
    return streaming ? pair.plain : pair.highlighted;
}

// =============================================================================
// Media URL Upgrade (post-sanitize DOM pass)
// =============================================================================
//
// Models show generated media by writing its URL (/ui/uploads/x.png, or an
// absolute http(s)://host/ui/uploads/... one), as a bare URL, a link or the
// markdown image form. This used to be a string rewrite BEFORE marked.parse,
// which split ![alt](url) across lines (the reply showed a literal "![alt]("
// around the player) and printed the raw <div ...> inside inline code and
// ``` fences (audit P01-chat-core-web-12). The upgrade now runs on the
// SANITIZED output: it walks the parsed DOM, turns <img>/<a> elements and text
// URLs into players, skips code, and builds every node with createElement and
// setAttribute -- no URL is ever spliced into markup. When it changed anything
// the result goes through sanitizeAssistantHtml once more, so the sanitizer
// stays the last word on every string renderMarkdown returns.

const MEDIA_EXT = 'png|jpg|jpeg|gif|webp|bmp|svg|mp4|webm|mov|avi|mkv|mp3|wav|ogg|m4a|flac|aac';

/** One media URL: /ui/uploads/... or http(s)://host/ui/uploads/..., ending in a media extension. */
const MEDIA_URL_SOURCE = `(?:https?:\\/\\/[^\\s\\/<>"']+)?\\/ui\\/uploads\\/[^\\s<>"']+?\\.(?:${MEDIA_EXT})(?![\\w\\/-]|\\.\\w)`;
const MEDIA_URL_IN_TEXT = new RegExp(MEDIA_URL_SOURCE, 'gi');
const MEDIA_URL_EXACT = new RegExp(`^${MEDIA_URL_SOURCE}$`, 'i');

const MEDIA_TYPES = {
    image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'],
    video: ['mp4', 'webm', 'mov', 'avi', 'mkv'],
    audio: ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac'],
};

/** Text in these never becomes a player (code stays code; links/players stay what they are). */
const SKIP_TEXT_IN = 'a, code, pre, kbd, samp, button, textarea, audio, video, .media-preview';

/**
 * Detect media type from URL/filename
 * @param {string} url - URL or filename
 * @returns {string|null} Media type (image, video, audio) or null
 */
function detectMediaType(url) {
    const ext = url.split('.').pop().toLowerCase();
    for (const [type, exts] of Object.entries(MEDIA_TYPES)) {
        if (exts.includes(ext)) return type;
    }
    return null;
}

/**
 * Build the media-preview block for one URL (DOM APIs only).
 * @param {Document} doc - Owner document of the fragment being upgraded
 * @param {string} url - A MEDIA_URL_EXACT match
 * @param {string} [alt] - Alt text for an image (defaults to the filename)
 * @returns {HTMLElement|null}
 */
function buildMediaPreview(doc, url, alt) {
    const type = detectMediaType(url);
    if (!type) return null;
    const filename = url.split('/').pop();
    const ext = filename.split('.').pop().toLowerCase();
    const el = (tag, attrs = {}, text) => {
        const node = doc.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
        if (text !== undefined) node.textContent = text;
        return node;
    };
    const downloadRow = (title) => {
        const row = el('div', { class: 'media-controls-row' });
        row.appendChild(el('a', { href: url, download: filename, class: 'media-download-btn', title }, '💾 Download'));
        return row;
    };

    const preview = el('div', { class: `media-preview media-${type}` });
    if (type === 'image') {
        preview.appendChild(el('img', { src: url, alt: alt || filename, loading: 'lazy' }));
        preview.appendChild(downloadRow('Download Image'));
        preview.appendChild(el('div', { class: 'media-caption' }, url));
    } else if (type === 'video') {
        const video = el('video', { controls: '', preload: 'metadata' });
        video.appendChild(el('source', { src: url, type: `video/${ext}` }));
        video.appendChild(doc.createTextNode('Your browser does not support video playback.'));
        preview.appendChild(video);
        preview.appendChild(downloadRow('Download Video'));
        preview.appendChild(el('div', { class: 'media-caption' }, url));
    } else {
        const audio = el('audio', { controls: '', preload: 'metadata' });
        audio.appendChild(el('source', { src: url, type: `audio/${ext === 'mp3' ? 'mpeg' : ext}` }));
        audio.appendChild(doc.createTextNode('Your browser does not support audio playback.'));
        preview.appendChild(audio);
        preview.appendChild(el('div', { class: 'media-caption' }, filename));
    }
    return preview;
}

/** True when a node holds nothing a reader would see. */
function isBlank(node) {
    if (!node) return true;
    if ((node.textContent || '').trim() !== '') return false;
    return !(node.querySelector && node.querySelector('img, video, audio, picture, svg, canvas'));
}

/**
 * A <div> inside a <p> does not survive serialization (the HTML parser closes
 * the <p> before it), so move a preview out of its paragraph: the paragraph is
 * split around it, inline wrappers included (Range.extractContents clones the
 * partially selected ancestors).
 * @param {HTMLElement} preview
 */
function hoistOutOfParagraph(preview) {
    const p = preview.parentElement ? preview.parentElement.closest('p') : null;
    if (!p) return;
    const doc = preview.ownerDocument;
    const range = doc.createRange();
    range.setStartAfter(preview);
    range.setEnd(p, p.childNodes.length);
    const after = range.extractContents();
    preview.remove();
    p.after(preview);
    if (!isBlank(after)) {
        const tail = doc.createElement('p');
        tail.appendChild(after);
        preview.after(tail);
    }
    if (isBlank(p)) p.remove();
}

/**
 * Sanitize HTML, then upgrade its media URLs to players (see the section comment).
 * @param {string} dirty - Untrusted HTML (marked output)
 * @returns {string} Safe HTML
 */
export function sanitizeWithMediaPlayers(dirty) {
    const source = (dirty === null || dirty === undefined) ? '' : String(dirty);
    if (source.indexOf('/ui/uploads/') === -1 || typeof document === 'undefined') {
        return sanitizeAssistantHtml(source);
    }
    const template = document.createElement('template');   // inert: no loads, no handlers
    template.innerHTML = sanitizeAssistantHtml(source);
    const root = template.content;
    const doc = root.ownerDocument;
    const previews = [];
    const inPreview = (node) => !!(node.parentElement && node.parentElement.closest('.media-preview'));

    // Links first: [text](url) and autolinked absolute URLs (an <a> may wrap an <img>).
    for (const a of Array.from(root.querySelectorAll('a[href]'))) {
        const href = (a.getAttribute('href') || '').trim();
        if (a.hasAttribute('class') || inPreview(a) || !MEDIA_URL_EXACT.test(href)) continue;
        const img = a.querySelector('img');
        const preview = buildMediaPreview(doc, href, img ? img.getAttribute('alt') : '');
        if (!preview) continue;
        a.replaceWith(preview);
        previews.push(preview);
    }

    // The markdown image form. Server-built images carry a class
    // (generated-image, inline-media-image...) and are left exactly as they are.
    for (const img of Array.from(root.querySelectorAll('img[src]'))) {
        const src = (img.getAttribute('src') || '').trim();
        if (img.hasAttribute('class') || inPreview(img) || !MEDIA_URL_EXACT.test(src)) continue;
        const preview = buildMediaPreview(doc, src, img.getAttribute('alt') || '');
        if (!preview) continue;
        img.replaceWith(preview);
        previews.push(preview);
    }

    // Bare URLs in text.
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.nodeValue.indexOf('/ui/uploads/') === -1) continue;
        if (node.parentElement && node.parentElement.closest(SKIP_TEXT_IN)) continue;
        textNodes.push(node);
    }
    for (const node of textNodes) {
        const text = node.nodeValue;
        const frag = doc.createDocumentFragment();
        let last = 0;
        let changed = false;
        MEDIA_URL_IN_TEXT.lastIndex = 0;
        let m;
        while ((m = MEDIA_URL_IN_TEXT.exec(text)) !== null) {
            const preview = buildMediaPreview(doc, m[0]);
            if (!preview) continue;
            if (m.index > last) frag.appendChild(doc.createTextNode(text.slice(last, m.index)));
            frag.appendChild(preview);
            previews.push(preview);
            last = m.index + m[0].length;
            changed = true;
        }
        if (!changed) continue;
        if (last < text.length) frag.appendChild(doc.createTextNode(text.slice(last)));
        node.replaceWith(frag);
    }

    if (!previews.length) return template.innerHTML;
    previews.forEach(hoistOutOfParagraph);
    return sanitizeAssistantHtml(template.innerHTML);
}

// =============================================================================
// Markdown Rendering
// =============================================================================

// =============================================================================
// Unfenced JSON Fencing (Phase A2, reply-parsing-and-rendering-hardening)
// =============================================================================
//
// Some models narrate tool transcripts / dump structured results inline as prose
// (e.g. {"results":[...]} or [ {...}, {...} ]). Fed to the markdown parser, the
// JSON's markdown-significant chars (_ [] ** #) render as a mangled mess. Before
// marked.parse we wrap any large/multi-line, *parse-validated* JSON run in a
// ```json fence so it renders via the existing clean code-block path. Conservative
// by design: a run is only fenced if it begins at a line start with { or [, is
// multi-line OR >= 80 chars, brace/bracket-balances, and actually parses as JSON.
// Anything else is left as prose -- so stray inline braces, [links](url),
// table rows, citations [1], and short {x} tokens are untouched. Runs already
// inside a ``` fenced region are skipped (no double-fencing). A partial/unbalanced
// blob (mid-stream) is left unchanged (no fence, no throw) -- streaming-safe.

const MIN_INLINE_JSON_CHARS = 80;

/**
 * Wrap any qualifying unfenced JSON run in a ```json fence.
 * Pure function: returns the input unchanged when no run qualifies.
 *
 * One forward pass over the lines, carrying the ``` fence state along. (The
 * old version rescanned the text from index 0 for every line-start brace to
 * learn whether it sat inside a fence -- O(n^2) per call, and renderMarkdown
 * runs on every streamed token; audit P01-chat-core-web-11.) A candidate is
 * the first non-whitespace char of a line, outside a fence; a consumed JSON
 * run cannot contain a fence marker (JSON has no raw newlines in strings and
 * no ``` token), so scanning resumes at the line after it.
 * @param {string} text - Raw assistant text (may contain markdown + JSON)
 * @returns {string} text with qualifying JSON runs fenced
 */
export function fenceUnfencedJson(text) {
    if (!text || typeof text !== 'string') return text;

    let out = '';
    let proseStart = 0;       // start of the not-yet-emitted prose run
    let inFence = false;
    let lineStart = 0;
    const n = text.length;

    while (lineStart < n) {
        // First non-whitespace char of this line.
        let k = lineStart;
        while (k < n && (text[k] === ' ' || text[k] === '\t')) k++;
        const c = text[k];
        let resumeAt = k;

        if (c === '`' && text[k + 1] === '`' && text[k + 2] === '`') {
            inFence = !inFence;
        } else if (!inFence && (c === '{' || c === '[')) {
            const end = jsonRunEnd(text, k);   // exclusive end, or -1 if unbalanced
            if (end > k) {
                const candidate = text.substring(k, end);
                const multiLine = candidate.indexOf('\n') !== -1;
                if ((multiLine || candidate.length >= MIN_INLINE_JSON_CHARS) && parsesAsJson(candidate)) {
                    // Emit prose preceding this blob verbatim, then the fenced JSON.
                    out += text.substring(proseStart, k);
                    out += '\n```json\n' + candidate + '\n```\n';
                    proseStart = end;
                    resumeAt = end;
                }
            }
        }

        // Next line (the rest of this one can hold no candidate).
        const nl = text.indexOf('\n', resumeAt);
        if (nl === -1) break;
        lineStart = nl + 1;
    }

    // Trailing prose (or the whole string if nothing matched).
    out += text.substring(proseStart);
    return out;
}

/**
 * Brace/bracket-balance starting at the opening char at [start] ({ or [).
 * Tracks string literals + escapes so braces inside JSON strings don't miscount.
 * Returns the exclusive index just past the matching close, or -1 if unbalanced.
 */
function jsonRunEnd(s, start) {
    let depth = 0;
    let inStr = false;
    let escaped = false;
    const n = s.length;
    for (let i = start; i < n; i++) {
        const c = s[i];
        if (inStr) {
            if (escaped) {
                escaped = false;
            } else if (c === '\\') {
                escaped = true;
            } else if (c === '"') {
                inStr = false;
            }
        } else {
            if (c === '"') {
                inStr = true;
            } else if (c === '{' || c === '[') {
                depth++;
            } else if (c === '}' || c === ']') {
                depth--;
                if (depth === 0) return i + 1;
                if (depth < 0) return -1;
            }
        }
    }
    return -1;   // never balanced
}

/** Parse-gate: only treat the candidate as JSON if it actually parses. */
function parsesAsJson(candidate) {
    try {
        JSON.parse(candidate);
        return true;
    } catch (e) {
        return false;
    }
}

/**
 * True when marked is loaded and usable.
 * @returns {boolean}
 */
function isMarkedAvailable() {
    return typeof marked !== 'undefined' && marked !== null && typeof marked.parse === 'function';
}

/**
 * The no-marked rendering of model text: newlines outside tags become <br>,
 * and the result is SANITIZED (never raw). marked is vendored, but a failed or
 * blocked script load can still lose it while DOMPurify is present; server-
 * built markup (media placeholders the pollers find by class, artifact
 * download links) then still renders, only markdown syntax shows as typed.
 * @param {string} text - Model text (markdown and/or HTML)
 * @returns {string} Safe HTML string
 */
function renderWithoutMarked(text) {
    warnDegraded('markdown');
    return sanitizeAssistantHtml(newlinesToBreaks(text));
}

/** Newlines outside tags become <br> (the no-marked line-break rendering). */
function newlinesToBreaks(text) {
    return text.replace(/(<[^>]*>)|\r?\n/g, (match, tag) => tag || '<br>');
}

/**
 * Convert markdown text to SANITIZED HTML with marked (no pre-processing).
 * Used directly by the CLI-agent renderers. Fails closed: when DOMPurify is
 * unavailable, or marked throws, the markdown SOURCE comes back escaped
 * (never unsanitized marked output); without marked it is sanitized as-is.
 * @param {string} text - Markdown text to render
 * @param {{streaming?: boolean}} [options] - streaming: a mid-stream render
 *   (code is highlighted only on the final, non-streaming one)
 * @returns {string} Safe HTML string
 */
export function markdownToSafeHtml(text, { streaming = false } = {}) {
    const source = (text === null || text === undefined) ? '' : String(text);
    if (!isSanitizerAvailable()) {
        warnDegraded('sanitizer');
        return plainTextHtml(source);
    }
    if (!isMarkedAvailable()) return renderWithoutMarked(source);
    try {
        return sanitizeAssistantHtml(markedFor(streaming).parse(source));
    } catch (e) {
        console.warn('Markdown parse error:', e);
        return plainTextHtml(source);
    }
}

/**
 * Convert markdown text to HTML using marked library
 * Also upgrades media URLs to embedded players (upgradeMediaUrls, after the
 * sanitizer: the URL is never spliced into markdown or markup)
 * @param {string} text - Markdown text to render
 * @param {{streaming?: boolean}} [options] - streaming: a mid-stream render,
 *   repeated per animation frame; code is highlighted only when it is false
 * @returns {string} Safe HTML string (fails closed to escaped text)
 */
export function renderMarkdown(text, { streaming = false } = {}) {
    const source = (text === null || text === undefined) ? '' : String(text);
    if (!isSanitizerAvailable()) {
        warnDegraded('sanitizer');
        return plainTextHtml(source);
    }
    try {
        // Wrap unfenced JSON blobs in a ```json fence so they render via the
        // clean code-block path instead of being mangled by the markdown parser.
        const fenced = fenceUnfencedJson(source);
        if (!isMarkedAvailable()) {
            warnDegraded('markdown');
            return sanitizeWithMediaPlayers(newlinesToBreaks(fenced));
        }
        return sanitizeWithMediaPlayers(markedFor(streaming).parse(fenced));
    } catch (e) {
        console.warn('Markdown parse error:', e);
        return plainTextHtml(source);
    }
}

/**
 * Wrap arbitrary text in a markdown inline-code span it cannot escape.
 *
 * marked HTML-escapes code-span text itself, so the text goes in RAW (escaping
 * it first shows literal &#39; / &quot;). The fence is one backtick longer than
 * the longest backtick run in the text, so the text can never close the span,
 * and newlines become spaces (a code span renders them as spaces anyway) so the
 * text cannot end the surrounding block line.
 * @param {*} text - Any value
 * @returns {string} Markdown source for one code span
 */
export function markdownCodeSpan(text) {
    const flat = String(text ?? '').replace(/\r?\n/g, ' ');
    const longestRun = (flat.match(/`+/g) || []).reduce((max, run) => Math.max(max, run.length), 0);
    const fence = '`'.repeat(longestRun + 1);
    // A span may not start or end with a backtick, and one space is stripped
    // from both ends when both are present: pad in exactly those cases.
    const pad = (/^`|`$/.test(flat) || /^ .* $/.test(flat)) ? ' ' : '';
    return `${fence}${pad}${flat}${pad}${fence}`;
}

// =============================================================================
// Code Block Utilities
// =============================================================================

/**
 * Highlight the `pre code` blocks in a container that are not highlighted
 * yet: markup that did not come through a final renderMarkdown /
 * markdownToSafeHtml (stored pre-rendered replies), whose blocks carry no
 * `hljs` class. Blocks those renders emit are highlighted at parse time and
 * are skipped, and so is a block labeled with a language highlight.js lacks
 * (highlightElement throws on it). highlightElement highlights the element's
 * TEXT, so it never turns sanitized text back into markup.
 * @param {HTMLElement} container - Container element with code blocks
 */
export function highlightCodeBlocks(container) {
    if (!container || typeof hljs === 'undefined' || !hljs || typeof hljs.highlightElement !== 'function') return;
    container.querySelectorAll('pre code').forEach(block => {
        if (block.classList.contains('hljs')) return;
        const langClass = Array.from(block.classList).find(c => c.startsWith('language-'));
        const lang = langClass ? langClass.slice('language-'.length) : '';
        if (lang && !hljs.getLanguage(lang)) return;
        try {
            hljs.highlightElement(block);
        } catch (e) {
            console.warn('Code highlighting failed:', e);
        }
    });
}

/**
 * Add copy buttons to all code blocks in a container
 * @param {HTMLElement} container - Container element with code blocks
 */
export function addCopyButtonsToCodeBlocks(container) {
    if (!container) return;

    const codeBlocks = container.querySelectorAll('pre code');
    codeBlocks.forEach(codeEl => {
        const pre = codeEl.parentElement;
        if (!pre) return;

        // Wrap in code-block-wrapper if not already wrapped
        if (!pre.parentElement || !pre.parentElement.classList.contains('code-block-wrapper')) {
            const wrapper = document.createElement('div');
            wrapper.className = 'code-block-wrapper';
            pre.parentNode.insertBefore(wrapper, pre);
            wrapper.appendChild(pre);
        }

        const wrapper = pre.parentElement;
        // Skip if header bar already exists
        if (wrapper.querySelector('.code-block-header')) return;

        // Detect language from class (hljs adds language-xxx)
        let lang = '';
        const langClass = Array.from(codeEl.classList).find(c => c.startsWith('language-'));
        if (langClass) lang = langClass.replace('language-', '');

        // Create header bar with language label + copy button
        const header = document.createElement('div');
        header.className = 'code-block-header';

        const langLabel = document.createElement('span');
        langLabel.className = 'code-block-lang';
        langLabel.textContent = lang || 'code';

        const btn = document.createElement('button');
        btn.className = 'code-copy-btn';
        btn.textContent = 'Copy';
        btn.onclick = (e) => {
            e.preventDefault();
            e.stopPropagation();
            const code = codeEl.textContent;
            copyToClipboard(code);
            btn.textContent = 'Copied!';
            btn.classList.add('copied');
            setTimeout(() => {
                btn.textContent = 'Copy';
                btn.classList.remove('copied');
            }, 2000);
        };

        header.appendChild(langLabel);
        header.appendChild(btn);
        wrapper.insertBefore(header, pre);
    });
}

/**
 * Copy text to clipboard
 * @param {string} text - Text to copy
 * @returns {boolean} Success status
 */
export function copyToClipboard(text) {
    // Try modern API first
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(err => {
            console.error('Clipboard API failed:', err);
            fallbackCopyToClipboard(text);
        });
        return true;
    }

    // Fallback for older browsers
    return fallbackCopyToClipboard(text);
}

/**
 * Fallback clipboard copy using textarea
 * @param {string} text - Text to copy
 * @returns {boolean} Success status
 */
function fallbackCopyToClipboard(text) {
    const el = document.createElement('textarea');
    el.value = text;
    el.setAttribute('readonly', '');
    el.style.position = 'absolute';
    el.style.left = '-9999px';
    document.body.appendChild(el);
    el.select();
    try {
        document.execCommand('copy');
        return true;
    } catch (e) {
        console.error("Copy failed:", e);
        return false;
    } finally {
        document.body.removeChild(el);
    }
}

// =============================================================================
// Timestamp Formatting
// =============================================================================

/**
 * Format a date as HH:MM timestamp
 * @param {Date} [date] - Date to format, defaults to now
 * @returns {string} Formatted timestamp
 */
export function formatTimestamp(date) {
    if (!date) date = new Date();
    const hours = date.getHours().toString().padStart(2, '0');
    const minutes = date.getMinutes().toString().padStart(2, '0');
    return `${hours}:${minutes}`;
}

/**
 * Add timestamp to a chat bubble
 * @param {HTMLElement} bubble - Bubble element
 */
export function addTimestampToBubble(bubble) {
    if (!bubble || bubble.querySelector('.bubble-timestamp')) return;

    const timestamp = document.createElement('span');
    timestamp.className = 'bubble-timestamp';
    timestamp.textContent = formatTimestamp();
    bubble.appendChild(timestamp);
}

// =============================================================================
// Syntax Highlighting
// =============================================================================

/**
 * Text-highlight rules, applied in order (citations, quotes, brackets,
 * keywords, labels, numbered items, numbers — same set and precedence as the
 * original string-replace chain). Text a rule does not match is handed to the
 * next rule; `inner: true` means the captured content is highlighted further.
 *
 * Built ONLY from text nodes and createElement + textContent: the old
 * implementation read each text node's DECODED textContent and assigned it to
 * innerHTML, which turned text DOMPurify had correctly left inert (e.g. an
 * escaped `&lt;img onerror=...&gt;`) back into live markup (audit
 * P01-chat-core-web-02, X04-security-clients-infra-07).
 */
const HIGHLIGHT_RULES = [
    { re: /\[(\d+)\]/g, kind: 'citation' },
    { re: /"([^"]*)"/g, open: '"', close: '"', mark: 'quote-mark', inner: true, innerKind: 'quoted-text' },
    { re: /\[([^\]]+)\]/g, open: '[', close: ']', mark: 'square-bracket', inner: true, innerKind: 'bracket-text',
      skip: (m) => /^\d+$/.test(m[1]) },
    { re: /\(([^)]+)\)/g, open: '(', close: ')', mark: 'round-bracket', inner: true, innerKind: 'bracket-text' },
    { re: /\b(checkpoint|operator|snapshot|context|fossil|manifest|volume)\b/gi, kind: 'keyword' },
    { re: /(\w+(?:\s+\w+)*)\s*([;:])/g, kind: 'label' },
    { re: /(^|\n)(\s*)(\d+)\./gm, kind: 'label', numbered: true },
    { re: /([\d,]+\.?\d*)/g, kind: 'number', skip: (m) => !/\d/.test(m[0]) },
];

/**
 * Create a highlight span holding plain text or already-built child nodes.
 * @param {Document} doc
 * @param {string} kind - data-highlight value
 * @param {string|Node[]} content
 * @returns {HTMLSpanElement}
 */
function highlightSpan(doc, kind, content) {
    const span = doc.createElement('span');
    span.setAttribute('data-highlight', kind);
    if (typeof content === 'string') {
        span.textContent = content;
    } else {
        content.forEach(n => span.appendChild(n));
    }
    return span;
}

/**
 * Split `text` (which starts at `offset` inside `full`) into text nodes and
 * highlight spans using HIGHLIGHT_RULES[ruleIndex..].
 * @returns {{nodes: Node[], changed: boolean}}
 */
function highlightSegment(doc, full, text, offset, ruleIndex) {
    if (!text) return { nodes: [], changed: false };
    if (ruleIndex >= HIGHLIGHT_RULES.length) {
        return { nodes: [doc.createTextNode(text)], changed: false };
    }

    const rule = HIGHLIGHT_RULES[ruleIndex];
    const re = new RegExp(rule.re.source, rule.re.flags);
    const nodes = [];
    let changed = false;
    let last = 0;

    const pushRest = (from, to) => {
        if (to <= from) return;
        const rest = highlightSegment(doc, full, text.slice(from, to), offset + from, ruleIndex + 1);
        nodes.push(...rest.nodes);
        changed = changed || rest.changed;
    };

    let m;
    while ((m = re.exec(text)) !== null) {
        if (m[0] === '') { re.lastIndex++; continue; }
        if (rule.skip && rule.skip(m)) continue;
        const start = m.index;
        let parts;
        if (rule.numbered) {
            // `^` must be a REAL line start of the original text node, not the
            // start of a slice left over from an earlier rule.
            const lineStart = m[1] === '\n' || offset + m.index === 0 || full[offset + m.index - 1] === '\n';
            if (!lineStart) continue;
            const lead = m[1] + m[2];
            pushRest(last, start);
            if (lead) nodes.push(doc.createTextNode(lead));
            parts = [highlightSpan(doc, rule.kind, `${m[3]}.`)];
        } else if (rule.inner) {
            pushRest(last, start);
            const innerStart = m.index + rule.open.length;
            const inner = highlightSegment(doc, full, m[1], offset + innerStart, ruleIndex + 1);
            parts = [
                highlightSpan(doc, rule.mark, rule.open),
                highlightSpan(doc, rule.innerKind, inner.nodes),
                highlightSpan(doc, rule.mark, rule.close),
            ];
        } else {
            pushRest(last, start);
            parts = [highlightSpan(doc, rule.kind, m[0])];
        }
        nodes.push(...parts);
        changed = true;
        last = m.index + m[0].length;
    }
    pushRest(last, text.length);
    return { nodes, changed };
}

/**
 * Apply syntax highlighting to special patterns in text
 * Highlights: citations, quotes, brackets, keywords, labels, numbers
 * Text inside code/pre and inside existing highlight spans is left alone; the
 * visible text is never changed, only wrapped in <span data-highlight=...>.
 * @param {HTMLElement} element - Element to process
 */
export function applySyntaxHighlighting(element) {
    if (!element) return;
    const doc = element.ownerDocument || document;

    // Get all text nodes
    const walker = doc.createTreeWalker(element, NodeFilter.SHOW_TEXT, null, false);
    const textNodes = [];
    let node;

    while (node = walker.nextNode()) {
        // Skip code blocks, pre elements and text that is already highlighted
        const parent = node.parentElement;
        if (parent && (parent.tagName === 'CODE' || parent.tagName === 'PRE' || parent.closest('pre') ||
                       parent.closest('[data-highlight]'))) {
            continue;
        }
        textNodes.push(node);
    }

    textNodes.forEach(textNode => {
        const text = textNode.textContent;
        const { nodes, changed } = highlightSegment(doc, text, text, 0, 0);
        // Only replace if we found something to highlight
        if (!changed || !textNode.parentNode) return;
        const fragment = doc.createDocumentFragment();
        nodes.forEach(n => fragment.appendChild(n));
        textNode.parentNode.replaceChild(fragment, textNode);
    });
}

// =============================================================================
// Combined Processing
// =============================================================================

/**
 * Process content with full rendering pipeline
 * @param {string} text - Raw text/markdown
 * @param {HTMLElement} container - Target container
 * @returns {string} Rendered HTML
 */
export function processContent(text, container) {
    if (!container) return renderMarkdown(text);
    container.innerHTML = renderMarkdown(text);
    addCopyButtonsToCodeBlocks(container);
    applySyntaxHighlighting(container);
    return container.innerHTML;
}
