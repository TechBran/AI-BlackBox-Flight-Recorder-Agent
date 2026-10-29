/**
 * native-bridge.js
 * The Android WebView's native bridge, in one place.
 *
 * Inside the Android app the Portal runs in a WebView whose PortalActivity
 * injects JavaScript interfaces: `AndroidMic` (native capture: a 24 kHz PCM16
 * stream, or a one-shot recording) and `Android` (file downloads). The voice
 * modules each probed and drove them with their own copy of the checks
 * (`typeof AndroidMic !== 'undefined'`), and the copies drifted (audit
 * P02-voice-web-12). The streaming callbacks the app calls back
 * (window.onNativeStreamStart/AudioChunk/Stop/Error) are owned by
 * mic-arbiter.js, which routes them to one module at a time; this module binds
 * them through it.
 *
 * Created: 2026-10-29 (audit rank 75)
 */

import { bindNativeStream, unbindNativeStream } from './mic-arbiter.js';

/** Sample rate of AndroidMic.startAudioStreaming (PCM16, mono). */
export const NATIVE_MIC_SAMPLE_RATE = 24000;

function androidMic() {
    return typeof AndroidMic !== 'undefined' ? AndroidMic : null;  // eslint-disable-line no-undef
}

/** @returns {boolean} Running inside the Android app's WebView (AndroidMic injected) */
export function isNativeAndroid() {
    return androidMic() !== null;
}

/** @returns {boolean} The native bridge can stream mic audio */
export function hasNativeStreaming() {
    const mic = androidMic();
    return !!(mic && typeof mic.startAudioStreaming === 'function');
}

/** @returns {boolean} The native bridge can make a one-shot recording */
export function hasNativeRecording() {
    const mic = androidMic();
    return !!(mic && typeof mic.startRecording === 'function');
}

/** @returns {boolean} Some microphone is reachable (native bridge or getUserMedia) */
export function hasMic() {
    return isNativeAndroid() || !!(typeof navigator !== 'undefined' && navigator.mediaDevices
        && navigator.mediaDevices.getUserMedia);
}

/**
 * Start the native 24 kHz PCM16 stream for `owner` (the app calls back the
 * handlers through mic-arbiter's dispatchers).
 * @param {string} owner - The module's mic-arbiter owner id
 * @param {{onStart?: Function, onChunk?: (base64: string) => void,
 *          onStop?: Function, onError?: (error: string) => void}} handlers
 */
export function startNativeStream(owner, { onStart, onChunk, onStop, onError } = {}) {
    bindNativeStream(owner, {
        onNativeStreamStart: onStart,
        onNativeAudioChunk: onChunk,
        onNativeStreamStop: onStop,
        onNativeStreamError: onError,
    });
    androidMic().startAudioStreaming();
}

/**
 * startNativeStream, resolved when the app confirms the stream started and
 * rejected on its error or after `timeoutMs` without a start.
 * @param {string} owner
 * @param {object} handlers - As for startNativeStream
 * @param {{timeoutMs?: number}} [opts]
 * @returns {Promise<void>}
 */
export function startNativeStreamAwaited(owner, handlers = {}, { timeoutMs = 3000 } = {}) {
    return new Promise((resolve, reject) => {
        let settled = false;
        const settle = (fn, value) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            fn(value);
        };
        const timer = setTimeout(() => settle(reject, new Error('Native streaming timeout')), timeoutMs);
        try {
            startNativeStream(owner, {
                ...handlers,
                onStart: () => {
                    if (handlers.onStart) handlers.onStart();
                    settle(resolve);
                },
                onError: (error) => {
                    if (handlers.onError) handlers.onError(error);
                    settle(reject, new Error(error));
                },
            });
        } catch (e) {
            settle(reject, e);
        }
    });
}

/**
 * Stop the native stream and drop `owner`'s callback binding (a stale stop
 * from another module cannot cut the current owner's feed).
 * @param {string} owner
 */
export function stopNativeStream(owner) {
    const mic = androidMic();
    if (mic && typeof mic.stopAudioStreaming === 'function') {
        try { mic.stopAudioStreaming(); } catch (e) { /* already stopped */ }
    }
    unbindNativeStream(owner);
}

/**
 * Hand a file to the Android app to download (the WebView cannot save a
 * download itself). False outside the app, or when the bridge lacks it.
 * @param {string} url - Absolute URL or blob: URL
 * @param {string} filename
 * @returns {boolean}
 */
export function nativeDownloadFile(url, filename) {
    if (typeof Android === 'undefined' || !Android.downloadFile) return false;  // eslint-disable-line no-undef
    Android.downloadFile(url, filename);  // eslint-disable-line no-undef
    return true;
}

/** @returns {boolean} The Android download bridge is available */
export function hasNativeDownload() {
    return typeof Android !== 'undefined' && !!Android.downloadFile;  // eslint-disable-line no-undef
}
