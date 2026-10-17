// Unit test for the Google Workspace modal's status mapping (audit W15).
//
// GET /gmail/status/{operator} now proves the stored grant with a live Google
// call. A revoked/expired grant (invalid_grant) comes back as connected:false +
// state "needs_reauth" + reauth_url, and the modal must offer a Reconnect
// action instead of "Not connected" or — as before — "Connected". A healthy
// token keeps the existing Connected/Disconnect flow; an older server without
// `state` still renders from the boolean.
//
// gmailStatusView is PURE (no DOM, no network).
// Run: node --test Portal/modules/gmail-modal.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import { gmailStatusView, authorizeUrl } from "./gmail-modal.js";

test("a dead grant renders a Reconnect action driven by reauth_url", () => {
    const view = gmailStatusView({
        connected: false, email: "", operator: "Robin", state: "needs_reauth",
        needs_reauth: true, error: "invalid_grant",
        reauth_url: "/auth/gmail/authorize?operator=Robin",
    }, "Robin");
    assert.equal(view.cls, "gmail-needs-reauth");
    assert.match(view.text, /Reconnect needed/);
    assert.match(view.text, /invalid_grant/);
    assert.equal(view.showConnect, true);
    assert.equal(view.connectLabel, "Reconnect Google Workspace");
    assert.equal(view.connectUrl, "/auth/gmail/authorize?operator=Robin");
    assert.equal(view.showDisconnect, true, "a dead link can also be removed");
});

test("a healthy token keeps the existing connected flow", () => {
    const view = gmailStatusView({
        connected: true, email: "robin@example.com", operator: "Robin", state: "connected",
        needs_reauth: false, error: null, reauth_url: null,
    }, "Robin");
    assert.equal(view.text, "Connected as robin@example.com");
    assert.equal(view.cls, "gmail-connected");
    assert.equal(view.showConnect, false);
    assert.equal(view.showDisconnect, true);
});

test("an unverifiable token stays connected but says so", () => {
    const view = gmailStatusView({ connected: true, email: "", state: "unverified", error: "probe_failed" }, "Robin");
    assert.equal(view.cls, "gmail-connected");
    assert.match(view.text, /could not reach Google/);
    assert.equal(view.showConnect, false);
});

test("no token renders Connect", () => {
    const view = gmailStatusView({ connected: false, email: "", state: "not_connected" }, "Robin");
    assert.equal(view.text, "Not connected");
    assert.equal(view.showConnect, true);
    assert.equal(view.connectLabel, "Connect Google Workspace");
    assert.equal(view.showDisconnect, false);
});

test("an older server without `state` falls back to the boolean", () => {
    assert.equal(gmailStatusView({ connected: true, email: "a@example.com" }, "Robin").cls, "gmail-connected");
    assert.equal(gmailStatusView({ connected: false }, "Robin").text, "Not connected");
});

test("a foreign reauth_url is never followed", () => {
    const view = gmailStatusView({
        connected: false, state: "needs_reauth", error: "invalid_grant",
        reauth_url: "https://evil.example/phish",
    }, "Robin");
    assert.equal(view.connectUrl, authorizeUrl("Robin"));
});

test("the authorize URL encodes the operator", () => {
    assert.equal(authorizeUrl("Flight Recorder"), "/auth/gmail/authorize?operator=Flight%20Recorder");
});
