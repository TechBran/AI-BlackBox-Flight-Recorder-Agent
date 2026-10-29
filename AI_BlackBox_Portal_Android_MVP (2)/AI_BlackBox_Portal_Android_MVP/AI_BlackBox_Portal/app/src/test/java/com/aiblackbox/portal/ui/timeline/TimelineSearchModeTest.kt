package com.aiblackbox.portal.ui.timeline

import android.app.Application
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * Review follow-up to A14-ui-misc-09: the timeline now restarts its poll on every resume,
 * and the restart used to leave search mode, so a search, a screen lock and a return
 * had the recent feed overwrite the search results within one poll (the query still in
 * the field). Only clearing the search, opening the screen or a new operator leaves it.
 *
 * Hermetic: the view model's launches are queued on a test Main dispatcher that is never
 * advanced, so no request is ever made.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class TimelineSearchModeTest {

    private val dispatcher = StandardTestDispatcher()

    @Before fun setUp() = Dispatchers.setMain(dispatcher)

    @After fun tearDown() = Dispatchers.resetMain()

    private fun vm() = TimelineViewModel(Application()).apply { initialize("http://box.test") }

    @Test fun `a resume keeps the search results on screen`() {
        val vm = vm()
        vm.search("ledger rails")
        assertTrue(vm.inSearchMode)

        vm.stopPolling()          // screen locked / app switched
        vm.startPolling("op-a")   // and back
        assertTrue("the poll must not replace the search results", vm.inSearchMode)
        vm.stopPolling()
    }

    @Test fun `clearing the search returns to the live feed`() {
        val vm = vm()
        vm.search("ledger rails")
        vm.clearSearch("op-a")
        assertFalse(vm.inSearchMode)
    }

    @Test fun `opening the screen or switching operator starts on the live feed`() {
        var dir: File? = File(".").absoluteFile
        var src: String? = null
        while (dir != null && src == null) {
            for (rel in listOf("src/main/java/com/aiblackbox/portal/ui/timeline/TimelineScreen.kt",
                "app/src/main/java/com/aiblackbox/portal/ui/timeline/TimelineScreen.kt")) {
                val f = File(dir, rel)
                if (f.isFile) src = f.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        val s = src ?: run { fail("TimelineScreen.kt not found"); "" }
        val open = s.substring(s.indexOf("LaunchedEffect(origin, operator) {"))
        assertTrue("the first load leaves search mode", "viewModel.clearSearch(operator)" in open.substring(0, open.indexOf("\n    }\n")))
        val resume = s.substring(s.indexOf("LifecycleResumeEffect(origin, operator) {"))
        assertTrue("a resume only restarts the poll", "viewModel.startPolling(operator)" in resume.substring(0, resume.indexOf("\n    }\n")))
    }
}
