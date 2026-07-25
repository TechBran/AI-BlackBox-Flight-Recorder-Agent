/**
 * fx/tuning.js — the particle field's two USER dials, and the only place their
 * defaults, bounds, storage keys and coercion rules are written down.
 *
 *   density  ("Intensity" in the UI)  a free multiplier on env.scale, i.e. on
 *            how many particles a viewport is worth. 1.0 is the shipped look.
 *   quality  (Low / High / Ultra)     a second multiplier on the same axis, so
 *            a weak device degrades gracefully instead of dropping frames.
 *            High is 1.0 — the default is today's behaviour EXACTLY.
 *
 * Both land on ONE number, countMultiplier(), which ember-fx folds into
 * fieldScale(). Effects therefore need no knowledge of either dial: they already
 * read env.scale.
 *
 * Why this is a module and not four lines inside ember-fx.js: every value that
 * arrives here has been through localStorage, an <input type=range> (whose
 * .value is a STRING), or a caller's object literal. `parseFloat("")` is NaN,
 * and a NaN multiplier silently produces a field with zero particles that looks
 * exactly like "the feature is broken". Every entry point is funnelled through
 * clampDensity()/resolveQuality(), which are total functions: they return a
 * usable value for absolutely any input, including undefined, null, "", "abc",
 * Infinity, -5 and {}. Tested in tuning.test.mjs.
 *
 * Storage is injectable (and optional) so the tests never touch a real
 * localStorage and so a browser in private mode — where reading throws — falls
 * back to the defaults instead of taking the settings panel down with it.
 */

/** localStorage keys. Siblings of bb_ember_mode / bb_particle_mode. */
export const STORAGE_KEYS = {
    density: 'bb_particle_density',
    quality: 'bb_particle_quality',
};

/** Density bounds. The floor is deliberately > 0: an effect scaled to zero is
 *  indistinguishable from a bug, and "off" already has its own control. */
export const DENSITY_MIN = 0.3;
export const DENSITY_MAX = 2.0;
export const DENSITY_STEP = 0.05;
export const DENSITY_DEFAULT = 1.0;

/** Quality tiers. `multiplier` is a particle-COUNT multiplier; High must stay
 *  1.0 so the default is byte-for-byte the shipped field. */
export const QUALITY_TIERS = [
    { id: 'low', label: 'Low', multiplier: 0.55 },
    { id: 'high', label: 'High', multiplier: 1.0 },
    { id: 'ultra', label: 'Ultra', multiplier: 1.5 },
];
export const QUALITY_DEFAULT = 'high';

/** Final clamp on the combined multiplier, so no combination of dials can hand
 *  an effect a scale it was never tuned against. */
export const SCALE_MIN = 0.15;
export const SCALE_MAX = 3.6;

/** @returns {{density:number, quality:string}} the shipped baseline. */
export function defaultTuning() {
    return { density: DENSITY_DEFAULT, quality: QUALITY_DEFAULT };
}

/**
 * Coerce anything at all into a usable density.
 * @param {*} value number | numeric string | garbage
 * @returns {number} within [DENSITY_MIN, DENSITY_MAX]; DENSITY_DEFAULT for
 *          absent/unparseable input. NEVER NaN.
 */
export function clampDensity(value) {
    const n = typeof value === 'string' ? parseFloat(value) : value;
    if (typeof n !== 'number' || !Number.isFinite(n)) return DENSITY_DEFAULT;
    const clamped = Math.max(DENSITY_MIN, Math.min(DENSITY_MAX, n));
    return Math.round(clamped * 100) / 100;   // kill float dust from the slider
}

/** @returns {string} a real tier id; QUALITY_DEFAULT for anything unknown. */
export function resolveQuality(id) {
    return QUALITY_TIERS.some(t => t.id === id) ? id : QUALITY_DEFAULT;
}

/** @returns {number} the particle-count multiplier for a tier id. */
export function qualityMultiplier(id) {
    const tier = QUALITY_TIERS.find(t => t.id === resolveQuality(id));
    return tier.multiplier;
}

/** Catalogue for the settings picker — the ONLY list of tiers. */
export function qualityList() {
    return QUALITY_TIERS.map(({ id, label }) => ({ id, label }));
}

/** @returns {{density:number, quality:string}} a sanitized copy of anything. */
export function normalizeTuning(tuning) {
    const t = (tuning && typeof tuning === 'object') ? tuning : {};
    return { density: clampDensity(t.density), quality: resolveQuality(t.quality) };
}

/**
 * The one number the engine consumes.
 * @returns {number} density x quality, clamped to [SCALE_MIN, SCALE_MAX].
 */
export function countMultiplier(tuning) {
    const t = normalizeTuning(tuning);
    const raw = t.density * qualityMultiplier(t.quality);
    return Math.max(SCALE_MIN, Math.min(SCALE_MAX, raw));
}

/** Resolve a storage-like object; null when there isn't one (SSR / node tests). */
function resolveStorage(storage) {
    if (storage) return storage;
    try {
        return (typeof localStorage !== 'undefined') ? localStorage : null;
    } catch (e) { return null; }   // some browsers THROW on the property access itself
}

/**
 * Read the persisted dials. Never throws, never returns NaN.
 * @param {Storage|object} [storage] defaults to window.localStorage
 */
export function loadTuning(storage) {
    const store = resolveStorage(storage);
    if (!store) return defaultTuning();
    let density, quality;
    try {
        density = store.getItem(STORAGE_KEYS.density);
        quality = store.getItem(STORAGE_KEYS.quality);
    } catch (e) { return defaultTuning(); }   // private mode / disabled storage
    return normalizeTuning({ density, quality });
}

/**
 * Persist the dials (already sanitized on the way in). Never throws.
 * @returns {{density:number, quality:string}} what was actually stored
 */
export function saveTuning(tuning, storage) {
    const t = normalizeTuning(tuning);
    const store = resolveStorage(storage);
    if (store) {
        try {
            store.setItem(STORAGE_KEYS.density, String(t.density));
            store.setItem(STORAGE_KEYS.quality, t.quality);
        } catch (e) { /* private mode: keep the in-memory value, drop the write */ }
    }
    return t;
}

/** Human-readable density, for the slider's <output>. */
export function formatDensity(value) {
    return Math.round(clampDensity(value) * 100) + '%';
}
