/**
 * markdown-renderer.js
 * Markdown parsing, HTML rendering, syntax highlighting, and code utilities
 *
 * Source: Portal/app.js lines 3507-3594, 3895-3994
 * Refactored: 2025-12-20
 *
 * Every function here that yields HTML for innerHTML returns SANITIZED markup
 * (via sanitize.js) and fails closed to escaped text when marked or DOMPurify
 * is missing or throws (audit P01-chat-core-web-02/03, X05-architecture-02).
 */

import { sanitizeAssistantHtml, isSanitizerAvailable, plainTextHtml } from './sanitize.js';

// =============================================================================
// Markdown Configuration
// =============================================================================

/**
 * Configure marked library with custom options
 * Sets up GFM, syntax highlighting, and other rendering options
 */
export function configureMarked() {
    if (typeof marked === 'undefined') return;

    marked.setOptions({
        breaks: true,
        gfm: true,
        headerIds: false,
        mangle: false,
        sanitize: false,
        highlight: function(code, lang) {
            if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
                try {
                    return hljs.highlight(code, { language: lang }).value;
                } catch (e) {
                    console.warn('Highlight error:', e);
                }
            }
            return code;
        }
    });
}

// =============================================================================
// Media URL Detection and Rendering
// =============================================================================

/**
 * Media file extensions by type
 */
const MEDIA_EXTENSIONS = {
    image: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'],
    video: ['.mp4', '.webm', '.mov', '.avi', '.mkv'],
    audio: ['.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac']
};

/**
 * Detect media type from URL/filename
 * @param {string} url - URL or filename
 * @returns {string|null} Media type (image, video, audio) or null
 */
function detectMediaType(url) {
    const lower = url.toLowerCase();
    for (const [type, exts] of Object.entries(MEDIA_EXTENSIONS)) {
        if (exts.some(ext => lower.endsWith(ext))) {
            return type;
        }
    }
    return null;
}

/**
 * Convert media URLs in text to HTML elements
 * Detects /ui/uploads/... URLs and converts them to img/video/audio tags
 *
 * Models can display media by outputting URLs like:
 *   /ui/uploads/image.png
 *   http://localhost:9091/ui/uploads/video.mp4
 *
 * @param {string} text - Text containing media URLs
 * @returns {string} Text with URLs converted to HTML elements
 */
export function convertMediaUrlsToHtml(text) {
    if (!text) return text;

    // Pattern to match media URLs:
    // - /ui/uploads/filename.ext
    // - http://localhost:9091/ui/uploads/filename.ext
    // - https://hostname/ui/uploads/filename.ext
    const urlPattern = /((?:https?:\/\/[^\s\/]+)?\/ui\/uploads\/[^\s<>"']+\.(?:png|jpg|jpeg|gif|webp|bmp|svg|mp4|webm|mov|avi|mkv|mp3|wav|ogg|m4a|flac|aac))/gi;

    return text.replace(urlPattern, (match) => {
        const mediaType = detectMediaType(match);

        // Clean the URL - ensure it starts with / for relative or http for absolute
        let cleanUrl = match.trim();

        // Extract just the filename for display
        const filename = cleanUrl.split('/').pop();

        switch (mediaType) {
            case 'image':
                return `\n<div class="media-preview media-image">
                    <img src="${cleanUrl}" alt="${filename}" loading="lazy">
                    <div class="media-controls-row">
                        <a href="${cleanUrl}" download="${filename}" class="media-download-btn" title="Download Image">💾 Download</a>
                    </div>
                    <div class="media-caption">${cleanUrl}</div>
                </div>\n`;

            case 'video':
                return `\n<div class="media-preview media-video">
                    <video controls preload="metadata">
                        <source src="${cleanUrl}" type="video/${cleanUrl.split('.').pop()}">
                        Your browser does not support video playback.
                    </video>
                    <div class="media-controls-row">
                        <a href="${cleanUrl}" download="${filename}" class="media-download-btn" title="Download Video">💾 Download</a>
                    </div>
                    <div class="media-caption">${cleanUrl}</div>
                </div>\n`;

            case 'audio':
                return `\n<div class="media-preview media-audio">
                    <audio controls preload="metadata">
                        <source src="${cleanUrl}" type="audio/${cleanUrl.split('.').pop() === 'mp3' ? 'mpeg' : cleanUrl.split('.').pop()}">
                        Your browser does not support audio playback.
                    </audio>
                    <div class="media-caption">${filename}</div>
                </div>\n`;

            default:
                return match; // Return unchanged if not a recognized media type
        }
    });
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
 * @param {string} text - Raw assistant text (may contain markdown + JSON)
 * @returns {string} text with qualifying JSON runs fenced
 */
export function fenceUnfencedJson(text) {
    if (!text || typeof text !== 'string') return text;

    let out = '';
    let proseStart = 0;       // start of the not-yet-emitted prose run
    let i = 0;
    const n = text.length;

    while (i < n) {
        const c = text[i];
        // Candidate must begin at the start of a line (after optional whitespace),
        // and must not already be inside a ``` fenced region.
        if ((c === '{' || c === '[') && atLineStart(text, i) && !insideFence(text, i)) {
            const end = jsonRunEnd(text, i);   // exclusive end, or -1 if unbalanced
            if (end > i) {
                const candidate = text.substring(i, end);
                const multiLine = candidate.indexOf('\n') !== -1;
                if ((multiLine || candidate.length >= MIN_INLINE_JSON_CHARS) && parsesAsJson(candidate)) {
                    // Emit prose preceding this blob verbatim, then the fenced JSON.
                    out += text.substring(proseStart, i);
                    out += '\n```json\n' + candidate + '\n```\n';
                    i = end;
                    proseStart = end;
                    continue;
                }
            }
        }
        i++;
    }

    // Trailing prose (or the whole string if nothing matched).
    out += text.substring(proseStart);
    return out;
}

/** True if [index] is the first non-whitespace char on its line. */
function atLineStart(s, index) {
    for (let j = index - 1; j >= 0; j--) {
        const ch = s[j];
        if (ch === '\n') return true;
        if (ch !== ' ' && ch !== '\t') return false;
    }
    return true;   // start of string
}

/** True if the char at [index] sits inside a ``` fenced region. Counts fence
 *  markers (lines whose first non-whitespace is ```) before [index]; an odd
 *  count means we are currently open inside a fence. */
function insideFence(s, index) {
    let open = false;
    let lineStart = 0;
    for (let k = 0; k < index; k++) {
        if (s[k] === '\n') {
            lineStart = k + 1;
        } else if (isFenceMarkerAt(s, lineStart, k)) {
            open = !open;
            // Skip to end of this line so we don't re-count the same marker.
            while (k < index && s[k] !== '\n') k++;
            lineStart = k + 1;
        }
    }
    return open;
}

/** True if position [k] is the start of a ``` marker for the line beginning at
 *  [lineStart] (i.e. only whitespace precedes it on the line and ``` follows). */
function isFenceMarkerAt(s, lineStart, k) {
    // k must be the first non-whitespace char of its line.
    for (let j = lineStart; j < k; j++) {
        if (s[j] !== ' ' && s[j] !== '\t') return false;
    }
    return s[k] === '`' && s[k + 1] === '`' && s[k + 2] === '`';
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
 * Convert markdown text to SANITIZED HTML with marked (no pre-processing).
 * Used directly by the CLI-agent renderers. Fails closed: when marked or
 * DOMPurify is unavailable, or either throws, the markdown SOURCE comes back
 * escaped (never unsanitized marked output).
 * @param {string} text - Markdown text to render
 * @returns {string} Safe HTML string
 */
export function markdownToSafeHtml(text) {
    const source = (text === null || text === undefined) ? '' : String(text);
    if (typeof marked === 'undefined' || typeof marked.parse !== 'function' || !isSanitizerAvailable()) {
        return plainTextHtml(source);
    }
    try {
        return sanitizeAssistantHtml(marked.parse(source));
    } catch (e) {
        console.warn('Markdown parse error:', e);
        return plainTextHtml(source);
    }
}

/**
 * Convert markdown text to HTML using marked library
 * Also converts media URLs to embedded elements
 * @param {string} text - Markdown text to render
 * @returns {string} Safe HTML string (fails closed to escaped text)
 */
export function renderMarkdown(text) {
    const source = (text === null || text === undefined) ? '' : String(text);
    try {
        // Wrap unfenced JSON blobs in a ```json fence so they render via the
        // clean code-block path instead of being mangled by the markdown parser.
        const fenced = fenceUnfencedJson(source);
        // Then convert any media URLs to HTML elements
        const textWithMedia = convertMediaUrlsToHtml(fenced);
        if (typeof marked === 'undefined' || typeof marked.parse !== 'function' || !isSanitizerAvailable()) {
            return plainTextHtml(source);
        }
        return sanitizeAssistantHtml(marked.parse(textWithMedia));
    } catch (e) {
        console.warn('Markdown parse error:', e);
        return plainTextHtml(source);
    }
}

// =============================================================================
// Code Block Utilities
// =============================================================================

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
