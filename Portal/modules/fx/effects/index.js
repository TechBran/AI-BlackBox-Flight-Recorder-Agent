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

registerEffect({ id: 'stars',  label: 'Rising Stars', load: () => import('./stars.js') });
registerEffect({ id: 'embers', label: 'Embers',       load: () => import('./embers.js') });
registerEffect({ id: 'matrix', label: 'Matrix',       load: () => import('./matrix.js') });
