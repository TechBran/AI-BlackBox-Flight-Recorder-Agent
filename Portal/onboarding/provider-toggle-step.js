// A wizard step that lets the operator choose which providers of one feature
// are ENABLED and which one is the DEFAULT. The web-search and image steps were
// the same 300-line module twice, differing only in ids, the provider list, the
// config block, the two env names and DuckDuckGo's keyless floor (audit
// P08-onboarding-installer-10); each is now a spec passed to this factory.
//
// Data flow:
//   1. GET /onboarding/current-config — read the spec's config block:
//        { providers: {<id>: {key_present, enabled}}, enabled: [...], default }
//   2. Render one checkbox per provider whose key is present (keyless ones
//      always), pre-checked when providers[p].enabled is true.
//   3. A "Preferred default" select lists ONLY the checked providers (the
//      backend guarantees default ∈ enabled; we enforce the same by building
//      the select from the enabled set). Pre-selects the configured default if
//      it's still enabled.
//   4. Continue POSTs /onboarding/save
//        {secrets:{<envEnabled>:"a,b,c", <envDefault>:"a"}}
//      (mirrors how api_keys / transcription persist), then advances via
//      next(), which POSTs /onboarding/step/complete.
//
// Reuses the CLI Agents grid/card styling (no new CSS).

import { cssEscape, escapeHtml, fetchJson } from "./util.js";
import { showHint } from "./hint.js";

/**
 * @param {object} spec
 * @param {string} spec.prefix - The ids' infix: ob-<prefix>-grid, -default, ...
 * @param {string} spec.sectionClass - The step section's class
 * @param {string} spec.sigilNum - The sigil number when the wizard gives none
 * @param {string} spec.sigilLabel - The sigil label
 * @param {string} spec.eyebrow - The eyebrow text
 * @param {string} spec.titleHtml - The title (trusted HTML)
 * @param {string} spec.ledeHtml - The lede (trusted HTML)
 * @param {string} spec.loadingHtml - The grid's loading text (trusted HTML)
 * @param {string} spec.emptyHtml - The grid when no provider is available (trusted HTML)
 * @param {string} spec.backDefault - The Back label when the wizard gives none
 * @param {string} spec.cfgKey - The block of /onboarding/current-config
 * @param {Array<{id: string, label: string}>} spec.providers - Display order + labels
 * @param {string[]} [spec.keylessIds] - Providers shown without a key ("Free")
 * @param {string} spec.envEnabled - The secret the enabled set is saved under
 * @param {string} spec.envDefault - The secret the default is saved under
 * @returns {function(Element, {next, back, skip, sigil}): Promise<void>} The step's render
 */
export function providerToggleStep(spec) {
    const { prefix, providers: PROVIDERS, keylessIds = [] } = spec;
    const sel = (name) => `#ob-${prefix}-${name}`;
    const HINT = { id: `ob-${prefix}-hint`, after: sel("grid") };
    let cfg = null;       // the spec's block from /onboarding/current-config
    let saving = false;   // prevents save double-fire

    // Only providers whose key is present are shown (keyless ones always).
    function visibleProviders() {
        const provs = (cfg && cfg.providers) || {};
        return PROVIDERS.filter((p) => keylessIds.includes(p.id) || (provs[p.id] && provs[p.id].key_present));
    }

    function renderGrid(container) {
        const grid = container.querySelector(sel("grid"));
        const visible = visibleProviders();
        const provs = (cfg && cfg.providers) || {};

        if (!visible.length) {
            grid.innerHTML = `<p class="ob-cli-agent-auth-blurb">${spec.emptyHtml}</p>`;
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
        const badge = keylessIds.includes(id)
            ? `<span class="ob-cli-agent-badge ob-cli-agent-badge-ok">&check; Free</span>`
            : `<span class="ob-cli-agent-badge ob-cli-agent-badge-ok">&check; Key on file</span>`;
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
        const grid = container.querySelector(sel("grid"));
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
        const wrap = container.querySelector(sel("default-wrap"));
        const select = container.querySelector(sel("default"));
        if (!wrap || !select) return;

        const checked = checkedProviders(container);
        if (!checked.length) {
            wrap.hidden = true;
            select.innerHTML = "";
            return;
        }

        const prior = select.value;  // whatever was selected before the re-sync
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
        select.innerHTML = checked
            .map((id) => `<option value="${escapeHtml(id)}"${id === want ? " selected" : ""}>${escapeHtml(labelOf(id))}</option>`)
            .join("");
        select.value = want;
        wrap.hidden = false;
    }

    // Continue requires at least one provider enabled.
    function updateContinue(container) {
        const cont = container.querySelector(sel("continue"));
        const note = container.querySelector(sel("none-note"));
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

    // Save the enabled set + default, then advance. Mirrors transcription's
    // "POST /onboarding/save then advance" mechanic — here we persist BOTH prefs
    // atomically (default is co-constrained with the enabled set) before next().
    async function save(container, next) {
        if (saving) return;
        const checked = checkedProviders(container);
        if (!checked.length) return;  // guarded by updateContinue, belt-and-suspenders

        const select = container.querySelector(sel("default"));
        let def = (select && select.value) || "";
        if (!checked.includes(def)) def = checked[0];  // never let default drift out of the set

        saving = true;
        const cont = container.querySelector(sel("continue"));
        if (cont) cont.disabled = true;
        try {
            const r = await fetch("/onboarding/save", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    secrets: {
                        [spec.envEnabled]: checked.join(","),
                        [spec.envDefault]: def,
                    },
                }),
            });
            if (!r.ok) throw new Error(`save returned ${r.status}`);
            await next();  // POSTs /onboarding/step/complete + advances
        } catch (e) {
            showHint(container, `Couldn't save your choices: ${e.message}. Try again.`, true, HINT);
            if (cont) cont.disabled = false;
        } finally {
            saving = false;
        }
    }

    return async function render(container, { next, back, skip, sigil }) {
        const id = (name) => `ob-${prefix}-${name}`;
        container.innerHTML = `
            <section class="ob-step ${spec.sectionClass}">
                <aside class="ob-step-sigil" aria-hidden="true">
                    <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : spec.sigilNum}</em></div>
                    <div class="ob-step-sigil-rule"></div>
                    <div class="ob-step-sigil-label">${spec.sigilLabel}</div>
                </aside>
                <div class="ob-step-body">
                    <div class="ob-step-eyebrow">
                        <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                        ${spec.eyebrow}
                    </div>
                    <h1 class="ob-step-title">
                        ${spec.titleHtml}
                    </h1>
                    <p class="ob-step-lede">
                        ${spec.ledeHtml}
                    </p>
                    <div id="${id("grid")}" class="ob-cli-agent-grid">
                        <div class="ob-loading">${spec.loadingHtml}</div>
                    </div>
                    <div id="${id("default-wrap")}" class="ob-cli-agent-meta-row" hidden
                         style="margin-top: var(--ob-space-5, 1.25rem); align-items: center; gap: var(--ob-space-3, 0.75rem);">
                        <label for="${id("default")}" class="ob-cli-agent-meta-label">Preferred default</label>
                        <select id="${id("default")}" class="ob-cli-agent-bin"
                                style="padding: 0.35rem 0.6rem; background: var(--ob-surface-elevated, #0a0a0a); color: var(--ob-text-primary, #fff); border: 1px solid var(--ob-border, #333);"></select>
                    </div>
                    <p id="${id("none-note")}" class="ob-step-helper" hidden></p>
                    <nav class="ob-step-nav" aria-label="Step navigation">
                        <button type="button" class="ob-back" id="${id("back")}">
                            <span aria-hidden="true">&larr;</span> Back to ${sigil && sigil.backLabel ? escapeHtml(sigil.backLabel.toLowerCase()) : spec.backDefault}
                        </button>
                        <button type="button" class="ob-cta" id="${id("continue")}" disabled>
                            Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                        </button>
                        <button type="button" class="ob-skip" id="${id("skip")}">
                            Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                        </button>
                    </nav>
                </div>
            </section>
        `;

        container.querySelector(sel("back")).addEventListener("click", back);
        container.querySelector(sel("skip")).addEventListener("click", skip);
        container.querySelector(sel("continue")).addEventListener("click", () => save(container, next));

        // Fail-open: if the config call fails we still render the providers we
        // know about (none pre-checked; only keyless ones show without key state).
        const full = await fetchJson("/onboarding/current-config");
        cfg = (full && full[spec.cfgKey]) || { providers: {}, enabled: [], default: "" };

        renderGrid(container);
        syncDefault(container);
        updateContinue(container);
    };
}
