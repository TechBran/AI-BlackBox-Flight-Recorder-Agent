package com.aiblackbox.portal

import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit X02/X04 (rank 29): the box answers an unpaired device with "open the BlackBox app on
 * that device once". That only works if EVERY launcher UI starts the control listener, whose
 * start registers the pairing secret ([NotificationListenerFgs]): the native UI did, the
 * WebView UI ([PortalActivity], `use_native_ui=false`) did not. Reads the sources as text.
 */
class ListenerStartWiringTest {

    private fun moduleSource(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    @Test fun both_launcher_uis_start_the_listener_that_registers_the_pairing_secret() {
        for (activity in listOf("NativeMainActivity", "PortalActivity")) {
            val src = moduleSource("src/main/java/com/aiblackbox/portal/$activity.kt")
            assertTrue("$activity must start NotificationListenerFgs",
                Regex("""NotificationListenerFgs\.start\(this\)""").containsMatchIn(src))
        }
    }

    @Test fun the_listener_start_registers_the_pairing_secret() {
        val src = moduleSource("src/main/java/com/aiblackbox/portal/NotificationListenerFgs.kt")
        assertTrue(src.contains("registerPairingSecret()"))
    }
}
