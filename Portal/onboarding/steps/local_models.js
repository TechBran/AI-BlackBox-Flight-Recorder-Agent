// On-box local model stack step (M8) — PLACEHOLDER. Full UI lands in Task 8.4.
// Registered now so the parity guard (a steps/<step>.js module must exist for
// every STEPS entry) is satisfied and the wizard can advance past this step.
export async function render(container, { next, back, skip, sigil }) {
    const s = sigil || { num: "05", backLabel: "memory & search" };
    container.innerHTML = `
        <section class="ob-step ob-local-models">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${s.num}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">ON-BOX</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    On-box model stack
                </div>
                <h1 class="ob-step-title">Run models <em>on the box</em>.</h1>
                <p class="ob-step-lede">Setup for on-box speech, memory, and
                    reranking is being prepared. You can skip for now.</p>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-lm-back">
                        <span aria-hidden="true">&larr;</span> Back to ${s.backLabel ? s.backLabel.toLowerCase() : "memory & search"}
                    </button>
                    <button type="button" class="ob-cta" id="ob-lm-continue">
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-lm-skip">
                        Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>`;
    document.getElementById("ob-lm-back").addEventListener("click", back);
    document.getElementById("ob-lm-continue").addEventListener("click", next);
    document.getElementById("ob-lm-skip").addEventListener("click", skip);
}
