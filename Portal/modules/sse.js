/**
 * sse.js
 * Server-Sent Events reader for fetch() response bodies.
 *
 * The /chat/stream loop used to split EACH reader.read() chunk on "\n\n" on
 * its own, so a frame that straddled two reads was lost: the first half failed
 * JSON.parse and the second half had no "event:"/"data:" prefix. The largest
 * frames (the "done" frame carries the whole reply) were the likeliest to
 * straddle. It also kept only the last data: line of a multi-line event
 * (audit P01-chat-core-web-04). This reader keeps a carry-over buffer across
 * reads and follows the WHATWG event-stream line rules, the same contract as
 * Android's SSEClient (BufferedReader.forEachLine + StringBuilder):
 *
 *   - lines end in "\n", "\r\n" or "\r" (a "\r" at the end of a read ends its
 *     line; an "\n" opening the next read is the other half of that "\r\n");
 *   - a blank line dispatches the event; a line starting with ":" is a comment
 *     (the server's ": keepalive" heartbeat) and dispatches nothing;
 *   - every data: line is kept, joined with "\n";
 *   - an event with no data: is not dispatched; one with no event: is a
 *     "message".
 *
 * At a clean end of stream a last event whose lines were all complete is still
 * dispatched (a server that ends on a single "\n"); a frame cut off mid-line
 * is dropped, never handed on as if it were whole.
 *
 * Created: 2026-10-23 (audit rank 65)
 */

/**
 * @typedef {{event: string, data: string, id: string}} SseEvent
 */

/**
 * Build an incremental parser. feed() takes decoded text in any split;
 * end() flushes a complete trailing event.
 * @param {(ev: SseEvent) => void} onEvent - Called once per dispatched event
 * @returns {{feed: (text: string) => void, end: () => void}}
 */
export function createSseParser(onEvent) {
    // The current line's text so far, when it spans reads.
    let partial = [];
    // The last read ended in a CR: an LF opening the next read is its pair.
    let skipLF = false;
    let eventType = '';
    let data = [];
    let lastId = '';
    // True while the current event has at least one complete field line.
    let pending = false;

    const dispatch = () => {
        const type = eventType;
        const lines = data;
        eventType = '';
        data = [];
        pending = false;
        if (!lines.length) return;
        onEvent({ event: type || 'message', data: lines.join('\n'), id: lastId });
    };

    const processLine = (line) => {
        if (line === '') {
            dispatch();
            return;
        }
        if (line.charCodeAt(0) === 58 /* ':' */) return; // comment / keepalive
        const colon = line.indexOf(':');
        let field = line;
        let value = '';
        if (colon !== -1) {
            field = line.slice(0, colon);
            value = line.slice(colon + 1);
            if (value.charCodeAt(0) === 32 /* ' ' */) value = value.slice(1);
        }
        pending = true;
        if (field === 'event') eventType = value;
        else if (field === 'data') data.push(value);
        else if (field === 'id' && value.indexOf('\0') === -1) lastId = value;
        // 'retry' and unknown fields are ignored.
    };

    return {
        feed(text) {
            if (!text) return;
            let start = 0;
            if (skipLF) {
                skipLF = false;
                if (text.charCodeAt(0) === 10 /* '\n' */) start = 1;
            }
            // Only THIS read is searched, and each terminator once, reused
            // until the scan passes it. Searching the whole carried buffer for
            // a CR from every line start was quadratic in the size of a read
            // (a CR-free stream rescanned to the end per line), and appending
            // to one buffer re-searched a long unfinished line on every read.
            let lf = text.indexOf('\n', start);
            let cr = text.indexOf('\r', start);
            while (lf !== -1 || cr !== -1) {
                let end;
                let next;
                if (cr !== -1 && (lf === -1 || cr < lf)) {
                    end = cr;
                    if (cr + 1 === text.length) {
                        skipLF = true; // the next read may open with this CR's LF
                        next = cr + 1;
                    } else {
                        next = text.charCodeAt(cr + 1) === 10 ? cr + 2 : cr + 1;
                    }
                } else {
                    end = lf;
                    next = lf + 1;
                }
                let line = text.slice(start, end);
                if (partial.length) {
                    partial.push(line);
                    line = partial.join('');
                    partial = [];
                }
                processLine(line);
                start = next;
                if (lf !== -1 && lf < start) lf = text.indexOf('\n', start);
                if (cr !== -1 && cr < start) cr = text.indexOf('\r', start);
            }
            if (start < text.length) partial.push(text.slice(start));
        },
        end() {
            skipLF = false;
            // Text after the last line end is a truncated line: drop it and
            // the event it belonged to.
            if (partial.length) {
                partial = [];
                eventType = '';
                data = [];
                pending = false;
                return;
            }
            if (pending) dispatch();
        },
    };
}

/**
 * Read an SSE response body to the end.
 * @param {ReadableStreamDefaultReader<Uint8Array>} reader - response.body.getReader()
 * @param {(ev: SseEvent) => void} onEvent - Called once per dispatched event
 * @param {object} [opts]
 * @param {() => void} [opts.onChunk] - Called for every read that carried bytes
 *   (keepalive comments included), e.g. to re-arm a stall watchdog
 * @param {AbortSignal} [opts.signal] - Aborting it cancels the reader and
 *   rejects with an AbortError
 * @returns {Promise<void>} Resolves at a clean end of stream; rejects on a read
 *   error (the events that arrived before it have already been dispatched)
 */
export async function parseSseStream(reader, onEvent, { onChunk, signal } = {}) {
    const decoder = new TextDecoder();
    const parser = createSseParser(onEvent);
    const abortError = () => {
        const err = new Error('SSE stream aborted');
        err.name = 'AbortError';
        return err;
    };
    const onAbort = () => {
        try { reader.cancel(); } catch (_) { /* already closed */ }
    };
    if (signal) {
        if (signal.aborted) {
            onAbort();
            throw abortError();
        }
        signal.addEventListener('abort', onAbort, { once: true });
    }
    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (signal && signal.aborted) throw abortError();
            if (done) break;
            if (value && value.length) {
                if (onChunk) onChunk();
                parser.feed(decoder.decode(value, { stream: true }));
                if (signal && signal.aborted) throw abortError();
            }
        }
        parser.feed(decoder.decode());
        parser.end();
    } finally {
        if (signal) signal.removeEventListener('abort', onAbort);
    }
}
