/**
 * agent-permission-mode.js
 * The Claude Code agent's permission modes (audit P03/O06). Pure — no DOM — so the mapping
 * the banner switch sends is node-testable (agent-permission-mode.test.mjs).
 *
 * Exactly two modes, each a CLI flag that works in print mode (see agent_routes.py):
 *   yolo — every tool approved (--dangerously-skip-permissions); the default
 *   plan — read-only (--permission-mode plan)
 * There is no "Normal (prompts)" mode: print mode has no approval round-trip, so it hung or
 * auto-denied behind a fake dialog. It comes back only with a real approval protocol.
 */

export const PERMISSION_MODES = ['yolo', 'plan'];
export const DEFAULT_PERMISSION_MODE = 'yolo';

export const PERMISSION_MODE_INFO = {
    yolo: { label: 'YOLO', className: 'mode-auto', description: 'YOLO (every tool approved)' },
    plan: { label: 'Plan Mode', className: 'mode-plan', description: 'Plan Mode (read-only)' },
};

const STORAGE_KEY = 'agentPermissionMode';

/**
 * Map any value to a real mode. Absent -> the default; the old 'auto' key -> yolo; anything
 * else unrecognised (a stale 'normal', garbage) -> plan, so a bad value never escalates.
 */
export function normalizePermissionMode(mode) {
    if (mode === undefined || mode === null || String(mode).trim() === '') {
        return DEFAULT_PERMISSION_MODE;
    }
    const m = String(mode).trim().toLowerCase();
    if (m === 'yolo' || m === 'auto') return 'yolo';
    return 'plan';
}

export function nextPermissionMode(mode) {
    const i = PERMISSION_MODES.indexOf(normalizePermissionMode(mode));
    return PERMISSION_MODES[(i + 1) % PERMISSION_MODES.length];
}

/** The fields every prompt/input frame carries. skip_permissions is true ONLY for YOLO (it
 *  keeps an older box, which reads only that flag, in the mode the user chose). */
export function permissionPayload(mode) {
    const m = normalizePermissionMode(mode);
    return { permission_mode: m, skip_permissions: m === 'yolo' };
}

/** The remembered mode (a per-browser convenience; any storage failure -> the default). */
export function loadPermissionMode(storage) {
    try {
        const raw = storage ? storage.getItem(STORAGE_KEY) : null;
        return raw === null ? DEFAULT_PERMISSION_MODE : normalizePermissionMode(raw);
    } catch (_) {
        return DEFAULT_PERMISSION_MODE;
    }
}

export function savePermissionMode(storage, mode) {
    try {
        if (storage) storage.setItem(STORAGE_KEY, normalizePermissionMode(mode));
    } catch (_) {
        // Private window / blocked storage: the switch still works for this page.
    }
}
