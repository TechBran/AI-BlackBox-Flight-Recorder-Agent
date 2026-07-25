package com.aiblackbox.portal.data.notifications

import com.aiblackbox.portal.data.location.LocationPermissionUx
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M4 — "ask once, ever; if denied, degrade silently and FOREVER", for POST_NOTIFICATIONS.
 *
 * The regression that made this milestone necessary was NOT a wrong prompt — it was a
 * MISSING one: the manifest declared the permission, nothing ever requested it, and on a
 * fresh Android 13+ install every M3 notification (the 07:30 navigation push included)
 * went nowhere with no error anywhere. So the tests pin both directions: it MUST ask when
 * it can, and it must never ask a second time when it cannot.
 */
class NotificationPermissionUxTest {

    private val TIRAMISU = 33
    private val ANDROID_12 = 32

    // ---- the ask-once policy ----------------------------------------------------

    @Test fun asks_on_first_launch_on_android_13() {
        assertTrue(
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = false, alreadyAsked = false,
            )
        )
    }

    @Test fun never_asks_below_api_33() {
        // There is no runtime grant to ask for; a dialog here would be a pure bug.
        assertFalse(
            NotificationPermissionUx.shouldAsk(
                sdkInt = ANDROID_12, hasPermission = false, alreadyAsked = false,
            )
        )
        assertFalse(
            NotificationPermissionUx.shouldAsk(
                sdkInt = 26, hasPermission = false, alreadyAsked = false,
            )
        )
        assertFalse(NotificationPermissionUx.isGrantRequired(ANDROID_12))
        assertTrue(NotificationPermissionUx.isGrantRequired(TIRAMISU))
        assertTrue("newer platforms still require it", NotificationPermissionUx.isGrantRequired(36))
    }

    @Test fun never_asks_twice_regardless_of_the_first_answer() {
        // The latch is written the moment the rationale appears, so ONE clause covers both
        // outcomes: a denial is permanent, and a grant needs no second prompt.
        assertFalse(
            "denied once = never again",
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = false, alreadyAsked = true,
            )
        )
        assertFalse(
            "granted = nothing to ask for",
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = true, alreadyAsked = true,
            )
        )
    }

    @Test fun denial_is_permanent_and_silent_across_many_launches() {
        // Simulate the real failure mode: every subsequent app open re-evaluates, and the
        // answer must stay "no" forever. An app that re-asks on every launch is exactly
        // what gets a permission revoked for good.
        repeat(50) {
            assertFalse(
                NotificationPermissionUx.shouldAsk(
                    sdkInt = TIRAMISU, hasPermission = false, alreadyAsked = true,
                )
            )
        }
        // And the state it leaves behind is REPORTED, not hidden.
        val state = NotificationPermissionUx.effectiveState(
            sdkInt = TIRAMISU,
            hasPermission = false,
            appNotificationsEnabled = true,
            navigationChannelEnabled = true,
        )
        assertEquals(NotificationDeliveryState.PERMISSION_MISSING, state)
        assertTrue(NotificationPermissionUx.needsAttention(state))
    }

    @Test fun never_asks_when_already_granted() {
        assertFalse(
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = true, alreadyAsked = false,
            )
        )
    }

    // ---- non-interference with the M1 location ask -------------------------------

    @Test fun the_two_permission_flows_never_prompt_at_the_same_time() {
        // Notification ask defers while a location rationale is up...
        assertFalse(
            "must not stack on the location rationale",
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU,
                hasPermission = false,
                alreadyAsked = false,
                anotherPromptVisible = true,
            )
        )
        // ...and the location ask defers while a notification ask is in flight.
        assertFalse(
            "must not stack on the notification rationale",
            LocationPermissionUx.shouldAsk(
                attachEnabled = true,
                hasPermission = false,
                alreadyAsked = false,
                anotherPromptVisible = true,
            )
        )
    }

    @Test fun a_deferred_ask_is_not_a_spent_ask() {
        // The latch is only written when we actually SHOW the dialog. So a deferral must
        // leave the very next evaluation asking — otherwise a collision would silently
        // burn an operator's single chance and the permission could never be granted.
        assertFalse(
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = false, alreadyAsked = false,
                anotherPromptVisible = true,
            )
        )
        assertTrue(
            "re-evaluated once the other prompt closed → asks now",
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = false, alreadyAsked = false,
                anotherPromptVisible = false,
            )
        )
        assertFalse(
            LocationPermissionUx.shouldAsk(
                attachEnabled = true, hasPermission = false, alreadyAsked = false,
                anotherPromptVisible = true,
            )
        )
        assertTrue(
            "location ask survives being deferred",
            LocationPermissionUx.shouldAsk(
                attachEnabled = true, hasPermission = false, alreadyAsked = false,
                anotherPromptVisible = false,
            )
        )
    }

    @Test fun the_two_latches_are_independent() {
        // Denying notifications must not suppress the location ask, and vice versa — they
        // are separate DataStore keys and separate decisions.
        assertTrue(
            "notifications denied, location never asked → location still asks",
            LocationPermissionUx.shouldAsk(
                attachEnabled = true, hasPermission = false, alreadyAsked = false,
            )
        )
        assertTrue(
            "location denied, notifications never asked → notifications still ask",
            NotificationPermissionUx.shouldAsk(
                sdkInt = TIRAMISU, hasPermission = false, alreadyAsked = false,
            )
        )
    }

    @Test fun the_location_ask_is_bit_for_bit_unchanged_for_pre_m4_call_sites() {
        // The new parameter defaults to false, so every existing three-argument call keeps
        // its exact M1 behaviour. If this ever fails, M4 regressed a shipped, device-proven ask.
        assertTrue(LocationPermissionUx.shouldAsk(true, false, false))
        assertFalse(LocationPermissionUx.shouldAsk(true, false, true))
        assertFalse(LocationPermissionUx.shouldAsk(true, true, false))
        assertFalse(LocationPermissionUx.shouldAsk(false, false, false))
        assertEquals(
            LocationPermissionUx.shouldAsk(true, false, false),
            LocationPermissionUx.shouldAsk(true, false, false, anotherPromptVisible = false),
        )
    }

    // ---- the effective delivery state (the anti-dead-end) ------------------------

    @Test fun all_three_switches_must_be_on_for_a_push_to_land() {
        assertEquals(
            NotificationDeliveryState.DELIVERS,
            NotificationPermissionUx.effectiveState(TIRAMISU, true, true, true),
        )
        assertEquals(
            NotificationDeliveryState.PERMISSION_MISSING,
            NotificationPermissionUx.effectiveState(TIRAMISU, false, true, true),
        )
        assertEquals(
            "the app being muted is NOT 'granted' — this was the real Fold state",
            NotificationDeliveryState.APP_DISABLED,
            NotificationPermissionUx.effectiveState(TIRAMISU, true, false, true),
        )
        assertEquals(
            NotificationDeliveryState.CHANNEL_DISABLED,
            NotificationPermissionUx.effectiveState(TIRAMISU, true, true, false),
        )
    }

    @Test fun below_api_33_the_permission_bit_is_irrelevant_but_the_mute_switches_are_not() {
        assertEquals(
            "no runtime grant exists on 12, so an ungranted flag cannot mean blocked",
            NotificationDeliveryState.DELIVERS,
            NotificationPermissionUx.effectiveState(ANDROID_12, false, true, true),
        )
        assertEquals(
            "but a muted app still swallows everything on 12",
            NotificationDeliveryState.APP_DISABLED,
            NotificationPermissionUx.effectiveState(ANDROID_12, false, false, true),
        )
    }

    @Test fun precedence_is_outermost_first_so_the_caption_names_the_fix_that_matters() {
        // Everything off at once → report the permission, because granting it is the first
        // thing the operator has to do; the deep link lands them on the same screen anyway.
        assertEquals(
            NotificationDeliveryState.PERMISSION_MISSING,
            NotificationPermissionUx.effectiveState(TIRAMISU, false, false, false),
        )
    }

    @Test fun every_broken_state_says_out_loud_that_nothing_will_arrive() {
        // A caption that merely says "off" leaves the dead end for the operator to infer.
        listOf(
            NotificationDeliveryState.PERMISSION_MISSING,
            NotificationDeliveryState.APP_DISABLED,
            NotificationDeliveryState.CHANNEL_DISABLED,
        ).forEach { state ->
            val caption = NotificationPermissionUx.caption(state)
            assertTrue("$state must say what will not arrive", caption.contains("will NOT"))
            assertTrue("$state must offer the fix", NotificationPermissionUx.needsAttention(state))
        }
        assertFalse(
            NotificationPermissionUx.needsAttention(NotificationDeliveryState.DELIVERS)
        )
        assertTrue(
            NotificationPermissionUx.caption(NotificationDeliveryState.DELIVERS).contains("arrive")
        )
    }

    @Test fun the_navigation_caption_is_specific_about_what_still_works() {
        // A channel-level mute is a PARTIAL failure; saying "notifications are off" there
        // would be a different lie.
        val caption = NotificationPermissionUx.caption(NotificationDeliveryState.CHANNEL_DISABLED)
        assertTrue(caption.contains("Task alerts still"))
        assertTrue(caption.contains("navigation"))
    }

    // ---- the rationale copy ------------------------------------------------------

    @Test fun the_rationale_says_what_it_is_FOR_in_the_users_terms() {
        val body = NotificationPermissionUx.RATIONALE_BODY
        assertTrue(
            "must name scheduled navigation",
            body.contains("Scheduled navigation")
        )
        assertTrue("must name task alerts", body.contains("task alerts"))
        assertTrue(
            "must say the failure is SILENT — that is the expensive part",
            body.contains("silently do not arrive")
        )
        assertTrue("must say it is reversible", body.contains("turn it off"))
        // No jargon leaking into the operator's dialog.
        assertFalse(body.contains("POST_NOTIFICATIONS"))
        assertFalse(body.contains("API"))
    }

    @Test fun exactly_one_permission_is_requested() {
        // If this ever changes, someone widened what the app ASKS for behind a rationale
        // that only explains notifications.
        assertEquals("android.permission.POST_NOTIFICATIONS", NotificationPermissionUx.PERMISSION)
        assertEquals(33, NotificationPermissionUx.MIN_SDK_REQUIRING_GRANT)
    }
}
