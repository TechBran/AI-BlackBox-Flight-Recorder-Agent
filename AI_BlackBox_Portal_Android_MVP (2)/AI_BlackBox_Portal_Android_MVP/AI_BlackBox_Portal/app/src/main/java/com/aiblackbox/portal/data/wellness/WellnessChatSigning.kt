package com.aiblackbox.portal.data.wellness

import android.content.Context

/**
 * The process's one chat-signing interceptor (on [com.aiblackbox.portal.data.api.HttpClients.base],
 * so every client the app derives from it carries it; it signs only the requests that opt in,
 * [WellnessSigningInterceptor.optIn]) and the WebView bridge's signer, both reading this
 * phone's binding, the collector's state and the paired box through [WellnessGraph].
 *
 * Until [install] runs (PortalApplication.onCreate) there is nothing to sign with, and every
 * request goes out as it always has: the JVM tests that never install it see no change.
 */
object WellnessChatSigning {

    @Volatile private var facts: (() -> WellnessSigningInterceptor.Facts?)? = null
    @Volatile private var skewS: () -> Long = { 0L }

    /** On [com.aiblackbox.portal.data.api.HttpClients.base]: a no-op for every request that does not opt in. */
    val interceptor: WellnessSigningInterceptor = WellnessSigningInterceptor(
        facts = { facts?.invoke() },
        nowSec = { nowSec() },
    )

    /** Wire this process's binding and state in. Idempotent. */
    fun install(context: Context) {
        val app = context.applicationContext
        facts = {
            val binding = WellnessGraph.binding(app)
            WellnessSigningInterceptor.Facts(binding.current(), WellnessGraph.state(app).load(),
                WellnessGraph.boxOrigin(app), adoptOrigin = { origin -> binding.adoptBoxOrigin(origin) })
        }
        // The box's clock as the collector last learned it (IngestClient.clockSkewS): a phone whose
        // clock drifted past the box's 120 s window would otherwise sign every chat turn in vain.
        skewS = { WellnessGraph.client(app)?.clockSkewS ?: 0L }
    }

    /** Tests: wire facts without a phone's files (null = not installed, as before [install]). */
    @androidx.annotation.VisibleForTesting
    internal fun installForTest(source: (() -> WellnessSigningInterceptor.Facts?)?) {
        facts = source
        skewS = { 0L }
    }

    private fun nowSec(): Long = System.currentTimeMillis() / 1000 + runCatching { skewS() }.getOrDefault(0L)

    /**
     * The in-app Portal's signer ([WellnessChatRules.BRIDGE_CHANNEL]; [WellnessChatRules.bridgeHeader]
     * over this phone's facts): the header value, or null. [portalOperator] is the operator the
     * in-app Portal is on. The caller has checked the message came from the paired box's main
     * frame ([WellnessChatRules.bridgeCallerAllowed]), so the request goes to the paired box; the
     * key still signs only when the binding was made with that box (contract (f): an app
     * re-pointed at another box gives its Portal no proof). An older binding adopts the paired
     * box only for a request that gets a header (decide first, adopt last), never for one the
     * bridge's rules refuse.
     */
    fun bridgeHeader(portalOperator: String?, method: String, pathWithQuery: String, bodySha256Hex: String): String? {
        val f = runCatching { facts?.invoke() }.getOrNull() ?: return null
        val paired = f.boxOrigin ?: return null
        if (f.binding == null) return null
        val use = WellnessSigningInterceptor.keyUse(f, paired)
        if (use is WellnessChatRules.KeyUse.Refused) return null
        val header = WellnessChatRules.bridgeHeader(f.binding, f.state, portalOperator, method, pathWithQuery, bodySha256Hex,
            nowSec(), OriginSigner.newNonce()) ?: return null
        return header.takeIf { WellnessSigningInterceptor.commitKeyUse(f, use) }
    }
}
