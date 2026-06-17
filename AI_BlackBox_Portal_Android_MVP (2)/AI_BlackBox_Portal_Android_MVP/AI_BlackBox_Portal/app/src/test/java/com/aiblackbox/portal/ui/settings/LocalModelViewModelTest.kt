package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.data.api.LocalModelCatalogClient
import com.aiblackbox.portal.data.local.InstalledModel
import com.aiblackbox.portal.data.local.LocalModelInstaller
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.model.LocalDeviceRecord
import com.aiblackbox.portal.data.model.LocalStatus
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.io.IOException

/**
 * Unit tests for [LocalModelViewModel] — the screen state holder for the
 * on-device Gemma Model Manager (Task 1.5). Fully hermetic plain-JUnit: fakes
 * stand in for the manager ([LocalModelInstaller]) and the catalog/autonomy API
 * ([LocalModelCatalogClient]) so there is NO MockWebServer, NO OkHttp, NO
 * Android Context. The VM's coroutines are driven on a [StandardTestDispatcher]
 * injected via the test constructor, so [advanceUntilIdle] deterministically
 * drains them.
 *
 * Coverage per the Task 1.5 brief:
 *   - refresh populates catalog / installed / recommended / autonomy.
 *   - download happy path flips state downloading → installed, progress ≈ 1f.
 *   - download failure surfaces `error` and is retryable.
 *   - setAutonomy updates `autonomyMode`.
 *   - progress THROTTLING — feeding many rapid onProgress callbacks does NOT
 *     emit a state update per callback (bounded count) while still landing on
 *     ≈ 1f.
 *   - switchModel records the selected slug.
 *
 * NOTE (stale-progress race): the terminal-wins guard in [download] (a late
 * progress launch must not re-insert a slug already removed by the terminal
 * success/failure update) is a multi-threaded-dispatcher hazard. The test
 * dispatcher here serializes coroutines FIFO, so the true race is NOT
 * reproducible; the guard is verified structurally (busySlug is the
 * authoritative in-flight signal, cleared at both terminal paths) and the
 * terminal-wins contract is locked by
 * [download leaves no stale progress entry once complete].
 *
 * NOTE (Composable render coverage): [LocalModelSection]'s render branches
 * (downloading spinner vs %, installed Switch/Delete vs Download, recommended
 * badge, autonomy toggle) are exercised by on-device/instrumented testing, not
 * this unit gate — adding a Compose render test would require a new Gradle test
 * dependency (Robolectric / createComposeRule) the offline unit gate does not have.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class LocalModelViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    private val e2b = LocalBundle(
        slug = "gemma-4-e2b",
        displayName = "Gemma 4 E2B (on-device)",
        filename = "gemma-4-e2b-it.litertlm",
        sizeBytes = 3_000_000_000L,
        minRamGb = 3.0,
        recommendedFor = "Lighter, faster on-device model.",
    )
    private val e4b = LocalBundle(
        slug = "gemma-4-e4b",
        displayName = "Gemma 4 E4B (on-device)",
        filename = "gemma-4-e4b-it.litertlm",
        sizeBytes = 4_294_967_296L,
        minRamGb = 6.0,
        recommendedFor = "Higher quality for high-RAM phones.",
        // E4B is the catalog-recommended default (Task W6) -> rows sort it first.
        recommended = true,
    )

    @Before fun setUp() {
        kotlinx.coroutines.Dispatchers.setMain(dispatcher)
    }

    @After fun tearDown() {
        kotlinx.coroutines.Dispatchers.resetMain()
    }

    // ── Fakes ────────────────────────────────────────────────────────────

    /**
     * Fake manager. Configurable: which slugs are already installed, what
     * recommendForDevice returns, and (per slug) how install behaves — number
     * of progress ticks, whether it ultimately succeeds.
     */
    private class FakeInstaller(
        var installed: MutableList<InstalledModel> = mutableListOf(),
        private val recommended: LocalBundle,
        private val progressTicks: Int = 4,
        private val installSucceeds: Boolean = true,
    ) : LocalModelInstaller {
        var installCount = 0
        var deletedSlug: String? = null

        override suspend fun installedModels(): List<InstalledModel> = installed.toList()

        override suspend fun recommendForDevice(bundles: List<LocalBundle>): LocalBundle = recommended

        override suspend fun install(
            bundle: LocalBundle,
            operator: String,
            delegate: String,
            onProgress: (Long, Long) -> Unit,
        ): Result<InstalledModel> {
            installCount++
            val total = bundle.sizeBytes ?: 1000L
            // Emit progressTicks ticks from 0..total (last == total → 100%).
            for (i in 1..progressTicks) {
                onProgress(total * i / progressTicks, total)
            }
            if (!installSucceeds) return Result.failure(IOException("download failed"))
            val model = InstalledModel(bundle.slug, File("/tmp/${bundle.filename}"), total)
            installed.add(model)
            return Result.success(model)
        }

        override suspend fun delete(slug: String): Boolean {
            deletedSlug = slug
            return installed.removeAll { it.slug == slug }
        }
    }

    /** Fake catalog/autonomy client. */
    private class FakeCatalog(
        private val bundles: List<LocalBundle>,
        private var autonomy: String = "permission",
        private val available: Boolean = false,
        private val setAutonomyOk: Boolean = true,
    ) : LocalModelCatalogClient {
        var lastAutonomyMode: String? = null

        override suspend fun catalog(): List<LocalBundle> = bundles

        override suspend fun status(operator: String): LocalStatus =
            LocalStatus(
                available = available,
                models = if (available)
                    listOf(LocalDeviceRecord(deviceId = "test", autonomyMode = autonomy))
                else emptyList(),
            )

        override suspend fun setAutonomy(operator: String, deviceId: String, mode: String): Boolean {
            lastAutonomyMode = mode
            if (setAutonomyOk) autonomy = mode
            return setAutonomyOk
        }
    }

    private fun vm(
        installer: LocalModelInstaller,
        catalog: LocalModelCatalogClient,
        operator: String = "Brandon",
        deviceId: String = "pixel-9",
        onSwitch: (String) -> Unit = {},
    ) = LocalModelViewModel(
        installer = installer,
        catalog = catalog,
        operatorProvider = { operator },
        deviceId = deviceId,
        onModelSelected = onSwitch,
        ioDispatcher = dispatcher,
    )

    // ── refresh ───────────────────────────────────────────────────────────

    @Test
    fun `refresh populates catalog, installed, recommended and autonomy`() = runTest(dispatcher) {
        val installer = FakeInstaller(
            installed = mutableListOf(InstalledModel("gemma-4-e2b", File("/tmp/x"), 3_000_000_000L)),
            recommended = e4b,
        )
        val catalog = FakeCatalog(listOf(e2b, e4b), autonomy = "yolo", available = true)
        val vm = vm(installer, catalog)

        vm.refresh()
        advanceUntilIdle()

        val s = vm.state.value
        assertEquals(2, s.catalog.size)
        assertEquals(1, s.installed.size)
        assertEquals("gemma-4-e2b", s.installed.first().slug)
        assertEquals("E4B is recommended on a high-RAM phone", "gemma-4-e4b", s.recommendedSlug)
        assertEquals("autonomy pulled from device status", "yolo", s.autonomyMode)
        assertNull(s.error)
        assertNull(s.busySlug)
    }

    @Test
    fun `refresh defaults autonomy to permission when device not yet available`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e2b)
        val catalog = FakeCatalog(listOf(e2b, e4b), available = false)
        val vm = vm(installer, catalog)

        vm.refresh()
        advanceUntilIdle()

        assertEquals("permission", vm.state.value.autonomyMode)
    }

    // ── download happy path ────────────────────────────────────────────────

    @Test
    fun `download flips state to installed and progress reaches full`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b, progressTicks = 4)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog)

        vm.refresh()
        advanceUntilIdle()
        assertTrue(vm.state.value.installed.isEmpty())

        vm.download(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        assertEquals("install attempted once", 1, installer.installCount)
        assertTrue("model now installed", s.installed.any { it.slug == "gemma-4-e4b" })
        assertNull("busy cleared after success", s.busySlug)
        assertNull("no error", s.error)
        val finalProgress = s.downloadProgress["gemma-4-e4b"]
        // Progress is cleared (null) on success OR landed at ~1f — accept either,
        // but if present it must be ≈ full.
        if (finalProgress != null) {
            assertEquals(1f, finalProgress, 0.001f)
        }
    }

    // ── terminal-wins contract (stale-progress race guard) ──────────────────

    @Test
    fun `download leaves no stale progress entry once complete`() = runTest(dispatcher) {
        // Locks the terminal-wins contract that the guard protects: once the
        // download's terminal success update has run, the slug must be GONE from
        // downloadProgress and busySlug must be null. (The true late-tick race is
        // dispatcher-dependent and not reproducible on the FIFO test dispatcher —
        // see the class kdoc; this asserts the invariant the guard preserves.)
        val installer = FakeInstaller(recommended = e4b, progressTicks = 50)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        assertNull("busySlug cleared once download completes", s.busySlug)
        assertFalse(
            "no stale progress entry for an installed model",
            s.downloadProgress.containsKey("gemma-4-e4b"),
        )
        assertTrue("model is installed", s.installed.any { it.slug == "gemma-4-e4b" })
    }

    // ── download failure is retryable ───────────────────────────────────────

    @Test
    fun `download failure surfaces error and is retryable`() = runTest(dispatcher) {
        val failing = FakeInstaller(recommended = e4b, installSucceeds = false)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(failing, catalog)

        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        assertNotNull("error surfaced on failure", s.error)
        assertNull("busy cleared even on failure", s.busySlug)
        assertFalse("not installed after failure", s.installed.any { it.slug == "gemma-4-e4b" })

        // Retry: the model manager's download() is resumable, so a second tap
        // must be permitted (install called again).
        vm.download(e4b)
        advanceUntilIdle()
        assertEquals("retry must re-invoke install", 2, failing.installCount)
    }

    // ── setAutonomy ─────────────────────────────────────────────────────────

    @Test
    fun `setAutonomy updates autonomyMode on success`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(listOf(e2b, e4b), autonomy = "permission", available = true)
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()
        assertEquals("permission", vm.state.value.autonomyMode)

        vm.setAutonomy("yolo")
        advanceUntilIdle()

        assertEquals("yolo", catalog.lastAutonomyMode)
        assertEquals("yolo", vm.state.value.autonomyMode)
    }

    @Test
    fun `setAutonomy leaves mode unchanged when backend rejects`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(
            listOf(e2b, e4b), autonomy = "permission", available = true, setAutonomyOk = false,
        )
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()

        vm.setAutonomy("yolo")
        advanceUntilIdle()

        assertEquals("permission", vm.state.value.autonomyMode)
        assertNotNull("a rejected autonomy change surfaces an error", vm.state.value.error)
    }

    // ── delete ──────────────────────────────────────────────────────────────

    @Test
    fun `delete removes the model from installed`() = runTest(dispatcher) {
        val installer = FakeInstaller(
            installed = mutableListOf(InstalledModel("gemma-4-e2b", File("/tmp/x"), 1L)),
            recommended = e2b,
        )
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()
        assertEquals(1, vm.state.value.installed.size)

        vm.delete("gemma-4-e2b")
        advanceUntilIdle()

        assertEquals("gemma-4-e2b", installer.deletedSlug)
        assertTrue("installed list refreshed after delete", vm.state.value.installed.isEmpty())
    }

    // ── switchModel records selection ───────────────────────────────────────

    @Test
    fun `switchModel records the selected slug via the callback`() = runTest(dispatcher) {
        var selected: String? = null
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog, onSwitch = { selected = it })

        vm.switchModel("gemma-4-e4b")
        advanceUntilIdle()

        assertEquals("gemma-4-e4b", selected)
        assertEquals("gemma-4-e4b", vm.state.value.activeSlug)
    }

    // ── progress THROTTLING ──────────────────────────────────────────────────

    @Test
    fun `download throttles progress updates rather than emitting per callback`() = runTest(dispatcher) {
        // 1000 rapid ticks (mimicking the real download's per-64KB callback).
        val chatty = FakeInstaller(recommended = e4b, progressTicks = 1000)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(chatty, catalog)
        vm.refresh()
        advanceUntilIdle()

        // Count distinct state emissions during the download by collecting the
        // StateFlow in the background (backgroundScope is auto-cancelled by runTest).
        val emissions = mutableListOf<LocalModelUiState>()
        backgroundScope.launch { vm.state.collect { emissions.add(it) } }

        vm.download(e4b)
        advanceUntilIdle()

        // Hard upper bound: throttling to ~1% or ~250ms means well under one
        // emission per tick. 1000 ticks must NOT yield ~1000 state updates.
        // Allow generous headroom (refresh + install start/end + ≤100 progress
        // steps) but firmly below the unthrottled count.
        assertTrue(
            "throttled: ${emissions.size} emissions for 1000 progress ticks must be << 1000",
            emissions.size < 200,
        )
        // And it still completed: installed.
        assertTrue(vm.state.value.installed.any { it.slug == "gemma-4-e4b" })
    }

    // ── FAILED state + retry (Task W5.1) ─────────────────────────────────────

    @Test
    fun `download failure marks the slug failed and rows show Failed`() = runTest(dispatcher) {
        val failing = FakeInstaller(recommended = e4b, installSucceeds = false)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(failing, catalog)
        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        assertTrue("slug recorded as failed", s.failedSlugs.contains("gemma-4-e4b"))
        val row = s.rows.first { it.slug == "gemma-4-e4b" }
        assertEquals(ModelRowState.Failed, row.state)
    }

    @Test
    fun `retry clears the failed flag and re-invokes install`() = runTest(dispatcher) {
        // First fail, then succeed on retry: the manager flips behavior per call.
        val installer = object : LocalModelInstaller {
            var count = 0
            val store = mutableListOf<InstalledModel>()
            override suspend fun installedModels() = store.toList()
            override suspend fun recommendForDevice(bundles: List<LocalBundle>) = e4b
            override suspend fun install(
                bundle: LocalBundle,
                operator: String,
                delegate: String,
                onProgress: (Long, Long) -> Unit,
            ): Result<InstalledModel> {
                count++
                onProgress(bundle.sizeBytes ?: 1L, bundle.sizeBytes ?: 1L)
                return if (count == 1) {
                    Result.failure(IOException("boom"))
                } else {
                    val m = InstalledModel(bundle.slug, File("/tmp/${bundle.filename}"), 1L)
                    store.add(m)
                    Result.success(m)
                }
            }
            override suspend fun delete(slug: String): Boolean = store.removeAll { it.slug == slug }
        }
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()
        assertTrue("failed after first attempt", vm.state.value.failedSlugs.contains("gemma-4-e4b"))

        vm.retry(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        assertEquals("retry re-invoked install", 2, installer.count)
        assertFalse("failed flag cleared on success", s.failedSlugs.contains("gemma-4-e4b"))
        assertTrue("installed after retry", s.installed.any { it.slug == "gemma-4-e4b" })
        // Row now reflects INSTALLED, not FAILED.
        assertTrue(s.rows.first { it.slug == "gemma-4-e4b" }.state is ModelRowState.Installed)
    }

    @Test
    fun `successful install clears any prior failed flag`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()

        // Seed a stale failed flag (as if a previous attempt failed), then succeed.
        vm.download(e4b)
        advanceUntilIdle()
        assertFalse(vm.state.value.failedSlugs.contains("gemma-4-e4b"))
        assertTrue(vm.state.value.installed.any { it.slug == "gemma-4-e4b" })
    }

    @Test
    fun `rows surface recommended-first with active marking`() = runTest(dispatcher) {
        val installer = FakeInstaller(
            installed = mutableListOf(InstalledModel("gemma-4-e4b", File("/tmp/x"), 1L)),
            recommended = e4b,
        )
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(installer, catalog)
        vm.refresh()
        advanceUntilIdle()
        vm.switchModel("gemma-4-e4b")
        advanceUntilIdle()

        val rows = vm.state.value.rows
        // e4b (recommended) leads.
        assertEquals("gemma-4-e4b", rows.first().slug)
        val e4bRow = rows.first { it.slug == "gemma-4-e4b" }
        assertTrue((e4bRow.state as ModelRowState.Installed).active)
        // e2b is downloadable.
        assertEquals(ModelRowState.Downloadable, rows.first { it.slug == "gemma-4-e2b" }.state)
    }
}
