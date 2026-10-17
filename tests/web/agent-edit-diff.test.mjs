// Claude-agent edit summaries ("> Changed: `old` -> `new`") are markdown code
// spans: marked escapes code-span text itself, so the Portal must pass the RAW
// substring (HTML-escaping it first showed literal &#39; / &quot; in every edit
// that touched a quote), and must build the span so the text can never close
// it (a backtick in the edit used to end the span and leak the rest as markup).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const { AgentChatHandler } = await import("../../Portal/modules/agent-handler.js");

/** Render one edit_diff event through the real handler and return the "Changed" blockquote. */
function renderEdit(oldString, newString) {
    const handler = Object.create(AgentChatHandler.prototype);
    handler.outputBuffer = "";
    handler.thinkingBuffer = "";
    handler.isStreaming = true;
    const wrap = document.createElement("div");
    wrap.innerHTML = '<span class="bubble-text"></span>';
    document.getElementById("history").appendChild(wrap);
    handler.currentBubble = wrap;
    handler.appendEditDiff({ filePath: "/x/app.py", diff: `-${oldString}\n+${newString}`, oldString, newString });
    return { wrap, quote: wrap.querySelector("blockquote") };
}

test("quotes in a short edit render as typed (no double escaping)", () => {
    const { quote } = renderEdit("name = 'foo'", 'name = "bar"');
    assert.ok(quote, "Changed line rendered");
    assert.equal(quote.textContent.trim(), "Changed: name = 'foo' → name = \"bar\"");
    assert.equal(quote.querySelectorAll("code").length, 2);
});

test("<, > and & in a short edit render as typed", () => {
    const { quote } = renderEdit("if (a < b && c > d)", "x &amp; y");
    assert.deepEqual([...quote.querySelectorAll("code")].map((c) => c.textContent), ["if (a < b && c > d)", "x &amp; y"]);
});

test("a backtick in the edit cannot close the code span and leak markup", () => {
    const hostile = "a`<img src=x onerror=\"window.__pwned=1\">`b";
    const { wrap, quote } = renderEdit(hostile, "``ok``");
    assert.deepEqual(scriptVectors(wrap), [], wrap.innerHTML);
    assert.equal(wrap.querySelector("blockquote img"), null, quote.innerHTML);
    assert.deepEqual([...quote.querySelectorAll("code")].map((c) => c.textContent), [hostile, "``ok``"]);
});

test("a newline in the edit cannot break the blockquote line", () => {
    const { quote } = renderEdit("a\n---\n# b", "c");
    assert.ok(quote, "still one blockquote");
    assert.deepEqual([...quote.querySelectorAll("code")].map((c) => c.textContent), ["a --- # b", "c"]);
});
