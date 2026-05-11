// Top-level orchestrator for AI BlackBox onboarding wizard.
// Reads ?mode= from URL — "setup" (default) is linear flow, "manage" is
// a step-grid landing page (Phase 2.10 — not yet implemented).

const STEPS = [
    "welcome", "tailscale", "api_keys",
    "optional_integrations", "pair_phone", "operator", "done",
];

const STEP_LABELS = {
    welcome: "WELCOME",
    tailscale: "TAILNET",
    api_keys: "KEYS",
    optional_integrations: "INTEGRATIONS",
    pair_phone: "PAIR",
    operator: "OPERATOR",
    done: "DONE",
};

const params = new URLSearchParams(location.search);
const MODE = params.get("mode") === "manage" ? "manage" : "setup";

let state = null;
let currentStepIdx = 0;

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
    container.innerHTML = `<div class="ob-loading">Loading ${stepName}&hellip;</div>`;
    try {
        const mod = await import(`./steps/${stepName}.js`);
        await mod.render(container, { state, next, back, skip, mode: MODE });
    } catch (e) {
        // Phase 2.1.1 ships before step components exist (Phases 2.2-2.7).
        // Render a clear placeholder so we know which step we're missing.
        container.innerHTML = `
            <div class="ob-step-missing">
                <h2 class="ob-step-title">Step coming soon: <em>${stepName}</em></h2>
                <p class="ob-step-lede">
                    The wizard shell is alive, but the <code>${stepName}</code>
                    step component hasn't been built yet (Phase 2.${STEPS.indexOf(stepName) + 2} of the onboarding plan).
                </p>
                <p class="ob-step-helper">Error: ${e.message}</p>
            </div>
        `;
    }
    updateProgress();
}

function updateProgress() {
    const pct = (currentStepIdx / (STEPS.length - 1)) * 100;
    const bar = document.getElementById("ob-progress-bar-fill");
    const stepNum = document.getElementById("ob-progress-step-num");
    const stepLabel = document.getElementById("ob-progress-step");
    if (bar) bar.style.width = pct + "%";
    if (stepNum) stepNum.textContent = String(currentStepIdx + 1).padStart(2, "0");
    if (stepLabel) stepLabel.textContent = STEP_LABELS[STEPS[currentStepIdx]] || "";
}

async function next() {
    if (currentStepIdx < STEPS.length - 1) {
        await fetch("/onboarding/step/complete", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({step: STEPS[currentStepIdx]}),
        });
        currentStepIdx++;
        await renderStep();
    }
}

async function back() {
    if (currentStepIdx > 0) {
        currentStepIdx--;
        await renderStep();
    }
}

async function skip() {
    await fetch("/onboarding/step/skip", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({step: STEPS[currentStepIdx]}),
    });
    if (currentStepIdx < STEPS.length - 1) {
        currentStepIdx++;
        await renderStep();
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
                <p class="ob-step-helper">Error: ${e.message}</p>
            </div>
        `;
    }
})();
