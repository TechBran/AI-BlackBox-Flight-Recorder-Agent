package com.aiblackbox.portal.data.remote

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * Audit X02/X04 (rank 29): the phone mints the 256-bit secret its control listener requires,
 * keeps it in private no-backup storage across restarts, and registers it with the box.
 */
class RemoteControlPairingTest {

    @get:Rule val tmp = TemporaryFolder()

    @Test fun a_new_secret_is_256_bits_of_lowercase_hex() {
        val s = RemoteControlPairing.newSecret()
        assertEquals(64, s.length)
        assertTrue(s, s.all { it in '0'..'9' || it in 'a'..'f' })
        assertTrue(RemoteControlPairing.isWellFormed(s))
        assertNotEquals(s, RemoteControlPairing.newSecret())
    }

    @Test fun well_formedness() {
        assertFalse(RemoteControlPairing.isWellFormed(""))
        assertFalse(RemoteControlPairing.isWellFormed("ab".repeat(31)))
        assertFalse(RemoteControlPairing.isWellFormed("AB".repeat(32)))
        assertFalse(RemoteControlPairing.isWellFormed("zz".repeat(32)))
        assertFalse(RemoteControlPairing.isWellFormed("ab".repeat(32) + "\n"))
    }

    @Test fun the_secret_is_minted_once_and_survives_a_restart() {
        val file = File(tmp.root, "pairing/secret")
        val first = RemoteControlPairing.loadOrCreate(file)
        assertTrue(file.exists())
        assertEquals(first, file.readText().trim())
        assertEquals(first, RemoteControlPairing.loadOrCreate(file))
    }

    @Test fun a_corrupt_secret_file_is_replaced_with_a_fresh_secret() {
        val file = File(tmp.root, "secret")
        file.writeText("not-a-secret")
        val s = RemoteControlPairing.loadOrCreate(file)
        assertTrue(RemoteControlPairing.isWellFormed(s))
        assertEquals(s, file.readText().trim())
    }

    @Test fun no_temp_file_is_left_behind() {
        val dir = tmp.newFolder("p")
        RemoteControlPairing.loadOrCreate(File(dir, "secret"))
        assertEquals(listOf("secret"), dir.list()!!.toList())
    }

    @Test fun the_registration_body_carries_identity_address_and_secret() {
        val body = RemoteControlPairing.registrationBody(
            deviceId = "android-id", tailnetIp = "100.88.0.7", secret = "ab".repeat(32),
            displayName = "Pixel \"Test\"")
        val obj = Json.parseToJsonElement(body).jsonObject
        assertEquals("android-id", obj["device_id"]!!.jsonPrimitive.content)
        assertEquals("100.88.0.7", obj["tailnet_ip"]!!.jsonPrimitive.content)
        assertEquals("ab".repeat(32), obj["secret"]!!.jsonPrimitive.content)
        assertEquals("Pixel \"Test\"", obj["display_name"]!!.jsonPrimitive.content)
        assertEquals("android", obj["device_kind"]!!.jsonPrimitive.content)
        // An older box ignores it; a current one keeps whichever candidate is a tailnet node.
        assertEquals(listOf("100.88.0.7"),
            obj["tailnet_ips"]!!.jsonArray.map { it.jsonPrimitive.content })
    }

    @Test fun the_registration_body_offers_every_tailnet_candidate() {
        val body = RemoteControlPairing.registrationBody(
            deviceId = "android-id", tailnetIp = "100.88.0.7", secret = "ab".repeat(32),
            displayName = "Pixel", tailnetIps = listOf("100.88.0.7", "100.70.3.9"))
        val obj = Json.parseToJsonElement(body).jsonObject
        assertEquals("100.88.0.7", obj["tailnet_ip"]!!.jsonPrimitive.content)
        assertEquals(listOf("100.88.0.7", "100.70.3.9"),
            obj["tailnet_ips"]!!.jsonArray.map { it.jsonPrimitive.content })
    }

    @Test fun registration_retries_back_off_and_are_capped() {
        assertEquals(30_000L, pairingRetryDelayMs(0))
        assertEquals(60_000L, pairingRetryDelayMs(1))
        assertEquals(120_000L, pairingRetryDelayMs(2))
        assertEquals(15 * 60_000L, pairingRetryDelayMs(10))
        assertEquals(15 * 60_000L, pairingRetryDelayMs(1000))
    }
}
