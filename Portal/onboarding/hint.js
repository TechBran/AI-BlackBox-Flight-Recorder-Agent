// The wizard steps' status line ("hint"): one helper instead of a copy per
// step (audit P08-onboarding-installer-10). It touches the DOM, so it lives
// here rather than in the pure util.js.

/**
 * Show a message in a step's hint line, creating the line on first use.
 *
 * @param {Element|null} container - The step's content (nothing happens without it)
 * @param {string} msg - The message (set as text, never as HTML)
 * @param {boolean} isError - Styled as an error
 * @param {object} where - Which line:
 * @param {string} where.id - The line's id
 * @param {string} [where.after] - Created right after this element (a selector)
 * @param {string} [where.inside] - ...or inside this one, replacing what it held
 * @param {string} [where.className="ob-cli-agent-hint"] - The line's class
 * @param {string} [where.errorClass] - Added for an error (default: `${className}-error`)
 *   With neither `after` nor `inside`, the line must already be in the step's markup.
 */
export function showHint(container, msg, isError, { id, after = null, inside = null,
    className = "ob-cli-agent-hint", errorClass = `${className}-error` }) {
    if (!container) return;
    let hint = container.querySelector(`#${id}`);
    if (!hint) {
        const anchor = (after || inside) && container.querySelector(after || inside);
        if (!anchor) return;
        hint = document.createElement("div");
        hint.id = id;
        hint.className = className;
        if (after) anchor.insertAdjacentElement("afterend", hint);
        else anchor.replaceChildren(hint);
    }
    hint.classList.toggle(errorClass, !!isError);
    hint.textContent = msg;
    hint.hidden = false;
}
