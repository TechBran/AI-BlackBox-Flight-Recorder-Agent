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
    openOnThisDevice(url);
    return false;
}

/**
 * Open `url` on THIS device: a new tab, or -- popup blocked, or a WebView with
 * no window host -- this tab, so the URL is at least reachable. (No "noopener"
 * feature string: with it window.open returns null even on success, so a
 * blocked popup could not be told apart; the opener is cut by hand instead.)
 * @param {string} url
 * @returns {boolean} Whether a new tab opened
 */
export function openOnThisDevice(url) {
    let win = null;
    try { win = window.open(url, "_blank"); } catch (_) { win = null; }
    if (win) {
        try { win.opener = null; } catch (_) { /* cross-origin already */ }
        return true;
    }
    window.location.assign(url);
    return false;
}
