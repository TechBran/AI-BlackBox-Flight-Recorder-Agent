// Two-tap inline confirm for a destructive wizard button, in place of
// window.confirm(). The Android app hosts this wizard in a WebView with no
// dialog host, where confirm() returns false without showing anything, so
// "Remove operator" and "Remove credential file" silently did nothing on the
// phone (audit P08-onboarding-installer-06). api_keys.js already uses an
// inline "Remove? ✓/✕"; this is the same idea for a single button. The
// Portal's Restart Service (modules/updates-manager.js) uses it too.
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
