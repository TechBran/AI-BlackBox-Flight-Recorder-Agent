package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.remote.classifyActuatorError
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M3) The navigation DELIVERY layer — the half of `navigate` that decides how the
 * request reaches the screen, and what we are allowed to claim about it.
 *
 * ## The measured defect these tests exist for
 * With the app BACKGROUNDED on a real Galaxy Z Fold 6, a remote navigate returned
 * `{"msg":"action_result","success":true,"detail":"started navigation"}` and Google Maps
 * NEVER OPENED. Android's Background Activity Launch restriction discarded the launch, and
 * `startActivity` does not throw, so the actuator could not tell it had failed.
 *
 * The false success is the dangerous part: a 07:30 cron would be told "navigation started",
 * the model would relay "I have started navigation to your job site", and the operator would
 * drive off with no directions and only discover it when they needed them.
 *
 * Every decision below is therefore PURE and pinned here on the host JVM:
 *  1. the foreground/background decision table,
 *  2. that a backgrounded direct launch produces a DISTINCT, classifiable failure,
 *  3. dedup, so a retrying cron cannot stack prompts,
 *  4. the delivery-mode parse INCLUDING the invalid case (fail loudly, never guess),
 *  5. that a navigation notification ALWAYS names the destination — a prompt that hides
 *     where it is about to send you is not consent.
 *
 * Destinations in fixtures are Google's documentation coordinates (37.4224,-122.0841) or
 * obvious fakes — never a real location.
 */
class NavigationDeliveryTest {

    @After fun resetForegroundFlag() = AppForegroundState.setForegroundForTest(false)

    // =====================================================================
    // 1. The foreground / background decision table
    // =====================================================================

    @Test fun `auto opens directly only when the app is in the foreground`() {
        assertEquals(
            NavigationDelivery.DIRECT,
            navigationDeliveryPlan(DeliveryMode.AUTO, appForeground = true),
        )
        assertEquals(
            NavigationDelivery.NOTIFY,
            navigationDeliveryPlan(DeliveryMode.AUTO, appForeground = false),
        )
    }

    @Test fun `direct is never silently upgraded to a notification when backgrounded`() {
        // The caller asked for a mechanism. Substituting another one silently is the same
        // class of dishonesty as reporting a success that did not happen — so `direct`
        // stays DIRECT and the actuator refuses it (see the background_launch_blocked test).
        assertEquals(
            NavigationDelivery.DIRECT,
            navigationDeliveryPlan(DeliveryMode.DIRECT, appForeground = false),
        )
        assertEquals(
            NavigationDelivery.DIRECT,
            navigationDeliveryPlan(DeliveryMode.DIRECT, appForeground = true),
        )
    }

    @Test fun `notify always notifies, even with the app in front`() {
        assertEquals(
            NavigationDelivery.NOTIFY,
            navigationDeliveryPlan(DeliveryMode.NOTIFY, appForeground = true),
        )
        assertEquals(
            NavigationDelivery.NOTIFY,
            navigationDeliveryPlan(DeliveryMode.NOTIFY, appForeground = false),
        )
    }

    @Test fun `the full decision table has no other outcomes`() {
        val table = DeliveryMode.entries.flatMap { m ->
            listOf(true, false).map { fg -> Triple(m, fg, navigationDeliveryPlan(m, fg)) }
        }
        assertEquals(6, table.size)
        assertEquals(
            listOf(
                Triple(DeliveryMode.AUTO, true, NavigationDelivery.DIRECT),
                Triple(DeliveryMode.AUTO, false, NavigationDelivery.NOTIFY),
                Triple(DeliveryMode.DIRECT, true, NavigationDelivery.DIRECT),
                Triple(DeliveryMode.DIRECT, false, NavigationDelivery.DIRECT),
                Triple(DeliveryMode.NOTIFY, true, NavigationDelivery.NOTIFY),
                Triple(DeliveryMode.NOTIFY, false, NavigationDelivery.NOTIFY),
            ),
            table,
        )
    }

    // =====================================================================
    // 2. The foreground flag itself — fail CLOSED
    // =====================================================================

    @Test fun `foreground state starts false so an unwired process never claims a launch`() {
        // Fail-closed is the whole point: an un-wired / never-started process must route to
        // a notification rather than fire a launch the OS will silently discard.
        AppForegroundState.setForegroundForTest(false)
        assertFalse(AppForegroundState.isForeground())
        assertEquals(
            NavigationDelivery.NOTIFY,
            navigationDeliveryPlan(DeliveryMode.AUTO, AppForegroundState.isForeground()),
        )
    }

    @Test fun `foreground state tracks the process lifecycle transitions`() {
        AppForegroundState.onForeground()
        assertTrue(AppForegroundState.isForeground())
        AppForegroundState.onBackground()
        assertFalse(AppForegroundState.isForeground())
        AppForegroundState.onForeground()
        assertTrue(AppForegroundState.isForeground())
    }

    // =====================================================================
    // 3. THE HONEST FAILURE — the whole reason for M3
    //    [navigationAction] is the function IntentActuator actually calls, so these
    //    assertions pin production behaviour, not a description of it.
    // =====================================================================

    @Test fun `a backgrounded direct navigate REFUSES instead of reporting success`() {
        // The measured defect: this used to return success:true / "started navigation" while
        // Maps never opened. A cron would relay that lie to the operator.
        val action = navigationAction(DeliveryMode.DIRECT, appForeground = false)
        assertTrue(action is NavigationAction.Refuse)
        assertEquals(BACKGROUND_LAUNCH_BLOCKED_DETAIL, (action as NavigationAction.Refuse).detail)
    }

    @Test fun `a backgrounded auto navigate delivers a prompt rather than refusing`() {
        assertEquals(NavigationAction.Notify, navigationAction(DeliveryMode.AUTO, appForeground = false))
    }

    @Test fun `a foreground navigate still launches directly — M2 is not regressed`() {
        // The shipped, device-proven path: app in front, auto or direct, fire the intent.
        assertEquals(NavigationAction.Launch, navigationAction(DeliveryMode.AUTO, appForeground = true))
        assertEquals(NavigationAction.Launch, navigationAction(DeliveryMode.DIRECT, appForeground = true))
    }

    @Test fun `an explicit notify never launches, foreground or not`() {
        assertEquals(NavigationAction.Notify, navigationAction(DeliveryMode.NOTIFY, appForeground = true))
        assertEquals(NavigationAction.Notify, navigationAction(DeliveryMode.NOTIFY, appForeground = false))
    }

    @Test fun `no combination of mode and state can launch from the background`() {
        // The invariant that closes the defect: Launch is unreachable while backgrounded.
        for (m in DeliveryMode.entries) {
            assertNotEquals(
                "must not launch in the background: $m",
                NavigationAction.Launch,
                navigationAction(m, appForeground = false),
            )
        }
    }

    @Test fun `backgrounded direct launch classifies as background_launch_blocked`() {
        val kind = classifyActuatorError(BACKGROUND_LAUNCH_BLOCKED_DETAIL)
        assertEquals("background_launch_blocked", kind)
        assertEquals(BACKGROUND_LAUNCH_BLOCKED, kind)
    }

    @Test fun `the blocked detail is not classified as a generic dispatch failure`() {
        // Before M3 this failure did not exist at all (it was reported as SUCCESS). It must
        // never collapse into dispatch_failed either — the box has to be able to tell the
        // operator specifically that the phone must be open, or to retry via notification.
        assertNotEquals("dispatch_failed", classifyActuatorError(BACKGROUND_LAUNCH_BLOCKED_DETAIL))
        assertNotEquals("node_not_found", classifyActuatorError(BACKGROUND_LAUNCH_BLOCKED_DETAIL))
        assertNotEquals("invalid_argument", classifyActuatorError(BACKGROUND_LAUNCH_BLOCKED_DETAIL))
    }

    @Test fun `the blocked detail explains the failure to a human and names the way out`() {
        val d = BACKGROUND_LAUNCH_BLOCKED_DETAIL
        assertTrue(d.startsWith(BACKGROUND_LAUNCH_BLOCKED))
        // States what did NOT happen — the opposite of "started navigation".
        assertTrue(d.contains("NOTHING opened"))
        // Names both remedies.
        assertTrue(d.contains("foreground"))
        assertTrue(d.contains("delivery=notify"))
    }

    @Test fun `an undelivered notification is its own honest failure, never a success`() {
        assertEquals(
            "notification_permission_missing",
            classifyActuatorError(NOTIFICATION_PERMISSION_MISSING_DETAIL),
        )
        assertEquals(
            "notification_delivery_failed",
            classifyActuatorError(NOTIFICATION_DELIVERY_FAILED_DETAIL),
        )
        assertTrue(NOTIFICATION_PERMISSION_MISSING_DETAIL.contains("NOT delivered"))
    }

    @Test fun `a posted prompt does not claim navigation has started`() {
        // The model relays this detail to the operator. "started navigation" would be a lie —
        // nothing has started; a prompt is waiting for a tap.
        assertFalse(NAVIGATION_NOTIFY_POSTED_DETAIL.contains("started navigation"))
        assertTrue(NAVIGATION_NOTIFY_POSTED_DETAIL.contains("waiting for the user to tap"))
    }

    @Test fun `no failure detail leaks the destination`() {
        // Leak discipline: actuator details are fixed phrases or a package name, never args.
        for (d in listOf(
            BACKGROUND_LAUNCH_BLOCKED_DETAIL,
            NOTIFICATION_PERMISSION_MISSING_DETAIL,
            NOTIFICATION_DELIVERY_FAILED_DETAIL,
            NAVIGATION_NOTIFY_POSTED_DETAIL,
        )) {
            assertFalse(d.contains("37.4224"))
            assertFalse(d.lowercase().contains("elm street"))
        }
    }

    // =====================================================================
    // 4. Delivery-mode parse — including the INVALID case (fail loudly)
    // =====================================================================

    @Test fun `every whitelisted delivery mode parses to itself`() {
        assertEquals(DeliveryMode.AUTO, parsedDeliveryMode("auto"))
        assertEquals(DeliveryMode.DIRECT, parsedDeliveryMode("direct"))
        assertEquals(DeliveryMode.NOTIFY, parsedDeliveryMode("notify"))
        assertEquals(setOf("auto", "direct", "notify"), NAVIGATION_DELIVERY_MODES)
    }

    @Test fun `delivery mode is trimmed and case-insensitive`() {
        assertEquals(DeliveryMode.NOTIFY, parsedDeliveryMode("NOTIFY"))
        assertEquals(DeliveryMode.DIRECT, parsedDeliveryMode("  Direct  "))
        assertEquals(DeliveryMode.AUTO, parsedDeliveryMode(" AuTo"))
    }

    @Test fun `absent or blank delivery defaults to auto`() {
        assertEquals(DeliveryMode.AUTO, parsedDeliveryMode(null))
        assertEquals(DeliveryMode.AUTO, parsedDeliveryMode(""))
        assertEquals(DeliveryMode.AUTO, parsedDeliveryMode("   "))
        assertNull(deliveryRejectionReason(null))
        assertNull(deliveryRejectionReason(""))
    }

    @Test fun `an unknown delivery value FAILS LOUDLY instead of picking one`() {
        for (bad in listOf("push", "background", "d", "auto-notify", "silent", "true", "1")) {
            assertNull("must not parse: $bad", parsedDeliveryMode(bad))
            val reason = deliveryRejectionReason(bad)
            assertTrue("must reject: $bad", reason != null)
            // The reason names the valid set, so the caller can correct itself.
            assertTrue(reason!!.contains("auto"))
            assertTrue(reason.contains("direct"))
            assertTrue(reason.contains("notify"))
        }
    }

    @Test fun `an invalid delivery is a caller-input error on the wire`() {
        // The phrase starts with "invalid navigation" so the SHIPPED classifier already maps
        // it to invalid_argument — no new error kind, no new classifier branch.
        val reason = deliveryRejectionReason("teleport")!!
        assertTrue(reason.startsWith("invalid navigation"))
        assertEquals("invalid_argument", classifyActuatorError(reason))
    }

    // =====================================================================
    // 5. Dedup — a retrying cron must not stack prompts
    // =====================================================================

    @Test fun `the same destination collapses onto one dedup key`() {
        assertEquals(
            navigationDedupKey(null, "1 Fake Industrial Way"),
            navigationDedupKey(null, "1 Fake Industrial Way"),
        )
    }

    @Test fun `dedup key ignores case and surrounding whitespace`() {
        assertEquals(
            navigationDedupKey(null, "1 Fake Industrial Way"),
            navigationDedupKey(null, "  1 FAKE Industrial Way  "),
        )
    }

    @Test fun `a different destination gets its own prompt`() {
        assertNotEquals(
            navigationDedupKey(null, "1 Fake Industrial Way"),
            navigationDedupKey(null, "2 Fake Industrial Way"),
        )
    }

    @Test fun `an explicit key from the bus wins over the derived one`() {
        assertEquals("notif-abc123", navigationDedupKey("notif-abc123", "1 Fake Industrial Way"))
        assertEquals("notif-abc123", navigationDedupKey("  notif-abc123  ", "1 Fake Industrial Way"))
        // …and a blank explicit key falls back to the derived one rather than to "".
        assertEquals(
            navigationDedupKey(null, "1 Fake Industrial Way"),
            navigationDedupKey("   ", "1 Fake Industrial Way"),
        )
    }

    @Test fun `a derived key is never blank`() {
        assertTrue(navigationDedupKey(null, "37.4224,-122.0841").isNotBlank())
    }

    // =====================================================================
    // 6. A navigation notification ALWAYS names the destination
    // =====================================================================

    @Test fun `the notification body always carries the destination in full`() {
        for (dest in listOf(
            "1 Fake Industrial Way",
            "37.4224,-122.0841",
            "A Very Long Fictional Job Site Address, Unit 1200, Nowhere County, Ontario",
        )) {
            assertTrue(navigationNotificationText(dest).contains(dest))
        }
    }

    @Test fun `the notification title carries the destination`() {
        assertTrue(navigationNotificationTitle("1 Fake Industrial Way").contains("1 Fake Industrial Way"))
        assertTrue(navigationNotificationTitle("37.4224,-122.0841").contains("37.4224,-122.0841"))
    }

    @Test fun `a very long destination is ellipsized in the title but never lost in the body`() {
        val long = "A Very Long Fictional Job Site Address, Unit 1200, Nowhere County, Ontario"
        val title = navigationNotificationTitle(long)
        assertTrue(title.length < long.length)
        assertTrue(title.endsWith("…"))
        // The full text survives where it matters — the expanded body.
        assertTrue(navigationNotificationText(long).contains(long))
    }

    @Test fun `the travel mode is surfaced next to the destination when specified`() {
        assertEquals("Driving", travelModeLabel("d"))
        assertEquals("Walking", travelModeLabel("w"))
        assertEquals("Bicycling", travelModeLabel("b"))
        assertEquals("Two-wheeler", travelModeLabel("l"))
        assertNull(travelModeLabel(null))
        // A non-whitelisted mode never reaches the notification text.
        assertNull(travelModeLabel("transit"))
        val text = navigationNotificationText("1 Fake Industrial Way", "w")
        assertTrue(text.contains("1 Fake Industrial Way"))
        assertTrue(text.contains("Walking"))
        assertEquals("1 Fake Industrial Way", navigationNotificationText("1 Fake Industrial Way", "transit"))
    }

    @Test fun `a navigation push cannot exist without a destination to show`() {
        // Structural: the payload type requires it, so no code path can post a prompt that
        // hides where it is about to send you.
        val push = NavigationPush(
            destination = "1 Fake Industrial Way",
            dedupKey = navigationDedupKey(null, "1 Fake Industrial Way"),
        )
        assertTrue(push.destination.isNotBlank())
        assertTrue(navigationNotificationText(push.destination).contains(push.destination))
        assertTrue(navigationNotificationTitle(push.destination).contains(push.destination))
    }

    @Test fun `the tap target is labelled Navigate`() {
        assertEquals("Navigate", NAVIGATION_ACTION_LABEL)
    }
}
