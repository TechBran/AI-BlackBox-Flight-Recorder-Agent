package com.aiblackbox.portal.ui.insets

import androidx.compose.runtime.staticCompositionLocalOf

/**
 * Per-screen toggle for the floating app chrome (operator pill +
 * snapshot count + connected indicator + timeline button) rendered by
 * [com.aiblackbox.portal.NativeMainActivity] over every route.
 *
 * Default is **true** — chrome remains visible everywhere unless an
 * ancestor composable explicitly provides `false`. Phase 4 / T20 uses
 * this to hide the chrome when the CLI Agent screen is showing a live
 * terminal session, where the SessionSwitcherTopBar owns the top region.
 *
 * Why CompositionLocal and not a ViewModel:
 *   - The pill lives high up in the composition tree (activity scaffold),
 *     but the decision to hide is per-screen.
 *   - The visibility doesn't need to survive process death, observe
 *     outside-composition state, or be queried from non-UI code — so a
 *     ViewModel would be over-engineered.
 *   - CompositionLocal is the idiomatic Compose pattern for "descendant
 *     wants to influence ancestor rendering without explicit plumbing
 *     through every layer."
 *
 * Why `staticCompositionLocalOf` (not `compositionLocalOf`):
 *   - `static` skips per-subscriber tracking. On change, ALL readers
 *     invalidate — which is fine here because the boolean toggles rarely
 *     (only on screen route transitions, not per-frame) and there's
 *     exactly one reader after T20's [AppChromeLayer] extraction.
 *   - Cheaper for booleans that rarely change.
 *
 * Usage from a screen:
 *
 *   CompositionLocalProvider(LocalShowAppChrome provides !terminalActive) {
 *       Scaffold(topBar = { ... }) { ... }
 *   }
 *
 * Usage from the chrome consumer (NativeMainActivity):
 *
 *   val showChrome = LocalShowAppChrome.current
 *   if (showChrome) { BlackBoxTopBar(...) }
 *
 * See `docs/plans/2026-05-25-phase4-zellij-mobile.md` (T20).
 */
val LocalShowAppChrome = staticCompositionLocalOf<Boolean> { true }
