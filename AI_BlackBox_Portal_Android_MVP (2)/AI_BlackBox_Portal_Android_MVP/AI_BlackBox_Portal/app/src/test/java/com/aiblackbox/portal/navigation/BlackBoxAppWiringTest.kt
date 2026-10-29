package com.aiblackbox.portal.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * WIRING guard for the NativeMainActivity split (audit A01-activities-root-10 / NEW-android-1,
 * rank 76). The composition cannot be stood up on the JVM, so this pins its shape: the
 * activity's setContent is the theme around [BlackBoxApp] and nothing else, the activity
 * hands over only the origin and the mic grant, and the app keeps its two activity-scoped
 * view models and its three extracted collaborators instead of growing the copies back.
 */
class BlackBoxAppWiringTest {

    private fun moduleSource(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    private val root = "src/main/java/com/aiblackbox/portal"
    private val activity by lazy { moduleSource("$root/NativeMainActivity.kt") }
    private val app by lazy { moduleSource("$root/navigation/BlackBoxApp.kt") }

    @Test fun `the activity's content is the app and nothing else`() {
        val content = activity.substringAfter("        setContent {\n").substringBefore("\n        }\n")
        val code = content.lines().map { it.trim() }.filter { it.isNotEmpty() && !it.startsWith("//") }
        assertEquals(
            listOf(
                "BlackBoxTheme {",
                "BlackBoxApp(origin = origin, withMicPermission = ::withMicPermission)",
                "}",
            ),
            code,
        )
        // No composition state is left in the activity.
        for (gone in listOf("remember", "collectAsState", "LaunchedEffect", "viewModel()")) {
            assertFalse(gone, gone in activity)
        }
    }

    @Test fun `the app asks for the mic only through its activity`() {
        assertTrue("withMicPermission: (action: () -> Unit) -> Unit," in app)
        assertFalse("Manifest.permission.RECORD_AUDIO" in app)
        assertFalse("registerForActivityResult" in app)
    }

    @Test fun `the app keeps one chat and one updates view model, from the activity's store`() {
        assertEquals(1, Regex("val chatViewModel: ChatViewModel = viewModel\\(\\)").findAll(app).count())
        assertEquals(1, Regex("val updatesVm: UpdatesViewModel = viewModel\\(\\)").findAll(app).count())
    }

    @Test fun `the app speaks and switches through its collaborators`() {
        assertTrue("ProviderModelWiring(store.providerModels())" in app)
        assertTrue("ChatSpeech(" in app)
        // The /tts/batch copies and the provider-to-screen map live in those classes now.
        assertFalse("/tts/batch\")" in app)
        assertFalse("\"gemini-agents\" -> Routes.GEMINI_AGENT" in app)
        assertFalse("MODEL_CONFIG[" in app)
    }
}
