/**
 * markdown-renderer.js
 * Markdown parsing, HTML rendering, syntax highlighting, and code utilities
 *
 * Source: Portal/app.js lines 3507-3594, 3895-3994
 * Refactored: 2025-12-20
 */

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
 * Convert markdown text to HTML using marked library
 * Also converts media URLs to embedded elements
 * @param {string} text - Markdown text to render
 * @returns {string} HTML string
 */
export function renderMarkdown(text) {
    if (typeof marked === 'undefined') return text;
    try {
        // Wrap unfenced JSON blobs in a ```json fence so they render via the
        // clean code-block path instead of being mangled by the markdown parser.
        const fenced = fenceUnfencedJson(text);
        // Then convert any media URLs to HTML elements
        const textWithMedia = convertMediaUrlsToHtml(fenced);
        const html = marked.parse(textWithMedia);
        return typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(html) : html;
    } catch (e) {
        console.warn('Markdown parse error:', e);
        return text;
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
 * Apply syntax highlighting to special patterns in text
 * Highlights: citations, quotes, brackets, keywords, labels, numbers
 * @param {HTMLElement} element - Element to process
 */
export function applySyntaxHighlighting(element) {
    console.log('[SYNTAX] applySyntaxHighlighting called on element:', element?.tagName, element?.className);

    // Get all text nodes
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, null, false);
    const textNodes = [];
    let node;

    while (node = walker.nextNode()) {
        // Skip code blocks and pre elements
        let parent = node.parentElement;
        if (parent && (parent.tagName === 'CODE' || parent.tagName === 'PRE' || parent.closest('pre'))) {
            continue;
        }
        textNodes.push(node);
    }

    console.log('[SYNTAX] Found', textNodes.length, 'text nodes to process');

    textNodes.forEach(textNode => {
        let text = textNode.textContent;
        let html = text;
        let hasHighlight = false;

        // Highlight citations [1], [2], etc.
        html = html.replace(/\[(\d+)\]/g, (match) => {
            hasHighlight = true;
            return `<span data-highlight="citation">${match}</span>`;
        });

        // Highlight quoted text - double quotes "text" only
        html = html.replace(/"([^"]*)"/g, (match, content) => {
            hasHighlight = true;
            return `<span data-highlight="quote-mark">"</span><span data-highlight="quoted-text">${content}</span><span data-highlight="quote-mark">"</span>`;
        });

        // Highlight square brackets [] with content (but not citations)
        html = html.replace(/\[([^\]]+)\]/g, (match, content) => {
            if (/^\d+$/.test(content)) {
                return match;
            }
            hasHighlight = true;
            return `<span data-highlight="square-bracket">[</span><span data-highlight="bracket-text">${content}</span><span data-highlight="square-bracket">]</span>`;
        });

        // Highlight round brackets/parentheses () with content
        html = html.replace(/\(([^)]+)\)/g, (match, content) => {
            hasHighlight = true;
            return `<span data-highlight="round-bracket">(</span><span data-highlight="bracket-text">${content}</span><span data-highlight="round-bracket">)</span>`;
        });

        // Highlight keywords
        html = html.replace(/\b(checkpoint|operator|snapshot|context|fossil|manifest|volume)\b/gi, (match) => {
            hasHighlight = true;
            return `<span data-highlight="keyword">${match}</span>`;
        });

        // Highlight labels (text before : or ;)
        html = html.replace(/(\w+(?:\s+\w+)*)\s*([;:])/g, (match, label, punctuation) => {
            hasHighlight = true;
            return `<span data-highlight="label">${label}${punctuation}</span>`;
        });

        // Highlight numbered list items
        html = html.replace(/(^|\n)(\s*)(\d+)\./gm, (match, start, spaces, number) => {
            hasHighlight = true;
            return `${start}${spaces}<span data-highlight="label">${number}.</span>`;
        });

        // Highlight all numbers
        html = html.replace(/([\d,]+\.?\d*)/g, (match) => {
            if (html.indexOf(`>${match}<`) !== -1 && html.indexOf(`data-highlight="label">${match}`) !== -1) {
                return match;
            }
            if (/\d/.test(match)) {
                hasHighlight = true;
                return `<span data-highlight="number">${match}</span>`;
            }
            return match;
        });

        // Only replace if we found something to highlight
        if (hasHighlight) {
            const wrapper = document.createElement('span');
            wrapper.innerHTML = html;
            textNode.parentNode.replaceChild(wrapper, textNode);
        }
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
    const html = renderMarkdown(text);
    if (container) {
        container.innerHTML = html;
        addCopyButtonsToCodeBlocks(container);
        applySyntaxHighlighting(container);
    }
    return html;
}
