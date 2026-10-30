package com.aiblackbox.portal.data.wellness

import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * The origin proof the phone attaches to every signed call to the box (wire contract):
 *
 *     X-BlackBox-Origin: v1;enr=<enrollment_id>;ts=<unix s>;nonce=<32 hex>;sig=<64 hex>
 *
 *     sig = HMAC-SHA256(K, "v1|<enr>|<ts>|<nonce>|<METHOD>|<path>|<sha256 hex of the body bytes>")
 *
 * K is the 32-byte device key the box delivered in `/wellness/bind` (base64url, no padding).
 * The path is signed WITHOUT its query string; the body digest covers the exact bytes sent
 * (the gzip bytes for a compressed ingest), or the empty string for no body. K is a signing
 * key, never sent; a leaked header is good for one request within the box's 120 s window
 * and its 10-minute nonce memory. OriginSignerGoldenTest reproduces the box's own vectors
 * (Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json) byte for byte.
 *
 * Never log K, the header or the canonical string.
 */
class OriginSigner(
    private val enrollmentId: String,
    keyB64Url: String,
    private val random: SecureRandom = SecureRandom(),
) {
    private val key: ByteArray = decodeKey(keyB64Url)

    init {
        require(key.size == KEY_BYTES) { "device key must be $KEY_BYTES bytes" }
        require(enrollmentId.isNotBlank()) { "enrollment id required" }
    }

    /** The header value for one request. [nowSec] and [nonceHex] are injectable for tests. */
    fun header(
        method: String,
        pathWithQuery: String,
        body: ByteArray?,
        nowSec: Long,
        nonceHex: String = newNonce(random),
    ): String {
        val ts = nowSec
        val sig = signature(key, enrollmentId, ts, nonceHex, method, pathWithQuery, body ?: EMPTY)
        return "v1;enr=$enrollmentId;ts=$ts;nonce=$nonceHex;sig=$sig"
    }

    /**
     * The header value for a request whose body this phone never holds, only its digest: the
     * WebView Portal (AndroidBridge.wellnessSign) hashes the exact UTF-8 bytes it sends and
     * hands over [bodySha256Hex] (64 lowercase hex), so the body never crosses the bridge and
     * K never enters JavaScript.
     */
    fun headerForDigest(
        method: String,
        pathWithQuery: String,
        bodySha256Hex: String,
        nowSec: Long,
        nonceHex: String = newNonce(random),
    ): String {
        require(isWellFormedDigest(bodySha256Hex)) { "a body digest is 64 lowercase hex characters" }
        val sig = hmacHex(key, canonicalForDigest(enrollmentId, nowSec, nonceHex, method, pathWithQuery, bodySha256Hex))
        return "v1;enr=$enrollmentId;ts=$nowSec;nonce=$nonceHex;sig=$sig"
    }

    companion object {
        const val HEADER = "X-BlackBox-Origin"
        const val KEY_BYTES = 32
        private val EMPTY = ByteArray(0)
        private val NONCE_RE = Regex("[0-9a-f]{32}")
        private val DIGEST_RE = Regex("[0-9a-f]{64}")

        /** A SHA-256 as the canonical string carries it: 64 lowercase hex characters. */
        fun isWellFormedDigest(value: String): Boolean = DIGEST_RE.matches(value)

        /** 16 random bytes as 32 lowercase hex characters. */
        fun newNonce(random: SecureRandom = SecureRandom()): String {
            val bytes = ByteArray(16)
            random.nextBytes(bytes)
            return hex(bytes)
        }

        fun isWellFormedNonce(value: String): Boolean = NONCE_RE.matches(value)

        /** The path the signature covers: no query string, no fragment. */
        fun signedPath(pathWithQuery: String): String =
            pathWithQuery.substringBefore('#').substringBefore('?')

        fun canonical(enr: String, ts: Long, nonce: String, method: String, path: String, body: ByteArray): String =
            canonicalForDigest(enr, ts, nonce, method, path, sha256Hex(body))

        /** [canonical] with the body's SHA-256 (hex) already computed. */
        fun canonicalForDigest(enr: String, ts: Long, nonce: String, method: String, path: String, bodySha256Hex: String): String =
            "v1|$enr|$ts|$nonce|${method.uppercase()}|${signedPath(path)}|$bodySha256Hex"

        fun signature(
            key: ByteArray,
            enr: String,
            ts: Long,
            nonce: String,
            method: String,
            path: String,
            body: ByteArray,
        ): String = hmacHex(key, canonical(enr, ts, nonce, method, path, body))

        private fun hmacHex(key: ByteArray, canonical: String): String {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(key, "HmacSHA256"))
            return hex(mac.doFinal(canonical.toByteArray(Charsets.UTF_8)))
        }

        fun sha256Hex(bytes: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(bytes))

        /** Base64url without padding (the contract's key encoding); padding is tolerated on input. */
        fun decodeKey(b64url: String): ByteArray = Base64.getUrlDecoder().decode(b64url.trim().trimEnd('='))

        fun encodeKey(raw: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(raw)

        fun isWellFormedKey(b64url: String): Boolean =
            runCatching { decodeKey(b64url).size == KEY_BYTES }.getOrDefault(false)

        private fun hex(bytes: ByteArray): String {
            val out = StringBuilder(bytes.size * 2)
            for (b in bytes) {
                val v = b.toInt() and 0xff
                out.append(HEX[v ushr 4]).append(HEX[v and 0x0f])
            }
            return out.toString()
        }

        private val HEX = "0123456789abcdef".toCharArray()
    }
}
