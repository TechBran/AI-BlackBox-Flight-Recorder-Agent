/**
 * gemini-recorder.js
 * Audio recording for Gemini integration with waveform visualization
 *
 * Source: Portal/app.js lines 9965-10622
 * Refactored: 2025-12-20
 */

import { $, toast, toastError } from './core-utils.js';
import { getAttachedFiles, addAttachedFile } from './file-upload.js';
import { claimMic, releaseMic } from './mic-arbiter.js';

/** This module's mic-arbiter owner id. */
const MIC_OWNER = 'gemini-recorder';

// =============================================================================
// State
// =============================================================================

/** Whether currently recording */
let geminiRecording = false;

/** Audio chunks collected during recording */
let geminiAudioChunks = [];

/** Recording start timestamp */
let geminiRecordStartTime = null;

/** Timer interval ID */
let geminiTimerInterval = null;

/** Animation frame ID */
let geminiAnimationFrame = null;

/** Audio context for visualization */
let geminiAudioContext = null;

/** Audio analyser node */
let geminiAnalyser = null;

/** Whether using native Android recording */
let usingNativeGeminiRecording = false;

/** Shared media stream (from STT module) */
let mediaStream = null;

/** Shared media recorder */
let mediaRec = null;

// Simulated waveform state
let simWavePhase = 0;
let simWaveAmplitude = 0.3;
let simWaveTargetAmp = 0.5;
let simWaveNoiseOffset = 0;

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Check if microphone is available
 * @returns {boolean}
 */
function hasMic() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

/**
 * Check if running in native Android app
 * @returns {boolean}
 */
function isNativeAndroid() {
    return typeof AndroidBridge !== 'undefined' || typeof AndroidMic !== 'undefined';
}

// =============================================================================
// Timer and Waveform
// =============================================================================

/**
 * Update timer display
 * @param {HTMLElement} timeDisplay - Time display element
 */
function updateTimer(timeDisplay) {
    if (!geminiRecordStartTime || !timeDisplay) return;
    const elapsed = Math.floor((Date.now() - geminiRecordStartTime) / 1000);
    const minutes = Math.floor(elapsed / 60);
    const seconds = elapsed % 60;
    timeDisplay.textContent = minutes + ':' + (seconds < 10 ? '0' : '') + seconds;
}

/**
 * Draw real waveform from audio analyser
 * @param {HTMLCanvasElement} canvas - Canvas element
 */
function drawWaveform(canvas) {
    if (!geminiAnalyser || !canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    geminiAnimationFrame = requestAnimationFrame(() => drawWaveform(canvas));

    const dataArray = new Uint8Array(geminiAnalyser.frequencyBinCount);
    geminiAnalyser.getByteTimeDomainData(dataArray);

    ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#4caf50';
    ctx.beginPath();

    const sliceWidth = canvas.width / dataArray.length;
    let x = 0;
    for (let i = 0; i < dataArray.length; i++) {
        const v = dataArray[i] / 128.0;
        const y = v * canvas.height / 2;
        if (i === 0) {
            ctx.moveTo(x, y);
        } else {
            ctx.lineTo(x, y);
        }
        x += sliceWidth;
    }
    ctx.lineTo(canvas.width, canvas.height / 2);
    ctx.stroke();
}

/**
 * Draw simulated waveform for native Android recording
 * @param {HTMLCanvasElement} canvas - Canvas element
 */
function drawSimulatedWaveform(canvas) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    geminiAnimationFrame = requestAnimationFrame(() => drawSimulatedWaveform(canvas));

    // Smoothly interpolate amplitude
    simWaveAmplitude += (simWaveTargetAmp - simWaveAmplitude) * 0.08;

    // Randomly change target amplitude
    if (Math.random() < 0.05) {
        simWaveTargetAmp = 0.2 + Math.random() * 0.6;
    }

    simWavePhase += 0.15;
    simWaveNoiseOffset += 0.02;

    ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.lineWidth = 2;
    ctx.strokeStyle = '#4caf50';
    ctx.beginPath();

    const centerY = canvas.height / 2;
    const points = 128;
    const sliceWidth = canvas.width / points;

    for (let i = 0; i < points; i++) {
        const x = i * sliceWidth;
        const normalizedX = i / points;

        const wave1 = Math.sin(normalizedX * 8 + simWavePhase) * 0.4;
        const wave2 = Math.sin(normalizedX * 12 + simWavePhase * 1.3) * 0.3;
        const wave3 = Math.sin(normalizedX * 20 + simWavePhase * 0.7) * 0.2;
        const noise = Math.sin(normalizedX * 50 + simWaveNoiseOffset) * 0.1;

        const envelope = Math.sin(normalizedX * Math.PI);
        const combinedWave = (wave1 + wave2 + wave3 + noise) * envelope * simWaveAmplitude;
        const y = centerY + combinedWave * centerY;

        if (i === 0) {
            ctx.moveTo(x, y);
        } else {
            ctx.lineTo(x, y);
        }
    }

    ctx.stroke();

    // Glow effect
    ctx.strokeStyle = 'rgba(76, 175, 80, 0.3)';
    ctx.lineWidth = 6;
    ctx.stroke();
}

// =============================================================================
// Audio Conversion
// =============================================================================

/**
 * Convert AudioBuffer to WAV format
 * @param {AudioBuffer} buffer - Audio buffer to convert
 * @returns {ArrayBuffer} WAV data
 */
function audioBufferToWav(buffer) {
    const numChannels = buffer.numberOfChannels;
    const sampleRate = buffer.sampleRate;
    const format = 1; // PCM
    const bitDepth = 16;
    const bytesPerSample = bitDepth / 8;
    const blockAlign = numChannels * bytesPerSample;

    let samples = buffer.getChannelData(0);
    if (numChannels === 2) {
        const left = buffer.getChannelData(0);
        const right = buffer.getChannelData(1);
        samples = new Float32Array(left.length * 2);
        for (let i = 0; i < left.length; i++) {
            samples[i * 2] = left[i];
            samples[i * 2 + 1] = right[i];
        }
    }

    const dataLength = samples.length * bytesPerSample;
    const arrayBuffer = new ArrayBuffer(44 + dataLength);
    const view = new DataView(arrayBuffer);

    function writeString(offset, str) {
        for (let i = 0; i < str.length; i++) {
            view.setUint8(offset + i, str.charCodeAt(i));
        }
    }

    // WAV header
    writeString(0, 'RIFF');
    view.setUint32(4, 36 + dataLength, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, format, true);
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * blockAlign, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitDepth, true);
    writeString(36, 'data');
    view.setUint32(40, dataLength, true);

    // Audio data
    let offset = 44;
    for (let i = 0; i < samples.length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        offset += 2;
    }

    return arrayBuffer;
}

// =============================================================================
// Recording Cleanup
// =============================================================================

/**
 * Cleanup recording state and resources
 * @param {HTMLButtonElement} recordBtn - Record button element
 * @param {HTMLElement} waveformContainer - Waveform container element
 */
function cleanupGeminiRecording(recordBtn, waveformContainer) {
    console.log('[GeminiAudio] Cleanup started');

    if (geminiTimerInterval) {
        clearInterval(geminiTimerInterval);
        geminiTimerInterval = null;
    }
    if (geminiAnimationFrame) {
        cancelAnimationFrame(geminiAnimationFrame);
        geminiAnimationFrame = null;
    }
    if (geminiAudioContext) {
        geminiAudioContext.close().catch(() => {});
        geminiAudioContext = null;
    }
    geminiAnalyser = null;
    geminiAudioChunks = [];
    geminiRecordStartTime = null;
    geminiRecording = false;
    usingNativeGeminiRecording = false;
    releaseMic(MIC_OWNER);

    if (recordBtn) recordBtn.setAttribute('aria-pressed', 'false');
    if (waveformContainer) waveformContainer.classList.add('hide');

    // Cleanup shared media stream
    if (mediaStream) {
        console.log('[GeminiAudio] Stopping mediaStream tracks...');
        mediaStream.getTracks().forEach(track => {
            console.log('[GeminiAudio] Stopping track:', track.kind, track.readyState);
            track.stop();
        });
        mediaStream = null;
    }
    if (mediaRec) {
        console.log('[GeminiAudio] Clearing mediaRec');
        mediaRec = null;
    }

    console.log('[GeminiAudio] Cleanup complete - mic should be released');
}

// =============================================================================
// Main Initialization
// =============================================================================

/**
 * Initialize Gemini audio recorder
 * @param {Object} options - Configuration options
 * @param {Function} [options.renderPreviews] - Function to render file previews
 */
export function initGeminiAudioRecorder(options = {}) {
    const recordBtn = $('ctlRecordAudio');
    const waveformContainer = $('audioWaveformContainer');
    const stopBtn = $('btnStopRecording');
    const cancelBtn = $('btnCancelRecording');
    const timeDisplay = $('recordingTime');
    const canvas = $('waveformCanvas');

    if (!recordBtn) {
        console.log('[GeminiAudio] Record button not found');
        return;
    }

    console.log('[GeminiAudio] Initializing...');

    const renderPreviews = options.renderPreviews || window.renderPreviews || (() => {});

    function updateButtonVisibility() {
        const providerSelect = $('providerSelect');
        const provider = providerSelect ? providerSelect.value : '';
        if (provider === 'google') {
            recordBtn.classList.remove('hide');
        } else {
            recordBtn.classList.add('hide');
            if (geminiRecording) {
                cancelGeminiRecording();
            }
        }
    }

    function processGeminiRecording() {
        console.log('[GeminiAudio] Processing recording, chunks:', geminiAudioChunks.length);

        if (geminiAudioChunks.length === 0) {
            toast('No audio captured');
            cleanupGeminiRecording(recordBtn, waveformContainer);
            return;
        }

        const blob = new Blob(geminiAudioChunks, { type: 'audio/webm' });
        console.log('[GeminiAudio] Blob size:', blob.size);

        if (blob.size === 0) {
            toast('No audio captured');
            cleanupGeminiRecording(recordBtn, waveformContainer);
            return;
        }

        toast('Processing audio...');

        const reader = new FileReader();
        reader.onload = async function() {
            const tempContext = new (window.AudioContext || window.webkitAudioContext)();
            tempContext.decodeAudioData(reader.result, async function(audioBuffer) {
                console.log('[GeminiAudio] Decoded audio buffer, duration:', audioBuffer.duration);

                const wavBuffer = audioBufferToWav(audioBuffer);
                const wavBlob = new Blob([wavBuffer], { type: 'audio/wav' });

                const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
                const filename = 'recording_' + timestamp + '.wav';
                const file = new File([wavBlob], filename, { type: 'audio/wav' });

                console.log('[GeminiAudio] Created WAV file:', filename, 'size:', file.size);

                // Add to attached files
                addAttachedFile(file);
                renderPreviews();

                toast('Transcribing audio...');

                // Send to Whisper for transcription
                try {
                    console.log('[GeminiAudio] Sending audio to Whisper for transcription...');
                    const fd = new FormData();
                    fd.append('file', wavBlob, filename);

                    const sttResponse = await fetch('/stt', { method: 'POST', body: fd });
                    const sttResult = await sttResponse.json();

                    if (sttResponse.ok && sttResult.text) {
                        const transcription = sttResult.text.trim();
                        console.log('[GeminiAudio] Transcription received:', transcription.substring(0, 100) + '...');

                        const promptBox = $('prompt');
                        if (promptBox) {
                            if (promptBox.value.trim()) {
                                promptBox.value = promptBox.value.trim() + ' ' + transcription;
                            } else {
                                promptBox.value = transcription;
                            }
                            promptBox.focus();
                            promptBox.dispatchEvent(new Event('input', { bubbles: true }));
                        }

                        toast('Recording attached + transcribed! Review and send.');
                    } else {
                        console.warn('[GeminiAudio] Whisper transcription failed:', sttResult);
                        toast('Recording attached! (Transcription unavailable)');
                    }
                } catch (sttErr) {
                    console.error('[GeminiAudio] Whisper transcription error:', sttErr);
                    toast('Recording attached! (Transcription failed)');
                }

                cleanupGeminiRecording(recordBtn, waveformContainer);
                tempContext.close();
            }, function(err) {
                console.error('[GeminiAudio] Failed to decode audio:', err);
                toast('Failed to process recording');
                cleanupGeminiRecording(recordBtn, waveformContainer);
            });
        };
        reader.onerror = function() {
            console.error('[GeminiAudio] FileReader error');
            toast('Failed to read audio');
            cleanupGeminiRecording(recordBtn, waveformContainer);
        };

        reader.readAsArrayBuffer(blob);
    }

    async function processNativeGeminiRecording(base64Data, mimeType, fileName) {
        console.log('[GeminiAudio] Processing native recording...');

        try {
            const byteCharacters = atob(base64Data);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], { type: mimeType });

            console.log('[GeminiAudio] Created blob from native audio:', blob.size, 'bytes');

            const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
            const newFileName = 'recording_' + timestamp + '.m4a';
            const correctedMimeType = (mimeType === 'audio/m4a') ? 'audio/aac' : mimeType;
            const file = new File([blob], newFileName, { type: correctedMimeType });

            console.log('[GeminiAudio] Created file:', newFileName, 'size:', file.size);

            addAttachedFile(file);
            renderPreviews();

            toast('Transcribing audio...');

            try {
                console.log('[GeminiAudio] Sending audio to Whisper for transcription...');
                const fd = new FormData();
                fd.append('file', blob, newFileName);

                const sttResponse = await fetch('/stt', { method: 'POST', body: fd });
                const sttResult = await sttResponse.json();

                if (sttResponse.ok && sttResult.text) {
                    const transcription = sttResult.text.trim();
                    console.log('[GeminiAudio] Transcription received:', transcription.substring(0, 100) + '...');

                    const promptBox = $('prompt');
                    if (promptBox) {
                        if (promptBox.value.trim()) {
                            promptBox.value = promptBox.value.trim() + ' ' + transcription;
                        } else {
                            promptBox.value = transcription;
                        }
                        promptBox.focus();
                        promptBox.dispatchEvent(new Event('input', { bubbles: true }));
                    }

                    toast('Recording attached + transcribed! Review and send.');
                } else {
                    console.warn('[GeminiAudio] Whisper transcription failed:', sttResult);
                    toast('Recording attached! (Transcription unavailable)');
                }
            } catch (sttErr) {
                console.error('[GeminiAudio] Whisper transcription error:', sttErr);
                toast('Recording attached! (Transcription failed)');
            }

            cleanupGeminiRecording(recordBtn, waveformContainer);

        } catch (err) {
            console.error('[GeminiAudio] Error processing native audio:', err);
            toast('Failed to process recording');
            cleanupGeminiRecording(recordBtn, waveformContainer);
        }
    }

    async function startGeminiRecording() {
        // Dictation and the voice agents let go of the mic through the shared
        // arbiter (window.micOn / window.stopSTT were never assigned).
        await claimMic(MIC_OWNER, cancelGeminiRecording);

        if (!hasMic()) {
            toast('Microphone not available');
            return;
        }

        console.log('[GeminiAudio] Starting recording...');
        geminiRecording = true;
        geminiAudioChunks = [];
        recordBtn.setAttribute('aria-pressed', 'true');

        if (isNativeAndroid() && typeof AndroidMic !== 'undefined' && typeof AndroidMic.stopRecordingAndGetAudio === 'function') {
            console.log('[GeminiAudio] Using native Android recording');
            usingNativeGeminiRecording = true;

            window.onNativeMicAudio = function(base64Data, mimeType, fileName) {
                console.log('[GeminiAudio] Received native audio:', fileName);
                processNativeGeminiRecording(base64Data, mimeType, fileName);
            };

            const originalErrorHandler = window.onNativeMicError;
            window.onNativeMicError = function(error) {
                console.error('[GeminiAudio] Native recording error:', error);
                toast('Recording error: ' + error);
                cleanupGeminiRecording(recordBtn, waveformContainer);
                window.onNativeMicError = originalErrorHandler;
            };

            try {
                AndroidMic.startRecording();
                geminiRecordStartTime = Date.now();

                if (waveformContainer) waveformContainer.classList.remove('hide');
                if (timeDisplay) timeDisplay.textContent = '0:00';

                geminiTimerInterval = setInterval(() => updateTimer(timeDisplay), 100);

                simWavePhase = 0;
                simWaveAmplitude = 0.3;
                simWaveTargetAmp = 0.5;
                drawSimulatedWaveform(canvas);

                toast('Recording for Gemini analysis...');
                console.log('[GeminiAudio] Native recording started');
            } catch (err) {
                console.error('[GeminiAudio] Failed to start native recording:', err);
                geminiRecording = false;
                usingNativeGeminiRecording = false;
                recordBtn.setAttribute('aria-pressed', 'false');
                toast('Mic error: ' + err.message);
            }
        } else {
            console.log('[GeminiAudio] Using WebView getUserMedia recording');
            usingNativeGeminiRecording = false;

            if (mediaStream) {
                console.log('[GeminiAudio] Releasing existing mediaStream...');
                mediaStream.getTracks().forEach(track => track.stop());
                mediaStream = null;
                await new Promise(resolve => setTimeout(resolve, 100));
            }

            try {
                console.log('[GeminiAudio] Calling getUserMedia with optimized constraints...');
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
                console.log('[GeminiAudio] Got media stream in MONO with processing enabled');

                try {
                    geminiAudioContext = new (window.AudioContext || window.webkitAudioContext)();
                    geminiAnalyser = geminiAudioContext.createAnalyser();
                    geminiAnalyser.fftSize = 2048;
                    const source = geminiAudioContext.createMediaStreamSource(mediaStream);
                    source.connect(geminiAnalyser);
                } catch (e) {
                    console.warn('[GeminiAudio] Could not setup visualizer:', e);
                }

                mediaRec = new MediaRecorder(mediaStream);

                mediaRec.ondataavailable = (e) => {
                    if (e.data.size > 0) {
                        geminiAudioChunks.push(e.data);
                    }
                };

                mediaRec.onstop = () => {
                    console.log('[GeminiAudio] MediaRecorder stopped');
                    if (geminiRecording) {
                        processGeminiRecording();
                    }
                };

                mediaRec.onerror = (e) => {
                    console.error('[GeminiAudio] MediaRecorder error:', e);
                    toast('Recording error');
                    cleanupGeminiRecording(recordBtn, waveformContainer);
                };

                mediaRec.start();
                geminiRecordStartTime = Date.now();

                if (waveformContainer) waveformContainer.classList.remove('hide');
                if (timeDisplay) timeDisplay.textContent = '0:00';

                geminiTimerInterval = setInterval(() => updateTimer(timeDisplay), 100);
                if (geminiAnalyser) drawWaveform(canvas);

                toast('Recording for Gemini analysis...');
                console.log('[GeminiAudio] WebView recording started');

            } catch (err) {
                console.error('[GeminiAudio] Failed to start:', err.name, err.message);
                geminiRecording = false;
                recordBtn.setAttribute('aria-pressed', 'false');

                if (err.name === 'NotAllowedError') {
                    toast('Microphone permission denied');
                } else if (err.name === 'NotReadableError') {
                    toastError('Mic in use! Close other apps using the microphone and try again.');
                } else {
                    toast('Mic error: ' + err.message);
                }
            }
        }
    }

    function stopGeminiRecording() {
        console.log('[GeminiAudio] Stopping recording...');

        if (usingNativeGeminiRecording) {
            console.log('[GeminiAudio] Stopping native recording...');
            try {
                AndroidMic.stopRecordingAndGetAudio();
            } catch (err) {
                console.error('[GeminiAudio] Error stopping native recording:', err);
                toast('Stop error: ' + err.message);
                cleanupGeminiRecording(recordBtn, waveformContainer);
            }
        } else {
            if (mediaRec && mediaRec.state !== 'inactive') {
                mediaRec.stop();
            }
        }
    }

    function cancelGeminiRecording() {
        console.log('[GeminiAudio] Cancelling recording...');
        geminiAudioChunks = [];
        geminiRecording = false;

        if (usingNativeGeminiRecording) {
            console.log('[GeminiAudio] Cancelling native recording...');
            try {
                if (typeof AndroidMic.releaseMic === 'function') {
                    AndroidMic.releaseMic();
                } else {
                    AndroidMic.stopRecording();
                }
            } catch (err) {
                console.error('[GeminiAudio] Error cancelling native recording:', err);
            }
        } else {
            if (mediaRec && mediaRec.state !== 'inactive') {
                mediaRec.stop();
            }
        }

        cleanupGeminiRecording(recordBtn, waveformContainer);
        toast('Recording cancelled');
    }

    // Event listeners
    recordBtn.addEventListener('click', function() {
        if (geminiRecording) {
            stopGeminiRecording();
        } else {
            startGeminiRecording();
        }
    });

    if (stopBtn) {
        stopBtn.addEventListener('click', stopGeminiRecording);
    }

    if (cancelBtn) {
        cancelBtn.addEventListener('click', cancelGeminiRecording);
    }

    // Update visibility when provider changes
    const providerSelect = $('providerSelect');
    if (providerSelect) {
        providerSelect.addEventListener('change', updateButtonVisibility);
    }

    updateButtonVisibility();
    console.log('[GeminiAudio] Initialized successfully');
}

// =============================================================================
// State Getters
// =============================================================================

/**
 * Check if currently recording
 * @returns {boolean}
 */
export function isRecording() {
    return geminiRecording;
}

/**
 * Get recording duration in seconds
 * @returns {number}
 */
export function getRecordingDuration() {
    if (!geminiRecordStartTime) return 0;
    return Math.floor((Date.now() - geminiRecordStartTime) / 1000);
}
