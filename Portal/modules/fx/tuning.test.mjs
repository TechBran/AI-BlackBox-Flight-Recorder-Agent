// Tuning tests — the two user dials (Intensity / Quality) and their persistence.
//
// The whole point of tuning.js is that a bad value can never reach the engine:
// every one of these inputs used to be capable of producing `env.scale = NaN`,
// which renders an EMPTY field and is indistinguishable from "the particle
// feature is broken". So the contract under test is total: any input at all
// yields a usable number, and an absent/garbage localStorage yields the shipped
// defaults.
//
// Run: node --test Portal/modules/fx/tuning.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
    STORAGE_KEYS, DENSITY_MIN, DENSITY_MAX, DENSITY_DEFAULT,
    QUALITY_TIERS, QUALITY_DEFAULT, SCALE_MIN, SCALE_MAX,
    defaultTuning, clampDensity, resolveQuality, qualityMultiplier, qualityList,
    normalizeTuning, countMultiplier, loadTuning, saveTuning, formatDensity,
} from "./tuning.js";

/** In-memory localStorage double. `boom` makes every access throw, which is
 *  exactly what Safari private mode does. */
function fakeStorage(seed = {}, boom = false) {
    const map = new Map(Object.entries(seed));
    return {
        map,
        getItem(k) { if (boom) throw new Error("SecurityError"); return map.has(k) ? map.get(k) : null; },
        setItem(k, v) { if (boom) throw new Error("SecurityError"); map.set(k, String(v)); },
    };
}

// ---------------------------------------------------------------------------
// 1. Defaults ARE today's behaviour
// ---------------------------------------------------------------------------

test("the defaults reproduce the shipped field exactly (multiplier 1.0)", () => {
    assert.deepEqual(defaultTuning(), { density: 1.0, quality: "high" });
    assert.equal(DENSITY_DEFAULT, 1.0);
    assert.equal(QUALITY_DEFAULT, "high");
    assert.equal(qualityMultiplier("high"), 1.0, "High must be 1.0 or the default look changes");
    assert.equal(countMultiplier(defaultTuning()), 1.0);
});

// ---------------------------------------------------------------------------
// 2. clampDensity is a TOTAL function — never NaN, never out of band
// ---------------------------------------------------------------------------

test("clampDensity falls back to the default for absent or unparseable input", () => {
    for (const bad of [undefined, null, "", "  ", "abc", "NaN", NaN, {}, [], true, () => {}]) {
        const v = clampDensity(bad);
        assert.equal(v, DENSITY_DEFAULT, `clampDensity(${JSON.stringify(bad)}) => ${v}`);
        assert.ok(Number.isFinite(v));
    }
});

test("clampDensity clamps instead of rejecting out-of-range numbers", () => {
    assert.equal(clampDensity(99), DENSITY_MAX);
    assert.equal(clampDensity(Infinity), DENSITY_DEFAULT, "Infinity is not finite -> default, not MAX");
    assert.equal(clampDensity(-Infinity), DENSITY_DEFAULT);
    assert.equal(clampDensity(0), DENSITY_MIN, "zero particles is a bug, not a setting");
    assert.equal(clampDensity(-5), DENSITY_MIN);
});

test("clampDensity accepts the STRING an <input type=range> actually hands us", () => {
    assert.equal(clampDensity("1.45"), 1.45);
    assert.equal(clampDensity("0.3"), 0.3);
    assert.equal(clampDensity("2"), 2);
    assert.equal(clampDensity(0.30000000000000004), 0.3, "float dust is rounded away");
});

// ---------------------------------------------------------------------------
// 3. Quality tiers
// ---------------------------------------------------------------------------

test("an unknown quality tier resolves to the default, never to undefined", () => {
    for (const bad of [undefined, null, "", "medium", "LOW", 3, {}]) {
        assert.equal(resolveQuality(bad), QUALITY_DEFAULT);
        assert.ok(Number.isFinite(qualityMultiplier(bad)));
    }
    assert.equal(resolveQuality("low"), "low");
    assert.equal(resolveQuality("ultra"), "ultra");
});

test("qualityList is the ONLY tier list and every entry is well-formed", () => {
    const list = qualityList();
    assert.equal(list.length, QUALITY_TIERS.length);
    assert.ok(list.some(t => t.id === QUALITY_DEFAULT), "the default tier must be listed");
    for (const t of list) {
        assert.equal(typeof t.id, "string");
        assert.ok(t.label.length, `tier ${t.id} has no label for the picker`);
        assert.ok(qualityMultiplier(t.id) > 0, `tier ${t.id} must not zero the field`);
    }
    // Low degrades, Ultra enriches — the ordering is the feature.
    assert.ok(qualityMultiplier("low") < qualityMultiplier("high"));
    assert.ok(qualityMultiplier("ultra") > qualityMultiplier("high"));
});

// ---------------------------------------------------------------------------
// 4. countMultiplier — the single number the engine consumes
// ---------------------------------------------------------------------------

test("countMultiplier survives any garbage and stays inside the scale band", () => {
    for (const bad of [undefined, null, "nope", 7, { density: NaN, quality: "nope" },
                       { density: "abc" }, { quality: 42 }]) {
        const m = countMultiplier(bad);
        assert.ok(Number.isFinite(m), `countMultiplier(${JSON.stringify(bad)}) => ${m}`);
        assert.ok(m >= SCALE_MIN && m <= SCALE_MAX, `out of band: ${m}`);
    }
    assert.equal(countMultiplier({ density: 1, quality: "high" }), 1.0);
    assert.equal(countMultiplier({ density: NaN, quality: undefined }), 1.0,
        "a fully invalid pair must land on the shipped look, not on zero");
});

test("countMultiplier composes the two dials", () => {
    const lo = countMultiplier({ density: DENSITY_MIN, quality: "low" });
    const hi = countMultiplier({ density: DENSITY_MAX, quality: "ultra" });
    assert.ok(lo < 1 && hi > 1);
    assert.ok(lo >= SCALE_MIN && hi <= SCALE_MAX);
    assert.equal(countMultiplier({ density: 2, quality: "high" }), 2);
});

test("normalizeTuning always returns both keys, correctly typed", () => {
    assert.deepEqual(normalizeTuning(undefined), defaultTuning());
    assert.deepEqual(normalizeTuning({ density: "1.5" }), { density: 1.5, quality: QUALITY_DEFAULT });
    assert.deepEqual(normalizeTuning({ quality: "ultra" }), { density: DENSITY_DEFAULT, quality: "ultra" });
});

// ---------------------------------------------------------------------------
// 5. Persistence — absent, corrupt and hostile storage
// ---------------------------------------------------------------------------

test("an EMPTY store yields the defaults", () => {
    assert.deepEqual(loadTuning(fakeStorage()), defaultTuning());
});

test("a CORRUPT store yields the defaults, never NaN", () => {
    const store = fakeStorage({
        [STORAGE_KEYS.density]: "banana",
        [STORAGE_KEYS.quality]: "extreme",
    });
    const t = loadTuning(store);
    assert.deepEqual(t, defaultTuning());
    assert.ok(Number.isFinite(t.density));
});

test("an OUT-OF-RANGE persisted density is clamped, not discarded", () => {
    assert.equal(loadTuning(fakeStorage({ [STORAGE_KEYS.density]: "12" })).density, DENSITY_MAX);
    assert.equal(loadTuning(fakeStorage({ [STORAGE_KEYS.density]: "-3" })).density, DENSITY_MIN);
});

test("a VALID store round-trips", () => {
    const store = fakeStorage();
    const saved = saveTuning({ density: "1.25", quality: "ultra" }, store);
    assert.deepEqual(saved, { density: 1.25, quality: "ultra" });
    assert.equal(store.map.get(STORAGE_KEYS.density), "1.25", "stored as a string, like every other key");
    assert.deepEqual(loadTuning(store), { density: 1.25, quality: "ultra" });
});

test("saveTuning sanitizes BEFORE writing, so a bad value can't be persisted", () => {
    const store = fakeStorage();
    saveTuning({ density: NaN, quality: "nope" }, store);
    assert.equal(store.map.get(STORAGE_KEYS.density), "1");
    assert.equal(store.map.get(STORAGE_KEYS.quality), QUALITY_DEFAULT);
    assert.deepEqual(loadTuning(store), defaultTuning());
});

test("a THROWING store (private mode) degrades to the defaults instead of taking the panel down", () => {
    const hostile = fakeStorage({}, true);
    assert.deepEqual(loadTuning(hostile), defaultTuning());
    assert.deepEqual(saveTuning({ density: 1.5, quality: "low" }, hostile),
        { density: 1.5, quality: "low" }, "the in-memory value still applies; only the write is dropped");
});

test("no storage at all (node / SSR) is not an error", () => {
    assert.deepEqual(loadTuning(null), defaultTuning());
    assert.deepEqual(saveTuning({ density: 0.5 }, null), { density: 0.5, quality: QUALITY_DEFAULT });
});

// ---------------------------------------------------------------------------
// 6. The readout
// ---------------------------------------------------------------------------

test("formatDensity never prints NaN%", () => {
    assert.equal(formatDensity(1), "100%");
    assert.equal(formatDensity("0.55"), "55%");
    assert.equal(formatDensity(undefined), "100%");
    assert.equal(formatDensity("garbage"), "100%");
});

// ---------------------------------------------------------------------------
// 7. Wiring — the dials reach the engine, and the UI has no second list
// ---------------------------------------------------------------------------

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

test("ember-fx folds the dials into fieldScale, the ONE definition of env.scale", () => {
    const src = read("../ember-fx.js");
    assert.ok(/countMultiplier\(tuning\)/.test(src),
        "fieldScale must apply countMultiplier — effects read env.scale, nothing else");
    assert.ok(/function setTuning\(/.test(src) && /setTuning,/.test(src),
        "setTuning must exist AND be on the window.EmberFX surface");
    assert.ok(/loadTuning\(\)/.test(src), "the persisted dials must be read at init");
    // The UI is not allowed to reach into module state; it goes through setTuning.
    assert.equal(/\btuning\.density\s*=/.test(src), false, "tuning is replaced wholesale, never mutated in place");
});

test("the dials reach even the fields that ignore env.scale (the DEFAULT is one)", async () => {
    // Rising Stars' 40/50/30 layer counts are an approved constant, not a
    // viewport density, so it reads env.tuning instead of env.scale. Without
    // this the Intensity slider is inert on the field most operators see.
    const { createEnv } = await import("./registry.js");
    const stars = (await import("./effects/stars.js")).default;
    const build = (tuningValue) => {
        const env = createEnv({ width: 1400, height: 800, rand: () => 0.5 });
        if (tuningValue !== undefined) env.tuning = tuningValue;
        stars.init(env);
        return env.state.sim.particles.length;
    };
    assert.equal(build(undefined), 120, "an env with no dials must be the shipped 120");
    assert.equal(build(1), 120, "the DEFAULT dials must be the shipped 120 exactly");
    assert.ok(build(2) > 120, "Intensity up must add particles");
    assert.ok(build(0.5) < 120, "Intensity down must remove them");
    assert.ok(build(0) === 120 || build(0) >= 3, "a zero/garbage multiplier must never empty the field");
    assert.equal(build(NaN), 120, "NaN must not produce a zero-particle field");
});

test("the settings markup builds BOTH pickers from a registry, not from hard-coded options", () => {
    const html = read("../../index.html");
    const section = html.slice(html.indexOf("ember-section"), html.indexOf("updates-section"));
    assert.ok(/id="particleModeSelect"/.test(section), "the field picker must exist in the settings menu");
    assert.ok(/id="particleQuality"/.test(section));
    assert.ok(/id="particleDensity"/.test(section));
    // The old three radios (and any replacement list of <option>s) must be gone:
    // registering an effect is the only step needed to appear in the picker.
    assert.equal(/name="particleMode"/.test(section), false, "the hard-coded particle radios must be gone");
    const optionsInSelects = section.match(/<option/g);
    assert.equal(optionsInSelects, null, "no <option> may be authored by hand — both lists are built at runtime");
    // The VISIBILITY axis is a separate control and stays exactly as it was.
    assert.equal((section.match(/name="emberMode"/g) || []).length, 3,
        "the Off / While generating / Always radios must be left alone");
});

test("the range input's static attributes agree with the module's bounds", () => {
    const html = read("../../index.html");
    const tag = html.match(/<input type="range" id="particleDensity"[\s\S]*?>/)[0];
    assert.ok(tag.includes(`min="${DENSITY_MIN}"`), `range min must be ${DENSITY_MIN}: ${tag}`);
    assert.ok(tag.includes(`max="${DENSITY_MAX}"`), `range max must be ${DENSITY_MAX}: ${tag}`);
    assert.ok(tag.includes(`value="${DENSITY_DEFAULT}"`.replace(".0", "")), "the markup default must be the shipped default");
});

test("a CSS change bumped the @import cache-bust (index.html alone does not bust it)", () => {
    const mainCss = read("../../styles/main.css");
    const ember = mainCss.match(/_ember-fx\.css\?v=([^']+)'/);
    assert.ok(ember, "_ember-fx.css must keep its own ?v= query string");
    const html = read("../../index.html");
    const shell = html.match(/main\.css\?v=([^"]+)"/);
    assert.ok(shell, "index.html must cache-bust main.css");
    assert.equal(ember[1], shell[1],
        `_ember-fx.css (${ember[1]}) is @imported with its OWN ?v= and must be bumped with main.css (${shell[1]})`);
});
