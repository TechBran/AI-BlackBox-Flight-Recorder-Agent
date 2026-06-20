package com.aiblackbox.portal.data.repository

import android.util.Log
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

// =============================================================================
// ImageCatalogRepository -- aligned with Portal generation-modals.js (Task 8).
//
// Both UIs hydrate the SAME GET /image/catalog so they stay in lock-step:
//   {"providers":[{"provider","label","default?",
//                  "params":[{"name","type","options?","min?","max?","default?"}]}]}
//
// Only ENABLED providers are returned; one carries default:true. The param names
// (aspectRatio/resolution/numberOfImages/size/quality) flow end-to-end through
// the GenIn model to the provider API -- see Orchestrator/image_catalog.py.
//
// Fails open to a single-provider fallback (mirrors Portal IMAGE_CATALOG_FALLBACK)
// so the screen is never broken if the endpoint errors / returns empty.
// =============================================================================

private const val TAG = "ImageCatalogRepo"

/**
 * One image-provider param spec from GET /image/catalog.
 * [type] is "enum" or "int"; [options] is present for enums; [min]/[max] for ints.
 * [default] is the catalog default rendered as the raw string (e.g. "16:9", "1").
 */
data class ImageParamSpec(
    val name: String,
    val type: String,
    val options: List<String> = emptyList(),
    val min: Int? = null,
    val max: Int? = null,
    val default: String? = null,
)

/**
 * One enabled image provider + its param schema. Mirrors a Portal catalog entry.
 */
data class ImageCatalogProvider(
    val provider: String,
    val label: String,
    val default: Boolean = false,
    val params: List<ImageParamSpec> = emptyList(),
)

class ImageCatalogRepository(private val api: BlackBoxApi) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    companion object {
        /** Human-friendly labels for known param names (fallback: the raw name). */
        val IMAGE_PARAM_LABELS: Map<String, String> = mapOf(
            "aspectRatio" to "Aspect Ratio",
            "resolution" to "Resolution",
            "numberOfImages" to "Images",
            "size" to "Size",
            "quality" to "Quality",
        )

        /**
         * Fail-open single-provider catalog if GET /image/catalog is empty/errors.
         * Mirrors Portal IMAGE_CATALOG_FALLBACK (Gemini Nano Banana).
         */
        val IMAGE_CATALOG_FALLBACK: List<ImageCatalogProvider> = listOf(
            ImageCatalogProvider(
                provider = "gemini",
                label = "Gemini Nano Banana",
                default = true,
                params = listOf(
                    ImageParamSpec(
                        "aspectRatio", "enum",
                        options = listOf("1:1", "16:9", "9:16", "4:3", "3:4"),
                        default = "16:9",
                    ),
                    ImageParamSpec(
                        "resolution", "enum",
                        options = listOf("1K", "2K"),
                        default = "1K",
                    ),
                    ImageParamSpec(
                        "numberOfImages", "int",
                        min = 1, max = 4, default = "1",
                    ),
                ),
            ),
        )
    }

    /**
     * Fetch the image provider catalog. Returns [IMAGE_CATALOG_FALLBACK] on ANY
     * failure (network, parse, empty) so the screen always has a usable provider.
     */
    suspend fun fetchCatalog(): List<ImageCatalogProvider> = try {
        val raw = api.get("/image/catalog")
        val providers = json.parseToJsonElement(raw).jsonObject["providers"]?.jsonArray
            ?: return IMAGE_CATALOG_FALLBACK
        val parsed = providers.map { p ->
            val o = p.jsonObject
            ImageCatalogProvider(
                provider = o["provider"]!!.jsonPrimitive.content,
                label = o["label"]?.jsonPrimitive?.content
                    ?: o["provider"]!!.jsonPrimitive.content,
                default = o["default"]?.jsonPrimitive?.content?.toBoolean() ?: false,
                params = (o["params"]?.jsonArray ?: JsonArray(emptyList())).map { sp ->
                    val so = sp.jsonObject
                    ImageParamSpec(
                        name = so["name"]!!.jsonPrimitive.content,
                        type = so["type"]?.jsonPrimitive?.content ?: "string",
                        options = (so["options"]?.jsonArray ?: JsonArray(emptyList()))
                            .map { it.jsonPrimitive.content },
                        min = so["min"]?.jsonPrimitive?.content?.toIntOrNull(),
                        max = so["max"]?.jsonPrimitive?.content?.toIntOrNull(),
                        default = so["default"]?.jsonPrimitive?.content,
                    )
                },
            )
        }
        if (parsed.isEmpty()) IMAGE_CATALOG_FALLBACK else parsed
    } catch (e: Exception) {
        Log.w(TAG, "fetchCatalog failed, using offline fallback: ${e.message}")
        IMAGE_CATALOG_FALLBACK
    }
}
