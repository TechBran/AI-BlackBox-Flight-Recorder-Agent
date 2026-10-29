package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * WIRING guard for the shared model catalog (audit A01-activities-root-10, rank 76). The
 * pickers live in AndroidViewModels and composables the JVM cannot stand up, so their
 * behaviour is tested through ModelCatalogRepository and ModelSelection; this pins that
 * they DO read the shared catalog. The Settings and SMS pickers read the hand-kept
 * Constants.MODEL_CONFIG directly, which is how retired ids reached them.
 */
class ModelCatalogWiringTest {

    private fun moduleDir(): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, "src/main/java"), File(dir, "app/src/main/java"))) {
                if (candidate.isDirectory) return candidate
            }
            dir = dir.parentFile
        }
        fail("could not locate src/main/java from ${File(".").absolutePath}")
        error("unreachable")
    }

    private val root by lazy { File(moduleDir(), "com/aiblackbox/portal") }

    private fun source(rel: String) = File(root, rel).readText()

    @Test fun `only placeholder and non-catalog readers touch the static lists`() {
        fun readsStaticList(file: File) = file.readLines().any { line ->
            val code = line.trim()
            !code.startsWith("//") && !code.startsWith("*") && "MODEL_CONFIG[" in code
        }
        val readers = root.walkTopDown()
            .filter { it.isFile && it.extension == "kt" && readsStaticList(it) }
            .map { it.relativeTo(root).invariantSeparatorsPath }
            .toSortedSet()
        assertEquals(
            sortedSetOf(
                // The composer's and CU screen's fallback before any catalog is known.
                "ui/chat/Composer.kt",
                "ui/computeruse/CuScreen.kt",
                // Robotics' one model id (no catalog), and the voice providers' fallbacks.
                "navigation/ProviderModelWiring.kt",
                "ui/voice/VoiceScreen.kt",
                // Constants' entry for a provider the box has no catalog for.
                "ui/settings/SettingsViewModel.kt",
                "ui/sms/SmsViewModel.kt",
            ),
            readers,
        )
    }

    @Test fun `every picker hydrates from the shared catalog`() {
        assertTrue("ModelCatalogs.forApi(" in source("ui/chat/ChatViewModel.kt"))
        assertTrue("ModelCatalogs.forApi(" in source("ui/cron/CronViewModel.kt"))
        assertTrue("ModelCatalogs.forApi(" in source("ui/settings/SettingsViewModel.kt"))
        assertTrue("ModelCatalogs.forApi(" in source("ui/sms/SmsViewModel.kt"))

        val sheet = source("ui/settings/SettingsSheet.kt")
        assertTrue("viewModel.modelOptions.collectAsState()" in sheet)
        assertTrue("viewModel.loadModels(provider)" in sheet)
        val sms = source("ui/sms/SmsInboxScreen.kt")
        assertTrue("viewModel.smsModelOptions.collectAsStateWithLifecycle()" in sms)
        assertFalse("Constants." in sms)

        // A fresh live catalog replaces the stored picks it no longer lists.
        for (vm in listOf("ui/chat/ChatViewModel.kt", "ui/settings/SettingsViewModel.kt")) {
            assertTrue(vm, "store.reconcileModelIds(" in source(vm))
        }
        assertTrue("ModelSelection.replacementFor(" in source("ui/sms/SmsViewModel.kt"))
        assertTrue("viewModel.reconciledModel(" in source("ui/cron/CronManagerScreen.kt"))
    }

    @Test fun `the app gives the catalogs their disk copy`() {
        assertTrue("ModelCatalogs.init(this)" in source("PortalApplication.kt"))
    }
}
