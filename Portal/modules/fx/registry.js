/**
 * fx/registry.js — the effect registry for the chat backdrop particle field.
 *
 * Before this module the engine had a hard-coded `const DRAW = {stars, embers,
 * matrix}` dispatch table and the SAME three-id whitelist written out three more
 * times (loadParticleMode, setParticleMode, the window.EmberFX surface). Adding
 * an effect meant editing five call sites and forgetting one of them silently
 * produced an effect you could select but not persist.
 *
 * Now: one Map, one whitelist derived from it, one dispatch.
 *
 * Two rules the engine — never an effect — is responsible for, because getting
 * them wrong is invisible until it is shipped:
 *
 *   clearPolicy   How the previous frame is disposed of: a hard clear, a
 *                 translucent fade (trails/smear), or nothing. Applied by
 *                 applyClearPolicy() so that no effect ever touches
 *                 globalCompositeOperation itself.
 *   blend         PER-EFFECT compositing. Additive ('lighter') is right for
 *                 emissive fields (embers) and WRONG for non-emissive ones
 *                 (snow, fog, bokeh) which blow out to white and destroy the
 *                 contrast of the chat text sitting on top. This has to exist
 *                 BEFORE the first non-emissive effect, not after.
 *
 * Lazy loading: an entry may register as a STUB ({id, label, load}) and be
 * completed later by the effect module itself calling registerEffect() again
 * with the same id. That is what makes `import()` on selection possible while
 * still giving the settings UI and the persistence whitelist a complete,
 * synchronous catalogue at load time.
 */

/** id -> descriptor. The single source of truth for "which effects exist". */
export const EFFECTS = new Map();

/** Fallback for an unknown/absent persisted id. */
// Ledger Rain is the shipped default (Brandon 2026-07-25: "it fits the black box aesthetic perfectly").
// It is a DEFAULT, not a lock-in — any operator selection persists and wins, and an existing install keeps whatever it already had, since a stored value is only ever replaced by the user.
export const DEFAULT_EFFECT_ID = 'ledger';

const NOOP = () => {};

/** Valid `blend` values (Canvas2D globalCompositeOperation subset we allow). */
const BLENDS = new Set(['source-over', 'lighter', 'screen', 'multiply', 'overlay', 'destination-out']);

function defaultClock() {
    return (typeof performance !== 'undefined' && typeof performance.now === 'function')
        ? performance.now()
        : Date.now();
}

/**
 * Parse a clearPolicy string into something the engine can act on.
 * @param {string} policy 'clear' | 'fade:<css-color>' | 'none'
 * @returns {{kind:'clear'|'fade'|'none', color:(string|null)}}
 */
export function parseClearPolicy(policy) {
    if (policy === 'clear' || policy === undefined || policy === null) return { kind: 'clear', color: null };
    if (policy === 'none') return { kind: 'none', color: null };
    if (typeof policy === 'string' && policy.startsWith('fade:')) {
        const color = policy.slice(5).trim();
        if (color) return { kind: 'fade', color };
    }
    throw new Error(`[fx] invalid clearPolicy: ${JSON.stringify(policy)} (want 'clear' | 'fade:<color>' | 'none')`);
}

/**
 * Register (or complete) an effect.
 *
 * Merge semantics: keys actually present on `descriptor` overwrite the stored
 * entry; everything else is left alone. So a `{id, label, load}` stub can be
 * upgraded in place by the lazily-imported module's full descriptor without the
 * module having to restate the catalogue metadata, and vice versa.
 *
 * @param {object} descriptor {id, label, blend, clearPolicy, init, resize, update, draw, rearm, load}
 * @returns {object} the stored descriptor
 */
export function registerEffect(descriptor) {
    if (!descriptor || typeof descriptor !== 'object') throw new Error('[fx] registerEffect: descriptor required');
    const id = descriptor.id;
    if (typeof id !== 'string' || !id) throw new Error('[fx] registerEffect: descriptor.id must be a non-empty string');
    if (descriptor.clearPolicy !== undefined) parseClearPolicy(descriptor.clearPolicy);   // throws on typo, at registration
    if (descriptor.blend !== undefined && !BLENDS.has(descriptor.blend)) {
        throw new Error(`[fx] registerEffect(${id}): unsupported blend ${JSON.stringify(descriptor.blend)}`);
    }

    const stored = EFFECTS.get(id) || { id };
    for (const key of Object.keys(descriptor)) {
        if (descriptor[key] !== undefined) stored[key] = descriptor[key];
    }
    // Defaults for anything still missing — never overwrite what was supplied.
    if (stored.label === undefined) stored.label = id;
    if (stored.blend === undefined) stored.blend = 'source-over';
    if (stored.clearPolicy === undefined) stored.clearPolicy = 'clear';
    for (const hook of ['init', 'resize', 'rearm']) if (typeof stored[hook] !== 'function') stored[hook] = NOOP;
    if (typeof stored.update !== 'function') stored.update = NOOP;
    if (typeof stored.draw !== 'function') stored.draw = NOOP;

    EFFECTS.set(id, stored);
    return stored;
}

/** @returns {object|undefined} */
export function getEffect(id) { return EFFECTS.get(id); }

/** THE whitelist. Nothing else may hard-code one. */
export function hasEffect(id) { return EFFECTS.has(id); }

/** @returns {string[]} registration order */
export function effectIds() { return [...EFFECTS.keys()]; }

/** Catalogue for a settings picker — available WITHOUT loading any effect module. */
export function effectList() { return [...EFFECTS.values()].map(e => ({ id: e.id, label: e.label })); }

/** Coerce a persisted/user-supplied id onto a real one. */
export function resolveEffectId(id) { return EFFECTS.has(id) ? id : DEFAULT_EFFECT_ID; }

/**
 * Load an effect's simulation module on demand (memoized per id).
 * A stub carries `load: () => import('./effects/<id>.js')`; the module registers
 * its full descriptor as a side effect of being imported.
 * @returns {Promise<object>} the completed descriptor
 */
export function loadEffect(id) {
    const wanted = resolveEffectId(id);
    const entry = EFFECTS.get(wanted);
    if (!entry) return Promise.reject(new Error(`[fx] no effect registered for '${wanted}'`));
    if (typeof entry.load !== 'function') return Promise.resolve(entry);   // already complete
    if (!entry._loading) {
        entry._loading = Promise.resolve()
            .then(() => entry.load())
            .then(() => EFFECTS.get(wanted))
            .catch(err => { entry._loading = null; throw err; });          // let a transient failure retry
    }
    return entry._loading;
}

/**
 * Build the environment handed to every effect hook.
 *
 * `rand` and `clock` are INJECTED rather than reached for as globals so effect
 * physics can be driven headlessly from a unit test with a seeded generator and
 * a fake clock — matching Android, which already has ParticleFieldTest.
 * `state` is the effect's own scratch space; the engine never reads it.
 */
export function createEnv(overrides = {}) {
    return {
        width: 0,
        height: 0,
        dpr: 1,
        now: 0,          // frame timestamp in ms (rAF timestamp in the browser)
        dt: 0,           // seconds since the previous frame, already clamped
        active: true,    // generation in progress => effects may keep spawning
        scale: 1,        // viewport-area * density multiplier (see ember-fx fieldScale)
        intensity: 1,
        surge: 0,        // load reactivity; no driver today (v1 locked: fields only)
        sprites: null,   // {ember: HTMLCanvasElement[], star: HTMLCanvasElement}
        rand: Math.random,
        clock: defaultClock,
        state: {},
        ...overrides,
    };
}

/**
 * Dispose of the previous frame according to the effect's declared policy.
 * Always runs under source-over: a fade drawn under 'lighter' would ADD light
 * instead of veiling it, which is the classic "why is my trail glowing" bug.
 */
export function applyClearPolicy(ctx, descriptor, env) {
    const { kind, color } = parseClearPolicy(descriptor && descriptor.clearPolicy);
    if (kind === 'none') return;
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    if (kind === 'clear') {
        ctx.clearRect(0, 0, env.width, env.height);
    } else {
        ctx.fillStyle = color;
        ctx.fillRect(0, 0, env.width, env.height);
    }
}

/** Select the effect's compositing mode. Effects never do this themselves. */
export function applyBlend(ctx, descriptor) {
    ctx.globalCompositeOperation = (descriptor && descriptor.blend) || 'source-over';
}

/** Hand the context back in a known-neutral state after an effect has drawn. */
export function resetBlend(ctx) {
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
}
