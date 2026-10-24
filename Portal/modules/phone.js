/**
 * phone.js
 * Phone numbers as the box files and dials them (audit P06-comms-web-11).
 */

/**
 * The key the box files a thread under: the number's last 10 digits (all of them when
 * it has fewer). The same rule as Orchestrator/sms/message_store.py _normalize_phone,
 * so a thread the inbox opens matches the row the box lists for it.
 * @param {string} raw
 * @returns {string}
 */
export function normalizePhone(raw) {
    const digits = String(raw ?? '').replace(/[^0-9]/g, '');
    return digits.length >= 10 ? digits.slice(-10) : digits;
}

/**
 * A number typed by the operator, or null when it is not one: only digits and the
 * usual punctuation (spaces, dashes, dots, parentheses, one leading +), 3 to 15
 * digits, the gateway's own rule (ami_client.is_valid_sms_destination), so a short
 * code can be texted as well as a full number. `to` is the destination the gateway
 * takes (digits, the leading + kept: it refuses spaces and punctuation); `key` is
 * the thread key.
 * @param {string} raw
 * @returns {{key: string, to: string} | null}
 */
export function parsePhoneInput(raw) {
    const text = String(raw ?? '').trim();
    if (!/^\+?[0-9 ().-]+$/.test(text)) return null;
    const digits = text.replace(/[^0-9]/g, '');
    if (digits.length < 3 || digits.length > 15) return null;
    return { key: normalizePhone(digits), to: (text.startsWith('+') ? '+' : '') + digits };
}
