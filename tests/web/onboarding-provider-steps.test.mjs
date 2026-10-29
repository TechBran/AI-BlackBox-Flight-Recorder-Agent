// The wizard's web-search and image steps are one provider-toggle step
// (audit P08-onboarding-installer-10): they differed only in ids, the provider
// list, the config block, the two env names and the keyless DuckDuckGo floor.
// These pin what each one renders and saves.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const settle = () => new Promise((r) => setTimeout(r, 20));
const json = (body, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body),
});

function serve(config) {
    const saved = [];
    globalThis.fetch = window.fetch = (url, init = {}) => {
        if (url === "/onboarding/current-config") return json(config);
        if (url === "/onboarding/save") {
            saved.push(JSON.parse(init.body));
            return json({ ok: true });
        }
        return json({}, 404);
    };
    return saved;
}

async function renderStep(name, config) {
    const saved = serve(config);
    const container = document.createElement("div");
    document.body.appendChild(container);
    let advanced = 0;
    const mod = await import(`../../Portal/onboarding/steps/${name}.js`);
    await mod.render(container, { next: async () => { advanced += 1; }, back() {}, skip() {}, sigil: null });
    return { container, saved, advanced: () => advanced };
}

const ids = (container, grid) =>
    [...container.querySelectorAll(`#${grid} input[type=checkbox]`)].map((cb) => cb.dataset.provider);

test("web search: keyed providers with a key, DuckDuckGo always; saves WEB_SEARCH_*", async () => {
    const { container, saved, advanced } = await renderStep("web_search", {
        web_search: {
            providers: { openai: { key_present: true, enabled: true }, gemini: { key_present: false }, duckduckgo: { enabled: true } },
            default: "duckduckgo",
        },
    });
    assert.deepEqual(ids(container, "ob-ws-grid"), ["openai", "duckduckgo"]);
    assert.match(container.querySelector('[data-provider="duckduckgo"]').textContent, /Free/);
    assert.equal(container.querySelector("#ob-ws-default").value, "duckduckgo");
    assert.equal(container.querySelector("#ob-ws-continue").disabled, false);
    container.querySelector("#ob-ws-continue").click();
    await settle();
    assert.deepEqual(saved, [{ secrets: { WEB_SEARCH_ENABLED: "openai,duckduckgo", WEB_SEARCH_DEFAULT: "duckduckgo" } }]);
    assert.equal(advanced(), 1);
    container.remove();
});

test("image: no keyless floor, the add-a-key message with no key; saves IMAGE_*", async () => {
    let { container } = await renderStep("image", { image: { providers: {}, default: "" } });
    assert.deepEqual(ids(container, "ob-img-grid"), []);
    assert.match(container.querySelector("#ob-img-grid").textContent, /No image providers are configured yet/);
    assert.equal(container.querySelector("#ob-img-continue").disabled, true);
    container.remove();

    const step = await renderStep("image", {
        image: { providers: { gemini: { key_present: true, enabled: false }, grok: { key_present: true, enabled: true } }, default: "gemini" },
    });
    container = step.container;
    assert.deepEqual(ids(container, "ob-img-grid"), ["gemini", "grok"]);
    assert.equal(container.querySelector("#ob-img-default").value, "grok", "the default is always an enabled provider");
    const gemini = container.querySelector('input[data-provider="gemini"]');
    gemini.checked = true;
    gemini.dispatchEvent(new Event("change"));
    assert.equal(container.querySelector("#ob-img-default").value, "grok", "a still-enabled choice is kept");
    container.querySelector("#ob-img-continue").click();
    await settle();
    assert.deepEqual(step.saved, [{ secrets: { IMAGE_ENABLED: "gemini,grok", IMAGE_DEFAULT: "grok" } }]);
    container.remove();
});

test("a failed save shows its hint after the grid and re-enables Continue", async () => {
    const { container } = await renderStep("web_search", { web_search: { providers: { duckduckgo: { enabled: true } } } });
    globalThis.fetch = window.fetch = () => json({}, 500);
    container.querySelector("#ob-ws-continue").click();
    await settle();
    const hint = container.querySelector("#ob-ws-hint");
    assert.ok(hint, "the hint is created");
    assert.equal(hint.previousElementSibling.id, "ob-ws-grid");
    assert.ok(hint.classList.contains("ob-cli-agent-hint-error"));
    assert.match(hint.textContent, /Couldn't save your choices/);
    assert.equal(container.querySelector("#ob-ws-continue").disabled, false);
    container.remove();
});
