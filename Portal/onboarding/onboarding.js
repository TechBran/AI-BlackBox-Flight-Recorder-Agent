// Top-level orchestrator for AI BlackBox onboarding wizard.
// Reads ?mode= from URL — "setup" (default) is linear flow, "manage" is
// a step-grid landing page (Phase 2.10 — not yet implemented).

const STEPS = [
    "welcome", "tailscale", "api_keys",
    "optional_integrations", "transcription", "pair_phone", "cli_agents", "operator", "done",
];

// IMPORTANT: if STEPS array changes, update STEP_LABELS to match.
// (We intentionally don't auto-derive — some labels need custom casing
// like "TAILNET" not "TAILSCALE" or "EXTRAS" not "OPTIONAL_INTEGRATIONS".)
// Keep labels short — long values cause header-overflow in the top-right
// "STEP NN / 08 LABEL" chrome (see T2.5.1 sign-off).
const STEP_LABELS = {
    welcome: "WELCOME",
    tailscale: "TAILNET",
    api_keys: "KEYS",
    optional_integrations: "EXTRAS",
    transcription: "SPEECH",
    pair_phone: "PAIR",
    cli_agents: "AGENTS",
    operator: "OPERATOR",
    done: "DONE",
};

const params = new URLSearchParams(location.search);
const MODE = params.get("mode") === "manage" ? "manage" : "setup";

let state = null;
let currentStepIdx = 0;
let busy = false;

// E7 final (Brandon's MSO2 Ultra testing 2026-05-16): target="_blank" links
// don't open browser in Tauri's WebKitGTK webview. Multiple Tauri-side attempts
// (on_navigation, firefox direct-spawn from Rust with explicit env) all failed
// because Tauri's webview policy / env-stripping fights us. Solution: backend
// FastAPI (running as bbx user) spawns firefox with the proper user-session
// env reconstructed from os.getuid(). This document-level click handler
// intercepts target=_blank anchors at capture phase and POSTs the URL to
// /onboarding/open-url. Backend handles the rest. Works for every wizard
// step automatically — no per-anchor JS wiring. Plain-browser (remote-
// wizard access via Tailscale) gets the same path; browser-spawn-from-server
// is unusual but the customer's already on the device's tailnet so they ARE
// the device session — opens firefox on the device, which is what they want
// for the auth/admin flows they were trying to reach.
document.addEventListener("click", function (e) {
    const a = e.target.closest && e.target.closest("a[target=\"_blank\"]");
    if (!a || !a.href) return;
    // Only intercept external links — localhost target=_blank passes through.
    try {
        const u = new URL(a.href);
        if (u.host === "localhost" || u.host === "127.0.0.1") return;
    } catch (_) { return; }
    e.preventDefault();
    fetch("/onboarding/open-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: a.href }),
    }).catch(() => {
        // Last-resort fallback: navigate current tab so URL is at least visible
        // in the wizard's webview address handling.
        window.location.assign(a.href);
    });
}, true);  // capture phase — fires before any per-anchor handlers

function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function ensureToastStyles() {
    if (document.getElementById("ob-toast-styles")) return;
    const style = document.createElement("style");
    style.id = "ob-toast-styles";
    style.textContent = `
        .ob-toast {
            position: fixed;
            bottom: var(--ob-space-8, 2rem);
            left: 50%;
            transform: translateX(-50%) translateY(20px);
            background: var(--ob-surface-elevated, #0a0a0a);
            color: var(--ob-text-primary, #fff);
            border: 1px solid var(--ob-accent, #cc0000);
            padding: var(--ob-space-3, 0.75rem) var(--ob-space-5, 1.25rem);
            font-family: var(--ob-font-body, ui-monospace, monospace);
            font-size: var(--ob-text-sm, 0.875rem);
            z-index: 9999;
            opacity: 0;
            pointer-events: none;
            transition: opacity 200ms, transform 200ms;
            box-shadow: var(--ob-accent-glow, 0 0 32px rgba(204, 0, 0, 0.18));
        }
        .ob-toast-visible {
            opacity: 1;
            transform: translateX(-50%) translateY(0);
            pointer-events: auto;
        }
    `;
    document.head.appendChild(style);
}

function showTransientError(msg) {
    // Minimal toast — appended to body, auto-dismisses after 4s.
    ensureToastStyles();
    let toast = document.getElementById("ob-toast");
    if (!toast) {
        toast = document.createElement("div");
        toast.id = "ob-toast";
        toast.className = "ob-toast";
        toast.setAttribute("role", "alert");
        document.body.appendChild(toast);
    }
    toast.textContent = msg;  // textContent — safe by construction
    toast.classList.add("ob-toast-visible");
    clearTimeout(toast._dismissTimer);
    toast._dismissTimer = setTimeout(() => {
        toast.classList.remove("ob-toast-visible");
    }, 4000);
}

async function fetchState() {
    const r = await fetch("/onboarding/state");
    if (!r.ok) {
        throw new Error(`/onboarding/state returned ${r.status}`);
    }
    state = await r.json();
    currentStepIdx = Math.max(0, STEPS.indexOf(state.current_step));
}

async function renderStep() {
    const stepName = STEPS[currentStepIdx];
    const container = document.getElementById("ob-step-container");
    container.innerHTML = `<div class="ob-loading">Loading ${escapeHtml(stepName)}&hellip;</div>`;
    try {
        const mod = await import(`./steps/${stepName}.js`);
        await mod.render(container, { state, next, back, skip, mode: MODE });
    } catch (e) {
        // Phase 2.1.1 ships before step components exist (Phases 2.2-2.7).
        // Render a clear placeholder so we know which step we're missing.
        container.innerHTML = `
            <div class="ob-step-missing">
                <h2 class="ob-step-title">Step coming soon: <em>${escapeHtml(stepName)}</em></h2>
                <p class="ob-step-lede">
                    The wizard shell is alive, but the <code>${escapeHtml(stepName)}</code>
                    step component hasn't been built yet (Phase 2.${STEPS.indexOf(stepName) + 2} of the onboarding plan).
                </p>
                <p class="ob-step-helper">Error: ${escapeHtml(e.message)}</p>
            </div>
        `;
    }
    updateProgress();
}

function updateProgress() {
    const pct = (currentStepIdx / (STEPS.length - 1)) * 100;
    const bar = document.getElementById("ob-progress-bar-fill");
    const stepNum = document.getElementById("ob-progress-step-num");
    const stepDenom = document.getElementById("ob-progress-step-total");
    const stepLabel = document.getElementById("ob-progress-step");
    if (bar) bar.style.width = pct + "%";
    if (stepNum) stepNum.textContent = String(currentStepIdx + 1).padStart(2, "0");
    if (stepDenom) stepDenom.textContent = String(STEPS.length).padStart(2, "0");
    if (stepLabel) stepLabel.textContent = STEP_LABELS[STEPS[currentStepIdx]] || "";
}

async function next() {
    if (currentStepIdx >= STEPS.length - 1) return;
    if (busy) return;
    busy = true;
    try {
        const r = await fetch("/onboarding/step/complete", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({step: STEPS[currentStepIdx]}),
        });
        if (!r.ok) {
            showTransientError(`Couldn't save step (${r.status}). Try again in a moment.`);
            return;  // do NOT advance
        }
        currentStepIdx++;
        await renderStep();
    } catch (e) {
        showTransientError(`Network error saving step. Check your connection.`);
    } finally {
        busy = false;
    }
}

async function back() {
    if (currentStepIdx <= 0) return;
    if (busy) return;
    busy = true;
    try {
        currentStepIdx--;
        await renderStep();
    } finally {
        busy = false;
    }
}

async function skip() {
    if (currentStepIdx >= STEPS.length - 1) return;
    if (busy) return;
    busy = true;
    try {
        const r = await fetch("/onboarding/step/skip", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({step: STEPS[currentStepIdx]}),
        });
        if (!r.ok) {
            showTransientError(`Couldn't record skip (${r.status}). Try again.`);
            return;
        }
        currentStepIdx++;
        await renderStep();
    } catch (e) {
        showTransientError(`Network error. Check your connection.`);
    } finally {
        busy = false;
    }
}

(async () => {
    try {
        await fetchState();
        if (state.is_complete && MODE === "setup") {
            location.href = "/ui";
            return;
        }
        await renderStep();
    } catch (e) {
        const container = document.getElementById("ob-step-container");
        container.innerHTML = `
            <div class="ob-step-error">
                <h2 class="ob-step-title">Setup unavailable</h2>
                <p class="ob-step-lede">Couldn't reach the BlackBox onboarding API. Check that the service is running, then refresh this page.</p>
                <p class="ob-step-helper">Error: ${escapeHtml(e.message)}</p>
            </div>
        `;
    }
})();
