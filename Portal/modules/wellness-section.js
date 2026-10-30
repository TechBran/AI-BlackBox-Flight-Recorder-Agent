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
 * Inside the BlackBox Android app (spec §6.3) the WebView's
 * AndroidBridge.wellnessSign(method, pathWithQuery, bodySha256Hex) returns
 * the X-BlackBox-Origin value for one request, signed on the phone with its
 * device key (the key never enters JavaScript), or null. The status read is
 * then signed, and the settings view the box returns to the enrolled phone is
 * shown read-only (pickSignedStatus): what BlackBox may use, where it may go,
 * the trouble cards with their fix, the instinct prompt and the
 * disk-encryption line, and, when opened, "Who saw my health data" from a
 * signed GET /wellness/disclosures. Changing any of it stays on the phone
 * (narrowing from the Portal is M7.3). An unsigned read (no bridge, an older
 * app without wellnessSign, or a null from it) shows the four fields only.
 * originProof / withOriginProof are the one signing path the chat uses too:
 * a fresh proof per request, over the sha256 of the exact UTF-8 body bytes.
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

/** The app's request signer (spec §6.3), or null outside the BlackBox Android app. */
export function wellnessSigner() {
    const bridge = typeof window !== 'undefined' ? window.AndroidBridge : undefined;
    return bridge && typeof bridge.wellnessSign === 'function' ? bridge : null;
}

/**
 * Lowercase hex SHA-256 of `text`'s UTF-8 bytes (what fetch sends for a string body).
 * @param {string} text - The exact body ('' for none)
 * @param {?SubtleCrypto} [subtle] - Injectable; the page's crypto.subtle by default
 * @returns {Promise<?string>} null when SubtleCrypto is unavailable (not a secure context) or fails
 */
export async function sha256Hex(text, subtle = globalThis.crypto && globalThis.crypto.subtle) {
    if (!subtle || typeof subtle.digest !== 'function' || typeof TextEncoder !== 'function') return null;
    try {
        const digest = await subtle.digest('SHA-256', new TextEncoder().encode(String(text ?? '')));
        return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
    } catch {
        return null;
    }
}

/**
 * The X-BlackBox-Origin value for ONE request, signed by the app, or null
 * (no bridge, no SubtleCrypto, the bridge refused or failed). Asked fresh
 * for every request: a proof carries a one-use nonce and is never cached.
 * @param {string} method - HTTP method
 * @param {string} pathWithQuery - e.g. "/wellness/status?operator=x" (the box signs the path)
 * @param {string} [body=''] - The exact body string the request sends
 * @returns {Promise<?string>}
 */
export async function originProof(method, pathWithQuery, body = '') {
    const signer = wellnessSigner();
    if (!signer || typeof body !== 'string') return null;
    const digest = await sha256Hex(body);
    if (!digest) return null;
    let value = null;
    try {
        value = signer.wellnessSign(String(method).toUpperCase(), String(pathWithQuery), digest);
    } catch {
        console.warn('[Health] the app could not sign this request; sending it unsigned');
        return null;
    }
    return typeof value === 'string' && PROOF_VALUE.test(value) ? value : null;
}

/**
 * `headers` with the origin proof added when the app signed this request (a
 * new object), else `headers` itself, unchanged.
 * @param {?Object} headers - The request's other headers
 * @param {string} method
 * @param {string} pathWithQuery
 * @param {string} [body='']
 * @returns {Promise<?Object>}
 */
export async function withOriginProof(headers, method, pathWithQuery, body = '') {
    const proof = await originProof(method, pathWithQuery, body);
    return proof ? { ...(headers || {}), [ORIGIN_HEADER]: proof } : headers;
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
        if (t.fix) card.append(node('p', 'wellness-trouble-fix', `On your phone: ${t.fix}`));
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
    try {
        const proof = await originProof('GET', '/wellness/disclosures');
        if (proof) {
            const data = await fetchJson('/wellness/disclosures', { cache: 'no-store', headers: { [ORIGIN_HEADER]: proof } });
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

    /** One status read for `operator`; false when the operator changed under it. */
    async function readFor(operator) {
        const path = `/wellness/status?operator=${encodeURIComponent(operator)}`;
        const init = { cache: 'no-store' };
        // Inside the app: signed by the phone, so the box answers with the
        // settings view when this is its enrolled operator. Only a read that
        // went out signed may show it.
        let signed = false;
        if (wellnessSigner()) {
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
