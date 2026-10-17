// The telephony wizard shows the phone PIN that /asterisk/gateways/{id}/apply
// generated (it is in that one response only) and warns when none is set,
// because inbound calls are refused without one (audit X03-security-backend-09).
//
// Run: node --test Portal/modules/telephony-wizard.pin.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

// Minimal DOM stand-in for escapeHtml (textContent -> innerHTML escaping).
globalThis.document = {
    createElement() {
        let text = "";
        return {
            set textContent(v) { text = String(v); },
            get innerHTML() {
                return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
            },
        };
    },
};

const { renderPhonePinNotice } = await import("./telephony-wizard.js");

test("a freshly generated PIN is shown once", () => {
    const html = renderPhonePinNotice({ enabled: true, configured: true, generated: true, length: 6, pin: "739104" });
    assert.match(html, /739104/);
    assert.match(html, /PHONE_PIN_CODE/);
});

test("an existing PIN is never echoed", () => {
    assert.equal(renderPhonePinNotice({ enabled: true, configured: true, generated: false, length: 6 }), "");
});

test("no PIN set warns that calls will be refused", () => {
    const html = renderPhonePinNotice({ enabled: true, configured: false, generated: false, length: 0 });
    assert.match(html, /refused/);
    assert.match(html, /wiz-notice-warn/);
});

test("disabled gate or an old backend shows nothing", () => {
    assert.equal(renderPhonePinNotice({ enabled: false, configured: false, generated: false }), "");
    assert.equal(renderPhonePinNotice(undefined), "");
});

test("the PIN is HTML-escaped", () => {
    const html = renderPhonePinNotice({ enabled: true, configured: true, generated: true, pin: "<img src=x onerror=alert(1)>" });
    assert.doesNotMatch(html, /<img/);
});
