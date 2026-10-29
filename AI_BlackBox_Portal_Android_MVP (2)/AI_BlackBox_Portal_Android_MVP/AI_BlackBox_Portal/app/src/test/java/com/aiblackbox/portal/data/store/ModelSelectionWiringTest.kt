package com.aiblackbox.portal.data.store

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * W22 / A11-settings-generation-06 — WIRING guard. SettingsViewModel (an
 * AndroidViewModel) and the composer pill in BlackBoxApp cannot be stood up on
 * the plain JVM, so their behaviour is tested through the shared path they delegate
 * to (ModelSelectionTest). This pins that they DO delegate — the surfaces drifted
 * once because each wrote the keys its own way:
 *  - both provider switches go through BlackBoxStore.setProviderAndModel (never a
 *    bare setProvider that leaves the previous provider's id in the global key), the
 *    composer pill's and the CU screen's by way of ProviderModelWiring;
 *  - every model picker goes through BlackBoxStore.selectModel: the Settings sheet
 *    (the on-device "Use" included, which selectModel keeps out of the global key),
 *    the composer pill, and the CU + robotics screens (BlackBoxNavGraph's
 *    onModelChange) — a picker that writes only the global key leaves a stale
 *    model_<provider> for the next switch back to restore;
 *  - the Settings sheet DISPLAYS the global model — the id ChatViewModel sends.
 *  - the DataStore is built with the retired-id migration and the pairing-operator one.
 */
class ModelSelectionWiringTest {

    private fun moduleSource(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    private val root = "src/main/java/com/aiblackbox/portal"

    /** The body of `fun <name>(` up to the next top-level member (good enough for one-liners). */
    private fun functionBody(src: String, name: String): String {
        val start = src.indexOf("fun $name(")
        assertTrue("fun $name( not found", start >= 0)
        val next = src.indexOf("\n    fun ", start + 1).let { if (it < 0) src.length else it }
        return src.substring(start, next)
    }

    @Test fun `the Settings sheet switches provider through the shared path`() {
        val vm = moduleSource("$root/ui/settings/SettingsViewModel.kt")
        val setProvider = functionBody(vm, "setProvider")
        assertTrue(setProvider, "store.setProviderAndModel(" in setProvider)
        assertTrue(setProvider, "fetchLiveModelIds(" in setProvider)
        assertFalse(setProvider, "store.setProvider(" in setProvider)

        val setModel = functionBody(vm, "setModel")
        assertTrue(setModel, "store.selectModel(" in setModel)
        assertFalse(setModel, "store.setModel(" in setModel)
    }

    /** A ProviderModelWiring member's body, through its closing brace at member indent. */
    private fun wiringMember(src: String, name: String): String {
        val start = src.indexOf("fun $name(", src.indexOf("class ProviderModelWiring("))
        assertTrue("ProviderModelWiring.$name( not found", start >= 0)
        val end = src.indexOf("\n    }", start).let { if (it < 0) src.length else it }
        return src.substring(start, end)
    }

    /** The app's composition (out of NativeMainActivity): the composer pill and the NavGraph call. */
    private val app = "$root/navigation/BlackBoxApp.kt"
    private val wiringFile = "$root/navigation/ProviderModelWiring.kt"

    @Test fun `the composer pill switches provider through the shared path`() {
        val activity = moduleSource(app)
        val pill = activity.substringAfter("onProviderChange = { newProvider ->")
            .substringBefore("// Auto-navigate")
        assertTrue(pill, "providerModels.switchProvider(newProvider)" in pill)
        assertFalse(pill, "store.setProvider(" in pill)
        assertFalse(pill, "store.setModel(" in pill)
        // The wiring is fed the live catalog's ids...
        val built = activity.substringAfter("ProviderModelWiring(store.providerModels())").substringBefore("\n    }")
        assertTrue(built, "ChatViewModel.fetchLiveModelIds(chatViewModel.getApi(), it)" in built)
        // ...and switches through the store's shared path with them.
        val wiring = moduleSource(wiringFile)
        val switch = wiringMember(wiring, "switchProvider")
        assertTrue(switch, "store.setProviderAndModel(newProvider) { liveModelIds(it) }" in switch)
        assertFalse(switch, "store.setProvider(" in switch)
        assertFalse(switch, "store.setModel(" in switch)

        // The composer's model pick (the line right after the pill) remembers per provider too.
        val composerModel = activity.substringAfter("onProviderChange = { newProvider ->")
            .substringAfter("onModelChange = ").substringBefore("\n")
        assertTrue(composerModel, "providerModels.selectModel(provider, it)" in composerModel)
        val select = wiringMember(wiring, "selectModel")
        assertTrue(select, "store.selectModel(provider, model)" in select)
        assertFalse(select, "store.setModel(" in select)
    }

    @Test fun `opening the CU screen switches provider through the shared path`() {
        val wiring = moduleSource(wiringFile)
        val entered = wiringMember(wiring, "onRouteEntered")
        val cu = entered.substringAfter("if (path == Routes.COMPUTER_USE)").substringBefore("if (path == Routes.ROBOTICS)")
        assertTrue(cu, "store.setProviderAndModel(\"computer-use\") { liveModelIds(it) }" in cu)
        assertFalse(cu, "store.setProvider(" in cu)
        // ...and every screen change goes through it.
        val activity = moduleSource(app)
        assertTrue("providerModels.onRouteEntered(entry.destination.route)" in activity)
    }

    @Test fun `the CU and robotics screens pick through the shared path`() {
        val activity = moduleSource(app)
        val navGraphCall = activity.substringAfter("BlackBoxNavGraph(").substringBefore("onSpeak = ")
        val screenModel = navGraphCall.substringAfter("onModelChange = ").substringBefore("\n")
        assertTrue(screenModel, "providerModels.selectModel(provider, it)" in screenModel)
        // No model picker in the app writes the bare global key any more.
        assertFalse("onModelChange = { scope.launch { store.setModel(" in activity)
        assertFalse("onModelChange = { scope.launch { store.selectModel(" in activity)

        // ...and that callback IS the CU + robotics screens' model picker.
        val navGraph = moduleSource("$root/navigation/NavGraph.kt")
        for (screen in listOf("CuScreen(", "RoboticsScreen(")) {
            val args = navGraph.substringAfter(screen).substringBefore("\n            )")
            assertTrue("$screen: $args", "onModelChange = onModelChange," in args)
        }
    }

    @Test fun `the Settings sheet shows the model the request sends`() {
        val sheet = moduleSource("$root/ui/settings/SettingsSheet.kt")
        assertTrue("val selectedModel = currentModel" in sheet)
        assertFalse("getString(\"model_\$provider\"" in sheet)
    }

    @Test fun `the store is built with the retired-id and pairing-operator migrations`() {
        val store = moduleSource("$root/data/store/BlackBoxStore.kt")
        val delegate = store.substringAfter("private val Context.dataStore").substringBefore("\nclass ")
        val migrations = delegate.substringAfter("produceMigrations = {").substringBefore("\n)")
        assertTrue(delegate, "RetiredModelIdMigration," in migrations)
        // The QR operator is read from the same prefs PairingActivity wrote it to.
        assertTrue(delegate, "LegacyPairingOperatorMigration {" in migrations)
        assertTrue(delegate, "getSharedPreferences(Constants.PREFS_NAME" in migrations)
        assertTrue(delegate, "getString(Constants.KEY_OPERATOR, null)" in migrations)
    }
}
