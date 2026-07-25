/**
 * fx/effects/index.js — the effect CATALOGUE.
 *
 * Registers one lightweight stub per effect: enough for the persistence
 * whitelist and the settings picker to exist synchronously at page load, while
 * the simulation itself is `import()`ed only when that effect is selected. A
 * catalogue of thirty effects therefore costs the same at load as three.
 *
 * The stub's `load()` imports the module, which calls registerEffect() again
 * with the same id; the registry merges, upgrading the stub in place.
 *
 * ADDING AN EFFECT = one file under ./ + one line here. Nothing else: the
 * whitelist, the dispatch and the settings options all derive from the registry.
 *
 * `label` is duplicated here on purpose — it is the one field the settings UI
 * needs BEFORE the module is loaded. A test asserts the two copies agree.
 */
import { registerEffect } from '../registry.js';

// --- shipped fields (the three that predate the registry) --------------------
registerEffect({ id: 'stars',      label: 'Rising Stars',        load: () => import('./stars.js') });
registerEffect({ id: 'embers',     label: 'Embers',              load: () => import('./embers.js') });
registerEffect({ id: 'matrix',     label: 'Matrix',              load: () => import('./matrix.js') });

// --- v1 catalogue (2026-07-24) -----------------------------------------------
// Ordered calmest-first: the top of this list is what a new operator meets, and
// the quiet fields are the ones that stay tolerable behind text all day.
registerEffect({ id: 'fireflies',  label: 'Fireflies',           load: () => import('./fireflies.js') });
registerEffect({ id: 'slipstream', label: 'Slipstream',          load: () => import('./slipstream.js') });
registerEffect({ id: 'bokeh',      label: 'Bokeh Depth',         load: () => import('./bokeh.js') });
registerEffect({ id: 'meteors',    label: 'Meteor Fall',         load: () => import('./meteors.js') });
registerEffect({ id: 'cinders',    label: 'Cinders on the Wind', load: () => import('./cinders.js') });
registerEffect({ id: 'snow',       label: 'Drift Snow',          load: () => import('./snow.js') });
registerEffect({ id: 'rain',       label: 'Rainfall',            load: () => import('./rain.js') });
registerEffect({ id: 'fog',        label: 'Low Fog',             load: () => import('./fog.js') });
registerEffect({ id: 'aurora',     label: 'Aurora Curtain',      load: () => import('./aurora.js') });
registerEffect({ id: 'ledger',     label: 'Ledger Rain',         load: () => import('./ledger.js') });
