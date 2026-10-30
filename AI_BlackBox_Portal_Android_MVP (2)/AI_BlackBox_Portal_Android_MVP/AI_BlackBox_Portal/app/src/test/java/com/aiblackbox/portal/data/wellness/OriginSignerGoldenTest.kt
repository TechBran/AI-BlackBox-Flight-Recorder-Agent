package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.Base64

/**
 * The phone's origin proof reproduces the BOX's test vectors byte for byte
 * (`Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json`, read from the repo root).
 * The vectors' signatures were computed by Python's hmac, so this is a cross-language check of
 * the canonical string, the key decoding, the body digest (gzip bytes included) and the
 * header format, not a self-check.
 */
class OriginSignerGoldenTest {

    private fun vectorsFile(): File {
        var dir: File? = File("").absoluteFile
        repeat(10) {
            val d = dir ?: return@repeat
            val f = File(d, "Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json")
            if (f.exists()) return f
            dir = d.parentFile
        }
        throw AssertionError("origin_proof_vectors.json not found above ${File("").absolutePath}")
    }

    private fun vectors(): List<JsonObject> =
        Json.parseToJsonElement(vectorsFile().readText()).jsonArray.map { it.jsonObject }

    /** A vector's body bytes: `body_b64` (binary, e.g. gzip), `body_hex`, or `body_utf8`. */
    private fun body(v: JsonObject): ByteArray {
        v["body_b64"]?.jsonPrimitive?.contentOrNull?.let { return Base64.getDecoder().decode(it) }
        v["body_hex"]?.jsonPrimitive?.contentOrNull?.let { hex ->
            return ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
        }
        return (v["body_utf8"]?.jsonPrimitive?.contentOrNull ?: "").toByteArray(Charsets.UTF_8)
    }

    private fun s(v: JsonObject, key: String): String = v.getValue(key).jsonPrimitive.content

    @Test
    fun `there are at least six vectors including an empty body and a binary body`() {
        val all = vectors()
        assertTrue("expected >= 6 vectors, got ${all.size}", all.size >= 6)
        assertTrue("an empty-body vector", all.any { body(it).isEmpty() })
        assertTrue("a gzip-body vector", all.any { b -> body(b).let { it.size > 2 && it[0] == 0x1f.toByte() && it[1] == 0x8b.toByte() } })
    }

    @Test
    fun `every vector's signature is reproduced`() {
        for (v in vectors()) {
            val key = OriginSigner.decodeKey(s(v, "k_b64url"))
            val sig = OriginSigner.signature(
                key, s(v, "enr"), v.getValue("ts").jsonPrimitive.long, s(v, "nonce"), s(v, "method"),
                v["request_target"]?.jsonPrimitive?.contentOrNull ?: s(v, "path"), body(v),
            )
            assertEquals("vector ${s(v, "method")} ${s(v, "path")}", s(v, "expected_sig"), sig)
            v["canonical"]?.jsonPrimitive?.contentOrNull?.let { expected ->
                assertEquals(expected, OriginSigner.canonical(s(v, "enr"), v.getValue("ts").jsonPrimitive.long,
                    s(v, "nonce"), s(v, "method"), s(v, "path"), body(v)))
            }
        }
    }

    @Test
    fun `the header carries exactly the contract fields`() {
        val v = vectors().first { s(it, "method").uppercase() == "POST" }
        val signer = OriginSigner(s(v, "enr"), s(v, "k_b64url"))
        val header = signer.header(s(v, "method"), s(v, "path"), body(v), v.getValue("ts").jsonPrimitive.long, s(v, "nonce"))
        assertEquals(
            "v1;enr=${s(v, "enr")};ts=${v.getValue("ts").jsonPrimitive.long};nonce=${s(v, "nonce")};sig=${s(v, "expected_sig")}",
            header,
        )
        assertTrue(Regex("v1;enr=en_[0-9a-f]{8};ts=\\d+;nonce=[0-9a-f]{32};sig=[0-9a-f]{64}").matches(header))
    }

    @Test
    fun `the query string is not signed and the method is upper-cased`() {
        val k = OriginSigner.encodeKey(ByteArray(32) { it.toByte() })
        val signer = OriginSigner("en_0badc0de", k)
        val nonce = "0123456789abcdef0123456789abcdef"
        assertEquals(
            signer.header("GET", "/wellness/status", null, 100, nonce),
            signer.header("get", "/wellness/status?operator=wellness-fixture", null, 100, nonce),
        )
    }

    @Test
    fun `a fresh nonce is 32 lowercase hex and keys must be 32 bytes`() {
        val n = OriginSigner.newNonce()
        assertTrue(OriginSigner.isWellFormedNonce(n))
        assertFalse(OriginSigner.isWellFormedKey(OriginSigner.encodeKey(ByteArray(16))))
        assertTrue(OriginSigner.isWellFormedKey(OriginSigner.encodeKey(ByteArray(32))))
        // Padding on input is tolerated.
        assertTrue(OriginSigner.isWellFormedKey(OriginSigner.encodeKey(ByteArray(32)) + "="))
    }
}
