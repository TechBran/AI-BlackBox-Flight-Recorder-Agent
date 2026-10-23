package com.aiblackbox.portal.data.store

import androidx.datastore.core.DataMigration
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * The box's operators, as GET /operators serves them:
 * `{"operators": [...], "default": "...", "reserved": [...]}`. `reserved` (additive,
 * 2026-07-23) names the box's own overseers (the Flight Recorder), which are never
 * anyone's operator by default; [people] is everyone else.
 */
data class OperatorRoster(
    val operators: List<String>,
    val default: String = "",
    val reserved: List<String> = emptyList(),
) {
    val people: List<String> get() = operators.filter { it !in reserved }
}

/** What the app does about the operator once the box's roster is known. */
sealed interface OperatorResolution {
    /** Use [operator] (written to the store when it differs from the stored one). */
    data class Resolved(val operator: String) : OperatorResolution
    /** Several people share the box and none is picked: the top-bar picker opens. */
    data class Choose(val operators: List<String>) : OperatorResolution
    /** The box lists nobody yet: guidance to add one. */
    data object NoOperators : OperatorResolution

    val prompt: OperatorPrompt
        get() = when (this) {
            is Resolved -> OperatorPrompt.NONE
            is Choose -> OperatorPrompt.CHOOSE
            NoOperators -> OperatorPrompt.ADD_FIRST
        }
}

/** What the top bar asks of the operator: nothing, a pick, or a first operator. */
enum class OperatorPrompt { NONE, CHOOSE, ADD_FIRST }

/**
 * Tags a load with the operator generation it started in. A screen that reloads on an
 * operator switch [advance]s it; a reply tagged before the switch is then stale and is
 * dropped, so a poll that lands after the new operator's load cannot show the previous
 * operator's data. Main-thread only (viewModelScope), like the state it guards.
 */
class OperatorGeneration {
    private var value = 0

    /** The operator changed: every load tagged before now is stale. */
    fun advance() { value++ }

    /** The tag for a load starting now. */
    fun tag(): Int = value

    /** Whether a load tagged [tag] may still apply its result. */
    fun isCurrent(tag: Int): Boolean = tag == value
}

/** A request that needs an operator was made while none is resolved. */
class OperatorUnresolvedException(message: String = OperatorResolver.CHOOSE_MESSAGE) :
    IllegalStateException(message)

/**
 * The operator comes from the box, never from a seed name (A09-data-local-model-05).
 * The ONE 0/1/many rule — the web Portal's chooseOperator (state-management.js) is its
 * twin, and the MCP server's choose_operator the server-side one:
 *  - a stored pick the box still lists stands;
 *  - otherwise the box's only person is used (reserved overseers do not count);
 *  - otherwise several people → the operator picks; nobody → guidance to add one.
 * The box's default is never adopted silently by a surface that can ask.
 */
object OperatorResolver {

    const val CHOOSE_MESSAGE = "Choose an operator first: tap Operator at the top."
    const val NO_OPERATORS_MESSAGE =
        "This BlackBox has no operators yet. Tap Operator at the top, then + New Operator."

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** GET /operators → roster; null when the reply is not a roster (unknown: change nothing). */
    fun parseRoster(raw: String): OperatorRoster? = try {
        val obj = json.parseToJsonElement(raw) as? JsonObject
        val ops = obj?.get("operators") as? JsonArray
        if (obj == null || ops == null) null
        else OperatorRoster(
            operators = names(ops),
            default = (obj["default"] as? JsonPrimitive)?.takeIf { it.isString }?.content?.trim().orEmpty(),
            reserved = (obj["reserved"] as? JsonArray)?.let(::names).orEmpty(),
        )
    } catch (_: Exception) {
        null
    }

    private fun names(arr: JsonArray): List<String> = arr.mapNotNull { el ->
        (el as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull?.trim()?.takeIf { it.isNotEmpty() }
    }

    fun resolve(stored: String, roster: OperatorRoster): OperatorResolution {
        val held = stored.trim()
        if (held.isNotEmpty() && held in roster.operators) return OperatorResolution.Resolved(held)
        val people = roster.people
        return when (people.size) {
            0 -> OperatorResolution.NoOperators
            1 -> OperatorResolution.Resolved(people.single())
            else -> OperatorResolution.Choose(roster.operators)
        }
    }

    /** Why a send must wait, or null when [operator] is resolved and it may go. */
    fun sendBlockedReason(operator: String, prompt: OperatorPrompt): String? = when {
        operator.isNotBlank() -> null
        prompt == OperatorPrompt.ADD_FIRST -> NO_OPERATORS_MESSAGE
        else -> CHOOSE_MESSAGE
    }

    /**
     * The first selection of a surface that cannot ask, only show (the floating overlay's
     * spinner, always holding a value the operator can change): the hand-over [stored]
     * operator or the rule's pick, else the box default while it lists it, else the first
     * person. "" only when the box lists nobody.
     */
    fun forSpinner(stored: String, roster: OperatorRoster): String =
        when (val r = resolve(stored, roster)) {
            is OperatorResolution.Resolved -> r.operator
            is OperatorResolution.Choose ->
                roster.default.takeIf { it in roster.people } ?: roster.people.first()
            OperatorResolution.NoOperators -> ""
        }

    /**
     * The web Portal's operator as WebView.evaluateJavascript hands it back: the JSON
     * encoding of a string (`"Ana"`), or `null` when the page has none. Anything else → "".
     */
    fun fromJsResult(raw: String?): String = try {
        val el = Json.parseToJsonElement(raw ?: return "") as? JsonPrimitive  // strict: JSON only
        el?.takeIf { it.isString }?.content?.trim().orEmpty()
    } catch (_: Exception) {
        ""
    }
}

/**
 * One-time DataStore migration (wired into BlackBoxStore's `preferencesDataStore`): the
 * operator a pairing QR carries used to be written to the legacy `bbx_prefs`
 * SharedPreferences and never read again, while the store fell back to a seed name. It
 * is moved into [BlackBoxStore.KEY_OPERATOR] once, unless an operator was already picked
 * in the app, then [KEY_MIGRATED] is stamped so a later value there never overrides a pick.
 * [readLegacy] reads `bbx_prefs` (a lambda so the migration runs on the plain JVM).
 */
class LegacyPairingOperatorMigration(private val readLegacy: () -> String?) : DataMigration<Preferences> {

    companion object {
        val KEY_MIGRATED = booleanPreferencesKey("legacy_pairing_operator_migrated")
    }

    override suspend fun shouldMigrate(currentData: Preferences): Boolean = currentData[KEY_MIGRATED] != true

    override suspend fun migrate(currentData: Preferences): Preferences {
        val prefs = currentData.toMutablePreferences()
        BlackBoxStore.adoptPairingOperatorInto(prefs, readLegacy().orEmpty())
        prefs[KEY_MIGRATED] = true
        return prefs.toPreferences()
    }

    override suspend fun cleanUp() = Unit
}
