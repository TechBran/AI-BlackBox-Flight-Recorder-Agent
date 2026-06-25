package com.aiblackbox.portal.data.store

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.runBlocking

/**
 * Device-local allow-list of operators THIS device has subscribed to notifications
 * for. Used as the second layer (defense in depth) on top of the tailnet-source check
 * when authorizing an inbound `POST /notify`: even a tailnet-sourced push is only
 * accepted for an operator this device opted into.
 *
 * **Empty == accept-all (fail-open).** Until a subscription UI writes here (a later
 * milestone), the set is empty and [isSubscribed] returns true for every operator, so
 * `/notify` is gated by the tailnet check alone — exactly the behaviour the bus needs
 * on a fresh box. Once any subscription is recorded, the allow-list becomes the gate.
 * (A blank/absent operator on the request is always accepted — metadata-only pushes
 * may omit it.)
 *
 * A SEPARATE DataStore file from [BlackBoxStore] so the notification subsystem owns
 * its own schema without widening the settings store.
 */
class NotificationSubscriptionStore(private val context: Context) {

    /** The set of operators this device is subscribed to (empty = accept-all). */
    val subscribedOperators: Flow<Set<String>> =
        context.notifSubsDataStore.data.map { it[KEY_OPERATORS] ?: emptySet() }

    /** Add an operator to the device's subscription allow-list. */
    suspend fun subscribe(operator: String) {
        val op = operator.trim()
        if (op.isEmpty()) return
        context.notifSubsDataStore.edit { prefs ->
            prefs[KEY_OPERATORS] = (prefs[KEY_OPERATORS] ?: emptySet()) + op
        }
    }

    /** Remove an operator from the device's subscription allow-list. */
    suspend fun unsubscribe(operator: String) {
        val op = operator.trim()
        context.notifSubsDataStore.edit { prefs ->
            prefs[KEY_OPERATORS] = (prefs[KEY_OPERATORS] ?: emptySet()) - op
        }
    }

    /**
     * Replace the entire allow-list with EXACTLY [operators] (blanks dropped). Used by
     * the subscription UI's write-through so the device-local /notify re-check stays in
     * lockstep with the backend routing record. An empty set means accept-all (the
     * store's documented fail-open) — the UI passes empty ONLY for "All operators" (or
     * the opt-in default, where the backend won't route here anyway).
     */
    suspend fun setOperators(operators: Set<String>) {
        val cleaned = operators.mapNotNull { it.trim().takeIf(String::isNotEmpty) }.toSet()
        context.notifSubsDataStore.edit { prefs ->
            prefs[KEY_OPERATORS] = cleaned
        }
    }

    /** Clear the allow-list (back to empty == accept-all). */
    suspend fun clear() {
        context.notifSubsDataStore.edit { prefs ->
            prefs[KEY_OPERATORS] = emptySet()
        }
    }

    /**
     * True iff this device should accept a `/notify` for [operator]. Empty allow-list
     * (nothing subscribed yet) accepts all; a blank operator (metadata-only push) is
     * always accepted. Blocking read for the listener's worker thread — never throws.
     */
    fun isSubscribed(operator: String): Boolean = runCatching {
        val op = operator.trim()
        if (op.isEmpty()) return@runCatching true
        val subs = runBlocking { subscribedOperators.first() }
        subs.isEmpty() || op in subs
    }.getOrDefault(true)

    companion object {
        private val KEY_OPERATORS = stringSetPreferencesKey("notif_subscribed_operators")
    }
}

private val Context.notifSubsDataStore: DataStore<Preferences> by
    preferencesDataStore(name = "bbx_notif_subscriptions")
