package com.aiblackbox.portal.data.location

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M1 — "ask once, ever; if denied, degrade silently and FOREVER."
 *
 * The nagging failure mode is the one worth a test: an app that re-asks on every send is
 * exactly what makes people revoke a permission for good.
 */
class LocationPermissionUxTest {

    @Test fun asks_on_first_use() {
        assertTrue(
            LocationPermissionUx.shouldAsk(attachEnabled = true, hasPermission = false, alreadyAsked = false)
        )
    }

    @Test fun never_asks_twice_regardless_of_the_first_answer() {
        // The latch is set the moment we ask, so BOTH outcomes are covered by one clause:
        // a denial is permanent, and a grant needs no second prompt.
        assertFalse(
            "denied once = never again",
            LocationPermissionUx.shouldAsk(attachEnabled = true, hasPermission = false, alreadyAsked = true)
        )
        assertFalse(
            "granted = nothing to ask for",
            LocationPermissionUx.shouldAsk(attachEnabled = true, hasPermission = true, alreadyAsked = true)
        )
    }

    @Test fun defers_instead_of_stacking_on_another_permission_prompt() {
        // M4 added a second one-shot ask (POST_NOTIFICATIONS). Two modal rationales must
        // never be on screen together — and critically, DEFERRING must not burn this ask's
        // latch, so the very next send still asks.
        assertFalse(
            "must not stack on the notification rationale",
            LocationPermissionUx.shouldAsk(
                attachEnabled = true, hasPermission = false, alreadyAsked = false,
                anotherPromptVisible = true,
            )
        )
        assertTrue(
            "the deferred ask survives — it happens on the next send",
            LocationPermissionUx.shouldAsk(
                attachEnabled = true, hasPermission = false, alreadyAsked = false,
                anotherPromptVisible = false,
            )
        )
    }

    @Test fun never_asks_when_already_granted() {
        assertFalse(
            LocationPermissionUx.shouldAsk(attachEnabled = true, hasPermission = true, alreadyAsked = false)
        )
    }

    @Test fun never_asks_when_the_operator_turned_the_feature_off() {
        // Opting out in Settings must not be answered with a permission prompt.
        assertFalse(
            LocationPermissionUx.shouldAsk(attachEnabled = false, hasPermission = false, alreadyAsked = false)
        )
        assertFalse(
            LocationPermissionUx.shouldAsk(attachEnabled = false, hasPermission = true, alreadyAsked = false)
        )
    }

    @Test fun both_switches_must_be_on_for_a_location_to_ride_along() {
        assertTrue(LocationPermissionUx.willAttach(attachEnabled = true, hasPermission = true))
        assertFalse("toggle off wins over an OS grant", LocationPermissionUx.willAttach(true, false))
        assertFalse(LocationPermissionUx.willAttach(false, true))
        assertFalse(LocationPermissionUx.willAttach(false, false))
    }

    @Test fun background_location_is_never_requested() {
        // The whole design avoids the Play Data Safety burden by staying while-in-use.
        // If this ever fails, someone added a permission that changes what this app IS.
        assertEquals(2, LocationPermissionUx.REQUESTED_PERMISSIONS.size)
        assertTrue(
            LocationPermissionUx.REQUESTED_PERMISSIONS.none { it.contains("BACKGROUND", ignoreCase = true) }
        )
        assertTrue(LocationPermissionUx.REQUESTED_PERMISSIONS.contains("android.permission.ACCESS_COARSE_LOCATION"))
        assertTrue(LocationPermissionUx.REQUESTED_PERMISSIONS.contains("android.permission.ACCESS_FINE_LOCATION"))
    }

    @Test fun the_rationale_is_plain_and_says_why() {
        assertTrue(LocationPermissionUx.RATIONALE_BODY.contains("where you are"))
        assertTrue("must say it is not a background tracker", LocationPermissionUx.RATIONALE_BODY.contains("never in the background"))
        assertTrue("must say it is reversible", LocationPermissionUx.RATIONALE_BODY.contains("turn it off"))
    }
}
