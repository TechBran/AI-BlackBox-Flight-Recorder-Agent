// Web-search step — lets the operator choose which web-search providers are
// ENABLED and which one is the DEFAULT. The same provider-toggle step as image
// (../provider-toggle-step.js); this module is its spec.
//
// The BlackBox can answer with live web results from several providers. Each
// provider (except DuckDuckGo) needs an API key entered in the Keys step; a
// provider whose key is absent is hidden here (you enable it once the key is
// in). DuckDuckGo is keyless and always available.
//
// It reads the .web_search block of GET /onboarding/current-config and saves
//   {secrets:{WEB_SEARCH_ENABLED:"a,b,c", WEB_SEARCH_DEFAULT:"a"}}
// to POST /onboarding/save, then advances via next(), which POSTs
// /onboarding/step/complete {step:"web_search"}.

import { providerToggleStep } from "../provider-toggle-step.js";

// Display order + labels (per the multi-provider web-search spec). The provider
// IDs match the backend PROVIDER_ENV keys exactly.
const PROVIDERS = [
    { id: "perplexity", label: "Perplexity" },
    { id: "openai", label: "OpenAI" },
    { id: "gemini", label: "Google Gemini" },
    { id: "grok", label: "Grok (web)" },
    { id: "grok_x", label: "Grok (X / Twitter)" },
    { id: "duckduckgo", label: "DuckDuckGo (free — no API key)" },
];

export const render = providerToggleStep({
    prefix: "ws",
    sectionClass: "ob-web-search",
    sigilNum: "06",
    sigilLabel: "WEB SEARCH",
    eyebrow: "Web-search providers",
    titleHtml: "Choose how the BlackBox <em>searches the web</em>.",
    ledeHtml: `Pick which providers the BlackBox may use for live web
                    results, then set a preferred default. <strong>DuckDuckGo</strong>
                    is free and needs no key. The others bill to the API key you
                    entered in the Keys step &mdash; only providers with a key on
                    file are shown here. The default is always one of the enabled
                    providers.`,
    loadingHtml: "Loading web-search options&hellip;",
    emptyHtml: "No web-search providers are configured yet. " +
        "Add an API key in the Keys step (or use DuckDuckGo, which is keyless).",
    backDefault: "speech",
    cfgKey: "web_search",
    providers: PROVIDERS,
    keylessIds: ["duckduckgo"],  // the keyless floor: always shown, badged "Free"
    envEnabled: "WEB_SEARCH_ENABLED",
    envDefault: "WEB_SEARCH_DEFAULT",
});
