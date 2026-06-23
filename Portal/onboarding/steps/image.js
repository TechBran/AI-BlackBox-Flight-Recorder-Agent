// Image-generation step — lets the operator choose which image providers are
// ENABLED and which one is the DEFAULT. Mirrors the web-search step's structure,
// styling, and save/advance mechanics.
//
// The BlackBox can generate images from several providers. Every image provider
// needs an API key entered in the Keys step — there is NO keyless floor (unlike
// web-search's DuckDuckGo). A provider whose key is absent is hidden here; you
// enable it once the key is in. If no provider has a key on file, this step
// tells the operator to add an image-provider key in the API Keys step.
//
// This step:
//   1. GET /onboarding/current-config — read the .image block:
//        providers[p] = {key_present, enabled}, enabled[], default
//   2. Render one checkbox per provider whose key is present, pre-checked when
//      providers[p].enabled is true.
//   3. A "Preferred default" select lists ONLY the currently-checked providers,
//      kept in sync as checkboxes toggle (a default must always be one of the
//      enabled set). Pre-selects image.default if it's still enabled.
//   4. Require at least one provider enabled — Continue is disabled otherwise.
//   5. On Continue: POST /onboarding/save with
//        {secrets:{IMAGE_ENABLED:"a,b,c", IMAGE_DEFAULT:"a"}}
//      (mirrors how web_search persists), then advance via next()
//      which POSTs /onboarding/step/complete {step:"image"}.
//
// Reuses the .ob-cli-agent-* card/badge/grid CSS (same selectable-card shape as
// the CLI Agents / web-search steps).

// Display order + labels (per the multi-provider image-generation spec). The
// provider IDs match the backend FEATURES["image"]["provider_env"] keys exactly.
const PROVIDERS = [
    { id: "gemini", label: "Gemini Nano Banana" },
    { id: "openai", label: "OpenAI (gpt-image)" },
    { id: "grok", label: "Grok image" },
];

let cfg = null;       // .image block from /onboarding/current-config
let saving = false;   // prevents save double-fire

export async function render(container, { next, back, skip, sigil }) {
    container.innerHTML = `
        <section class="ob-step ob-image">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "07"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">IMAGE</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Image-generation providers
                </div>
                <h1 class="ob-step-title">
                    Choose how the BlackBox <em>generates images</em>.
                </h1>
                <p class="ob-step-lede">
                    Pick which providers the BlackBox may use to generate images,
                    then set a preferred default. Every image provider bills to the
                    API key you entered in the Keys step &mdash; only providers with
                    a key on file are shown here. The default is always one of the
                    enabled providers.
                </p>
                <div id="ob-img-grid" class="ob-cli-agent-grid">
                    <div class="ob-loading">Loading image options&hellip;</div>
                </div>
                <div id="ob-img-default-wrap" class="ob-cli-agent-meta-row" hidden
                     style="margin-top: var(--ob-space-5, 1.25rem); align-items: center; gap: var(--ob-space-3, 0.75rem);">
                    <label for="ob-img-default" class="ob-cli-agent-meta-label">Preferred default</label>
                    <select id="ob-img-default" class="ob-cli-agent-bin"
                            style="padding: 0.35rem 0.6rem; background: var(--ob-surface-elevated, #0a0a0a); color: var(--ob-text-primary, #fff); border: 1px solid var(--ob-border, #333);"></select>
                </div>
                <p id="ob-img-none-note" class="ob-step-helper" hidden></p>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-img-back">
                        <span aria-hidden="true">&larr;</span> Back to ${sigil && sigil.backLabel ? sigil.backLabel.toLowerCase() : "web search"}
                    </button>
                    <button type="button" class="ob-cta" id="ob-img-continue" disabled>
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-img-skip">
                        Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;

    document.getElementById("ob-img-back").addEventListener("click", back);
    document.getElementById("ob-img-skip").addEventListener("click", skip);
    document.getElementById("ob-img-continue").addEventListener("click", () => save(container, next));

    // Fail-open: if the config call fails we still render the providers we know
    // about (none pre-checked, all hidden if key state is unknown — image has no
    // keyless provider, so a failed config call shows the "add a key" message).
    const full = await fetchJson("/onboarding/current-config");
    cfg = (full && full.image) || { providers: {}, enabled: [], default: "" };

    renderGrid(container);
    syncDefault(container);
    updateContinue(container);
}

// Only providers whose key is present are shown — image has NO keyless floor.
function visibleProviders() {
    const provs = (cfg && cfg.providers) || {};
    return PROVIDERS.filter((p) => provs[p.id] && provs[p.id].key_present);
}

function renderGrid(container) {
    const grid = container.querySelector("#ob-img-grid");
    const visible = visibleProviders();
    const provs = (cfg && cfg.providers) || {};

    if (!visible.length) {
        grid.innerHTML =
            `<p class="ob-cli-agent-auth-blurb">No image providers are configured yet. ` +
            `Add an image-provider API key (Gemini, OpenAI, or Grok) in the API Keys step to enable image generation.</p>`;
        return;
    }

    grid.innerHTML = visible.map((p) => {
        const checked = !!(provs[p.id] && provs[p.id].enabled);
        return renderCard(p, checked);
    }).join("");

    // Wire each checkbox: toggling re-syncs the default select + Continue state.
    visible.forEach((p) => {
        const cb = grid.querySelector(`input[type="checkbox"][data-provider="${cssEscape(p.id)}"]`);
        if (cb) {
            cb.addEventListener("change", () => {
                syncDefault(container);
                updateContinue(container);
            });
        }
        // Whole card toggles the checkbox (matches the click-the-card feel of
        // the sibling steps) — but only for clicks on the card PADDING. The
        // checkbox lives inside an implicit <label> (input is a descendant, no
        // "for"), so clicking the box OR the label text already toggles once via
        // the native label action; re-toggling here would double-fire change.
        const card = grid.querySelector(`.ob-cli-agent-card[data-provider="${cssEscape(p.id)}"]`);
        if (card) {
            card.addEventListener("click", (e) => {
                // Let the native label/input handle clicks inside the label.
                if (e.target && (e.target.tagName === "INPUT" || (e.target.closest && e.target.closest("label")))) return;
                const box = card.querySelector("input[type=\"checkbox\"]");
                if (box) {
                    box.checked = !box.checked;
                    box.dispatchEvent(new Event("change"));
                }
            });
        }
    });
}

function renderCard(meta, checked) {
    const id = meta.id;
    const badge = `<span class="ob-cli-agent-badge ob-cli-agent-badge-ok">&check; Key on file</span>`;
    // Every visible card has a key on file (keyless-unavailable providers are
    // hidden), so an UNCHECKED card is "off", not "needs attention". Use the
    // green "ready" state when selected and omit data-state when unselected so
    // the card reads as a neutral, plain border (no misleading amber).
    const dataState = checked ? "ready" : "";

    return `
        <div class="ob-cli-agent-card" data-provider="${escapeHtml(id)}"
             ${dataState ? `data-state="${dataState}"` : ""} tabindex="0">
            <div class="ob-cli-agent-head">
                <div class="ob-cli-agent-title">
                    <label class="ob-cli-agent-name" style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                        <input type="checkbox" data-provider="${escapeHtml(id)}"${checked ? " checked" : ""}>
                        <span>${escapeHtml(meta.label)}</span>
                    </label>
                </div>
                ${badge}
            </div>
        </div>
    `;
}

// Returns the list of currently-checked provider ids (in display order).
function checkedProviders(container) {
    const grid = container.querySelector("#ob-img-grid");
    if (!grid) return [];
    return visibleProviders()
        .filter((p) => {
            const cb = grid.querySelector(`input[type="checkbox"][data-provider="${cssEscape(p.id)}"]`);
            return cb && cb.checked;
        })
        .map((p) => p.id);
}

// Keep the "Preferred default" select listing ONLY the checked providers. The
// default must always be one of the enabled set; we preserve the prior choice
// when still valid, then fall back to the configured default, then the first.
function syncDefault(container) {
    const wrap = container.querySelector("#ob-img-default-wrap");
    const sel = container.querySelector("#ob-img-default");
    if (!wrap || !sel) return;

    const checked = checkedProviders(container);
    if (!checked.length) {
        wrap.hidden = true;
        sel.innerHTML = "";
        return;
    }

    const prior = sel.value;  // whatever was selected before the re-sync
    let want = "";
    if (checked.includes(prior)) {
        want = prior;
    } else if (checked.includes((cfg && cfg.default) || "")) {
        want = cfg.default;
    } else {
        want = checked[0];
    }

    const labelOf = (id) => {
        const m = PROVIDERS.find((p) => p.id === id);
        return m ? m.label : id;
    };
    sel.innerHTML = checked
        .map((id) => `<option value="${escapeHtml(id)}"${id === want ? " selected" : ""}>${escapeHtml(labelOf(id))}</option>`)
        .join("");
    sel.value = want;
    wrap.hidden = false;
}

// Continue requires at least one provider enabled.
function updateContinue(container) {
    const cont = container.querySelector("#ob-img-continue");
    const note = container.querySelector("#ob-img-none-note");
    if (!cont) return;
    const checked = checkedProviders(container);
    cont.disabled = checked.length === 0;
    if (note) {
        if (checked.length === 0) {
            note.textContent = "Select at least one provider to continue.";
            note.hidden = false;
        } else {
            note.hidden = true;
        }
    }
}

// Save the enabled set + default, then advance. Mirrors web_search's
// "POST /onboarding/save then advance" mechanic — here we persist BOTH prefs
// atomically (default is co-constrained with the enabled set) before next().
async function save(container, next) {
    if (saving) return;
    const checked = checkedProviders(container);
    if (!checked.length) return;  // guarded by updateContinue, belt-and-suspenders

    const sel = container.querySelector("#ob-img-default");
    let def = (sel && sel.value) || "";
    if (!checked.includes(def)) def = checked[0];  // never let default drift out of the set

    saving = true;
    const cont = container.querySelector("#ob-img-continue");
    if (cont) cont.disabled = true;
    try {
        const r = await fetch("/onboarding/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                secrets: {
                    IMAGE_ENABLED: checked.join(","),
                    IMAGE_DEFAULT: def,
                },
            }),
        });
        if (!r.ok) throw new Error(`save returned ${r.status}`);
        await next();  // POSTs /onboarding/step/complete {step:"image"} + advances
    } catch (e) {
        showHint(container, `Couldn't save your choices: ${e.message}. Try again.`, true);
        if (cont) cont.disabled = false;
    } finally {
        saving = false;
    }
}

function showHint(container, msg, isError) {
    let hint = container.querySelector("#ob-img-hint");
    if (!hint) {
        hint = document.createElement("div");
        hint.id = "ob-img-hint";
        hint.className = "ob-cli-agent-hint";
        const grid = container.querySelector("#ob-img-grid");
        if (grid) grid.insertAdjacentElement("afterend", hint);
    }
    hint.classList.toggle("ob-cli-agent-hint-error", !!isError);
    hint.textContent = msg;
}

async function fetchJson(url) {
    try {
        const r = await fetch(url);
        if (!r.ok) return null;
        return await r.json();
    } catch (_) {
        return null;
    }
}

// cssEscape is the shared pure helper in ../util.js (de-duped across steps).
import { cssEscape } from "../util.js";

function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}
