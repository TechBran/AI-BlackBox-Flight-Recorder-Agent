package com.aiblackbox.portal.data.model

import com.aiblackbox.portal.data.location.GeoPlace
import com.aiblackbox.portal.data.location.LocationRules
import com.aiblackbox.portal.data.location.UserLocation
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M1 (Android half) — proves the WIRE SHAPE of the location ride-along.
 *
 * ChatRepository builds a [StreamRequest] and encodes it with the SAME Json config as
 * BlackBoxApi (encodeDefaults = true), so exercising that serialization here is
 * exercising the exact payload the backend reads. Mirrors StreamRequestOriginTest, the
 * precedent this field follows.
 *
 * GUARDRAIL: Google's documented sample coordinate only — never a real position.
 */
class StreamRequestLocationTest {

    // Mirror of BlackBoxApi.json.
    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
    }

    private val mtnView = GeoPlace(
        city = "Mountain View",
        state = "California",
        label = "Mountain View, California",
    )

    private fun userMessages() = listOf(
        ChatMessage(role = "user", content = JsonPrimitive("find me a restaurant nearby"))
    )

    private fun request(location: UserLocation?) = StreamRequest(
        messages = userMessages(),
        operator = "Brandon",
        location = location,
    )

    @Test fun location_rides_along_under_the_snake_case_wire_keys() {
        val loc = LocationRules.build(37.4224, -122.0841, accuracyM = 12.0, place = mtnView)!!
        val body = json.encodeToString(StreamRequest.serializer(), request(loc))
        assertTrue("expected a location object, got: $body", body.contains("\"location\":{"))
        assertTrue(body.contains("\"lat\":37.4224"))
        assertTrue(body.contains("\"lon\":-122.0841"))
        assertTrue(body.contains("\"accuracy_m\":12.0"))
        // Brandon's package contents: coordinates + city + state (plus the composed label).
        assertTrue(body.contains("\"place\":\"Mountain View, California\""))
        assertTrue(body.contains("\"city\":\"Mountain View\""))
        assertTrue(body.contains("\"state\":\"California\""))
        // @SerialName must win — the backend reads accuracy_m, not the Kotlin name.
        assertFalse(body.contains("accuracyM"))
    }

    @Test fun absent_location_is_a_no_op_on_the_wire() {
        // Every surface without a fix — Portal, CLI terminals, cron, denied permission,
        // toggle off, no GPS lock inside the budget — sends this. The backend's
        // body.get("location") is None and the turn is identical to today's.
        val body = json.encodeToString(StreamRequest.serializer(), request(null))
        assertTrue("location must be explicitly null, not an empty object", body.contains("\"location\":null"))
        assertFalse(body.contains("\"lat\""))
        assertFalse(body.contains("\"place\""))
        // The rest of the request is untouched.
        assertTrue(body.contains("\"operator\":\"Brandon\""))
        assertTrue(body.contains("find me a restaurant nearby"))
    }

    @Test fun the_default_stream_request_carries_no_location() {
        // Nothing opts in by accident: a request built without the parameter has none.
        val body = json.encodeToString(
            StreamRequest.serializer(),
            StreamRequest(messages = userMessages(), operator = "Brandon"),
        )
        val decoded = json.decodeFromString(StreamRequest.serializer(), body)
        assertNull(decoded.location)
    }

    @Test fun location_round_trips() {
        val loc = LocationRules.build(37.4224, -122.0841, place = mtnView)!!
        val body = json.encodeToString(StreamRequest.serializer(), request(loc))
        val decoded = json.decodeFromString(StreamRequest.serializer(), body)
        assertEquals(loc, decoded.location)
    }

    @Test fun coordinates_ride_alone_when_the_geocoder_gave_nothing() {
        val loc = LocationRules.build(37.4224, -122.0841)!!
        val body = json.encodeToString(StreamRequest.serializer(), request(loc))
        assertTrue(body.contains("\"lat\":37.4224"))
        assertTrue(body.contains("\"place\":null"))
    }

    @Test fun location_does_not_disturb_the_origin_device_field_beside_it() {
        val loc = LocationRules.build(37.4224, -122.0841)!!
        val body = json.encodeToString(
            StreamRequest.serializer(),
            StreamRequest(
                messages = userMessages(),
                operator = "Brandon",
                originDeviceId = "100.64.1.2",
                location = loc,
            ),
        )
        assertTrue(body.contains("\"origin_device_id\":\"100.64.1.2\""))
        assertTrue(body.contains("\"location\":{"))
    }
}
