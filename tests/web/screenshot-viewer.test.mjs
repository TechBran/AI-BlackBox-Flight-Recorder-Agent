// Computer Use screenshots open full-size through ONE delegated click handler
// (task-manager.js). The sanitizer keeps the screenshot classes, so model
// output can wear them: the new tab must get no window.opener (reverse
// tabnabbing could redirect the Portal tab), and only http(s) URLs open.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

await import("../../Portal/modules/task-manager.js");

function clickScreenshot(src, className = "browser-screenshot") {
    const calls = [];
    const realOpen = window.open;
    window.open = (...args) => { calls.push(args); return null; };
    const img = document.createElement("img");
    img.className = className;
    img.setAttribute("src", src);
    document.body.appendChild(img);
    try {
        img.click();
    } finally {
        img.remove();
        window.open = realOpen;
    }
    return calls;
}

for (const cls of ["browser-screenshot", "browser-gallery-thumb"]) {
    test(`${cls} opens in a new tab WITHOUT an opener handle`, () => {
        const calls = clickScreenshot("https://evil.example/x.png", cls);
        assert.equal(calls.length, 1);
        const [url, target, features] = calls[0];
        assert.equal(url, "https://evil.example/x.png");
        assert.equal(target, "_blank");
        assert.match(String(features || ""), /\bnoopener\b/);
        assert.match(String(features || ""), /\bnoreferrer\b/);
    });
}

test("same-origin screenshots still open", () => {
    const calls = clickScreenshot("/ui/uploads/cu/shot.png");
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "http://localhost:9091/ui/uploads/cu/shot.png");
});

test("javascript: and data: screenshot URLs never open", () => {
    assert.deepEqual(clickScreenshot("javascript:alert(1)"), []);
    assert.deepEqual(clickScreenshot("data:text/html,<script>alert(1)</script>"), []);
});
