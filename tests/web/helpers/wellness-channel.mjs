// A stand-in for the BlackBox Android app's health signer channel (M7 wave
// 3b, contract (c)): the in-app Portal posts
//   window.BlackBoxWellness.postMessage(JSON.stringify({id, method, path, sha256}))
// and the app answers with a `message` event on the same object whose `data`
// is JSON {id, value} (`value` the X-BlackBox-Origin header or null). The app
// (WellnessChatRules.parseBridgeRequest) gives a malformed request no answer
// at all, so the Portal times out and sends unsigned; this fake does the same.
//
// The answer is asynchronous, as WebViewCompat's reply proxy is. `signed`
// records every well-formed request as {method, path, hex}, the shape the
// earlier AndroidBridge.wellnessSign fakes recorded.

const ID = /^[A-Za-z0-9_.:-]{1,64}$/;
const MAX_MESSAGE = 2048;

/** The app's parse of one request, or null (no answer). */
export function parseRequest(message) {
    if (typeof message !== "string" || message.length > MAX_MESSAGE) return null;
    let obj;
    try {
        obj = JSON.parse(message);
    } catch {
        return null;
    }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
    const idOk = (typeof obj.id === "string" && ID.test(obj.id)) || Number.isInteger(obj.id);
    if (!idOk || typeof obj.method !== "string" || typeof obj.path !== "string" || typeof obj.sha256 !== "string") return null;
    return obj;
}

/**
 * A fake window.BlackBoxWellness.
 * @param {object} [opts]
 * @param {(method: string, path: string, hex: string) => *} [opts.sign] - the value to answer
 *   (default: a fresh, well-formed proof per request)
 * @param {"answer"|"never"|"wrong-id-first"|"twice"} [opts.mode="answer"]
 * @param {number} [opts.delayMs=0] - how long the app takes to answer
 */
export function fakeChannel({ sign, mode = "answer", delayMs = 0 } = {}) {
    const channel = new window.EventTarget();      // the page's own, as WebView's object is
    const signed = [];
    const posted = [];
    let n = 0;
    const answer = (id, value) => channel.dispatchEvent(new window.MessageEvent("message", { data: JSON.stringify({ id, value }) }));
    channel.signed = signed;
    channel.posted = posted;
    channel.postMessage = (message) => {
        posted.push(message);
        const req = parseRequest(message);
        if (!req || mode === "never") return;              // the app sends nothing back
        signed.push({ method: req.method, path: req.path, hex: req.sha256 });
        let value;
        if (sign) {
            value = sign(req.method, req.path, req.sha256);
        } else {
            n += 1;
            value = `v1;enr=en_0000abcd;ts=${1800000000 + n};nonce=${String(n).padStart(32, "0")};sig=${"a".repeat(64)}`;
        }
        setTimeout(() => {
            if (mode === "wrong-id-first") answer(`${req.id}-other`, "v1;enr=en_0000beef;ts=1;nonce=x;sig=y");
            answer(req.id, value === undefined ? null : value);
            if (mode === "twice") answer(req.id, "v1;enr=en_0000beef;ts=2;nonce=x;sig=z");
        }, delayMs);
    };
    return channel;
}

/** Put `channel` on the page (null: no channel, a desktop browser or an older app). */
export function installChannel(channel) {
    if (channel) window.BlackBoxWellness = channel;
    else delete window.BlackBoxWellness;
}
