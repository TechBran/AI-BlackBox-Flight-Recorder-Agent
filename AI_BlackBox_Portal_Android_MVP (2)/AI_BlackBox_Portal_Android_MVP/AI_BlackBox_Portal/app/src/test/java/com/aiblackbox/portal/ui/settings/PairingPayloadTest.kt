package com.aiblackbox.portal.ui.settings

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Settings > Pair New Device shows a QR another device scans. Like the web Portal's
 * pairing dialog (U16 review), the token is minted for the operator who opened it, and
 * the QR names that operator; with none resolved it names nobody, so the new device
 * resolves its operator from the box's roster instead of adopting a default. Values are
 * JSON-encoded, so a quote in an operator or origin cannot break the payload.
 */
class PairingPayloadTest {

    private fun obj(raw: String) = Json.parseToJsonElement(raw).jsonObject

    @Test fun `the token is minted for the operator opening the dialog`() {
        assertEquals("Ana Lee", obj(PairingPayload.startBody(" Ana Lee "))["operator"]!!.jsonPrimitive.content)
        // None resolved: no operator is requested, never a stand-in.
        assertNull(obj(PairingPayload.startBody(""))["operator"])
        assertNull(obj(PairingPayload.startBody("  "))["operator"])
    }

    @Test fun `the QR names that operator, JSON-encoded`() {
        val qr = obj(PairingPayload.qr(token = "tok", exp = 42L, origin = "https://box.example/ui", operator = "Ana \"A\""))
        assertEquals("pair", qr["type"]!!.jsonPrimitive.content)
        assertEquals("tok", qr["token"]!!.jsonPrimitive.content)
        assertEquals("42", qr["exp"]!!.jsonPrimitive.content)
        assertEquals("https://box.example/ui", qr["origin"]!!.jsonPrimitive.content)
        assertEquals("Ana \"A\"", qr["operator"]!!.jsonPrimitive.content)
    }

    @Test fun `with no operator or no token the QR still carries the origin`() {
        val bare = obj(PairingPayload.qr(token = null, exp = 0L, origin = "http://192.0.2.10:9091/ui", operator = ""))
        assertEquals("http://192.0.2.10:9091/ui", bare["origin"]!!.jsonPrimitive.content)
        assertNull(bare["operator"])
        assertNull(bare["token"])
    }
}
