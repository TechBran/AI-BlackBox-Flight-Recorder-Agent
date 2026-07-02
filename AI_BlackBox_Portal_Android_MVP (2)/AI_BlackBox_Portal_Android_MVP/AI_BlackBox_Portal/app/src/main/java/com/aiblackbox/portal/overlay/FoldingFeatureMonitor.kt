package com.aiblackbox.portal.overlay

import android.app.Activity
import android.util.Log
import androidx.window.layout.FoldingFeature
import androidx.window.layout.WindowInfoTracker
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * (M5.5) POSTURE MONITOR — tracks a foldable's hinge posture (FLAT / HALF_OPENED + hinge
 * orientation) via Jetpack WindowManager's [WindowInfoTracker] / [FoldingFeature], and
 * surfaces a **posture-change flag** so the cloud control loop re-observes before its next
 * coordinate action (a fold/unfold between observations invalidates every screen coordinate).
 *
 * ## Why this matters (the whole point)
 * A coordinate `tap`/`swipe` is only valid for the screen geometry it was computed against.
 * On a Galaxy Z Fold, folding/unfolding (or crossing HALF_OPENED) changes the window bounds
 * AND the display, so a coordinate the model chose one step ago can land on the wrong control
 * after a posture change. The M2 loop already re-observes each step; this monitor makes that
 * re-observe *informed* — the observation carries the current [posture] (inside
 * `device_capability`) and a `posture_changed` flag ([consumePostureChanged]) the loop reads.
 *
 * ## Pure state machine + thin framework shell
 * The state machine ([update] / [consumePostureChanged] / [currentPosture]) and the change
 * decision ([postureInvalidates]) are framework-free and JVM-unit-tested with a FAKE posture
 * (no androidx.window). Only [start] touches the framework (WindowInfoTracker collection) and
 * is device-verified. [start] NEVER crashes: on a device without WindowManager / FoldingFeature
 * (or when the collect throws) it degrades to a null posture — the form-factor classification
 * falls back to `smallestScreenWidthDp` (today's behavior) and no posture is emitted.
 *
 * ## Lifecycle
 * A single process-wide [instance] holds the last-known posture. The Activity drives the
 * collection ([start]) from a `repeatOnLifecycle(STARTED)` block, so it re-arms on every STARTED.
 * This is load-bearing on a Fold: the cover↔main display switch RECREATES the Activity (its old
 * `lifecycleScope`, and thus the prior collector, is cancelled on onDestroy), so a once-only
 * `start()` would leave posture tracking permanently dead after the first recreation. [start] is
 * therefore IDEMPOTENT PER SCOPE — it cancels any prior collection job and relaunches in the new
 * scope, so exactly one collector is ever live and it always follows the current Activity. The
 * device-side observation builder ([com.aiblackbox.portal.data.remote.ObservationBuilder.fromDevice])
 * reads [instance] whenever it builds an observation — even if the Activity is backgrounded, the
 * last-known posture stands (stale-but-safe; a real change re-arms the flag when it resumes).
 */
class FoldingFeatureMonitor {

    private val _posture = MutableStateFlow<DevicePosture?>(null)

    /** The current posture as an observable flow (null = not a foldable / posture unknown). */
    val posture: StateFlow<DevicePosture?> = _posture.asStateFlow()

    // Set true whenever [update] records a posture CHANGE; read-and-cleared by
    // [consumePostureChanged] when an observation is built. AtomicBoolean so the collector
    // thread and the observation-builder thread don't race on the flag.
    private val changed = AtomicBoolean(false)

    // The single live framework-collection job (or null). [start] cancels any prior job and
    // relaunches in the caller's scope (see the Lifecycle note), so posture tracking survives an
    // Activity recreation without ever running two collectors at once.
    @Volatile private var collectJob: Job? = null

    /** The last-known posture, or null when this is not a foldable / posture is unknown. */
    fun currentPosture(): DevicePosture? = _posture.value

    /**
     * Read-and-CLEAR the "posture changed since the last observation" flag. Returns true exactly
     * once per distinct posture change: the observation that consumes it stamps
     * `posture_changed = true`, and subsequent observations see false until the next change. This
     * is the signal the cloud loop uses to force a fresh observation before its next coordinate tap.
     *
     * NOTE (honesty): the flag is ADVISORY today — the M2 loop already re-observes every step, so
     * nothing yet keys behavior off it. It exists so a future step-skipping optimization can force
     * a re-observe across a posture change without a false-negative.
     */
    fun consumePostureChanged(): Boolean = changed.getAndSet(false)

    /**
     * Record a new [next] posture. If it INVALIDATES the current one ([postureInvalidates] — any
     * change, including null↔non-null or a FLAT↔HALF_OPENED transition), the flow updates and the
     * change flag is raised. An identical posture is a no-op (no spurious re-observe). Pure enough
     * to unit-test directly with a fake posture (this is what a fake FoldingFeature feeds).
     */
    fun update(next: DevicePosture?) {
        if (postureInvalidates(_posture.value, next)) {
            _posture.value = next
            changed.set(true)
        }
    }

    /**
     * (framework, device-verified) (Re)start collecting posture from [WindowInfoTracker] on
     * [activity] within [scope]. IDEMPOTENT PER SCOPE: any prior collection job is cancelled and a
     * fresh one launched — so a Fold cover↔main display switch (which RECREATES the Activity and
     * cancels its old lifecycleScope) re-arms posture tracking instead of leaving it dead, and no
     * second collector ever leaks. Best driven from a `repeatOnLifecycle(STARTED)` block so the
     * collection follows the Activity's STARTED lifecycle. Maps each [FoldingFeature] to a
     * [DevicePosture] via the thin [postureFromFeature] shell and feeds [update]. Wrapped so a
     * missing WindowManager / androidx.window failure degrades to null posture (never crashes the
     * Activity); a non-foldable simply never emits a posture (graceful no-op).
     */
    fun start(activity: Activity, scope: CoroutineScope) {
        val tracker = try {
            WindowInfoTracker.getOrCreate(activity)
        } catch (e: Throwable) {
            // No WindowManager / androidx.window unavailable → stay at null posture (graceful).
            Log.w(TAG, "folding monitor start failed (${e.javaClass.simpleName})")
            return
        }
        val postures: Flow<DevicePosture?> = tracker.windowLayoutInfo(activity).map { layoutInfo ->
            postureFromFeature(
                layoutInfo.displayFeatures.filterIsInstance<FoldingFeature>().firstOrNull(),
            )
        }
        restartCollection(scope, postures)
    }

    /**
     * Cancel any prior collector and launch a fresh one collecting [source] into the posture state
     * machine within [scope]; returns the new [Job]. This is THE per-scope idempotency point (no
     * double-collection: exactly one collector is ever live). Framework-free — the caller maps the
     * androidx.window flow to postures — so it is JVM-unit-testable with a fake flow (proving the
     * collection re-arms after an Activity-recreation scope cancel, the [start] latch bug's fix).
     */
    internal fun restartCollection(scope: CoroutineScope, source: Flow<DevicePosture?>): Job {
        collectJob?.cancel()
        return scope.launch {
            try {
                source.collect { update(it) }
            } catch (e: CancellationException) {
                throw e // never swallow cancellation (structured concurrency)
            } catch (e: Throwable) {
                // A framework collect failure degrades to the last-known posture (never crashes).
                Log.w(TAG, "posture collection ended (${e.javaClass.simpleName})")
            }
        }.also { collectJob = it }
    }

    /**
     * (framework) Map a live [FoldingFeature] to the wire [DevicePosture]. Null feature (no hinge
     * in the current layout) → null (not a foldable posture this step). An unrecognized state → null.
     */
    private fun postureFromFeature(feature: FoldingFeature?): DevicePosture? {
        if (feature == null) return null
        val state = when (feature.state) {
            FoldingFeature.State.HALF_OPENED -> PostureState.HALF_OPENED
            FoldingFeature.State.FLAT -> PostureState.FLAT
            else -> return null
        }
        val orientation = when (feature.orientation) {
            FoldingFeature.Orientation.VERTICAL -> HingeOrientation.VERTICAL
            FoldingFeature.Orientation.HORIZONTAL -> HingeOrientation.HORIZONTAL
            else -> null
        }
        return DevicePosture(state, orientation)
    }

    companion object {
        private const val TAG = "FoldingFeatureMonitor"

        /** The process-wide monitor the Activity starts and the observation builder reads. */
        val instance: FoldingFeatureMonitor by lazy { FoldingFeatureMonitor() }
    }
}

/**
 * PURE (M5.5): did the posture change in a way that INVALIDATES existing screen coordinates? Any
 * difference qualifies — entering/leaving a folded posture (null↔non-null) or a FLAT↔HALF_OPENED
 * transition — because each changes the window geometry a prior coordinate was computed against.
 * Structural equality on [DevicePosture] (a data class) makes this exact + testable.
 */
fun postureInvalidates(prev: DevicePosture?, next: DevicePosture?): Boolean = prev != next

/**
 * The foldable hinge posture carried in `device_capability.posture`. [state] is required
 * (FLAT / HALF_OPENED); [orientation] is the hinge axis when known. Serializes to the wire shape
 * in `docs/schema/device_capability.json` — the property names (`state` / `orientation`) and the
 * lowercase enum values match that schema EXACTLY. Omitted from the wire entirely when a device is
 * not a foldable (`posture = null`, dropped by the `explicitNulls=false` wire encoder).
 */
@Serializable
data class DevicePosture(
    val state: PostureState,
    val orientation: HingeOrientation? = null,
)

/** Hinge state. Serializes to the schema's lowercase enum (`flat` / `half_opened`). */
@Serializable
enum class PostureState {
    @SerialName("flat") FLAT,
    @SerialName("half_opened") HALF_OPENED,
}

/** Hinge axis. Serializes to the schema's lowercase enum (`vertical` / `horizontal`). */
@Serializable
enum class HingeOrientation {
    @SerialName("vertical") VERTICAL,
    @SerialName("horizontal") HORIZONTAL,
}
