/**
 * mic-arbiter.js
 * One microphone owner at a time across the Portal.
 *
 * Dictation (tts-stt.js / stt-stream.js), the Gemini recorder and the three
 * live voice agents (GPT Realtime, Gemini Live, Grok Live) each take the mic.
 * They used to coordinate through window.micOn, window.stopSTT and
 * window.geminiRecording, which nothing ever assigned, so starting a voice
 * session over live dictation left two capture consumers running ("Mic in
 * use!" / NotReadableError on the Android WebView) (audit P02-voice-web-02).
 *
 *   claimMic(owner, release)  -- the current owner, if another, is asked to let
 *                                go (its release() is awaited), then `owner`
 *                                holds the mic;
 *   releaseMic(owner)         -- drops the claim if `owner` still holds it.
 *
 * Native Android streaming (PortalActivity calls window.onNativeStreamStart /
 * onNativeAudioChunk / onNativeStreamStop / onNativeStreamError) is ONE global
 * callback set. Modules overwrote it and later "restored" what they had saved,
 * so a late restore from one module silently cut another's audio feed (dictation
 * restoring null under a running Realtime session). The arbiter installs one
 * dispatcher per callback and routes it to the module that bound last:
 *
 *   bindNativeStream(owner, handlers) -- route the callbacks to `owner`;
 *   unbindNativeStream(owner)         -- stop routing, only if `owner` still
 *                                        holds them (a stale unbind is a no-op).
 *
 * Created: 2026-10-23 (audit rank 67)
 */

/** The global callbacks the Android bridge calls for streaming capture. */
export const NATIVE_STREAM_CALLBACKS = Object.freeze([
    'onNativeStreamStart', 'onNativeAudioChunk', 'onNativeStreamStop', 'onNativeStreamError',
]);

/** @type {{id: string, release: (Function|null)}|null} */
let _owner = null;
let _nativeOwner = null;
let _nativeHandlers = null;
let _dispatchersInstalled = false;

/**
 * Take the microphone for `owner`, stopping whoever holds it now.
 * @param {string} owner - A stable module id ('dictation', 'realtime', ...)
 * @param {() => (void|Promise<void>)} [release] - Called when ANOTHER owner claims
 * @returns {Promise<void>} Resolves once the previous owner has let go
 */
export async function claimMic(owner, release = null) {
    const previous = _owner;
    _owner = { id: owner, release };
    if (previous && previous.id !== owner && typeof previous.release === 'function') {
        try {
            await previous.release();
        } catch (e) {
            console.warn(`[MIC] ${previous.id} failed to release the mic for ${owner}:`, e);
        }
    }
}

/**
 * Give the microphone back (no-op when `owner` no longer holds it).
 * @param {string} owner
 */
export function releaseMic(owner) {
    if (_owner && _owner.id === owner) _owner = null;
}

/** @returns {string|null} Who holds the microphone */
export function currentMicOwner() {
    return _owner ? _owner.id : null;
}

function installDispatchers() {
    if (_dispatchersInstalled || typeof window === 'undefined') return;
    _dispatchersInstalled = true;
    for (const name of NATIVE_STREAM_CALLBACKS) {
        window[name] = (...args) => {
            const handler = _nativeHandlers ? _nativeHandlers[name] : null;
            if (typeof handler === 'function') return handler(...args);
            return undefined;
        };
    }
}

/**
 * Route the native streaming callbacks to `owner`.
 * @param {string} owner
 * @param {Object<string, Function>} handlers - Any of NATIVE_STREAM_CALLBACKS
 */
export function bindNativeStream(owner, handlers) {
    installDispatchers();
    _nativeOwner = owner;
    _nativeHandlers = handlers || {};
}

/**
 * Stop routing the native streaming callbacks to `owner` (no-op when another
 * module has bound them since).
 * @param {string} owner
 */
export function unbindNativeStream(owner) {
    if (_nativeOwner !== owner) return;
    _nativeOwner = null;
    _nativeHandlers = null;
}

/** @returns {string|null} Who receives the native streaming callbacks */
export function nativeStreamOwner() {
    return _nativeOwner;
}
