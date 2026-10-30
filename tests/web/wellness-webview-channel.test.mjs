// The in-app Portal's health signer (M7 wave 3b, contract (c); spec §6.3).
//
// Inside the BlackBox Android app the Portal asks the phone to sign a request
// over the message channel the app puts in the paired box's main frame:
//   window.BlackBoxWellness.postMessage(JSON.stringify({id, method, path, sha256}))
// and the answer is a `message` event on window.BlackBoxWellness whose data is
// JSON {id, value} (the X-BlackBox-Origin value or null). The old
// AndroidBridge.wellnessSign is gone. Each request has a fresh id and gets
// exactly one answer; a malformed one gets none, so after SIGN_TIMEOUT_MS
// (2 s) the request goes unsigned. Over plain http the WebView is not a secure
// context and has no crypto.subtle, so the Portal hashes the body itself
// (sha256HexOf), checked here against the box's golden vectors. A GET is
// signed only when it names the Portal's operator once in its query
// (withOperatorQuery), and a signed chat body names the surface
// "portal_webview" (contract (d)). A box refusal of a text that recalls a
// health result (POST /tts*: 403 wellness_text, contract (e)) is recognised
// by isWellnessTextRefusal.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, REPO_ROOT } from "./helpers/portal-dom.mjs";
import { fakeChannel, installChannel, parseRequest } from "./helpers/wellness-channel.mjs";

installDom();
localStorage.setItem("bbx_operator", "wellness-fixture");

const wellness = await import("../../Portal/modules/wellness-section.js");
const {
    wellnessSigner, originProof, withOriginProof, sha256Hex, sha256HexOf, utf8Bytes, withOperatorQuery,
    signedChatRequest, isWellnessTextRefusal, isRefusedProof, SIGN_TIMEOUT_MS, SIGN_CHANNEL, PORTAL_WEBVIEW_SURFACE,
    ORIGIN_HEADER,
} = wellness;

const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const nodeSha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const VECTORS = JSON.parse(readFileSync(resolve(REPO_ROOT, "Orchestrator", "tests", "fixtures", "wellness",
    "origin_proof_vectors.json"), "utf8"));
const vectorBytes = (v) => Buffer.from(v.body_b64, "base64");

function useChannel(t, channel) {
    installChannel(channel);
    t.after(() => installChannel(null));
    return channel;
}

// ── The channel ─────────────────────────────────────────────────────────────

test("the signer is window.BlackBoxWellness with a postMessage, and nothing else", (t) => {
    t.after(() => { installChannel(null); delete window.AndroidBridge; });
    assert.equal(SIGN_CHANNEL, "BlackBoxWellness", "WellnessChatRules.BRIDGE_CHANNEL");
    installChannel(null);
    assert.equal(wellnessSigner(), null, "a desktop browser");
    window.AndroidBridge = { wellnessSign: () => "v1;enr=x;ts=1;nonce=n;sig=s", wellnessOpenSettings() {} };
    assert.equal(wellnessSigner(), null, "the old AndroidBridge.wellnessSign is no signer any more");
    window.BlackBoxWellness = { addEventListener() {} };
    assert.equal(wellnessSigner(), null, "no postMessage, no channel");
    const channel = fakeChannel();
    installChannel(channel);
    assert.equal(wellnessSigner(), channel);
});

test("originProof posts {id, method, path, sha256} and resolves with the answer for that id", async (t) => {
    const channel = useChannel(t, fakeChannel());
    const body = "{\"a\":\"ü\"}";
    const value = await originProof("post", "/chat/stream", body);
    assert.equal(value, `v1;enr=en_0000abcd;ts=1800000001;nonce=${"1".padStart(32, "0")};sig=${"a".repeat(64)}`);
    assert.equal(channel.posted.length, 1);
    const req = JSON.parse(channel.posted[0]);
    assert.deepEqual(Object.keys(req), ["id", "method", "path", "sha256"]);
    assert.match(req.id, /^[A-Za-z0-9_.:-]{1,64}$/);
    assert.equal(req.method, "POST", "upper-case");
    assert.equal(req.path, "/chat/stream");
    assert.equal(req.sha256, nodeSha(Buffer.from(body, "utf8")), "lowercase hex of the exact UTF-8 bytes");
    assert.ok(parseRequest(channel.posted[0]), "a request the app parses");
});

test("the path goes with its query; the empty body is the digest of the empty string", async (t) => {
    const channel = useChannel(t, fakeChannel());
    await originProof("GET", "/wellness/status?operator=wellness-fixture");
    assert.deepEqual(channel.signed, [{ method: "GET", path: "/wellness/status?operator=wellness-fixture", hex: EMPTY_SHA256 }]);
});

test("a fresh id and a fresh proof for every request, never cached", async (t) => {
    const channel = useChannel(t, fakeChannel());
    const a = await originProof("GET", "/wellness/status?operator=wellness-fixture");
    const b = await originProof("GET", "/wellness/status?operator=wellness-fixture");
    assert.notEqual(a, b);
    const ids = channel.posted.map((m) => JSON.parse(m).id);
    assert.equal(new Set(ids).size, 2);
});

test("concurrent requests each get their own answer, whatever order the app answers in", async (t) => {
    const channel = new window.EventTarget();
    const pending = [];
    channel.postMessage = (m) => pending.push(JSON.parse(m));
    useChannel(t, channel);
    const first = originProof("GET", "/wellness/ref/w-20261029-7f3a?operator=wellness-fixture");
    const second = originProof("POST", "/chat/stream", "{}");
    const until = Date.now() + 1000;
    while (pending.length < 2 && Date.now() < until) await new Promise((r) => setTimeout(r, 5));
    assert.equal(pending.length, 2, "both requests were posted");
    for (const req of [...pending].reverse()) {
        channel.dispatchEvent(new window.MessageEvent("message",
            { data: JSON.stringify({ id: req.id, value: `v1;enr=en_1;ts=1;nonce=${req.method};sig=s` }) }));
    }
    assert.equal(await first, "v1;enr=en_1;ts=1;nonce=GET;sig=s");
    assert.equal(await second, "v1;enr=en_1;ts=1;nonce=POST;sig=s");
});

test("an answer for another id is ignored; the first answer for this id wins", async (t) => {
    useChannel(t, fakeChannel({ mode: "wrong-id-first" }));
    assert.match(await originProof("GET", "/wellness/status?operator=wellness-fixture"), /^v1;enr=en_0000abcd;/);
    useChannel(t, fakeChannel({ mode: "twice" }));
    assert.match(await originProof("GET", "/wellness/status?operator=wellness-fixture"), /^v1;enr=en_0000abcd;/);
});

test("other messages on the channel (junk, no id, a number id) are ignored", async (t) => {
    const channel = new window.EventTarget();
    channel.postMessage = (m) => {
        const { id } = JSON.parse(m);
        setTimeout(() => {
            for (const data of ["not json", JSON.stringify({ value: "v1;x" }), JSON.stringify([id, "v1"]),
                JSON.stringify({ id: 7, value: "v1;x" }), null]) {
                channel.dispatchEvent(new window.MessageEvent("message", { data }));
            }
            channel.dispatchEvent(new window.MessageEvent("message", { data: JSON.stringify({ id, value: "v1;enr=en_2;ts=1;nonce=n;sig=s" }) }));
        }, 0);
    };
    useChannel(t, channel);
    assert.equal(await originProof("POST", "/chat", "{}"), "v1;enr=en_2;ts=1;nonce=n;sig=s");
});

test("a request the app does not answer (malformed to it) times out to null: the caller goes unsigned", async (t) => {
    useChannel(t, fakeChannel({ mode: "never" }));
    assert.equal(SIGN_TIMEOUT_MS, 2000);
    const started = Date.now();
    assert.equal(await originProof("POST", "/chat/stream", "{}", { timeoutMs: 40 }), null);
    assert.ok(Date.now() - started >= 35, "it waited for the answer first");
    const base = { "Content-Type": "application/json" };
    assert.equal(await withOriginProof(base, "POST", "/chat", "{}", { timeoutMs: 20 }), base);
});

test("an answer that comes after the timeout is dropped, and the next request still works", async (t) => {
    const channel = useChannel(t, fakeChannel({ delayMs: 60 }));
    assert.equal(await originProof("GET", "/wellness/status?operator=wellness-fixture", "", { timeoutMs: 10 }), null);
    await new Promise((r) => setTimeout(r, 80));                // the late answer arrives and is dropped
    assert.match(await originProof("GET", "/wellness/status?operator=wellness-fixture"), /^v1;/);
    assert.equal(channel.signed.length, 2);
});

for (const [name, sign] of Object.entries({
    null: () => null,
    "a number": () => 42,
    "a header break": () => "v1;enr=x\r\nInjected: 1",
    "an empty string": () => "",
    "an object": () => ({ v: 1 }),
})) {
    test(`an answer of ${name} is no proof`, async (t) => {
        useChannel(t, fakeChannel({ sign }));
        assert.equal(await originProof("GET", "/wellness/status?operator=wellness-fixture"), null);
    });
}

test("a postMessage that throws is no proof, at once", async (t) => {
    const channel = new window.EventTarget();
    channel.postMessage = () => { throw new Error("detached"); };
    useChannel(t, channel);
    const warn = console.warn;
    console.warn = () => {};
    t.after(() => { console.warn = warn; });
    const started = Date.now();
    assert.equal(await originProof("POST", "/chat", "{}"), null);
    assert.ok(Date.now() - started < SIGN_TIMEOUT_MS / 2, "no wait for an answer that cannot come");
});

test("a request too long for the app is never posted", async (t) => {
    const channel = useChannel(t, fakeChannel());
    assert.equal(await originProof("GET", `/wellness/status?operator=${"x".repeat(2100)}`), null);
    assert.equal(channel.posted.length, 0);
});

test("a channel object with only onmessage works too, and keeps the page's own handler", async (t) => {
    const seen = [];
    const channel = {
        onmessage: (e) => seen.push(e.data),
        postMessage(m) {
            const { id } = JSON.parse(m);
            setTimeout(() => this.onmessage({ data: JSON.stringify({ id, value: "v1;enr=en_3;ts=1;nonce=n;sig=s" }) }), 0);
        },
    };
    useChannel(t, channel);
    assert.equal(await originProof("POST", "/chat", "{}"), "v1;enr=en_3;ts=1;nonce=n;sig=s");
    assert.equal(seen.length, 1, "the earlier onmessage still runs");
});

test("no channel: no proof, no wait, the same headers object", async () => {
    installChannel(null);
    assert.equal(await originProof("GET", "/wellness/status?operator=x"), null);
    const base = { "Content-Type": "application/json" };
    assert.equal(await withOriginProof(base, "POST", "/chat", "{}"), base);
});

// ── SHA-256 without SubtleCrypto (plain http in the WebView) ───────────────

test("sha256HexOf: every golden vector's body bytes, as the box hashes them", () => {
    assert.ok(VECTORS.length >= 6);
    for (const v of VECTORS) {
        assert.equal(sha256HexOf(vectorBytes(v)), nodeSha(vectorBytes(v)), `${v.method} ${v.path}`);
        if (v.content_encoding === null) {
            assert.equal(sha256HexOf(v.body_utf8), nodeSha(vectorBytes(v)), `${v.method} ${v.path} as text`);
        }
    }
    assert.equal(sha256HexOf(""), EMPTY_SHA256);
    assert.equal(sha256HexOf(new Uint8Array(0)), EMPTY_SHA256);
});

test("sha256HexOf: block boundaries, long input, multi-byte text", () => {
    for (const n of [1, 55, 56, 57, 63, 64, 65, 119, 120, 128, 1000, 70000]) {
        const bytes = Buffer.alloc(n, n % 251);
        assert.equal(sha256HexOf(bytes), nodeSha(bytes), `${n} bytes`);
    }
    for (const s of ["abc", "Montre connectée — Ärzte ✓ 心拍 😀", "\ud83d", "a\udc00b", "\u0000\u007f\u0080߿ࠀ￿"]) {
        assert.equal(sha256HexOf(s), nodeSha(Buffer.from(s, "utf8")), JSON.stringify(s));
    }
    // FIPS 180-2 appendix B.2.
    assert.equal(sha256HexOf("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
});

test("utf8Bytes without TextEncoder encodes as fetch does (a lone surrogate is U+FFFD)", () => {
    for (const s of ["", "plain", "ü ✓ 心拍 😀", "\ud83d", "x\udc00", "😀\ud83d", "{\"operator\":\"wellness-fixture\"}"]) {
        assert.deepEqual([...utf8Bytes(s, null)], [...Buffer.from(s, "utf8")], JSON.stringify(s));
        assert.deepEqual([...utf8Bytes(s)], [...Buffer.from(s, "utf8")]);
    }
});

test("sha256Hex: SubtleCrypto when present, the page's own when it is absent or fails: the same digest", async () => {
    const text = "ünïcödé ✓ {\"a\":1}";
    const want = nodeSha(Buffer.from(text, "utf8"));
    assert.equal(await sha256Hex(text), want, "the page's SubtleCrypto");
    assert.equal(await sha256Hex(text, null), want, "no SubtleCrypto (plain http)");
    assert.equal(await sha256Hex(text, undefined), want);
    assert.equal(await sha256Hex(text, { digest: () => Promise.reject(new Error("nope")) }), want);
    assert.equal(await sha256Hex("", null), EMPTY_SHA256);
    let used = 0;
    const subtle = { digest: (alg, data) => { used += 1; return globalThis.crypto.subtle.digest(alg, data); } };
    assert.equal(await sha256Hex(text, subtle), want);
    assert.equal(used, 1, "SubtleCrypto is used when it is there");
});

test("the Portal's digest, signed the way the phone signs, reproduces every golden vector", async (t) => {
    // Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json (synthetic
    // keys): sig = HMAC-SHA256(K, "v1|enr|ts|nonce|METHOD|path|sha256hex(body)"),
    // the path without its query. The app does the HMAC; what the Portal owes
    // it is the request target as sent and that hex, over the exact UTF-8
    // bytes of the string body. Without SubtleCrypto too.
    const subtle = Object.getOwnPropertyDescriptor(globalThis.crypto, "subtle")
        || Object.getOwnPropertyDescriptor(Object.getPrototypeOf(globalThis.crypto), "subtle");
    for (const plainHttp of [false, true]) {
        if (plainHttp) {
            Object.defineProperty(globalThis.crypto, "subtle", { value: undefined, configurable: true });
            t.after(() => {
                delete globalThis.crypto.subtle;
                if (!globalThis.crypto.subtle && subtle) Object.defineProperty(globalThis.crypto, "subtle", subtle);
            });
            assert.equal(globalThis.crypto.subtle, undefined);
        }
        for (const v of VECTORS.filter((x) => x.content_encoding === null)) {
            const key = Buffer.from(v.k_b64url, "base64url");
            const target = v.request_target ?? v.path;
            const channel = fakeChannel({
                sign(method, path, hex) {
                    const canonical = `v1|${v.enr}|${v.ts}|${v.nonce}|${method.toUpperCase()}|${path.split("?")[0]}|${hex}`;
                    if (v.canonical !== undefined) assert.equal(canonical, v.canonical, `${v.method} ${target}`);
                    const sig = createHmac("sha256", key).update(canonical, "utf8").digest("hex");
                    return `v1;enr=${v.enr};ts=${v.ts};nonce=${v.nonce};sig=${sig}`;
                },
            });
            installChannel(channel);
            const proof = await originProof(v.method, target, v.body_utf8);
            assert.equal(proof, `v1;enr=${v.enr};ts=${v.ts};nonce=${v.nonce};sig=${v.expected_sig}`,
                `${v.method} ${target}${plainHttp ? " (no SubtleCrypto)" : ""}`);
            assert.equal(channel.signed[0].path, target, "the path goes to the app with its query");
            assert.equal(channel.signed[0].method, v.method.toUpperCase());
        }
    }
    installChannel(null);
});

// ── GETs name the operator; chat POSTs name the surface ────────────────────

test("withOperatorQuery: the operator once, encoded; a path that names one is left alone", () => {
    assert.equal(withOperatorQuery("/wellness/disclosures", "wellness-fixture"), "/wellness/disclosures?operator=wellness-fixture");
    assert.equal(withOperatorQuery("/wellness/ref/w-20261029-7f3a", "Ana María"), "/wellness/ref/w-20261029-7f3a?operator=Ana%20Mar%C3%ADa");
    assert.equal(withOperatorQuery("/fossil/snapshot/SNAP-1?x=1", "op"), "/fossil/snapshot/SNAP-1?x=1&operator=op");
    assert.equal(withOperatorQuery("/wellness/status?operator=op", "op"), "/wellness/status?operator=op");
    assert.equal(withOperatorQuery("/wellness/status?operator=someone", "op"), "/wellness/status?operator=someone",
        "never a second operator (the app then refuses to sign)");
    assert.equal(withOperatorQuery("/wellness/disclosures", ""), "/wellness/disclosures", "no operator, nothing to name");
    assert.equal(withOperatorQuery("/wellness/disclosures"), "/wellness/disclosures?operator=wellness-fixture", "the Portal's operator by default");
    const named = new URL(withOperatorQuery("/x", "a&operator=b"), "http://box").searchParams.getAll("operator");
    assert.deepEqual(named, ["a&operator=b"], "one parameter, whatever the name holds");
});

test("signedChatRequest: a signed body names portal_webview, and the proof is over that exact body", async (t) => {
    const channel = useChannel(t, fakeChannel());
    assert.equal(PORTAL_WEBVIEW_SURFACE, "portal_webview");
    const body = JSON.stringify({ messages: [{ role: "user", content: "ü" }], provider: "anthropic", model: "", operator: "wellness-fixture" });
    const headers = { "Content-Type": "application/json" };
    const out = await signedChatRequest("/chat/stream", body, headers);
    assert.equal(out.signed, true);
    assert.equal(out.body, JSON.stringify({ ...JSON.parse(body), wellness_surface: "portal_webview" }));
    assert.deepEqual(channel.signed, [{ method: "POST", path: "/chat/stream", hex: nodeSha(Buffer.from(out.body, "utf8")) }]);
    assert.equal(out.headers["Content-Type"], "application/json");
    assert.match(out.headers[ORIGIN_HEADER], /^v1;enr=en_0000abcd;/);
    assert.equal(headers[ORIGIN_HEADER], undefined, "the caller's headers are not changed");
});

test("signedChatRequest unsigned (no channel, a null, no answer, not an object): exactly the given body and headers", async (t) => {
    const body = JSON.stringify({ messages: [], operator: "wellness-fixture" });
    const headers = { "Content-Type": "application/json" };
    installChannel(null);
    for (const channel of [null, fakeChannel({ sign: () => null }), fakeChannel({ mode: "never" })]) {
        useChannel(t, channel);
        const out = await signedChatRequest("/chat", body, headers, { timeoutMs: 20 });
        assert.equal(out.signed, false);
        assert.equal(out.body, body, "byte-identical");
        assert.equal(out.headers, headers, "the same headers object");
    }
    useChannel(t, fakeChannel());
    for (const odd of ["[1,2]", "null", "not json", "\"x\""]) {
        const out = await signedChatRequest("/chat", odd, headers);
        assert.deepEqual([out.body, out.headers, out.signed], [odd, headers, false]);
    }
});

test("isRefusedProof: 401, 409 and 410 are a proof the box would not take", () => {
    for (const s of [401, 409, 410]) assert.equal(isRefusedProof(s), true, String(s));
    for (const s of [200, 400, 403, 404, 500, undefined, "401"]) assert.equal(isRefusedProof(s), false, String(s));
});

// ── Speech: the box's refusal of a health text ──────────────────────────────

test("isWellnessTextRefusal: 403 {error: wellness_text} only, and the body stays readable", async () => {
    const res = (status, body) => new Response(typeof body === "string" ? body : JSON.stringify(body),
        { status, headers: { "Content-Type": "application/json" } });
    const refusal = res(403, { error: "wellness_text", detail: "This text repeats your health data." });
    assert.equal(await isWellnessTextRefusal(refusal), true);
    assert.equal((await refusal.json()).error, "wellness_text", "a copy was read, not the response");
    assert.equal(await isWellnessTextRefusal(res(403, { detail: { error: "wellness_text" } })), true, "an HTTPException's shape");
    assert.equal(await isWellnessTextRefusal(res(403, { error: "forbidden" })), false);
    assert.equal(await isWellnessTextRefusal(res(403, "not json")), false);
    assert.equal(await isWellnessTextRefusal(res(500, { error: "wellness_text" })), false, "only a 403");
    const other = res(400, { detail: "bad voice" });
    assert.equal(await isWellnessTextRefusal(other), false);
    assert.equal((await other.json()).detail, "bad voice", "any other answer is not read");
    assert.equal(await isWellnessTextRefusal(null), false);
});
