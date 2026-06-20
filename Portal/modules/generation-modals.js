/**
 * generation-modals.js
 * Modal dialogs for image, video, and music generation
 *
 * Features:
 * - Nano Banana Pro (Gemini Image Generation) with aspect ratio, resolution, multiple images
 * - Reference image uploads for style/subject guidance
 * - Video generation with Veo 3.1
 * - Music generation with Lyria-002
 *
 * Source: Portal/app.js lines 2890-3081
 * Refactored: 2025-12-20
 * Updated: 2026-01-04 - Added Nano Banana Pro full features
 */

import { $, toast } from './core-utils.js';
import { generateImageAsync, generateVideoAsync, extendVideoAsync, generateMusicAsync } from './task-manager.js';
import { toggleSTTFor } from './tts-stt.js';

// =============================================================================
// State
// =============================================================================

/** Current video URL for extension */
let currentVideoToExtend = null;

/** Reference images for image generation (base64 encoded) */
let referenceImages = [];

/** Video start image for image-to-video (base64 encoded) */
let videoStartImageData = null;

/**
 * Image-generation provider catalog (from GET /image/catalog).
 * Each entry: { provider, label, default?, params:[{name,type,options?,min?,max?,default?}] }
 * Cached after the first successful fetch.
 */
let imageCatalog = null;

/** Fail-open single-provider catalog if GET /image/catalog is empty/errors. */
const IMAGE_CATALOG_FALLBACK = [{
    provider: 'gemini',
    label: 'Gemini Nano Banana',
    default: true,
    params: [
        { name: 'aspectRatio', type: 'enum', options: ['1:1', '16:9', '9:16', '4:3', '3:4'], default: '16:9' },
        { name: 'resolution', type: 'enum', options: ['1K', '2K'], default: '1K' },
        { name: 'numberOfImages', type: 'int', min: 1, max: 4, default: 1 }
    ]
}];

/** Human-friendly labels for known param names (fallback: the raw name). */
const IMAGE_PARAM_LABELS = {
    aspectRatio: 'Aspect Ratio',
    resolution: 'Resolution',
    numberOfImages: 'Images',
    size: 'Size',
    quality: 'Quality'
};

/**
 * Fetch the image provider catalog (cached). Fails open to a single-provider
 * fallback so the modal is never broken if the endpoint errors/returns empty.
 * @returns {Promise<Array>} list of provider entries
 */
async function fetchImageCatalog() {
    if (imageCatalog) return imageCatalog;
    try {
        const res = await fetch('/image/catalog');
        if (res.ok) {
            const data = await res.json();
            const providers = (data && data.providers) || [];
            imageCatalog = providers.length > 0 ? providers : IMAGE_CATALOG_FALLBACK;
        } else {
            console.warn('[ImageModal] /image/catalog returned', res.status, '- using fallback');
            imageCatalog = IMAGE_CATALOG_FALLBACK;
        }
    } catch (e) {
        console.error('[ImageModal] Failed to fetch /image/catalog:', e);
        imageCatalog = IMAGE_CATALOG_FALLBACK;
    }
    return imageCatalog;
}

/**
 * Render one provider param into a labeled control inside #imageParams.
 * enum -> <select>; int -> <input type="number">. Each control carries
 * data-param="<name>" so the value can be read back keyed by its catalog name.
 * @param {Object} param - { name, type, options?, min?, max?, default? }
 * @returns {HTMLElement} the .gen-option-group wrapper
 */
function buildImageParamControl(param) {
    const group = document.createElement('div');
    group.className = 'gen-option-group';

    const label = document.createElement('label');
    label.textContent = IMAGE_PARAM_LABELS[param.name] || param.name;
    group.appendChild(label);

    let control;
    if (param.type === 'enum') {
        control = document.createElement('select');
        (param.options || []).forEach(opt => {
            const o = document.createElement('option');
            o.value = opt;
            o.textContent = opt;
            if (String(opt) === String(param.default)) o.selected = true;
            control.appendChild(o);
        });
    } else {
        // int (or any non-enum): numeric input
        control = document.createElement('input');
        control.type = 'number';
        if (param.min !== undefined && param.min !== null) control.min = param.min;
        if (param.max !== undefined && param.max !== null) control.max = param.max;
        control.value = (param.default !== undefined && param.default !== null) ? param.default : 1;
    }
    control.setAttribute('data-param', param.name);
    control.dataset.paramType = param.type || 'string';
    label.htmlFor = `imageParam_${param.name}`;
    control.id = `imageParam_${param.name}`;
    group.appendChild(control);
    return group;
}

/**
 * Render the params for the given provider into #imageParams, replacing whatever
 * was there (so controls SWAP when the provider dropdown changes).
 * @param {string} provider - provider slug
 */
function renderImageParams(provider) {
    const container = $('imageParams');
    if (!container) return;
    container.innerHTML = '';
    const entry = (imageCatalog || []).find(p => p.provider === provider);
    if (!entry) return;
    (entry.params || []).forEach(param => {
        container.appendChild(buildImageParamControl(param));
    });
}

/**
 * Read back the rendered param controls in #imageParams into a plain object
 * keyed by each control's catalog param name (data-param). int controls are
 * parsed to Number; everything else is the raw string value.
 * @returns {Object} params payload fragment
 */
function gatherImageParams() {
    const params = {};
    const container = $('imageParams');
    if (!container) return params;
    container.querySelectorAll('[data-param]').forEach(el => {
        const name = el.getAttribute('data-param');
        if (el.dataset.paramType === 'int') {
            const n = parseInt(el.value, 10);
            params[name] = Number.isNaN(n) ? undefined : n;
        } else {
            params[name] = el.value;
        }
    });
    return params;
}

/**
 * Populate #imageProvider from the catalog and render the default provider's
 * params. Wires the change handler exactly once (idempotent across re-opens).
 */
async function setupImageProviderControls() {
    const providerSelect = $('imageProvider');
    if (!providerSelect) return;

    const catalog = await fetchImageCatalog();

    // Populate the provider dropdown (value=slug, text=label).
    providerSelect.innerHTML = '';
    let defaultProvider = catalog.length > 0 ? catalog[0].provider : null;
    catalog.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.provider;
        opt.textContent = p.label || p.provider;
        if (p.default) {
            opt.selected = true;
            defaultProvider = p.provider;
        }
        providerSelect.appendChild(opt);
    });

    // Render the (default) provider's params.
    if (defaultProvider) {
        providerSelect.value = defaultProvider;
        renderImageParams(defaultProvider);
    }

    // Wire change -> re-render params (only once).
    if (!providerSelect.dataset.bound) {
        providerSelect.addEventListener('change', () => {
            renderImageParams(providerSelect.value);
        });
        providerSelect.dataset.bound = '1';
    }
}

// =============================================================================
// Music Presets
// =============================================================================

/** Music genre presets with detailed prompts */
const MUSIC_PRESETS = {
    epic: "Orchestral instrumental with brass fanfares, timpani, and strings building in intensity",
    chill: "Mellow piano chords with soft drums and gentle bass, relaxed tempo",
    electronic: "Synthesizer arpeggios with four-on-the-floor drums and bass, upbeat tempo",
    acoustic: "Fingerpicked acoustic guitar with light percussion, warm and intimate",
    jazz: "Piano trio with walking bass and brushed drums, medium swing tempo",
    classical: "String quartet with flowing melodies and delicate harmonies",
    ambient: "Evolving synthesizer pads with subtle textures and reverb, slow and meditative",
    rock: "Electric guitar riffs with drums and bass, energetic tempo"
};

// =============================================================================
// Video Extension Modal
// =============================================================================

/**
 * Open video extension modal
 * @param {string} videoUrl - URL of video to extend
 */
export function openVideoExtensionModal(videoUrl) {
    currentVideoToExtend = videoUrl;
    const modal = $("videoExtensionModal");
    const promptArea = $("videoExtensionPrompt");

    if (promptArea) promptArea.value = '';
    if (modal) modal.classList.remove('hide');
}

/**
 * Setup video extension modal event handlers
 */
export function setupVideoExtensionModal() {
    const modal = $("videoExtensionModal");
    const extendBtn = $("btnExtendVideo");
    const cancelBtn = $("btnCancelExtension");
    const promptArea = $("videoExtensionPrompt");

    if (extendBtn) {
        extendBtn.addEventListener('click', async () => {
            const prompt = promptArea ? promptArea.value.trim() : '';

            if (!currentVideoToExtend) {
                toast('No video selected');
                return;
            }

            if (modal) modal.classList.add('hide');

            await extendVideoAsync(currentVideoToExtend, prompt);
            currentVideoToExtend = null;
        });
    }

    if (cancelBtn) {
        cancelBtn.addEventListener('click', () => {
            if (modal) modal.classList.add('hide');
            currentVideoToExtend = null;
        });
    }
}

// =============================================================================
// Image/Video Generation Modal
// =============================================================================

/**
 * Setup image/video generation modal event handlers
 * Includes Nano Banana Pro features: aspect ratio, resolution, reference images
 */
export function setupGenerationModal() {
    const modal = $("generationModal");
    const generateBtn = $("btnGenerate");
    const cancelBtn = $("btnCancelGenerate");
    const closeBtn = $("btnCloseGeneration");
    const promptArea = $("generationPrompt");
    const titleEl = $("generationTitle");
    const imageToVideoOption = $("imageToVideoOption");
    const imageGenOptions = $("imageGenOptions");
    const videoImagePreview = $("videoImagePreview");
    const micBtn = $("micGenerationPrompt");

    // Reference image elements
    const btnAddReference = $("btnAddReferenceImages");
    const referenceInput = $("referenceImageInput");
    const referencePreview = $("referenceImagesPreview");

    let currentType = null; // 'image' or 'video'

    // Setup Whisper mic button for voice input
    if (micBtn) {
        micBtn.addEventListener('click', () => {
            toggleSTTFor('generationPrompt', 'micGenerationPrompt');
        });
    }

    // Image Generation Button
    const btnGenImage = $("btnGenImage");
    if (btnGenImage) {
        btnGenImage.addEventListener('click', () => {
            currentType = 'image';
            if (titleEl) titleEl.textContent = 'Generate Image';
            if (promptArea) promptArea.value = '';
            if (imageToVideoOption) imageToVideoOption.classList.add('hide');
            if (imageGenOptions) imageGenOptions.classList.remove('hide');
            // Populate provider dropdown + render the default provider params
            // (catalog-driven; fails open to a single default provider).
            setupImageProviderControls();
            // Clear reference images
            referenceImages = [];
            if (referencePreview) {
                referencePreview.innerHTML = '';
                referencePreview.classList.add('hide');
            }
            if (modal) modal.classList.remove('hide');
        });
    }

    // Video Generation Button - now opens dedicated video modal
    const btnGenVideo = $("btnGenVideo");
    if (btnGenVideo) {
        btnGenVideo.addEventListener('click', () => {
            // Open the new dedicated video generation modal instead
            const videoModal = $("videoGenModal");
            if (videoModal) {
                // Reset fields
                const videoPrompt = $("videoPrompt");
                const videoNegative = $("videoNegativePrompt");
                if (videoPrompt) videoPrompt.value = '';
                if (videoNegative) videoNegative.value = '';

                // Reset image preview
                videoStartImageData = null;
                const preview = $("videoStartImagePreview");
                if (preview) preview.classList.add('hide');

                // Reset to defaults
                const aspectRatio = $("videoAspectRatio");
                const duration = $("videoDuration");
                const resolution = $("videoResolution");
                if (aspectRatio) aspectRatio.value = '16:9';
                if (duration) duration.value = '8';
                if (resolution) resolution.value = '720p';

                videoModal.classList.remove('hide');
            }
        });
    }

    // Reference Images Upload - with Android native picker support
    const hasAndroidFilePicker = typeof AndroidFilePicker !== 'undefined' &&
                                  typeof AndroidFilePicker.openFileChooser === 'function';

    // Setup callback for reference image selection (Android uses window.onNativeFileSelected)
    // We set a flag to indicate we're selecting reference images
    window.referenceImageSelectionActive = false;
    window.referenceImageSelectionCallback = async function(base64Data, mimeType, fileName) {
        console.log('[RefImages] Callback received:', fileName);
        window.referenceImageSelectionActive = false;

        if (referenceImages.length >= 14) {
            toast('Maximum 14 reference images allowed');
            return;
        }

        try {
            // Android provides base64 data directly
            referenceImages.push({
                data: base64Data,
                mimeType: mimeType || 'image/jpeg',
                name: fileName || 'image.jpg'
            });
            updateReferenceImagesPreview();
            toast(`Added ${fileName || 'image'}`);
        } catch (err) {
            console.error('Failed to process reference image:', err);
            toast('Failed to add image');
        }
    };

    // Patch the existing onNativeFileSelected to handle reference images and video start images
    const originalOnNativeFileSelected = window.onNativeFileSelected;
    window.onNativeFileSelected = function(base64Data, mimeType, fileName) {
        // Check for reference image selection (image generation)
        if (window.referenceImageSelectionActive && window.referenceImageSelectionCallback) {
            console.log('[RefImages] Routing to reference image callback');
            window.referenceImageSelectionCallback(base64Data, mimeType, fileName);
            return;
        }
        // Check for video start image selection (video generation image-to-video)
        if (window.videoStartImageSelectionActive && window.videoStartImageSelectionCallback) {
            console.log('[VideoStartImage] Routing to video start image callback');
            window.videoStartImageSelectionCallback(base64Data, mimeType, fileName);
            return;
        }
        // Call original handler for normal attachments
        if (originalOnNativeFileSelected) {
            originalOnNativeFileSelected(base64Data, mimeType, fileName);
        }
    };

    if (btnAddReference) {
        btnAddReference.addEventListener('click', () => {
            console.log('[RefImages] Add button clicked, Android picker:', hasAndroidFilePicker);

            if (hasAndroidFilePicker) {
                // Use native Android file picker for images only
                console.log('[RefImages] Using native Android file picker');
                window.referenceImageSelectionActive = true;
                try {
                    AndroidFilePicker.openFileChooser('image/*');
                } catch (e) {
                    console.error('[RefImages] Native file picker error:', e);
                    window.referenceImageSelectionActive = false;
                    // Fallback to HTML input
                    if (referenceInput) referenceInput.click();
                }
            } else if (referenceInput) {
                // Use standard HTML file input
                console.log('[RefImages] Using HTML file input');
                referenceInput.click();
            } else {
                toast('File picker not available');
            }
        });
    }

    if (referenceInput) {
        referenceInput.addEventListener('change', async (e) => {
            const files = Array.from(e.target.files);
            console.log('[RefImages] Files selected via HTML input:', files.length);
            if (files.length === 0) return;

            // Check total limit (14 max)
            if (referenceImages.length + files.length > 14) {
                toast(`Maximum 14 reference images allowed. You can add ${14 - referenceImages.length} more.`);
                return;
            }

            // Process each file
            for (const file of files) {
                if (!file.type.startsWith('image/')) continue;

                try {
                    const base64 = await fileToBase64(file);
                    referenceImages.push({
                        data: base64.split(',')[1], // Remove data:image/xxx;base64, prefix
                        mimeType: file.type,
                        name: file.name
                    });
                    console.log('[RefImages] Added:', file.name);
                } catch (err) {
                    console.error('Failed to process reference image:', err);
                }
            }

            // Update preview
            updateReferenceImagesPreview();
            referenceInput.value = ''; // Clear input for next selection
        });
    }

    // Generate Button Handler
    if (generateBtn) {
        generateBtn.addEventListener('click', async () => {
            const prompt = promptArea ? promptArea.value.trim() : '';
            if (!prompt) {
                toast('Please enter a prompt');
                return;
            }

            if (modal) modal.classList.add('hide');

            if (currentType === 'image') {
                // Provider-aware: gather the selected provider + each rendered
                // param value (keyed by its catalog name: aspectRatio/resolution/
                // numberOfImages/size/quality). Names match GET /image/catalog
                // and the backend GenIn model.
                const providerSelect = $('imageProvider');
                const provider = providerSelect ? providerSelect.value : 'gemini';
                const params = gatherImageParams();

                // Reference images stay a gemini-only feature in v1.
                const refs = (provider === 'gemini' && referenceImages.length > 0)
                    ? referenceImages
                    : null;

                // Generate with the dynamic params + provider.
                await generateImageAsync(prompt, {
                    provider,
                    ...params,
                    referenceImages: refs
                });

                // Clear reference images after generation
                referenceImages = [];
            } else if (currentType === 'video') {
                // Use globally stored image data
                await generateVideoAsync(prompt, window.videoImageData);
                // Clear image after generation
                window.videoImageData = null;
                if (videoImagePreview) videoImagePreview.classList.add('hide');
            }
        });
    }

    // Cancel/Close Button
    const closeModal = () => {
        if (modal) modal.classList.add('hide');
        referenceImages = [];
    };

    if (cancelBtn) {
        cancelBtn.addEventListener('click', closeModal);
    }
    if (closeBtn) {
        closeBtn.addEventListener('click', closeModal);
    }

    /**
     * Update reference images preview thumbnails
     */
    function updateReferenceImagesPreview() {
        if (!referencePreview) return;

        if (referenceImages.length === 0) {
            referencePreview.classList.add('hide');
            referencePreview.innerHTML = '';
            return;
        }

        referencePreview.classList.remove('hide');
        referencePreview.innerHTML = referenceImages.map((img, idx) => `
            <div class="reference-thumb" data-index="${idx}">
                <img src="data:${img.mimeType};base64,${img.data}" alt="${img.name}">
                <button class="reference-remove" data-index="${idx}" title="Remove">×</button>
            </div>
        `).join('');

        // Add remove handlers
        referencePreview.querySelectorAll('.reference-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                referenceImages.splice(index, 1);
                updateReferenceImagesPreview();
            });
        });
    }
}

/**
 * Convert file to base64 data URL
 * @param {File} file - File to convert
 * @returns {Promise<string>} Base64 data URL
 */
function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
    });
}

// =============================================================================
// Music Generation Modal
// =============================================================================

/**
 * Setup music generation modal event handlers
 */
export function setupMusicGenerationModal() {
    const modal = $("musicGenModal");
    const btnGenMusic = $("btnGenMusic");
    const btnClose = $("btnCloseMusicGen");
    const btnCancel = $("btnCancelMusicGen");
    const btnGenerate = $("btnStartMusicGen");
    const promptInput = $("musicPrompt");
    const negativeInput = $("musicNegativePrompt");
    const seedInput = $("musicSeed");
    const sampleCountSelect = $("musicSampleCount");
    const seedHint = $("seedHint");
    const micBtn = $("micMusicPrompt");
    const presetChips = document.querySelectorAll('.preset-chip');

    // Setup Whisper mic button for voice input
    if (micBtn) {
        micBtn.addEventListener('click', () => {
            toggleSTTFor('musicPrompt', 'micMusicPrompt');
        });
    }

    // Mutual exclusivity: seed and sample_count > 1 cannot be used together
    if (sampleCountSelect && seedInput) {
        sampleCountSelect.addEventListener('change', () => {
            const sampleCount = parseInt(sampleCountSelect.value, 10);
            if (sampleCount > 1) {
                // Disable seed input when generating multiple variations
                seedInput.disabled = true;
                seedInput.value = '';
                seedInput.placeholder = 'Disabled (not compatible with multiple variations)';
                if (seedHint) seedHint.style.color = '#ff6b6b';
            } else {
                // Re-enable seed input for single track
                seedInput.disabled = false;
                seedInput.placeholder = 'Leave empty for random';
                if (seedHint) seedHint.style.color = '';
            }
        });
    }

    // Open modal
    if (btnGenMusic) {
        btnGenMusic.addEventListener('click', () => {
            if (modal) modal.classList.remove('hide');
            // Clear previous inputs
            if (promptInput) promptInput.value = '';
            if (negativeInput) negativeInput.value = '';
            if (seedInput) {
                seedInput.value = '';
                seedInput.disabled = false;
                seedInput.placeholder = 'Leave empty for random';
            }
            if (sampleCountSelect) sampleCountSelect.value = '1';
            if (seedHint) seedHint.style.color = '';
            // Clear preset selection
            presetChips.forEach(chip => chip.classList.remove('active'));
        });
    }

    // Close modal handlers
    [btnClose, btnCancel].forEach(btn => {
        if (btn) {
            btn.addEventListener('click', () => {
                if (modal) modal.classList.add('hide');
            });
        }
    });

    // Preset chip handlers
    presetChips.forEach(chip => {
        chip.addEventListener('click', () => {
            const preset = chip.dataset.preset;
            if (preset && MUSIC_PRESETS[preset]) {
                // Update prompt
                if (promptInput) promptInput.value = MUSIC_PRESETS[preset];
                // Update visual selection
                presetChips.forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
            }
        });
    });

    // Generate button
    if (btnGenerate) {
        btnGenerate.addEventListener('click', async () => {
            const prompt = promptInput ? promptInput.value.trim() : '';
            if (!prompt) {
                toast('Please enter a music description');
                return;
            }

            const negativePrompt = negativeInput ? negativeInput.value.trim() : null;
            const seed = seedInput && seedInput.value && !seedInput.disabled
                ? parseInt(seedInput.value, 10)
                : null;
            const sampleCount = sampleCountSelect
                ? parseInt(sampleCountSelect.value, 10)
                : 1;

            // Close modal
            if (modal) modal.classList.add('hide');

            // Generate music with sample count
            await generateMusicAsync(prompt, negativePrompt, seed, sampleCount);
        });
    }
}

// =============================================================================
// Gemini Pro Voice Data
// =============================================================================

const GEMINI_PRO_VOICES = [
    { name: "Zephyr", desc: "Bright, cheerful" },
    { name: "Puck", desc: "Playful, mischievous" },
    { name: "Charon", desc: "Calm, informative" },
    { name: "Kore", desc: "Clear, versatile" },
    { name: "Fenrir", desc: "Bold, confident" },
    { name: "Leda", desc: "Warm, youthful" },
    { name: "Orus", desc: "Deep, firm" },
    { name: "Aoede", desc: "Breezy, conversational" },
    { name: "Callirrhoe", desc: "Smooth, flowing" },
    { name: "Autonoe", desc: "Gentle, measured" },
    { name: "Enceladus", desc: "Rich, resonant" },
    { name: "Iapetus", desc: "Deep, steady" },
    { name: "Umbriel", desc: "Soft, mysterious" },
    { name: "Algieba", desc: "Warm, articulate" },
    { name: "Despina", desc: "Light, energetic" },
    { name: "Erinome", desc: "Serene, melodic" },
    { name: "Algenib", desc: "Crisp, precise" },
    { name: "Rasalgethi", desc: "Grand, theatrical" },
    { name: "Laomedeia", desc: "Graceful, elegant" },
    { name: "Achernar", desc: "Bright, radiant" },
    { name: "Alnilam", desc: "Strong, commanding" },
    { name: "Schedar", desc: "Regal, distinguished" },
    { name: "Gacrux", desc: "Earthy, grounded" },
    { name: "Pulcherrima", desc: "Beautiful, refined" },
    { name: "Achird", desc: "Friendly, approachable" },
    { name: "Zubenelgenubi", desc: "Balanced, neutral" },
    { name: "Vindemiatrix", desc: "Mature, wise" },
    { name: "Sadachbia", desc: "Lucky, optimistic" },
    { name: "Sadaltager", desc: "Hopeful, bright" },
    { name: "Sulafat", desc: "Lyrical, musical" }
];

/**
 * Populate a select element with Gemini Pro voices
 * @param {HTMLSelectElement} select - The select element to populate
 * @param {string} defaultVoice - Default voice to select
 */
function populateGeminiProVoices(select, defaultVoice = "Charon") {
    if (!select) return;
    select.innerHTML = '';

    GEMINI_PRO_VOICES.forEach(voice => {
        const option = document.createElement('option');
        option.value = voice.name;
        option.textContent = `${voice.name} - ${voice.desc}`;
        if (voice.name === defaultVoice) option.selected = true;
        select.appendChild(option);
    });
}

// =============================================================================
// Google SSML Modal
// =============================================================================

/** Cached Google Cloud TTS voices */
let googleCloudVoices = null;

/**
 * Fetch Google Cloud TTS voices from server
 */
async function fetchGoogleCloudVoices() {
    if (googleCloudVoices) return googleCloudVoices;

    try {
        const res = await fetch('/tts/voices');
        if (res.ok) {
            googleCloudVoices = await res.json();
            return googleCloudVoices;
        }
    } catch (e) {
        console.error('Failed to fetch Google Cloud voices:', e);
    }
    return null;
}

/**
 * Populate language dropdown for Google SSML modal
 */
async function populateGoogleLanguages() {
    const langSelect = $("google-voice-language-select");
    const voiceSelect = $("google-voice-name-select");
    if (!langSelect || !voiceSelect) return;

    const voices = await fetchGoogleCloudVoices();
    if (!voices || !voices.voices) {
        // Fallback - add a default option
        langSelect.innerHTML = '<option value="en-US">English (US)</option>';
        voiceSelect.innerHTML = '<option value="en-US-Neural2-D">en-US-Neural2-D</option>';
        return;
    }

    // Extract unique language codes
    const languages = new Map();
    voices.voices.forEach(v => {
        v.languageCodes.forEach(lang => {
            if (!languages.has(lang)) {
                // Create friendly language name
                const parts = lang.split('-');
                const langName = new Intl.DisplayNames(['en'], { type: 'language' }).of(parts[0]) || parts[0];
                const region = parts[1] ? ` (${parts[1]})` : '';
                languages.set(lang, `${langName}${region}`);
            }
        });
    });

    // Sort and populate language dropdown
    const sortedLangs = Array.from(languages.entries()).sort((a, b) => a[1].localeCompare(b[1]));
    langSelect.innerHTML = '';
    sortedLangs.forEach(([code, name]) => {
        const option = document.createElement('option');
        option.value = code;
        option.textContent = `${name} (${code})`;
        if (code === 'en-US') option.selected = true;
        langSelect.appendChild(option);
    });

    // Populate voices for default language
    populateGoogleVoicesForLanguage('en-US');

    // Update voices when language changes
    langSelect.addEventListener('change', () => {
        populateGoogleVoicesForLanguage(langSelect.value);
    });
}

/**
 * Populate voice dropdown for selected language
 * @param {string} langCode - Language code (e.g., 'en-US')
 */
function populateGoogleVoicesForLanguage(langCode) {
    const voiceSelect = $("google-voice-name-select");
    if (!voiceSelect || !googleCloudVoices?.voices) return;

    // Filter voices for this language
    const filteredVoices = googleCloudVoices.voices.filter(v =>
        v.languageCodes.includes(langCode)
    );

    // Sort by name
    filteredVoices.sort((a, b) => a.name.localeCompare(b.name));

    voiceSelect.innerHTML = '';
    filteredVoices.forEach(voice => {
        const option = document.createElement('option');
        option.value = voice.name;
        // Include gender info if available
        const gender = voice.ssmlGender ? ` (${voice.ssmlGender.toLowerCase()})` : '';
        option.textContent = `${voice.name}${gender}`;
        voiceSelect.appendChild(option);
    });
}

/**
 * Poll task status until complete
 * @param {string} taskId - Task ID
 * @param {function} onStatus - Status update callback
 * @returns {Promise<Object>} Task result
 */
async function pollTaskStatus(taskId, onStatus) {
    const maxWait = 120000; // 2 minutes
    const start = Date.now();

    while (Date.now() - start < maxWait) {
        const res = await fetch(`/tasks/status/${taskId}`);
        if (!res.ok) throw new Error('Failed to check task status');

        const task = await res.json();

        if (task.status === 'completed') {
            return task;
        }

        if (task.status === 'failed') {
            throw new Error(task.error || 'Generation failed');
        }

        if (onStatus) onStatus(task.status);

        // Wait before next poll
        await new Promise(r => setTimeout(r, 1000));
    }

    throw new Error('Generation timed out');
}

/**
 * Setup Google SSML audio generation modal
 */
export function setupGoogleSSMLModal() {
    const modal = $("googleSSMLModal");
    const btnOpen = $("btnGenGoogleSSML");
    const btnCancel = $("btnCancelSSML");
    const btnGenerate = $("google-ssml-generate-btn");
    const statusEl = $("google-ssml-status");
    const resultContainer = $("google-ssml-result-container");
    const audioPlayer = $("google-ssml-audio-player");

    // Open modal and populate voices
    if (btnOpen) {
        btnOpen.addEventListener('click', async () => {
            if (modal) modal.classList.remove('hide');
            // Populate voices on first open
            await populateGoogleLanguages();
        });
    }

    // Close modal
    if (btnCancel) {
        btnCancel.addEventListener('click', () => {
            if (modal) modal.classList.add('hide');
        });
    }

    // Generate button
    if (btnGenerate) {
        btnGenerate.addEventListener('click', async () => {
            const textInput = $("google-ssml-input");
            const voiceSelect = $("google-voice-name-select");
            const pitchSlider = $("google-ssml-pitch");
            const rateSlider = $("google-ssml-rate");

            const text = textInput?.value?.trim();
            if (!text) {
                toast('Please enter text to speak');
                return;
            }

            const voiceName = voiceSelect?.value;
            if (!voiceName) {
                toast('Please select a voice');
                return;
            }

            const pitch = parseFloat(pitchSlider?.value || '0');
            const rate = parseFloat(rateSlider?.value || '1');
            const operator = localStorage.getItem('bbx_operator') || 'system';

            // Determine if input is SSML or plain text
            const isSSML = text.trim().startsWith('<speak');

            try {
                btnGenerate.disabled = true;
                if (statusEl) statusEl.textContent = 'Generating...';
                if (resultContainer) resultContainer.classList.add('hide');

                const body = {
                    voice_name: voiceName,
                    speaking_rate: rate,
                    pitch: pitch,
                    operator: operator
                };

                if (isSSML) {
                    body.ssml = text;
                } else {
                    body.text = text;
                }

                const res = await fetch('/generate/google_ssml', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });

                if (!res.ok) throw new Error('Failed to start generation');

                const data = await res.json();
                const taskId = data.task_id;

                if (statusEl) statusEl.textContent = 'Processing...';

                // Poll for completion
                const result = await pollTaskStatus(taskId, (status) => {
                    if (statusEl) statusEl.textContent = `Status: ${status}`;
                });

                // Play result
                if (result.result_url) {
                    if (audioPlayer) audioPlayer.src = result.result_url;
                    if (resultContainer) resultContainer.classList.remove('hide');
                    if (statusEl) statusEl.textContent = 'Ready';
                    audioPlayer?.play();
                    toast('Audio generated!');
                }

            } catch (e) {
                console.error('Google SSML generation error:', e);
                toast('Generation failed: ' + e.message);
                if (statusEl) statusEl.textContent = 'Error: ' + e.message;
            } finally {
                btnGenerate.disabled = false;
            }
        });
    }
}

// =============================================================================
// Gemini Pro TTS Modal
// =============================================================================

/**
 * Setup Gemini Pro TTS audio generation modal
 */
export function setupGeminiProTTSModal() {
    const modal = $("geminiProTTSModal");
    const btnOpen = $("btnGenGeminiProAudio");
    const btnCancel = $("btnCancelGeminiPro");
    const btnGenerate = $("gemini-pro-generate-btn");
    const statusEl = $("gemini-pro-status");
    const resultContainer = $("gemini-pro-result-container");
    const audioPlayer = $("gemini-pro-audio-player");
    const multiSpeakerToggle = $("gemini-multi-speaker-toggle");
    const singleSpeakerConfig = $("gemini-single-speaker-config");
    const multiSpeakerConfig = $("gemini-multi-speaker-config");
    const speakerHint = $("gemini-speaker-hint");

    // Populate voice dropdowns
    const mainVoiceSelect = $("gemini-pro-voice-select");
    const speaker1Voice = $("gemini-speaker1-voice");
    const speaker2Voice = $("gemini-speaker2-voice");

    populateGeminiProVoices(mainVoiceSelect, "Charon");
    populateGeminiProVoices(speaker1Voice, "Charon");
    populateGeminiProVoices(speaker2Voice, "Kore");

    // Multi-speaker toggle
    if (multiSpeakerToggle) {
        multiSpeakerToggle.addEventListener('change', () => {
            const isMulti = multiSpeakerToggle.checked;
            if (singleSpeakerConfig) singleSpeakerConfig.classList.toggle('hide', isMulti);
            if (multiSpeakerConfig) multiSpeakerConfig.classList.toggle('hide', !isMulti);
            if (speakerHint) speakerHint.classList.toggle('hide', !isMulti);
        });
    }

    // Open modal
    if (btnOpen) {
        btnOpen.addEventListener('click', () => {
            if (modal) modal.classList.remove('hide');
        });
    }

    // Close modal
    if (btnCancel) {
        btnCancel.addEventListener('click', () => {
            if (modal) modal.classList.add('hide');
        });
    }

    // Generate button
    if (btnGenerate) {
        btnGenerate.addEventListener('click', async () => {
            const textInput = $("gemini-pro-text-input");
            const text = textInput?.value?.trim();

            if (!text) {
                toast('Please enter text to speak');
                return;
            }

            const operator = localStorage.getItem('bbx_operator') || 'system';
            const isMultiSpeaker = multiSpeakerToggle?.checked || false;

            try {
                btnGenerate.disabled = true;
                if (statusEl) statusEl.textContent = 'Generating...';
                if (resultContainer) resultContainer.classList.add('hide');

                let body;

                if (isMultiSpeaker) {
                    // Multi-speaker mode
                    const speaker1Name = $("gemini-speaker1-name")?.value?.trim() || 'Speaker1';
                    const speaker1VoiceVal = speaker1Voice?.value || 'Charon';
                    const speaker2Name = $("gemini-speaker2-name")?.value?.trim() || 'Speaker2';
                    const speaker2VoiceVal = speaker2Voice?.value || 'Kore';

                    body = {
                        text: text,
                        multi_speaker: true,
                        speakers: [
                            { name: speaker1Name, voice: speaker1VoiceVal },
                            { name: speaker2Name, voice: speaker2VoiceVal }
                        ],
                        model: "gemini-2.5-pro-tts",
                        operator: operator
                    };
                } else {
                    // Single speaker mode
                    const voiceName = mainVoiceSelect?.value || 'Charon';
                    body = {
                        text: text,
                        voice_name: voiceName,
                        multi_speaker: false,
                        model: "gemini-2.5-pro-tts",
                        operator: operator
                    };
                }

                const res = await fetch('/generate/gemini_tts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });

                if (!res.ok) throw new Error('Failed to start generation');

                const data = await res.json();
                const taskId = data.task_id;

                if (statusEl) statusEl.textContent = 'Processing...';

                // Poll for completion
                const result = await pollTaskStatus(taskId, (status) => {
                    if (statusEl) statusEl.textContent = `Status: ${status}`;
                });

                // Play result
                if (result.result_url) {
                    if (audioPlayer) audioPlayer.src = result.result_url;
                    if (resultContainer) resultContainer.classList.remove('hide');
                    if (statusEl) statusEl.textContent = 'Ready';
                    audioPlayer?.play();
                    toast('Audio generated!');
                }

            } catch (e) {
                console.error('Gemini Pro TTS generation error:', e);
                toast('Generation failed: ' + e.message);
                if (statusEl) statusEl.textContent = 'Error: ' + e.message;
            } finally {
                btnGenerate.disabled = false;
            }
        });
    }
}

// =============================================================================
// Video Generation Modal (Veo 3.1 Full Features)
// =============================================================================

/**
 * Setup video generation modal with full Veo 3.1 options
 * Features: aspect ratio, duration, resolution, negative prompts, image-to-video
 */
export function setupVideoGenerationModal() {
    const modal = $("videoGenModal");
    const btnClose = $("btnCloseVideoGen");
    const btnCancel = $("btnCancelVideoGen");
    const btnGenerate = $("btnStartVideoGen");
    const promptInput = $("videoPrompt");
    const negativeInput = $("videoNegativePrompt");
    const aspectSelect = $("videoAspectRatio");
    const durationSelect = $("videoDuration");
    const resolutionSelect = $("videoResolution");
    const micBtn = $("micVideoPrompt");

    // Image-to-Video elements
    const btnSelectImage = $("btnSelectVideoStartImage");
    const imageInput = $("videoStartImageInput");
    const imagePreview = $("videoStartImagePreview");
    const imagePreviewImg = $("videoStartImagePreviewImg");
    const btnClearImage = $("btnClearVideoStartImage");

    // Setup Whisper mic button for voice input
    if (micBtn) {
        micBtn.addEventListener('click', () => {
            toggleSTTFor('videoPrompt', 'micVideoPrompt');
        });
    }

    // Resolution constraint: 1080p only available for 8-second videos
    if (durationSelect && resolutionSelect) {
        durationSelect.addEventListener('change', () => {
            const duration = durationSelect.value;
            const is1080pOption = resolutionSelect.querySelector('option[value="1080p"]');

            if (duration !== '8') {
                // Disable 1080p for non-8s durations
                if (is1080pOption) {
                    is1080pOption.disabled = true;
                    is1080pOption.textContent = '1080p (HD - 8s only)';
                }
                // If 1080p was selected, switch to 720p
                if (resolutionSelect.value === '1080p') {
                    resolutionSelect.value = '720p';
                    toast('1080p resolution requires 8-second duration');
                }
            } else {
                // Enable 1080p for 8s duration
                if (is1080pOption) {
                    is1080pOption.disabled = false;
                    is1080pOption.textContent = '1080p (HD)';
                }
            }
        });
    }

    // Image-to-Video: Select starting image (with Android native picker support)
    const hasAndroidFilePicker = typeof AndroidFilePicker !== 'undefined' &&
                                  typeof AndroidFilePicker.openFileChooser === 'function';

    // Setup callback for video start image selection (Android uses window.onNativeFileSelected)
    window.videoStartImageSelectionActive = false;
    window.videoStartImageSelectionCallback = async function(base64Data, mimeType, fileName) {
        console.log('[VideoStartImage] Callback received:', fileName);
        window.videoStartImageSelectionActive = false;

        try {
            videoStartImageData = {
                data: base64Data,
                mimeType: mimeType || 'image/jpeg',
                name: fileName || 'image.jpg'
            };

            // Show preview - need full data URL for display
            if (imagePreviewImg) {
                imagePreviewImg.src = `data:${mimeType || 'image/jpeg'};base64,${base64Data}`;
            }
            if (imagePreview) {
                imagePreview.classList.remove('hide');
            }

            toast(`Image "${fileName}" selected for animation`);
        } catch (err) {
            console.error('[VideoStartImage] Failed to process image:', err);
            toast('Failed to load image');
        }
    };

    if (btnSelectImage) {
        btnSelectImage.addEventListener('click', () => {
            console.log('[VideoStartImage] Select button clicked, Android picker:', hasAndroidFilePicker);

            if (hasAndroidFilePicker) {
                // Use native Android file picker for images
                console.log('[VideoStartImage] Using native Android file picker');
                window.videoStartImageSelectionActive = true;
                try {
                    AndroidFilePicker.openFileChooser('image/*');
                } catch (e) {
                    console.error('[VideoStartImage] Native file picker error:', e);
                    window.videoStartImageSelectionActive = false;
                    // Fallback to HTML input
                    if (imageInput) imageInput.click();
                }
            } else if (imageInput) {
                // Use standard HTML file input (desktop browsers)
                console.log('[VideoStartImage] Using HTML file input');
                imageInput.click();
            } else {
                toast('File picker not available');
            }
        });
    }

    // HTML file input handler (for desktop browsers)
    if (imageInput) {
        imageInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            console.log('[VideoStartImage] File selected via HTML input:', file.name);

            if (!file.type.startsWith('image/')) {
                toast('Please select an image file');
                return;
            }

            try {
                const base64 = await fileToBase64(file);
                videoStartImageData = {
                    data: base64.split(',')[1], // Remove data:image/xxx;base64, prefix
                    mimeType: file.type,
                    name: file.name
                };

                // Show preview
                if (imagePreviewImg) {
                    imagePreviewImg.src = base64;
                }
                if (imagePreview) {
                    imagePreview.classList.remove('hide');
                }

                toast(`Image "${file.name}" selected for animation`);
            } catch (err) {
                console.error('[VideoStartImage] Failed to process image:', err);
                toast('Failed to load image');
            }

            // Clear input for next selection
            imageInput.value = '';
        });
    }

    // Clear starting image
    if (btnClearImage) {
        btnClearImage.addEventListener('click', () => {
            videoStartImageData = null;
            if (imagePreview) imagePreview.classList.add('hide');
            if (imagePreviewImg) imagePreviewImg.src = '';
            toast('Starting image cleared');
        });
    }

    // Close modal handlers
    const closeModal = () => {
        if (modal) modal.classList.add('hide');
        videoStartImageData = null;
    };

    if (btnClose) btnClose.addEventListener('click', closeModal);
    if (btnCancel) btnCancel.addEventListener('click', closeModal);

    // Generate button handler
    if (btnGenerate) {
        btnGenerate.addEventListener('click', async () => {
            const prompt = promptInput ? promptInput.value.trim() : '';
            if (!prompt) {
                toast('Please enter a video description');
                return;
            }

            // Gather video options
            const aspectRatio = aspectSelect?.value || '16:9';
            const duration = parseInt(durationSelect?.value || '8', 10);
            const resolution = resolutionSelect?.value || '720p';
            const negativePrompt = negativeInput?.value?.trim() || null;

            // Validate 1080p + 8s constraint
            if (resolution === '1080p' && duration !== 8) {
                toast('1080p resolution requires 8-second duration');
                return;
            }

            // Close modal
            if (modal) modal.classList.add('hide');

            // Prepare image data if provided (image-to-video)
            const imageData = videoStartImageData ? {
                data: videoStartImageData.data,
                mimeType: videoStartImageData.mimeType
            } : null;

            // Generate video with full options
            try {
                await generateVideoAsync(prompt, imageData, {
                    aspectRatio,
                    duration,
                    resolution,
                    negativePrompt
                });
            } catch (err) {
                console.error('Video generation failed:', err);
                toast(`Video generation failed: ${err.message}`);
            }

            // Clear state
            videoStartImageData = null;
        });
    }
}

// =============================================================================
// Initialize All Generation Modals
// =============================================================================

/**
 * Initialize all generation modal handlers
 */
export function initGenerationModals() {
    setupVideoExtensionModal();
    setupGenerationModal();
    setupVideoGenerationModal();
    setupMusicGenerationModal();
    setupGoogleSSMLModal();
    setupGeminiProTTSModal();
}

/**
 * Setup audio analysis modal
 * Note: This function is now obsolete as we use the paperclip icon.
 * Kept for backward compatibility.
 */
export function setupAudioAnalysisModal() {
    // This function is now OBSOLETE as we use the paperclip icon
    // We leave it here empty to prevent errors if it's called elsewhere
}
