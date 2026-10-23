// Grep-level scanners for Portal HTML sinks, shared by html-sinks-guard.test.mjs
// (stripComments also by client-literals-guard.test.mjs). Comment-aware (a small
// tokenizer for strings, templates and regex literals), so commented-out code and
// `//` inside URLs never confuse the guards.

/**
 * Strip // and /* *\/ comments. A small tokenizer that understands strings,
 * template literals (with nested ${...} expressions) and regex literals, so
 * a `//` inside a URL string or a quote inside a regex never derails it.
 * With `keepNewlines`, a block comment leaves its line breaks behind, so line
 * numbers in the result still match the source.
 */
export function stripComments(code, { keepNewlines = false } = {}) {
    let out = "";
    let i = 0;
    // Stack of contexts: "code" (with brace depth) or "template".
    const stack = [{ kind: "code", depth: 0 }];
    let lastSignificant = ""; // last non-space char emitted in code context
    const regexCanStart = () => lastSignificant === "" || /[(,=:[!&|?{};+\-*%<>~^]/.test(lastSignificant)
        || /\b(return|typeof|case|in|of|delete|void|throw|new)$/.test(out.slice(-12).trimEnd());
    while (i < code.length) {
        const top = stack[stack.length - 1];
        const c = code[i];
        const n = code[i + 1];
        if (top.kind === "template") {
            out += c;
            if (c === "\\") { out += n ?? ""; i += 2; continue; }
            if (c === "`") { stack.pop(); lastSignificant = "`"; i++; continue; }
            if (c === "$" && n === "{") { out += "{"; stack.push({ kind: "code", depth: 0, inTemplate: true }); i += 2; lastSignificant = "{"; continue; }
            i++;
            continue;
        }
        // code context
        if (c === "/" && n === "/") { while (i < code.length && code[i] !== "\n") i++; continue; }
        if (c === "/" && n === "*") {
            const end = code.indexOf("*/", i + 2);
            const stop = end < 0 ? code.length : end + 2;
            if (keepNewlines) out += code.slice(i, stop).replace(/[^\n]/g, "");
            i = stop;
            continue;
        }
        if (c === "'" || c === '"') {
            let j = i + 1;
            while (j < code.length && code[j] !== c && code[j] !== "\n") { if (code[j] === "\\") j++; j++; }
            out += code.slice(i, j + 1);
            i = j + 1;
            lastSignificant = c;
            continue;
        }
        if (c === "`") { out += c; stack.push({ kind: "template" }); i++; continue; }
        if (c === "/" && regexCanStart()) {
            let j = i + 1;
            let inClass = false;
            while (j < code.length && code[j] !== "\n") {
                if (code[j] === "\\") { j += 2; continue; }
                if (code[j] === "[") inClass = true;
                else if (code[j] === "]") inClass = false;
                else if (code[j] === "/" && !inClass) break;
                j++;
            }
            while (j + 1 < code.length && /[a-z]/i.test(code[j + 1])) j++;
            out += code.slice(i, j + 1);
            i = j + 1;
            lastSignificant = "/";
            continue;
        }
        if (c === "{") top.depth++;
        if (c === "}") {
            if (top.depth === 0 && top.inTemplate) { stack.pop(); out += c; i++; continue; }
            top.depth--;
        }
        out += c;
        if (!/\s/.test(c)) lastSignificant = c;
        i++;
    }
    return out;
}

// A statement continues past a newline when the next line starts with one of
// these (no ASI before a binary operator, member access, call, index or template).
const CONTINUES_EXPRESSION = /[+\-*/%?:|&.,=<>!^~([`]/;

/**
 * The right-hand side of each `.innerHTML =` / `+=` assignment, up to the end
 * of the statement (template literals and parentheses are balanced, and a
 * newline followed by an operator such as `+ more` continues the statement).
 */
export function innerHtmlAssignments(code) {
    const results = [];
    const re = /\.innerHTML\s*(\+?=)(?!=)\s*/g;
    let m;
    while ((m = re.exec(code)) !== null) {
        let i = re.lastIndex;
        let depth = 0;
        let quote = null;
        const start = i;
        for (; i < code.length; i++) {
            const c = code[i];
            if (quote) {
                if (c === "\\") { i++; continue; }
                if (c === quote) quote = null;
                continue;
            }
            if (c === "'" || c === '"' || c === "`") { quote = c; continue; }
            if (c === "(" || c === "[" || c === "{") depth++;
            else if (c === ")" || c === "]" || c === "}") { if (depth === 0) break; depth--; }
            else if (c === ";" && depth === 0) break;
            else if (c === "\n" && depth === 0) {
                const next = code.slice(i + 1).match(/\S/);
                if (!next || !CONTINUES_EXPRESSION.test(next[0])) break;
            }
        }
        const line = code.slice(0, m.index).split("\n").length;
        results.push({ line, op: m[1], rhs: code.slice(start, i).trim() });
    }
    return results;
}

const SAFE_PRODUCERS = ["sanitizeAssistantHtml", "renderMarkdown", "markdownToSafeHtml", "plainTextHtml"];

/** Index just past the parenthesis that closes the one at `open`, or -1. */
function closingParen(s, open) {
    let depth = 0;
    let quote = null;
    for (let i = open; i < s.length; i++) {
        const c = s[i];
        if (quote) {
            if (c === "\\") { i++; continue; }
            if (c === quote) quote = null;
            continue;
        }
        if (c === "'" || c === '"' || c === "`") { quote = c; continue; }
        if (c === "(") depth++;
        else if (c === ")" && --depth === 0) return i + 1;
    }
    return -1;
}

/**
 * Static markup, or EXACTLY one call to a sanctioned safe producer: the call
 * must be the whole right-hand side, so `sanitizeAssistantHtml(a) + raw`,
 * `renderMarkdown(a) || raw` or `plainTextHtml(a).replace(...)` are unsafe.
 */
export function isSafeRhs(rhs) {
    const r = rhs.trim();
    if (/^'[^'\\]*'$/.test(r) || /^"[^"\\]*"$/.test(r)) return true;      // plain string literal
    if (/^`[^`\\]*`$/.test(r) && !r.includes("${")) return true;           // template with no interpolation
    const call = new RegExp(`^(?:${SAFE_PRODUCERS.join("|")})\\s*\\(`).exec(r);
    return call !== null && closingParen(r, call[0].length - 1) === r.length;
}

// Every other API that hands a string to the HTML parser. None is needed in
// the guarded files, so any use is flagged (bracket notation included, which
// the `.innerHTML` scan above cannot see).
const RAW_HTML_APIS = [
    ["bracket innerHTML/outerHTML", /\[\s*(['"`])(?:inner|outer)HTML\1\s*\]/g],
    ["outerHTML assignment", /\.outerHTML\s*\+?=(?!=)/g],
    ["insertAdjacentHTML", /\binsertAdjacentHTML\s*\(/g],
    ["document.write", /\bdocument\s*\.\s*write(?:ln)?\s*\(/g],
    ["setHTMLUnsafe", /\bsetHTMLUnsafe\s*\(/g],
    ["parseHTMLUnsafe", /\bparseHTMLUnsafe\s*\(/g],
    ["createContextualFragment", /\bcreateContextualFragment\s*\(/g],
    ["srcdoc assignment", /\.srcdoc\s*=(?!=)/g],
];

/** Uses of the raw HTML-parsing APIs in (comment-stripped) code. */
export function rawHtmlApiUses(code) {
    const uses = [];
    for (const [api, re] of RAW_HTML_APIS) {
        for (const m of code.matchAll(re)) {
            uses.push({ line: code.slice(0, m.index).split("\n").length, api });
        }
    }
    return uses.sort((a, b) => a.line - b.line);
}
