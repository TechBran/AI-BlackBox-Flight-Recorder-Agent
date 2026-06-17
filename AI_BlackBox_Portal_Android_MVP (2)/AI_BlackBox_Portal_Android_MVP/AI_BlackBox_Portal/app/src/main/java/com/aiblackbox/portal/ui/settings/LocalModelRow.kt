package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.data.local.InstalledModel
import com.aiblackbox.portal.data.model.LocalBundle

/**
 * Per-model display state for the Edge-Gallery-style on-device model picker
 * (Task W5.1). Each catalog entry resolves to exactly one of these:
 *
 *  - [Downloadable]: in the catalog, NOT on disk, nothing in flight.
 *  - [Downloading]: a download is in progress; carries the fractional progress
 *    in 0f..1f, or [PROGRESS_INDETERMINATE] (-1f) when the total is unknown.
 *  - [Installed]: present on disk (verified bytes); [active] marks the one the
 *    user has selected as the live on-device model.
 *  - [Failed]: the last download attempt failed and nothing is on disk yet —
 *    the row offers a Retry affordance (the underlying download is resumable).
 *
 * INSTALLED beats DOWNLOADABLE: a model that is both in the catalog AND on disk
 * renders as [Installed] (see [modelRowsFrom]). DOWNLOADING beats both. FAILED
 * only applies when the model is neither installed nor currently downloading.
 */
sealed interface ModelRowState {
    object Downloadable : ModelRowState
    data class Downloading(val progress: Float) : ModelRowState
    data class Installed(val active: Boolean) : ModelRowState
    object Failed : ModelRowState
}

/**
 * One fully-resolved picker row: the catalog [bundle] (the single source for the
 * display name / size / min-RAM / W6 per-model config) plus the merged [state],
 * the [recommended] flag (catalog hint, drives the badge + sort), and the
 * human-readable [contextNote] surfaced under the model name ("Recommended — ..."
 * for E4B, "Experimental — ..." for E2B).
 *
 * `fitsRam` reflects whether the device has enough RAM for the bundle's
 * `minRamGb` -- null when unknown (no RAM figure supplied to the reducer), so the
 * UI only ever warns when it is sure the model will not fit.
 */
data class ModelRow(
    val slug: String,
    val bundle: LocalBundle,
    val state: ModelRowState,
    val recommended: Boolean,
    val contextNote: String?,
    val fitsRam: Boolean? = null,
)

/** Sentinel progress fraction for "total unknown" -> render an indeterminate spinner. */
const val PROGRESS_INDETERMINATE = -1f

/**
 * Convert a (bytesSoFar, totalBytes) pair into a whole-number download percent in
 * 0..100, or -1 when the total is unknown (indeterminate). Pure + testable -- the
 * one place the progress->percent mapping lives, so the UI and tests agree.
 *
 *  - totalBytes <= 0  -> -1 (indeterminate; the row shows a spinner, not "0%").
 *  - otherwise        -> floor(soFar/total * 100), clamped to 0..100.
 */
fun progressToPercent(bytesSoFar: Long, totalBytes: Long): Int {
    if (totalBytes <= 0L) return -1
    val pct = (bytesSoFar.toDouble() / totalBytes.toDouble() * 100.0).toInt()
    return pct.coerceIn(0, 100)
}

/**
 * Convert a fractional progress in 0f..1f (or [PROGRESS_INDETERMINATE]) into a
 * whole-number percent in 0..100, or -1 when indeterminate. Pure + testable.
 */
fun fractionToPercent(fraction: Float): Int {
    if (fraction < 0f) return -1
    return (fraction.coerceIn(0f, 1f) * 100f).toInt().coerceIn(0, 100)
}

/**
 * MERGE the downloadable [catalog] with the on-disk [installed] set, the
 * in-flight [downloading] progress map, the [failed] slug set, and the
 * [activeSlug] into a per-model list of [ModelRow]s (Task W5.1). Pure + fully
 * unit-testable -- no Android, no IO.
 *
 * State precedence (highest first):
 *   1. DOWNLOADING -- the slug has a `downloading` entry (a fraction, possibly
 *      [PROGRESS_INDETERMINATE]). A download in flight wins over everything.
 *   2. INSTALLED -- the slug is in `installed` (verified bytes on disk). Installed
 *      wins over downloadable; `active == (slug == activeSlug)`.
 *   3. FAILED -- the slug is in `failed` AND not installed/downloading.
 *   4. DOWNLOADABLE -- the default (in the catalog, nothing else true).
 *
 * Ordering: the catalog-[recommended] model sorts FIRST (so the recommended
 * default leads the picker), then the rest in their catalog order (stable). The
 * `recommended` flag comes from the catalog bundle's own field (Task W6), so it
 * is the badge AND the sort key -- no second source of truth.
 *
 * **Installed-but-not-in-catalog (R2 catalog-unavailable fix).** An installed
 * model whose slug is NOT in [catalog] STILL gets a row, synthesized from the
 * [InstalledModel] itself (slug as the display name, the on-disk size, the
 * sidecar's recommended/contextNote). This is what makes a present, sideloaded
 * model -- or ANY installed model when the catalog 404s/returns empty -- render
 * instead of vanishing. Such rows are always [ModelRowState.Installed] (the
 * bytes are on disk); they have no Download/Failed affordance since there is no
 * catalog entry to (re)download from.
 *
 * @param ramBytes optional device RAM (bytes) used to compute each row's
 *   `fitsRam`. Null (the default) leaves `fitsRam` null on every row -- the merge
 *   itself never depends on RAM (recommendation/min-RAM gating lives in the
 *   manager); this is purely a display hint.
 */
fun modelRowsFrom(
    catalog: List<LocalBundle>,
    installed: List<InstalledModel>,
    downloading: Map<String, Float>,
    failed: Set<String>,
    activeSlug: String?,
    ramBytes: Long? = null,
): List<ModelRow> {
    val installedSlugs = installed.mapTo(HashSet()) { it.slug }
    val catalogSlugs = catalog.mapTo(HashSet()) { it.slug }
    val catalogRows = catalog.map { bundle ->
        val slug = bundle.slug
        val state: ModelRowState = when {
            downloading.containsKey(slug) ->
                ModelRowState.Downloading(downloading.getValue(slug))
            installedSlugs.contains(slug) ->
                ModelRowState.Installed(active = slug == activeSlug)
            failed.contains(slug) -> ModelRowState.Failed
            else -> ModelRowState.Downloadable
        }
        val fitsRam: Boolean? = ramBytes?.let { ram ->
            if (bundle.minRamGb <= 0.0) true
            else (bundle.minRamGb * BYTES_PER_GIB).toLong() <= ram
        }
        ModelRow(
            slug = slug,
            bundle = bundle,
            state = state,
            recommended = bundle.recommended,
            contextNote = bundle.contextNote?.takeIf { it.isNotBlank() },
            fitsRam = fitsRam,
        )
    }
    // R2: installed models with NO catalog entry still get a row (synthesized
    // from the InstalledModel), so a present model never vanishes when the
    // catalog 404s/returns empty. Always INSTALLED (bytes are on disk).
    val installedOnlyRows = installed
        .filter { it.slug !in catalogSlugs }
        .map { model ->
            val synthBundle = LocalBundle(
                slug = model.slug,
                displayName = model.slug,
                filename = model.file.name,
                sizeBytes = model.sizeBytes,
                recommended = model.config.recommended,
                contextNote = model.config.contextNote,
            )
            ModelRow(
                slug = model.slug,
                bundle = synthBundle,
                state = ModelRowState.Installed(active = model.slug == activeSlug),
                recommended = model.config.recommended,
                contextNote = model.config.contextNote?.takeIf { it.isNotBlank() },
                // No catalog min-RAM to test against; the model is already on disk.
                fitsRam = null,
            )
        }
    // Recommended-first, otherwise stable order (catalog rows, then installed-only).
    // sortedBy is stable, so catalog order + the installed-only suffix are preserved.
    return (catalogRows + installedOnlyRows).sortedBy { if (it.recommended) 0 else 1 }
}

/** 1 GiB, matching ActivityManager.MemoryInfo.totalMem's byte units. */
private const val BYTES_PER_GIB: Long = 1_073_741_824L
