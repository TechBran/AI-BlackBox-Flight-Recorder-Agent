package com.aiblackbox.portal.ui.telephony

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * A13-ui-ops-03: the Add Gateway form sent "http_user": null for the (default) blank
 * username, which the backend's `http_user: str = "admin"` rejects with a 422 — and the
 * dialog had already closed, discarding every typed field. The payload is now a JSON
 * object that leaves a blank username out (the box default applies), and the dialog
 * stays open with the error until the save succeeds.
 */
class GatewayPayloadTest {

    private fun payload(
        name: String = "Office TG200",
        httpUser: String = "",
        httpPassword: String = "",
        amiUser: String = "",
        amiSecret: String = "",
        ports: List<GwPort> = emptyList(),
    ) = Json.parseToJsonElement(
        buildGatewayPayload(
            name = name, ip = "192.168.1.100", model = "TG200", sipPort = 5060, httpPort = 80,
            codec = "g711", httpUser = httpUser, httpPassword = httpPassword, amiUser = amiUser,
            amiSecret = amiSecret, ports = ports, operator = "op-a",
        )
    ).jsonObject

    @Test fun `a blank username is left out so the box default applies`() {
        val p = payload()
        assertFalse("no null http_user (422)", "http_user" in p)
        assertFalse("no null ami_user", "ami_user" in p)
    }

    @Test fun `a typed username is sent`() {
        assertEquals("admin2", payload(httpUser = "admin2")["http_user"]!!.jsonPrimitive.content)
    }

    @Test fun `secrets are sent only when typed`() {
        assertFalse("http_password" in payload())
        assertEquals("s3cr\"et", payload(httpPassword = "s3cr\"et")["http_password"]!!.jsonPrimitive.content)
    }

    @Test fun `any character in a field survives as text`() {
        val name = "Line \"A\" \\ back\nslash"
        assertEquals(name, payload(name = name)["name"]!!.jsonPrimitive.content)
    }

    @Test fun `ports keep their shape`() {
        val p = payload(ports = listOf(GwPort(span = 2, slot = null, phoneNumber = "+15550001111", carrier = "x", enabled = true, operator = "op-b")))
        val port = p["ports"]!!.jsonArray.single().jsonObject
        assertEquals(2, port["span"]!!.jsonPrimitive.int)
        assertFalse("slot omitted when unknown", "slot" in port)
        assertEquals("op-b", port["operator"]!!.jsonPrimitive.content)
        assertTrue(port["enabled"]!!.jsonPrimitive.boolean)
    }

    @Test fun `the form stays open until the save succeeds`() {
        var dir: File? = File(".").absoluteFile
        var src: String? = null
        while (dir != null && src == null) {
            for (rel in listOf("src/main/java/com/aiblackbox/portal/ui/telephony/TelephonyScreen.kt",
                "app/src/main/java/com/aiblackbox/portal/ui/telephony/TelephonyScreen.kt")) {
                val f = File(dir, rel)
                if (f.isFile) src = f.readText()
            }
            dir = dir.parentFile
        }
        val s = src ?: run { fail("TelephonyScreen.kt not found"); "" }
        val onSave = s.substring(s.indexOf("onSave = { payload ->"))
        val block = onSave.substring(0, onSave.indexOf("\n            }\n"))
        assertFalse("the form is not closed before the result", Regex("^\\s*showForm = false", RegexOption.MULTILINE).containsMatchIn(block.substringBefore("{ error ->")))
        assertTrue("it closes on success only", "if (error == null)" in block)
    }
}
