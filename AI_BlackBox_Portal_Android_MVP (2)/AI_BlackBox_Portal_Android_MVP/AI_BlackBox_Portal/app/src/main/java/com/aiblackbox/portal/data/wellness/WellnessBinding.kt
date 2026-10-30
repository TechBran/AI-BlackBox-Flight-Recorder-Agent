package com.aiblackbox.portal.data.wellness

import android.util.Log
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * The phone's enrollment state (spec §3.1): which operator this phone is the health source
 * for, the enrollment id and epoch, and the device key K the box delivered in
 * `/wellness/bind`. Plus the INTENT NONCE: minted when the user taps "Use this phone for my
 * health", alive for two minutes, and the only thing that lets a bind in. A tailnet peer that
 * planted its own pairing secret for this phone's address still cannot bind (WireGuard pins
 * the address to the node key, and the nonce never left this phone except to the box over
 * the enroll call the user started).
 *
 * All of it lives in [WellnessFiles] (no-backup storage). Thread-safe: the listener's worker
 * threads and the UI both reach it.
 *
 * GENERATION FENCE. A sync or history import can run for minutes with the record it read at
 * its start. Every change of binding (a new enrollment or epoch, an unbind, an erase) bumps
 * [generation]; the engine writes only through [runIfCurrent] with the generation it started
 * with, under this object's lock. So a run that outlives its binding can neither recreate the
 * files an unbind deleted, nor sign with the old key and then clear the NEW binding when the
 * box refuses that key.
 */
class WellnessBinding(
    private val files: WellnessFiles,
    private val clock: () -> Long = System::currentTimeMillis,
    private val random: SecureRandom = SecureRandom(),
    private val state: WellnessSyncStateStore = WellnessSyncStateStore(files.syncState),
) {
    private val lock = Any()

    /** Bumped on every change of binding (see the class doc). In memory: runs are in-process. */
    private var generation = 0L

    /** This install's stable id (sent at enrollment). Survives unbind; a reinstall mints a new one. */
    fun installId(): String = synchronized(lock) {
        val existing = WellnessAtomic.readTextOrNull(files.installId)?.trim()
        if (existing != null && INSTALL_ID_RE.matches(existing)) return existing
        val fresh = hex(ByteArray(16).also { random.nextBytes(it) })
        WellnessAtomic.writeText(files.installId, fresh)
        fresh
    }

    /** The live binding, or null when this phone is not a health source. */
    fun current(): WellnessBindingRecord? = synchronized(lock) { readRecord() }

    /** The live binding and its generation, read together (a run starts from this). */
    fun snapshot(): Pair<WellnessBindingRecord, Long>? = synchronized(lock) { readRecord()?.let { it to generation } }

    /**
     * Run [block] under the binding lock if the binding is still the one of [gen]. Returns
     * false (and runs nothing) when it changed: the caller's run is stale and must stop.
     */
    fun runIfCurrent(gen: Long, block: () -> Unit): Boolean = synchronized(lock) {
        if (gen != generation || readRecord() == null) return false
        block()
        true
    }

    /** The operator the box may address `/wellness/sync` and `/wellness/unbind` to ("" if none). */
    fun enrolledOperator(): String = current()?.operator.orEmpty()

    /**
     * Start an enrollment for [operator]: a fresh 2-minute nonce, persisted. [boxOrigin] is the
     * box the enroll call goes to (the paired box): the bind it answers with arrives while that
     * call is held, so the binding records it as the one box its key may sign for (contract (f)).
     */
    fun beginIntent(operator: String, boxOrigin: String? = null): WellnessIntent = synchronized(lock) {
        require(operator.isNotBlank()) { "operator required" }
        val intent = WellnessIntent(
            operator = operator.trim(),
            nonce = OriginSigner.newNonce(random),
            expiresAtMs = clock() + INTENT_LIFE_MS,
            boxOrigin = WellnessChatRules.originOf(boxOrigin),
        )
        WellnessAtomic.writeText(files.intent, JSON.encodeToString(intent))
        intent
    }

    /** The intent that is still alive, or null (none, expired, or unreadable). */
    fun liveIntent(): WellnessIntent? = synchronized(lock) { readLiveIntent() }

    /** The operator a bind must name right now ("" when no intent is alive). */
    fun bindOperator(): String = liveIntent()?.operator.orEmpty()

    fun clearIntent() = synchronized(lock) { files.intent.delete(); Unit }

    /**
     * Accept or refuse a `/wellness/bind` from the box. Accepted only when an intent is alive,
     * its nonce matches (constant time), the operator is the one the user chose, and the
     * enrollment id, epoch and key are well formed. On accept the record replaces any older
     * one (a re-enroll is a new epoch) and the intent is spent.
     */
    fun acceptBind(request: WellnessBindRequest): BindDecision = synchronized(lock) {
        val intent = readLiveIntent() ?: return BindDecision.Rejected(403, "no_live_intent")
        if (!constantTimeEquals(intent.nonce, request.nonce.trim().lowercase()))
            return BindDecision.Rejected(403, "nonce_mismatch")
        if (request.operator.trim() != intent.operator)
            return BindDecision.Rejected(403, "operator_mismatch")
        if (!ENROLLMENT_RE.matches(request.enrollmentId)) return BindDecision.Rejected(400, "bad_enrollment_id")
        if (request.epoch < 1) return BindDecision.Rejected(400, "bad_epoch")
        if (!OriginSigner.isWellFormedKey(request.k)) return BindDecision.Rejected(400, "bad_key")
        val record = WellnessBindingRecord(
            operator = intent.operator,
            enrollmentId = request.enrollmentId,
            epoch = request.epoch,
            k = request.k,
            boundAtMs = clock(),
            boundBy = intent.nonce,
            boxOrigin = intent.boxOrigin,
        )
        val previous = readRecord()
        val sameEnrollment = previous != null && previous.enrollmentId == record.enrollmentId &&
            previous.epoch == record.epoch
        if (!sameEnrollment) resetCollectedLocked()
        // A run still going with the old record must not write or sign another thing.
        if (!sameEnrollment || previous?.k != record.k) generation++
        WellnessAtomic.writeText(files.binding, JSON.encodeToString(record))
        files.intent.delete()
        Log.i(TAG, "health source bound (epoch=${record.epoch})")
        BindDecision.Accepted(record)
    }

    /**
     * Forget the enrollment and everything collected for it: key, tokens, cursor, index,
     * outbox. The install id stays (it names the install, not the person's data).
     */
    fun clearAll() = synchronized(lock) { clearAllLocked() }

    /**
     * [clearAll], but only if the binding is still the one of [gen]. The engine uses it when
     * the box refuses a key for good (409/410): if a re-enroll replaced that binding while the
     * refused request was in flight, the NEW one stays. True when it cleared.
     */
    fun clearIfCurrent(gen: Long): Boolean = synchronized(lock) {
        if (gen != generation) return false
        clearAllLocked()
        true
    }

    /**
     * Undo a bind that an enrollment attempt received but did not finish (the box's answer
     * was a refusal, a 5xx, lost, or named another enrollment): only the binding that
     * attempt's [nonce] created is removed, never an older or newer one. True when it was.
     */
    fun rollBackBind(nonce: String): Boolean = synchronized(lock) {
        val record = readRecord() ?: return false
        if (record.boundBy == null || !constantTimeEquals(record.boundBy, nonce)) return false
        clearAllLocked()
        true
    }

    private fun clearAllLocked() {
        generation++
        files.root.listFiles()?.forEach { f ->
            if (f.name != files.installId.name) f.deleteRecursively()
        }
    }

    /**
     * A new enrollment or epoch starts collection over: the Changes tokens, the id index and
     * the backfill cursor belong to the old one, and batches still in the outbox carry the old
     * epoch (Health Connect still holds that data; the new backfill reads it again). Consent,
     * the interval and the heart-rate origin choice are the person's settings and stay.
     */
    private fun resetCollectedLocked() {
        files.tokens.delete()
        files.dayIndex.delete()
        files.outbox.deleteRecursively()
        state.update {
            it.copy(backfill = BackfillCursor(), needsReconnect = false, localHold = null, backoffUntilMs = 0L,
                quotaAttempt = 0, lastPushMs = 0L, lastReconcileMs = 0L, lastStatusMs = 0L)
        }
    }

    /** A signer for the live binding, or null when not bound. */
    fun signer(): OriginSigner? = current()?.let { OriginSigner(it.enrollmentId, it.k, random) }

    /**
     * Pin a binding written before `box_origin` existed (wave 3 and earlier) to [origin], the
     * paired box of its first signed use ([WellnessChatRules.KeyUse.Adopt]). The record is
     * rewritten ONCE, in place (same key, enrollment and generation: a run in flight carries on);
     * a binding that already names a box is never re-pinned. True when the binding now names
     * [origin], so its key may sign for it.
     */
    fun adoptBoxOrigin(origin: String): Boolean = synchronized(lock) {
        val pinned = WellnessChatRules.originOf(origin) ?: return false
        val record = readRecord() ?: return false
        if (record.boxOrigin != null) return WellnessChatRules.sameOrigin(record.boxOrigin, pinned)
        WellnessAtomic.writeText(files.binding, JSON.encodeToString(record.copy(boxOrigin = pinned)))
        Log.i(TAG, "health source pinned to its box (an older binding)")
        true
    }

    /**
     * May this phone's key sign a request to [requestUrl] while the app is paired with
     * [pairedOrigin] ([WellnessChatRules.keyUse])? An older binding adopts the paired box here.
     * The one check IngestClient makes before it signs anything.
     */
    fun mayUseKeyFor(requestUrl: String, pairedOrigin: String?): Boolean {
        val record = current() ?: return false
        return when (val use = WellnessChatRules.keyUse(record.boxOrigin, requestUrl, pairedOrigin)) {
            WellnessChatRules.KeyUse.Pinned -> true
            is WellnessChatRules.KeyUse.Adopt -> adoptBoxOrigin(use.origin)
            is WellnessChatRules.KeyUse.Refused -> false
        }
    }

    private fun readRecord(): WellnessBindingRecord? {
        val raw = WellnessAtomic.readTextOrNull(files.binding) ?: return null
        return runCatching { JSON.decodeFromString<WellnessBindingRecord>(raw) }.getOrNull()
            ?.takeIf { it.operator.isNotBlank() && OriginSigner.isWellFormedKey(it.k) }
    }

    private fun readLiveIntent(): WellnessIntent? {
        val raw = WellnessAtomic.readTextOrNull(files.intent) ?: return null
        val intent = runCatching { JSON.decodeFromString<WellnessIntent>(raw) }.getOrNull() ?: return null
        return intent.takeIf { clock() < it.expiresAtMs && OriginSigner.isWellFormedNonce(it.nonce) }
    }

    companion object {
        private const val TAG = "WellnessBinding"
        const val INTENT_LIFE_MS = 2 * 60_000L
        val ENROLLMENT_RE = Regex("en_[0-9a-f]{8}")
        private val INSTALL_ID_RE = Regex("[0-9a-f]{32}")

        internal val JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }

        fun constantTimeEquals(a: String, b: String): Boolean =
            MessageDigest.isEqual(a.toByteArray(Charsets.UTF_8), b.toByteArray(Charsets.UTF_8))

        private fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }
    }
}

@Serializable
data class WellnessBindingRecord(
    val operator: String,
    @SerialName("enrollment_id") val enrollmentId: String,
    val epoch: Int,
    val k: String,
    @SerialName("bound_at_ms") val boundAtMs: Long = 0L,
    /** The intent nonce of the enrollment attempt that delivered this bind (spent; see rollBackBind). */
    @SerialName("bound_by") val boundBy: String? = null,
    /**
     * `scheme://host[:port]` of the box that delivered the bind, the only box this key signs for
     * (contract (f)). Null in a binding written before the field existed: it adopts the paired
     * box of its first signed use ([WellnessBinding.adoptBoxOrigin]).
     */
    @SerialName("box_origin") val boxOrigin: String? = null,
)

@Serializable
data class WellnessIntent(
    val operator: String,
    val nonce: String,
    @SerialName("expires_at_ms") val expiresAtMs: Long,
    /** The box the enroll call went to; the bind it delivers is pinned to it. */
    @SerialName("box_origin") val boxOrigin: String? = null,
)

/** The box's `/wellness/bind` body (wire contract). */
@Serializable
data class WellnessBindRequest(
    val operator: String = "",
    @SerialName("enrollment_id") val enrollmentId: String = "",
    val epoch: Int = 0,
    val nonce: String = "",
    val k: String = "",
)

sealed class BindDecision {
    data class Accepted(val record: WellnessBindingRecord) : BindDecision()
    data class Rejected(val status: Int, val error: String) : BindDecision()
}
