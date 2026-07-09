/**
 * cron-manager.js
 * Cron Job Scheduler management interface
 */

import { $, toast, toastSuccess, toastError } from './core-utils.js';
import { getOperator, fetchAvailableModels, getHydratedModels } from './state-management.js';

// Cache server operator list (populated once on first modal open)
let _operatorList = null;

// Provider catalog keys offered in the cron picker — the SAME canonical keys
// the chat composer uses (and that the M4.1 backend stores in job.provider).
// Order matches the index.html <select id="cronProvider"> options.
const CRON_PROVIDERS = ['google', 'openai', 'anthropic', 'xai', 'custom', 'computer-use'];
const CRON_DEFAULT_PROVIDER = 'google';

// Legacy jobs (pre-M4.1) stored a coarse word in `model` and had no `provider`
// field. Map those words back to the canonical catalog key so the edit form
// can still select a sensible provider. Specific model ids fall through this.
const LEGACY_MODEL_TO_PROVIDER = {
    gemini: 'google',
    google: 'google',
    openai: 'openai',
    gpt: 'openai',
    claude: 'anthropic',
    anthropic: 'anthropic',
    grok: 'xai',
    xai: 'xai',
    'computer-use': 'computer-use'
};

/**
 * Derive the canonical provider key for a job that lacks an explicit
 * `provider` field (legacy rows). Tries the coarse legacy word map first,
 * then scans every hydrated catalog for an id match.
 * @param {string} model - the raw job.model value
 * @returns {string} canonical provider key (defaults to CRON_DEFAULT_PROVIDER)
 */
function deriveProviderFromModel(model) {
    if (!model) return CRON_DEFAULT_PROVIDER;
    if (LEGACY_MODEL_TO_PROVIDER[model]) return LEGACY_MODEL_TO_PROVIDER[model];
    for (const provider of CRON_PROVIDERS) {
        if (getHydratedModels(provider).some(m => m.id === model)) return provider;
    }
    return CRON_DEFAULT_PROVIDER;
}

/**
 * Resolve a friendly display name for a (provider, modelId) pair from the
 * hydrated catalog. Falls back to the raw id when the catalog isn't loaded
 * or the id is unknown — never throws, never shows blank.
 * @param {string} provider - canonical provider key
 * @param {string} modelId - specific model id ('' = Auto / provider default)
 * @returns {string}
 */
function friendlyModelName(provider, modelId) {
    if (provider === 'computer-use' && !modelId) return 'Computer Use';
    const models = getHydratedModels(provider);
    const match = models.find(m => m.id === (modelId || ''));
    if (match) return match.name;
    if (!modelId) return 'Auto';
    return modelId;
}

/**
 * Hydrate #cronModelId for the given provider via the shared chat-composer
 * fetch (fetchAvailableModels mutates MODEL_CONFIG in place + caches), then
 * read the hydrated list with getHydratedModels and build the options:
 * a first `(Auto - <default>)` entry (value '') followed by every specific id.
 * @param {string} provider - canonical provider key
 * @param {string} [selectedId] - id to pre-select ('' = Auto)
 */
async function hydrateCronModelSelect(provider, selectedId) {
    const sel = $('cronModelId');
    if (!sel) return;

    await fetchAvailableModels(provider);
    const models = getHydratedModels(provider);

    sel.innerHTML = '';
    if (models.length === 0) {
        // No catalog at all (offline + unknown provider) — synthesize Auto.
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '(Auto)';
        sel.appendChild(opt);
    } else {
        // First entry from buildHydratedModels is always the Auto placeholder
        // (id ''); the rest are specific ids. Render flat (cron has no optgroup).
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.name;
            sel.appendChild(opt);
        });
    }

    // Pre-select: explicit request → last-used (localStorage) → Auto ('').
    let target = selectedId;
    if (target === undefined || target === null) {
        target = localStorage.getItem(`bb_cron_model_${provider}`) || '';
    }
    if ([...sel.options].some(o => o.value === target)) {
        sel.value = target;
    } else {
        sel.value = '';
    }
}

// =============================================================================
// State
// =============================================================================

let allJobs = [];
let currentFilter = 'all';
let currentSearch = '';
let _pollInterval = null;

// =============================================================================
// API Functions
// =============================================================================

async function fetchJobs(status = null) {
    const params = new URLSearchParams();
    if (status && status !== 'all') params.set('status', status);
    const res = await fetch(`/api/cron/jobs?${params}`);
    if (!res.ok) throw new Error('Failed to fetch jobs');
    const data = await res.json();
    return data.jobs;
}

async function createJob(jobData) {
    const res = await fetch('/api/cron/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(jobData)
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Failed to create job');
    }
    return (await res.json()).job;
}

async function updateJob(jobId, updates) {
    const res = await fetch(`/api/cron/jobs/${jobId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to update job');
    }
    return (await res.json()).job;
}

async function deleteJob(jobId) {
    const res = await fetch(`/api/cron/jobs/${jobId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Failed to delete job');
}

async function pauseJob(jobId) {
    const res = await fetch(`/api/cron/jobs/${jobId}/pause`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to pause job');
    return (await res.json()).job;
}

async function resumeJob(jobId) {
    const res = await fetch(`/api/cron/jobs/${jobId}/resume`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to resume job');
    return (await res.json()).job;
}

async function runJobNow(jobId) {
    const res = await fetch(`/api/cron/jobs/${jobId}/run`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to run job');
    return (await res.json()).result;
}

async function fetchHistory(jobId) {
    const res = await fetch(`/api/cron/jobs/${jobId}/history`);
    if (!res.ok) throw new Error('Failed to fetch history');
    return (await res.json()).history;
}

async function fetchContacts() {
    try {
        // Fresh-box-safe operator fallback: current operator → server-driven
        // operator list head → empty (NEVER a hardcoded person's name).
        const operator = getOperator() || window.__operator || (_operatorList && _operatorList[0]) || '';
        const resp = await fetch(`/api/cron/contacts?operator=${encodeURIComponent(operator)}`);
        if (!resp.ok) return [];
        const data = await resp.json();
        return data.contacts || [];
    } catch { return []; }
}

async function populateOperatorSelector(selectedOperator) {
    const sel = $('cronOperator');
    if (!sel) return;

    // Fetch operator list from server (cached after first call)
    if (!_operatorList) {
        try {
            const r = await fetch('/health');
            const j = await r.json();
            _operatorList = (j.users && Array.isArray(j.users.list)) ? j.users.list : [];
        } catch {
            _operatorList = [];
        }
    }

    sel.innerHTML = '';
    _operatorList.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        sel.appendChild(opt);
    });

    // Default to selected operator, or current operator, or first in list
    const target = selectedOperator || getOperator() || (_operatorList[0] || '');
    if (target && _operatorList.includes(target)) {
        sel.value = target;
    }
}

// =============================================================================
// Job List Rendering
// =============================================================================

async function refreshJobList() {
    try {
        const status = currentFilter !== 'all' ? currentFilter : null;
        allJobs = await fetchJobs(status);
        renderFilteredJobs();
    } catch (e) {
        console.error('Failed to refresh jobs:', e);
        toastError('Failed to load jobs');
    }
}

function renderFilteredJobs() {
    let jobs = allJobs;
    if (currentSearch) {
        const q = currentSearch.toLowerCase();
        jobs = jobs.filter(j =>
            j.name.toLowerCase().includes(q) ||
            j.prompt.toLowerCase().includes(q)
        );
    }
    renderJobList(jobs);
}

function renderJobList(jobs) {
    const container = $('cronJobList');
    if (!container) return;

    if (jobs.length === 0) {
        container.innerHTML = `
            <div class="cron-empty-state">
                <p>No scheduled jobs yet</p>
                <p class="cron-empty-hint">Create your first job to automate tasks on a schedule</p>
            </div>`;
        return;
    }

    container.innerHTML = jobs.map(job => {
        // Status drives the left-border accent + badge only (no block fill).
        // M3 introduced status='error'; map it to the red accent/badge.
        const statusClass = {
            active: 'cron-status-active',
            error: 'cron-status-error'
        }[job.status] || 'cron-status-paused';
        const statusLabel = {
            active: 'Active',
            error: 'Error'
        }[job.status] || 'Paused';
        const hint = job.frequency_hint || job.schedule;
        const deliveryLabel = {
            snapshot: 'Snapshot',
            sms: 'SMS',
            voice_call: 'Voice',
            notification: 'Alert'
        }[job.delivery] || job.delivery;

        return `
        <div class="cron-job-card ${statusClass}" data-job-id="${job.id}">
            <div class="cron-job-header">
                <span class="cron-job-status-badge ${statusClass}">${statusLabel}</span>
                <span class="cron-job-name">${escapeHtml(job.name)}</span>
                <span class="cron-job-schedule">${escapeHtml(hint)}</span>
            </div>
            <div class="cron-job-prompt">${escapeHtml(job.prompt)}</div>
            <div class="cron-job-meta">
                <span class="cron-job-model">${escapeHtml(friendlyModelName(job.provider || deriveProviderFromModel(job.model), job.model))}</span>
                ${job.operator ? `<span class="cron-job-operator">${escapeHtml(job.operator)}</span>` : ''}
                <span class="cron-job-delivery">${deliveryLabel}</span>
                ${job.run_count ? `<span class="cron-job-runs">${job.run_count} runs</span>` : ''}
                ${job.last_run_at ? `<span class="cron-job-lastrun">Last: ${formatTime(job.last_run_at)}</span>` : ''}
                ${job.next_run_at ? `<span class="cron-job-nextrun">Next: ${formatTime(job.next_run_at)}</span>` : ''}
            </div>
            <div class="cron-job-actions">
                <button class="cron-action-btn" data-action="run" data-job-id="${job.id}" title="Run Now">&#9654;</button>
                <button class="cron-action-btn" data-action="${job.status === 'active' ? 'pause' : 'resume'}" data-job-id="${job.id}" title="${job.status === 'active' ? 'Pause' : 'Resume'}">${job.status === 'active' ? '&#9208;' : '&#9654;&#65039;'}</button>
                <button class="cron-action-btn" data-action="edit" data-job-id="${job.id}" title="Edit">&#9998;</button>
                <button class="cron-action-btn" data-action="history" data-job-id="${job.id}" title="History">&#128203;</button>
                <button class="cron-action-btn cron-action-delete" data-action="delete" data-job-id="${job.id}" title="Delete">&#128465;</button>
            </div>
        </div>`;
    }).join('');

    // Bind action buttons
    container.querySelectorAll('.cron-action-btn').forEach(btn => {
        btn.addEventListener('click', handleJobAction);
    });
}

// =============================================================================
// Job Actions
// =============================================================================

async function handleJobAction(e) {
    const btn = e.currentTarget;
    const action = btn.dataset.action;
    const jobId = btn.dataset.jobId;

    try {
        switch (action) {
            case 'run':
                toast('Running job...');
                await runJobNow(jobId);
                toastSuccess('Job executed successfully');
                await refreshJobList();
                break;
            case 'pause':
                await pauseJob(jobId);
                toast('Job paused');
                await refreshJobList();
                break;
            case 'resume':
                await resumeJob(jobId);
                toastSuccess('Job resumed');
                await refreshJobList();
                break;
            case 'edit':
                openEditModal(jobId);
                break;
            case 'history':
                openHistoryModal(jobId);
                break;
            case 'delete':
                if (confirm('Delete this scheduled job? This cannot be undone.')) {
                    await deleteJob(jobId);
                    toast('Job deleted');
                    await refreshJobList();
                }
                break;
        }
    } catch (err) {
        console.error(`Action '${action}' failed:`, err);
        toastError(`Failed to ${action} job: ${err.message}`);
    }
}

// =============================================================================
// Edit Modal
// =============================================================================

async function openEditModal(jobId) {
    const modal = $('cronEditModal');
    if (!modal) return;

    // Reset form
    $('cronEditJobId').value = '';
    $('cronJobName').value = '';
    $('cronJobPrompt').value = '';
    $('cronFrequency').value = 'daily';
    $('cronTime').value = '07:00';
    $('cronDelivery').value = 'snapshot';
    $('cronDeliveryTarget').value = '';
    if ($('cronDeliverySource')) $('cronDeliverySource').value = 'manual';
    $('cronDeliveryTarget')?.classList.remove('hide');
    $('cronOneShot').checked = false;
    $('cronExpression').value = '';
    $('cronNextRuns')?.classList.add('hide'); // clear any stale next-run preview
    $('cronDeliveryTargetWrap').classList.add('hide');
    $('cronDayWrap').classList.add('hide');
    $('cronIntervalWrap').classList.add('hide');
    $('cronTimeWrap').classList.remove('hide');

    // Clear any error highlights
    ['cronJobName', 'cronJobPrompt', 'cronExpression'].forEach(id => {
        $(id)?.classList.remove('cron-input-error');
    });

    // Reset to simple tab
    document.querySelectorAll('.cron-tab').forEach(t => t.classList.remove('active'));
    document.querySelector('.cron-tab[data-tab="simple"]')?.classList.add('active');
    $('cronSimpleSchedule')?.classList.remove('hide');
    $('cronAdvancedSchedule')?.classList.add('hide');

    if (jobId) {
        // Edit mode - populate from existing job
        const job = allJobs.find(j => j.id === jobId);
        if (!job) return;

        $('cronEditTitle').textContent = 'Edit Scheduled Job';
        $('cronEditJobId').value = job.id;
        $('cronJobName').value = job.name;
        $('cronJobPrompt').value = job.prompt;
        $('cronDelivery').value = job.delivery || 'snapshot';
        $('cronDeliveryTarget').value = job.delivery_target || '';
        $('cronOneShot').checked = !!job.one_shot;

        // Provider+model: prefer the backend's canonical `provider` key; for a
        // legacy job that predates M4.1 derive it from the coarse `model` word.
        const provider = job.provider || deriveProviderFromModel(job.model);
        if ($('cronProvider')) $('cronProvider').value = provider;
        // Hydrate the model list for that provider, then select the saved id.
        // job.model holds the specific id ('' = Auto / provider default).
        await hydrateCronModelSelect(provider, job.model || '');

        // Populate operator selector with existing job's operator
        populateOperatorSelector(job.operator);

        if (['sms', 'voice_call'].includes(job.delivery)) {
            $('cronDeliveryTargetWrap').classList.remove('hide');
            populateContactSelector(job.delivery_target);
        }

        // Set cron expression in advanced tab
        $('cronExpression').value = job.schedule;

        // Try to parse into simple mode
        if (!parseToSimple(job.schedule)) {
            // Switch to advanced tab
            document.querySelectorAll('.cron-tab').forEach(t => t.classList.remove('active'));
            document.querySelector('.cron-tab[data-tab="advanced"]')?.classList.add('active');
            $('cronSimpleSchedule')?.classList.add('hide');
            $('cronAdvancedSchedule')?.classList.remove('hide');
        }
    } else {
        $('cronEditTitle').textContent = 'New Scheduled Job';
        // Default provider = last-used (mirrors chat's bb_provider), else Google.
        const provider = localStorage.getItem('bb_cron_provider') || CRON_DEFAULT_PROVIDER;
        if ($('cronProvider')) $('cronProvider').value = provider;
        // Hydrate models; selectedId undefined → picks up last-used per provider.
        await hydrateCronModelSelect(provider);
        // Populate operator selector defaulting to current operator
        populateOperatorSelector();
    }

    updateSchedulePreview();
    modal.classList.remove('hide');
}

function parseToSimple(cron) {
    // Try to parse a cron expression back into simple mode fields
    const parts = cron.split(' ');
    if (parts.length !== 5) return false;

    const [min, hour, dom, mon, dow] = parts;

    // Every hour: 0 * * * *
    if (min === '0' && hour === '*' && dom === '*' && mon === '*' && dow === '*') {
        $('cronFrequency').value = 'hourly';
        $('cronTimeWrap').classList.add('hide');
        $('cronDayWrap').classList.add('hide');
        $('cronIntervalWrap').classList.add('hide');
        return true;
    }

    // Every N minutes: */N * * * *
    if (min.startsWith('*/') && hour === '*' && dom === '*' && mon === '*' && dow === '*') {
        $('cronFrequency').value = 'custom_interval';
        $('cronIntervalValue').value = min.slice(2);
        $('cronIntervalUnit').value = 'minutes';
        $('cronTimeWrap').classList.add('hide');
        $('cronDayWrap').classList.add('hide');
        $('cronIntervalWrap').classList.remove('hide');
        return true;
    }

    // Every N hours: 0 */N * * *
    if (min === '0' && hour.startsWith('*/') && dom === '*' && mon === '*' && dow === '*') {
        $('cronFrequency').value = 'custom_interval';
        $('cronIntervalValue').value = hour.slice(2);
        $('cronIntervalUnit').value = 'hours';
        $('cronTimeWrap').classList.add('hide');
        $('cronDayWrap').classList.add('hide');
        $('cronIntervalWrap').classList.remove('hide');
        return true;
    }

    // Daily: M H * * *
    if (/^\d+$/.test(min) && /^\d+$/.test(hour) && dom === '*' && mon === '*' && dow === '*') {
        $('cronFrequency').value = 'daily';
        $('cronTime').value = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
        $('cronTimeWrap').classList.remove('hide');
        $('cronDayWrap').classList.add('hide');
        $('cronIntervalWrap').classList.add('hide');
        return true;
    }

    // Weekly: M H * * D
    if (/^\d+$/.test(min) && /^\d+$/.test(hour) && dom === '*' && mon === '*' && /^\d+$/.test(dow)) {
        $('cronFrequency').value = 'weekly';
        $('cronTime').value = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
        $('cronDay').value = dow;
        $('cronTimeWrap').classList.remove('hide');
        $('cronDayWrap').classList.remove('hide');
        $('cronIntervalWrap').classList.add('hide');
        return true;
    }

    return false;
}

// =============================================================================
// Schedule Builder
// =============================================================================

function simpleToCron() {
    const freq = $('cronFrequency')?.value;
    const time = $('cronTime')?.value || '07:00';
    const [hours, minutes] = time.split(':').map(Number);
    const day = $('cronDay')?.value;

    switch (freq) {
        case 'hourly':
            return { cron: '0 * * * *', hint: 'Every hour' };
        case 'daily':
            return { cron: `${minutes} ${hours} * * *`, hint: `Daily at ${formatTimeStr(hours, minutes)}` };
        case 'weekly':
            return { cron: `${minutes} ${hours} * * ${day}`, hint: `Weekly on ${dayName(day)} at ${formatTimeStr(hours, minutes)}` };
        case 'custom_interval': {
            const val = parseInt($('cronIntervalValue')?.value || '30');
            const unit = $('cronIntervalUnit')?.value || 'minutes';
            if (unit === 'minutes') return { cron: `*/${val} * * * *`, hint: `Every ${val} minutes` };
            return { cron: `0 */${val} * * *`, hint: `Every ${val} hours` };
        }
        default:
            return { cron: '0 7 * * *', hint: 'Daily at 7:00 AM' };
    }
}

function updateSchedulePreview() {
    // Check which tab is active
    const simpleActive = !$('cronSimpleSchedule')?.classList.contains('hide');
    if (simpleActive) {
        const { hint } = simpleToCron();
        const preview = $('cronPreviewText');
        if (preview) preview.textContent = `Runs ${hint.toLowerCase()}`;
    } else {
        const expr = $('cronExpression')?.value || '';
        const preview = $('cronExpressionPreview');
        if (preview) {
            preview.textContent = expr ? describeCron(expr) : 'Enter a cron expression';
        }
    }
    // Live next-run preview (debounced POST /api/cron/preview).
    scheduleNextRunPreview();
}

// =============================================================================
// Next-run Preview (M5.2 — POST /api/cron/preview while editing)
// =============================================================================

let _previewTimer = null;
// Monotonic token so a stale in-flight response can't clobber a newer one
// (debounce stops most overlap, but a slow request could still land late).
let _previewSeq = 0;

/**
 * Resolve the cron expression the form currently describes, using the SAME
 * schedule-building logic the save path uses: simpleToCron() for the simple
 * tab, the raw #cronExpression for the advanced tab. Returns '' when there's
 * nothing to preview yet (advanced tab, empty field).
 * @returns {string}
 */
function currentScheduleExpr() {
    const simpleActive = !$('cronSimpleSchedule')?.classList.contains('hide');
    if (simpleActive) return simpleToCron().cron;
    return $('cronExpression')?.value?.trim() || '';
}

/** Debounce the next-run preview ~300ms so we POST once per typing pause. */
function scheduleNextRunPreview() {
    clearTimeout(_previewTimer);
    _previewTimer = setTimeout(refreshNextRunPreview, 300);
}

/**
 * POST the current schedule to /api/cron/preview and render the next ~3 fire
 * times near the schedule field. A 400 (user mid-typing an invalid cron) shows
 * a subtle muted "invalid schedule" hint — NOT an error toast. Any other
 * failure (e.g. the route 404s on an un-restarted server, or a network drop)
 * silently hides the preview rather than nagging the user.
 */
async function refreshNextRunPreview() {
    const el = $('cronNextRuns');
    if (!el) return;

    const schedule = currentScheduleExpr();
    if (!schedule) {
        // Bump the token so an in-flight request can't re-render stale
        // times over the now-empty field after it resolves (M5b review).
        ++_previewSeq;
        el.classList.add('hide');
        return;
    }

    const seq = ++_previewSeq;
    let res;
    try {
        res = await fetch('/api/cron/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ schedule, count: 3 })
        });
    } catch {
        // Network/transport error — keep the UI quiet, hide the hint.
        if (seq === _previewSeq) el.classList.add('hide');
        return;
    }

    if (seq !== _previewSeq) return; // a newer request superseded this one

    if (res.status === 400) {
        // Expected mid-typing invalid cron — subtle hint, no toast.
        el.classList.remove('hide');
        el.classList.add('cron-next-runs-invalid');
        el.textContent = '— invalid schedule';
        return;
    }

    if (!res.ok) {
        // 404 (route not live yet) or any other status — hide gracefully.
        el.classList.add('hide');
        return;
    }

    let data;
    try { data = await res.json(); } catch { el.classList.add('hide'); return; }
    if (seq !== _previewSeq) return;

    const runs = Array.isArray(data.next_runs) ? data.next_runs : [];
    if (runs.length === 0) {
        el.classList.remove('hide');
        el.classList.add('cron-next-runs-invalid');
        el.textContent = '— no upcoming runs';
        return;
    }

    el.classList.remove('hide', 'cron-next-runs-invalid');
    const label = document.createElement('span');
    label.className = 'cron-next-runs-label';
    label.textContent = 'Next runs:';
    el.replaceChildren(label, document.createTextNode(' ' + runs.map(formatPreviewTime).join(', ')));
}

/**
 * Render a box-local ISO fire time as a compact wall-clock label, e.g.
 * "Jun 25 3:00 PM". The backend returns tz-aware ISO strings, so Date parses
 * the offset correctly and toLocaleString reflects the box's local time.
 * @param {string} isoStr
 * @returns {string}
 */
function formatPreviewTime(isoStr) {
    try {
        return new Date(isoStr).toLocaleString(undefined, {
            month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit'
        });
    } catch {
        return isoStr;
    }
}

function describeCron(cron) {
    const parts = cron.trim().split(/\s+/);
    if (parts.length !== 5) return 'Invalid cron expression';
    try {
        const [min, hour, dom, mon, dow] = parts;
        if (min === '0' && hour === '*') return 'Every hour';
        if (min.startsWith('*/')) return `Every ${min.slice(2)} minutes`;
        if (hour.startsWith('*/')) return `Every ${hour.slice(2)} hours`;
        if (dom === '*' && mon === '*' && dow === '*' && /^\d+$/.test(min) && /^\d+$/.test(hour)) {
            return `Daily at ${formatTimeStr(parseInt(hour), parseInt(min))}`;
        }
        if (dom === '*' && mon === '*' && /^\d+$/.test(dow) && /^\d+$/.test(min) && /^\d+$/.test(hour)) {
            return `${dayName(dow)} at ${formatTimeStr(parseInt(hour), parseInt(min))}`;
        }
        return `Cron: ${cron}`;
    } catch {
        return `Cron: ${cron}`;
    }
}

function handleScheduleTab(e) {
    const tab = e.currentTarget;
    const target = tab.dataset.tab;

    document.querySelectorAll('.cron-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');

    if (target === 'simple') {
        $('cronSimpleSchedule')?.classList.remove('hide');
        $('cronAdvancedSchedule')?.classList.add('hide');
    } else {
        $('cronSimpleSchedule')?.classList.add('hide');
        $('cronAdvancedSchedule')?.classList.remove('hide');
        // Sync cron expression from simple mode
        const { cron } = simpleToCron();
        if ($('cronExpression')) $('cronExpression').value = cron;
    }
    updateSchedulePreview();
}

function setupScheduleBuilder() {
    // Frequency change
    const freq = $('cronFrequency');
    if (freq) {
        freq.addEventListener('change', () => {
            const v = freq.value;
            $('cronTimeWrap')?.classList.toggle('hide', v === 'hourly' || v === 'custom_interval');
            $('cronDayWrap')?.classList.toggle('hide', v !== 'weekly');
            $('cronIntervalWrap')?.classList.toggle('hide', v !== 'custom_interval');
            updateSchedulePreview();
        });
    }

    // Time/day/interval change
    ['cronTime', 'cronDay', 'cronIntervalValue', 'cronIntervalUnit'].forEach(id => {
        const el = $(id);
        if (el) el.addEventListener('change', updateSchedulePreview);
    });

    // Advanced cron expression input
    const cronExpr = $('cronExpression');
    if (cronExpr) {
        cronExpr.addEventListener('input', updateSchedulePreview);
    }
}

// =============================================================================
// Save Job
// =============================================================================

let _saving = false;
async function handleSaveJob() {
    if (_saving) return;
    _saving = true;
    const saveBtn = $('btnSaveCronJob');
    if (saveBtn) saveBtn.disabled = true;

    try { await _doSaveJob(); } finally {
        _saving = false;
        if (saveBtn) saveBtn.disabled = false;
    }
}

async function _doSaveJob() {
    const jobId = $('cronEditJobId')?.value;
    const name = $('cronJobName')?.value?.trim();
    const prompt = $('cronJobPrompt')?.value?.trim();
    const provider = $('cronProvider')?.value || CRON_DEFAULT_PROVIDER;
    const model = $('cronModelId')?.value || '';
    try {
        localStorage.setItem('bb_cron_provider', provider);
        localStorage.setItem(`bb_cron_model_${provider}`, model);
    } catch (_) { /* storage disabled — non-fatal */ }
    const delivery = $('cronDelivery')?.value;
    const oneShot = $('cronOneShot')?.checked || false;

    // Resolve delivery target from contact selector or manual input
    let deliveryTarget = '';
    const deliverySource = $('cronDeliverySource')?.value;
    if (deliverySource && deliverySource !== 'manual') {
        // Contact selected - the value is the phone number
        deliveryTarget = deliverySource;
    } else {
        deliveryTarget = $('cronDeliveryTarget')?.value?.trim() || '';
    }

    // Clear previous error highlights
    ['cronJobName', 'cronJobPrompt', 'cronExpression'].forEach(id => {
        $(id)?.classList.remove('cron-input-error');
    });

    let hasError = false;
    if (!name) {
        $('cronJobName')?.classList.add('cron-input-error');
        hasError = true;
    }
    if (!prompt) {
        $('cronJobPrompt')?.classList.add('cron-input-error');
        hasError = true;
    }
    if (hasError) {
        toastError('Please fill in all required fields');
        return;
    }

    // Determine schedule
    let schedule, frequencyHint;
    const simpleActive = !$('cronSimpleSchedule')?.classList.contains('hide');
    if (simpleActive) {
        const result = simpleToCron();
        schedule = result.cron;
        frequencyHint = result.hint;
    } else {
        schedule = $('cronExpression')?.value?.trim();
        if (!schedule) {
            $('cronExpression')?.classList.add('cron-input-error');
            toastError('Cron expression is required');
            return;
        }
        frequencyHint = describeCron(schedule);
    }

    try {
        // Fresh-box-safe operator fallback: the server-driven #cronOperator
        // dropdown is the primary source; fall back to current operator, then
        // the server operator list head, then empty (never a hardcoded name).
        const operator = $('cronOperator')?.value || getOperator() || (_operatorList && _operatorList[0]) || '';

        if (jobId) {
            // Update existing
            await updateJob(jobId, {
                name, prompt, schedule,
                frequency_hint: frequencyHint,
                provider, model, delivery,
                delivery_target: deliveryTarget || null,
                operator,
                one_shot: oneShot
            });
            toastSuccess('Job updated');
        } else {
            // Create new
            await createJob({
                name, prompt, schedule,
                frequency_hint: frequencyHint,
                provider, model, delivery,
                delivery_target: deliveryTarget || null,
                operator,
                one_shot: oneShot
            });
            toastSuccess('Job created');
        }

        $('cronEditModal')?.classList.add('hide');
        await refreshJobList();
    } catch (err) {
        console.error('Save job failed:', err);
        toastError(`Failed to save job: ${err.message}`);
    }
}

// =============================================================================
// History Modal
// =============================================================================

async function openHistoryModal(jobId) {
    const modal = $('cronHistoryModal');
    if (!modal) return;

    const job = allJobs.find(j => j.id === jobId);
    $('cronHistoryTitle').textContent = job ? `History: ${job.name}` : 'Job History';

    const container = $('cronHistoryList');
    container.innerHTML = '<div class="cron-empty-state">Loading...</div>';
    modal.classList.remove('hide');

    try {
        const history = await fetchHistory(jobId);
        if (history.length === 0) {
            container.innerHTML = '<div class="cron-empty-state">No execution history yet</div>';
            return;
        }

        container.innerHTML = history.map(h => {
            const hProvider = h.provider || job?.provider || deriveProviderFromModel(h.model);
            return `
            <div class="cron-history-item ${h.error ? 'cron-history-error' : 'cron-history-success'}">
                <div class="cron-history-time">${formatTime(h.run_at)}</div>
                <div class="cron-history-meta">
                    <span class="cron-history-model">${escapeHtml(friendlyModelName(hProvider, h.model))}</span>
                    <span class="cron-history-duration">${h.duration_ms}ms</span>
                    <span class="cron-history-status">${h.delivery_status || 'completed'}</span>
                </div>
                ${h.result ? `<div class="cron-history-result">${escapeHtml(h.result.substring(0, 300))}${h.result.length > 300 ? '...' : ''}</div>` : ''}
                ${h.error ? `<div class="cron-history-error-text">${escapeHtml(h.error)}</div>` : ''}
            </div>
        `;
        }).join('');
    } catch (err) {
        container.innerHTML = `<div class="cron-empty-state">Failed to load history: ${err.message}</div>`;
    }
}

// =============================================================================
// Contact Selector
// =============================================================================

async function populateContactSelector(existingPhone) {
    const select = $('cronDeliverySource');
    if (!select) return;

    // Reset to just the manual option
    select.innerHTML = '<option value="manual">Enter phone number</option>';

    const contacts = await fetchContacts();
    let matchedContact = false;

    contacts.forEach(c => {
        if (!c.phone) return;
        const label = c.name ? `${c.name} (${c.phone})` : c.phone;
        const opt = document.createElement('option');
        opt.value = c.phone;
        opt.textContent = label;
        select.appendChild(opt);

        // If editing and existing phone matches a contact, pre-select it
        if (existingPhone && c.phone === existingPhone) {
            matchedContact = true;
        }
    });

    if (existingPhone && matchedContact) {
        select.value = existingPhone;
        $('cronDeliveryTarget')?.classList.add('hide');
    } else if (existingPhone) {
        // Phone number not in contacts, show manual input
        select.value = 'manual';
        const manualInput = $('cronDeliveryTarget');
        if (manualInput) {
            manualInput.classList.remove('hide');
            manualInput.value = existingPhone;
        }
    } else {
        select.value = 'manual';
        $('cronDeliveryTarget')?.classList.remove('hide');
    }
}

function handleDeliverySourceChange() {
    const select = $('cronDeliverySource');
    const manualInput = $('cronDeliveryTarget');
    if (!select || !manualInput) return;

    if (select.value === 'manual') {
        manualInput.classList.remove('hide');
        manualInput.value = '';
        manualInput.focus();
    } else {
        manualInput.classList.add('hide');
    }
}

// =============================================================================
// Utility Functions
// =============================================================================

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatTime(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        const now = new Date();
        const diffMs = now - d;

        // If less than 24 hours ago, show relative
        if (diffMs > 0 && diffMs < 86400000) {
            if (diffMs < 60000) return 'Just now';
            if (diffMs < 3600000) return `${Math.floor(diffMs / 60000)}m ago`;
            return `${Math.floor(diffMs / 3600000)}h ago`;
        }

        // Future dates or > 24h
        return d.toLocaleString(undefined, {
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit'
        });
    } catch {
        return isoStr;
    }
}

function formatTimeStr(hours, minutes) {
    const ampm = hours >= 12 ? 'PM' : 'AM';
    const h = hours % 12 || 12;
    return `${h}:${String(minutes).padStart(2, '0')} ${ampm}`;
}

function dayName(dayNum) {
    return ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][parseInt(dayNum)] || `Day ${dayNum}`;
}

// =============================================================================
// Live Polling — refresh job list while cron manager is open
// =============================================================================

function _startPolling() {
    _stopPolling();
    _pollInterval = setInterval(refreshJobList, 5000);
}

function _stopPolling() {
    if (_pollInterval) {
        clearInterval(_pollInterval);
        _pollInterval = null;
    }
}

// =============================================================================
// Init & Export
// =============================================================================

export function initCronManager() {
    // Open manager modal — start live polling
    const btnOpen = $('btnCronManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', async () => {
            const modal = $('cronManagerModal');
            if (modal) modal.classList.remove('hide');
            await refreshJobList();
            _startPolling();
        });
    }

    // Close buttons — stop polling when manager closes
    ['btnCloseCronManager', 'btnCloseCronEdit', 'btnCloseCronHistory'].forEach(id => {
        const btn = $(id);
        if (btn) btn.addEventListener('click', () => {
            btn.closest('.modal').classList.add('hide');
            if (id === 'btnCloseCronManager') _stopPolling();
        });
    });

    // New Job button
    const btnNew = $('btnNewCronJob');
    if (btnNew) {
        btnNew.addEventListener('click', () => openEditModal(null));
    }

    // Save Job button
    const btnSave = $('btnSaveCronJob');
    if (btnSave) {
        btnSave.addEventListener('click', handleSaveJob);
    }

    // Cancel edit
    const btnCancel = $('btnCancelCronEdit');
    if (btnCancel) {
        btnCancel.addEventListener('click', () => $('cronEditModal')?.classList.add('hide'));
    }

    // Provider change → re-hydrate the model dropdown (mirrors the chat
    // composer's providerSelect → fetchAvailableModels → rebuild flow).
    const providerSelect = $('cronProvider');
    if (providerSelect) {
        providerSelect.addEventListener('change', async () => {
            const provider = providerSelect.value;
            try { localStorage.setItem('bb_cron_provider', provider); } catch (_) { /* non-fatal */ }
            await hydrateCronModelSelect(provider);
        });
    }

    // Search input (debounced)
    const searchInput = $('cronSearchInput');
    if (searchInput) {
        let searchTimeout;
        searchInput.addEventListener('input', () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                currentSearch = searchInput.value;
                renderFilteredJobs();
            }, 200);
        });
    }

    // Status filter
    const statusFilter = $('cronStatusFilter');
    if (statusFilter) {
        statusFilter.addEventListener('change', () => {
            currentFilter = statusFilter.value;
            refreshJobList();
        });
    }

    // Schedule tabs
    document.querySelectorAll('.cron-tab').forEach(tab => {
        tab.addEventListener('click', handleScheduleTab);
    });

    // Schedule builder listeners
    setupScheduleBuilder();

    // Delivery type change (show/hide phone number + populate contacts)
    const deliverySelect = $('cronDelivery');
    if (deliverySelect) {
        deliverySelect.addEventListener('change', () => {
            const needsTarget = ['sms', 'voice_call'].includes(deliverySelect.value);
            $('cronDeliveryTargetWrap')?.classList.toggle('hide', !needsTarget);
            if (needsTarget) {
                populateContactSelector();
            }
        });
    }

    // Contact source selector change
    const deliverySourceSelect = $('cronDeliverySource');
    if (deliverySourceSelect) {
        deliverySourceSelect.addEventListener('change', handleDeliverySourceChange);
    }

    // Clear error highlighting on input
    ['cronJobName', 'cronJobPrompt', 'cronExpression'].forEach(id => {
        const el = $(id);
        if (el) {
            el.addEventListener('input', () => el.classList.remove('cron-input-error'));
        }
    });

    // Click outside modal to close
    ['cronManagerModal', 'cronEditModal', 'cronHistoryModal'].forEach(id => {
        const modal = $(id);
        if (modal) {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    modal.classList.add('hide');
                    if (id === 'cronManagerModal') _stopPolling();
                }
            });
        }
    });
}
