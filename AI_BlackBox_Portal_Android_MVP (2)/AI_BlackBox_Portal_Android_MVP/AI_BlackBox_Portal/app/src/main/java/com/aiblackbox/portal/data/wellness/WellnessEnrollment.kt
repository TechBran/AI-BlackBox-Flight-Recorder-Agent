package com.aiblackbox.portal.data.wellness

import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * The phone's half of enrollment (spec §3.1, wire contract `POST /wellness/enroll`):
 *
 *  1. mint the intent nonce (2-minute life) for the chosen operator;
 *  2. POST {operator, install_id, tailnet_ips, nonce, capability_report, categories, replace,
 *     assign_to_me} (unsigned: there is no key yet);
 *  3. while the box holds that request it calls this phone's `/wellness/bind` with the same
 *     nonce and the new key; the listener stores it ([WellnessBinding.acceptBind]);
 *  4. the box answers 200 {enrollment_id, epoch, state, sync_interval_min, policy_version}
 *     and this checks the bind it received is that enrollment.
 *
 * Anything else ends the attempt with NO key on the phone: a bind this attempt received is
 * rolled back ([WellnessBinding.rollBackBind]) when the box's answer is a refusal, a 5xx, lost,
 * or names another enrollment, and [onRolledBack] cancels whatever was scheduled for it. A
 * key the box never stored would only collect health data into a 401.
 *
 * [Outcome] carries the user-facing words for every refusal the contract defines; for codes
 * this build does not know, the box's own `fix` (or `detail`) text.
 */
class WellnessEnrollment(
    private val binding: WellnessBinding,
    private val post: suspend (body: String) -> WellnessHttp,
    private val tailnetIps: () -> List<String>,
    private val bindWaitMs: Long = 3_000L,
    private val onRolledBack: () -> Unit = {},
) {

    sealed class Outcome(val message: String) {
        /**
         * The box took this phone. [state] "replace_pending": it replaces a source that is still
         * online, and the box waits for that phone's approval before it accepts anything from
         * this one ([awaitsApproval]).
         */
        class Enrolled(val enrollmentId: String, val epoch: Int, val state: String, val syncIntervalMin: Int?,
                       val policyVersion: String?) : Outcome(
            if (WellnessRules.awaitsApproval(state)) WellnessRules.AWAITING_APPROVAL_MESSAGE else WellnessRules.ENROLLED_MESSAGE,
        ) {
            val awaitsApproval: Boolean get() = WellnessRules.awaitsApproval(state)
        }
        object OwnerMismatch : Outcome(
            "This phone belongs to another operator in Devices. Its owner (or the box owner) can reassign it.")
        object AlreadyEnrolled : Outcome(
            "You already have a health source. Choose \"Replace my health source\" to move it to this phone.")
        object DeviceInUse : Outcome("This phone is already the health source for someone else.")
        object NotPaired : Outcome("Open the BlackBox app once on this phone while it can reach your box, then try again.")
        object BindFailed : Outcome("The box couldn't reach this phone to finish setup. Keep BlackBox open and try again.")
        object NotAllowed : Outcome("This operator can't set up Health. Pick your own operator in Settings.")
        object NotOnTailnet : Outcome("This phone isn't on your BlackBox network (Tailscale). Connect it and try again.")
        object UnknownDevice : Outcome(
            "The box doesn't know this phone yet. Open BlackBox once so it appears in Devices. If the app reaches " +
                "your box by a local network address, switch it to the box's Tailscale address and try again.")
        object WellnessDisabled : Outcome("Health is turned off on your box. Turn it on in the box's settings, then try again.")
        object Unreachable : Outcome("Couldn't reach your box. Check the connection and try again.")
        /** A refusal this build has no words for: the box's own fix text when it sent one. */
        class Refused(val status: Int, val error: String?, fix: String? = null) : Outcome(
            fix?.takeIf { it.isNotBlank() } ?: "The box didn't accept the setup (${error ?: "HTTP $status"}).")
        object BindMissing : Outcome("The box answered but this phone never received its key. Try again.")
    }

    suspend fun enroll(
        operator: String,
        consent: Map<WellnessCategory, WellnessConsent>,
        capabilityReportJson: String,
        assignToMe: Boolean,
        replace: Boolean,
    ): Outcome {
        val ips = tailnetIps()
        if (ips.isEmpty()) return Outcome.NotOnTailnet
        val intent = binding.beginIntent(operator)
        val body = requestBody(operator, binding.installId(), ips, intent.nonce, capabilityReportJson, consent, replace, assignToMe)
        val reply = post(body)
        val outcome = interpret(reply)
        if (outcome !is Outcome.Enrolled) {
            binding.clearIntent()
            rollBack(intent.nonce)
            return outcome
        }
        // The bind normally lands before the box answers; allow a short race either way.
        var waited = 0L
        while (binding.current()?.enrollmentId != outcome.enrollmentId && waited < bindWaitMs) {
            delay(100)
            waited += 100
        }
        if (binding.current()?.enrollmentId == outcome.enrollmentId) return outcome
        binding.clearIntent()
        rollBack(intent.nonce)
        return Outcome.BindMissing
    }

    private fun rollBack(nonce: String) {
        if (binding.rollBackBind(nonce)) runCatching { onRolledBack() }
    }

    companion object {
        private val JSON = Json { ignoreUnknownKeys = true }

        fun requestBody(
            operator: String,
            installId: String,
            tailnetIps: List<String>,
            nonce: String,
            capabilityReportJson: String,
            consent: Map<WellnessCategory, WellnessConsent>,
            replace: Boolean,
            assignToMe: Boolean,
        ): String = buildJsonObject {
            put("operator", JsonPrimitive(operator))
            put("install_id", JsonPrimitive(installId))
            put("tailnet_ips", buildJsonArray { tailnetIps.forEach { add(JsonPrimitive(it)) } })
            put("nonce", JsonPrimitive(nonce))
            put("capability_report", JSON.parseToJsonElement(capabilityReportJson))
            put("categories", buildJsonObject {
                for (cat in WellnessCategory.entries) put(cat.wire, JsonPrimitive((consent[cat] ?: WellnessConsent.OFF).wire))
            })
            put("replace", JsonPrimitive(replace))
            put("assign_to_me", JsonPrimitive(assignToMe))
        }.toString()

        fun interpret(reply: WellnessHttp): Outcome {
            if (!reply.reached) return Outcome.Unreachable
            val obj = runCatching { JSON.parseToJsonElement(reply.body).jsonObject }.getOrNull()
            val error = obj?.get("error")?.jsonPrimitive?.contentOrNull
            val fix = (obj?.get("fix") ?: obj?.get("detail"))?.let { runCatching { it.jsonPrimitive.contentOrNull }.getOrNull() }
            return when (reply.status) {
                200 -> {
                    val id = obj?.get("enrollment_id")?.jsonPrimitive?.contentOrNull
                    val epoch = obj?.get("epoch")?.jsonPrimitive?.intOrNull
                    if (id == null || epoch == null) Outcome.Refused(200, "malformed reply")
                    else Outcome.Enrolled(
                        enrollmentId = id,
                        epoch = epoch,
                        state = obj["state"]?.jsonPrimitive?.contentOrNull ?: "active",
                        syncIntervalMin = obj["sync_interval_min"]?.jsonPrimitive?.intOrNull,
                        policyVersion = obj["policy_version"]?.jsonPrimitive?.contentOrNull,
                    )
                }
                409 -> when (error) {
                    "owner_mismatch" -> Outcome.OwnerMismatch
                    "already_enrolled" -> Outcome.AlreadyEnrolled
                    "device_in_use" -> Outcome.DeviceInUse
                    else -> Outcome.Refused(409, error, fix)
                }
                424 -> Outcome.NotPaired
                502 -> Outcome.BindFailed
                403 -> Outcome.NotAllowed
                404 -> if (error == "unknown_device") Outcome.UnknownDevice else Outcome.Refused(404, error, fix)
                503 -> if (error == "wellness_disabled") Outcome.WellnessDisabled else Outcome.Refused(503, error, fix)
                else -> Outcome.Refused(reply.status, error, fix)
            }
        }
    }
}
