package com.aiblackbox.portal.data.model

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.wellness.WellnessUsed
import com.aiblackbox.portal.ui.chat.ChatViewModel
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The M7 wire and storage fields are additive (spec §3.7): `wellness_ref` on a history
 * message and `wellness_turn` on /chat/save appear only when the turn has one, so every other
 * request is byte-identical to before; the bubble's health use survives the local history,
 * and a history written before M7 still loads.
 */
class WellnessWireFieldsTest {

    // The JSON the requests are encoded with (BlackBoxApi.json: encodeDefaults = true).
    private val wire = BlackBoxApi("http://localhost:9091").json
    // ChatHistoryStore's.
    private val local = Json { ignoreUnknownKeys = true; isLenient = true; encodeDefaults = true }

    @Test
    fun `a history message carries wellness_ref only when its turn used health data`() {
        val plain = ChatMessage(role = "assistant", content = JsonPrimitive("hi"), timestamp = 1)
        assertEquals("""{"role":"assistant","content":"hi","timestamp":1,"model":null,"provider":null,"reasoning":null}""",
            wire.encodeToString(ChatMessage.serializer(), plain))
        val tagged = plain.copy(wellnessRef = "w-20261028-ab12")
        val obj = wire.parseToJsonElement(wire.encodeToString(ChatMessage.serializer(), tagged)).jsonObject
        assertEquals("w-20261028-ab12", obj["wellness_ref"]?.jsonPrimitive?.content)
    }

    @Test
    fun `a stream request without health is exactly what it was`() {
        val req = StreamRequest(messages = listOf(ChatMessage("user", JsonPrimitive("hello"), 1)), operator = "wellness-fixture")
        val text = wire.encodeToString(StreamRequest.serializer(), req)
        assertFalse(text.contains("wellness_"))
        val messages = wire.parseToJsonElement(text).jsonObject.getValue("messages").jsonArray
        assertEquals(setOf("role", "content", "timestamp", "model", "provider", "reasoning"), messages[0].jsonObject.keys)
    }

    @Test
    fun `the save echoes the turn key only for a turn that carried one`() {
        val none = ChatViewModel.buildSaveRequest("wellness-fixture", "q", "a", "", null, null, null)
        assertFalse(wire.encodeToString(SaveRequest.serializer(), none).contains("wellness_turn"))
        val blank = ChatViewModel.buildSaveRequest("wellness-fixture", "q", "a", "", null, null, null, wellnessTurn = " ")
        assertNull(blank.wellnessTurn)
        val keyed = ChatViewModel.buildSaveRequest("wellness-fixture", "q", "a", "", null, null, null,
            wellnessTurn = "wt_0123456789abcdef")
        val obj = wire.parseToJsonElement(wire.encodeToString(SaveRequest.serializer(), keyed)).jsonObject
        assertEquals("wt_0123456789abcdef", obj["wellness_turn"]?.jsonPrimitive?.content)
    }

    @Test
    fun `the bubble's health use survives the local history, and old history still loads`() {
        val used = WellnessUsed("w-20261028-ab12", categories = listOf("sleep"), asOf = "2026-10-28T07:10-04:00",
            providerLabel = "Anthropic")
        val m = UiMessage(id = "m1", role = "assistant", content = "x", timestamp = 1, wellnessRef = used.ref, wellnessUsed = used)
        val text = local.encodeToString(ListSerializer(UiMessage.serializer()), listOf(m))
        assertEquals(listOf(m), local.decodeFromString(ListSerializer(UiMessage.serializer()), text))

        val before = """[{"id":"m0","role":"assistant","content":"old","timestamp":1}]"""
        val old = local.decodeFromString(ListSerializer(UiMessage.serializer()), before).single()
        assertNull(old.wellnessRef)
        assertNull(old.wellnessUsed)
    }
}
