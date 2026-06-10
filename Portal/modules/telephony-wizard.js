/**
 * telephony-wizard.js
 * Guided telephony gateway setup wizard — validate connectivity, copy TG-side steps,
 * preview + apply our-side Asterisk config, finish.
 *
 * Backend endpoints (all JSON, on `app`):
 *   POST /asterisk/gateways/{id}/validate
 *   GET  /asterisk/gateways/{id}/config-preview
 *   POST /asterisk/gateways/{id}/apply
 */

import { $, toast, toastSuccess, toastError } from './core-utils.js';

// =============================================================================
// State (module-scope)
// =============================================================================

let _gatewayId = null;
let _gatewayName = '';
let _currentStep = 0;          // 0=Identify, 1=Validate, 2=Configure, 3=Done
let _lastValidate = null;      // last /validate response
let _lastPreview = null;       // last /config-preview response
let _applyResult = null;       // last /apply response

const STEPS = ['Identify', 'Validate', 'Configure', 'Done'];

// =============================================================================
// Utility
// =============================================================================

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

async function copyToClipboard(text, label) {
    try {
        await navigator.clipboard.writeText(text);
        toastSuccess(`${label || 'Text'} copied`);
    } catch (e) {
        console.error('[TelephonyWizard] Copy failed:', e);
        toastError('Copy failed — select and copy manually');
    }
}

function checkRow(label, ok) {
    const icon = ok
        ? '<span class="wiz-check-ok">&#10003;</span>'
        : '<span class="wiz-check-fail">&#10007;</span>';
    return `<div class="wiz-check-row ${ok ? 'wiz-ok' : 'wiz-fail'}">${icon}<span class="wiz-check-label">${escapeHtml(label)}</span></div>`;
}

// =============================================================================
// API
// =============================================================================

async function runValidate() {
    const body = $('telephonyWizardBody');
    if (body) body.innerHTML = '<div class="wiz-loading"><span class="discover-spinner"></span> Validating connectivity...</div>';
    try {
        const res = await fetch(`/asterisk/gateways/${_gatewayId}/validate`, { method: 'POST' });
        if (!res.ok) throw new Error('Validation request failed');
        _lastValidate = await res.json();
    } catch (e) {
        console.error('[TelephonyWizard] Validate failed:', e);
        toastError('Validation failed: ' + e.message);
        _lastValidate = null;
    }
    renderStep();
}

async function loadPreview() {
    const body = $('telephonyWizardBody');
    if (body) body.innerHTML = '<div class="wiz-loading"><span class="discover-spinner"></span> Loading configuration preview...</div>';
    try {
        const res = await fetch(`/asterisk/gateways/${_gatewayId}/config-preview`);
        if (!res.ok) throw new Error('Config preview request failed');
        _lastPreview = await res.json();
    } catch (e) {
        console.error('[TelephonyWizard] Config preview failed:', e);
        toastError('Failed to load config preview: ' + e.message);
        _lastPreview = null;
    }
    renderStep();
}

async function applyConfig() {
    const btn = $('wizApplyBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Applying...'; }
    try {
        const res = await fetch(`/asterisk/gateways/${_gatewayId}/apply`, { method: 'POST' });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Apply failed');
        }
        _applyResult = await res.json();
        if (_applyResult.applied) {
            toastSuccess('Our-side config applied');
        } else {
            toast('Config processed');
        }
        renderStep();
    } catch (e) {
        console.error('[TelephonyWizard] Apply failed:', e);
        toastError('Failed to apply config: ' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = 'Apply our-side config'; }
    }
}

// =============================================================================
// Step Rendering
// =============================================================================

function renderStepper() {
    return `
        <div class="wiz-stepper">
            ${STEPS.map((label, i) => {
                let cls = 'wiz-step';
                if (i === _currentStep) cls += ' wiz-step-active';
                else if (i < _currentStep) cls += ' wiz-step-done';
                return `<div class="${cls}"><span class="wiz-step-num">${i + 1}</span><span class="wiz-step-label">${escapeHtml(label)}</span></div>`;
            }).join('<span class="wiz-step-sep"></span>')}
        </div>`;
}

function renderIdentify() {
    return `
        <div class="wiz-panel">
            <h4 class="wiz-panel-title">Set up this gateway</h4>
            <p class="wiz-panel-desc">This wizard will check connectivity, walk you through the gateway-side (NeoGate GUI) steps, and apply the BlackBox-side Asterisk configuration.</p>
            <div class="wiz-identify-card">
                <div class="wiz-identify-row"><span class="wiz-identify-key">Gateway</span><span class="wiz-identify-val">${escapeHtml(_gatewayName || '(unnamed)')}</span></div>
                <div class="wiz-identify-row"><span class="wiz-identify-key">ID</span><span class="wiz-identify-val wiz-mono">${escapeHtml(_gatewayId)}</span></div>
            </div>
        </div>`;
}

function renderValidate() {
    if (!_lastValidate) {
        return `
            <div class="wiz-panel">
                <h4 class="wiz-panel-title">Validate connectivity</h4>
                <p class="wiz-panel-desc">Run a quick check of reachability, AMI authentication, the SIP trunk, and each SIM.</p>
                <div class="wiz-panel-actions">
                    <button class="btn btn-confirm" id="wizValidateBtn">Run validation</button>
                </div>
            </div>`;
    }

    const v = _lastValidate;
    const spans = Array.isArray(v.spans) ? v.spans : [];
    const anyFail = !v.reachable || !v.ami_auth || !v.trunk_online ||
        spans.some(s => !s.registered);

    let simHtml = '';
    if (spans.length > 0) {
        simHtml = `
            <div class="wiz-sim-list">
                <div class="wiz-subhead">SIMs</div>
                ${spans.map(s => {
                    const ok = s.registered === true;
                    const icon = ok
                        ? '<span class="wiz-check-ok">&#10003;</span>'
                        : '<span class="wiz-check-fail">&#10007;</span>';
                    const carrier = escapeHtml(s.carrier || 'Unknown');
                    const sig = (s.signal !== null && s.signal !== undefined && s.signal !== '')
                        ? `${escapeHtml(String(s.signal))}%` : '--';
                    const phone = s.phone_number ? ` &middot; ${escapeHtml(s.phone_number)}` : '';
                    return `<div class="wiz-check-row ${ok ? 'wiz-ok' : 'wiz-fail'}">${icon}<span class="wiz-check-label">Span ${escapeHtml(s.span)} — ${carrier} &middot; signal ${sig}${phone}</span></div>`;
                }).join('')}
            </div>`;
    } else {
        simHtml = `<div class="wiz-sim-list"><div class="wiz-subhead">SIMs</div><div class="wiz-empty-note">No SIM spans reported.</div></div>`;
    }

    const flag = anyFail
        ? '<div class="wiz-notice wiz-notice-warn">Some checks failed. You can still continue — validation is informational — but the gateway may not work until they pass.</div>'
        : '<div class="wiz-notice wiz-notice-ok">All checks passed.</div>';

    return `
        <div class="wiz-panel">
            <h4 class="wiz-panel-title">Validation results</h4>
            <div class="wiz-check-list">
                ${checkRow('Reachable', v.reachable === true)}
                ${checkRow('AMI authentication', v.ami_auth === true)}
                ${checkRow('SIP trunk online', v.trunk_online === true)}
            </div>
            ${simHtml}
            ${flag}
            <div class="wiz-panel-actions">
                <button class="btn" id="wizValidateBtn">Re-validate</button>
            </div>
        </div>`;
}

function renderConfigure() {
    if (!_lastPreview) {
        return `
            <div class="wiz-panel">
                <h4 class="wiz-panel-title">Configure</h4>
                <p class="wiz-panel-desc">Load the gateway-side steps and the BlackBox-side Asterisk configuration.</p>
                <div class="wiz-panel-actions">
                    <button class="btn btn-confirm" id="wizPreviewBtn">Load configuration</button>
                </div>
            </div>`;
    }

    const p = _lastPreview;
    const steps = Array.isArray(p.tg_steps) ? p.tg_steps : [];
    const conf = p.asterisk_conf || '';

    const stepsHtml = steps.length > 0
        ? `<ol class="wiz-tg-steps">
                ${steps.map((step, i) => `
                    <li class="wiz-tg-step">
                        <span class="wiz-tg-step-text">${escapeHtml(step)}</span>
                        <button class="btn wiz-copy-btn" data-copy-step="${i}">Copy</button>
                    </li>`).join('')}
           </ol>`
        : '<div class="wiz-empty-note">No gateway-side steps provided.</div>';

    let restartNotice = '';
    if (_applyResult && _applyResult.restart_recommended) {
        restartNotice = `<div class="wiz-notice wiz-notice-warn">A BlackBox restart is needed to finish — ask your admin or use the restart control.</div>`;
    } else if (_applyResult && _applyResult.applied) {
        restartNotice = `<div class="wiz-notice wiz-notice-ok">Configuration applied${_applyResult.reload && _applyResult.reload.ok ? ' and reloaded' : ''}.</div>`;
    }

    return `
        <div class="wiz-panel">
            <h4 class="wiz-panel-title">Gateway-side steps (NeoGate GUI)</h4>
            <p class="wiz-panel-desc">Do these in the gateway's web interface. Use Copy to grab each value.</p>
            ${stepsHtml}

            <div class="wiz-subhead wiz-subhead-spaced">BlackBox-side Asterisk config</div>
            <div class="wiz-conf-wrap">
                <button class="btn wiz-copy-btn wiz-conf-copy" id="wizCopyConf">Copy</button>
                <pre class="wiz-conf-pre">${escapeHtml(conf)}</pre>
            </div>

            ${restartNotice}
            <div class="wiz-panel-actions">
                <button class="btn btn-confirm" id="wizApplyBtn">Apply our-side config</button>
            </div>
        </div>`;
}

function renderDone() {
    let restartNote = '';
    if (_applyResult && _applyResult.restart_recommended) {
        restartNote = `<div class="wiz-notice wiz-notice-warn">A BlackBox restart is still needed to finish — ask your admin or use the restart control.</div>`;
    }
    const applied = _applyResult && _applyResult.applied;
    return `
        <div class="wiz-panel wiz-panel-done">
            <div class="wiz-done-icon">&#10003;</div>
            <h4 class="wiz-panel-title">Setup complete</h4>
            <p class="wiz-panel-desc">${escapeHtml(_gatewayName || 'The gateway')} has been walked through setup.</p>
            <ul class="wiz-done-summary">
                <li>${_lastValidate ? 'Connectivity validated' : 'Validation skipped'}</li>
                <li>${_lastPreview ? 'Configuration reviewed' : 'Configuration not loaded'}</li>
                <li>${applied ? 'Our-side config applied' : 'Our-side config not applied'}</li>
            </ul>
            ${restartNote}
        </div>`;
}

function renderBody() {
    switch (_currentStep) {
        case 0: return renderIdentify();
        case 1: return renderValidate();
        case 2: return renderConfigure();
        case 3: return renderDone();
        default: return '';
    }
}

function renderFooter() {
    const isFirst = _currentStep === 0;
    const isLast = _currentStep === STEPS.length - 1;

    const backBtn = `<button class="btn" id="wizBackBtn" ${isFirst ? 'disabled' : ''}>Back</button>`;

    let primaryBtn;
    if (isFirst) {
        primaryBtn = `<button class="btn btn-confirm" id="wizNextBtn">Start</button>`;
    } else if (isLast) {
        primaryBtn = `<button class="btn btn-confirm" id="wizCloseFooterBtn">Close</button>`;
    } else {
        primaryBtn = `<button class="btn btn-confirm" id="wizNextBtn">Next</button>`;
    }

    return `
        <div class="wiz-footer">
            ${backBtn}
            ${primaryBtn}
        </div>`;
}

function renderStep() {
    const stepper = $('telephonyWizardStepper');
    const body = $('telephonyWizardBody');
    const footer = $('telephonyWizardFooter');
    if (stepper) stepper.innerHTML = renderStepper();
    if (body) body.innerHTML = renderBody();
    if (footer) footer.innerHTML = renderFooter();
}

// =============================================================================
// Navigation
// =============================================================================

function goNext() {
    if (_currentStep < STEPS.length - 1) {
        _currentStep++;
        renderStep();
    }
}

function goBack() {
    if (_currentStep > 0) {
        _currentStep--;
        renderStep();
    }
}

function closeWizard() {
    const modal = $('telephonyWizardModal');
    if (modal) modal.classList.add('hide');
}

// =============================================================================
// Event delegation (single listener on modal)
// =============================================================================

function handleWizardClick(e) {
    const modal = $('telephonyWizardModal');

    // Click outside the card closes
    if (e.target === modal) {
        closeWizard();
        return;
    }

    const target = e.target.closest('button');
    if (!target) return;

    switch (target.id) {
        case 'wizCloseBtn':
        case 'wizCloseFooterBtn':
            closeWizard();
            return;
        case 'wizNextBtn':
            goNext();
            return;
        case 'wizBackBtn':
            goBack();
            return;
        case 'wizValidateBtn':
            runValidate();
            return;
        case 'wizPreviewBtn':
            loadPreview();
            return;
        case 'wizApplyBtn':
            applyConfig();
            return;
        case 'wizCopyConf':
            copyToClipboard(_lastPreview ? (_lastPreview.asterisk_conf || '') : '', 'Asterisk config');
            return;
    }

    // Per-step copy buttons
    if (target.dataset.copyStep !== undefined) {
        const idx = parseInt(target.dataset.copyStep, 10);
        const steps = (_lastPreview && Array.isArray(_lastPreview.tg_steps)) ? _lastPreview.tg_steps : [];
        if (!Number.isNaN(idx) && steps[idx] !== undefined) {
            copyToClipboard(steps[idx], `Step ${idx + 1}`);
        }
    }
}

// =============================================================================
// Public API
// =============================================================================

export function openWizard(gatewayId, gatewayName) {
    _gatewayId = gatewayId;
    _gatewayName = gatewayName || '';
    _currentStep = 0;
    _lastValidate = null;
    _lastPreview = null;
    _applyResult = null;

    const modal = $('telephonyWizardModal');
    if (modal) modal.classList.remove('hide');
    renderStep();
}

export function initTelephonyWizard() {
    const modal = $('telephonyWizardModal');
    if (modal) {
        modal.addEventListener('click', handleWizardClick);
    }
}
