package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * What `GET /wellness/status` says (wire contract), parsed tolerantly: the uncredentialed
 * answer has only state, source label, last sync and enrolled; a signed request adds the
 * troubles, consent, providers, wear, sync, the instinct prompt and the policy version.
 * Missing fields stay null rather than defaulting to something the box did not say.
 */
data class WellnessBoxStatus(
    val state: WellnessRules.EnrollmentState,
    val enrolled: Boolean,
    val sourceLabel: String?,
    val lastSync: String?,
    val credentialed: Boolean,
    val troubles: List<String> = emptyList(),
    val categories: Map<WellnessCategory, WellnessConsent>? = null,
    val localOnly: Map<WellnessCategory, Boolean>? = null,
    val providersAllowed: Set<String>? = null,
    val googlePaidKeyConfirmed: Boolean? = null,
    val surfacesMode: String? = null,
    val instinctPrompt: String? = null,
    val hrMode: String? = null,
    val nightsWorn7: Int? = null,
    val lastPushAt: String? = null,
    val outboxHint: String? = null,
    val policyVersion: String? = null,
    val disclosuresCount: Int? = null,
) {
    /** The settings view of a credentialed status, falling back to the catalog defaults. */
    fun settings(): WellnessRules.Settings = WellnessRules.Settings(
        categories = categories ?: WellnessCatalog.DEFAULT_CONSENT,
        localOnly = localOnly ?: WellnessCatalog.DEFAULT_LOCAL_ONLY,
        providersAllowed = providersAllowed ?: emptySet(),
        googlePaidKeyConfirmed = googlePaidKeyConfirmed ?: false,
        surfacesMode = surfacesMode ?: "auto",
        instinctPrompt = instinctPrompt,
    )
}

/** One "Who saw my health data" row (no values: when, where, which AI, which kinds). */
data class WellnessDisclosure(
    val time: String,
    val surface: String,
    val provider: String,
    val model: String,
    val tool: String,
    val categories: List<String>,
)

object WellnessStatusParser {

    private val JSON = Json { ignoreUnknownKeys = true; isLenient = true }

    fun parse(body: String): WellnessBoxStatus? {
        val obj = runCatching { JSON.parseToJsonElement(body).jsonObject }.getOrNull() ?: return null
        val categories = (obj["categories"] as? JsonObject)?.let { c ->
            WellnessCategory.entries.associateWith { WellnessConsent.fromWire(c.str(it.wire)) }
        }
        val localOnly = (obj["local_only"] as? JsonObject)?.let { c ->
            WellnessCategory.entries.associateWith { c[it.wire]?.jsonPrimitive?.booleanOrNull ?: WellnessCatalog.DEFAULT_LOCAL_ONLY.getValue(it) }
        }
        val wear = obj["wear"] as? JsonObject
        val sync = obj["sync"] as? JsonObject
        val credentialed = categories != null || obj.containsKey("troubles") || obj.containsKey("providers_allowed")
        return WellnessBoxStatus(
            state = WellnessRules.EnrollmentState.fromWire(obj.str("state")),
            enrolled = obj["enrolled"]?.jsonPrimitive?.booleanOrNull ?: false,
            sourceLabel = obj.str("source_label"),
            lastSync = obj.str("last_sync"),
            credentialed = credentialed,
            troubles = obj.strings("troubles"),
            categories = categories,
            localOnly = localOnly,
            providersAllowed = (obj["providers_allowed"] as? JsonArray)?.let { obj.strings("providers_allowed").toSet() },
            googlePaidKeyConfirmed = obj["google_paid_key_confirmed"]?.jsonPrimitive?.booleanOrNull,
            surfacesMode = obj.str("surfaces_mode"),
            instinctPrompt = obj.str("instinct_prompt"),
            hrMode = wear?.str("hr_mode"),
            nightsWorn7 = wear?.get("nights_worn_7")?.jsonPrimitive?.intOrNull,
            lastPushAt = sync?.str("last_push_at"),
            outboxHint = sync?.get("outbox_hint")?.let { if (it is JsonPrimitive) it.contentOrNull else it.toString() },
            policyVersion = obj.str("policy_version"),
            disclosuresCount = obj["disclosures_count"]?.jsonPrimitive?.intOrNull,
        )
    }

    /** `GET /wellness/disclosures`: a list, bare or under "disclosures"/"items", newest first. */
    fun parseDisclosures(body: String): List<WellnessDisclosure> {
        val root = runCatching { JSON.parseToJsonElement(body) }.getOrNull() ?: return emptyList()
        val list: JsonArray = when (root) {
            is JsonArray -> root
            is JsonObject -> (root["disclosures"] ?: root["items"]) as? JsonArray ?: return emptyList()
            else -> return emptyList()
        }
        return list.mapNotNull { it as? JsonObject }.map { o ->
            val cats = when (val c = o["categories"]) {
                is JsonArray -> c.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
                is JsonPrimitive -> c.contentOrNull?.split(',')?.map { it.trim() }?.filter { it.isNotEmpty() } ?: emptyList()
                else -> emptyList()
            }
            WellnessDisclosure(
                time = o.str("time") ?: o.str("ts") ?: o.str("at") ?: "",
                surface = o.str("surface") ?: "",
                provider = o.str("provider") ?: "",
                model = o.str("model") ?: "",
                tool = o.str("tool") ?: "",
                categories = cats,
            )
        }.take(200)
    }

    /**
     * The `PUT /wellness/settings` body: the operator plus ONLY the fields that changed from
     * [old] to [new] (the contract makes every field optional), so a narrowing edit can never
     * carry a widening one along by accident. [include] names fields to send even when
     * unchanged (right after enrollment the phone does not know the box's defaults, so setup
     * states its choices).
     */
    fun settingsBody(
        operator: String,
        old: WellnessRules.Settings,
        new: WellnessRules.Settings,
        include: Set<String> = emptySet(),
    ): String = buildJsonObject {
        fun send(field: String, changed: Boolean) = changed || field in include
        put("operator", JsonPrimitive(operator))
        if (send("categories", new.categories != old.categories)) put("categories", buildJsonObject {
            for (cat in WellnessCategory.entries) put(cat.wire, JsonPrimitive((new.categories[cat] ?: WellnessConsent.OFF).wire))
        })
        if (send("local_only", new.localOnly != old.localOnly)) put("local_only", buildJsonObject {
            for (cat in WellnessCategory.entries) put(cat.wire, JsonPrimitive(new.localOnly[cat] ?: true))
        })
        if (send("providers_allowed", new.providersAllowed != old.providersAllowed)) put("providers_allowed", buildJsonArray {
            new.providersAllowed.sorted().forEach { add(JsonPrimitive(it)) }
        })
        if (send("google_paid_key_confirmed", new.googlePaidKeyConfirmed != old.googlePaidKeyConfirmed))
            put("google_paid_key_confirmed", JsonPrimitive(new.googlePaidKeyConfirmed))
        if (send("surfaces_mode", new.surfacesMode != old.surfacesMode)) put("surfaces_mode", JsonPrimitive(new.surfacesMode))
        if (send("instinct_prompt", new.instinctPrompt != old.instinctPrompt)) put("instinct_prompt",
            new.instinctPrompt?.let { JsonPrimitive(WellnessRules.sanitizeInstinctPrompt(it)) } ?: JsonNull)
    }.toString()

    /** What setup states explicitly after enrolling (consent itself travels in the enroll call). */
    val SETUP_FIELDS: Set<String> = setOf("local_only", "providers_allowed", "google_paid_key_confirmed")

    private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.contentOrNull

    private fun JsonObject.strings(key: String): List<String> =
        (this[key] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull } ?: emptyList()
}
