package com.aiblackbox.portal.overlay

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Tests for the hardened `navigate` intent — the ONE-SHOT "take me there" primitive.
 *
 * A model says "navigate me to X" and the phone opens turn-by-turn: no ReAct loop, no
 * screenshots, no accessibility, one deterministic intent. Everything that could make
 * that unsafe or unreliable is a PURE decision in [IntentActions], so it is pinned here
 * on the host JVM with no device:
 *
 *  1. **Strict whitelists** — `mode` ∈ {d,b,l,w}, `avoid` ⊆ {t,h,f}. Every rejected
 *     value is enumerated below; a non-whitelisted value must be REJECTED with a
 *     reason naming the valid set, and must be impossible to emit into the URI even
 *     if the rejection were skipped.
 *  2. **The lat/lng branch** — coordinates keep a LITERAL comma
 *     (`google.navigation:q=37.4224,-122.0841`, the documented form). Form-encoding
 *     them to `%2C` — the pre-hardening behaviour — is not the documented form and
 *     Maps may not honour it.
 *  3. **The resolveActivity preflight** — [navigationLaunchPlan] decides what a
 *     missing Google Maps / a missing user-named app means, so the framework method
 *     stays a 3-line probe and every failure message is testable.
 *  4. **The ORIGIN gate** — a cloud-pushed navigate confirms; the on-device path does
 *     not. Both directions, plus the YOLO/PERMISSION semantics it must not disturb.
 *
 * Coordinates in fixtures are Google's own documentation example (37.4224,-122.0841)
 * or obvious fakes — never a real location.
 */
class NavigationIntentTest {

    // =====================================================================
    // 1a. mode whitelist — every accepted value, and every kind of rejection
    // =====================================================================

    @Test fun `every whitelisted travel mode normalizes to itself`() {
        for (m in listOf("d", "b", "l", "w")) {
            assertEquals(m, normalizedNavigationMode(m))
        }
        assertEquals(setOf("d", "b", "l", "w"), NAVIGATION_MODES)
    }

    @Test fun `travel mode is trimmed and case-insensitive`() {
        assertEquals("d", normalizedNavigationMode("D"))
        assertEquals("w", normalizedNavigationMode("  W  "))
        assertEquals("l", normalizedNavigationMode(" l"))
    }

    @Test fun `absent or blank travel mode is simply unspecified (not an error)`() {
        assertNull(normalizedNavigationMode(null))
        assertNull(normalizedNavigationMode(""))
        assertNull(normalizedNavigationMode("   "))
        // …and the pure envelope agrees: absent/blank is ALLOWED through.
        assertNull(navigationRejectionReason("dest", null, null))
        assertNull(navigationRejectionReason("dest", "", ""))
        assertNull(navigationRejectionReason("dest", "  ", "  "))
    }

    @Test fun `every non-whitelisted travel mode is rejected, not passed through`() {
        val rejected = listOf(
            "driving", "walking", "bicycling", "transit", "two-wheeler", // plausible words
            "t", "x", "0", "1",                                         // wrong letters
            "dd", "db", "dw",                                           // multi-letter
            "d,b", "d b", "d;b",                                        // separator smuggling
            "d&avoid=t", "d&mode=w", "d#x", "d?x", "d=x",               // query-param smuggling
            "%64", "\u0000d", "\u0000",                     // encoding / control-char tricks
        )
        for (m in rejected) {
            assertNull("mode \"$m\" must be REJECTED", normalizedNavigationMode(m))
            val reason = navigationRejectionReason("anywhere", m, null)
            assertTrue("mode \"$m\" must produce a rejection reason", reason != null)
            // The reason names the VALID SET, so a model can self-correct.
            assertTrue(reason!!.startsWith("invalid navigation mode"))
            for (valid in listOf("d", "b", "l", "w")) assertTrue(reason.contains(valid))
        }
        // ...and it is ONE FIXED PHRASE: no rejected argument content can ride back out
        // in a detail string (the leak-discipline rule this whole surface follows).
        assertEquals(1, rejected.map { navigationRejectionReason("anywhere", it, null) }.toSet().size)
    }

    // =====================================================================
    // 1b. avoid whitelist — subset of {t,h,f}, no duplicates
    // =====================================================================

    @Test fun `every whitelisted avoid combination normalizes to itself`() {
        for (a in listOf("t", "h", "f", "th", "tf", "hf", "thf", "fht")) {
            assertEquals(a, normalizedNavigationAvoid(a))
        }
        assertEquals(setOf("t", "h", "f"), NAVIGATION_AVOID_FLAGS)
    }

    @Test fun `avoid is trimmed and case-insensitive`() {
        assertEquals("tf", normalizedNavigationAvoid("TF"))
        assertEquals("thf", normalizedNavigationAvoid("  ThF "))
    }

    @Test fun `absent or blank avoid is simply unspecified (not an error)`() {
        assertNull(normalizedNavigationAvoid(null))
        assertNull(normalizedNavigationAvoid(""))
        assertNull(normalizedNavigationAvoid("   "))
    }

    @Test fun `every non-whitelisted avoid value is rejected, not passed through`() {
        val rejected = listOf(
            "tolls", "highways", "ferries", "none",   // plausible words
            "x", "d", "1",                            // wrong letters
            "tt", "hh", "thh", "tft",                 // duplicates
            "thfx", "thff", "txh",                    // out-of-set member
            "t,f", "t f", "t|f", "t+f",               // separator smuggling
            "t&mode=w", "tf&x=1", "t%2Cf",            // query-param smuggling
        )
        for (a in rejected) {
            assertNull("avoid \"$a\" must be REJECTED", normalizedNavigationAvoid(a))
            val reason = navigationRejectionReason("anywhere", null, a)
            assertTrue("avoid \"$a\" must produce a rejection reason", reason != null)
            assertTrue(reason!!.startsWith("invalid navigation avoid"))
            for (valid in listOf("t", "h", "f")) assertTrue(reason.contains(valid))
        }
        // ...and it is ONE FIXED PHRASE (see the mode case above).
        assertEquals(1, rejected.map { navigationRejectionReason("anywhere", null, it) }.toSet().size)
    }

    @Test fun `a missing destination is rejected before anything else`() {
        assertEquals("destination required", navigationRejectionReason(null, null, null))
        assertEquals("destination required", navigationRejectionReason("", "d", "t"))
        assertEquals("destination required", navigationRejectionReason("   ", "nonsense", "nonsense"))
    }

    @Test fun `the builder DROPS a non-whitelisted mode or avoid (second layer)`() {
        // Belt-and-suspenders: the actuator rejects first, but even a caller that skipped
        // validation can NEVER emit a non-whitelisted value into the deep link.
        assertEquals("google.navigation:q=home", navigationUri("home", "driving", "tolls"))
        assertEquals("google.navigation:q=home", navigationUri("home", "d&avoid=t", "t,f"))
        assertEquals("google.navigation:q=home&mode=d", navigationUri("home", "d", "xyz"))
        assertEquals("google.navigation:q=home&avoid=tf", navigationUri("home", "transit", "tf"))
    }

    // =====================================================================
    // 2. the URI builder: free text vs. the lat/lng branch
    // =====================================================================

    @Test fun `free-text destinations stay form-encoded (unchanged behaviour)`() {
        assertEquals("google.navigation:q=coffee+shop", navigationUri("coffee shop"))
        assertEquals("google.navigation:q=1600+Amphitheatre+Pkwy", navigationUri("1600 Amphitheatre Pkwy"))
        assertEquals("google.navigation:q=tea+%26+coffee", navigationUri("tea & coffee"))
    }

    @Test fun `a raw calendar-event address passes straight through (no geocoder needed)`() {
        // The M2 premise: google.navigation:q=<free text> lets Maps geocode it itself.
        assertEquals(
            "google.navigation:q=742+Evergreen+Terrace%2C+Springfield%2C+IL",
            navigationUri("742 Evergreen Terrace, Springfield, IL"),
        )
    }

    @Test fun `a lat,lng destination emits the comma LITERALLY`() {
        // The documented form is q=lat,lng — NOT q=lat%2Clng (what URLEncoder produced
        // before hardening, and what Maps may not honour).
        assertEquals("google.navigation:q=37.4224,-122.0841", navigationUri("37.4224,-122.0841"))
        assertFalse(navigationUri("37.4224,-122.0841").contains("%2C"))
    }

    @Test fun `lat,lng is recognized with signs, integers and interior spaces`() {
        assertEquals("google.navigation:q=37.4224,-122.0841", navigationUri(" 37.4224, -122.0841 "))
        assertEquals("google.navigation:q=-33.8688,151.2093", navigationUri("-33.8688,151.2093"))
        assertEquals("google.navigation:q=0,0", navigationUri("0,0"))
        assertEquals("google.navigation:q=10,-20", navigationUri("10,-20"))
        for (d in listOf("37.4224,-122.0841", "37.4224, -122.0841", "0,0", "-1,-1", "12,34")) {
            assertTrue("\"$d\" must be treated as coordinates", isLatLngDestination(d))
        }
    }

    @Test fun `near-miss coordinate strings are NOT the lat,lng branch`() {
        // Anything that isn't exactly two signed numbers is free text and must be encoded,
        // so a place name containing digits can never smuggle a literal comma in.
        val freeText = listOf(
            "37.4224", "37.4224,", ",-122.0841", "37.4224,-122.0841,5",
            "37.4224;-122.0841", "37.4224,-122.0841 CA", "Cafe 37.4224,-122.0841",
            "37.4224,-122.0841&mode=w", "1,2,3", "a,b", "37..4,1", "+37.4,-122.0",
        )
        for (d in freeText) {
            assertFalse("\"$d\" must NOT be treated as coordinates", isLatLngDestination(d))
            assertFalse("\"$d\" must be form-encoded", navigationUri(d).contains(","))
        }
    }

    @Test fun `mode and avoid are appended as documented query params`() {
        assertEquals("google.navigation:q=home&mode=w", navigationUri("home", "w"))
        assertEquals("google.navigation:q=home&avoid=tf", navigationUri("home", null, "tf"))
        assertEquals("google.navigation:q=home&mode=b&avoid=thf", navigationUri("home", "b", "thf"))
        assertEquals(
            "google.navigation:q=37.4224,-122.0841&mode=d&avoid=t",
            navigationUri("37.4224,-122.0841", "D", " T "),
        )
    }

    // =====================================================================
    // 3. the resolveActivity preflight (the "no Maps on this device" path)
    // =====================================================================

    @Test fun `no package argument targets Google Maps`() {
        assertEquals("com.google.android.apps.maps", navigationTargetPackage(null))
        assertEquals("com.google.android.apps.maps", navigationTargetPackage(""))
        assertEquals("com.google.android.apps.maps", navigationTargetPackage("   "))
        assertEquals("com.google.android.apps.maps", DEFAULT_NAVIGATION_PACKAGE)
    }

    @Test fun `an explicit package is honoured and any-style values mean implicit`() {
        assertEquals("com.waze", navigationTargetPackage("com.waze"))
        assertEquals("net.osmand", navigationTargetPackage("  net.osmand "))
        for (any in listOf("any", "ANY", "none", "*", "default", " Any ")) {
            assertNull("\"$any\" must mean NO package (implicit)", navigationTargetPackage(any))
        }
    }

    @Test fun `the happy path launches pinned to Google Maps`() {
        val plan = navigationLaunchPlan(null, resolvesTarget = true, resolvesImplicit = true)
        assertEquals(NavigationLaunch.Launch("com.google.android.apps.maps"), plan)
    }

    @Test fun `Maps missing but another nav app present falls back to the implicit intent`() {
        // A Waze-only phone still navigates — pinning Maps must never regress a device
        // that works today.
        val plan = navigationLaunchPlan(null, resolvesTarget = false, resolvesImplicit = true)
        assertEquals(NavigationLaunch.Launch(null), plan)
    }

    @Test fun `NO navigation app at all fails clearly instead of opaquely`() {
        // Pre-hardening this was an unpinned launch that threw ActivityNotFoundException
        // and reported "navigate failed (ActivityNotFoundException)".
        assertEquals(
            NavigationLaunch.Fail("no navigation app installed"),
            navigationLaunchPlan(null, resolvesTarget = false, resolvesImplicit = false),
        )
        assertEquals(
            NavigationLaunch.Fail("no navigation app installed"),
            navigationLaunchPlan("any", resolvesTarget = false, resolvesImplicit = false),
        )
    }

    @Test fun `an explicitly requested app that is missing fails BY NAME and is never retargeted`() {
        // "navigate with Waze" must not silently open Maps instead — even though the
        // implicit intent WOULD have resolved.
        assertEquals(
            NavigationLaunch.Fail("navigation app not installed: com.waze"),
            navigationLaunchPlan("com.waze", resolvesTarget = false, resolvesImplicit = true),
        )
    }

    @Test fun `an explicitly requested app that IS installed launches pinned to it`() {
        assertEquals(
            NavigationLaunch.Launch("com.waze"),
            navigationLaunchPlan("com.waze", resolvesTarget = true, resolvesImplicit = false),
        )
    }

    @Test fun `an explicit any launches implicitly when something can handle it`() {
        assertEquals(
            NavigationLaunch.Launch(null),
            navigationLaunchPlan("any", resolvesTarget = true, resolvesImplicit = true),
        )
    }

    // =====================================================================
    // 4. the ORIGIN gate — both directions
    // =====================================================================

    /** Records the confirm prompts it was shown; [answer] is the user's decision. */
    private class RecordingConfirm(private val answer: Boolean) : ConfirmUi {
        val asked = mutableListOf<String>()
        override suspend fun confirm(description: String): Boolean {
            asked.add(description)
            return answer
        }
    }

    /**
     * The actuator's net gate outcome for `navigate`, composed exactly as
     * [IntentActuator] composes it: `if (shouldConfirmIntent(mode, name, origin)) confirm(...)`.
     * true == the intent would fire.
     */
    private fun navWouldFire(
        mode: AutonomyMode,
        origin: ActionOrigin,
        confirm: RecordingConfirm,
        destination: String = "1600 Amphitheatre Pkwy",
    ): Boolean = runBlocking {
        if (!shouldConfirmIntent(mode, "navigate", origin)) true
        else confirm.confirm(describeIntent("navigate", destination))
    }

    @Test fun `LOCAL navigate is UNGATED — the device-proven on-device path is unchanged`() {
        for (mode in listOf(AutonomyMode.PERMISSION, AutonomyMode.YOLO)) {
            val confirm = RecordingConfirm(answer = false)   // would DENY if it were ever asked
            assertTrue("on-device navigate must fire without a prompt", navWouldFire(mode, ActionOrigin.LOCAL, confirm))
            assertTrue("on-device navigate must not prompt at all", confirm.asked.isEmpty())
        }
    }

    @Test fun `REMOTE navigate CONFIRMS and a decline launches nothing`() {
        val confirm = RecordingConfirm(answer = false)
        assertFalse(
            "a cloud-pushed navigate must not fire when declined",
            navWouldFire(AutonomyMode.PERMISSION, ActionOrigin.REMOTE, confirm),
        )
        assertEquals(1, confirm.asked.size)
        // The prompt must say WHERE it is about to send you — a confirm that hides the
        // destination is not consent.
        assertEquals("Start navigation to \"1600 Amphitheatre Pkwy\"", confirm.asked[0])
    }

    @Test fun `REMOTE navigate fires once the user approves`() {
        val confirm = RecordingConfirm(answer = true)
        assertTrue(navWouldFire(AutonomyMode.PERMISSION, ActionOrigin.REMOTE, confirm))
        assertEquals(1, confirm.asked.size)
    }

    @Test fun `the default origin is LOCAL, so every pre-existing call-site is unchanged`() {
        assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate"))
        assertEquals(
            shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate", ActionOrigin.LOCAL),
            shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate"),
        )
    }

    @Test fun `navigate is NOT high-consequence wholesale`() {
        // Adding it to HIGH_CONSEQUENCE_INTENTS would gate the on-device path too — the
        // exact regression the origin gate exists to avoid.
        assertFalse(isHighConsequenceIntent("navigate"))
        assertTrue(isRemoteGatedIntent("navigate"))
        assertTrue(isRemoteGatedIntent("  Navigate  "))
    }

    @Test fun `REMOTE does not newly gate any other benign intent`() {
        for (name in listOf("show_map", "open_url", "dial", "flashlight_on", "set_timer", "play_media")) {
            assertFalse("$name must stay ungated", isRemoteGatedIntent(name))
            assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, name, ActionOrigin.REMOTE))
        }
    }

    @Test fun `YOLO semantics are untouched — nothing gates, including a remote navigate`() {
        val confirm = RecordingConfirm(answer = false)
        assertTrue(navWouldFire(AutonomyMode.YOLO, ActionOrigin.REMOTE, confirm))
        assertTrue(confirm.asked.isEmpty())
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "navigate", ActionOrigin.REMOTE))
        // …and the high-consequence intents keep their existing YOLO behaviour too.
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "send_email", ActionOrigin.REMOTE))
    }

    @Test fun `high-consequence intents still gate regardless of origin`() {
        for (origin in listOf(ActionOrigin.LOCAL, ActionOrigin.REMOTE)) {
            assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_email", origin))
            assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_sms", origin))
            assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_intent", origin))
        }
    }

    @Test fun `the navigate confirm string is a fixed function of the destination`() {
        assertEquals("Start navigation to \"742 Evergreen Terrace\"", describeIntent("navigate", "742 Evergreen Terrace"))
        assertEquals("Start navigation", describeIntent("navigate", null))
        assertEquals("Start navigation", describeIntent("navigate", "   "))
    }
}
