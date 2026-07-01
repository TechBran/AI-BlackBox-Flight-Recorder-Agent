package com.aiblackbox.portal.data.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M3 task 3.6 (Android half): proves the chat request the app sends carries the
 * originating device's tailnet id as `origin_device_id`. ChatRepository builds a
 * [StreamRequest] and encodes it with the SAME Json config as BlackBoxApi
 * (encodeDefaults = true), so exercising that serialization is exercising the wire
 * payload the backend reads.
 */
class StreamRequestOriginTest {

    // Mirror of BlackBoxApi.json.
    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
    }

    private fun userMessages() = listOf(
        ChatMessage(role = "user", content = JsonPrimitive("turn my phone volume up"))
    )

    @Test fun origin_device_id_is_included_when_set() {
        val request = StreamRequest(
            messages = userMessages(),
            operator = "Brandon",
            originDeviceId = "100.64.1.2",
        )
        val body = json.encodeToString(StreamRequest.serializer(), request)
        assertTrue(
            "expected origin_device_id in body, got: $body",
            body.contains("\"origin_device_id\":\"100.64.1.2\""),
        )
    }

    @Test fun uses_the_snake_case_wire_key_not_the_kotlin_name() {
        val request = StreamRequest(
            messages = userMessages(),
            operator = "Brandon",
            originDeviceId = "100.88.0.7",
        )
        val body = json.encodeToString(StreamRequest.serializer(), request)
        // @SerialName must win: the backend reads body["origin_device_id"].
        assertTrue(body.contains("\"origin_device_id\""))
        assertFalse(body.contains("\"originDeviceId\""))
    }

    @Test fun back_compat_when_origin_absent() {
        // Default (no origin) still serializes cleanly with operator + messages intact;
        // the backend treats a null/absent origin as "fall back to the primary device".
        val request = StreamRequest(messages = userMessages(), operator = "Brandon")
        val body = json.encodeToString(StreamRequest.serializer(), request)
        assertTrue(body.contains("\"operator\":\"Brandon\""))
        // Round-trips back to a null origin.
        val decoded = json.decodeFromString(StreamRequest.serializer(), body)
        assertTrue(decoded.originDeviceId == null)
    }
}
