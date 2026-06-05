/**
 * tts-stt.js
 * Text-to-Speech and Speech-to-Text functionality
 * Voice selection, audio generation, audio players, and STT recording
 *
 * Source: Portal/app.js lines 611-974, 1135-1623, 2336-2458, 3696-3703
 * Refactored: 2025-12-20
 */

import { $, toast, simpleHash } from './core-utils.js';
import { getOperator, getAudioCache, saveAudioCache } from './state-management.js';
import { sttStream } from './stt-stream.js';

// =============================================================================
// TTS Constants
// =============================================================================

const TTS_MODEL = (window.PORTAL && window.PORTAL.audio_model) || "tts-1";
const TTS_FMT = (window.PORTAL && window.PORTAL.audio_format) || "mp3";
const TTS_DEFAULT_VOICE = "gemini-pro:Charon";  // Default: Gemini Pro with Charon voice

// OpenAI TTS has a 4,096 character limit per request
// Using 4000 as max to leave buffer for any encoding overhead
const TTS_MAX_CHARS = 4000;

// Per-operator voice preference storage (synced to server for cross-device persistence)
const OPERATOR_VOICE_STORE = "bb_operator_voices";

// =============================================================================
// TTS State
// =============================================================================

/** Audio player instance */
let ttsPlayer = new Audio();

/** TTS state flags */
let ttsState = { isFetching: false, isPlaying: false };

/** Active AbortController for cancellable TTS requests */
let activeTTSAbort = null;

/** Cancel any in-flight TTS request */
function cancelActiveTTS() {
    if (activeTTSAbort) {
        activeTTSAbort.abort();
        activeTTSAbort = null;
    }
}

// Cancel TTS on page unload/navigation to prevent blocking reload
if (typeof window !== 'undefined') {
    window.addEventListener('beforeunload', cancelActiveTTS);
    window.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') cancelActiveTTS();
    });
}

/** Last assistant text for main speaker button */
let lastAssistantText = "";

// =============================================================================
// STT State
// =============================================================================

/** Mic recording state (legacy MediaRecorder path) */
let micOn = false;

/**
 * Streaming dictation active flag (sttStream path). Tracked separately from
 * micOn so the public state getters can report mic-on/transcribing whether the
 * streaming client OR the legacy recorder is running.
 */
let sttStreamingActive = false;

/** MediaRecorder instance */
let mediaRec = null;

/** Media stream instance */
let mediaStream = null;

// =============================================================================
// Streaming TTS Queue (for Auto-TTS during streaming responses)
// =============================================================================

/**
 * StreamingTTSQueue - Handles chunked TTS generation during streaming responses
 *
 * When Auto-TTS is enabled, this queue:
 * 1. Accumulates text during streaming
 * 2. Detects sentence boundaries (., !, ?, newlines)
 * 3. Queues complete sentences for TTS generation
 * 4. Plays audio chunks as they complete (no waiting for full response)
 * 5. Combines all audio at the end for a scrubbable player
 */
class StreamingTTSQueue {
    constructor(bubbleElement) {
        this.bubble = bubbleElement;
        this.textBuffer = "";
        this.sentences = [];
        this.audioBlobs = [];
        this.audioUrls = [];
        this.isGenerating = false;
        this.isPlaying = false;
        this.currentPlayIndex = 0;
        this.generationQueue = [];
        this.voiceConfig = null;
        this.totalGenerated = 0;
        this.finalized = false;
        this.audioPlayer = null;
        this.playbackAudio = null;

        // Minimum sentence length to avoid tiny TTS calls
        this.MIN_SENTENCE_LENGTH = 20;

        console.log('[StreamingTTS] Queue initialized');
    }

    /**
     * Start the streaming TTS queue
     */
    start() {
        this.voiceConfig = getTTSVoice();
        console.log('[StreamingTTS] Started with voice:', this.voiceConfig);

        // Create a streaming status indicator
        this._createStreamingIndicator();
    }

    /**
     * Create streaming TTS indicator in the bubble
     */
    _createStreamingIndicator() {
        const existingIndicator = this.bubble.querySelector('.streaming-tts-indicator');
        if (existingIndicator) existingIndicator.remove();

        const indicator = document.createElement('div');
        indicator.className = 'streaming-tts-indicator';
        indicator.innerHTML = `
            <div class="streaming-tts-status">
                <span class="tts-pulse"></span>
                <span class="tts-label">Preparing audio...</span>
            </div>
        `;
        indicator.style.cssText = `
            padding: 8px 12px;
            background: rgba(39, 217, 128, 0.1);
            border-radius: 8px;
            margin-top: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            color: #27d980;
        `;

        const pulseStyle = indicator.querySelector('.tts-pulse');
        if (pulseStyle) {
            pulseStyle.style.cssText = `
                width: 8px;
                height: 8px;
                background: #27d980;
                border-radius: 50%;
                animation: ttsPulse 1s infinite;
            `;
        }

        // Add pulse animation if not exists
        if (!document.querySelector('#streaming-tts-styles')) {
            const style = document.createElement('style');
            style.id = 'streaming-tts-styles';
            style.textContent = `
                @keyframes ttsPulse {
                    0%, 100% { opacity: 1; transform: scale(1); }
                    50% { opacity: 0.5; transform: scale(0.8); }
                }
            `;
            document.head.appendChild(style);
        }

        this.bubble.appendChild(indicator);
        this.indicator = indicator;
    }

    /**
     * Update the streaming indicator status
     */
    _updateIndicator(message) {
        if (this.indicator) {
            const label = this.indicator.querySelector('.tts-label');
            if (label) label.textContent = message;
        }
    }

    /**
     * Feed text chunk from streaming response
     * @param {string} chunk - Text chunk from stream
     */
    feedChunk(chunk) {
        if (this.finalized) return;

        this.textBuffer += chunk;

        // Check for sentence boundaries
        this._extractSentences();
    }

    /**
     * Extract complete sentences from buffer
     */
    _extractSentences() {
        // Sentence boundary regex - matches ., !, ?, or newlines followed by space/newline/end
        const sentenceEndRegex = /([.!?])\s+|(\n\n)/g;

        let match;
        let lastIndex = 0;

        while ((match = sentenceEndRegex.exec(this.textBuffer)) !== null) {
            const sentenceEnd = match.index + match[0].length;
            const sentence = this.textBuffer.slice(lastIndex, sentenceEnd).trim();

            // Only queue sentences that meet minimum length
            if (sentence.length >= this.MIN_SENTENCE_LENGTH) {
                this.sentences.push(sentence);
                this._queueSentence(sentence);
            } else if (sentence.length > 0) {
                // Short sentence - hold it for the next one
                lastIndex = match.index - sentence.length;
                break;
            }

            lastIndex = sentenceEnd;
        }

        // Keep remaining text in buffer
        this.textBuffer = this.textBuffer.slice(lastIndex);
    }

    /**
     * Queue a sentence for TTS generation
     * @param {string} sentence - Sentence to generate
     */
    _queueSentence(sentence) {
        console.log('[StreamingTTS] Queueing sentence:', sentence.substring(0, 50) + '...');
        this.generationQueue.push(sentence);
        this._processQueue();
    }

    /**
     * Process the generation queue — fires all pending sentences concurrently
     */
    async _processQueue() {
        if (this.isGenerating || this.generationQueue.length === 0) return;
        this.isGenerating = true;

        try {
            // Grab all queued sentences at once
            const batch = [...this.generationQueue];
            this.generationQueue.length = 0;

            this._updateIndicator(`Generating ${batch.length} audio segment${batch.length > 1 ? 's' : ''}...`);

            // Fire all TTS requests concurrently
            const promises = batch.map(async (sentence, i) => {
                const index = this.totalGenerated + i;
                try {
                    console.log(`[StreamingTTS] Generating segment ${index + 1} concurrently`);
                    const blob = await this._generateTTS(sentence);
                    return { index, blob };
                } catch (err) {
                    console.error(`[StreamingTTS] Segment ${index + 1} failed:`, err);
                    return { index, blob: null };
                }
            });

            const results = await Promise.all(promises);

            // Add blobs in order (they're already ordered by index from map)
            for (const { blob } of results) {
                if (blob) {
                    this.audioBlobs.push(blob);
                    const url = URL.createObjectURL(blob);
                    this.audioUrls.push(url);
                }
            }
            this.totalGenerated += batch.length;

            console.log(`[StreamingTTS] Batch of ${batch.length} complete, total: ${this.totalGenerated}`);

            // Start playback if not already playing
            if (!this.isPlaying && this.audioUrls.length > 0) {
                this._startPlayback();
            }
        } finally {
            this.isGenerating = false;
            // Process any new sentences that arrived while we were generating
            if (this.generationQueue.length > 0) {
                this._processQueue();
            }
        }
    }

    /**
     * Generate TTS for a single chunk using /tts/batch (bypasses Gemini task queue)
     * @param {string} text - Text to convert to speech
     * @returns {Promise<Blob>} Audio blob
     */
    async _generateTTS(text) {
        const speakableText = extractSpeakableText(text);
        if (!speakableText) return null;

        try {
            const isGemini = this.voiceConfig.provider !== "openai";
            const operator = (localStorage.getItem("bbx_operator") || "").trim();

            const r = await fetch("/tts/batch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    text: speakableText,
                    provider: this.voiceConfig.provider,
                    voice: this.voiceConfig.voice,
                    model: isGemini ? getGeminiModel(this.voiceConfig.provider) : TTS_MODEL,
                    format: TTS_FMT,
                    operator: operator
                })
            });
            if (!r.ok) throw new Error("TTS generation failed");
            return await r.blob();
        } catch (err) {
            console.error('[StreamingTTS] TTS error:', err);
            return null;
        }
    }

    /**
     * Start audio playback
     */
    _startPlayback() {
        if (this.isPlaying || this.audioUrls.length === 0) return;

        this.isPlaying = true;
        this._updateIndicator('Playing audio...');
        this._playNext();
    }

    /**
     * Play next audio chunk in queue
     */
    _playNext() {
        if (this.currentPlayIndex >= this.audioUrls.length) {
            // Check if we should wait for more audio or finalize
            if (this.finalized) {
                this.isPlaying = false;
                this._createFinalPlayer();
            } else {
                // Wait for more audio
                this.isPlaying = false;
                this._updateIndicator('Waiting for more audio...');
            }
            return;
        }

        const url = this.audioUrls[this.currentPlayIndex];
        this.playbackAudio = new Audio(url);

        this.playbackAudio.onended = () => {
            this.currentPlayIndex++;
            this._playNext();
        };

        this.playbackAudio.onerror = (e) => {
            console.error('[StreamingTTS] Playback error:', e);
            this.currentPlayIndex++;
            this._playNext();
        };

        this._updateIndicator(`Playing ${this.currentPlayIndex + 1}/${this.audioUrls.length}...`);
        this.playbackAudio.play().catch(e => {
            console.error('[StreamingTTS] Play error:', e);
            this.currentPlayIndex++;
            this._playNext();
        });
    }

    /**
     * Finalize the streaming TTS (called when streaming response completes)
     * @param {string} fullText - Complete response text
     */
    async finalize(fullText) {
        if (this.finalized) return;
        this.finalized = true;

        console.log('[StreamingTTS] Finalizing, buffer remaining:', this.textBuffer.length);

        // Process any remaining text in buffer
        if (this.textBuffer.trim().length >= this.MIN_SENTENCE_LENGTH) {
            this.sentences.push(this.textBuffer.trim());
            this._queueSentence(this.textBuffer.trim());
            this.textBuffer = "";
        }

        // Wait for all generation to complete
        while (this.isGenerating || this.generationQueue.length > 0) {
            await new Promise(r => setTimeout(r, 100));
        }

        // If nothing was generated (short response), generate full TTS
        if (this.audioBlobs.length === 0 && fullText && fullText.trim().length > 0) {
            console.log('[StreamingTTS] No chunks generated, generating full TTS');
            this._updateIndicator('Generating full audio...');
            const blob = await this._generateTTS(fullText);
            if (blob) {
                this.audioBlobs.push(blob);
                const url = URL.createObjectURL(blob);
                this.audioUrls.push(url);
            }
        }

        // Resume playback if paused waiting for more audio
        if (!this.isPlaying && this.currentPlayIndex < this.audioUrls.length) {
            this._startPlayback();
        } else if (!this.isPlaying) {
            this._createFinalPlayer();
        }
    }

    /**
     * Create the final combined audio player
     */
    async _createFinalPlayer() {
        console.log('[StreamingTTS] Creating final player with', this.audioBlobs.length, 'chunks');

        if (this.indicator) {
            this.indicator.remove();
        }

        if (this.audioBlobs.length === 0) {
            console.log('[StreamingTTS] No audio to combine');
            return;
        }

        const isGemini = this.voiceConfig.provider !== "openai";
        const audioFormat = isGemini ? "wav" : TTS_FMT;

        let audioDataURL;

        if (this.audioBlobs.length === 1) {
            // Single chunk — no stitching needed
            audioDataURL = await blobToDataURL(this.audioBlobs[0]);
        } else if (!isGemini) {
            // MP3 — frame-based, naive concatenation works fine
            const combinedBlob = new Blob(this.audioBlobs, { type: `audio/${audioFormat}` });
            audioDataURL = await blobToDataURL(combinedBlob);
        } else {
            // WAV — use backend stitch endpoint for proper header handling
            try {
                const formData = new FormData();
                this.audioBlobs.forEach((blob, i) => {
                    formData.append('chunks', blob, `chunk_${i}.wav`);
                });

                const r = await fetch('/tts/stitch', { method: 'POST', body: formData });
                if (r.ok) {
                    const stitchedBlob = await r.blob();
                    audioDataURL = await blobToDataURL(stitchedBlob);
                } else {
                    console.warn('[StreamingTTS] Stitch endpoint failed, using naive concat');
                    const combinedBlob = new Blob(this.audioBlobs, { type: `audio/${audioFormat}` });
                    audioDataURL = await blobToDataURL(combinedBlob);
                }
            } catch (e) {
                console.warn('[StreamingTTS] Stitch error, fallback:', e);
                const combinedBlob = new Blob(this.audioBlobs, { type: `audio/${audioFormat}` });
                audioDataURL = await blobToDataURL(combinedBlob);
            }
        }

        console.log('[StreamingTTS] Combined audio size:', (audioDataURL.length / 1024).toFixed(2), 'KB');

        const speakBtn = this.bubble.querySelector('.speak-btn');
        attachAudioPlayer(this.bubble, audioDataURL, false, speakBtn);

        // Clean up object URLs
        this.audioUrls.forEach(url => URL.revokeObjectURL(url));

        toast('Streaming TTS complete');
    }

    /**
     * Stop and cleanup
     */
    stop() {
        this.finalized = true;
        if (this.playbackAudio) {
            this.playbackAudio.pause();
            this.playbackAudio = null;
        }
        this.audioUrls.forEach(url => URL.revokeObjectURL(url));
        if (this.indicator) {
            this.indicator.remove();
        }
    }
}

// Global streaming TTS instance (one per streaming response)
let activeStreamingTTS = null;

/**
 * Start streaming TTS for a bubble
 * @param {HTMLElement} bubble - The bubble element
 * @returns {StreamingTTSQueue} The streaming TTS queue instance
 */
export function startStreamingTTS(bubble) {
    // Stop any existing streaming TTS
    if (activeStreamingTTS) {
        activeStreamingTTS.stop();
    }

    activeStreamingTTS = new StreamingTTSQueue(bubble);
    activeStreamingTTS.start();
    return activeStreamingTTS;
}

/**
 * Feed text chunk to streaming TTS
 * @param {string} chunk - Text chunk from stream
 */
export function feedStreamingTTSChunk(chunk) {
    if (activeStreamingTTS && window.autoTTSEnabled) {
        activeStreamingTTS.feedChunk(chunk);
    }
}

/**
 * Finalize streaming TTS
 * @param {string} fullText - Complete response text
 */
export function finalizeStreamingTTS(fullText) {
    if (activeStreamingTTS) {
        activeStreamingTTS.finalize(fullText);
        activeStreamingTTS = null;
    }
}

/**
 * Check if streaming TTS is active
 * @returns {boolean}
 */
export function isStreamingTTSActive() {
    return activeStreamingTTS !== null && window.autoTTSEnabled;
}

/** Audio chunks buffer */
let audioChunks = [];

/** Current STT target input field */
let currentSTTTarget = "prompt";

/** Current STT button ID */
let currentSTTButton = "ctlMic";

// =============================================================================
// Voice Preference Functions
// =============================================================================

/**
 * Get all operator voice preferences from localStorage
 * @returns {Object} Voice preferences by operator
 */
export function getOperatorVoices() {
    try {
        const raw = localStorage.getItem(OPERATOR_VOICE_STORE);
        return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
}

/**
 * Set voice preference for an operator (saves to localStorage and server)
 * @param {string} operator - Operator name
 * @param {string} voice - Voice value (e.g., "gemini-pro:Charon")
 */
export function setOperatorVoice(operator, voice) {
    // Save to localStorage for immediate use
    const voices = getOperatorVoices();
    voices[operator] = voice;
    localStorage.setItem(OPERATOR_VOICE_STORE, JSON.stringify(voices));

    // Sync to server for cross-device persistence (fire and forget)
    fetch(`/operator/preferences/${encodeURIComponent(operator)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tts_voice: voice })
    }).catch(err => console.warn("[PREFS] Failed to sync voice to server:", err));
}

/**
 * Fetch voice preference from server and update localStorage
 * @param {string} operator - Operator name
 * @returns {Promise<string>} Voice value
 */
export async function fetchOperatorVoiceFromServer(operator) {
    try {
        const res = await fetch(`/operator/preferences/${encodeURIComponent(operator)}`);
        if (res.ok) {
            const data = await res.json();
            const serverVoice = data.preferences?.tts_voice;
            if (serverVoice) {
                // Update localStorage with server value
                const voices = getOperatorVoices();
                voices[operator] = serverVoice;
                localStorage.setItem(OPERATOR_VOICE_STORE, JSON.stringify(voices));
                return serverVoice;
            }
        }
    } catch (err) {
        console.warn("[PREFS] Failed to fetch voice from server:", err);
    }
    // Fallback to localStorage
    const voices = getOperatorVoices();
    return voices[operator] || TTS_DEFAULT_VOICE;
}

/**
 * Get current voice preference for active operator
 * @returns {string} Voice value
 */
export function getCurrentVoice() {
    const operator = (localStorage.getItem("bbx_operator") || "").trim();
    if (!operator) return TTS_DEFAULT_VOICE;
    const voices = getOperatorVoices();
    return voices[operator] || TTS_DEFAULT_VOICE;
}

/**
 * Parse voice value into provider and voice name
 * Format: "provider:voice" e.g., "openai:alloy", "gemini-pro:Charon"
 * @param {string} voiceValue - Voice value string
 * @returns {Object} {provider, voice}
 */
export function parseVoiceValue(voiceValue) {
    const parts = (voiceValue || TTS_DEFAULT_VOICE).split(":");
    if (parts.length === 2) {
        return { provider: parts[0], voice: parts[1] };
    }
    // Legacy format (just voice name) - assume OpenAI
    return { provider: "openai", voice: voiceValue || "onyx" };
}

/**
 * Get TTS voice configuration for current operator
 * @returns {Object} {provider, voice}
 */
export function getTTSVoice() {
    return parseVoiceValue(getCurrentVoice());
}

/**
 * Get Gemini model name based on provider
 * @param {string} provider - Provider name
 * @returns {string} Model name
 */
export function getGeminiModel(provider) {
    if (provider === "gemini-flash") return "gemini-2.5-flash-tts";
    return "gemini-2.5-pro-tts";  // Default to pro
}

/**
 * Sync voice dropdown to current operator's preference
 */
export async function syncVoiceDropdown() {
    const voiceSelect = document.getElementById("ttsVoiceSelect");
    if (voiceSelect) {
        const operator = (localStorage.getItem("bbx_operator") || "").trim();
        if (operator) {
            const serverVoice = await fetchOperatorVoiceFromServer(operator);
            voiceSelect.value = serverVoice;
        } else {
            voiceSelect.value = getCurrentVoice();
        }
    }
}

// =============================================================================
// TTS Generation Functions
// =============================================================================

/**
 * Poll Gemini TTS task until complete
 * @param {string} taskId - Task ID
 * @returns {Promise<string|null>} Audio URL or null
 */
async function pollGeminiTTSTask(taskId) {
    const start = Date.now();
    let lastProgressToast = 0;
    const PROGRESS_TOAST_INTERVAL = 30000;

    while (true) {
        const r = await fetch(`/tasks/status/${taskId}`);
        if (!r.ok) {
            console.error("Gemini TTS task status fetch failed");
            return null;
        }

        const task = await r.json();

        if (task.status === "completed" && task.result_url) {
            const elapsed = ((Date.now() - start) / 1000).toFixed(1);
            console.log(`[TTS] Task ${taskId} completed in ${elapsed}s`);
            return task.result_url;
        }

        if (task.status === "failed") {
            console.error("Gemini TTS task failed:", task.error);
            return null;
        }

        // Show progress toast periodically
        const elapsed = Date.now() - start;
        if (elapsed - lastProgressToast >= PROGRESS_TOAST_INTERVAL) {
            lastProgressToast = elapsed;
            const mins = Math.floor(elapsed / 60000);
            const secs = Math.floor((elapsed % 60000) / 1000);
            const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
            toast(`Audio still generating... (${timeStr})`);
        }

        // Adaptive polling
        const pollInterval = elapsed < 60000 ? 1000 : 3000;
        await new Promise(resolve => setTimeout(resolve, pollInterval));
    }
}

/**
 * Generate a single Gemini TTS chunk
 * @param {string} text - Text to convert
 * @param {Object} voiceConfig - Voice configuration
 * @returns {Promise<string|null>} Audio URL or null
 */
export async function generateGeminiTTSChunk(text, voiceConfig) {
    const operator = (localStorage.getItem("bbx_operator") || "").trim();
    const model = getGeminiModel(voiceConfig.provider);

    const r = await fetch("/generate/gemini_tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            text: text,
            voice_name: voiceConfig.voice,
            model: model,
            operator: operator
        })
    });

    if (!r.ok) return null;

    const data = await r.json();
    const taskId = data.task_id;

    return await pollGeminiTTSTask(taskId);
}

/**
 * Generate TTS audio with current operator's voice
 * @param {string} text - Text to convert
 * @returns {Promise<string|null>} Blob URL or null
 */
export async function generateTTSAudio(text) {
    const voiceConfig = getTTSVoice();
    return generateTTSAudioWithVoice(text, voiceConfig);
}

/**
 * Generate TTS audio with explicit voice configuration
 * @param {string} text - Text to convert
 * @param {Object} voiceConfig - Voice configuration {provider, voice}
 * @returns {Promise<string|null>} Blob URL or null
 */
export async function generateTTSAudioWithVoice(text, voiceConfig) {
    const operator = (localStorage.getItem("bbx_operator") || "").trim();

    if (voiceConfig.provider === "openai") {
        const r = await fetch("/tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: text,
                model: TTS_MODEL,
                voice: voiceConfig.voice,
                format: TTS_FMT
            })
        });

        if (r.ok) {
            const blob = await r.blob();
            return URL.createObjectURL(blob);
        }
        console.error("OpenAI TTS failed:", r.status, await r.text());
        return null;
    } else {
        // Gemini TTS
        const model = getGeminiModel(voiceConfig.provider);

        const r = await fetch("/generate/gemini_tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: text,
                voice_name: voiceConfig.voice,
                model: model,
                operator: operator
            })
        });

        if (!r.ok) {
            console.error("Gemini TTS queue failed:", r.status);
            return null;
        }

        const data = await r.json();
        const taskId = data.task_id;

        return await pollGeminiTTSTask(taskId);
    }
}

/**
 * Chunk text for TTS (splits at sentence boundaries)
 * OpenAI TTS has a 4,096 character limit per request.
 * This function splits text at natural sentence boundaries to stay under the limit.
 *
 * @param {string} text - Text to chunk
 * @param {number} maxLen - Maximum chunk length (default: 4000 chars)
 * @returns {string[]} Array of text chunks, each under maxLen characters
 */
export function chunkForTTS(text, maxLen = TTS_MAX_CHARS) {
    if (!text || text.length === 0) {
        return [];
    }

    // If text is already under the limit, return as single chunk
    if (text.length <= maxLen) {
        console.log(`[TTS Chunking] Text under limit (${text.length} chars), single chunk`);
        return [text];
    }

    console.log(`[TTS Chunking] Text exceeds limit (${text.length} chars), splitting at sentence boundaries...`);

    const out = [];
    let buf = "";

    // Split at sentence boundaries: . ! ? followed by whitespace, or double newlines
    // Using lookbehind to keep the punctuation with the sentence
    const parts = text.split(/(?<=[.!?])\s+|(?<=\n\n)/);

    for (const seg of parts) {
        const trimmedSeg = seg.trim();
        if (!trimmedSeg) continue;

        const next = buf ? (buf + " " + trimmedSeg) : trimmedSeg;

        if (next.length <= maxLen) {
            buf = next;
        } else {
            // Current buffer is full, push it and start new
            if (buf) {
                out.push(buf.trim());
                console.log(`[TTS Chunking] Chunk ${out.length}: ${buf.length} chars`);
            }

            // Check if single segment exceeds limit (very long sentence)
            if (trimmedSeg.length > maxLen) {
                console.log(`[TTS Chunking] Long segment (${trimmedSeg.length} chars), splitting by word boundaries...`);

                // Split very long segments by word boundaries
                const words = trimmedSeg.split(/\s+/);
                let wordBuf = "";

                for (const word of words) {
                    const nextWord = wordBuf ? (wordBuf + " " + word) : word;

                    if (nextWord.length <= maxLen) {
                        wordBuf = nextWord;
                    } else {
                        if (wordBuf) {
                            out.push(wordBuf.trim());
                            console.log(`[TTS Chunking] Word-split chunk ${out.length}: ${wordBuf.length} chars`);
                        }

                        // Handle extremely long single words (rare edge case)
                        if (word.length > maxLen) {
                            for (let i = 0; i < word.length; i += maxLen) {
                                const wordChunk = word.slice(i, i + maxLen);
                                out.push(wordChunk);
                                console.log(`[TTS Chunking] Force-split chunk ${out.length}: ${wordChunk.length} chars`);
                            }
                            wordBuf = "";
                        } else {
                            wordBuf = word;
                        }
                    }
                }

                // Don't forget remaining words
                buf = wordBuf;
            } else {
                buf = trimmedSeg;
            }
        }
    }

    // Push any remaining buffer
    if (buf && buf.trim()) {
        out.push(buf.trim());
        console.log(`[TTS Chunking] Final chunk ${out.length}: ${buf.length} chars`);
    }

    console.log(`[TTS Chunking] Split into ${out.length} chunks for TTS generation`);
    return out;
}

// =============================================================================
// Bubble State Management
// =============================================================================

/**
 * Set bubble button state (loading/waiting)
 * @param {HTMLElement} btn - Button element
 * @param {boolean} waiting - Whether waiting/loading
 */
export function setBubbleState(btn, waiting) {
    if (!btn) return;
    btn.setAttribute("aria-pressed", waiting ? "true" : "false");
    btn.classList.toggle("bubble--waiting", !!waiting);
    if (waiting) {
        btn.classList.add('bubble-btn-loading');
        btn.classList.remove('bubble-btn-playing');
    } else {
        btn.classList.remove('bubble-btn-loading');
    }
}

// =============================================================================
// TTS Playback Functions
// =============================================================================

/**
 * Speak text using TTS (main speaker button)
 * @param {string} text - Text to speak
 * @param {HTMLElement} btn - Button element
 */
export async function speak(text, btn) {
    if (ttsState.isFetching || ttsState.isPlaying) return;

    try {
        ttsState.isFetching = true;
        setBubbleState(btn, true);

        const voiceConfig = getTTSVoice();
        const isGemini = voiceConfig.provider !== "openai";
        const operator = (localStorage.getItem("bbx_operator") || "").trim();

        ttsState.isPlaying = true;

        const r = await fetch("/tts/batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: text,
                provider: voiceConfig.provider,
                voice: voiceConfig.voice,
                model: isGemini ? getGeminiModel(voiceConfig.provider) : TTS_MODEL,
                format: TTS_FMT,
                operator: operator
            })
        });

        if (!r.ok) {
            // Fallback to browser speech synthesis
            const u = new SpeechSynthesisUtterance(text);
            speechSynthesis.speak(u);
            await new Promise(resolve => u.onend = resolve);
        } else {
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            ttsPlayer.src = url;
            await ttsPlayer.play();
            await new Promise(resolve => ttsPlayer.onended = () => { URL.revokeObjectURL(url); resolve(); });
        }
    } catch (e) {
        console.error("Speak error:", e);
        const u = new SpeechSynthesisUtterance(text);
        speechSynthesis.speak(u); // Fallback
    } finally {
        ttsState.isFetching = false;
        ttsState.isPlaying = false;
        setBubbleState(btn, false);
    }
}

/**
 * Extract only speakable text (removes media elements)
 * @param {string} text - HTML text
 * @returns {string} Clean text
 */
export function extractSpeakableText(text) {
    if (!text) return "";

    const temp = document.createElement('div');
    temp.innerHTML = text;

    // Remove all media elements
    temp.querySelectorAll('img, video, audio, .generated-image, .generated-video, .audio-result, .image-loading-placeholder, .video-loading-placeholder, .music-loading-placeholder, .bubble-audio-player').forEach(el => el.remove());

    let cleanText = temp.textContent || temp.innerText || "";
    cleanText = cleanText.replace(/\s+/g, ' ').trim();

    return cleanText;
}

/**
 * Convert Blob to data URL
 * @param {Blob} blob - Blob to convert
 * @returns {Promise<string>} Data URL
 */
export async function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
    });
}

/**
 * Generate TTS and attach player to bubble
 * Uses the /tts/batch backend endpoint which handles chunking and stitching server-side.
 *
 * @param {string} text - Text to speak
 * @param {HTMLElement} bubbleElement - Bubble element
 * @param {HTMLElement} btn - Speaker button
 */
export async function speakToBubble(text, bubbleElement, btn) {
    // Cancel any previous in-flight TTS instead of blocking
    if (ttsState.isFetching) {
        cancelActiveTTS();
    }

    try {
        ttsState.isFetching = true;
        setBubbleState(btn, true);

        const speakableText = extractSpeakableText(text);

        if (!speakableText) {
            toast("No text to speak");
            ttsState.isFetching = false;
            setBubbleState(btn, false);
            return;
        }

        const voiceConfig = getTTSVoice();
        const isGemini = voiceConfig.provider !== "openai";

        // Voice-aware cache key so switching voices regenerates audio
        const contentKey = simpleHash(speakableText + ':' + voiceConfig.provider + ':' + voiceConfig.voice);

        // Check cache first
        const audioCache = getAudioCache();
        if (audioCache[contentKey]) {
            console.log('[TTS] Cache hit for key:', contentKey);
            bubbleElement.dataset.audioKey = contentKey;
            attachAudioPlayer(bubbleElement, audioCache[contentKey], true, btn);
            ttsState.isFetching = false;
            setBubbleState(btn, false);
            return;
        }

        console.log(`[TTS] Starting batch generation for ${speakableText.length} characters`);
        toast("Generating audio...");

        // Create AbortController so this request can be cancelled (page reload, new TTS, etc.)
        const abortCtrl = new AbortController();
        activeTTSAbort = abortCtrl;

        const r = await fetch("/tts/batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: abortCtrl.signal,
            body: JSON.stringify({
                text: speakableText,
                provider: voiceConfig.provider,
                voice: voiceConfig.voice,
                model: isGemini ? getGeminiModel(voiceConfig.provider) : TTS_MODEL,
                format: TTS_FMT,
                operator: localStorage.getItem("bbx_operator") || ""
            })
        });

        if (!r.ok) {
            const errorText = await r.text();
            console.error('[TTS] Batch TTS failed:', errorText);
            throw new Error('TTS generation failed');
        }

        const blob = await r.blob();
        activeTTSAbort = null; // Request complete, clear abort controller

        const audioDataURL = await blobToDataURL(blob);

        console.log(`[TTS] Batch audio received, size: ${(audioDataURL.length / 1024).toFixed(2)} KB`);

        // Cache audio with voice-aware key
        audioCache[contentKey] = audioDataURL;
        saveAudioCache();

        bubbleElement.dataset.audioKey = contentKey;
        attachAudioPlayer(bubbleElement, audioDataURL, true, btn);
        toast("Audio ready");

    } catch (e) {
        if (e.name === 'AbortError') {
            console.log('[TTS] Request cancelled');
            return;
        }
        console.error("[TTS] Speak to bubble error:", e);
        toast("Audio generation failed: " + e.message);
    } finally {
        activeTTSAbort = null;
        ttsState.isFetching = false;
        setBubbleState(btn, false);
    }
}

// =============================================================================
// Audio Player Functions
// =============================================================================

/**
 * Ensure URL is absolute (for Android WebView)
 * @param {string} url - URL to process
 * @returns {string} Absolute URL
 */
export function ensureAbsoluteUrl(url) {
    if (!url) return url;
    if (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('blob:') || url.startsWith('data:')) {
        return url;
    }
    return window.location.origin + (url.startsWith('/') ? url : '/' + url);
}

/**
 * Download TTS audio file
 * @param {string} audioSrc - Audio source URL
 * @param {string} filenamePrefix - Filename prefix
 */
export function downloadTTSAudio(audioSrc, filenamePrefix = 'tts-audio') {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const isDataURL = audioSrc.startsWith('data:');

    let ext = 'mp3';
    if (isDataURL) {
        const mimeMatch = audioSrc.match(/^data:audio\/([^;]+)/);
        if (mimeMatch) {
            ext = mimeMatch[1] === 'mpeg' ? 'mp3' : mimeMatch[1];
        }
    }

    const filename = `${filenamePrefix}-${timestamp}.${ext}`;

    if (typeof Android !== 'undefined' && Android.downloadFile) {
        if (isDataURL) {
            fetch(audioSrc)
                .then(res => res.blob())
                .then(blob => {
                    const blobUrl = URL.createObjectURL(blob);
                    Android.downloadFile(blobUrl, filename);
                    toast(`Downloading ${filename}...`);
                    setTimeout(() => URL.revokeObjectURL(blobUrl), 5000);
                })
                .catch(e => {
                    console.error('Download error:', e);
                    toast('Download failed');
                });
        } else {
            Android.downloadFile(ensureAbsoluteUrl(audioSrc), filename);
            toast(`Downloading ${filename}...`);
        }
    } else {
        const a = document.createElement('a');
        a.href = audioSrc;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        toast(`Downloading ${filename}...`);
    }
}

/**
 * Download generated audio from URL
 * @param {string} audioURL - Audio URL
 * @param {string} filenamePrefix - Filename prefix
 */
export function downloadGeneratedAudio(audioURL, filenamePrefix = 'generated-audio') {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);

    let ext = 'wav';
    const urlPath = audioURL.split('?')[0];
    const urlExt = urlPath.split('.').pop();
    if (['wav', 'mp3', 'ogg', 'webm', 'm4a'].includes(urlExt)) {
        ext = urlExt;
    }

    const filename = `${filenamePrefix}-${timestamp}.${ext}`;

    if (typeof Android !== 'undefined' && Android.downloadFile) {
        Android.downloadFile(ensureAbsoluteUrl(audioURL), filename);
        toast(`Downloading ${filename}...`);
    } else {
        const a = document.createElement('a');
        a.href = audioURL;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        toast(`Downloading ${filename}...`);
    }
}

/**
 * Attach audio player to bubble element
 * @param {HTMLElement} bubbleElement - Bubble element
 * @param {string} audioDataURL - Audio data URL
 * @param {boolean} autoplay - Auto-play audio
 * @param {HTMLElement} speakerBtn - Speaker button
 */
export function attachAudioPlayer(bubbleElement, audioDataURL, autoplay = false, speakerBtn = null) {
    const existingPlayer = bubbleElement.querySelector('.bubble-audio-player');
    if (existingPlayer) existingPlayer.remove();

    const ttsBtn = speakerBtn || bubbleElement.querySelector('.bubble-btn[title="Listen"]');

    const audioContainer = document.createElement('div');
    audioContainer.className = 'bubble-audio-player';
    audioContainer.innerHTML = `
        <audio src="${audioDataURL}" preload="metadata"></audio>
        <button class="audio-play-btn" title="Play/Pause">
            <svg class="play-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            <svg class="pause-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="display:none">
                <rect x="6" y="4" width="4" height="16"/>
                <rect x="14" y="4" width="4" height="16"/>
            </svg>
        </button>
        <div class="audio-progress-container">
            <div class="audio-progress-bar">
                <div class="audio-progress-fill"></div>
            </div>
        </div>
        <span class="audio-time">0:00</span>
        <button class="audio-download-btn" title="Download Audio">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
        </button>
    `;

    const audio = audioContainer.querySelector('audio');
    const playBtn = audioContainer.querySelector('.audio-play-btn');
    const playIcon = audioContainer.querySelector('.play-icon');
    const pauseIcon = audioContainer.querySelector('.pause-icon');
    const progressBar = audioContainer.querySelector('.audio-progress-bar');
    const progressFill = audioContainer.querySelector('.audio-progress-fill');
    const timeDisplay = audioContainer.querySelector('.audio-time');

    const formatTime = (seconds) => {
        if (isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    playBtn.onclick = () => {
        if (audio.paused) audio.play();
        else audio.pause();
    };

    audio.onplay = () => {
        playIcon.style.display = 'none';
        pauseIcon.style.display = 'block';
        playBtn.classList.add('playing');
        if (ttsBtn) {
            ttsBtn.classList.add('bubble-btn-playing');
            ttsBtn.classList.remove('bubble-btn-loading');
        }
    };

    audio.onpause = () => {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
        playBtn.classList.remove('playing');
    };

    audio.onended = () => {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
        playBtn.classList.remove('playing');
        progressFill.style.width = '0%';
        if (ttsBtn) ttsBtn.classList.remove('bubble-btn-playing');
    };

    audio.ontimeupdate = () => {
        if (audio.duration) {
            const percent = (audio.currentTime / audio.duration) * 100;
            progressFill.style.width = `${percent}%`;
            timeDisplay.textContent = formatTime(audio.currentTime);
        }
    };

    audio.onloadedmetadata = () => {
        timeDisplay.textContent = formatTime(audio.duration);
    };

    progressBar.onclick = (e) => {
        const rect = progressBar.getBoundingClientRect();
        const percent = (e.clientX - rect.left) / rect.width;
        audio.currentTime = percent * audio.duration;
    };

    const downloadBtn = audioContainer.querySelector('.audio-download-btn');
    downloadBtn.onclick = () => downloadTTSAudio(audioDataURL, 'tts-response');

    const controlsBar = bubbleElement.querySelector('.bubble-controls');
    if (controlsBar) {
        controlsBar.insertAdjacentElement('afterend', audioContainer);
    } else {
        bubbleElement.appendChild(audioContainer);
    }

    if (autoplay) {
        audio.play().catch(e => console.log("Autoplay prevented by browser:", e));
    }
}

/**
 * Attach audio player for generated audio (Gemini TTS, Lyria Music)
 * @param {HTMLElement} bubbleElement - Bubble element
 * @param {string} audioURL - Audio URL
 * @param {string} filenamePrefix - Filename prefix
 */
export function attachGeneratedAudioPlayer(bubbleElement, audioURL, filenamePrefix = 'generated-audio') {
    const audioContainer = document.createElement('div');
    audioContainer.className = 'bubble-audio-player generated-audio-player';
    audioContainer.innerHTML = `
        <audio src="${audioURL}" preload="metadata"></audio>
        <button class="audio-play-btn" title="Play/Pause">
            <svg class="play-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            <svg class="pause-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="display:none">
                <rect x="6" y="4" width="4" height="16"/>
                <rect x="14" y="4" width="4" height="16"/>
            </svg>
        </button>
        <div class="audio-progress-container">
            <div class="audio-progress-bar">
                <div class="audio-progress-fill"></div>
            </div>
        </div>
        <span class="audio-time">0:00</span>
        <button class="audio-download-btn" title="Download Audio">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
        </button>
    `;

    const audio = audioContainer.querySelector('audio');
    const playBtn = audioContainer.querySelector('.audio-play-btn');
    const playIcon = audioContainer.querySelector('.play-icon');
    const pauseIcon = audioContainer.querySelector('.pause-icon');
    const progressBar = audioContainer.querySelector('.audio-progress-bar');
    const progressFill = audioContainer.querySelector('.audio-progress-fill');
    const timeDisplay = audioContainer.querySelector('.audio-time');

    const formatTime = (seconds) => {
        if (isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    playBtn.onclick = () => {
        if (audio.paused) audio.play();
        else audio.pause();
    };

    audio.onplay = () => {
        playIcon.style.display = 'none';
        pauseIcon.style.display = 'block';
        playBtn.classList.add('playing');
    };

    audio.onpause = () => {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
        playBtn.classList.remove('playing');
    };

    audio.onended = () => {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
        playBtn.classList.remove('playing');
        progressFill.style.width = '0%';
    };

    audio.ontimeupdate = () => {
        if (audio.duration) {
            const percent = (audio.currentTime / audio.duration) * 100;
            progressFill.style.width = `${percent}%`;
            timeDisplay.textContent = formatTime(audio.currentTime);
        }
    };

    audio.onloadedmetadata = () => {
        timeDisplay.textContent = formatTime(audio.duration);
    };

    progressBar.onclick = (e) => {
        const rect = progressBar.getBoundingClientRect();
        const percent = (e.clientX - rect.left) / rect.width;
        audio.currentTime = percent * audio.duration;
    };

    const downloadBtn = audioContainer.querySelector('.audio-download-btn');
    downloadBtn.onclick = () => downloadGeneratedAudio(audioURL, filenamePrefix);

    const audioResult = bubbleElement.querySelector('.audio-result');
    if (audioResult) {
        audioResult.appendChild(audioContainer);
    } else {
        bubbleElement.appendChild(audioContainer);
    }
}

/**
 * Speak thinking/reasoning content
 * @param {HTMLElement} btn - Button element
 */
export async function speakThinkingContent(btn) {
    if (ttsState.isFetching) {
        toast("Audio generation in progress...");
        return;
    }

    const panel = btn.closest('.streaming-thinking-panel');
    if (!panel) {
        toast("No thinking content found");
        return;
    }

    const thinkingContent = panel.querySelector('.thinking-content');
    if (!thinkingContent) {
        toast("No thinking content found");
        return;
    }

    const text = thinkingContent.textContent || thinkingContent.innerText || "";
    if (!text.trim()) {
        toast("No thinking content to speak");
        return;
    }

    try {
        ttsState.isFetching = true;
        btn.classList.add('thinking-btn-loading');
        btn.classList.remove('thinking-btn-playing');
        toast("Generating reasoning audio...");

        const speakableText = text.trim();
        const voiceConfig = getTTSVoice();
        const isGemini = voiceConfig.provider !== "openai";
        const operator = (localStorage.getItem("bbx_operator") || "").trim();

        // Single batch request — backend handles chunking + parallel + stitching
        const abortCtrl = new AbortController();
        activeTTSAbort = abortCtrl;

        const r = await fetch("/tts/batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: abortCtrl.signal,
            body: JSON.stringify({
                text: speakableText,
                provider: voiceConfig.provider,
                voice: voiceConfig.voice,
                model: isGemini ? getGeminiModel(voiceConfig.provider) : TTS_MODEL,
                format: TTS_FMT,
                operator: operator
            })
        });

        if (!r.ok) throw new Error("TTS generation failed");

        const blob = await r.blob();
        activeTTSAbort = null;
        const audioDataURL = await blobToDataURL(blob);

        btn.classList.remove('thinking-btn-loading');
        btn.classList.add('thinking-btn-playing');

        attachThinkingAudioPlayer(panel, audioDataURL, btn, true);
        toast("Reasoning audio ready");

    } catch (e) {
        if (e.name === 'AbortError') {
            console.log('[SpeakThinking] Request cancelled');
            btn.classList.remove('thinking-btn-loading');
            return;
        }
        console.error("Speak thinking error:", e);
        toast("Audio generation failed");
        btn.classList.remove('thinking-btn-loading');
        btn.classList.remove('thinking-btn-playing');
    } finally {
        activeTTSAbort = null;
        ttsState.isFetching = false;
    }
}

/**
 * Attach audio player for thinking content
 * @param {HTMLElement} panel - Thinking panel
 * @param {string} audioDataURL - Audio data URL
 * @param {HTMLElement} btn - Button element
 * @param {boolean} autoplay - Auto-play audio
 */
export function attachThinkingAudioPlayer(panel, audioDataURL, btn, autoplay = false) {
    const existingPlayer = panel.parentElement.querySelector('.thinking-audio-player');
    if (existingPlayer) existingPlayer.remove();

    const audioContainer = document.createElement('div');
    audioContainer.className = 'bubble-audio-player thinking-audio-player';
    audioContainer.innerHTML = `
        <audio src="${audioDataURL}" preload="metadata"></audio>
        <span class="audio-label-icon">🧠</span>
        <button class="audio-play-btn" title="Play/Pause">
            <svg class="play-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            <svg class="pause-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="display:none">
                <rect x="6" y="4" width="4" height="16"/>
                <rect x="14" y="4" width="4" height="16"/>
            </svg>
        </button>
        <div class="audio-progress-container">
            <div class="audio-progress-bar">
                <div class="audio-progress-fill"></div>
            </div>
        </div>
        <span class="audio-time">0:00</span>
        <button class="audio-download-btn" title="Download Audio">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
        </button>
    `;

    const audio = audioContainer.querySelector('audio');
    const playBtn = audioContainer.querySelector('.audio-play-btn');
    const playIcon = audioContainer.querySelector('.play-icon');
    const pauseIcon = audioContainer.querySelector('.pause-icon');
    const progressBar = audioContainer.querySelector('.audio-progress-bar');
    const progressFill = audioContainer.querySelector('.audio-progress-fill');
    const timeDisplay = audioContainer.querySelector('.audio-time');

    const formatTime = (seconds) => {
        if (isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    playBtn.onclick = () => {
        if (audio.paused) audio.play();
        else audio.pause();
    };

    audio.onplay = () => {
        playIcon.style.display = 'none';
        pauseIcon.style.display = 'block';
        playBtn.classList.add('playing');
        btn.classList.add('thinking-btn-playing');
        btn.classList.remove('thinking-btn-loading');
    };

    audio.onpause = () => {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
        playBtn.classList.remove('playing');
    };

    audio.onended = () => {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
        playBtn.classList.remove('playing');
        progressFill.style.width = '0%';
        btn.classList.remove('thinking-btn-playing');
    };

    audio.ontimeupdate = () => {
        if (audio.duration) {
            const percent = (audio.currentTime / audio.duration) * 100;
            progressFill.style.width = `${percent}%`;
            timeDisplay.textContent = formatTime(audio.currentTime);
        }
    };

    audio.onloadedmetadata = () => {
        timeDisplay.textContent = formatTime(audio.duration);
    };

    progressBar.onclick = (e) => {
        const rect = progressBar.getBoundingClientRect();
        const percent = (e.clientX - rect.left) / rect.width;
        audio.currentTime = percent * audio.duration;
    };

    const downloadBtn = audioContainer.querySelector('.audio-download-btn');
    downloadBtn.onclick = () => downloadTTSAudio(audioDataURL, 'tts-reasoning');

    panel.insertAdjacentElement('afterend', audioContainer);

    if (autoplay) {
        audio.play().catch(e => console.log("Autoplay prevented by browser:", e));
    }
}

// =============================================================================
// Voice Selector Initialization
// =============================================================================

/**
 * Build the TTS voice dropdown from the backend catalog (single source of truth).
 * Falls back to whatever static <option>s are already in the DOM if the fetch fails
 * or returns an empty catalog.
 */
async function populateVoiceCatalog() {
    const sel = document.getElementById("ttsVoiceSelect");
    if (!sel) return;
    try {
        const res = await fetch("/tts/catalog");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const { groups } = await res.json();
        if (!groups || !groups.length) return;  // keep static fallback
        const prev = sel.value || TTS_DEFAULT_VOICE;
        sel.innerHTML = "";
        for (const g of groups) {
            const og = document.createElement("optgroup");
            og.label = `${g.label} (${g.voices.length} voices)`;
            for (const v of g.voices) {
                const o = document.createElement("option");
                o.value = v.id;
                o.textContent = `${v.name} - ${v.description}`;
                og.appendChild(o);
            }
            sel.appendChild(og);
        }
        sel.value = [...sel.options].some(o => o.value === prev) ? prev : TTS_DEFAULT_VOICE;
    } catch (e) {
        console.error("voice catalog fetch failed, keeping static options", e);
    }
}

/**
 * Initialize voice selector dropdown and preview button
 */
export function initVoiceSelector() {
    const voiceSelect = document.getElementById("ttsVoiceSelect");
    const previewBtn = document.getElementById("btnPreviewVoice");

    if (voiceSelect) {
        // Build options from the backend catalog (SoT), then sync the
        // operator's saved preference once the options exist.
        populateVoiceCatalog().then(syncVoiceDropdown);

        voiceSelect.onchange = () => {
            const operator = (localStorage.getItem("bbx_operator") || "").trim();
            if (operator) {
                setOperatorVoice(operator, voiceSelect.value);
                toast(`Voice set to ${voiceSelect.options[voiceSelect.selectedIndex].text.split(' - ')[0]}`);
            } else {
                toast("Select an operator first");
            }
        };
    }

    if (previewBtn) {
        previewBtn.onclick = async () => {
            const previewText = "Hello! This is a preview of the selected voice.";

            try {
                previewBtn.disabled = true;
                previewBtn.textContent = "...";

                const selectedValue = voiceSelect ? voiceSelect.value : getCurrentVoice();
                const voiceConfig = parseVoiceValue(selectedValue);

                const audioUrl = await generateTTSAudioWithVoice(previewText, voiceConfig);
                if (audioUrl) {
                    const audio = new Audio(audioUrl);
                    audio.play();
                    audio.onended = () => {
                        if (audioUrl.startsWith('blob:')) URL.revokeObjectURL(audioUrl);
                    };
                } else {
                    toast("Preview failed");
                }
            } catch (e) {
                console.error("Preview error:", e);
                toast("Preview error: " + e.message);
            } finally {
                previewBtn.disabled = false;
                previewBtn.textContent = "▶️";
            }
        };
    }
}

// =============================================================================
// STT Functions
// =============================================================================

/**
 * Check if microphone is available
 * @returns {boolean} Mic available
 */
export function hasMic() {
    return (typeof AndroidMic !== "undefined") || !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

/**
 * Check if running on native Android
 * @returns {boolean} Is native Android
 */
export function isNativeAndroid() {
    return typeof AndroidMic !== "undefined";
}

// =============================================================================
// STT public entry points — streaming-first with legacy record->/stt fallback
//
// Buttons are wired (in ui-setup.js + generation-modals.js) to startSTT /
// startSTTFor / toggleSTTFor / stopSTT. These public functions now route through
// the shared streaming client (sttStream) so dictation is LIVE (editable text
// appears as you speak). If streaming can't get going for an invocation (no STT
// provider configured, ws fails to open, early ws/stt_error), they transparently
// fall back to the legacy MediaRecorder->/stt one-shot path for that invocation.
//
// The legacy implementations are preserved verbatim below as legacy* functions.
// =============================================================================

/**
 * Start STT for a specific input field.
 * Streaming-first; falls back to legacy on streaming-unavailable.
 * @param {string} targetInputId - Target input ID
 * @param {string} buttonId - Button ID
 */
export async function startSTTFor(targetInputId, buttonId) {
    currentSTTTarget = targetInputId || "prompt";
    currentSTTButton = buttonId || "ctlMic";
    await startSTT();
}

/**
 * Toggle STT for a specific input field.
 * Works for both the streaming and legacy paths: if any dictation is active,
 * stop it (re-targeting if a different field is requested); otherwise start.
 * @param {string} targetInputId - Target input ID
 * @param {string} buttonId - Button ID
 */
export function toggleSTTFor(targetInputId, buttonId) {
    const wasStreaming = sttStreamingActive;
    const active = sttStreamingActive || micOn;
    if (active && currentSTTTarget === targetInputId) {
        stopSTT();
    } else if (active) {
        // Switching target — stop current then start the new one.
        stopSTT(true);
        if (wasStreaming) {
            // The streaming client keeps its socket open for a short grace window
            // after stop() (so a trailing final lands), during which sttStream.start
            // no-ops. Wait until it has fully released before retargeting.
            waitForStreamReleaseThen(() => startSTTFor(targetInputId, buttonId));
        } else {
            setTimeout(() => startSTTFor(targetInputId, buttonId), 150);
        }
    } else {
        startSTTFor(targetInputId, buttonId);
    }
}

/**
 * Poll until the streaming client has fully released its socket (post-stop grace
 * window elapsed), then run cb. Capped so it never hangs if something goes wrong.
 * @param {Function} cb
 */
function waitForStreamReleaseThen(cb) {
    const startedAt = Date.now();
    const MAX_WAIT_MS = 2000;
    const tick = () => {
        if (!sttStream.isStreaming() || (Date.now() - startedAt) > MAX_WAIT_MS) {
            cb();
        } else {
            setTimeout(tick, 100);
        }
    };
    setTimeout(tick, 100);
}

/**
 * Start STT dictation into currentSTTTarget.
 * Tries the streaming client first; on streaming-unavailable for THIS
 * invocation, falls back to the legacy MediaRecorder->/stt flow.
 */
export async function startSTT() {
    if (!hasMic()) {
        toast("Microphone not supported");
        return;
    }
    // Already dictating? No-op (button handlers gate on isMicOn, but be safe).
    if (sttStreamingActive || micOn) return;

    // A just-stopped streaming session keeps its socket open briefly (grace
    // window for a trailing final), during which sttStream.start() no-ops. If we
    // started now we'd light the button but capture nothing. Wait it out first.
    if (sttStream.isStreaming()) {
        waitForStreamReleaseThen(() => startSTT());
        return;
    }

    const targetId = currentSTTTarget || "prompt";
    const buttonId = currentSTTButton || "ctlMic";

    // Mark the streaming attempt active + light the button up immediately. If we
    // fall back, legacyStartSTT re-asserts its own button state.
    sttStreamingActive = true;
    const btn = $(buttonId);
    if (btn) btn.setAttribute("aria-pressed", "true");

    try {
        await sttStream.start(targetId, buttonId, {
            onUnavailable: (reason) => {
                // Streaming couldn't run for this invocation — drop streaming
                // state and hand off to the legacy record->/stt path.
                console.warn("[STT] Streaming unavailable, falling back to legacy:", reason);
                sttStreamingActive = false;
                legacyStartSTT();
            }
        });
    } catch (e) {
        // Defensive: any synchronous throw from the streaming client → legacy.
        console.error("[STT] Streaming start threw, falling back to legacy:", e);
        sttStreamingActive = false;
        legacyStartSTT();
    }
}

/**
 * Stop STT dictation.
 * @param {boolean} cancelOnly - Cancel without committing (used when retargeting)
 */
export function stopSTT(cancelOnly = false) {
    if (sttStreamingActive) {
        sttStreamingActive = false;
        try { sttStream.stop(); } catch (e) { console.error("[STT] sttStream.stop error:", e); }
        // Streaming applies text live + commits its own final via the ws grace
        // window; clear the button visual now (stt-stream also clears it).
        const btn = $(currentSTTButton);
        if (btn) btn.setAttribute("aria-pressed", "false");
        return;
    }
    legacyStopSTT(cancelOnly);
}

/**
 * Legacy: Start STT recording (MediaRecorder -> POST /stt). Fallback path.
 */
async function legacyStartSTT() {
    if (!hasMic()) {
        toast("Microphone not supported");
        return;
    }
    const btn = $(currentSTTButton);
    if (btn) btn.setAttribute("aria-pressed", "true");
    micOn = true;

    if (isNativeAndroid()) {
        try {
            AndroidMic.startRecording();
        } catch (e) {
            console.error(e);
            toast("Mic error: " + e.message);
            legacyStopSTT(true);
        }
    } else {
        try {
            audioChunks = [];
            // Audio constraints - require echo cancellation
            const audioConstraints = {
                audio: {
                    sampleRate: { ideal: 48000 },
                    channelCount: 1,              // FORCE MONO - Critical for audio interfaces
                    echoCancellation: true,       // Required - not just ideal
                    noiseSuppression: true,       // Required - not just ideal
                    autoGainControl: true         // Required - not just ideal
                }
            };
            mediaStream = await navigator.mediaDevices.getUserMedia(audioConstraints);
            mediaRec = new MediaRecorder(mediaStream);
            mediaRec.ondataavailable = (e) => {
                if (e.data.size > 0) audioChunks.push(e.data);
            };
            mediaRec.onstop = async () => {
                const blob = new Blob(audioChunks, { type: "audio/webm" });
                if (!blob.size) {
                    toast("No audio captured");
                    exitTranscribingState();
                    return;
                }
                const fd = new FormData();
                fd.append("file", blob, "mic.webm");
                try {
                    const r = await fetch("/stt", { method: "POST", body: fd });
                    const j = await r.json();
                    if (!r.ok) throw new Error(j.detail || `STT failed: ${r.status}`);
                    const text = (j.text || "").trim();
                    if (text) {
                        const box = $(currentSTTTarget);
                        if (box) {
                            if (box.value.trim()) {
                                box.value = box.value.trim() + " " + text;
                            } else {
                                box.value = text;
                            }
                            box.focus();
                            box.setSelectionRange(box.value.length, box.value.length);
                        }
                    } else {
                        toast("No transcript returned");
                    }
                } catch (e) {
                    toast(`STT error: ${e.message}`);
                } finally {
                    exitTranscribingState();
                }
            };
            mediaRec.start();
            toast("Recording… tap mic again to stop");
        } catch (e) {
            toast(`Mic error: ${e.message}`);
            legacyStopSTT(true);
        }
    }
}

/**
 * Legacy: Stop STT recording (MediaRecorder path). Fallback path.
 * @param {boolean} cancelOnly - Cancel without processing
 */
function legacyStopSTT(cancelOnly = false) {
    const btn = $(currentSTTButton);
    micOn = false;

    if (cancelOnly) {
        // Cancel: go directly to idle
        if (btn) btn.setAttribute("aria-pressed", "false");
    } else {
        // Normal stop: enter transcribing state (spinning ring)
        enterTranscribingState();
    }

    if (isNativeAndroid()) {
        if (cancelOnly) return;
        try {
            AndroidMic.stopRecording();
        } catch (e) {
            console.error(e);
            toast("Stop error: " + e.message);
            exitTranscribingState();
        }
    } else {
        if (mediaRec && mediaRec.state !== "inactive") {
            mediaRec.stop();
        }
        if (mediaStream) {
            mediaStream.getTracks().forEach(track => track.stop());
        }
        mediaStream = null;
        mediaRec = null;
        if (!cancelOnly) toast("Processing transcript...");
    }
}

/**
 * Setup native Android callbacks
 */
export function setupNativeSTTCallbacks() {
    console.log('[STT] Setting up native Android callbacks...');

    window.onNativeMicStart = function() {
        console.log('[STT] onNativeMicStart called');
        toast("Recording… tap mic again to stop");
    };

    window.onNativeMicTranscript = function(text) {
        console.log('[STT] onNativeMicTranscript called with text:', text);
        console.log('[STT] currentSTTTarget:', currentSTTTarget);
        const box = $(currentSTTTarget);
        console.log('[STT] Target input box found:', !!box);
        if (box) {
            if (box.value.trim()) {
                box.value = box.value.trim() + " " + text;
            } else {
                box.value = text;
            }
            console.log('[STT] Text inserted, new value length:', box.value.length);
            box.focus();
            box.setSelectionRange(box.value.length, box.value.length);
        } else {
            console.error('[STT] Target input box not found! currentSTTTarget =', currentSTTTarget);
        }
        exitTranscribingState();
        toast("Transcript ready");
    };

    window.onNativeMicError = function(error) {
        exitTranscribingState();
        toast("Mic error: " + error);
        console.error("[STT] Native mic error:", error);
    };

    console.log('[STT] Native callbacks setup complete');
}

// =============================================================================
// State Getters/Setters
// =============================================================================

/**
 * Get TTS state
 * @returns {Object} TTS state
 */
export function getTTSState() {
    return ttsState;
}

/**
 * Get last assistant text
 * @returns {string} Last assistant text
 */
export function getLastAssistantText() {
    return lastAssistantText;
}

/**
 * Set last assistant text
 * @param {string} text - Text to store
 */
export function setLastAssistantText(text) {
    lastAssistantText = text;
}

/**
 * Check if the mic is on (dictation in progress) — true for EITHER the streaming
 * client OR the legacy recorder, so button handlers gate correctly in both modes.
 * @returns {boolean} Mic state
 */
export function isMicOn() {
    return sttStreamingActive || micOn;
}

// =============================================================================
// STT Transcribing State (visual feedback while Whisper processes)
// =============================================================================

let sttTranscribing = false;
let sttTranscribeTimeout = null;

/**
 * Enter transcribing visual state — spinning ring on mic button
 */
function enterTranscribingState() {
    sttTranscribing = true;
    const btn = $(currentSTTButton);
    if (btn) {
        btn.setAttribute("aria-pressed", "false");
        btn.setAttribute("aria-busy", "true");
        btn.classList.add("stt-transcribing");
    }
    // Safety: auto-exit after 30 seconds
    clearTimeout(sttTranscribeTimeout);
    sttTranscribeTimeout = setTimeout(() => {
        if (sttTranscribing) {
            console.warn('[STT] Transcription timeout — clearing state');
            exitTranscribingState();
        }
    }, 30000);
}

/**
 * Exit transcribing visual state — back to idle
 */
function exitTranscribingState() {
    sttTranscribing = false;
    clearTimeout(sttTranscribeTimeout);
    const btn = $(currentSTTButton);
    if (btn) {
        btn.setAttribute("aria-busy", "false");
        btn.classList.remove("stt-transcribing");
    }
}

/**
 * Check if STT transcription is in progress
 * @returns {boolean}
 */
export function isTranscribing() {
    return sttTranscribing;
}

// =============================================================================
// Auto TTS Functions
// =============================================================================

/**
 * Initialize Auto TTS toggle button
 * Sets up the button handler and loads saved state from localStorage
 *
 * Auto-TTS Mode: Waits for COMPLETE response before generating audio.
 * This is more reliable than real-time chunking and avoids:
 * - Multiple API calls (wasted resources)
 * - Audio gaps between chunks
 * - Cut-off audio from incomplete generation
 */
export function initAutoTTS() {
    // Load saved state from localStorage
    window.autoTTSEnabled = localStorage.getItem('bb_auto_tts') === 'true';

    const autoTTSBtn = $("ctlSpeak");
    if (autoTTSBtn) {
        // Set initial state from localStorage
        autoTTSBtn.setAttribute('aria-pressed', window.autoTTSEnabled ? 'true' : 'false');

        // Set up click handler
        autoTTSBtn.onclick = () => {
            // Toggle Auto-TTS mode
            window.autoTTSEnabled = !window.autoTTSEnabled;
            localStorage.setItem('bb_auto_tts', window.autoTTSEnabled ? 'true' : 'false');

            autoTTSBtn.setAttribute('aria-pressed', window.autoTTSEnabled ? 'true' : 'false');

            // Show toast notification
            if (window.autoTTSEnabled) {
                toast("Auto-TTS enabled - Responses will be spoken after completion");
            } else {
                toast("Auto-TTS disabled");
            }
        };
    }

    // Set up the global triggerAutoTTS function
    // This is called ONLY after the full response is complete
    window.triggerAutoTTS = function(text, bubble) {
        if (!window.autoTTSEnabled || !text || !bubble) {
            console.log('[AutoTTS] Skipped - enabled:', window.autoTTSEnabled, 'text:', !!text, 'bubble:', !!bubble);
            return;
        }

        console.log('[AutoTTS] Triggering for complete response, length:', text.length);

        // Find the speak button in the bubble
        const speakBtn = bubble.querySelector('.speak-btn');
        if (speakBtn) {
            // Wait a bit to ensure DOM is fully updated
            setTimeout(() => {
                console.log('[AutoTTS] Starting TTS generation for complete response');
                speakToBubble(text, bubble, speakBtn);
            }, 1000);  // 1 second delay to ensure response is complete
        } else {
            console.warn('[AutoTTS] No speak button found in bubble');
        }
    };
}
