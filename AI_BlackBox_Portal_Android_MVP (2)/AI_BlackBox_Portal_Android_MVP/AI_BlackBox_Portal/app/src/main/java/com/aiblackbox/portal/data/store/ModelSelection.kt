package com.aiblackbox.portal.data.store

import android.util.Log
import androidx.datastore.core.DataMigration
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.MutablePreferences
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.util.Constants

/**
 * The chat provider + model selection, written in ONE place (W22 / A11-settings-generation-06).
 *
 * Two kinds of key carry it:
 *  - the GLOBAL `model` key ([BlackBoxStore.KEY_MODEL]) is the id ChatViewModel sends with
 *    whatever provider is active, so it may only ever hold an id OF the active provider
 *    ("" = Auto, resolved server-side);
 *  - `model_<provider>` remembers the last pick per provider, so switching back can restore it.
 *
 * The composer pill and the Settings sheet used to write these their own ways: the pill reset
 * the global key to Auto on a provider switch, the sheet left the previous provider's id in it
 * (a Claude id sent to Gemini → 404); only the sheet wrote `model_<provider>`, so the sheet's
 * per-provider display drifted from what was sent; and the on-device Model Manager's "Use"
 * wrote a Gemma slug into the global key, sent to the cloud provider. Both provider switches
 * (pill, sheet) now go through [setProviderAndModel], and every model picker (pill, sheet,
 * the CU + robotics screens) through [selectModel], via BlackBoxStore, so they cannot drift.
 * Opening the CU screen switches through [setProviderAndModel] too. Still outside it: the
 * robotics route auto-set in NativeMainActivity, which writes robotics' one model id from
 * Constants directly (robotics has no live catalog).
 *
 * A pick the provider's live catalog no longer lists is replaced by [reconcileWithCatalog]
 * (same-family successor, else Auto) once a fresh live catalog arrives (rank 76).
 */
object ModelSelection {

    /**
     * The newest live model of each Claude family — where a retired id of that family goes
     * (the owner's 2026-10-17 decision; the server applies the identical map). Each is on
     * the live /models/anthropic list, so a migrated `model_anthropic` can still be
     * restored by a switch back.
     */
    val FAMILY_SUCCESSORS: Map<String, String> = mapOf(
        "opus" to "claude-opus-5-5",
        "sonnet" to "claude-sonnet-5-5",
        "haiku" to "claude-haiku-5-5",
    )

    /**
     * Claude ids the Android pickers offered that are not on the live catalogs (the undated
     * 4.5 aliases, 2026-10-17): dropped from Constants.MODEL_CONFIG, and rewritten like a
     * retired id, since no picker can show (or a switch back restore) them any more.
     */
    val DROPPED_PICKER_IDS: Set<String> = setOf("claude-sonnet-4-5", "claude-haiku-4-5")

    /** Non-Claude ids dropped from the pickers for the same reason; they go to Auto. */
    val DROPPED_NON_CLAUDE_IDS: Set<String> = setOf("grok-3-mini-beta")

    /**
     * The ids an older build stored that can only 404 (or that no picker offers), each
     * named with its family's [FAMILY_SUCCESSORS] entry; [successorOf] covers every other
     * retired id ([RETIRED_CLAUDE_ID]) the same way. [RetiredModelIdMigration] rewrites
     * them once per [RETIRED_MODEL_IDS_VERSION].
     */
    val RETIRED_MODEL_IDS: Map<String, String> = mapOf(
        // Dropped from Constants.MODEL_CONFIG["anthropic"] (the Settings + SMS pickers) by 6071f260.
        "claude-opus-4-1" to "claude-opus-5-5",
        "claude-sonnet-4" to "claude-sonnet-5-5",
        // Their dated twins, offered to the composer pill by the server's /models/anthropic
        // fallback catalog until the same commit.
        "claude-opus-4-1-20250805" to "claude-opus-5-5",
        "claude-sonnet-4-20250514" to "claude-sonnet-5-5",
        // The two 3.x ids that fallback catalog also offered.
        "claude-3-7-sonnet-20250219" to "claude-sonnet-5-5",
        "claude-3-5-haiku-20241022" to "claude-haiku-5-5",
        // [DROPPED_PICKER_IDS].
        "claude-sonnet-4-5" to "claude-sonnet-5-5",
        "claude-haiku-4-5" to "claude-haiku-5-5",
    )

    /**
     * What a stored Claude [id] becomes: its [RETIRED_MODEL_IDS] entry; for any other
     * retired id, its family's newest ("" = Auto for a retired id of no family, such as
     * claude-2 or claude-instant); null when [id] is current and stays.
     */
    fun successorOf(id: String): String? = RETIRED_MODEL_IDS[id]
        ?: if (RETIRED_CLAUDE_ID.matches(id)) {
            FAMILY_SUCCESSORS.entries.firstOrNull { (family, _) -> family in id }?.value ?: ""
        } else null

    /**
     * Every Claude id the server treats as retired — the same pattern as RETIRED_ID_RX in
     * Orchestrator/tests/test_retired_claude_ids.py, matched against the whole id: Claude
     * 4.0 (claude-{sonnet,opus}-4, -4-0, -4-YYYYMMDD), Opus 4.1 (claude-opus-4-1, dated or
     * not), every claude-3*, claude-2* and claude-instant*. Current 4.5+ ids do not match.
     */
    val RETIRED_CLAUDE_ID = Regex(
        "claude-(?:" +
            "(?:sonnet|opus)-4(?:-0|-1)?(?:-\\d{8})?" +
            "|3(?:[.-][\\w.-]*)?" +
            "|2(?:[.-][\\w.-]*)?" +
            "|instant(?:-[\\w.-]*)?" +
            ")"
    )

    /**
     * Run-once stamp for [RetiredModelIdMigration]. Bump [RETIRED_MODEL_IDS_VERSION] when
     * [RETIRED_MODEL_IDS], [FAMILY_SUCCESSORS], [RETIRED_CLAUDE_ID], the dropped-id sets or
     * the rules in [rewriteRetiredModelIds] change, so the next launch rewrites the newly
     * retired ids once. 2 (2026-10-17): same-family successors, the dropped picker ids.
     */
    val KEY_RETIRED_MODEL_IDS_VERSION = intPreferencesKey("retired_model_ids_version")
    const val RETIRED_MODEL_IDS_VERSION = 2

    private const val PROVIDER_MODEL_PREFIX = "model_"

    /** The providers a bare Claude id can be sent to: their live lists carry claude-* ids. */
    private val CLAUDE_ID_PROVIDERS = setOf(ChatProvider.ANTHROPIC.id, ChatProvider.COMPUTER_USE.id)

    fun providerModelKey(provider: String): Preferences.Key<String> =
        stringPreferencesKey(PROVIDER_MODEL_PREFIX + provider)

    /**
     * What a model [id] stored for [provider] becomes in the migration; null = keep it.
     * A dropped non-Claude picker id → Auto. A Claude id under a provider that cannot serve
     * one (custom ids are alias-qualified) was never a pick for it, only A11 residue that
     * 404s → Auto. Under a Claude provider a retired or dropped id gets [successorOf]; a
     * current id stays.
     */
    private fun migratedModelId(id: String, provider: String): String? = when {
        id in DROPPED_NON_CLAUDE_IDS -> ""
        !id.startsWith("claude-") -> null
        provider !in CLAUDE_ID_PROVIDERS -> ""
        else -> successorOf(id)
    }

    /**
     * Rewrite, once, the stored model ids an older build left behind that can only 404:
     *  - a retired Claude id in the global `model` key or a `model_<provider>` key, per
     *    [migratedModelId] (the global key's provider is the active one);
     *  - A11 residue in the global key: a Claude id the active provider cannot serve, and
     *    the on-device slug the old Model Manager "Use" copied there (it equals `model_local`).
     * Another provider's non-Claude id left in the global key cannot be told apart from a
     * pick offline, so it stays until the next switch or pick. Every other key, and a
     * `model_` key that names no provider, is untouched. Returns how many values changed.
     */
    fun rewriteRetiredModelIds(prefs: MutablePreferences): Int {
        val active = prefs[BlackBoxStore.KEY_PROVIDER] ?: Constants.DEFAULT_PROVIDER
        val localSlug = prefs[providerModelKey(ChatProvider.LOCAL.id)].orEmpty()
        var rewritten = 0
        for ((key, value) in prefs.asMap().entries.toList()) {
            val id = value as? String ?: continue
            val name = key.name
            val migrated = when {
                name == BlackBoxStore.KEY_MODEL.name ->
                    if (localSlug.isNotEmpty() && id == localSlug) "" else migratedModelId(id, active)
                name.startsWith(PROVIDER_MODEL_PREFIX) -> {
                    val provider = name.removePrefix(PROVIDER_MODEL_PREFIX)
                    if (ChatProvider.entries.any { it.id == provider }) migratedModelId(id, provider) else null
                }
                else -> null
            } ?: continue
            prefs[stringPreferencesKey(name)] = migrated
            rewritten++
        }
        return rewritten
    }

    /**
     * Switch the active provider — THE path for the composer pill and the Settings sheet.
     *
     * 0. Re-selecting the active provider while it already sends its remembered pick
     *    changes nothing (the Settings dropdown never did): resetting it would lose the
     *    pick for good on a provider with no live list (agents, robotics, voice) or offline.
     * 1. Otherwise provider + Auto in ONE edit, before any network, so the very next send
     *    can never carry the previous provider's id (what the pill always did: "so we
     *    don't send e.g. claude-opus-4-6 to Gemini").
     * 2. Then restore `model_<provider>` only if [liveModelIds] (that provider's live list,
     *    null = unknown) still offers it, so a retired or foreign id is never brought back.
     *    Skipped when a pick or another switch landed while the list was being fetched.
     *    On-device never restores: its slug lives in `model_local`, not the global key.
     */
    suspend fun setProviderAndModel(
        dataStore: DataStore<Preferences>,
        provider: String,
        liveModelIds: suspend (provider: String) -> Collection<String>?,
    ) {
        var reselected = false
        val remembered = dataStore.edit { prefs ->
            val active = prefs[BlackBoxStore.KEY_PROVIDER] ?: Constants.DEFAULT_PROVIDER
            reselected = provider == active &&
                prefs[BlackBoxStore.KEY_MODEL].orEmpty() == prefs[providerModelKey(provider)].orEmpty()
            if (!reselected) {
                prefs[BlackBoxStore.KEY_PROVIDER] = provider
                prefs[BlackBoxStore.KEY_MODEL] = ""
            }
        }[providerModelKey(provider)].orEmpty()
        if (reselected || remembered.isBlank() || ChatProvider.fromId(provider).isLocal) return

        val live = liveModelIds(provider) ?: return
        if (remembered !in live) return
        dataStore.edit { prefs ->
            val untouched = prefs[BlackBoxStore.KEY_PROVIDER] == provider &&
                prefs[BlackBoxStore.KEY_MODEL].isNullOrEmpty() &&
                prefs[providerModelKey(provider)] == remembered
            if (untouched) prefs[BlackBoxStore.KEY_MODEL] = remembered
        }
    }

    /**
     * A model pick from either picker: remembered as `model_<provider>`, and sent (the global
     * key) only when [provider] is the active one. On-device picks (the Model Manager's "Use")
     * write `model_local` ONLY: ChatViewModel and LocalModelService read the slug from there,
     * and the global key belongs to the cloud request.
     */
    suspend fun selectModel(dataStore: DataStore<Preferences>, provider: String, model: String) {
        dataStore.edit { prefs ->
            prefs[providerModelKey(provider)] = model
            val active = prefs[BlackBoxStore.KEY_PROVIDER] ?: Constants.DEFAULT_PROVIDER
            if (provider == active && !ChatProvider.fromId(provider).isLocal) {
                prefs[BlackBoxStore.KEY_MODEL] = model
            }
        }
    }

    /**
     * What a stored [id] becomes when [liveIds], an authoritative live catalog
     * (ModelCatalog.isAuthoritative), does not list it (audit A01-activities-root-10): its
     * [RETIRED_MODEL_IDS] entry, or for any other Claude id its family's newest
     * ([FAMILY_SUCCESSORS]), when the catalog lists that one; otherwise Auto (""). Null when
     * [id] is Auto or listed, and stays.
     */
    fun replacementFor(id: String, liveIds: Collection<String>): String? {
        if (id.isBlank() || id in liveIds) return null
        val successor = RETIRED_MODEL_IDS[id]
            ?: if (id.startsWith("claude-")) {
                FAMILY_SUCCESSORS.entries.firstOrNull { (family, _) -> family in id }?.value
            } else null
        return successor?.takeIf { it in liveIds } ?: ""
    }

    /**
     * Replace the stored picks [provider]'s live catalog no longer lists ([replacementFor]):
     * `model_<provider>`, and the global `model` key while [provider] is the active one
     * (an id of another provider left there is replaced too: it can only 404). Called
     * with an authoritative catalog only, and in one edit, so a pick that lands meanwhile
     * is a listed id and stays. Returns how many values changed.
     */
    suspend fun reconcileWithCatalog(
        dataStore: DataStore<Preferences>,
        provider: String,
        liveIds: Collection<String>,
    ): Int {
        if (ChatProvider.fromId(provider).isLocal || liveIds.isEmpty()) return 0
        var changed = 0
        dataStore.edit { prefs ->
            val perProvider = providerModelKey(provider)
            replacementFor(prefs[perProvider].orEmpty(), liveIds)?.let {
                prefs[perProvider] = it
                changed++
            }
            val active = prefs[BlackBoxStore.KEY_PROVIDER] ?: Constants.DEFAULT_PROVIDER
            if (active == provider) {
                replacementFor(prefs[BlackBoxStore.KEY_MODEL].orEmpty(), liveIds)?.let {
                    prefs[BlackBoxStore.KEY_MODEL] = it
                    changed++
                }
            }
        }
        if (changed > 0) Log.i("BlackBoxStore", "replaced $changed $provider model id(s) its live catalog no longer lists")
        return changed
    }
}

/**
 * One-time DataStore migration (wired into BlackBoxStore's `preferencesDataStore`): rewrites
 * retired and A11-residue model ids ([ModelSelection.rewriteRetiredModelIds]) before the
 * first read, then stamps [ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION] so it never runs
 * again for this version.
 */
object RetiredModelIdMigration : DataMigration<Preferences> {
    override suspend fun shouldMigrate(currentData: Preferences): Boolean =
        (currentData[ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION] ?: 0) <
            ModelSelection.RETIRED_MODEL_IDS_VERSION

    override suspend fun migrate(currentData: Preferences): Preferences {
        val prefs = currentData.toMutablePreferences()
        val rewritten = ModelSelection.rewriteRetiredModelIds(prefs)
        prefs[ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION] = ModelSelection.RETIRED_MODEL_IDS_VERSION
        Log.i("BlackBoxStore", "retired model ids v${ModelSelection.RETIRED_MODEL_IDS_VERSION}: rewrote $rewritten")
        return prefs.toPreferences()
    }

    override suspend fun cleanUp() = Unit
}
