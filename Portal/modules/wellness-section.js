/**
 * wellness-section.js
 * System-Menu "Health" card (M7.2 of docs/plans/2026-10-25-samsung-health-integration.md,
 * §6.2): the current operator's health tracking status, read-only.
 *
 * Health Connect permissions can only be granted on the phone, so setup and
 * every sharing choice happen in the BlackBox Android app (Settings, Health).
 * This card shows what the box tells ANY surface about the operator:
 *
 *   GET /wellness/status?operator=X  ->  {state, source_label, last_sync, enrolled}
 *
 * and nothing more. Categories, providers and trouble codes come back only to
 * a signed request (the enrolled phone; approved browsers arrive in M7.3), so
 * a reply is cut down to those four fields (pickStatus) before anything is
 * drawn, whatever else it carries. The card only ever asks about the operator
 * this Portal is on (getOperator()), never another one (rule W17).
 *
 * Inside the BlackBox Android app (spec §6.3) the WebView's message channel
 * window.BlackBoxWellness (WebViewCompat.addWebMessageListener, the paired
 * box's main frame only) returns the X-BlackBox-Origin value for one request,
 * signed on the phone with its device key (the key never enters JavaScript),
 * or null: originProof posts {id, method, path, sha256} and waits for the
 * {id, value} message with that id. The status read is then signed, and the
 * settings view the box returns to the enrolled phone is shown read-only
 * (pickSignedStatus): what BlackBox may use, where it may go, the trouble
 * cards with their fix, the instinct prompt and the disk-encryption line,
 * and, when opened, "Who saw my health data" from a signed GET
 * /wellness/disclosures. Changing any of it stays on the phone (narrowing
 * from the Portal is M7.3). An unsigned read (no channel, an app that does
 * not answer within SIGN_TIMEOUT_MS, or a null from it) shows the four
 * fields only, and so does a signed read the box refuses (it is read again,
 * once, unsigned). originProof / withOriginProof are the one signing path the
 * chat and the Timeline use too: a fresh proof per request, over the sha256 of
 * the exact UTF-8 body bytes. A signed GET names the operator in its query
 * (withOperatorQuery): the app signs a GET only for the operator this Portal
 * is on, and only when the request says so.
 *
 * When the operator has not set Health up, the card explains what it needs:
 * an Android phone of their own with the BlackBox app and Health Connect
 * (iPhone / HealthKit is out of scope). Whether they own one comes from the
 * same mesh read the Devices section uses, as that operator.
 *
 * The markup is static in index.html (#wellnessSection); this module only sets
 * textContent, classes and `hidden` (the signed block, #wellnessSigned, is
 * built with createElement), so server text is never parsed as HTML.
 *
 * Polls every WELLNESS_POLL_MS while the System Menu is open (createPoller:
 * paused while the page is hidden, backing off while the box is unreachable).
 * "Use for health" in Devices pokes it through the `bbx:wellness-changed`
 * window event; an operator switch (`bbx:operator-changed`) re-asks at once.
 */

import { $, fetchJson, createPoller } from './core-utils.js';
import { getOperator, OPERATOR_REQUIRED_MESSAGE } from './state-management.js';

/** How often the card re-asks while the System Menu is open. */
export const WELLNESS_POLL_MS = 30000;

/** Where everything Health is managed, in the app's own words. */
export const PHONE_PATH = 'BlackBox, Settings, Health';

/** The request header the box checks (Orchestrator/wellness/origin_proof.py). */
export const ORIGIN_HEADER = 'X-BlackBox-Origin';

// A runaway label must not stretch the menu; the box writes "<name>'s <model> (<code>)".
const SOURCE_LABEL_MAX = 200;

// What the box sends for last_sync: Python's isoformat(), with or without a
// zone and fractional seconds.
const ISO_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

// A header value the page may send: printable ASCII, one line, bounded (the
// box's own parser takes at most 256 characters). Anything else from the
// bridge is no proof, never a fetch that throws on an invalid header.
const PROOF_VALUE = /^[\x21-\x7e][\x20-\x7e]{0,510}$/;

// The signed settings view (pickSignedStatus): only these words are named.
const CATEGORY_LABELS = { sleep: 'Sleep', heart: 'Heart', activity: 'Activity', blood_pressure: 'Blood pressure' };
const CONSENT_LABELS = { off: 'Off', ask: 'Ask', proactive: 'Proactive' };
// Orchestrator/wellness/destinations.py PROVIDER_LABELS; a custom server is its alias.
const PROVIDER_LABELS = { anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google', xai: 'xAI', local: 'this phone' };
const SURFACE_LABELS = {
    android: 'BlackBox app', portal_webview: 'Portal in the app', portal: 'Portal',
    cron: 'scheduled', voice: 'voice', on_device: 'on-device AI',
};
const FDE_METHODS = { luks: 'LUKS', 'dm-crypt': 'dm-crypt' };
// What a signed GET (/wellness/status, /fossil/snapshot/{id}) answers a proof
// it cannot verify (enroll.verify_signed_request): 401 bad_signature (an
// enrollment this box does not know, a stale key, a phone clock off by more
// than 2 min), 409 owner_changed, 410 revoked / source_moved. The box refuses
// a bad proof rather than downgrading it, and the phone only learns of it on
// its next push, so the reader asks again unsigned meanwhile.
const SIGNED_REFUSALS = new Set([401, 409, 410]);
const TEXT_MAX = 300;
const INSTINCT_MAX = 2000;      // the box's own cap on the instinct prompt
const TROUBLES_MAX = 12;
const DISCLOSURES_SHOWN = 50;

// Enrollment state -> chip label, chip tone and the next step. Only states in
// this table are ever named; anything else falls back to a fixed label, so a
// state string from the box never reaches the page as text.
const STATE_VIEWS = {
    awaiting_phone: {
        label: 'Finishing setup', tone: 'pending',
        note: `Finish setting up Health on your phone: open ${PHONE_PATH}.`,
    },
    active: { label: 'Active', tone: 'ok', note: `Manage on your phone: ${PHONE_PATH}.` },
    paused: { label: 'Paused', tone: 'muted', note: `Paused. Resume or manage it on your phone: ${PHONE_PATH}.` },
    stale: {
        label: 'Needs attention', tone: 'warn',
        note: `No new data has arrived lately. Open ${PHONE_PATH} on your phone to see why.`,
    },
    needs_attention: {
        label: 'Needs attention', tone: 'warn',
        note: `Something needs a look. Open ${PHONE_PATH} on your phone.`,
    },
    ownership_changed: {
        label: 'Needs attention', tone: 'warn',
        note: 'This phone is no longer assigned to you in Devices, so Health has stopped. '
            + 'Set Health up again on a phone you own.',
    },
    revoked: {
        label: 'Disconnected', tone: 'muted',
        note: `Health is disconnected. To use it again, set it up on your phone: ${PHONE_PATH}.`,
    },
};

// The states that mean "nothing is set up" (the box's word, or none at all).
const NOT_SET_UP_STATES = new Set(['', 'none', 'not_set_up', 'not_enrolled']);

const NOT_SET_UP = { label: 'Not set up', tone: 'muted' };
// Enrolled, in a state this Portal does not know yet (a newer box).
const UNKNOWN_ENROLLED = { label: 'Connected', tone: 'muted', note: `Manage on your phone: ${PHONE_PATH}.` };
const UNAVAILABLE = { label: 'Unavailable', tone: 'muted', note: 'Health is not available on this BlackBox yet.' };
const CHECKING = { label: 'Checking…', tone: 'muted' };
const NOT_FOR_OPERATOR = 'Health is not available for this operator.';

const knownState = (state) =>
    typeof state === 'string' && Object.prototype.hasOwnProperty.call(STATE_VIEWS, state);

/**
 * The chip label for an enrollment state, shared with the Devices badge.
 * @param {*} state - health_state / state from the box
 * @returns {string} The label, or '' for a state this Portal does not know
 */
export function healthStateLabel(state) {
    return knownState(state) ? STATE_VIEWS[state].label : '';
}

/**
 * The status reply cut down to the four fields an unapproved browser may show.
 * Everything else in the reply is dropped here, before anything is rendered.
 * @param {*} raw - The parsed GET /wellness/status body
 * @returns {{state: ?string, source_label: ?string, last_sync: ?string, enrolled: boolean}}
 */
export function pickStatus(raw) {
    const r = raw && typeof raw === 'object' ? raw : {};
    return {
        state: typeof r.state === 'string' ? r.state : null,
        source_label: typeof r.source_label === 'string' && r.source_label.trim()
            ? r.source_label.trim().slice(0, SOURCE_LABEL_MAX) : null,
        last_sync: typeof r.last_sync === 'string' && r.last_sync ? r.last_sync : null,
        enrolled: r.enrolled === true,
    };
}

/**
 * A time from the box (Python's isoformat()) as epoch ms.
 * ISO 8601 only: Date.parse alone is lenient (V8 reads a date out of
 * "<img src=x ...>"), and an unreadable time is unknown, not Jan 1.
 * @param {*} iso - ISO 8601 text
 * @returns {?number} null when missing or unreadable
 */
export function parseWellnessTime(iso) {
    if (typeof iso !== 'string' || !ISO_TIME.test(iso)) return null;
    const t = Date.parse(iso.replace(' ', 'T'));
    return Number.isFinite(t) ? t : null;
}

/**
 * "12 min ago"-style text for the last sync: relative for two days, then a date.
 * @param {?string} iso - ISO 8601 time from the box
 * @param {number} [now=Date.now()] - Injected clock (ms)
 * @returns {?string} null when the time is missing or unreadable
 */
export function formatLastSync(iso, now = Date.now()) {
    const t = parseWellnessTime(iso);
    if (t === null) return null;
    // A phone clock a little ahead of the box is "just now", not "in 5 min".
    const mins = Math.floor((now - t) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 48) return `${hours} h ago`;
    return new Date(t).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** `value` as one line of text, or '' (non-strings, blank, runaway lengths). */
function textOf(value, max = TEXT_MAX) {
    return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim().slice(0, max) : '';
}

/** A provider id from the box (destinations.py) in words: "Anthropic", a custom server's alias. */
export function providerLabel(id) {
    if (typeof id !== 'string' || !id) return '';
    if (Object.prototype.hasOwnProperty.call(PROVIDER_LABELS, id)) return PROVIDER_LABELS[id];
    if (id.startsWith('custom:')) return textOf(id.slice('custom:'.length), 80);
    return textOf(id, 80);
}

/**
 * The settings view a signed status read returns to the enrolled phone
 * (wellness_routes.full_status), cut down to what this card shows, or null
 * when the reply is not one (the four fields only, or junk). Categories and
 * consent words this Portal does not know are dropped, never named.
 * @param {*} raw - The parsed GET /wellness/status body
 * @returns {?{categories: Array<{label: string, consent: string, localOnly: boolean}>,
 *   providers: string[], troubles: Array<{title: string, body: string, fix: string}>,
 *   instinct: {text: string, isDefault: boolean}, fde: string}}
 */
export function pickSignedStatus(raw) {
    const r = raw && typeof raw === 'object' ? raw : null;
    if (!r || !r.categories || typeof r.categories !== 'object' || !Array.isArray(r.troubles)) return null;
    const localOnly = r.local_only && typeof r.local_only === 'object' ? r.local_only : {};
    const categories = Object.keys(CATEGORY_LABELS)
        .filter((c) => Object.prototype.hasOwnProperty.call(CONSENT_LABELS, r.categories[c]))
        .map((c) => ({ label: CATEGORY_LABELS[c], consent: CONSENT_LABELS[r.categories[c]], localOnly: localOnly[c] === true }));

    // Local and on-device destinations are always allowed (destinations.allowed_to);
    // each cloud provider is one the operator ticked on the phone.
    const providers = ['AI on your network'];
    for (const id of Array.isArray(r.providers_allowed) ? r.providers_allowed.slice(0, 20) : []) {
        const label = providerLabel(id);
        if (!label) continue;
        providers.push(id === 'google' && r.google_paid_key_confirmed !== true ? `${label} (needs a paid key)` : label);
    }

    const troubles = (Array.isArray(r.trouble_details) ? r.trouble_details : [])
        .filter((d) => d && typeof d === 'object' && textOf(d.title))
        .slice(0, TROUBLES_MAX)
        .map((d) => ({ title: textOf(d.title), body: textOf(d.body), fix: textOf(d.fix) }));

    const own = typeof r.instinct_prompt === 'string' && r.instinct_prompt.trim();
    const instinct = {
        text: (own ? r.instinct_prompt : (typeof r.instinct_prompt_default === 'string' ? r.instinct_prompt_default : ''))
            .trim().slice(0, INSTINCT_MAX),
        isDefault: !own,
    };
    return { categories, providers, troubles, instinct, fde: fdeLine(r.fde) };
}

/** The disk-encryption line of the settings view (Orchestrator/wellness/fde.py). */
function fdeLine(fde) {
    const f = fde && typeof fde === 'object' ? fde : {};
    // The store's own filesystem decides; the root's answers when the store's is unknown.
    const on = typeof f.store_encrypted === 'boolean' ? f.store_encrypted
        : (typeof f.root_encrypted === 'boolean' ? f.root_encrypted : null);
    if (on === true) {
        const method = Object.prototype.hasOwnProperty.call(FDE_METHODS, f.method) ? FDE_METHODS[f.method] : '';
        return method ? `Disk encryption: on (${method})` : 'Disk encryption: on';
    }
    if (on === false) return 'Disk encryption: off. The health store is a private file on an unencrypted disk.';
    return 'Disk encryption: could not be checked.';
}

/**
 * One "Who saw my health data" row (wellness_routes.disclosure_log) in words.
 * @param {*} row - {time, surface, provider, model, tool, categories}
 * @returns {string} '' for a row that is not one
 */
export function disclosureLine(row) {
    if (!row || typeof row !== 'object') return '';
    const t = parseWellnessTime(row.time);
    const when = t === null ? '' : new Date(t).toLocaleString(undefined,
        { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const where = Object.prototype.hasOwnProperty.call(SURFACE_LABELS, row.surface) ? SURFACE_LABELS[row.surface] : 'another surface';
    // A custom server's turn names provider "custom" and the model "<alias>::<model>".
    let label = providerLabel(row.provider);
    let model = textOf(row.model, 120);
    if (row.provider === 'custom' && model.includes('::')) {
        label = model.slice(0, model.indexOf('::'));
        model = model.slice(model.indexOf('::') + 2);
    }
    const who = label && model ? `${label} (${model})` : (label || model);
    const what = (Array.isArray(row.categories) ? row.categories : [])
        .map((c) => (Object.prototype.hasOwnProperty.call(CATEGORY_LABELS, c) ? CATEGORY_LABELS[c] : ''))
        .filter(Boolean).join(', ');
    return [when, where, who, what].filter(Boolean).join(' · ');
}

// -----------------------------------------------------------------------------
// Signing (the in-app WebView only)
// -----------------------------------------------------------------------------

/** The WebView channel the app answers sign requests on (WellnessChatRules.BRIDGE_CHANNEL). */
export const SIGN_CHANNEL = 'BlackBoxWellness';

/**
 * How long the app has to answer one sign request. It answers every
 * well-formed request exactly once and a malformed one never, so a request
 * that gets no answer in time goes out unsigned.
 */
export const SIGN_TIMEOUT_MS = 2000;

// The app reads at most this much of one request (WellnessChatRules
// MAX_BRIDGE_MESSAGE); a longer one would only time out.
const SIGN_MESSAGE_MAX = 2048;

/** The surface a signed chat body from the in-app Portal names (Orchestrator/wellness/turn.SURFACE_PORTAL_WEBVIEW). */
export const PORTAL_WEBVIEW_SURFACE = 'portal_webview';

/**
 * The app's request signer (spec §6.3): window.BlackBoxWellness, the message
 * channel the app puts in the paired box's main frame only, or null outside
 * the BlackBox Android app (a desktop browser, an iframe, an app older than
 * the channel).
 */
export function wellnessSigner() {
    const channel = typeof window !== 'undefined' ? window[SIGN_CHANNEL] : undefined;
    return channel && typeof channel.postMessage === 'function' ? channel : null;
}

/**
 * True for a status a signed read gets when the box would not take its proof
 * (401, 409, 410): the reader asks again, once, unsigned.
 * @param {*} status - The HTTP status
 */
export function isRefusedProof(status) {
    return SIGNED_REFUSALS.has(status);
}

/**
 * `path` naming `operator` once in its query, as the app requires of a GET it
 * signs (WellnessChatRules.bridgeOperator: exactly that operator, once). A
 * path that already names an operator, or no operator, is left as it is.
 * @param {string} path - e.g. "/wellness/disclosures"
 * @param {string} [operator=getOperator()]
 * @returns {string} e.g. "/wellness/disclosures?operator=x"
 */
export function withOperatorQuery(path, operator = getOperator()) {
    const p = String(path);
    if (!operator) return p;
    const q = p.indexOf('?');
    if (q >= 0 && new URLSearchParams(p.slice(q + 1)).has('operator')) return p;
    const sep = q < 0 ? '?' : (p.endsWith('?') || p.endsWith('&') ? '' : '&');
    return `${p}${sep}operator=${encodeURIComponent(operator)}`;
}

/**
 * The UTF-8 bytes fetch sends for a string body (a lone surrogate becomes
 * U+FFFD, as TextEncoder and fetch encode it).
 * @param {string} text
 * @param {?Function} [Encoder] - Injectable; the page's TextEncoder by default
 * @returns {Uint8Array}
 */
export function utf8Bytes(text, Encoder = globalThis.TextEncoder) {
    const s = String(text ?? '');
    if (typeof Encoder === 'function') return new Encoder().encode(s);
    const out = [];
    for (let i = 0; i < s.length; i++) {
        let c = s.charCodeAt(i);
        if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length && s.charCodeAt(i + 1) >= 0xdc00 && s.charCodeAt(i + 1) <= 0xdfff) {
            c = 0x10000 + ((c - 0xd800) << 10) + (s.charCodeAt(++i) - 0xdc00);
        } else if (c >= 0xd800 && c <= 0xdfff) {
            c = 0xfffd;
        }
        if (c < 0x80) out.push(c);
        else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
        else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
        else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 0x3f), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
    }
    return Uint8Array.from(out);
}

// FIPS 180-4 SHA-256 round constants.
const SHA256_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotr = (x, n) => (x >>> n) | (x << (32 - n));

const hexOf = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

/**
 * Lowercase hex SHA-256, computed by the page itself (FIPS 180-4). The
 * in-app Portal on a LAN or Tailscale IP is plain http, not a secure context,
 * so crypto.subtle does not exist there; sha256Hex falls back to this.
 * @param {string|Uint8Array} input - Text (hashed as its UTF-8 bytes) or bytes
 * @returns {string}
 */
export function sha256HexOf(input) {
    const bytes = ArrayBuffer.isView(input) ? new Uint8Array(input.buffer, input.byteOffset, input.byteLength) : utf8Bytes(input);
    const len = bytes.length;
    const total = Math.ceil((len + 9) / 64) * 64;      // the message, 0x80, zeros, the 64-bit bit length
    const block = new Uint8Array(total);
    block.set(bytes);
    block[len] = 0x80;
    const view = new DataView(block.buffer);
    view.setUint32(total - 8, Math.floor(len / 0x20000000));
    view.setUint32(total - 4, (len * 8) >>> 0);
    const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    const w = new Uint32Array(64);
    for (let off = 0; off < total; off += 64) {
        for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
        for (let i = 16; i < 64; i++) {
            const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
            const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
        }
        let [a, b, c, d, e, f, g, k] = h;
        for (let i = 0; i < 64; i++) {
            const t1 = (k + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + SHA256_K[i] + w[i]) >>> 0;
            const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
            k = g; g = f; f = e; e = (d + t1) >>> 0;
            d = c; c = b; b = a; a = (t1 + t2) >>> 0;
        }
        h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0;
        h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0; h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + k) >>> 0;
    }
    const out = new Uint8Array(32);
    const outView = new DataView(out.buffer);
    h.forEach((word, i) => outView.setUint32(i * 4, word));
    return hexOf(out);
}

/**
 * Lowercase hex SHA-256 of `text`'s UTF-8 bytes (what fetch sends for a string body).
 * SubtleCrypto when the page has it (a secure context), else the page's own
 * (sha256HexOf): the same digest either way.
 * @param {string} text - The exact body ('' for none)
 * @param {?SubtleCrypto} [subtle] - Injectable; the page's crypto.subtle by default
 * @returns {Promise<string>}
 */
export async function sha256Hex(text, subtle = globalThis.crypto && globalThis.crypto.subtle) {
    const bytes = utf8Bytes(text);
    if (subtle && typeof subtle.digest === 'function') {
        try {
            return hexOf(new Uint8Array(await subtle.digest('SHA-256', bytes)));
        } catch {
            // a SubtleCrypto that will not digest: the page's own does
        }
    }
    return sha256HexOf(bytes);
}

// Sign requests waiting for the app's answer: id -> settle(value).
const _waiting = new Map();
// Channels the reply listener is on (one listener per channel, for every request).
const _listening = new WeakSet();
let _signSeq = 0;

/** A fresh request id ([A-Za-z0-9_.:-]{1,64}, what the app echoes). */
function newSignId() {
    const random = new Uint8Array(6);
    if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
        globalThis.crypto.getRandomValues(random);       // available outside a secure context too
    } else {
        for (let i = 0; i < random.length; i++) random[i] = Math.floor(Math.random() * 256);
    }
    _signSeq += 1;
    return `w${_signSeq.toString(36)}.${hexOf(random)}`;
}

/** One `message` event from the app: {id, value} as JSON; anything else is ignored. */
function onSignReply(event) {
    let reply = event ? event.data : null;
    if (typeof reply === 'string') {
        try {
            reply = JSON.parse(reply);
        } catch {
            return;
        }
    }
    if (!reply || typeof reply !== 'object' || typeof reply.id !== 'string') return;
    const settle = _waiting.get(reply.id);
    if (settle) settle(reply.value);
}

function listenForReplies(channel) {
    if (_listening.has(channel)) return true;
    try {
        if (typeof channel.addEventListener === 'function') {
            channel.addEventListener('message', onSignReply);
        } else {
            const previous = channel.onmessage;
            channel.onmessage = function onmessage(event) {
                onSignReply(event);
                if (typeof previous === 'function') previous.call(this, event);
            };
        }
    } catch {
        return false;
    }
    _listening.add(channel);
    return true;
}

/**
 * The X-BlackBox-Origin value for ONE request, signed by the app, or null
 * (no channel, no answer within the timeout, the app refused, a value that
 * is no header). Asked fresh for every request: a proof carries a one-use
 * nonce and is never cached. The app gets the method, the path with its
 * query and the body's digest, never the body.
 * @param {string} method - HTTP method
 * @param {string} pathWithQuery - e.g. "/wellness/status?operator=x" (the box signs the path)
 * @param {string} [body=''] - The exact body string the request sends
 * @param {{timeoutMs?: number}} [opts] - timeoutMs: injectable for tests (SIGN_TIMEOUT_MS)
 * @returns {Promise<?string>}
 */
export async function originProof(method, pathWithQuery, body = '', { timeoutMs = SIGN_TIMEOUT_MS } = {}) {
    const channel = wellnessSigner();
    if (!channel || typeof body !== 'string') return null;
    const digest = await sha256Hex(body);
    const id = newSignId();
    const message = JSON.stringify({ id, method: String(method).toUpperCase(), path: String(pathWithQuery), sha256: digest });
    if (message.length > SIGN_MESSAGE_MAX || !listenForReplies(channel)) return null;
    const value = await new Promise((resolve) => {
        let timer = null;
        const settle = (v) => {
            if (timer !== null) clearTimeout(timer);
            _waiting.delete(id);
            resolve(v);
        };
        _waiting.set(id, settle);
        timer = setTimeout(() => settle(null), timeoutMs);
        try {
            channel.postMessage(message);
        } catch {
            console.warn('[Health] the app could not sign this request; sending it unsigned');
            settle(null);
        }
    });
    return typeof value === 'string' && PROOF_VALUE.test(value) ? value : null;
}

/**
 * `headers` with the origin proof added when the app signed this request (a
 * new object), else `headers` itself, unchanged.
 * @param {?Object} headers - The request's other headers
 * @param {string} method
 * @param {string} pathWithQuery
 * @param {string} [body='']
 * @param {{timeoutMs?: number}} [opts]
 * @returns {Promise<?Object>}
 */
export async function withOriginProof(headers, method, pathWithQuery, body = '', opts = {}) {
    const proof = await originProof(method, pathWithQuery, body, opts);
    return proof ? { ...(headers || {}), [ORIGIN_HEADER]: proof } : headers;
}

/**
 * A JSON chat POST (/chat/stream, /chat) as the in-app Portal sends it. When
 * the app signs it, the body gains "wellness_surface": "portal_webview" (the
 * box then labels the credentialed turn as the Portal inside the app; a
 * declaration without a valid proof is ignored) and the proof is over that
 * exact body. Unsigned (no channel, no answer, a null, a body that is not a
 * JSON object), the body and headers are exactly the ones given.
 * @param {string} path - "/chat/stream" or "/chat"
 * @param {string} body - The JSON body as it would go out unsigned
 * @param {Object} headers - Its headers
 * @param {{timeoutMs?: number}} [opts]
 * @returns {Promise<{headers: Object, body: string, signed: boolean}>}
 */
export async function signedChatRequest(path, body, headers, opts = {}) {
    const unsigned = { headers, body, signed: false };
    if (!wellnessSigner() || typeof body !== 'string') return unsigned;
    let labelled;
    try {
        const obj = JSON.parse(body);
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return unsigned;
        labelled = JSON.stringify({ ...obj, wellness_surface: PORTAL_WEBVIEW_SURFACE });
    } catch {
        return unsigned;
    }
    const proof = await originProof('POST', path, labelled, opts);
    return proof ? { headers: { ...(headers || {}), [ORIGIN_HEADER]: proof }, body: labelled, signed: true } : unsigned;
}

// -----------------------------------------------------------------------------
// Speech (rules W26, W31, W32)
// -----------------------------------------------------------------------------

/**
 * What every surface says instead of reading a health reply aloud: the
 * on-box voice writes the audio under Portal/uploads, every other voice is a
 * service the health settings never allowed.
 */
export const WELLNESS_SPEECH_OFF = "Health replies aren't read aloud: the voice service would get your health data.";

/**
 * True for the box's refusal of a text that recalls a health result of the
 * request's operator (POST /tts, /tts/batch, /tts/queue: 403
 * {"error": "wellness_text"}). Reads a copy of the body, and only of a 403,
 * so any other answer is left for its caller to read.
 * @param {?Response} response
 * @returns {Promise<boolean>}
 */
export async function isWellnessTextRefusal(response) {
    if (!response || response.status !== 403) return false;
    try {
        const copy = typeof response.clone === 'function' ? response.clone() : response;
        const body = await copy.json();
        const code = body && typeof body === 'object' ? body.error ?? (body.detail && body.detail.error) : null;
        return code === 'wellness_text';
    } catch {
        return false;
    }
}

// The phone label of the last status read, for its operator: the chat's
// "Used your health data" footer names it. Not persisted.
let _lastSource = { operator: null, label: null };

/**
 * The source label the card last read for `operator` (the box's
 * "<name>'s <model> (<code>)"), or null when it has not read one for them.
 * @param {string} [operator=getOperator()]
 * @returns {?string}
 */
export function lastSourceLabel(operator = getOperator()) {
    return operator && _lastSource.operator === operator ? _lastSource.label : null;
}

/** Which view a (picked) status gets. */
function viewFor(status) {
    if (status.state === 'disabled') return UNAVAILABLE;
    if (knownState(status.state)) return STATE_VIEWS[status.state];
    if (status.enrolled) return UNKNOWN_ENROLLED;
    return NOT_SET_UP;
}

/** The native Health screen, when this page runs inside the Android app (spec §6.3). */
function nativeOpener() {
    const bridge = typeof window !== 'undefined' ? window.AndroidBridge : undefined;
    return bridge && typeof bridge.wellnessOpenSettings === 'function' ? bridge : null;
}

// What to do when Health is not set up. `ownsAndroid` is null when the device
// list could not be read: the general steps cover both cases.
function setupGuide(ownsAndroid) {
    if (ownsAndroid === true) {
        return {
            note: 'Health is not set up for you yet.',
            steps: [
                'In Devices, choose Use for health on your phone.',
                `On that phone, open ${PHONE_PATH} and follow the steps. Health permissions are granted there.`,
            ],
            footnote: '',
        };
    }
    return {
        note: 'Health tracking needs the BlackBox Android app with Health Connect, on a phone that is yours.',
        steps: [
            'Install the BlackBox app on your Android phone and open it once, so it pairs with this BlackBox.',
            'In Devices, assign the phone to yourself and choose Use for health.',
            `Finish on the phone: ${PHONE_PATH}.`,
        ],
        footnote: 'iPhone and Apple Health are not supported.',
    };
}

// -----------------------------------------------------------------------------
// Rendering (textContent / hidden only)
// -----------------------------------------------------------------------------
function els() {
    return {
        section: $('wellnessSection'),
        chip: $('wellnessStateChip'),
        sourceRow: $('wellnessSourceRow'),
        source: $('wellnessSource'),
        syncRow: $('wellnessSyncRow'),
        lastSync: $('wellnessLastSync'),
        note: $('wellnessNote'),
        steps: $('wellnessSteps'),
        footnote: $('wellnessFootnote'),
        error: $('wellnessError'),
        openPhone: $('btnWellnessOpenPhone'),
    };
}

function setChip(e, view) {
    if (!e.chip) return;
    e.chip.textContent = view.label;
    e.chip.dataset.tone = view.tone;
}

function setSteps(e, steps) {
    if (!e.steps) return;
    e.steps.replaceChildren(...steps.map((text) => {
        const li = document.createElement('li');
        li.textContent = text;
        return li;
    }));
    e.steps.hidden = steps.length === 0;
}

function setText(el, text) {
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
}

function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
}

/**
 * The signed settings view's block (#wellnessSigned): built once, before the
 * "can't reach" line, and refilled on every signed read; null when it does
 * not exist and `create` is false.
 */
function signedBlock(e, create) {
    let block = $('wellnessSigned');
    if (block || !create || !e.section) return block;
    block = node('div', 'wellness-signed');
    block.id = 'wellnessSigned';
    block.hidden = true;
    const instinct = node('details', 'wellness-instinct');
    instinct.append(node('summary', '', 'Health instinct prompt'), node('p', 'wellness-instinct-text'));
    const disclosures = node('details', 'wellness-disclosures');
    disclosures.append(node('summary', '', 'Who saw my health data'),
        node('ul', 'wellness-list wellness-disclosure-rows'));
    disclosures.addEventListener('toggle', () => loadDisclosures(disclosures));
    block.append(
        node('div', 'wellness-troubles'),
        node('p', 'wellness-subhead wellness-uses-head', 'What BlackBox may use'),
        node('ul', 'wellness-list wellness-uses'),
        node('p', 'wellness-subhead', 'Where it may go'),
        node('ul', 'wellness-list wellness-providers'),
        node('p', 'wellness-fde'),
        instinct,
        disclosures,
        node('p', 'wellness-signed-note', `To change any of this, open ${PHONE_PATH} on your phone.`),
    );
    e.section.insertBefore(block, e.error && e.error.parentNode === e.section ? e.error : null);
    return block;
}

/** Fill the signed block from a picked settings view, or empty and hide it (null). */
function renderSigned(e, view) {
    const block = signedBlock(e, !!view);
    if (!block) return;
    const q = (sel) => block.querySelector(sel);
    const disclosures = q('.wellness-disclosures');
    if (!view) {
        block.hidden = true;
        for (const sel of ['.wellness-troubles', '.wellness-uses', '.wellness-providers', '.wellness-disclosure-rows']) {
            q(sel).replaceChildren();
        }
        q('.wellness-fde').textContent = '';
        q('.wellness-instinct-text').textContent = '';
        q('.wellness-instinct').open = false;
        disclosures.open = false;
        disclosures.dataset.state = '';
        return;
    }
    q('.wellness-troubles').replaceChildren(...view.troubles.map((t) => {
        const card = node('div', 'wellness-trouble');
        card.append(node('p', 'wellness-trouble-title', t.title));
        if (t.body) card.append(node('p', 'wellness-trouble-body', t.body));
        // The fix names where it is done ("Open BlackBox on your phone").
        if (t.fix) card.append(node('p', 'wellness-trouble-fix', `To fix: ${t.fix}`));
        return card;
    }));
    q('.wellness-uses').replaceChildren(...view.categories.map((c) => node('li', '',
        `${c.label}: ${c.consent}${c.localOnly && c.consent !== 'Off' ? ', local AI only' : ''}`)));
    q('.wellness-uses-head').hidden = view.categories.length === 0;
    q('.wellness-uses').hidden = view.categories.length === 0;
    q('.wellness-providers').replaceChildren(...view.providers.map((p) => node('li', '', p)));
    q('.wellness-fde').textContent = view.fde;
    q('.wellness-instinct summary').textContent = view.instinct.isDefault
        ? 'Health instinct prompt (the default)' : 'Health instinct prompt';
    q('.wellness-instinct-text').textContent = view.instinct.text || 'None';
    block.hidden = false;
}

/**
 * "Who saw my health data", read when it is opened: a signed GET
 * /wellness/disclosures for the current operator, never cached; a refusal
 * (403 from a phone that is not the active one, 404, no proof) is "Not
 * available". Once per opening.
 */
async function loadDisclosures(details) {
    const list = details.querySelector('.wellness-disclosure-rows');
    if (!details.open) {
        details.dataset.state = '';
        return;
    }
    if (details.dataset.state) return;          // loading, or loaded for this opening
    details.dataset.state = 'loading';
    const operator = getOperator();
    list.replaceChildren(node('li', 'wellness-muted', 'Loading…'));
    let rows = null;
    let message = 'Not available.';
    // The app signs a GET only when it names this Portal's operator.
    const path = withOperatorQuery('/wellness/disclosures', operator);
    try {
        const proof = operator ? await originProof('GET', path) : null;
        if (proof) {
            const data = await fetchJson(path, { cache: 'no-store', headers: { [ORIGIN_HEADER]: proof } });
            rows = Array.isArray(data && data.disclosures) ? data.disclosures : [];
        }
    } catch (err) {
        const code = err && err.status;
        if (!(code >= 400 && code < 500)) message = "Couldn't load it right now. Close and open it to try again.";
    }
    if (operator !== getOperator() || !details.open || details.dataset.state !== 'loading') return;
    if (rows === null) {
        list.replaceChildren(node('li', 'wellness-muted', message));
        details.dataset.state = message === 'Not available.' ? 'loaded' : '';
        return;
    }
    const lines = rows.map(disclosureLine).filter(Boolean).slice(0, DISCLOSURES_SHOWN);
    list.replaceChildren(...(lines.length
        ? lines.map((line) => node('li', '', line))
        : [node('li', 'wellness-muted', 'No AI has read your health data yet.')]));
    details.dataset.state = 'loaded';
}

/** A plain message with no status: no operator yet, the box lacks the routes, or a switch is in progress. */
function renderMessage(view, note) {
    const e = els();
    if (!e.section) return;
    setChip(e, view);
    if (e.sourceRow) e.sourceRow.hidden = true;
    if (e.syncRow) e.syncRow.hidden = true;
    setText(e.note, note);
    setSteps(e, []);
    setText(e.footnote, '');
    if (e.error) e.error.hidden = true;
    if (e.openPhone) e.openPhone.hidden = true;
    renderSigned(e, null);
}

function renderStatus(status, ownsAndroid, now, settings = null) {
    const e = els();
    if (!e.section) return;
    const view = viewFor(status);
    setChip(e, view);

    const setUp = view !== NOT_SET_UP && view !== UNAVAILABLE;
    if (e.sourceRow && e.source) {
        e.source.textContent = setUp && status.source_label ? status.source_label : '';
        e.sourceRow.hidden = !(setUp && status.source_label);
    }
    if (e.syncRow && e.lastSync) {
        e.lastSync.textContent = formatLastSync(status.last_sync, now) || 'Not synced yet';
        e.syncRow.hidden = !setUp;
    }

    if (view === NOT_SET_UP) {
        const guide = setupGuide(ownsAndroid);
        setText(e.note, guide.note);
        setSteps(e, guide.steps);
        setText(e.footnote, guide.footnote);
    } else {
        setText(e.note, view.note);
        setSteps(e, []);
        setText(e.footnote, '');
    }
    if (e.error) e.error.hidden = true;
    if (e.openPhone) e.openPhone.hidden = !(nativeOpener() && view !== UNAVAILABLE);
    renderSigned(e, setUp ? settings : null);
}

function showUnreachable() {
    const e = els();
    if (e.error) e.error.hidden = false;
}

// -----------------------------------------------------------------------------
// Controller
// -----------------------------------------------------------------------------
let _controller = null;

/**
 * Wire the Health card: poll while the System Menu is open, re-ask on an
 * operator switch or a Devices pick. Safe to call once per page; a second call
 * returns the running controller.
 * @param {object} [opts]
 * @param {number} [opts.intervalMs=WELLNESS_POLL_MS]
 * @param {{setTimeout: Function, clearTimeout: Function}} [opts.timers] - Injectable for tests
 * @param {() => number} [opts.now] - Injectable clock (ms) for the "last sync" text
 * @returns {{refresh: () => void, destroy: () => void}}
 */
export function initWellnessSection({ intervalMs = WELLNESS_POLL_MS, timers = null, now = () => Date.now() } = {}) {
    if (_controller) return _controller;
    if (!$('wellnessSection')) return { refresh() {}, destroy() {} };

    // operator -> owns an Android phone (true/false), or null when the device
    // list could not be read. Cleared each time the menu opens and on a switch.
    const owns = new Map();

    async function ownsAndroidPhone(operator) {
        if (owns.has(operator)) return owns.get(operator);
        let answer = null;
        try {
            const data = await fetchJson(`/devices/mesh?operator=${encodeURIComponent(operator)}`);
            const rows = Array.isArray(data && data.devices) ? data.devices : [];
            answer = rows.some((d) => d && d.owner === operator
                && String(d.type || '').toLowerCase() === 'android');
        } catch {
            answer = null;
        }
        owns.set(operator, answer);
        return answer;
    }

    /**
     * One status read for `operator`; false when the operator changed under it.
     * @param {string} operator
     * @param {{unsigned?: boolean}} [opts] - unsigned: never ask the app for a proof
     */
    async function readFor(operator, { unsigned = false } = {}) {
        const path = `/wellness/status?operator=${encodeURIComponent(operator)}`;
        const init = { cache: 'no-store' };
        // Inside the app: signed by the phone, so the box answers with the
        // settings view when this is its enrolled operator. Only a read that
        // went out signed may show it.
        let signed = false;
        if (!unsigned && wellnessSigner()) {
            const proof = await originProof('GET', path);
            if (operator !== getOperator()) return false;
            if (proof) {
                init.headers = { [ORIGIN_HEADER]: proof };
                signed = true;
            }
        }
        let raw;
        try {
            raw = await fetchJson(path, init);
        } catch (err) {
            if (operator !== getOperator()) return false;
            const code = err && err.status;
            // A proof the box would not take (a retired or unknown key, a
            // phone clock off, the phone given to someone else) is no reason
            // to show less than a desktop browser does: the same read, once,
            // unsigned, drawn as the four-field card (its state names the
            // trouble, e.g. ownership_changed or revoked).
            if (signed && SIGNED_REFUSALS.has(code)) return readFor(operator, { unsigned: true });
            if (code === 404) {
                renderMessage(UNAVAILABLE, UNAVAILABLE.note);
                return true;
            }
            // A refusal (an operator the box will not track, a bad name) is an
            // answer, not an outage: say so, without the box's wording.
            if (code >= 400 && code < 500 && code !== 408 && code !== 429) {
                renderMessage(UNAVAILABLE, NOT_FOR_OPERATOR);
                return true;
            }
            showUnreachable();
            throw err;   // createPoller backs off
        }
        if (operator !== getOperator()) return false;
        const status = pickStatus(raw);
        const settings = signed ? pickSignedStatus(raw) : null;
        const ownsAndroid = viewFor(status) === NOT_SET_UP ? await ownsAndroidPhone(operator) : null;
        if (operator !== getOperator()) return false;
        renderStatus(status, ownsAndroid, now(), settings);
        _lastSource = { operator, label: status.source_label };
        return true;
    }

    async function tick() {
        // An answer for an operator who is no longer selected is dropped and
        // the current one asked at once (bounded, in case of a flapping picker).
        for (let i = 0; i < 3; i++) {
            const operator = getOperator();
            if (!operator) {
                renderMessage(NOT_SET_UP, OPERATOR_REQUIRED_MESSAGE);
                return;
            }
            if (await readFor(operator)) return;
        }
    }

    const poller = createPoller(tick, { intervalMs, timers });

    const menu = document.getElementById('menuModal');
    let menuOpen = false;
    const syncToMenu = () => {
        const open = !menu || !menu.classList.contains('hide');
        if (open && !menuOpen) {
            owns.clear();
            poller.start();
        } else if (!open && menuOpen) {
            poller.stop();
        }
        menuOpen = open;
    };
    const observer = menu ? new MutationObserver(syncToMenu) : null;
    if (observer) observer.observe(menu, { attributes: true, attributeFilter: ['class'] });

    const onOperatorChanged = () => {
        owns.clear();
        renderMessage(CHECKING, '');
        poller.poke();
    };
    const onWellnessChanged = () => poller.poke();
    window.addEventListener('bbx:operator-changed', onOperatorChanged);
    window.addEventListener('bbx:wellness-changed', onWellnessChanged);

    const openPhone = $('btnWellnessOpenPhone');
    const onOpenPhone = () => {
        const bridge = nativeOpener();
        try {
            if (bridge) bridge.wellnessOpenSettings();
        } catch (err) {
            console.warn('[Health] could not open the native Health screen', err);
        }
    };
    if (openPhone) openPhone.addEventListener('click', onOpenPhone);

    syncToMenu();

    _controller = {
        refresh: () => poller.poke(),
        destroy() {
            poller.stop();
            renderSigned(els(), null);
            if (observer) observer.disconnect();
            window.removeEventListener('bbx:operator-changed', onOperatorChanged);
            window.removeEventListener('bbx:wellness-changed', onWellnessChanged);
            if (openPhone) openPhone.removeEventListener('click', onOpenPhone);
            _controller = null;
        },
    };
    console.log('[Health] System-Menu Health card initialized');
    return _controller;
}

export default { initWellnessSection };
