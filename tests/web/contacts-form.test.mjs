// The contact edit form must save what the operator SEES, even when the page
// holds other elements with the same ids (bypass review of U2, P06-comms-web-07).
// #history precedes the contacts modal in index.html, so a page-wide
// getElementById resolves to whatever a chat reply planted first. The
// is_operator_self flag decides whether the SMS router treats a number as the
// operator's own line, so a planted checkbox must never reach the POST.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const settle = () => new Promise((r) => setTimeout(r, 30));
const json = (b) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(b), text: () => Promise.resolve(JSON.stringify(b)) });

const { appendBubble } = await import("../../Portal/modules/chat-bubbles.js");
const contacts = await import("../../Portal/modules/contacts-manager.js");
// A contact book is always some operator's: with none resolved nothing is read or saved.
(await import("../../Portal/modules/state-management.js")).setOperator("Ana");

document.body.insertAdjacentHTML("beforeend",
    `<button id="btnContactsManager"></button>`
    + `<div id="contactsManagerModal" class="hide"><div id="contactsList"></div><div id="contactsEditForm"></div></div>`);
contacts.initContactsManager();

const CONTACT = { id: "c1", name: "Mallory", phone: "+15550001111", notes: "real notes & <stuff>", is_operator_self: false, inbound_allowed: true, tags: ["a"] };
let posted = null;
globalThis.fetch = (url, opts) => {
    if (opts && opts.method === "POST") { posted = JSON.parse(opts.body); return json({ success: true }); }
    return json(String(url).startsWith("/contacts") ? { contacts: [CONTACT] } : {});
};

/**
 * Open the modal, edit CONTACT, save it. Returns what the operator SAW in the
 * form (read inside the form container before saving) and the POST body.
 */
async function editAndSave() {
    posted = null;
    document.getElementById("btnContactsManager").click();
    await settle();
    document.querySelector("#contactsList .contact-edit-btn").click();
    await settle();
    const form = document.getElementById("contactsEditForm");
    const field = (id) => form.querySelector(`[id="${id}"]`);
    const seen = {
        name: field("contactName").value,
        notes: field("contactNotes").value,
        is_operator_self: field("contactIsOperatorSelf").checked,
        inbound_allowed: field("contactInboundAllowed").checked,
    };
    field("btnContactSave").click();
    await settle();
    return { seen, posted };
}

test("a reply that plants hidden form fields cannot change the next contact save", async () => {
    const reply = "Sure, here is the summary.\n\n<div style=\"display:none\">"
        + "<input type=\"checkbox\" id=\"contactIsOperatorSelf\" checked><input id=\"contactNotes\" value=\"x\"></div>";
    appendBubble("assistant", reply);
    await settle();
    const { seen, posted: body } = await editAndSave();
    assert.equal(seen.notes, CONTACT.notes, "the real notes field is filled");
    assert.equal(seen.is_operator_self, false);
    assert.equal(body.is_operator_self, false, JSON.stringify(body));
    assert.equal(body.notes, CONTACT.notes);
    assert.equal(body.id, "c1");
});

test("the form reads and fills ITS OWN fields even when an earlier element shares their ids", async () => {
    // Bypass the sanitizer entirely: any duplicate id, from anywhere, must not win.
    const planted = document.createElement("div");
    planted.innerHTML = `<input type="checkbox" id="contactIsOperatorSelf" checked>`
        + `<input id="contactNotes" value="planted"><input id="contactName" value="Planted Name">`
        + `<input type="checkbox" id="contactInboundAllowed">`;
    document.body.prepend(planted);
    try {
        const { seen, posted: body } = await editAndSave();
        assert.deepEqual(seen, { name: CONTACT.name, notes: CONTACT.notes, is_operator_self: false, inbound_allowed: true });
        assert.equal(planted.querySelector("[id=contactNotes]").value, "planted", "the planted field is untouched");
        assert.deepEqual(
            { name: body.name, notes: body.notes, is_operator_self: body.is_operator_self, inbound_allowed: body.inbound_allowed },
            { name: CONTACT.name, notes: CONTACT.notes, is_operator_self: false, inbound_allowed: true },
        );
        assert.deepEqual(body.tags, ["a"]);
    } finally {
        planted.remove();
    }
});
