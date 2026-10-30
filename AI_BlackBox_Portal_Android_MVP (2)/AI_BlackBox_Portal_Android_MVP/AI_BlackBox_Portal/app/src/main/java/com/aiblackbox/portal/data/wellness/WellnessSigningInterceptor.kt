package com.aiblackbox.portal.data.wellness

import okhttp3.Interceptor
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.Buffer

/**
 * Makes the enrolled phone's chat a credentialed surface (spec §4.1, §6.1 "Chat"): a request
 * that OPTS IN ([optIn]) and goes to an allow-listed path on the box this app is paired with
 * gets `X-BlackBox-Origin` when [WellnessChatRules.signingDecision] says Sign, computed by
 * [OriginSigner] over the EXACT bytes sent (method, path, SHA-256 of the body). Otherwise the
 * request goes on as it came, untouched.
 *
 * Opt-in, not every client derived from HttpClients.base: only the surfaces that show a
 * health turn as one (the native chat with its footer and Show numbers, Show numbers' own
 * read, and the Timeline's and the SNAP peek sheet's ledger read) ask for the credential. The WebView's download client (it writes what it fetches
 * to shared Downloads, at a URL the page chose) and the overlay's /chat (it cannot render a
 * masked health turn) never carry the tag, so they never sign. The tag is an OkHttp tag: it
 * never reaches the wire.
 *
 * The body is read once into memory and the request is rebuilt on those same bytes, so the
 * signature can never cover a body other than the one sent. Not signed: a request that is
 * already signed (IngestClient signs its own), a body with a `Content-Encoding` (the gzip
 * bytes are produced below this layer, where the signature cannot reach them), a body of
 * unknown length (chunked, streamed), a one-shot or duplex body, and a body over
 * [MAX_SIGNED_BODY].
 *
 * [facts] reads the binding, the collector's state and the paired box (null: Health is not
 * wired in this process, or the phone is no health source). The key never leaves this class:
 * nothing here logs, and the decision's toString leaves the key out.
 */
class WellnessSigningInterceptor(
    private val facts: () -> Facts?,
    private val nowSec: () -> Long = { System.currentTimeMillis() / 1000 },
    private val nonce: () -> String = { OriginSigner.newNonce() },
) : Interceptor {

    /**
     * What the decision needs from the phone: its binding, the collector's view of the box,
     * and the origin of the box this app is paired with ([boxOrigin]; null = unpaired, so
     * nothing is signed). A request to any other host is never signed: a proof is live for
     * 120 s at the box that enrolled this phone, whoever receives it. Nor is a request to the
     * paired box when the binding belongs to another one (the app was re-pointed:
     * [WellnessChatRules.keyUse]); a binding older than its `box_origin` is pinned to the
     * paired box on its first signed use through [adoptOrigin] (WellnessBinding.adoptBoxOrigin;
     * false, the default, = it could not be, so nothing is signed). Only a request that is
     * really signed adopts ([commitKeyUse], the last step): one that goes out unsigned anyway
     * (another operator's, a revoked state's) never pins a binding to the box it went to.
     */
    class Facts(
        val binding: WellnessBindingRecord?,
        val state: WellnessSyncState?,
        val boxOrigin: String?,
        val adoptOrigin: (String) -> Boolean = { false },
    )

    /** The opt-in tag ([optIn]). */
    object SignHealth

    /** The operator on the app's screen, for a request that names none itself (the ledger read; [optIn]). */
    class ForOperator(val operator: String) {
        override fun toString(): String = "ForOperator"
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val signed = runCatching { signedOrNull(request) }.getOrNull()
        return chain.proceed(signed ?: request)
    }

    /** [request] with the origin proof on the same body bytes, or null to send it exactly as it is. */
    fun signedOrNull(request: Request): Request? {
        if (request.tag(SignHealth::class.java) == null) return null
        if (request.header(OriginSigner.HEADER) != null) return null
        val path = request.url.encodedPath
        if (!WellnessChatRules.isSignable(request.method, path)) return null
        if (request.header("Content-Encoding") != null) return null
        val f = facts() ?: return null
        if (f.binding == null) return null
        val use = keyUse(f, request.url.toString())
        if (use is WellnessChatRules.KeyUse.Refused) return null
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
        val operator = WellnessChatRules.requestOperator(path, bytes, request.url.queryParameter("operator"),
            request.tag(ForOperator::class.java)?.operator)
        val decision = WellnessChatRules.signingDecision(f.binding, f.state, operator, request.method, path)
            as? WellnessChatRules.SigningDecision.Sign ?: return null
        // Decided: this request is signed. Only now may an older binding be pinned to its box.
        if (!commitKeyUse(f, use)) return null
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

        /**
         * Ask for the credential on this request (the native chat, Show numbers and the ledger
         * read only). [operator] is the operator on the app's screen, for the ledger read, whose
         * request names none ([WellnessChatRules.requestOperator]); it never overrides the
         * operator a body or query names.
         */
        fun optIn(builder: Request.Builder, operator: String? = null): Request.Builder =
            builder.tag(SignHealth::class.java, SignHealth)
                .let { b -> operator?.takeIf { it.isNotBlank() }?.let { b.tag(ForOperator::class.java, ForOperator(it)) } ?: b }

        /** Where [f]'s key may go for [requestUrl] ([WellnessChatRules.keyUse]). No side effect. */
        fun keyUse(f: Facts, requestUrl: String): WellnessChatRules.KeyUse =
            WellnessChatRules.keyUse(f.binding?.boxOrigin, requestUrl, f.boxOrigin)

        /**
         * The last step before a proof goes out, once everything else has decided to sign:
         * Pinned signs; Adopt pins a binding without `box_origin` to the paired box now (once,
         * [Facts.adoptOrigin]) and signs only if that stuck; Refused never signs. Decide first,
         * adopt last: contract (f) pins an older binding to "the paired origin of its first
         * signed use", never to a box that only saw an unsigned request.
         */
        fun commitKeyUse(f: Facts, use: WellnessChatRules.KeyUse): Boolean = when (use) {
            WellnessChatRules.KeyUse.Pinned -> true
            is WellnessChatRules.KeyUse.Adopt -> runCatching { f.adoptOrigin(use.origin) }.getOrDefault(false)
            is WellnessChatRules.KeyUse.Refused -> false
        }
    }
}
