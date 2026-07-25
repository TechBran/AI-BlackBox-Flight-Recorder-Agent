package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.overlay.NavigationNotifier
import com.aiblackbox.portal.overlay.NavigationNotifyOutcome
import com.aiblackbox.portal.overlay.NavigationPush
import com.aiblackbox.portal.overlay.navigationNotificationText
import com.aiblackbox.portal.overlay.navigationNotificationTitle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Minimal task handler — /notify is model-free, so nothing here is ever consulted. */
private class NavFakeHandler : RemoteTaskHandler {
    override fun submitTask(task: String, operator: String): String = "task-1"
    override fun taskStatus(taskId: String): RemoteStatus? = null
    override fun healthz(): Boolean = true
}

/**
 * (M3) `POST /notify` carrying a NAVIGATION destination — the delivery that works when the
 * phone is backgrounded or LOCKED, which is the only state a 07:30 cron ever finds it in.
 *
 * A direct activity launch is silently discarded by Android in that state (measured: the
 * actuator reported success:true and Maps never opened), so the prompt-plus-tap path is not
 * a nicety — it is the ONLY mechanism. These tests pin the routing decisions around it:
 * the cross-operator metadata-only rule, dedup, the strict envelope, and the refusal to
 * answer `ok:true` for a notification that was never delivered.
 */
class NotifyNavigationTest {

    /** Captures the navigation prompt; returns a canned outcome. */
    private class FakeNavNotifier(
        private val outcome: NavigationNotifyOutcome = NavigationNotifyOutcome.POSTED,
    ) : NavigationNotifier {
        var calls = 0
        var last: NavigationPush? = null
        override fun postNavigation(push: NavigationPush): NavigationNotifyOutcome {
            calls++; last = push; return outcome
        }
    }

    /** Captures a plain notification (the metadata-only fallback). */
    private class FakePlainNotifier : Notifier {
        var calls = 0
        var lastTitle: String? = null
        var lastBody: String? = null
        var lastCategory: String? = null
        override fun postNotification(
            title: String, body: String, category: String, operator: String, notifId: String,
        ) {
            calls++; lastTitle = title; lastBody = body; lastCategory = category
        }
    }

    private fun notify(
        body: String,
        nav: NavigationNotifier? = null,
        plain: Notifier? = null,
        boundOperator: String = "Brandon",
    ) = routeRequest(
        "POST", "/notify", body, NavFakeHandler(), plain, null, nav, boundOperator,
    )

    // =====================================================================
    // The navigation prompt is delivered for the device's OWN operator
    // =====================================================================

    @Test fun a_navigation_payload_posts_a_navigation_prompt() {
        val nav = FakeNavNotifier()
        val r = notify(
            """{"title":"First job","operator":"Brandon","notif_id":"cron-0730",
               "destination":"1 Fake Industrial Way","nav_mode":"d"}""",
            nav = nav,
        )
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("\"ok\":true"))
        assertEquals(1, nav.calls)
        assertEquals("1 Fake Industrial Way", nav.last!!.destination)
        assertEquals("d", nav.last!!.travelMode)
    }

    @Test fun the_prompt_always_carries_the_destination() {
        // A notification that hides where it is about to send you is not consent.
        val nav = FakeNavNotifier()
        notify(
            """{"operator":"Brandon","destination":"37.4224,-122.0841"}""",
            nav = nav,
        )
        val push = nav.last!!
        assertTrue(push.destination.isNotBlank())
        assertTrue(navigationNotificationTitle(push.destination).contains("37.4224,-122.0841"))
        assertTrue(navigationNotificationText(push.destination).contains("37.4224,-122.0841"))
    }

    @Test fun a_navigation_payload_needs_no_title_or_body() {
        // The destination IS the thing to show, so the old "title or body required" rule
        // must not reject a well-formed navigation push that carries only a destination.
        val nav = FakeNavNotifier()
        val r = notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        assertEquals(200, r.status)
        assertEquals(1, nav.calls)
    }

    @Test fun the_navigation_package_default_is_resolved_for_the_tap() {
        val nav = FakeNavNotifier()
        notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        assertEquals("com.google.android.apps.maps", nav.last!!.packageName)
    }

    @Test fun a_caller_can_opt_out_of_the_pinned_maps_package() {
        val nav = FakeNavNotifier()
        notify(
            """{"operator":"Brandon","destination":"1 Fake Industrial Way","nav_package":"any"}""",
            nav = nav,
        )
        assertEquals(null, nav.last!!.packageName)
    }

    // =====================================================================
    // CROSS-OPERATOR: metadata-only, and never a one-tap "drive here" button
    // =====================================================================

    @Test fun a_navigation_payload_for_another_operator_stays_metadata_only() {
        val nav = FakeNavNotifier()
        val plain = FakePlainNotifier()
        val r = notify(
            """{"title":"Sarah: schedule","body":"heading to 9 Other Street","category":"navigation",
               "operator":"Sarah","destination":"9 Other Street"}""",
            nav = nav, plain = plain, boundOperator = "Brandon",
        )
        assertEquals(200, r.status)
        // NO navigation prompt: another operator's schedule can never put a one-tap
        // "drive here" button on this operator's lock screen.
        assertEquals(0, nav.calls)
        // …and it degrades to metadata only: the body (which restated the destination) is
        // dropped, not merely the action button.
        assertEquals(1, plain.calls)
        assertEquals("", plain.lastBody)
        assertEquals("Sarah: schedule", plain.lastTitle)
        assertEquals("navigation", plain.lastCategory)
        assertFalse(plain.lastBody!!.contains("9 Other Street"))
    }

    @Test fun a_blank_bound_operator_fail_closes_to_metadata_only() {
        // Fail-closed, matching authorize(): an unbound device never renders an actionable
        // navigation prompt for anyone.
        val nav = FakeNavNotifier()
        val plain = FakePlainNotifier()
        val r = notify(
            """{"title":"Job","operator":"Brandon","destination":"1 Fake Industrial Way"}""",
            nav = nav, plain = plain, boundOperator = "",
        )
        assertEquals(200, r.status)
        assertEquals(0, nav.calls)
        assertEquals(1, plain.calls)
        assertEquals("", plain.lastBody)
    }

    @Test fun a_blank_request_operator_fail_closes_to_metadata_only() {
        val nav = FakeNavNotifier()
        val plain = FakePlainNotifier()
        notify(
            """{"title":"Job","destination":"1 Fake Industrial Way"}""",
            nav = nav, plain = plain, boundOperator = "Brandon",
        )
        assertEquals(0, nav.calls)
        assertEquals(1, plain.calls)
    }

    @Test fun a_cross_operator_navigation_payload_with_no_metadata_is_400() {
        // Nothing survives the degrade → there is nothing to show at all.
        val nav = FakeNavNotifier()
        val plain = FakePlainNotifier()
        val r = notify(
            """{"operator":"Sarah","destination":"9 Other Street"}""",
            nav = nav, plain = plain, boundOperator = "Brandon",
        )
        assertEquals(400, r.status)
        assertEquals(0, nav.calls)
        assertEquals(0, plain.calls)
    }

    @Test fun operator_match_is_case_insensitive_and_trimmed() {
        val nav = FakeNavNotifier()
        notify(
            """{"operator":"  brandon  ","destination":"1 Fake Industrial Way"}""",
            nav = nav, boundOperator = "Brandon",
        )
        assertEquals(1, nav.calls)
    }

    // =====================================================================
    // DEDUP — a retrying cron must not stack duplicate prompts
    // =====================================================================

    @Test fun a_retried_push_reuses_the_same_dedup_key() {
        val nav = FakeNavNotifier()
        val payload =
            """{"operator":"Brandon","notif_id":"cron-0730","destination":"1 Fake Industrial Way"}"""
        notify(payload, nav = nav)
        val first = nav.last!!.dedupKey
        notify(payload, nav = nav)
        assertEquals(2, nav.calls)
        assertEquals(first, nav.last!!.dedupKey)
        assertEquals("cron-0730", first)
    }

    @Test fun a_keyless_retry_still_collapses_via_the_destination() {
        val nav = FakeNavNotifier()
        notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        val first = nav.last!!.dedupKey
        notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        assertEquals(first, nav.last!!.dedupKey)
        assertTrue(first.isNotBlank())
    }

    @Test fun a_different_destination_is_a_different_prompt() {
        val nav = FakeNavNotifier()
        notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        val first = nav.last!!.dedupKey
        notify("""{"operator":"Brandon","destination":"2 Fake Industrial Way"}""", nav = nav)
        assertNotEquals(first, nav.last!!.dedupKey)
    }

    // =====================================================================
    // HONEST DELIVERY — never ok:true for a prompt that was not delivered
    // =====================================================================

    @Test fun a_missing_notification_permission_is_reported_not_swallowed() {
        val nav = FakeNavNotifier(NavigationNotifyOutcome.PERMISSION_MISSING)
        val r = notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        assertEquals(503, r.status)
        assertFalse(r.json, r.json.contains("\"ok\":true"))
        assertTrue(r.json, r.json.contains("notification_permission_missing"))
    }

    @Test fun a_refused_post_is_reported_not_swallowed() {
        val nav = FakeNavNotifier(NavigationNotifyOutcome.FAILED)
        val r = notify("""{"operator":"Brandon","destination":"1 Fake Industrial Way"}""", nav = nav)
        assertEquals(503, r.status)
        assertTrue(r.json, r.json.contains("notification_delivery_failed"))
    }

    @Test fun a_navigation_payload_with_no_navigation_notifier_is_503() {
        // Never silently downgraded to a plain, un-actionable note: the operator would be
        // left with a card and no way to navigate, believing the push worked.
        val plain = FakePlainNotifier()
        val r = notify(
            """{"operator":"Brandon","destination":"1 Fake Industrial Way"}""",
            nav = null, plain = plain,
        )
        assertEquals(503, r.status)
        assertEquals(0, plain.calls)
    }

    // =====================================================================
    // Envelope: the same strict whitelists as the /action path
    // =====================================================================

    @Test fun an_out_of_whitelist_travel_mode_is_400() {
        val nav = FakeNavNotifier()
        val r = notify(
            """{"operator":"Brandon","destination":"1 Fake Industrial Way","nav_mode":"transit"}""",
            nav = nav,
        )
        assertEquals(400, r.status)
        assertEquals(0, nav.calls)
        assertTrue(r.json, r.json.contains("invalid navigation mode"))
    }

    @Test fun an_out_of_whitelist_avoid_flag_is_400() {
        val nav = FakeNavNotifier()
        val r = notify(
            """{"operator":"Brandon","destination":"1 Fake Industrial Way","nav_avoid":"xyz"}""",
            nav = nav,
        )
        assertEquals(400, r.status)
        assertEquals(0, nav.calls)
    }

    @Test fun a_navigation_category_with_no_destination_is_400() {
        val nav = FakeNavNotifier()
        val plain = FakePlainNotifier()
        val r = notify(
            """{"title":"Go now","category":"navigation","operator":"Brandon"}""",
            nav = nav, plain = plain,
        )
        assertEquals(400, r.status)
        assertEquals(0, nav.calls)
        assertEquals(0, plain.calls)
        assertTrue(r.json, r.json.contains("destination required"))
    }

    // =====================================================================
    // Back-compat: an ordinary push is completely unaffected
    // =====================================================================

    @Test fun an_ordinary_notification_still_routes_to_the_plain_notifier() {
        val nav = FakeNavNotifier()
        val plain = FakePlainNotifier()
        val r = notify(
            """{"title":"Build done","body":"green","category":"ci","operator":"Brandon"}""",
            nav = nav, plain = plain,
        )
        assertEquals(200, r.status)
        assertEquals(0, nav.calls)
        assertEquals(1, plain.calls)
        assertEquals("green", plain.lastBody)
    }

    @Test fun an_ordinary_cross_operator_push_keeps_its_body() {
        // The metadata-only degrade applies ONLY to navigation payloads; the shipped
        // cross-operator behaviour for ordinary notifications is untouched here (the bus
        // already strips those server-side).
        val plain = FakePlainNotifier()
        notify(
            """{"title":"Sarah: chat","body":"hello","category":"chat","operator":"Sarah"}""",
            nav = FakeNavNotifier(), plain = plain, boundOperator = "Brandon",
        )
        assertEquals(1, plain.calls)
        assertEquals("hello", plain.lastBody)
    }
}
