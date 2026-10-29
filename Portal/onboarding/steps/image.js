// Image-generation step — lets the operator choose which image providers are
// ENABLED and which one is the DEFAULT. The same provider-toggle step as web
// search (../provider-toggle-step.js); this module is its spec.
//
// The BlackBox can generate images from several providers. Every image provider
// needs an API key entered in the Keys step — there is NO keyless floor (unlike
// web-search's DuckDuckGo). A provider whose key is absent is hidden here; you
// enable it once the key is in. If no provider has a key on file, this step
// tells the operator to add an image-provider key in the API Keys step.
//
// It reads the .image block of GET /onboarding/current-config and saves
//   {secrets:{IMAGE_ENABLED:"a,b,c", IMAGE_DEFAULT:"a"}}
// to POST /onboarding/save, then advances via next(), which POSTs
// /onboarding/step/complete {step:"image"}.

import { providerToggleStep } from "../provider-toggle-step.js";

// Display order + labels (per the multi-provider image-generation spec). The
// provider IDs match the backend FEATURES["image"]["provider_env"] keys exactly.
const PROVIDERS = [
    { id: "gemini", label: "Gemini Nano Banana" },
    { id: "openai", label: "OpenAI (gpt-image)" },
    { id: "grok", label: "Grok image" },
];

export const render = providerToggleStep({
    prefix: "img",
    sectionClass: "ob-image",
    sigilNum: "07",
    sigilLabel: "IMAGE",
    eyebrow: "Image-generation providers",
    titleHtml: "Choose how the BlackBox <em>generates images</em>.",
    ledeHtml: `Pick which providers the BlackBox may use to generate images,
                    then set a preferred default. Every image provider bills to the
                    API key you entered in the Keys step &mdash; only providers with
                    a key on file are shown here. The default is always one of the
                    enabled providers.`,
    loadingHtml: "Loading image options&hellip;",
    emptyHtml: "No image providers are configured yet. " +
        "Add an image-provider API key (Gemini, OpenAI, or Grok) in the API Keys step to enable image generation.",
    backDefault: "web search",
    cfgKey: "image",
    providers: PROVIDERS,
    keylessIds: [],
    envEnabled: "IMAGE_ENABLED",
    envDefault: "IMAGE_DEFAULT",
});
