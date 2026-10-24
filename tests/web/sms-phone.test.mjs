// Phone numbers typed into the SMS inbox (audit P06-comms-web-11). The new
// conversation used the raw text ("+1 (555) 010-0123") as both the thread key and
// the send target: the box files threads under the last 10 digits, so the open
// thread never matched its row after the next poll, and the gateway refuses a
// destination with spaces or punctuation (ami_client.is_valid_sms_destination).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { normalizePhone, parsePhoneInput } from "../../Portal/modules/phone.js";

test("the thread key is the box's rule: the last 10 digits", () => {
    // Orchestrator/sms/message_store.py _normalize_phone
    assert.equal(normalizePhone("+1 (555) 010-0123"), "5550100123");
    assert.equal(normalizePhone("+15550100123"), "5550100123");
    assert.equal(normalizePhone("5550100123"), "5550100123");
    assert.equal(normalizePhone("+44 20 7946 0958"), "2079460958");
    assert.equal(normalizePhone("22000"), "22000", "fewer than 10 digits keep them all");
    assert.equal(normalizePhone(""), "");
    assert.equal(normalizePhone(null), "");
});

test("a typed number gives the thread key and a destination the gateway takes", () => {
    assert.deepEqual(parsePhoneInput("+1 (555) 010-0123"), { key: "5550100123", to: "+15550100123" });
    assert.deepEqual(parsePhoneInput("  555.010.0123 "), { key: "5550100123", to: "5550100123" });
    assert.deepEqual(parsePhoneInput("+15550100123"), { key: "5550100123", to: "+15550100123" });
    assert.deepEqual(parsePhoneInput("+44 20 7946 0958"), { key: "2079460958", to: "+442079460958" });
    for (const { to } of [parsePhoneInput("+1 (555) 010-0123"), parsePhoneInput("555-010-0123")]) {
        assert.match(to, /^\+?[0-9]{3,15}$/, "the backend's E.164-style destination rule");
    }
});

test("anything that is not a phone number is refused", () => {
    for (const raw of ["", "   ", "555-0123", "12345", "call me", "555O100123", "+1 555 010 0123 ext 4",
        "1+5550100123", "++15550100123", "5550100123;", "+1234567890123456", "١٢٣٤٥٦٧٨٩٠",
        "+1\n5550100123\r\nAction: Logoff", null, undefined]) {
        assert.equal(parsePhoneInput(raw), null, `refused: ${JSON.stringify(raw)}`);
    }
});
