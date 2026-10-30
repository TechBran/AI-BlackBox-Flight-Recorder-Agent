package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.api.HttpClients
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * The phone's HTTP calls to the box's `/wellness/` routes (wire contract). Signed calls
 * carry `X-BlackBox-Origin` from [OriginSigner] over the EXACT bytes sent; the ingest body is
 * gzip with `Content-Encoding: gzip`. Enrollment is the one unsigned POST (there is no key
 * yet). Answers come back as [WellnessHttp] (status -1 = no answer), never as exceptions, so
 * the outbox table and the UI handle every outcome the same way.
 *
 * The key signs only for the box that delivered it ([keyPinned], contract (f)): a request that
 * is to be signed but goes to any other box (the app was re-pointed) is not sent at all, signed
 * or unsigned (status -1, [OTHER_BOX]): an ingest body is health data, and a proof would be live
 * for 120 s at the box that enrolled this phone. Unsigned calls (enroll, the provider list) are
 * not affected.
 *
 * Never logs a body, a header or a key.
 */
class IngestClient(
    private val baseUrl: String,
    base: OkHttpClient = HttpClients.base,
    private val nowSec: () -> Long = { System.currentTimeMillis() / 1000 },
    /**
     * May this phone's key sign a request to the given URL? WellnessGraph wires
     * WellnessBinding.mayUseKeyFor with the paired box; the default signs nothing.
     */
    private val keyPinned: (requestUrl: String) -> Boolean = { false },
) {
    /** The box this client talks to, `scheme://host[:port]` (the enroll call's box: the bind is pinned to it). */
    val origin: String? get() = WellnessChatRules.originOf(baseUrl)

    private val client: OkHttpClient = base.newBuilder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    private val jsonType = "application/json; charset=utf-8".toMediaType()

    /**
     * Seconds to add to this phone's clock when signing. The box refuses a signature more than
     * 120 s from its own clock (and says only "bad_signature"), so a phone whose clock drifted
     * would look like it needs reconnecting. Every reply's `Date` header keeps this current;
     * only a drift of more than [SKEW_TOLERANCE_S] is corrected.
     */
    @Volatile var clockSkewS: Long = 0L
        private set

    /** POST /wellness/ingest with a gzip body (already compressed, from the outbox). */
    suspend fun ingest(gzipBody: ByteArray, signer: OriginSigner): WellnessHttp =
        send("POST", INGEST, gzipBody, signer, gzip = true)

    suspend fun capabilities(report: String, signer: OriginSigner): WellnessHttp =
        send("POST", CAPABILITIES, report.toByteArray(Charsets.UTF_8), signer)

    /** POST /wellness/enroll (unsigned: the key does not exist yet). */
    suspend fun enroll(body: String): WellnessHttp = send("POST", ENROLL, body.toByteArray(Charsets.UTF_8), null)

    /** GET /wellness/status; signed it returns the credentialed fields. */
    suspend fun status(operator: String, signer: OriginSigner?): WellnessHttp =
        send("GET", "$STATUS?operator=${enc(operator)}", null, signer)

    suspend fun putSettings(body: String, signer: OriginSigner?): WellnessHttp =
        send("PUT", SETTINGS, body.toByteArray(Charsets.UTF_8), signer)

    /** pause / resume / disconnect / erase: POST with a small JSON body. */
    suspend fun action(path: String, body: String, signer: OriginSigner?): WellnessHttp =
        send("POST", path, body.toByteArray(Charsets.UTF_8), signer)

    suspend fun get(pathWithQuery: String, signer: OriginSigner?): WellnessHttp =
        send("GET", pathWithQuery, null, signer)

    suspend fun send(
        method: String,
        pathWithQuery: String,
        body: ByteArray?,
        signer: OriginSigner?,
        gzip: Boolean = false,
    ): WellnessHttp = withContext(Dispatchers.IO) {
        try {
            val url = baseUrl.trimEnd('/') + pathWithQuery
            if (signer != null && !runCatching { keyPinned(url) }.getOrDefault(false)) {
                return@withContext WellnessHttp(status = -1, body = "", error = OTHER_BOX)
            }
            val builder = Request.Builder()
                .url(url)
                .header("X-BlackBox-Client", "native-android/1.0")
            if (signer != null) {
                builder.header(OriginSigner.HEADER, signer.header(method, pathWithQuery, body, nowSec() + clockSkewS))
            }
            if (gzip) builder.header("Content-Encoding", "gzip")
            val requestBody = body?.toRequestBody(jsonType)
            builder.method(method, if (method == "GET") null else (requestBody ?: ByteArray(0).toRequestBody(jsonType)))
            client.newCall(builder.build()).execute().use { response ->
                response.headers.getDate("Date")?.let { boxDate -> clockSkewS = skewFor(boxDate.time / 1000, nowSec()) }
                WellnessHttp(
                    status = response.code,
                    body = response.body?.string().orEmpty().take(MAX_REPLY_CHARS),
                    retryAfterS = response.header("Retry-After")?.trim()?.toLongOrNull(),
                    policyVersion = response.header("X-Policy-Version"),
                )
            }
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            WellnessHttp(status = -1, body = "", error = e.javaClass.simpleName)
        }
    }

    companion object {
        const val ENROLL = "/wellness/enroll"
        const val INGEST = "/wellness/ingest"
        const val CAPABILITIES = "/wellness/capabilities"
        const val STATUS = "/wellness/status"
        const val SETTINGS = "/wellness/settings"
        const val PAUSE = "/wellness/pause"
        const val RESUME = "/wellness/resume"
        const val DISCONNECT = "/wellness/disconnect"
        const val ERASE = "/wellness/erase"
        const val DISCLOSURES = "/wellness/disclosures"
        const val REPLACE_APPROVE = "/wellness/replace-approve"
        const val PRIVACY_POLICY = "/wellness/privacy-policy"
        private const val MAX_REPLY_CHARS = 1_000_000

        /** A signed call not sent: the key belongs to another box than this client's ([keyPinned]). */
        const val OTHER_BOX = "other_box"

        /** Drift under this is left alone (the box allows 120 s; HTTP dates have 1 s grain). */
        const val SKEW_TOLERANCE_S = 30L

        /** The correction to apply given the box's clock and ours (0 inside the tolerance). PURE. */
        fun skewFor(boxSec: Long, phoneSec: Long): Long {
            val drift = boxSec - phoneSec
            return if (kotlin.math.abs(drift) > SKEW_TOLERANCE_S) drift else 0L
        }

        private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")
    }
}

data class WellnessHttp(
    val status: Int,
    val body: String,
    val retryAfterS: Long? = null,
    val policyVersion: String? = null,
    val error: String? = null,
) {
    val ok: Boolean get() = status in 200..299
    val reached: Boolean get() = status > 0
}
