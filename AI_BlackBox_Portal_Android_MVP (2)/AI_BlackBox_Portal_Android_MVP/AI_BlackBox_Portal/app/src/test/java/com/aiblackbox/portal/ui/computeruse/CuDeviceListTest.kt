package com.aiblackbox.portal.ui.computeruse

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The CU device picker reads GET /devices/ through the typed Device DTO the Device Manager
 * uses (rank 76) instead of hand-parsing the same payload.
 */
class CuDeviceListTest {

    private val body = """{"devices":[
        {"id":"desk","name":"Desk PC","tailscale_ip":"100.64.0.2","device_type":"desktop","protocol":"vnc",
         "owner":"","adb_port":5555,"vnc_port":5900,"rdp_port":3389,"status":"online","is_primary":false,
         "default_provider":null,"metadata":{"tailscale_online":true}},
        {"id":"fold","name":"","tailscale_ip":"100.64.0.3","device_type":"android","protocol":"adb","owner":"op",
         "status":"offline"},
        {"id":"","name":"no id","protocol":"vnc"},
        {"id":"odd","adb_port":"not a number"}
    ]}"""

    @Test fun `Anthropic and OpenAI get local and VNC devices, the box first`() {
        val list = CuDevice.fromDevicesJson(body, providerIsGemini = false)
        assertEquals(listOf("blackbox", "desk"), list.map { it.id })
        assertEquals(CuDevice("desk", "Desk PC", "vnc", "online"), list[1])
    }

    @Test fun `Gemini gets every device, ADB included, a blank name shows the id`() {
        val list = CuDevice.fromDevicesJson(body, providerIsGemini = true)
        assertEquals(listOf("blackbox", "desk", "fold"), list.map { it.id })
        assertEquals(CuDevice("fold", "fold", "adb", "offline"), list[2])
    }

    @Test fun `a box that lists itself is not offered twice, junk leaves only the box`() {
        val own = """{"devices":[{"id":"blackbox","name":"This box","protocol":"local","status":"online"}]}"""
        assertEquals(listOf(CuDevice("blackbox", "This box", "local", "online")), CuDevice.fromDevicesJson(own, false))
        assertEquals(listOf(CuDevice.BLACKBOX), CuDevice.fromDevicesJson("<html>", false))
        assertEquals(listOf(CuDevice.BLACKBOX), CuDevice.fromDevicesJson("""{"detail":"down"}""", true))
    }
}
