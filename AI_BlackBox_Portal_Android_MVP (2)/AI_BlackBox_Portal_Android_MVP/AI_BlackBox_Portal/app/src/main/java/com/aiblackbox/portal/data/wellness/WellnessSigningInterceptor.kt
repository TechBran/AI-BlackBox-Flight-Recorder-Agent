package com.aiblackbox.portal.data.wellness

import okhttp3.Interceptor
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.Buffer

/**
 * Makes the enrolled phone's chat a credentialed surface (spec §4.1, §6.1 "Chat"): every
 * request the app's BlackBoxApi clients send to an allow-listed path gets `X-BlackBox-Origin`
 * when [WellnessChatRules.signingDecision] says Sign, computed by [OriginSigner] over the
 * EXACT bytes sent (method, path, SHA-256 of the body). Otherwise the request goes on as it
 * came, untouched.
 *
 * The body is read once into memory and the request is rebuilt on those same bytes, so the
 * signature can never cover a body other than the one sent. Not signed: a request that is
 * already signed (IngestClient signs its own), a body with a `Content-Encoding` (the gzip
 * bytes are produced below this layer, where the signature cannot reach them), a body of
 * unknown length (chunked, streamed), a one-shot or duplex body, and a body over
 * [MAX_SIGNED_BODY].
 *
 * [facts] reads the binding and the collector's state (null: Health is not wired in this
 * process, or the phone is no health source). The key never leaves this class: nothing here
 * logs, and the decision's toString leaves the key out.
 */
class WellnessSigningInterceptor(
    private val facts: () -> Facts?,
    private val nowSec: () -> Long = { System.currentTimeMillis() / 1000 },
    private val nonce: () -> String = { OriginSigner.newNonce() },
) : Interceptor {

    /** What the decision needs from the phone: its binding and the collector's view of the box. */
    class Facts(val binding: WellnessBindingRecord?, val state: WellnessSyncState?)

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val signed = runCatching { signedOrNull(request) }.getOrNull()
        return chain.proceed(signed ?: request)
    }

    /** [request] with the origin proof on the same body bytes, or null to send it exactly as it is. */
    fun signedOrNull(request: Request): Request? {
        if (request.header(OriginSigner.HEADER) != null) return null
        val path = request.url.encodedPath
        if (!WellnessChatRules.isSignable(request.method, path)) return null
        if (request.header("Content-Encoding") != null) return null
        val f = facts() ?: return null
        if (f.binding == null) return null
        val body = request.body
        val bytes: ByteArray? = when {
            body == null -> null
            body.isOneShot() || body.isDuplex() -> return null
            else -> {
                val length = body.contentLength()
                if (length < 0 || length > MAX_SIGNED_BODY) return null
                Buffer().also { body.writeTo(it) }.readByteArray()
            }
        }
        val operator = WellnessChatRules.operatorOf(bytes, request.url.queryParameter("operator"))
        val decision = WellnessChatRules.signingDecision(f.binding, f.state, operator, request.method, path)
            as? WellnessChatRules.SigningDecision.Sign ?: return null
        val header = OriginSigner(decision.enrollmentId, decision.key)
            .header(request.method, path, bytes ?: EMPTY, nowSec(), nonce())
        val builder = request.newBuilder().header(OriginSigner.HEADER, header)
        if (body != null && bytes != null) builder.method(request.method, bytes.toRequestBody(body.contentType()))
        return builder.build()
    }

    companion object {
        /** A chat body is text (attachments travel as URLs); anything larger goes unsigned. */
        const val MAX_SIGNED_BODY = 4L * 1024 * 1024
        private val EMPTY = ByteArray(0)
    }
}
