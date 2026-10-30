package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * What the enrolled phone's chat may sign and what it shows the person about their health data
 * (spec §6.1 "Chat", §6.3, §3.7), PURE (no Android types), the WellnessRules pattern: the
 * interceptor, the WebView bridge, the chat bubble and the Show numbers sheet only ask.
 * Tested by WellnessChatRulesTest.
 *
 * SIGNING. A request carries `X-BlackBox-Origin` only when it goes to an allow-listed path
 * with that path's method, this phone holds a binding the box knows as live
 * ([SIGNING_STATES]), the box has not refused its key, no Disconnect or erase is waiting on
 * the box, the request is for the enrolled operator, and it goes to the box that delivered the
 * bind while the app is still paired with it ([keyUse]). Everything else goes out unsigned,
 * byte-identical to a build without Health. The box decides on its own as well (it grants a
 * chat turn the credential only when the proof is the body operator's enrolled phone): this
 * rule only keeps the phone from spending its key where it cannot count.
 */
object WellnessChatRules {

    // ── which requests carry the origin proof ──────────────────────────────────────────────

    const val CHAT_STREAM = "/chat/stream"
    const val CHAT = "/chat"
    const val REF_PREFIX = "/wellness/ref/"

    /**
     * The ledger read (`GET /fossil/snapshot/<id>`, Timeline and the SNAP peek sheet). Signed, the
     * box reads the snapshot as the enrolled operator's own screen: a health turn of theirs comes
     * back masked ("[·]") with its WELLNESS gauge, so Show numbers can offer its refs; unsigned
     * (or for anyone else's turn) it stays "[wellness conversation · not shared]".
     */
    const val LEDGER_PREFIX = "/fossil/snapshot/"

    /** The exact paths (no query) that may be signed, each with the one method it takes. */
    private val SIGNED_EXACT: Map<String, String> = mapOf(
        CHAT_STREAM to "POST",
        CHAT to "POST",
        IngestClient.STATUS to "GET",
        IngestClient.DISCLOSURES to "GET",
    )

    /** A delivery ref as the box mints them (api.new_ref): `w-<yyyymmdd>-<hex>`. */
    val REF_RE = Regex("w-[0-9]{8}-[0-9a-f]{4,16}")

    /** A snapshot id as the box takes it: `SNAP-<yyyymmdd>-<n>`, or the short `SNAP-<n>` models cite. */
    val SNAP_ID_RE = Regex("SNAP-(?:[0-9]{8}-)?[0-9]{1,12}")

    /**
     * May [method] [path] carry the proof? [path] is the request path without its query: one
     * of the exact paths with its method, `GET /wellness/ref/<one ref>` (Show numbers), or
     * `GET /fossil/snapshot/<one snapshot id>` (a turn reloaded from the ledger). Nothing else,
     * so no other route ever receives a signature from the chat side.
     */
    fun isSignable(method: String, path: String): Boolean {
        val m = method.trim().uppercase(Locale.ROOT)
        SIGNED_EXACT[path]?.let { return it == m }
        if (m != "GET") return false
        if (path.startsWith(REF_PREFIX)) return REF_RE.matches(path.removePrefix(REF_PREFIX))
        return isLedgerRead(path)
    }

    /** Is [path] (no query) the ledger read of one snapshot? */
    fun isLedgerRead(path: String): Boolean =
        path.startsWith(LEDGER_PREFIX) && SNAP_ID_RE.matches(path.removePrefix(LEDGER_PREFIX))

    /** `/fossil/snapshot/<id>`, with no query (the native app never sends one), or null for an id the box would not take. */
    fun ledgerPath(snapId: String): String? = snapId.trim().takeIf { SNAP_ID_RE.matches(it) }?.let { LEDGER_PREFIX + it }

    /**
     * The box refused the proof itself (401 bad_signature, 409 owner_changed, 410 revoked or
     * source_moved; never downgraded to an unsigned read on its side). The ledger read then asks
     * once more unsigned, so the Timeline never shows less than it did before Health.
     */
    fun proofRefused(status: Int): Boolean = status == 401 || status == 409 || status == 410

    /** The enrollment states in which the box's gate answers from this phone's data (gate.GATE_STATES). */
    val SIGNING_STATES: Set<WellnessRules.EnrollmentState> = setOf(
        WellnessRules.EnrollmentState.ACTIVE,
        WellnessRules.EnrollmentState.STALE,
        WellnessRules.EnrollmentState.PAUSED,
        WellnessRules.EnrollmentState.NEEDS_ATTENTION,
    )

    const val UNSIGNED_PATH = "path_not_allowed"
    const val UNSIGNED_NOT_ENROLLED = "not_enrolled"
    const val UNSIGNED_NO_OPERATOR = "no_operator"
    const val UNSIGNED_OTHER_OPERATOR = "other_operator"
    const val UNSIGNED_NOT_LIVE = "not_live"
    const val UNSIGNED_KEY_REFUSED = "key_refused"
    const val UNSIGNED_STOPPING = "stopping"

    sealed class SigningDecision {
        /** Sign with this enrollment's key. [toString] never prints the key. */
        class Sign(val enrollmentId: String, val key: String) : SigningDecision() {
            override fun toString(): String = "Sign(enr=$enrollmentId)"
            override fun equals(other: Any?): Boolean =
                other is Sign && other.enrollmentId == enrollmentId && other.key == key
            override fun hashCode(): Int = enrollmentId.hashCode()
        }

        data class Unsigned(val reason: String) : SigningDecision()
    }

    /**
     * The signing decision for one request: [binding] is this phone's enrollment (null when
     * it is not a health source), [state] the collector's last view of the box's state,
     * [operator] the operator the request is for ([operatorOf]), [method] and [path] the
     * request's (path without query).
     */
    fun signingDecision(
        binding: WellnessBindingRecord?,
        state: WellnessSyncState?,
        operator: String?,
        method: String,
        path: String,
    ): SigningDecision = when {
        !isSignable(method, path) -> SigningDecision.Unsigned(UNSIGNED_PATH)
        binding == null -> SigningDecision.Unsigned(UNSIGNED_NOT_ENROLLED)
        operator.isNullOrBlank() -> SigningDecision.Unsigned(UNSIGNED_NO_OPERATOR)
        operator.trim() != binding.operator -> SigningDecision.Unsigned(UNSIGNED_OTHER_OPERATOR)
        state == null || state.enrollmentState() !in SIGNING_STATES -> SigningDecision.Unsigned(UNSIGNED_NOT_LIVE)
        state.needsReconnect -> SigningDecision.Unsigned(UNSIGNED_KEY_REFUSED)
        state.localHold == WellnessRules.HOLD_DISCONNECT || state.localHold == WellnessRules.HOLD_ERASE ->
            SigningDecision.Unsigned(UNSIGNED_STOPPING)
        else -> SigningDecision.Sign(binding.enrollmentId, binding.k)
    }

    /**
     * Is [url] on the box at [boxOrigin] (same scheme, host and port)? False when either does
     * not parse or [boxOrigin] is null (unpaired): a request to any other host never carries a
     * proof, which would be live for 120 s at the box that enrolled this phone.
     */
    fun sameOrigin(url: String?, boxOrigin: String?): Boolean {
        val a = url?.toHttpUrlOrNull() ?: return false
        val b = boxOrigin?.toHttpUrlOrNull() ?: return false
        return a.scheme == b.scheme && a.host == b.host && a.port == b.port
    }

    /** `scheme://host[:port]` of [url] (the port only when it is not the scheme's default), or null. */
    fun originOf(url: String?): String? {
        val u = url?.trim()?.toHttpUrlOrNull() ?: return null
        val host = if (':' in u.host) "[${u.host}]" else u.host
        val port = if (u.port == HttpUrl.defaultPort(u.scheme)) "" else ":${u.port}"
        return u.scheme + "://" + host + port
    }

    /** May this phone's key sign a request to a given box ([keyUse])? */
    sealed class KeyUse {
        /** The binding is pinned to the paired box and the request goes there: sign. */
        data object Pinned : KeyUse()

        /**
         * A binding written before `box_origin` existed (wave 3 and earlier): it was made with
         * the box the app was paired with then, which is the one it is paired with now unless
         * the app was re-pointed in between. It adopts [origin], the paired box of its first
         * signed use, and is rewritten once (WellnessBinding.adoptBoxOrigin); then it is Pinned.
         */
        data class Adopt(val origin: String) : KeyUse()

        data class Refused(val reason: String) : KeyUse()
    }

    const val KEY_UNPAIRED = "unpaired"
    const val KEY_OTHER_HOST = "other_host"
    const val KEY_OTHER_BOX = "other_box"

    /**
     * Where this phone's key may go (contract (f)): only to the box that delivered the bind
     * ([bindingOrigin], the binding's `box_origin`), only while the app is still paired with it
     * ([pairedOrigin]), and only for a request that goes there ([requestUrl]). An app re-pointed
     * at another box (the same operator name can exist there) therefore never hands that box a
     * proof, which would be live for 120 s at the box that enrolled this phone.
     */
    fun keyUse(bindingOrigin: String?, requestUrl: String?, pairedOrigin: String?): KeyUse {
        val paired = originOf(pairedOrigin) ?: return KeyUse.Refused(KEY_UNPAIRED)
        if (!sameOrigin(requestUrl, paired)) return KeyUse.Refused(KEY_OTHER_HOST)
        if (bindingOrigin.isNullOrBlank()) return KeyUse.Adopt(paired)
        return if (sameOrigin(bindingOrigin, paired)) KeyUse.Pinned else KeyUse.Refused(KEY_OTHER_BOX)
    }

    private val JSON = Json { ignoreUnknownKeys = true; isLenient = true }

    /**
     * The operator a request is for: the JSON body's top-level `operator` when the request
     * has a body (the chat routes read it there), else the `operator` query parameter (the
     * status and disclosure reads, and Show numbers, which names the snapshot's operator).
     * Null when the request names none.
     */
    fun operatorOf(body: ByteArray?, queryOperator: String?): String? {
        if (body != null && body.isNotEmpty()) {
            val obj = runCatching { JSON.parseToJsonElement(body.toString(Charsets.UTF_8)) as? JsonObject }.getOrNull()
            return (obj?.get("operator") as? JsonPrimitive)?.takeIf { it.isString }?.content
        }
        return queryOperator
    }

    /**
     * The operator a request to [path] (no query) is for, as the interceptor decides it. A ledger
     * read has no body and, from the native app, no query: it is for the operator on the app's
     * screen ([screenOperator], the one its Timeline and chat are on), since the box answers a
     * signed read as the proof's operator's own screen. A query naming someone else makes it
     * nobody's. Every other path: [operatorOf] (a screen operator never overrides a body or query).
     */
    fun requestOperator(path: String, body: ByteArray?, queryOperator: String?, screenOperator: String?): String? {
        if (!isLedgerRead(path)) return operatorOf(body, queryOperator)
        val screen = screenOperator?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        return screen.takeIf { queryOperator == null || queryOperator.trim() == it }
    }

    /** The `operator` query parameter of [pathWithQuery] (decoded), or null. */
    fun queryOperator(pathWithQuery: String): String? =
        "http://bridge.invalid$pathWithQuery".toHttpUrlOrNull()?.queryParameter("operator")

    // ── the in-app Portal's signer (spec §6.3) ─────────────────────────────────────────────

    /**
     * The WebView message channel the in-app Portal signs through:
     * `WebViewCompat.addWebMessageListener(web, BRIDGE_CHANNEL, setOf(<paired origin>), …)`.
     * Not an `addJavascriptInterface` method: those are injected into EVERY frame, the
     * sandboxed app-preview iframe that runs registered apps' code among them, and cannot tell
     * which frame called. The channel object exists only in frames of the paired origin (an
     * opaque-origin iframe never matches), and each message says whether it came from the main
     * frame.
     *
     * The contract with the Portal: `window.BlackBoxWellness.postMessage(JSON.stringify({id,
     * method, path, sha256}))` (`path` with its query, `sha256` the lowercase hex SHA-256 of the
     * exact UTF-8 body bytes, of the empty string when there is none); the answer arrives as a `message` event on
     * `window.BlackBoxWellness` whose `data` is `JSON.stringify({id, value})`, `value` the
     * X-BlackBox-Origin header or null. Every well-formed request gets exactly one answer.
     */
    const val BRIDGE_CHANNEL = "BlackBoxWellness"

    /** One sign request from the Portal ([parseBridgeRequest]); [id] is echoed as it came (a string or an integer). */
    data class BridgeSignRequest(val id: JsonPrimitive, val method: String, val pathWithQuery: String, val bodySha256Hex: String)

    private val BRIDGE_ID_RE = Regex("[A-Za-z0-9_.:-]{1,64}")
    private const val MAX_BRIDGE_MESSAGE = 2048

    /** [message] as a sign request, or null (not JSON, a field missing or of the wrong type, too long): no answer. */
    fun parseBridgeRequest(message: String?): BridgeSignRequest? {
        if (message == null || message.length > MAX_BRIDGE_MESSAGE) return null
        val obj = runCatching { JSON.parseToJsonElement(message) as? JsonObject }.getOrNull() ?: return null
        val id = (obj["id"] as? JsonPrimitive)?.takeIf { p ->
            if (p.isString) BRIDGE_ID_RE.matches(p.content) else p.content.toLongOrNull() != null
        } ?: return null
        val method = obj.str("method") ?: return null
        val path = (obj["path"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        val sha = obj.str("sha256") ?: return null
        return BridgeSignRequest(id, method, path, sha)
    }

    /** The answer to [id]: `{"id": <id>, "value": <header or null>}`. */
    fun bridgeReply(id: JsonPrimitive, value: String?): String =
        JsonObject(mapOf("id" to id, "value" to (value?.let(::JsonPrimitive) ?: kotlinx.serialization.json.JsonNull))).toString()

    /**
     * May a sign request be answered with a proof? Only from the paired box's MAIN frame:
     * [sourceOrigin] is the framework's account of the sender (not the page's word) and must be
     * [pairedOrigin], [isMainFrame] must hold, and the page the WebView shows must be the
     * paired box ([pageOrigin]). A same-origin iframe, an opaque-origin one ("null") and a
     * page that has navigated off the box never are.
     */
    fun bridgeCallerAllowed(sourceOrigin: String?, isMainFrame: Boolean, pairedOrigin: String?, pageOrigin: String?): Boolean =
        isMainFrame && pairedOrigin != null && sameOrigin(sourceOrigin, pairedOrigin) && sameOrigin(pageOrigin, pairedOrigin)

    /**
     * The operator a Portal request is for. A POST is for [portalOperator], the operator the
     * in-app Portal is on (the box reads the body's operator, which the bridge never sees; a
     * query naming another is ignored). A GET must name exactly one `operator` in its query,
     * and it must be [portalOperator]. Null (no proof) otherwise, and when the Portal's
     * operator is unknown.
     */
    fun bridgeOperator(method: String, pathWithQuery: String, portalOperator: String?): String? {
        val portal = portalOperator?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        if (method.trim().uppercase(Locale.ROOT) != "GET") return portal
        val named = "http://bridge.invalid$pathWithQuery".toHttpUrlOrNull()?.queryParameterValues("operator") ?: return null
        return named.singleOrNull()?.trim()?.takeIf { it == portal }
    }

    /**
     * The X-BlackBox-Origin value for a request the in-app Portal is about to send, or null.
     * Same decision as the interceptor's, for the operator [bridgeOperator] names; the Portal
     * hands over only the method, the path with its query and the SHA-256 of the exact UTF-8
     * body bytes, never the body. The caller has checked [bridgeCallerAllowed].
     */
    fun bridgeHeader(
        binding: WellnessBindingRecord?,
        state: WellnessSyncState?,
        portalOperator: String?,
        method: String,
        pathWithQuery: String,
        bodySha256Hex: String,
        nowSec: Long,
        nonceHex: String,
    ): String? {
        val m = method.trim().uppercase(Locale.ROOT)
        val digest = bodySha256Hex.trim().lowercase(Locale.ROOT)
        if (!OriginSigner.isWellFormedDigest(digest)) return null
        if (!pathWithQuery.startsWith("/") || pathWithQuery.length > MAX_BRIDGE_PATH) return null
        val path = OriginSigner.signedPath(pathWithQuery)
        val operator = bridgeOperator(m, pathWithQuery, portalOperator) ?: return null
        val decision = signingDecision(binding, state, operator, m, path) as? SigningDecision.Sign ?: return null
        return runCatching {
            OriginSigner(decision.enrollmentId, decision.key).headerForDigest(m, path, digest, nowSec, nonceHex)
        }.getOrNull()
    }

    private const val MAX_BRIDGE_PATH = 512

    // ── the turn's health use (the UI-only wellness_used frame) ────────────────────────────

    /**
     * One `wellness_used` frame: `{ref, categories, as_of, provider, provider_label}`. Null
     * when it is not an object or names no well-formed ref. Unknown fields are ignored.
     */
    fun parseUsedFrame(data: String): WellnessUsed? {
        val obj = runCatching { JSON.parseToJsonElement(data).jsonObject }.getOrNull() ?: return null
        val ref = obj.str("ref")?.takeIf { REF_RE.matches(it) } ?: return null
        val cats = (obj["categories"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }
            ?.filter { it.isNotBlank() }.orEmpty()
        return WellnessUsed(
            ref = ref,
            refs = listOf(ref),
            categories = cats.distinct(),
            asOf = obj.str("as_of"),
            providerLabel = obj.str("provider_label")?.let(::label) ?: obj.str("provider")?.let(::label),
        )
    }

    /** Fold a further frame of the same turn into what the turn already used. */
    fun merge(prev: WellnessUsed?, next: WellnessUsed): WellnessUsed {
        if (prev == null) return next
        return prev.copy(
            refs = (prev.refs + next.refs).distinct(),
            categories = (prev.categories + next.categories).distinct(),
            asOf = next.asOf ?: prev.asOf,
            providerLabel = next.providerLabel ?: prev.providerLabel,
        )
    }

    // ── the footer ─────────────────────────────────────────────────────────────────────────

    const val FOOTER_LEAD = "Used your health data"
    const val SOURCE_FALLBACK = "your phone"

    /**
     * "Used your health data · <source> · as of <time or date> · <provider>": calm, and no
     * health number in it. [sourceLabel] is the source phone's label from the last signed
     * status (else "your phone"); [nowMs] and [zone] decide time-or-date.
     */
    fun footerLine(
        used: WellnessUsed,
        sourceLabel: String?,
        nowMs: Long,
        zone: ZoneId,
        use24Hour: Boolean,
        locale: Locale = Locale.getDefault(),
    ): String {
        val parts = mutableListOf(FOOTER_LEAD, sourceLabel?.let(::label) ?: SOURCE_FALLBACK)
        asOfLabel(used.asOf, nowMs, zone, use24Hour, locale)?.let { parts += "as of $it" }
        used.providerLabel?.let(::label)?.let { parts += it }
        return parts.joinToString(" · ")
    }

    /**
     * The footer's source: this phone's label only when the chat is for the operator this phone
     * is the health source of. Another operator's turn (household mode lets one through) used
     * THEIR source, which this phone cannot name, so the footer says "your phone" generically.
     */
    fun footerSource(chatOperator: String?, enrolledOperator: String?, sourceLabel: String?): String? =
        sourceLabel?.takeIf { !enrolledOperator.isNullOrBlank() && chatOperator?.trim() == enrolledOperator }

    /**
     * The box's `as_of` (ISO-8601 with an offset, minute grain) in [zone]: the clock time when
     * it is today there, else the date ("Oct 27"; with the year when it is not this year).
     * Null when absent or unreadable.
     */
    fun asOfLabel(asOf: String?, nowMs: Long, zone: ZoneId, use24Hour: Boolean, locale: Locale = Locale.getDefault()): String? {
        val instant = parseInstant(asOf) ?: return null
        val local = instant.atZone(zone)
        val today = Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate()
        val pattern = when {
            local.toLocalDate() == today -> if (use24Hour) "HH:mm" else "h:mm a"
            local.year == today.year -> "MMM d"
            else -> "MMM d, yyyy"
        }
        return local.format(DateTimeFormatter.ofPattern(pattern, locale))
    }

    fun parseInstant(text: String?): Instant? {
        val t = text?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        return runCatching { OffsetDateTime.parse(t, DateTimeFormatter.ISO_OFFSET_DATE_TIME).toInstant() }.getOrNull()
            ?: runCatching { Instant.parse(t) }.getOrNull()
    }

    private val LABEL_JUNK = Regex("[\\u0000-\\u001f\\u007f·]+")

    /** A label for one footer slot: no control characters or separators, one line, at most 60 characters. */
    fun label(text: String): String? =
        LABEL_JUNK.replace(text, " ").split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
            .take(60).trim().takeIf { it.isNotEmpty() }

    // ── reading a reply aloud ──────────────────────────────────────────────────────────────

    /**
     * May a reply be read aloud? Not when it used the person's health data ([usedHealth]), in
     * v1, with any voice, automatically or by a tap (contract (e), the same rule as the
     * Portal's). Every cloud voice is a service the health settings never allowed (rule W31:
     * they name which AI may see the data, never a voice), a "local" voice server is one the
     * phone cannot tell is on the person's network, and even the on-box voice is no exception:
     * it writes the clip as a WAV under Portal/uploads and serves it at /ui/uploads/, where no
     * health value may ever land (rules W26, W32). The box refuses such a text on its own as
     * well ([isHealthTextRefusal]).
     */
    fun maySpeak(usedHealth: Boolean): Boolean = !usedHealth

    /** What the person reads when a health reply is not spoken: identical on the Portal and in the app. */
    const val NOT_SPOKEN_MESSAGE = "Health replies aren't read aloud: the voice service would get your health data."

    /** The box's answer to a TTS text that recalls a wellness delivery (POST /tts, /tts/batch, /tts/queue). */
    const val TTS_REFUSAL_ERROR = "wellness_text"

    /**
     * Did the box refuse to speak a text because it recalls the operator's health data (403
     * `{"error": "wellness_text"}`)? The client then shows [NOT_SPOKEN_MESSAGE] and never retries.
     */
    fun isHealthTextRefusal(status: Int, error: String?): Boolean = status == 403 && error?.trim() == TTS_REFUSAL_ERROR

    /** [isHealthTextRefusal] over a raw response body (the direct /tts/batch call reads one). */
    fun isHealthTextRefusal(status: Int, body: ByteArray?): Boolean {
        if (status != 403 || body == null || body.isEmpty() || body.size > 4096) return false
        val obj = runCatching { JSON.parseToJsonElement(body.toString(Charsets.UTF_8)) as? JsonObject }.getOrNull()
        return isHealthTextRefusal(status, obj?.str("error"))
    }

    // ── masked turns and Show numbers ──────────────────────────────────────────────────────

    /** The box's mask glyph (mask.MASK): a health number the ledger does not keep. */
    const val MASK = "[·]"

    fun isMasked(text: String?): Boolean = text != null && MASK in text

    /**
     * The refs Show numbers fetches for a bubble or a snapshot: none unless the text is
     * masked; otherwise [refs] (the bubble's own) or, for a ledger snapshot, the refs of its
     * WELLNESS tag. Deduplicated, well-formed refs only, at most [MAX_REFS].
     */
    fun showNumbersRefs(text: String?, refs: List<String> = emptyList()): List<String> {
        if (!isMasked(text)) return emptyList()
        val own = refs.filter { REF_RE.matches(it) }
        return (own.ifEmpty { refsFromTag(text) }).distinct().take(MAX_REFS)
    }

    const val MAX_REFS = 6

    private const val LOG_HEAD = "\nRaw Session Log\n"
    private const val GAUGES_HEAD = "GAUGES\n"
    private val GAUGE_RE = Regex("^WELLNESS: ([^\\n]+?) · values not recorded[ \\t]*$", RegexOption.MULTILINE)

    /**
     * The refs of a snapshot's WELLNESS gauge (redact.format_gauge: "WELLNESS: sleep,heart ·
     * night of 2026-10-24 · ref w-20261025-7f3a · values not recorded"), read from the
     * header's GAUGES block only, as the box's redact.parse_tag reads it: the session log
     * below is anyone's words.
     */
    fun refsFromTag(snapshotText: String?): List<String> {
        val block = gaugesBlock(snapshotText ?: return emptyList())
        val body = GAUGE_RE.find(block)?.groupValues?.get(1) ?: return emptyList()
        val refPart = body.split(" · ").firstOrNull { it.startsWith("ref ") } ?: return emptyList()
        return REF_RE.findAll(refPart).map { it.value }.distinct().toList()
    }

    /** The header's GAUGES block ("GAUGES" up to the blank line, before the session log), or "". */
    private fun gaugesBlock(text: String): String {
        val log = text.indexOf(LOG_HEAD)
        val head = if (log < 0) text else text.substring(0, log + 1)
        val start = if (head.startsWith(GAUGES_HEAD)) GAUGES_HEAD.length else {
            val i = head.indexOf("\n" + GAUGES_HEAD)
            if (i < 0) return ""
            i + 1 + GAUGES_HEAD.length
        }
        val end = head.indexOf("\n\n", start)
        return if (end < 0) head.substring(start) else head.substring(start, end + 1)
    }

    /** Is Show numbers on offer for a masked text of [textOperator], on a phone enrolled for [enrolledOperator]? */
    fun offersShowNumbers(text: String?, refs: List<String>, textOperator: String?, enrolledOperator: String?): Boolean =
        !enrolledOperator.isNullOrBlank() && textOperator?.trim() == enrolledOperator &&
            showNumbersRefs(text, refs).isNotEmpty()

    /** `GET /wellness/ref/<ref>?operator=<op>` (the operator rides in the query for the signing decision). */
    fun refPath(ref: String, operator: String): String =
        REF_PREFIX + ref + "?operator=" + java.net.URLEncoder.encode(operator, "UTF-8")

    private fun JsonObject.str(key: String): String? =
        (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull?.trim()?.takeIf { it.isNotEmpty() }
}

/**
 * What one assistant turn used of the person's health data, from the stream's UI-only
 * `wellness_used` frames: the refs (the first one tags the bubble for the box's history
 * scrub), the categories, the data's as-of time and the AI it was shared with. Kept with the
 * local chat history for the footer; never sent to the box (only [ref], as `wellness_ref`).
 */
@Serializable
data class WellnessUsed(
    val ref: String,
    val refs: List<String> = listOf(ref),
    val categories: List<String> = emptyList(),
    @SerialName("as_of") val asOf: String? = null,
    @SerialName("provider_label") val providerLabel: String? = null,
)
