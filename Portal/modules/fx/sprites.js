/**
 * fx/sprites.js — the pre-rendered sprite atlas for the particle field.
 *
 * drawImage() of a baked sprite instead of a per-particle radial gradient is the
 * #1 per-frame win in this engine. Baked LAZILY (first canvas init) so a non-DOM
 * import — unit test, SSR — never touches `document`.
 *
 * Lives outside the effect modules because it is shared infrastructure: several
 * effects will draw from the same atlas, and it must be baked once per document,
 * not once per effect.
 */

/** blackbody-ish ember ramp: white-hot core -> deep ember red */
export const RAMP = [[255, 255, 240], [255, 238, 150], [255, 182, 64], [255, 110, 22], [201, 44, 6], [92, 16, 5]];

let EMBER_SPR = null;   // one sprite per RAMP stop
let STAR_SPR = null;    // soft warm-white blob for the hero-star glow

/**
 * A 64px radial gradient magnified ~4x on a black backdrop is the textbook
 * banding case, and banding is the single most visible "cheap" artifact we have.
 * Triangular-distributed noise at ±1/255 breaks the flat bands into dither.
 * Paid ONCE at bake time (six sprites), zero per-frame cost — this is the one
 * place Math.random() is fine, because it never runs inside a frame.
 */
function dither(x, size) {
    const img = x.getImageData(0, 0, size, size);
    const d = img.data;
    for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] === 0) continue;                     // leave fully transparent px alone
        // Triangular PDF (sum of two uniforms) — flatter spectrum than uniform.
        const n = (Math.random() + Math.random() - 1) * 1.5;
        d[i] = Math.max(0, Math.min(255, d[i] + n));
        d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + n));
        d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + n));
        d[i + 3] = Math.max(0, Math.min(255, d[i + 3] + n));
    }
    x.putImageData(img, 0, 0);
}

export function makeSprite(size, r, g, b) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const x = c.getContext('2d');
    const gr = x.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gr.addColorStop(0, `rgba(${r},${g},${b},1)`);
    gr.addColorStop(0.35, `rgba(${r},${g},${b},.5)`);
    gr.addColorStop(1, `rgba(${r},${g},${b},0)`);
    x.fillStyle = gr;
    x.fillRect(0, 0, size, size);
    dither(x, size);
    return c;
}

/** Bake once; returns the atlas handed to effects as env.sprites. */
export function ensureSprites() {
    if (!EMBER_SPR) {
        EMBER_SPR = RAMP.map(c => makeSprite(64, c[0], c[1], c[2]));
        STAR_SPR = makeSprite(48, 255, 252, 246);
    }
    return { ember: EMBER_SPR, star: STAR_SPR };
}

/** @returns {{ember:Array, star:*}|null} null until ensureSprites() has run. */
export function getSprites() {
    return EMBER_SPR ? { ember: EMBER_SPR, star: STAR_SPR } : null;
}
