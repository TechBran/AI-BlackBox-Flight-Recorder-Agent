package com.aiblackbox.portal.overlay

import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * The on-device single-frame SCREEN CAPTURE seam (Task W4.2) — grabs ONE screen
 * frame as PNG bytes for the "look at my screen" vision path, with PASSWORD
 * REDACTION at the boundary.
 *
 * ## Security guarantee (the point of the redaction gate)
 * A screenshot would bypass the accessibility-text password redaction (which masks
 * password NODE text via [nodeText]). So before capturing, the implementation asks
 * whether a password field is currently focused ([UiTreeReader.isPasswordFieldFocused])
 * and, if so, REFUSES the capture — returning [ScreenCaptureResult.RefusedPassword]
 * instead of bytes. A credential entry is NEVER screenshotted.
 *
 * ## Ephemerality guarantee
 * The captured PNG bytes are EPHEMERAL: they are produced in memory (no file), used
 * ONLY to build the model's prompt ([com.aiblackbox.portal.data.local.VisionLlm.generateWithImage]),
 * and never written to the BlackBox ledger / snapshot transcript. The
 * [OverlayScreenCapture] implementation returns the bytes and keeps no reference;
 * the caller must drop them after the turn.
 *
 * ## Design: pure decision + thin framework shell
 * The redaction DECISION ([shouldRefuseCapture]) is a pure, JVM-unit-testable
 * top-level function. The capture itself (MediaProjection → ImageReader → PNG) and
 * the focus query are framework/device-verified, behind this seam so the chat
 * caller can be tested with a fake.
 */
interface ScreenCapture {
    /**
     * Capture one screen frame, applying the password-redaction gate first.
     *
     *  - A password field is focused → [ScreenCaptureResult.RefusedPassword] (no
     *    capture happens).
     *  - Capture unavailable (overlay/projection not running, or no frame) →
     *    [ScreenCaptureResult.Unavailable] with a customer-facing reason.
     *  - Otherwise → [ScreenCaptureResult.Success] with the PNG bytes.
     *
     * Suspends until the (callback-based) framework capture completes.
     */
    suspend fun capture(): ScreenCaptureResult
}

/** The outcome of a [ScreenCapture.capture]. */
sealed interface ScreenCaptureResult {
    /** Captured frame as PNG bytes (EPHEMERAL — never persist these). */
    data class Success(val pngBytes: ByteArray) : ScreenCaptureResult {
        // ByteArray needs structural equals/hashCode for value semantics.
        override fun equals(other: Any?): Boolean =
            this === other || (other is Success && pngBytes.contentEquals(other.pngBytes))
        override fun hashCode(): Int = pngBytes.contentHashCode()
    }

    /** Refused because a password field was focused (the redaction gate fired). */
    data object RefusedPassword : ScreenCaptureResult

    /** Capture could not be performed; [reason] is customer-facing. */
    data class Unavailable(val reason: String) : ScreenCaptureResult
}

/**
 * THE REDACTION DECISION (Task W4.2) — refuse a screen capture when a password
 * field is focused. Pure + unit-tested ([ScreenCaptureTest]); the only input is the
 * focused-password fact, so the rule is one testable place. Today it's identity
 * (refuse iff a password is focused), named so a future nuance lands here.
 */
fun shouldRefuseCapture(passwordFocused: Boolean): Boolean = passwordFocused

/** Customer-facing reasons for an [ScreenCaptureResult.Unavailable]. */
const val CAPTURE_UNAVAILABLE_NO_OVERLAY =
    "Screen capture isn't available right now. Start the BlackBox overlay (which grants screen access) and try again."
const val CAPTURE_UNAVAILABLE_NO_FRAME =
    "Couldn't capture the screen. Please try again."

/**
 * Production [ScreenCapture]: reuses the running [OverlayService]'s MediaProjection
 * (via [OverlayService.captureScreenPng]) and the live accessibility tree (via
 * [uiTree]) for the password gate. No new projection-consent flow — the overlay's
 * existing consent is reused.
 *
 * Every framework fact is a constructor SEAM (a lambda) so the gate ordering +
 * result mapping unit-test with plain JUnit — no Service, no Robolectric, no
 * AccessibilityNodeInfo:
 * @param passwordFocused whether a password field is focused; production reads the
 *   live tree via [UiTreeReader.fromService] → [UiTreeReader.isPasswordFieldFocused].
 * @param overlayRunning whether the overlay (and thus the MediaProjection) is up;
 *   production is [OverlayService.isRunning].
 * @param overlayCapture the framework capture hop; production is
 *   [OverlayService.Companion.captureScreenPng].
 */
class OverlayScreenCapture(
    private val passwordFocused: () -> Boolean = { UiTreeReader.fromService().isPasswordFieldFocused() },
    private val overlayRunning: () -> Boolean = { OverlayService.isRunning() },
    private val overlayCapture: ((ByteArray?) -> Unit) -> Unit = { cb ->
        OverlayService.captureScreenPng(cb)
    },
) : ScreenCapture {

    override suspend fun capture(): ScreenCaptureResult {
        // 1. REDACTION GATE FIRST — never even grab a frame of a password entry.
        //    (No logging here: this method is JVM-unit-tested via injected lambdas,
        //    and android.util.Log throws in the unit-test android.jar; the result
        //    type already communicates the refusal.)
        if (shouldRefuseCapture(passwordFocused())) {
            return ScreenCaptureResult.RefusedPassword
        }
        // 2. Grab one frame via the overlay's MediaProjection (callback -> suspend).
        if (!overlayRunning()) {
            return ScreenCaptureResult.Unavailable(CAPTURE_UNAVAILABLE_NO_OVERLAY)
        }
        val bytes = suspendCancellableCoroutine<ByteArray?> { cont ->
            overlayCapture { result -> if (cont.isActive) cont.resume(result) }
        }
        return if (bytes != null && bytes.isNotEmpty()) {
            ScreenCaptureResult.Success(bytes)
        } else {
            ScreenCaptureResult.Unavailable(CAPTURE_UNAVAILABLE_NO_FRAME)
        }
    }
}
