package com.aiblackbox.portal.data.location

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Address
import android.location.Geocoder
import android.location.Location
import android.location.LocationManager
import android.os.Build
import android.os.CancellationSignal
import android.os.SystemClock
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CancellableContinuation
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.util.Locale
import kotlin.coroutines.resume
import kotlin.math.abs
import kotlin.math.roundToLong

/**
 * M1 — LOCATION RIDES ALONG WITH THE PROMPT.
 *
 * Brandon's locked design (2026-07-24): *"We just send the location along with our user
 * prompt. One location gets saved per snapshot. That's it."* This is deliberately NOT a
 * tracking system — there is no polling, no background service, no location history
 * store, and no location-minted snapshot. A fix is read ONCE, at send time, on the turn
 * the operator was already taking, and rides in [com.aiblackbox.portal.data.model.StreamRequest]
 * beside `origin_device_id` (the same per-turn-metadata seam, added for exactly this
 * class of field).
 *
 * ### The invariant that governs every line below
 * **Absent location is a perfect no-op.** A denied permission, location services off, a
 * phone with no fix, a missing/dead Geocoder, a thrown SecurityException, the operator's
 * settings toggle off — all resolve to `null`, and the turn goes out byte-for-byte as it
 * does today. *Nothing here may ever fail, delay, or alter a chat turn.* Every Android
 * call is wrapped; the whole capture is fenced by [CAPTURE_BUDGET_MS].
 *
 * ### Zero external dependencies, by decision
 * Uses the platform's own [LocationManager] and [Geocoder] only — no Play services
 * artifact (`FusedLocationProviderClient` would add a dependency this build does not
 * carry), no geocoding API, no key, no billing, and **no network call of our own**.
 * On API 31+ [LocationManager.FUSED_PROVIDER] gives us the platform's fused fix through
 * the same free interface.
 *
 * ### Permissions
 * `ACCESS_COARSE_LOCATION` + `ACCESS_FINE_LOCATION`, **while-in-use only**.
 * `ACCESS_BACKGROUND_LOCATION` is FORBIDDEN by this design — capture happens only in
 * direct response to a send the operator just performed, so background access would buy
 * nothing and cost a Play Data Safety burden. Do not add it.
 */
class LocationProvider(context: Context) {

    private val appContext = context.applicationContext

    /**
     * Capture a location for THIS turn, or null.
     *
     * @param attachEnabled the operator's settings toggle (BlackBoxStore.locationAttachEnabled).
     *   False → nothing is read at all, and the OS permission stays granted (the operator can
     *   stop attaching location without revoking it).
     */
    suspend fun capture(attachEnabled: Boolean): UserLocation? = LocationRules.captureWith(
        budgetMs = CAPTURE_BUDGET_MS,
        permitted = attachEnabled && hasPermission(),
        lastFix = { lastKnownFix() },
        freshFix = { freshFix() },
        geocode = { lat, lon -> reverseGeocode(lat, lon) },
    )

    /**
     * COARSE **or** FINE is enough — coarse city-block precision already answers
     * "what's near me", which is the whole point of the feature.
     */
    fun hasPermission(): Boolean = try {
        ContextCompat.checkSelfPermission(appContext, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(appContext, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
    } catch (_: Throwable) {
        false
    }

    private fun locationManager(): LocationManager? = try {
        appContext.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
    } catch (_: Throwable) {
        null
    }

    /**
     * The free, instant path: whatever fix the OS already has. No sensor is powered on,
     * no callback is registered — this is a single binder read per provider.
     */
    @SuppressLint("MissingPermission")
    private fun lastKnownFix(): RawFix? {
        val lm = locationManager() ?: return null
        var best: Location? = null
        var bestAge = Long.MAX_VALUE
        for (name in providerNames()) {
            val candidate = try {
                lm.getLastKnownLocation(name)
            } catch (_: Throwable) {
                // Unknown/disabled provider, or a SecurityException on a revoked
                // permission mid-flight. Neither is fatal — try the next one.
                null
            } ?: continue
            val age = ageMs(candidate)
            if (age < bestAge) {
                best = candidate
                bestAge = age
            }
        }
        val fix = best ?: return null
        return RawFix(fix.latitude, fix.longitude, fix.accuracy.toDouble(), bestAge)
    }

    /**
     * A SINGLE fresh fix, requested only when the cached one is missing or stale, and only
     * on API 30+ where [LocationManager.getCurrentLocation] gives us a one-shot with a
     * [CancellationSignal]. Below 30 we take the last-known fix or nothing: the
     * pre-30 one-shot APIs need a Looper and cannot be cancelled cleanly, and a fix that
     * cannot arrive inside ~1s is worth nothing to this feature anyway.
     */
    @SuppressLint("MissingPermission")
    private suspend fun freshFix(): RawFix? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return null
        val lm = locationManager() ?: return null
        val provider = providerNames().firstOrNull { name ->
            try {
                lm.isProviderEnabled(name)
            } catch (_: Throwable) {
                false
            }
        } ?: return null
        return try {
            suspendCancellableCoroutine { cont: CancellableContinuation<RawFix?> ->
                val signal = CancellationSignal()
                cont.invokeOnCancellation { runCatching { signal.cancel() } }
                try {
                    lm.getCurrentLocation(
                        provider,
                        signal,
                        ContextCompat.getMainExecutor(appContext),
                    ) { loc: Location? ->
                        if (cont.isActive) {
                            cont.resume(
                                loc?.let { RawFix(it.latitude, it.longitude, it.accuracy.toDouble(), ageMs(it)) }
                            )
                        }
                    }
                } catch (t: Throwable) {
                    if (cont.isActive) cont.resume(null)
                }
            }
        } catch (c: CancellationException) {
            throw c
        } catch (_: Throwable) {
            null
        }
    }

    /**
     * ON-DEVICE reverse geocode — [Geocoder], the platform's own. The place name is an
     * ENRICHMENT, never a requirement: any failure returns null and the coordinates ride
     * alone.
     *
     * API 33+ uses the async callback overload. Below 33 the deprecated overload BLOCKS
     * (it does real I/O), so it is confined to [Dispatchers.IO] — it must never touch the
     * main thread.
     */
    private suspend fun reverseGeocode(lat: Double, lon: Double): GeoPlace? {
        val present = try {
            Geocoder.isPresent()
        } catch (_: Throwable) {
            false
        }
        if (!present) return null
        val geocoder = try {
            Geocoder(appContext, Locale.getDefault())
        } catch (_: Throwable) {
            return null
        }
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                suspendCancellableCoroutine { cont: CancellableContinuation<GeoPlace?> ->
                    try {
                        geocoder.getFromLocation(lat, lon, 1, object : Geocoder.GeocodeListener {
                            override fun onGeocode(addresses: MutableList<Address>) {
                                if (cont.isActive) cont.resume(LocationRules.placeOf(addresses.firstOrNull()))
                            }

                            override fun onError(errorMessage: String?) {
                                if (cont.isActive) cont.resume(null)
                            }
                        })
                    } catch (t: Throwable) {
                        if (cont.isActive) cont.resume(null)
                    }
                }
            } else {
                withContext(Dispatchers.IO) {
                    @Suppress("DEPRECATION")
                    val addresses = geocoder.getFromLocation(lat, lon, 1)
                    LocationRules.placeOf(addresses?.firstOrNull())
                }
            }
        } catch (c: CancellationException) {
            throw c
        } catch (_: Throwable) {
            null
        }
    }

    private fun providerNames(): List<String> = buildList {
        // Platform fused provider (API 31+) — the best fix available without Play services.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) add(LocationManager.FUSED_PROVIDER)
        add(LocationManager.NETWORK_PROVIDER) // cheap + fast; city precision is enough
        add(LocationManager.GPS_PROVIDER)
        add(LocationManager.PASSIVE_PROVIDER)
    }

    /**
     * Age from the monotonic clock where available (immune to wall-clock jumps/NTP
     * corrections), falling back to wall time. Clamped at 0 — a "future" fix is a clock
     * artifact, not a reason to discard a usable position.
     */
    private fun ageMs(location: Location): Long = try {
        val elapsed = location.elapsedRealtimeNanos
        if (elapsed > 0L) {
            ((SystemClock.elapsedRealtimeNanos() - elapsed) / 1_000_000L).coerceAtLeast(0L)
        } else {
            (System.currentTimeMillis() - location.time).coerceAtLeast(0L)
        }
    } catch (_: Throwable) {
        Long.MAX_VALUE
    }

    companion object {
        /**
         * HARD budget for the ENTIRE capture — cached read, optional one-shot fix, and
         * reverse geocode combined. Sending never blocks on GPS: when the budget expires
         * the prompt goes out with whatever was already in hand (coordinates without a
         * place name), or with no location at all.
         */
        const val CAPTURE_BUDGET_MS = 1_000L
    }
}

/**
 * The wire object appended to the chat request.
 *
 * Brandon's package contents, verbatim: **coordinates + city + state**. `place` is the
 * composed "City, State" label; `city`/`state` are sent SEPARATELY as well so the backend
 * keeps them structured rather than re-splitting a string.
 *
 * Field names are exactly the keys `Orchestrator/location.py: normalize()` reads. Every
 * field but the two coordinates is optional by contract — a fix with no geocode is a
 * complete, valid payload.
 */
@Serializable
data class UserLocation(
    val lat: Double,
    val lon: Double,
    @SerialName("accuracy_m") val accuracyM: Double? = null,
    val place: String? = null,
    val city: String? = null,
    val state: String? = null,
)

/**
 * A reverse-geocode result, split the way the backend stores it. `label` is what a human
 * reads ("Hamilton, Ontario"); `city`/`state` are the parts it was composed from.
 */
data class GeoPlace(
    val city: String? = null,
    val state: String? = null,
    val label: String? = null,
) {
    fun isEmpty(): Boolean = city.isNullOrBlank() && state.isNullOrBlank() && label.isNullOrBlank()
}

/** A fix stripped of Android types so every rule below is JVM-unit-testable. */
data class RawFix(
    val lat: Double,
    val lon: Double,
    val accuracyM: Double?,
    val ageMs: Long,
)

/**
 * PURE rules + the capture orchestration, deliberately free of Android types so the whole
 * decision surface (staleness, time budget, coordinate validity, "no location rather than
 * a partial object") is tested on the JVM with no device and no Robolectric.
 */
object LocationRules {

    /**
     * A cached fix older than this triggers ONE fresh-fix attempt. Two minutes: long
     * enough that consecutive prompts in a conversation cost nothing, short enough that
     * "what's near me" is not answered from the last town.
     */
    const val MAX_FIX_AGE_MS = 120_000L

    /** Coordinates are rounded to ~1 m. Beyond that is float noise, not information. */
    private const val COORD_DECIMALS = 5

    fun isStale(ageMs: Long): Boolean = ageMs > MAX_FIX_AGE_MS

    /** Read the cache first, always; only escalate when it is missing or stale. */
    fun needsFreshFix(hasLastFix: Boolean, lastFixAgeMs: Long): Boolean =
        !hasLastFix || isStale(lastFixAgeMs)

    /**
     * Untrusted-input validation. Sensors and mocks both produce junk: NaN from a
     * half-initialised fix, 0/0 "null island" from a provider that never got a fix,
     * out-of-range values from a spoofer.
     */
    fun isValidCoordinate(lat: Double, lon: Double): Boolean =
        lat.isFinite() && lon.isFinite() &&
            lat >= -90.0 && lat <= 90.0 &&
            lon >= -180.0 && lon <= 180.0 &&
            !(abs(lat) < 1e-9 && abs(lon) < 1e-9)

    /**
     * The payload builder. Invalid coordinates yield **null** — never a partial object,
     * never a place name with no position.
     */
    fun build(
        lat: Double,
        lon: Double,
        accuracyM: Double? = null,
        place: GeoPlace? = null,
    ): UserLocation? {
        if (!isValidCoordinate(lat, lon)) return null
        val accuracy = accuracyM?.takeIf { it.isFinite() && it >= 0.0 }?.let { round(it, 1) }
        return UserLocation(
            lat = round(lat, COORD_DECIMALS),
            lon = round(lon, COORD_DECIMALS),
            accuracyM = accuracy,
            place = place?.label?.trim()?.takeIf { it.isNotEmpty() },
            city = place?.city?.trim()?.takeIf { it.isNotEmpty() },
            state = place?.state?.trim()?.takeIf { it.isNotEmpty() },
        )
    }

    /** "City, Region" from a geocoded address, degrading through what is actually present. */
    fun placeName(
        locality: String?,
        adminArea: String?,
        subAdminArea: String? = null,
        countryName: String? = null,
    ): String? {
        val parts = placeParts(locality, adminArea, subAdminArea, countryName)
        return parts.label
    }

    /**
     * Split a geocoded address into the parts Brandon asked to ship — city + state — plus
     * the composed label. Degrades through what the Geocoder actually returned: a county
     * stands in for a missing locality, a country for a missing admin area.
     */
    fun placeParts(
        locality: String?,
        adminArea: String?,
        subAdminArea: String? = null,
        countryName: String? = null,
    ): GeoPlace {
        val city = firstNonBlank(locality, subAdminArea)
        val state = firstNonBlank(adminArea, countryName)
        val label = when {
            // City-states (Singapore) would otherwise read "Singapore, Singapore".
            city != null && state != null && !city.equals(state, ignoreCase = true) -> "$city, $state"
            city != null -> city
            state != null -> state
            else -> null
        }
        return GeoPlace(city = city, state = state, label = label)
    }

    /** Android-typed shim over [placeParts]; kept here so the provider stays thin. */
    fun placeOf(address: Address?): GeoPlace? = address
        ?.let { placeParts(it.locality, it.adminArea, it.subAdminArea, it.countryName) }
        ?.takeIf { !it.isEmpty() }

    /**
     * The capture orchestration, injectable end to end.
     *
     * Order and failure semantics:
     *  1. Not permitted (denied permission **or** the settings toggle off) → null, and
     *     nothing is read.
     *  2. Cached fix is adopted IMMEDIATELY, before any escalation, so a slow or hanging
     *     fresh-fix attempt can only ever *improve* the result — it can never lose a
     *     usable position to the timeout.
     *  3. A fresh fix is attempted only when the cache is missing or stale, and only
     *     inside the remaining budget.
     *  4. The place name is looked up last, with whatever budget is left. Geocoder
     *     failure, absence, or timeout leaves the coordinates intact.
     *  5. The whole thing is fenced by [budgetMs] and by per-step failure isolation:
     *     any throw from any injected step degrades to "no location", never to a crash.
     */
    suspend fun captureWith(
        budgetMs: Long,
        permitted: Boolean,
        lastFix: suspend () -> RawFix?,
        freshFix: suspend () -> RawFix?,
        geocode: suspend (Double, Double) -> GeoPlace?,
    ): UserLocation? {
        if (!permitted) return null
        if (budgetMs <= 0L) return null

        var fix: RawFix? = null
        var place: GeoPlace? = null

        withTimeoutOrNull(budgetMs) {
            val cached = quiet { lastFix() }
            // Adopt the cached fix FIRST: a stale-but-real position beats none, and this
            // is what survives if everything after here times out.
            if (cached != null) fix = cached
            if (needsFreshFix(cached != null, cached?.ageMs ?: Long.MAX_VALUE)) {
                val fresh = quiet { freshFix() }
                if (fresh != null) fix = fresh
            }
            val current = fix ?: return@withTimeoutOrNull
            place = quiet { geocode(current.lat, current.lon) }
        }

        val resolved = fix ?: return null
        return build(resolved.lat, resolved.lon, resolved.accuracyM, place)
    }

    /**
     * Swallow step failures, but NEVER a cancellation — swallowing that would let a step
     * run past the deadline the budget just enforced.
     */
    private inline fun <T> quiet(block: () -> T): T? = try {
        block()
    } catch (c: CancellationException) {
        throw c
    } catch (_: Throwable) {
        null
    }

    private fun firstNonBlank(vararg values: String?): String? =
        values.firstOrNull { !it.isNullOrBlank() }?.trim()

    private fun round(value: Double, decimals: Int): Double {
        var factor = 1.0
        repeat(decimals) { factor *= 10.0 }
        return (value * factor).roundToLong() / factor
    }
}
