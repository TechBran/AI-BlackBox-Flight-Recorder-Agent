// Welcome step — first screen of the onboarding wizard.
// Renders the brand introduction + 4 feature highlights + primary CTA.
// CTA wires to ctx.next() which POSTs /onboarding/step/complete and advances.
//
// Visual reference: Portal/onboarding/_mocks/welcome.html (Brandon-approved 2026-05-11).
// All classes come from onboarding.css (extracted from the mock in T2.1.1).
//
// Pattern: subsequent step components (tailscale.js, api_keys.js, etc.) follow
// the same `export async function render(container, ctx)` signature and consume
// the same design-system classes from onboarding.css.

export async function render(container, { next, skip, sigil }) {
    container.innerHTML = `
        <section class="ob-step ob-welcome">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "01"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">Welcome</div>
            </aside>

            <div class="ob-step-body">
                <div class="ob-step-eyebrow">A new kind of personal infrastructure</div>

                <h1 class="ob-step-title">
                    Welcome to your <em>private</em> AI.
                </h1>

                <p class="ob-step-lede">
                    Voice, vision, memory, and tools &mdash; all running on hardware you own.
                    We'll walk through setup in a few minutes. Skip anything you're not
                    ready for and come back later.
                </p>

                <section class="ob-features" aria-label="Setup overview">
                    <article class="ob-feature">
                        <div class="ob-feature-num"><em>01</em></div>
                        <div class="ob-feature-body">
                            <h2 class="ob-feature-label">Your data, your hardware</h2>
                            <p class="ob-feature-desc">
                                Conversations, memory, and tools live on your BlackBox &mdash;
                                not in someone else's cloud.
                            </p>
                        </div>
                        <div class="ob-feature-meta">~ 30 seconds</div>
                    </article>

                    <article class="ob-feature">
                        <div class="ob-feature-num"><em>02</em></div>
                        <div class="ob-feature-body">
                            <h2 class="ob-feature-label">Reach it from anywhere</h2>
                            <p class="ob-feature-desc">
                                Tailscale gives you a private mesh. Phone, laptop, BlackBox &mdash;
                                secure access without exposing the public internet.
                            </p>
                        </div>
                        <div class="ob-feature-meta">~ 2 minutes</div>
                    </article>

                    <article class="ob-feature">
                        <div class="ob-feature-num"><em>03</em></div>
                        <div class="ob-feature-body">
                            <h2 class="ob-feature-label">Bring your own keys</h2>
                            <p class="ob-feature-desc">
                                OpenAI, Anthropic, Google &mdash; paste in your keys, pay providers
                                directly. No middle-man billing.
                            </p>
                        </div>
                        <div class="ob-feature-meta">~ 3 minutes</div>
                    </article>

                    <article class="ob-feature">
                        <div class="ob-feature-num"><em>04</em></div>
                        <div class="ob-feature-body">
                            <h2 class="ob-feature-label">Your phone is the remote</h2>
                            <p class="ob-feature-desc">
                                Pair your phone with a QR scan. On-the-go voice, vision,
                                and tool access &mdash; same memory.
                            </p>
                        </div>
                        <div class="ob-feature-meta">~ 1 minute</div>
                    </article>
                </section>

                <div class="ob-cta-row">
                    <button type="button" class="ob-cta" id="ob-welcome-cta">
                        Begin setup <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-welcome-skip">
                        Skip &mdash; I'll set up later
                    </button>
                </div>
            </div>
        </section>
    `;

    // Wire interactive elements after innerHTML is set.
    // CTA passes the `next` callback directly (orchestrator handles state advancement).
    document.getElementById("ob-welcome-cta").addEventListener("click", next);
    document.getElementById("ob-welcome-skip").addEventListener("click", skip);
}
