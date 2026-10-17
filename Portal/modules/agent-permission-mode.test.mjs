/**
 * agent-permission-mode.test.mjs — the Claude Code agent's permission modes (audit P03/O06).
 *
 * The banner's mode switch used to be cosmetic: every prompt sent skip_permissions:true, so
 * "Plan Mode (read-only)" ran a fully privileged agent, and "Normal (prompts)" promised an
 * approval dialog print mode cannot deliver. These pin the real mapping: two modes, Plan ->
 * permission_mode "plan" with skip_permissions false, YOLO -> skip_permissions true, and a
 * stale or unknown value never escalates.
 *
 * Run: node --test Portal/modules/agent-permission-mode.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    PERMISSION_MODES,
    DEFAULT_PERMISSION_MODE,
    PERMISSION_MODE_INFO,
    normalizePermissionMode,
    nextPermissionMode,
    permissionPayload,
    loadPermissionMode,
    savePermissionMode,
} from './agent-permission-mode.js';

test('exactly two modes exist and "normal" is gone', () => {
    assert.deepEqual(PERMISSION_MODES, ['yolo', 'plan']);
    assert.equal(DEFAULT_PERMISSION_MODE, 'yolo');
    for (const info of Object.values(PERMISSION_MODE_INFO)) {
        assert.doesNotMatch(info.description, /prompts for permissions/i);
    }
    assert.equal(PERMISSION_MODE_INFO.normal, undefined);
});

test('Plan sends the read-only mode and never skip_permissions', () => {
    assert.deepEqual(permissionPayload('plan'), { permission_mode: 'plan', skip_permissions: false });
});

test('only YOLO sends skip_permissions:true', () => {
    assert.deepEqual(permissionPayload('yolo'), { permission_mode: 'yolo', skip_permissions: true });
    for (const mode of ['plan', 'normal', 'ask', 'garbage']) {
        assert.equal(permissionPayload(mode).skip_permissions, false, mode);
    }
});

test('normalisation: legacy auto is YOLO, unknown values fall to plan', () => {
    assert.equal(normalizePermissionMode('auto'), 'yolo');
    assert.equal(normalizePermissionMode('YOLO'), 'yolo');
    assert.equal(normalizePermissionMode(' plan '), 'plan');
    assert.equal(normalizePermissionMode('normal'), 'plan');
    assert.equal(normalizePermissionMode('bypassPermissions'), 'plan');
    assert.equal(normalizePermissionMode(undefined), 'yolo');
    assert.equal(normalizePermissionMode(null), 'yolo');
    assert.equal(normalizePermissionMode(''), 'yolo');
});

test('cycling alternates YOLO and Plan', () => {
    assert.equal(nextPermissionMode('yolo'), 'plan');
    assert.equal(nextPermissionMode('plan'), 'yolo');
    assert.equal(nextPermissionMode('normal'), 'yolo');   // a stale 'normal' (= plan) moves on
});

test('the chosen mode persists, and storage failures never break the switch', () => {
    const store = new Map();
    const storage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
    };
    assert.equal(loadPermissionMode(storage), 'yolo');
    savePermissionMode(storage, 'plan');
    assert.equal(loadPermissionMode(storage), 'plan');
    store.set([...store.keys()][0], 'normal');
    assert.equal(loadPermissionMode(storage), 'plan');

    const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
    assert.equal(loadPermissionMode(broken), 'yolo');
    assert.doesNotThrow(() => savePermissionMode(broken, 'plan'));
    assert.equal(loadPermissionMode(undefined), 'yolo');
});
