package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * The privacy policy the app bundles (`res/raw/health_privacy.md`, shown by
 * WellnessRationaleActivity and the setup flow's "Your privacy" step) is byte for byte the one
 * the box serves at `GET /wellness/privacy-policy` (`Orchestrator/wellness/policy/health_privacy.md`).
 *
 * The two sides agree through ONE literal: `src/test/resources/wellness/policy_sha256.txt`. This
 * test holds the bundled copy to it; the box's Python contract test reads the same file and
 * holds the box's copy to it. Changing the policy means changing both copies and this literal
 * in one commit (the version the box reports, catalog.POLICY_VERSION, is bumped separately).
 */
class PolicyHashTest {

    private fun expected(): String {
        val url = javaClass.classLoader!!.getResource("wellness/policy_sha256.txt")
            ?: throw AssertionError("wellness/policy_sha256.txt is missing from the test resources")
        return url.readText().trim()
    }

    /** [relative] under the first ancestor of the test's working directory that has it. */
    private fun above(relative: String): File? {
        var dir: File? = File("").absoluteFile
        repeat(10) {
            val d = dir ?: return null
            val f = File(d, relative)
            if (f.exists()) return f
            dir = d.parentFile
        }
        return null
    }

    @Test
    fun `the shared literal is one lowercase sha-256`() {
        assertTrue("policy_sha256.txt holds 64 lowercase hex characters", Regex("[0-9a-f]{64}").matches(expected()))
    }

    @Test
    fun `the bundled policy hashes to the shared literal`() {
        val bundled = above("src/main/res/raw/health_privacy.md")
            ?: throw AssertionError("res/raw/health_privacy.md not found above ${File("").absolutePath}")
        assertEquals("res/raw/health_privacy.md changed: copy the box's policy and update policy_sha256.txt",
            expected(), OriginSigner.sha256Hex(bundled.readBytes()))
    }

    @Test
    fun `the bundled policy is the box's own copy when the repo is checked out whole`() {
        val box = above("Orchestrator/wellness/policy/health_privacy.md") ?: return // the app built on its own
        val bundled = above("src/main/res/raw/health_privacy.md")!!
        assertEquals(OriginSigner.sha256Hex(box.readBytes()), OriginSigner.sha256Hex(bundled.readBytes()))
    }
}
