package com.aiblackbox.portal.ui.cron

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * A legacy cron job (no `provider`) keeps a sensible provider in the editor: the catalog
 * that lists its model, else the provider its id names (rank 76: the static
 * Constants.MODEL_CONFIG lists this used to scan are now Auto-only seeds).
 */
class CronModelProviderTest {

    private val none = emptyMap<String, List<String>>()

    @Test fun `a known catalog that lists the id wins`() {
        val catalogs = linkedMapOf(
            "google" to listOf("gemini-3.1-pro"),
            "anthropic" to listOf("claude-opus-5-5"),
            "computer-use" to listOf("claude-opus-5-5", "gemini-cu"),
        )
        // Picker order: anthropic before computer-use for an id both list.
        assertEquals("anthropic", CronViewModel.cronProviderForModelId("claude-opus-5-5", catalogs))
        assertEquals("computer-use", CronViewModel.cronProviderForModelId("gemini-cu", catalogs))
    }

    @Test fun `with no catalog loaded the id names its provider`() {
        assertEquals("anthropic", CronViewModel.cronProviderForModelId("claude-opus-4-8", none))
        assertEquals("google", CronViewModel.cronProviderForModelId("gemini-2.5-pro", none))
        assertEquals("openai", CronViewModel.cronProviderForModelId("gpt-5.5", none))
        assertEquals("openai", CronViewModel.cronProviderForModelId("o4-mini", none))
        assertEquals("xai", CronViewModel.cronProviderForModelId("grok-4.3", none))
        assertEquals("custom", CronViewModel.cronProviderForModelId("gemma-box::gemma-26b", none))
        assertNull(CronViewModel.cronProviderForModelId("mystery-model", none))
    }
}
