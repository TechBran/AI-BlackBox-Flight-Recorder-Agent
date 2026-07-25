package com.aiblackbox.portal.data.location

import kotlinx.coroutines.delay
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M1 — the location ride-along, JVM half.
 *
 * Everything a device is NOT needed for: the payload builder, the staleness rule, the
 * hard time budget, coordinate validity, and the invariant that governs the feature —
 * **a missing/denied/broken anything yields NO location, never a crash and never a
 * partial object.**
 *
 * GUARDRAIL: no real coordinates. Google's documented sample (37.4224,-122.0841 —
 * Mountain View) and obvious fakes only. Test files are tracked; the ledger is not.
 */
class LocationRulesTest {

    // Google's documented example coordinate.
    private val sampleLat = 37.4224
    private val sampleLon = -122.0841

    // Brandon's package contents: coordinates + city + state. The label is what a human
    // reads; city/state ride separately so the backend keeps them structured.
    private val mtnView = GeoPlace(
        city = "Mountain View",
        state = "California",
        label = "Mountain View, California",
    )

    // ─────────────────────────────────────────────────────────────────────────
    // Payload builder
    // ─────────────────────────────────────────────────────────────────────────

    @Test fun builds_a_payload_from_valid_coordinates() {
        val loc = LocationRules.build(sampleLat, sampleLon, accuracyM = 12.4, place = mtnView)
        assertNotNull(loc)
        assertEquals(sampleLat, loc!!.lat, 1e-9)
        assertEquals(sampleLon, loc.lon, 1e-9)
        assertEquals(12.4, loc.accuracyM!!, 1e-9)
        assertEquals("Mountain View, California", loc.place)
        assertEquals("Mountain View", loc.city)
        assertEquals("California", loc.state)
    }

    @Test fun coordinates_alone_are_a_complete_payload() {
        // The place name is an ENRICHMENT, never a requirement.
        val loc = LocationRules.build(sampleLat, sampleLon)
        assertNotNull(loc)
        assertNull(loc!!.place)
        assertNull(loc.accuracyM)
    }

    @Test fun blank_place_is_dropped_not_stored_as_empty() {
        assertNull(LocationRules.build(sampleLat, sampleLon, place = GeoPlace(label = "   "))!!.place)
        assertEquals("Hamilton", LocationRules.build(sampleLat, sampleLon, place = GeoPlace(city = "Hamilton", label = "  Hamilton  "))!!.place)
    }

    @Test fun coordinates_are_rounded_to_about_a_metre() {
        // Float noise past ~5 decimals is not information; it just bloats the wire and
        // defeats any coordinate-keyed cache on the backend.
        val loc = LocationRules.build(37.42246123456, -122.08412987654)!!
        assertEquals(37.42246, loc.lat, 1e-9)
        assertEquals(-122.08413, loc.lon, 1e-9)
    }

    @Test fun nonsense_accuracy_is_dropped_but_the_position_survives() {
        val nan = LocationRules.build(sampleLat, sampleLon, accuracyM = Double.NaN)
        assertNotNull(nan)
        assertNull(nan!!.accuracyM)
        assertNull(LocationRules.build(sampleLat, sampleLon, accuracyM = -5.0)!!.accuracyM)
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Coordinate validity — untrusted sensor/mock input
    // ─────────────────────────────────────────────────────────────────────────

    @Test fun valid_coordinates_are_accepted() {
        assertTrue(LocationRules.isValidCoordinate(sampleLat, sampleLon))
        assertTrue(LocationRules.isValidCoordinate(90.0, 180.0))
        assertTrue(LocationRules.isValidCoordinate(-90.0, -180.0))
    }

    @Test fun out_of_range_coordinates_are_rejected() {
        assertFalse(LocationRules.isValidCoordinate(91.0, 0.0))
        assertFalse(LocationRules.isValidCoordinate(-90.001, 0.0))
        assertFalse(LocationRules.isValidCoordinate(0.0, 180.5))
        assertFalse(LocationRules.isValidCoordinate(0.0, -181.0))
    }

    @Test fun non_finite_coordinates_are_rejected() {
        assertFalse(LocationRules.isValidCoordinate(Double.NaN, 0.0))
        assertFalse(LocationRules.isValidCoordinate(0.0, Double.NaN))
        assertFalse(LocationRules.isValidCoordinate(Double.POSITIVE_INFINITY, 0.0))
        assertFalse(LocationRules.isValidCoordinate(0.0, Double.NEGATIVE_INFINITY))
    }

    @Test fun null_island_is_rejected() {
        // 0,0 is a provider that never got a fix, not a position in the Gulf of Guinea.
        assertFalse(LocationRules.isValidCoordinate(0.0, 0.0))
    }

    @Test fun invalid_coordinates_yield_no_payload_at_all() {
        // NOT a partial object, NOT a place name with no position.
        assertNull(LocationRules.build(91.0, 0.0))
        assertNull(LocationRules.build(Double.NaN, 0.0, place = GeoPlace(label = "Somewhere")))
        assertNull(LocationRules.build(0.0, 0.0, accuracyM = 5.0, place = GeoPlace(label = "Null Island")))
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Staleness
    // ─────────────────────────────────────────────────────────────────────────

    @Test fun a_recent_fix_is_not_stale() {
        assertFalse(LocationRules.isStale(0L))
        assertFalse(LocationRules.isStale(30_000L))
        assertFalse(LocationRules.isStale(LocationRules.MAX_FIX_AGE_MS)) // boundary is inclusive
    }

    @Test fun a_fix_past_the_age_limit_is_stale() {
        assertTrue(LocationRules.isStale(LocationRules.MAX_FIX_AGE_MS + 1))
        assertTrue(LocationRules.isStale(10 * 60_000L))
    }

    @Test fun fresh_fix_is_requested_only_when_the_cache_cannot_serve() {
        assertTrue("no cached fix at all", LocationRules.needsFreshFix(hasLastFix = false, lastFixAgeMs = 0L))
        assertTrue("cached fix too old", LocationRules.needsFreshFix(hasLastFix = true, lastFixAgeMs = 5 * 60_000L))
        assertFalse("cached fix is good", LocationRules.needsFreshFix(hasLastFix = true, lastFixAgeMs = 1_000L))
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Place name
    // ─────────────────────────────────────────────────────────────────────────

    @Test fun place_is_city_comma_region() {
        assertEquals("Hamilton, Ontario", LocationRules.placeName("Hamilton", "Ontario"))
    }

    @Test fun place_degrades_through_whatever_the_geocoder_returned() {
        assertEquals("Hamilton", LocationRules.placeName("Hamilton", null))
        assertEquals("Ontario", LocationRules.placeName(null, "Ontario"))
        assertEquals("Ontario", LocationRules.placeName("  ", "Ontario"))
        // County stands in for a missing locality; country stands in for a missing region.
        assertEquals("Santa Clara County, Canada", LocationRules.placeName(null, null, "Santa Clara County", "Canada"))
        assertNull(LocationRules.placeName(null, null, null, null))
        assertNull(LocationRules.placeName("", "   "))
    }

    @Test fun a_city_state_that_share_a_name_is_not_doubled() {
        assertEquals("Singapore", LocationRules.placeName("Singapore", "Singapore"))
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Capture orchestration — permission, budget, degradation
    // ─────────────────────────────────────────────────────────────────────────

    private fun fix(ageMs: Long) = RawFix(sampleLat, sampleLon, 10.0, ageMs)

    private suspend fun capture(
        permitted: Boolean = true,
        budgetMs: Long = 1_000L,
        last: suspend () -> RawFix? = { fix(0L) },
        fresh: suspend () -> RawFix? = { null },
        geocode: suspend (Double, Double) -> GeoPlace? = { _, _ -> mtnView },
    ) = LocationRules.captureWith(budgetMs, permitted, last, fresh, geocode)

    @Test fun denied_permission_yields_no_location_and_reads_nothing() = runTest {
        var touched = false
        val result = capture(
            permitted = false,
            last = { touched = true; fix(0L) },
            fresh = { touched = true; fix(0L) },
            geocode = { _, _ -> touched = true; mtnView },
        )
        assertNull("a denied permission must produce NO location", result)
        assertFalse("nothing may be read without permission", touched)
    }

    @Test fun no_fix_available_yields_no_location() = runTest {
        assertNull(capture(last = { null }, fresh = { null }))
    }

    @Test fun a_fresh_cached_fix_is_used_without_escalating() = runTest {
        var freshCalled = false
        val result = capture(last = { fix(1_000L) }, fresh = { freshCalled = true; null })
        assertNotNull(result)
        assertFalse("a good cached fix must not power on a sensor", freshCalled)
    }

    @Test fun a_stale_cached_fix_escalates_to_one_fresh_fix() = runTest {
        var freshCalls = 0
        val result = capture(
            last = { fix(10 * 60_000L) },
            fresh = { freshCalls++; RawFix(1.5, 2.5, 4.0, 0L) },
        )
        assertEquals(1, freshCalls)
        assertEquals(1.5, result!!.lat, 1e-9)
    }

    @Test fun a_stale_fix_is_still_better_than_nothing_when_the_fresh_one_fails() = runTest {
        val result = capture(last = { fix(10 * 60_000L) }, fresh = { null })
        assertNotNull("a stale position beats no position", result)
        assertEquals(sampleLat, result!!.lat, 1e-9)
    }

    // ── the hard time budget ────────────────────────────────────────────────

    @kotlinx.coroutines.ExperimentalCoroutinesApi
    @Test fun a_hanging_fresh_fix_cannot_hold_the_send_past_the_budget() = runTest {
        // runTest's virtual clock: a 60s hang resolves instantly here, and the
        // assertion is that captureWith RETURNED rather than waiting for it.
        val result = capture(
            budgetMs = 1_000L,
            last = { null },
            fresh = { delay(60_000L); fix(0L) },
        )
        assertNull("no fix inside the budget → send without location", result)
        assertTrue("must not have waited for the hung provider", currentVirtualTime() <= 1_000L)
    }

    @Test fun a_hanging_fresh_fix_never_loses_an_already_cached_position() = runTest {
        // The cached fix is adopted BEFORE escalating, so the timeout can only cost us
        // the *improvement*, never the position we already had.
        val result = capture(
            budgetMs = 1_000L,
            last = { fix(10 * 60_000L) },
            fresh = { delay(60_000L); RawFix(1.0, 2.0, 1.0, 0L) },
            geocode = { _, _ -> GeoPlace(label = "never reached") },
        )
        assertNotNull(result)
        assertEquals(sampleLat, result!!.lat, 1e-9)
        assertNull("a timed-out turn carries coordinates, not a place name", result.place)
    }

    @Test fun a_hanging_geocoder_costs_the_place_name_not_the_coordinates() = runTest {
        val result = capture(
            budgetMs = 1_000L,
            last = { fix(0L) },
            geocode = { _, _ -> delay(60_000L); GeoPlace(label = "Too Slow") },
        )
        assertNotNull(result)
        assertEquals(sampleLat, result!!.lat, 1e-9)
        assertNull(result.place)
    }

    @Test fun a_zero_or_negative_budget_attaches_nothing() = runTest {
        assertNull(capture(budgetMs = 0L))
        assertNull(capture(budgetMs = -1L))
    }

    // ── failure isolation: nothing here may ever crash a chat turn ──────────

    @Test fun a_throwing_location_provider_degrades_to_no_location() = runTest {
        val result = capture(
            last = { error("SecurityException: permission revoked mid-flight") },
            fresh = { error("provider blew up") },
        )
        assertNull(result)
    }

    @Test fun a_missing_or_throwing_geocoder_still_sends_the_coordinates() = runTest {
        // Geocoder.isPresent() == false is modelled as a null return; a dead service as a throw.
        val absent = capture(geocode = { _, _ -> null })
        assertNotNull(absent)
        assertNull(absent!!.place)

        val broken = capture(geocode = { _, _ -> error("IOException: geocoder service not available") })
        assertNotNull(broken)
        assertEquals(sampleLat, broken!!.lat, 1e-9)
        assertNull(broken.place)
    }

    @Test fun a_garbage_fix_yields_no_location_rather_than_a_partial_object() = runTest {
        val result = capture(last = { RawFix(Double.NaN, 999.0, null, 0L) }, fresh = { null })
        assertNull(result)
    }

    @Test fun the_happy_path_carries_coordinates_and_place_together() = runTest {
        val result = capture()
        assertNotNull(result)
        assertEquals(sampleLat, result!!.lat, 1e-9)
        assertEquals(sampleLon, result.lon, 1e-9)
        assertEquals("Mountain View, California", result.place)
        assertEquals("Mountain View", result.city)
        assertEquals("California", result.state)
    }

    // ─────────────────────────────────────────────────────────────────────────
    // placeParts — the city + state split Brandon asked to ship
    // ─────────────────────────────────────────────────────────────────────────

    @Test fun place_parts_split_city_and_state() {
        val parts = LocationRules.placeParts("Hamilton", "Ontario")
        assertEquals("Hamilton", parts.city)
        assertEquals("Ontario", parts.state)
        assertEquals("Hamilton, Ontario", parts.label)
        assertFalse(parts.isEmpty())
    }

    @Test fun place_parts_fall_back_to_county_and_country() {
        val parts = LocationRules.placeParts(null, null, "Santa Clara County", "United States")
        assertEquals("Santa Clara County", parts.city)
        assertEquals("United States", parts.state)
    }

    @Test fun an_empty_geocode_result_is_empty() {
        assertTrue(LocationRules.placeParts(null, null).isEmpty())
        assertTrue(LocationRules.placeParts("  ", "\t").isEmpty())
        assertNull(LocationRules.placeOf(null))
    }
}

/** kotlinx-coroutines-test virtual clock accessor, kept out of the assertions above. */
@kotlinx.coroutines.ExperimentalCoroutinesApi
private fun kotlinx.coroutines.test.TestScope.currentVirtualTime(): Long =
    this.testScheduler.currentTime
