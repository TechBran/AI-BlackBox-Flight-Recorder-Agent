// The MCP step's token Revoke reads the DELETE /mcp/tokens reply (M6 u44): when
// the running MCP server could not be told ({live: false}), the token still
// works there until it reloads or restarts, and the step says so next to the
// list instead of silently showing the token as gone.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
});

const TOKEN_ID = "sha256:0123456789ab";
const NOTE = "Token removed. The running MCP server still accepts it until "
    + "blackbox-mcp.service reloads or restarts.";

function serve({ live }) {
    let tokens = [{ token_id: TOKEN_ID, operator: "op-a" }];
    const deletes = [];
    globalThis.fetch = window.fetch = async (url, opts = {}) => {
        const u = String(url);
        if (opts.method === "DELETE" && u.startsWith("/mcp/tokens?")) {
            deletes.push(u);
            tokens = [];
            return json({ ok: true, revoked: 1, live, note: live ? null : NOTE });
        }
        if (u === "/mcp/status") return json({ service: { installed: true }, mcp_up: true, tokens_present: true });
        if (u === "/operators") return json({ operators: ["op-a"] });
        if (u === "/mcp/tokens") return json({ tokens });
        if (u === "/mcp/connection") return json({ per_app_configs: {} });
        return json({}, 404);
    };
    return deletes;
}

async function renderStep() {
    const { render } = await import("../../Portal/onboarding/steps/mcp.js");
    const container = document.createElement("div");
    document.body.appendChild(container);
    await render(container, { next() {}, back() {}, skip() {}, sigil: null });
    return container;
}

test("a revoke the running MCP server did not hear says so", async () => {
    const deletes = serve({ live: false });
    const container = await renderStep();
    try {
        container.querySelector(`[data-revoke="${TOKEN_ID}"]`).click();
        await settle();
        assert.deepEqual(deletes, [`/mcp/tokens?token_id=${encodeURIComponent(TOKEN_ID)}`]);
        const list = container.querySelector("#ob-mcp-token-list");
        assert.match(list.textContent, /No tokens yet/);
        assert.match(list.textContent, new RegExp(`${TOKEN_ID}: Token removed\\. The running MCP server still accepts it`));
    } finally {
        container.remove();
    }
});

test("a revoke the running MCP server applied shows no note", async () => {
    serve({ live: true });
    const container = await renderStep();
    try {
        container.querySelector(`[data-revoke="${TOKEN_ID}"]`).click();
        await settle();
        const list = container.querySelector("#ob-mcp-token-list");
        assert.match(list.textContent, /No tokens yet/);
        assert.doesNotMatch(list.textContent, /still accepts it/);
    } finally {
        container.remove();
    }
});
