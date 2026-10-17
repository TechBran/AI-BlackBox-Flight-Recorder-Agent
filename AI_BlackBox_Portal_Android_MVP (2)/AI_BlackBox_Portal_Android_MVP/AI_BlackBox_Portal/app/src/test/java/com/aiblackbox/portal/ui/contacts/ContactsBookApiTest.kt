package com.aiblackbox.portal.ui.contacts

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * A14-ui-misc-07 — Contacts saved and deleted in the book of whichever operator was
 * current when the screen FIRST opened, while the dialog already named the new one
 * ("This number IS operator <new>"), so is_operator_self could land in the wrong book.
 * [ContactsBookApi] reads the operator at EVERY request. Real HTTP, local MockWebServer.
 */
class ContactsBookApiTest {

    private val server = MockWebServer()
    private var operator = "Ana"
    private lateinit var book: ContactsBookApi

    @Before fun setUp() {
        server.start()
        book = ContactsBookApi(BlackBoxApi(server.url("").toString().removeSuffix("/"))) { operator }
    }

    @After fun tearDown() = server.close()

    private fun reply(body: String = "{}") = server.enqueue(MockResponse.Builder().body(body).build())
    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!

    private suspend fun save(name: String = "Mallory", self: Boolean = false) = book.save(
        name = name, phone = "+15550001111", email = "", relationship = "", notes = "a \"quoted\" note",
        tags = "a, b,", inboundAllowed = true, isOperatorSelf = self,
    )

    @Test fun `list, save and delete each use the operator current at that request`() = runBlocking {
        repeat(3) { reply() }
        book.list()
        assertEquals("Ana", taken().url.queryParameter("operator"))

        operator = "Ben"  // switched while the Contacts screen is open
        save(self = true)
        val saved = Json.parseToJsonElement(taken().body!!.utf8()).jsonObject
        assertEquals("Ben", saved["operator"]!!.jsonPrimitive.content)
        assertEquals(true, saved["is_operator_self"]!!.jsonPrimitive.boolean)
        assertEquals("a \"quoted\" note", saved["notes"]!!.jsonPrimitive.content)
        assertEquals(listOf("a", "b"), saved["tags"]!!.jsonArray.map { it.jsonPrimitive.content })

        book.delete("c1")
        val deleted = taken()
        assertEquals("DELETE", deleted.method)
        assertEquals("/contacts/c1", deleted.url.encodedPath)
        assertEquals("Ben", deleted.url.queryParameter("operator"))
    }

    @Test fun `with no operator the book is neither read nor written`() = runBlocking {
        operator = ""
        assertNull(book.list())
        assertThrows(OperatorUnresolvedException::class.java) { runBlocking { save() } }
        assertThrows(OperatorUnresolvedException::class.java) { runBlocking { book.delete("c1") } }
        assertEquals(0, server.requestCount)
    }
}
