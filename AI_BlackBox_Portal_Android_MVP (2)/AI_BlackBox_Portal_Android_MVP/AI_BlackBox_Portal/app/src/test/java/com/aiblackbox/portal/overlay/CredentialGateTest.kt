package com.aiblackbox.portal.overlay

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the PURE core of the CREDENTIAL HANDOFF (Phase 4, Task 4.7).
 *
 * Task 4.7 turns the 4.3 bare password refusal into a graceful handoff: when the
 * model tries to fill a password field, its attempted text is DISCARDED, the user
 * is asked to type the secret themselves, then the agent resumes. The password
 * reaches the model in NEITHER direction.
 *
 * The whole decision surface is the pure [credentialDecision] plus the
 * [CredentialHandoff] seam. Those are tested exhaustively here. The live
 * [Actuators.type] that wires them together is framework-coupled (it resolves a
 * real [android.view.accessibility.AccessibilityNodeInfo] and calls
 * `ACTION_SET_TEXT` through the live accessibility service — `Stub!` throws in the
 * unit-test android.jar), so its password branch is mirrored here by
 * [typePasswordBranch] — the EXACT routing [Actuators.type] runs for a password
 * target — and is end-to-end device-verified in Task 4.8.
 *
 * The dangerous invariants asserted: a password type takes the HANDOFF branch (not
 * the type branch); the model's attempted text is DISCARDED (never typed, never in
 * the handoff description); the handoff fails SAFE (declines) when un-wired.
 */
class CredentialGateTest {

    // ---- credentialDecision: all three branches ---------------------------

    @Test
    fun `non-password target types normally`() {
        assertEquals(
            CredentialAction.TYPE_NORMAL,
            credentialDecision(isPasswordTarget = false, hasSavedCredential = false),
        )
        // Even if a saved credential somehow existed, a non-password field is normal.
        assertEquals(
            CredentialAction.TYPE_NORMAL,
            credentialDecision(isPasswordTarget = false, hasSavedCredential = true),
        )
    }

    @Test
    fun `password target with a saved credential routes to system autofill`() {
        // DEFERRED branch (Credential Manager picker) — kept + tested so the
        // decision is correct the day autofill lands.
        assertEquals(
            CredentialAction.SYSTEM_AUTOFILL,
            credentialDecision(isPasswordTarget = true, hasSavedCredential = true),
        )
    }

    @Test
    fun `password target with no saved credential routes to user handoff`() {
        // The v1 call-site always passes hasSavedCredential = false, so this is the
        // live path: a password field -> hand entry back to the user.
        assertEquals(
            CredentialAction.USER_HANDOFF,
            credentialDecision(isPasswordTarget = true, hasSavedCredential = false),
        )
    }

    // ---- the field description is GENERIC, never a secret ------------------

    @Test
    fun `the credential field description is generic and carries no secret`() {
        // The handoff prompt is fed only this fixed, content-free string — never the
        // model's attempted text.
        assertEquals("the password field", CREDENTIAL_FIELD_DESCRIPTION)
        assertFalse(CREDENTIAL_FIELD_DESCRIPTION.contains("hunter2"))
    }

    // ---- the password routing the live Actuators.type runs -----------------

    /**
     * A fake [CredentialHandoff] that records what it was shown + how often, and
     * returns a fixed answer. The recorded description lets us assert the model's
     * attempted text never reaches the prompt.
     */
    private class FakeHandoff(private val answer: Boolean) : CredentialHandoff {
        var calls = 0
        var lastDescription: String? = null
        override suspend fun requestUserEntry(fieldDescription: String): Boolean {
            calls++
            lastDescription = fieldDescription
            return answer
        }
    }

    /** Records every text value ACTION_SET_TEXT would have received (the type path). */
    private class TypeSpy {
        val typedTexts = mutableListOf<String>()
        fun type(text: String) { typedTexts.add(text) }
    }

    /**
     * The EXACT branch [Actuators.type] runs once it knows whether the target is a
     * password field — extracted here so it can be JVM-tested without the live
     * accessibility service. The live wiring (node resolution + ACTION_SET_TEXT)
     * around it is device-verified in 4.8.
     *
     * Mirrors [Actuators.type]: a password target (USER_HANDOFF / SYSTEM_AUTOFILL)
     * DISCARDS [text] and calls the handoff; a non-password target (TYPE_NORMAL)
     * passes [text] to the type sink.
     */
    private suspend fun typePasswordBranch(
        isPasswordTarget: Boolean,
        text: String,
        handoff: CredentialHandoff,
        typeSpy: TypeSpy,
    ): ActuatorResult {
        return when (credentialDecision(isPasswordTarget, hasSavedCredential = false)) {
            CredentialAction.USER_HANDOFF, CredentialAction.SYSTEM_AUTOFILL -> {
                // `text` is NEVER read/forwarded here — discarded.
                val entered = handoff.requestUserEntry(CREDENTIAL_FIELD_DESCRIPTION)
                if (entered) ActuatorResult(true, "user entered their credential")
                else ActuatorResult(false, "user declined credential entry")
            }
            CredentialAction.TYPE_NORMAL -> {
                typeSpy.type(text)
                ActuatorResult(true, "set text on node")
            }
        }
    }

    @Test
    fun `a password type takes the handoff branch, never the type branch`() = runBlocking {
        val handoff = FakeHandoff(answer = true)
        val spy = TypeSpy()
        val secret = "hunter2"

        val result = typePasswordBranch(
            isPasswordTarget = true,
            text = secret,
            handoff = handoff,
            typeSpy = spy,
        )

        // The HANDOFF was taken, NOT the type path.
        assertEquals("the handoff must be invoked for a password target", 1, handoff.calls)
        assertTrue("nothing must be typed for a password target", spy.typedTexts.isEmpty())
        // The model's attempted text is DISCARDED — never typed, never in the prompt.
        assertEquals("the handoff prompt is the generic field description", "the password field", handoff.lastDescription)
        assertFalse("the secret must never reach the handoff prompt", handoff.lastDescription!!.contains(secret))
        // Success result — the model continues (e.g. taps Sign In), never learns the secret.
        assertTrue(result.success)
        assertEquals("user entered their credential", result.detail)
        assertFalse("the secret must never appear in the result detail", result.detail.contains(secret))
    }

    @Test
    fun `a declined credential handoff returns the declined result, still no typing`() = runBlocking {
        val handoff = FakeHandoff(answer = false)
        val spy = TypeSpy()

        val result = typePasswordBranch(
            isPasswordTarget = true,
            text = "hunter2",
            handoff = handoff,
            typeSpy = spy,
        )

        assertEquals(1, handoff.calls)
        assertTrue("a declined handoff still types nothing", spy.typedTexts.isEmpty())
        assertFalse(result.success)
        assertEquals("user declined credential entry", result.detail)
    }

    @Test
    fun `a non-password type goes through the normal type path with the model text`() = runBlocking {
        val handoff = FakeHandoff(answer = true)
        val spy = TypeSpy()

        val result = typePasswordBranch(
            isPasswordTarget = false,
            text = "hello world",
            handoff = handoff,
            typeSpy = spy,
        )

        // The normal type path: text IS typed, the handoff is NOT consulted.
        assertEquals("the handoff must NOT be consulted for a non-password field", 0, handoff.calls)
        assertEquals(listOf("hello world"), spy.typedTexts)
        assertTrue(result.success)
        assertNull("no handoff prompt for a non-password field", handoff.lastDescription)
    }

    // ---- the default handoff fails SAFE (un-wired -> declines) --------------

    @Test
    fun `the default credential handoff declines (fails safe when un-wired)`() = runBlocking {
        // An un-wired Actuators uses AutoDeclineCredentialHandoff: a password entry
        // can NEVER silently proceed.
        val declined = AutoDeclineCredentialHandoff.requestUserEntry(CREDENTIAL_FIELD_DESCRIPTION)
        assertFalse("an un-wired handoff must decline", declined)

        // And routed through the branch, it yields the declined result with no typing.
        val spy = TypeSpy()
        val result = typePasswordBranch(
            isPasswordTarget = true,
            text = "hunter2",
            handoff = AutoDeclineCredentialHandoff,
            typeSpy = spy,
        )
        assertFalse(result.success)
        assertEquals("user declined credential entry", result.detail)
        assertTrue(spy.typedTexts.isEmpty())
    }
}
