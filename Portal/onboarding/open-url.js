// Opening external links from the onboarding wizard.
//
// The wizard runs in the Tauri shell (WebKitGTK, where target=_blank does not
// reach a browser), in plain browsers over Tailscale/LAN and in the Android
// app's WebView. onboarding.js routes every external target=_blank click here.

/**
 * Ask the box to open `url` in a browser on its own display; if it cannot,
 * open it here. The route answers HTTP 200 {ok:false} when no browser can be
 * spawned (and 400 for a refused URL), which used to count as success, so on
 * remote and Android WebView access every external link was a dead click
 * (audit P08-onboarding-installer-01).
 *
 * The answer can take many seconds (each browser the box tries gets a 4 s
 * probe, then gio and the desktop portal 10 s each), by which time the click
 * no longer counts as a user gesture and the new tab is blocked. The link is
 * then offered inline for a fresh tap; this tab is never navigated to it,
 * which would throw away what the operator typed into the wizard.
 * @param {string} url - An external http(s) URL
 * @returns {Promise<boolean>} Whether the box opened it
 */
export async function openExternalUrl(url) {
    let opened = false;
    try {
        const r = await fetch("/onboarding/open-url", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });
        const body = await r.json().catch(() => null);
        opened = r.ok && !!(body && body.ok);
    } catch (_) {
        opened = false;
    }
    if (opened) return true;
    if (!openNewTab(url)) offerLink(url);
    return false;
}

/**
 * window.open in a new tab, with the opener cut. (No "noopener" feature
 * string: with it window.open returns null even on success, so a blocked
 * popup could not be told apart; the opener is cut by hand instead.)
 * @param {string} url
 * @returns {boolean} Whether a new tab opened
 */
function openNewTab(url) {
    let win = null;
    try { win = window.open(url, "_blank"); } catch (_) { win = null; }
    if (!win) return false;
    try { win.opener = null; } catch (_) { /* cross-origin already */ }
    return true;
}

/**
 * Show `url` as a link the operator can tap (a fresh gesture, so the tab is
 * not blocked). One notice at a time; the tap or the dismiss button clears it.
 * @param {string} url
 */
function offerLink(url) {
    document.querySelector(".ob-open-link-notice")?.remove();
    const notice = document.createElement("div");
    notice.className = "ob-open-link-notice";
    notice.setAttribute("role", "status");
    const text = document.createElement("span");
    text.textContent = "Couldn't open the link automatically. Tap to open it: ";
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = url;
    // onboarding.js's open-url interceptor skips wired anchors: this tap opens it here.
    link.dataset.openWired = "1";
    link.addEventListener("click", (e) => {
        e.preventDefault();
        notice.remove();
        openOnThisDevice(url);
    });
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "ob-open-link-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "\u00d7";
    dismiss.addEventListener("click", () => notice.remove());
    text.appendChild(link);
    notice.append(text, dismiss);
    document.body.appendChild(notice);
}

/**
 * Open `url` on THIS device from a click: a new tab, or -- popup blocked, or
 * a WebView with no window host -- this tab, so the URL is at least reachable.
 * Only for a click handler that runs synchronously; after an await, use
 * openExternalUrl's inline offer instead.
 * @param {string} url
 * @returns {boolean} Whether a new tab opened
 */
export function openOnThisDevice(url) {
    if (openNewTab(url)) return true;
    window.location.assign(url);
    return false;
}
