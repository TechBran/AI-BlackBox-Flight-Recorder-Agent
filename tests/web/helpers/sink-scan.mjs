// Grep-level scanners for Portal HTML sinks, shared by html-sinks-guard.test.mjs.
// Comment-aware (a small tokenizer for strings, templates and regex literals),
// so commented-out code and `//` inside URLs never confuse the guards.

/**
 * Strip // and /* *\/ comments. A small tokenizer that understands strings,
 * template literals (with nested ${...} expressions) and regex literals, so
 * a `//` inside a URL string or a quote inside a regex never derails it.
 */
export function stripComments(code) {
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
        if (c === "/" && n === "*") { i = code.indexOf("*/", i + 2); i = i < 0 ? code.length : i + 2; continue; }
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

/**
 * The right-hand side of each `.innerHTML =` / `+=` assignment, up to the end
 * of the statement (template literals and parentheses are balanced).
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
            else if ((c === ";" || c === "\n") && depth === 0) break;
        }
        const line = code.slice(0, m.index).split("\n").length;
        results.push({ line, op: m[1], rhs: code.slice(start, i).trim() });
    }
    return results;
}

/** Static markup, or the output of one of the sanctioned safe producers. */
export function isSafeRhs(rhs) {
    if (/^'[^']*'$/.test(rhs) || /^"[^"]*"$/.test(rhs)) return true;           // plain string literal
    if (/^`[^`]*`$/.test(rhs) && !rhs.includes("${")) return true;             // template with no interpolation
    return /^(sanitizeAssistantHtml|renderMarkdown|markdownToSafeHtml|plainTextHtml)\(/.test(rhs);
}
