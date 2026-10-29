// Two-tap inline confirm for a destructive wizard button, in place of
// window.confirm(). The Android app hosts this wizard in a WebView with no
// dialog host, where confirm() returns false without showing anything, so
// "Remove operator" and "Remove credential file" silently did nothing on the
// phone (audit P08-onboarding-installer-06). api_keys.js already uses an
// inline "Remove? ✓/✕"; this is the same idea for a single button. The
// Portal's Updates panel (modules/updates-manager.js) uses it for Restart
// Service, and askInline (below) for the questions that need their text.
//
//   if (!confirmInline(btn, { prompt: "Remove?" })) return;   // first tap arms
//   ...destructive action...                                   // second tap runs

const ARMED = new WeakMap();   // button -> { timer, label, title }

/**
 * First call: re-label the button with `prompt`, arm it for `timeoutMs`, and
 * return false. A second call within that window disarms it and returns true.
 * The label (and title) come back when the window lapses or on confirm.
 * @param {HTMLElement} button - The button that was tapped
 * @param {object} [opts]
 * @param {string} [opts.prompt="Tap again to confirm"] - Armed label
 * @param {string} [opts.detail] - Shown as the armed button's title (what happens)
 * @param {number} [opts.timeoutMs=4000] - How long the second tap is accepted
 * @returns {boolean} True when this tap confirms
 */
export function confirmInline(button, { prompt = "Tap again to confirm", detail = "", timeoutMs = 4000 } = {}) {
    if (!button) return false;
    const armed = ARMED.get(button);
    if (armed) {
        disarm(button, armed);
        return true;
    }
    const state = { label: button.textContent, title: button.getAttribute("title") };
    state.timer = setTimeout(() => disarm(button, state), timeoutMs);
    ARMED.set(button, state);
    button.textContent = prompt;
    if (detail) button.setAttribute("title", detail);
    button.classList.add("ob-confirm-armed");
    return false;
}

function disarm(button, state) {
    clearTimeout(state.timer);
    ARMED.delete(button);
    button.textContent = state.label;
    if (state.title === null) button.removeAttribute("title");
    else button.setAttribute("title", state.title);
    button.classList.remove("ob-confirm-armed");
}

const ASKING = new WeakMap();  // anchor -> settle() of its open confirm row

/**
 * Inline confirm row, for a question a button label cannot carry (what an
 * install disconnects, which local changes get stashed): `text` plus
 * Continue/Cancel buttons, placed right after the anchor's row. Same reason
 * as confirmInline: no confirm() dialog in the Android WebView.
 *
 *   if (!await askInline(btn, "Install 2 update(s)?", { okLabel: "Install" })) return;
 *
 * @param {HTMLElement} anchor - The control that asked; the row goes after its parent
 * @param {string} text - The question (plain text; newlines are kept)
 * @param {object} [opts]
 * @param {string} [opts.okLabel="Continue"]
 * @param {string} [opts.cancelLabel="Cancel"]
 * @param {string} [opts.buttonClass] - Both buttons' class (default: the anchor's first class)
 * @returns {Promise<boolean>} True on Continue; false on Cancel, or when the
 *   same anchor asks again (the newer question replaces this one)
 */
export function askInline(anchor, text, { okLabel = "Continue", cancelLabel = "Cancel", buttonClass } = {}) {
    const parent = anchor && anchor.parentElement;
    if (!parent) return Promise.resolve(false);
    const host = parent === document.body ? anchor : parent;  // never after <body>
    const open = ASKING.get(anchor);
    if (open) open(false);
    const cls = buttonClass !== undefined ? buttonClass : (anchor.classList[0] || "");
    return new Promise((resolve) => {
        const row = document.createElement("div");
        row.className = "inline-confirm-row";
        row.setAttribute("role", "alertdialog");
        const question = document.createElement("p");
        question.className = "inline-confirm-text";
        question.style.whiteSpace = "pre-line";
        question.textContent = text;   // textContent: server text never becomes markup
        const button = (label, kind) => {
            const b = document.createElement("button");
            b.type = "button";
            b.className = `${cls} ${kind}`.trim();
            b.textContent = label;
            return b;
        };
        const ok = button(okLabel, "inline-confirm-ok");
        const cancel = button(cancelLabel, "inline-confirm-cancel");
        row.append(question, ok, cancel);
        host.insertAdjacentElement("afterend", row);
        const settle = (answer) => {
            if (ASKING.get(anchor) === settle) ASKING.delete(anchor);
            row.remove();
            resolve(answer);
        };
        ASKING.set(anchor, settle);
        ok.addEventListener("click", () => settle(true));
        cancel.addEventListener("click", () => settle(false));
    });
}
