package com.aiblackbox.portal

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * U16 review — WIRING guard for the surfaces that act as the operator but cannot be
 * stood up on the plain JVM (an Activity, a Service, AndroidViewModels). Their rules are
 * tested where they live (LegacyPairingOperatorMigrationTest, OperatorResolutionTest,
 * SmsInboxApiTest, CliAgentScreenStateTest); this pins that each surface uses them:
 *  - PairingActivity adopts the QR's operator only into a store that has none (a
 *    re-pair never overwrites the operator already picked);
 *  - a voice session and the floating overlay's prompt and realtime paths do not start
 *    while no operator is resolved (the store's operator is "" until the box resolves it);
 *  - the SMS and Contacts screens drop a reply that lands after an operator switch,
 *    instead of showing the previous operator's threads or book.
 */
class OperatorGateWiringTest {

    private fun source(rel: String): String {
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

    private val root = "src/main/java/com/aiblackbox/portal"

    /** From `fun <name>(` to the next member at [indent] (the surrounding class's members). */
    private fun functionBody(src: String, name: String, indent: String = "    "): String {
        val start = Regex("fun $name\\(").find(src)?.range?.first ?: -1
        assertTrue("fun $name( not found", start >= 0)
        val next = Regex("\n$indent(?:(?:private|internal|override|suspend|public) )*fun ")
            .find(src, start + 1)?.range?.first ?: src.length
        return src.substring(start, next)
    }

    @Test fun `pairing adopts the QR operator only into an empty store`() {
        val src = source("$root/PairingActivity.kt")
        assertTrue(".adoptPairingOperator(" in src)
        assertFalse("a QR must never overwrite the picked operator", ".setOperator(" in src)
    }

    @Test fun `a voice session does not start without an operator`() {
        val connect = functionBody(source("$root/ui/voice/VoiceScreen.kt"), "connect")
        val gate = connect.indexOf("OperatorResolver.sendBlockedReason(")
        assertTrue(connect, gate >= 0)
        assertTrue("the gate runs before the socket opens", gate < connect.indexOf("voiceClient?.connect("))
    }

    @Test fun `the overlay neither prompts nor opens a realtime session without an operator`() {
        val src = source("$root/overlay/OverlayService.kt")
        for (name in listOf("sendPromptToAI", "connectRealtime")) {
            val body = functionBody(src, name)
            assertTrue("$name: $body", "operatorBlocks()" in body)
        }
        assertTrue("OverlayService gate uses the shared rule", "OperatorResolver.sendBlockedReason(" in functionBody(src, "operatorBlocks"))
    }

    @Test fun `SMS and Contacts drop a reply that lands after an operator switch`() {
        val sms = source("$root/ui/sms/SmsViewModel.kt")
        val contacts = source("$root/ui/contacts/ContactsViewModel.kt")
        for ((label, src) in listOf("SmsViewModel" to sms, "ContactsViewModel" to contacts)) {
            assertTrue("$label advances the generation on a switch", "generation.advance()" in src)
        }
        for (name in listOf("loadThreads", "loadMessages", "loadUnreadCount", "loadSmsPreferences")) {
            val body = functionBody(sms, name)
            assertTrue("$name tags its load", "generation.tag()" in body)
            assertTrue("$name checks the tag", "generation.isCurrent(" in body)
        }
        val load = functionBody(contacts, "loadContacts")
        assertTrue(load, "generation.tag()" in load && "generation.isCurrent(" in load)
    }
}
