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
 * When the operator has not set Health up, the card explains what it needs:
 * an Android phone of their own with the BlackBox app and Health Connect
 * (iPhone / HealthKit is out of scope). Whether they own one comes from the
 * same mesh read the Devices section uses, as that operator.
 *
 * The markup is static in index.html (#wellnessSection); this module only sets
 * textContent, classes and `hidden`, so server text is never parsed as HTML.
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

// A runaway label must not stretch the menu; the box writes "<name>'s <model> (<code>)".
const SOURCE_LABEL_MAX = 200;

// What the box sends for last_sync: Python's isoformat(), with or without a
// zone and fractional seconds.
const ISO_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

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
 * "12 min ago"-style text for the last sync: relative for two days, then a date.
 * @param {?string} iso - ISO 8601 time from the box
 * @param {number} [now=Date.now()] - Injected clock (ms)
 * @returns {?string} null when the time is missing or unreadable
 */
export function formatLastSync(iso, now = Date.now()) {
    // ISO 8601 only: Date.parse alone is lenient (V8 reads a date out of
    // "<img src=x ...>"), and an unreadable time is "not synced yet", not Jan 1.
    if (typeof iso !== 'string' || !ISO_TIME.test(iso)) return null;
    const t = Date.parse(iso.replace(' ', 'T'));
    if (!Number.isFinite(t)) return null;
    // A phone clock a little ahead of the box is "just now", not "in 5 min".
    const mins = Math.floor((now - t) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 48) return `${hours} h ago`;
    return new Date(t).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
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
}

function renderStatus(status, ownsAndroid, now) {
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
        let raw;
        try {
            raw = await fetchJson(`/wellness/status?operator=${encodeURIComponent(operator)}`, { cache: 'no-store' });
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
        const ownsAndroid = viewFor(status) === NOT_SET_UP ? await ownsAndroidPhone(operator) : null;
        if (operator !== getOperator()) return false;
        renderStatus(status, ownsAndroid, now());
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
