# aiblackboxfc.com — Website Revamp Design (2026-06-27)

**Status:** Approved in brainstorming with Brandon. Build on `main` (staging-as-prod box).
**Source:** `Apps/landing-page/` (`index.html`, `style.css`, `app.js`).
**Hosting:** Firebase Hosting (project `ai-blackbox-landing`); dev preview on `python3 server.py` → `http://localhost:8070`. Ships via `firebase deploy --only hosting` after Brandon approves the local preview.

## Goal

Catch the marketing site up to the 2026 product, fix pricing/hardware, add the cellular + Workspace + voice + Android stories, and rewrite the whole thing in a friendly, plainspoken voice. The current site is a 2025 snapshot: single price, generic "Mini-ITX PC," no cellular/SMS/phone/Workspace, polished-corporate tone, no GitHub/YouTube.

## Locked decisions

### Commerce (two tiers + add-on)
| SKU | Hardware | Price |
|---|---|---|
| **AI BlackBox — Standard** | Minisforum **MS-01** (Core i9-13900H) | **$2,000** |
| **AI BlackBox — Ultra** | Minisforum **MS-02 Ultra** (Core Ultra 9 285HX) | **$2,500** |
| **TG200 LTE Cellular Add-On** | Yeastar TG200 LTE-A (street ~$549; our setup markup) | **$750** |
| **Software-only** | BYO hardware | *Coming soon — teased, not buyable yet* |

- **Both tiers ship identical:** 1TB NVMe + 32GB DDR5, user-expandable. The difference is the **CPU** and the **ceiling**: MS-01 → up to 64GB RAM, 10GbE, 3× M.2; MS-02 Ultra → up to **256GB RAM**, 25GbE, PCIe 5.0, full-size GPU slot, NPU.
- Box works **without** the TG200; cellular is a true optional add-on.
- **Every kit includes the encrypted Apricorn USB** = one-click backup of snapshot volume + manifest + keys. (The one-click backup feature is not built in the BlackBox yet; Brandon approved advertising it — phrase value-forward, not as a literal "click here today" UI promise.)
- Checkout stays **Stripe**. For now all buy buttons point at the existing Stripe link (`buy.stripe.com/aFa3cufqIdLAgVk8Ib4ow00`). **Follow-up:** create per-SKU Stripe prices ($2,000 / $2,500 / $750) so checkout reflects the chosen config.

### Tone
**Friendly & plainspoken** — casual and warm, but still credible enough to sell $2K+ hardware. Contractions, second person, short sentences, no "sovereign intelligence appliance" corporate-speak. Confidence without swagger.

### Capabilities cleared to advertise (all customer-ready, confirmed)
Cellular SMS + phone/voice (TG200) · Google Workspace (Docs/Sheets/Slides/Drive/Gmail/Calendar) · Voice agent + premium TTS · Android app. Honesty rule: only advertise what a customer can use day one — do **not** claim MMS/picture messaging (TG200 = SMS + voice only, per hardware research).

### Out of scope (this pass)
GitHub links (repo is private — omit entirely). YouTube **embed** (Brandon still producing the overview video) — build an intentional, styled **video-ready slot** in the Demo section + footer placeholder; drop the embed in later.

## Structure — single scrolling page, section map
1. **Nav** — links: Features · Memory · Cellular · Specs · Pricing · FAQ; CTA "From $2,000".
2. **Hero** — friendly tone, two-tier framing, CTAs "Grab one — from $2,000" / "Watch it work".
3. **Subscription-trap comparison** — recomputed: SaaS 3-yr **$6,732** vs BlackBox Standard **$2,360** ($2,000 + ~$10/mo BYOK) = **save $4,372**, pays for itself in **~12 months**.
4. **Features grid** — refresh copy; expand to include Cellular + Workspace teasers.
5. **Memory** (core differentiator) — keep; ground numbers (use "2,000+ snapshots" and qualitative "high-dimensional embeddings" rather than brittle exact dims that go stale when the embedding model changes).
6. 🆕 **Cellular (TG200)** — "Give your AI a phone number." SMS + phone/voice over a real cell line. +$750.
7. 🆕 **Google Workspace** — "It lives in your Google, too."
8. 🆕 **Hardware & Specs** — MS-01 vs MS-02 Ultra two-column spec comparison.
9. **How It Works** — update unbox step to the real mini-PCs + Apricorn one-click backup.
10. **Demo** — intentional video-ready slot ("overview video dropping soon").
11. **Apps showcase** — keep Whisper Everywhere + Storybox.
12. **About / founder / tech stack** — keep bio; ground tech stack (fix stale embedding-dim claim).
13. **Pricing** — two tier cards + TG200 add-on strip + software-only teaser.
14. **FAQ** — add tiers, cellular, software-only; keep privacy/BYOK/box-contents.
15. **Footer** — links + brandon@aiblackboxfc.com + YouTube "coming soon" placeholder.

## Visual system (reuse existing tokens)
Dark theme `--color-bg #000`, surfaces `#0a0a0a`/`#141414`; brand red `--color-primary #ff4a4a`, `--gradient-primary #ff4a4a→#ff6b6b→#ff8585`; green `#22c55e` for "good"; Inter + JetBrains Mono; radius + glow tokens. New sections reuse `.container`, `.section-header`, card patterns, `.btn*`, `.gradient-text`. New CSS appended to `style.css` only for: two-tier pricing grid, add-on strip, software-only teaser, cellular/workspace layouts, hardware spec table, video-ready slot.

## Build steps
1. Rewrite `index.html` (all sections, friendly tone, new sections, two-tier pricing). Cache-bust `?v=` on css/js; add inline SVG favicon.
2. Append new component CSS to `style.css`.
3. Update `app.js` only if new interactions needed (existing FAQ accordion + checkout handler reused).
4. Preview on `:8070`, iterate with Brandon, then `firebase deploy`.
5. Snapshot the work (`/chat/save` auto-mint via Orchestrator HTTP — MCP tools are disconnected this session).
