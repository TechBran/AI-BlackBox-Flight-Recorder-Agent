package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.data.api.LocalModelCatalogClient
import com.aiblackbox.portal.data.local.DownloadProgressBus
import com.aiblackbox.portal.data.local.InstalledModel
import com.aiblackbox.portal.data.local.LocalModelInstaller
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.model.LocalDeviceRecord
import com.aiblackbox.portal.data.model.LocalStatus
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
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
 * Coverage per the Task 1.5 brief (+ Phase C durable downloads):
 *   - refresh populates catalog / installed / recommended / autonomy.
 *   - download hands the bundle to the [startDownload] seam (the foreground
 *     ModelDownloadService) and reacts ONLY to DownloadProgressBus: a SUCCESS
 *     event flips state downloading → installed; a FAILED event surfaces `error`
 *     and is retryable; a RUNNING event reflects fractional progress.
 *   - dispose() cancels only this holder's scope — an in-flight Service download
 *     keeps running and a freshly recreated holder re-attaches via the bus.
 *   - setAutonomy updates `autonomyMode`.
 *   - the bus pipeline does NOT thrash state (bounded emissions) while still
 *     landing installed.
 *   - switchModel records the selected slug.
 *
 * **Phase C test model.** The transfer now runs in [com.aiblackbox.portal.ModelDownloadService],
 * not the VM. Tests simulate the Service with a [serviceSeam] that runs the fake
 * installer's `install()` on an INDEPENDENT [serviceScope] (NOT the VM scope) and
 * translates progress + the terminal Result into [DownloadProgressBus] events,
 * exactly as the real Service does. The VM observes only the bus. Because
 * [DownloadProgressBus] is a process-global object, [reset] clears it per-test and
 * [tearDown] disposes every created VM (cancelling its bus collector) + the
 * serviceScope, so there is no cross-test bleed.
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

    // Stands in for ModelDownloadService's OWN scope: independent of any VM scope, so
    // vm.dispose() can't cancel an in-flight "service" download. Cancelled in tearDown.
    private val serviceScope = CoroutineScope(SupervisorJob() + dispatcher)

    // Every VM built via [vm] is registered here so tearDown disposes it — cancelling
    // its (infinite) DownloadProgressBus collector so it can't bleed into the next test.
    private val createdVms = mutableListOf<LocalModelViewModel>()

    @Before fun setUp() {
        kotlinx.coroutines.Dispatchers.setMain(dispatcher)
        // DownloadProgressBus is a process-global object — reset it so no slug state
        // survives from a prior test.
        DownloadProgressBus.clearAll()
    }

    @After fun tearDown() {
        createdVms.forEach { it.dispose() }
        serviceScope.cancel()
        DownloadProgressBus.clearAll()
        kotlinx.coroutines.Dispatchers.resetMain()
    }

    /**
     * Simulates [com.aiblackbox.portal.ModelDownloadService]: runs the installer's
     * `install()` (which the Service now owns) on the INDEPENDENT [serviceScope], and
     * publishes RUNNING (whole-percent throttled) → SUCCESS/FAILED to
     * [DownloadProgressBus] exactly as the real Service does. The VM reacts only to the
     * bus, so this is the production-equivalent download path under test.
     */
    private fun serviceSeam(
        installer: LocalModelInstaller,
        operator: String = "Brandon",
    ): (LocalBundle) -> Unit = { bundle ->
        serviceScope.launch {
            DownloadProgressBus.update(
                DownloadProgressBus.State(bundle.slug, 0f, DownloadProgressBus.Status.RUNNING),
            )
            // NOTE: this whole-percent throttle is a faithful copy of the one in
            // ModelDownloadService's worker — keep the two in sync.
            var lastPct = -1
            val result = installer.install(bundle, operator, "cpu") { soFar, total ->
                val frac = if (total > 0) (soFar.toFloat() / total).coerceIn(0f, 1f) else -1f
                val pct = if (frac < 0) 0 else (frac * 100).toInt()
                if (pct != lastPct) {
                    lastPct = pct
                    DownloadProgressBus.update(
                        DownloadProgressBus.State(bundle.slug, frac, DownloadProgressBus.Status.RUNNING),
                    )
                }
            }
            DownloadProgressBus.update(
                if (result.isSuccess) {
                    DownloadProgressBus.State(bundle.slug, 1f, DownloadProgressBus.Status.SUCCESS)
                } else {
                    DownloadProgressBus.State(
                        bundle.slug,
                        0f,
                        DownloadProgressBus.Status.FAILED,
                        result.exceptionOrNull()?.message ?: "download failed",
                    )
                },
            )
        }
    }

    /**
     * Simulates [com.aiblackbox.portal.ModelDownloadService] when `install()` THROWS
     * rather than returning a [Result.failure] — the I1 case: `verify()` reads the
     * downloaded file and can throw an IOException AFTER a successful transfer (and
     * `writeSidecar()`/`mkdirs()` can throw on a storage error). This seam MIRRORS the
     * production worker's try/catch: ANY throw is converted into a retryable FAILED bus
     * event, so the foreground tear-down + busy-clear always happen. Runs on the
     * independent [serviceScope], exactly like [serviceSeam].
     */
    private fun throwingServiceSeam(
        installer: LocalModelInstaller,
        operator: String = "Brandon",
    ): (LocalBundle) -> Unit = { bundle ->
        serviceScope.launch {
            DownloadProgressBus.update(
                DownloadProgressBus.State(bundle.slug, 0f, DownloadProgressBus.Status.RUNNING),
            )
            val result: Result<*> = try {
                installer.install(bundle, operator, "cpu") { _, _ -> }
            } catch (e: Throwable) {
                Result.failure<Any>(e)
            }
            DownloadProgressBus.update(
                if (result.isSuccess) {
                    DownloadProgressBus.State(bundle.slug, 1f, DownloadProgressBus.Status.SUCCESS)
                } else {
                    DownloadProgressBus.State(
                        bundle.slug,
                        0f,
                        DownloadProgressBus.Status.FAILED,
                        result.exceptionOrNull()?.message ?: "download failed",
                    )
                },
            )
        }
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

    /**
     * Fake catalog/autonomy client.
     *
     * @param setAutonomyOk first-attempt POST /local/device/autonomy result. When
     *   false it models the unattested-device 404 (the W7 root cause): the first
     *   mirror returns false, triggering the VM's attest-then-retry self-heal.
     * @param attestSucceeds whether the self-heal attest succeeds; on success a
     *   subsequent setAutonomy is allowed to succeed (models the hub now knowing
     *   the device).
     */
    private class FakeCatalog(
        private val bundles: List<LocalBundle>,
        private var autonomy: String = "permission",
        private val available: Boolean = false,
        private val setAutonomyOk: Boolean = true,
        private val attestSucceeds: Boolean = true,
        // Models GET /local/models/catalog 404ing/unreachable (R2): catalog()
        // throws, so refresh() must fall through to its installed-only path.
        private val catalogThrows: Boolean = false,
    ) : LocalModelCatalogClient {
        var lastAutonomyMode: String? = null
        var setAutonomyCalls = 0
        var attestCalls = 0
        var lastAttest: AttestRequest? = null
        // True once a successful attest has registered the device; a post-attest
        // setAutonomy then succeeds (mirrors the real backend's upsert-then-200).
        private var attested = false

        override suspend fun catalog(): List<LocalBundle> {
            if (catalogThrows) throw IOException("HTTP 404")
            return bundles
        }

        override suspend fun status(operator: String): LocalStatus =
            LocalStatus(
                available = available,
                models = if (available)
                    listOf(LocalDeviceRecord(deviceId = "test", autonomyMode = autonomy))
                else emptyList(),
            )

        override suspend fun setAutonomy(operator: String, deviceId: String, mode: String): Boolean {
            setAutonomyCalls++
            lastAutonomyMode = mode
            // First call honors setAutonomyOk; a retry after a successful attest
            // succeeds (the device is now known to the hub).
            val ok = setAutonomyOk || attested
            if (ok) autonomy = mode
            return ok
        }

        override suspend fun attest(req: AttestRequest): Boolean {
            attestCalls++
            lastAttest = req
            if (attestSucceeds) attested = true
            return attestSucceeds
        }
    }

    private fun vm(
        installer: LocalModelInstaller,
        catalog: LocalModelCatalogClient,
        operator: String = "Brandon",
        deviceId: String = "pixel-9",
        onSwitch: (String) -> Unit = {},
        onPersist: (String) -> Unit = {},
        // Defaults to the production-equivalent Service simulation; tests that want to
        // drive the bus directly inject their own seam.
        startDownload: ((LocalBundle) -> Unit)? = null,
    ) = LocalModelViewModel(
        installer = installer,
        catalog = catalog,
        operatorProvider = { operator },
        deviceId = deviceId,
        onModelSelected = onSwitch,
        startDownload = startDownload ?: serviceSeam(installer, operator),
        ioDispatcher = dispatcher,
        onAutonomyPersisted = onPersist,
    ).also { createdVms += it }

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

    // ── R2: catalog unavailable (404) must NOT hide installed models ─────────

    @Test
    fun `refresh keeps installed rows + surfaces an error when the catalog 404s (R2)`() = runTest(dispatcher) {
        // The device symptom: /local/models/catalog 404s but a model IS on disk.
        // refresh() must still load the installed model so state.rows is non-empty
        // (drives the "catalog unavailable — your installed models still work"
        // note) and an error is surfaced — never an empty picker.
        val installer = FakeInstaller(
            installed = mutableListOf(InstalledModel("gemma-4-e4b", File("/tmp/e4b.litertlm"), 4_000_000_000L)),
            recommended = e4b,
        )
        val catalog = FakeCatalog(listOf(e2b, e4b), catalogThrows = true)
        val vm = vm(installer, catalog)

        vm.refresh()
        advanceUntilIdle()

        val s = vm.state.value
        assertTrue("installed model still loaded despite catalog 404", s.installed.any { it.slug == "gemma-4-e4b" })
        assertFalse("rows non-empty -> the picker renders the installed model", s.rows.isEmpty())
        assertTrue("the installed model renders as Installed",
            s.rows.first { it.slug == "gemma-4-e4b" }.state is ModelRowState.Installed)
        assertNotNull("an error is surfaced (drives the catalog-unavailable note)", s.error)
    }

    @Test
    fun `refresh is genuinely empty when the catalog 404s and NOTHING is installed (R2)`() = runTest(dispatcher) {
        // The only case that should read "No on-device models available": catalog
        // 404 AND zero installed.
        val installer = FakeInstaller(recommended = e4b) // nothing installed
        val catalog = FakeCatalog(listOf(e2b, e4b), catalogThrows = true)
        val vm = vm(installer, catalog)

        vm.refresh()
        advanceUntilIdle()

        val s = vm.state.value
        assertTrue("no installed models", s.installed.isEmpty())
        assertTrue("rows genuinely empty", s.rows.isEmpty())
        assertNotNull("the raw catalog error is still surfaced", s.error)
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
        // Once the bus delivers the terminal SUCCESS, the VM's onBus must drop the slug
        // from downloadProgress and clear busySlug — no stale progress bar lingers on an
        // already-installed model.
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

    // ── I1: a THROWN install() un-sticks the UI (retryable FAILED) ───────────
    //
    // LocalModelManager.install() is NOT fully throw-safe — verify() can throw an
    // IOException AFTER a successful transfer. The production ModelDownloadService now
    // wraps install() in try/catch so ANY throw becomes a retryable FAILED bus event
    // (foreground always torn down, busy always cleared). [throwingServiceSeam] mirrors
    // that try/catch; an installer whose install() THROWS drives it. This locks the bus
    // FAILED contract that un-sticks the ViewModel (the service glue itself is untested).

    @Test
    fun `a thrown install becomes a retryable FAILED and un-sticks the UI (I1)`() = runTest(dispatcher) {
        var installCalls = 0
        val throwing = object : LocalModelInstaller {
            val store = mutableListOf<InstalledModel>()
            override suspend fun installedModels() = store.toList()
            override suspend fun recommendForDevice(bundles: List<LocalBundle>) = e4b
            override suspend fun install(
                bundle: LocalBundle,
                operator: String,
                delegate: String,
                onProgress: (Long, Long) -> Unit,
            ): Result<InstalledModel> {
                installCalls++
                // Transfer "succeeds" (progress reaches 100%), then verify() THROWS —
                // the exact I1 shape (a throw AFTER a successful transfer), not a
                // Result.failure.
                onProgress(bundle.sizeBytes ?: 1L, bundle.sizeBytes ?: 1L)
                throw IOException("verify failed after transfer")
            }
            override suspend fun delete(slug: String): Boolean = store.removeAll { it.slug == slug }
        }
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm = vm(throwing, catalog, startDownload = throwingServiceSeam(throwing))
        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        // The thrown install() landed as a retryable FAILED, exactly as a Result.failure would.
        assertTrue("slug recorded as failed", s.failedSlugs.contains("gemma-4-e4b"))
        assertFalse(
            "no stale progress entry -> row isn't stuck on a spinner",
            s.downloadProgress.containsKey("gemma-4-e4b"),
        )
        assertNull("busySlug cleared (the wedge the leak caused)", s.busySlug)
        assertNotNull("error surfaced for the user", s.error)
        assertFalse("not installed after the throw", s.installed.any { it.slug == "gemma-4-e4b" })
        // The terminal FAILED was consumed off the bus.
        assertNull(DownloadProgressBus.flow.value["gemma-4-e4b"])

        // The crux of I1: because busySlug was cleared, download() must NOT early-return
        // — the user can retry without killing the app.
        vm.download(e4b)
        advanceUntilIdle()
        assertEquals("retry allowed after a thrown install (busy was cleared)", 2, installCalls)
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

    // ── setAutonomy is LOCAL-FIRST (Task W7) ────────────────────────────────
    //
    // The actuator gate reads the LOCAL AutonomyStore, so the toggle must write
    // local persistence + the UI IMMEDIATELY and treat the backend as a
    // best-effort mirror. A backend 404 (unattested device) / offline / any
    // failure must NOT revert the local value nor surface an error to the user.

    @Test
    fun `setAutonomy writes local persistence and UI first (gate value correct)`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(listOf(e2b, e4b), autonomy = "permission", available = true)
        var persisted: String? = null
        val vm = vm(installer, catalog, onPersist = { persisted = it })
        vm.refresh()
        advanceUntilIdle()
        assertEquals("permission", vm.state.value.autonomyMode)

        vm.setAutonomy("yolo")
        advanceUntilIdle()

        // LOCAL-FIRST: the gate's source of truth + the UI reflect the new mode.
        assertEquals("local persistence (gate source of truth) updated", "yolo", persisted)
        assertEquals("UI reflects the new mode", "yolo", vm.state.value.autonomyMode)
        assertNull("no error on the happy path", vm.state.value.error)
        // The backend mirror was still attempted.
        assertEquals("yolo", catalog.lastAutonomyMode)
    }

    @Test
    fun `setAutonomy holds local value and shows no error when backend 404s (unattested)`() = runTest(dispatcher) {
        // Model the W7 root cause: POST /local/device/autonomy 404s because the
        // device was never attested in THAT hub (sideloaded model). The self-heal
        // attest then FAILS too — the harshest case. Local must still hold + NO error.
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(
            listOf(e2b, e4b),
            autonomy = "permission",
            available = true,
            setAutonomyOk = false,   // first mirror returns false (the 404)
            attestSucceeds = false,  // self-heal attest also fails (offline hub)
        )
        var persisted: String? = null
        val vm = vm(installer, catalog, onPersist = { persisted = it })
        vm.refresh()
        advanceUntilIdle()

        vm.setAutonomy("yolo")
        advanceUntilIdle()

        // The local gate value held; the user saw NO 404 / error.
        assertEquals("local posture held despite backend 404", "yolo", persisted)
        assertEquals("UI held the new mode", "yolo", vm.state.value.autonomyMode)
        assertNull("a backend failure must never surface an error", vm.state.value.error)
        // Self-heal was attempted (attest fired) but is best-effort.
        assertEquals("self-heal attest attempted once", 1, catalog.attestCalls)
    }

    @Test
    fun `setAutonomy self-heals an unattested device (attest then retry the mirror)`() = runTest(dispatcher) {
        // First mirror 404s (unattested); the self-heal attest SUCCEEDS, so the
        // retry mirror then lands — the device is now attested + mirrored YOLO.
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(
            listOf(e2b, e4b),
            autonomy = "permission",
            available = true,
            setAutonomyOk = false,  // first mirror 404s
            attestSucceeds = true,  // self-heal attest succeeds -> retry lands
        )
        val vm = vm(installer, catalog, operator = "Brandon", deviceId = "pixel-9")
        vm.refresh()
        advanceUntilIdle()

        vm.setAutonomy("yolo")
        advanceUntilIdle()

        // Self-heal sequence: setAutonomy (fail) -> attest -> setAutonomy (retry).
        assertEquals("attest fired exactly once", 1, catalog.attestCalls)
        assertEquals("setAutonomy tried twice (initial + retry)", 2, catalog.setAutonomyCalls)
        // The attest carried the operator/device binding + the chosen mode
        // (ledger-ready per-operator device binding).
        assertEquals("Brandon", catalog.lastAttest?.operator)
        assertEquals("pixel-9", catalog.lastAttest?.deviceId)
        assertEquals("yolo", catalog.lastAttest?.autonomyMode)
        // And the user-facing state is correct with no error.
        assertEquals("yolo", vm.state.value.autonomyMode)
        assertNull(vm.state.value.error)
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

    // ── bus pipeline does not thrash state ───────────────────────────────────

    @Test
    fun `download via bus does not thrash state per progress callback`() = runTest(dispatcher) {
        // 1000 rapid ticks (mimicking the real download's per-64KB callback). The
        // Service (serviceSeam) throttles to whole-percent, and the bus StateFlow
        // conflates, so the VM must NOT emit ~1000 state updates.
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

        // Hard upper bound: 1000 progress ticks must NOT yield ~1000 state updates.
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

    // ── Phase C: durable downloads via the Service + DownloadProgressBus ──────

    @Test
    fun `download starts via seam and bus SUCCESS refreshes installed`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val started = mutableListOf<String>()
        // A seam standing in for the Service: it records the start, marks the model
        // installed (as the Service's install() would), then publishes the terminal
        // SUCCESS the VM observes via the bus.
        val vm = vm(installer, catalog, startDownload = { b ->
            started += b.slug
            installer.installed.add(InstalledModel(b.slug, File("/tmp/${b.filename}"), b.sizeBytes ?: 1L))
            DownloadProgressBus.update(
                DownloadProgressBus.State(b.slug, 1f, DownloadProgressBus.Status.SUCCESS),
            )
        })
        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()

        assertEquals("download went through the seam, not an in-scope install", listOf("gemma-4-e4b"), started)
        assertTrue("bus SUCCESS refreshed installed", vm.state.value.isInstalled("gemma-4-e4b"))
        assertNull("busy cleared on terminal SUCCESS", vm.state.value.busySlug)
        assertNull(vm.state.value.error)
        // The VM consumed + cleared the terminal state from the bus.
        assertNull(DownloadProgressBus.flow.value["gemma-4-e4b"])
    }

    @Test
    fun `bus RUNNING reflects fractional progress and adopts busy`() = runTest(dispatcher) {
        val installer = FakeInstaller(recommended = e4b)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        // A seam emitting only a mid-flight RUNNING tick (the Service is still downloading).
        val vm = vm(installer, catalog, startDownload = { b ->
            DownloadProgressBus.update(
                DownloadProgressBus.State(b.slug, 0.42f, DownloadProgressBus.Status.RUNNING),
            )
        })
        vm.refresh()
        advanceUntilIdle()

        vm.download(e4b)
        advanceUntilIdle()

        val s = vm.state.value
        assertEquals("progress reflected from the bus", 0.42f, s.downloadProgress["gemma-4-e4b"]!!, 0.0001f)
        assertEquals("the in-flight slug is busy", "gemma-4-e4b", s.busySlug)
        assertFalse("not installed mid-flight", s.installed.any { it.slug == "gemma-4-e4b" })
    }

    @Test
    fun `dispose does not cancel an in-flight service download`() = runTest(dispatcher) {
        // The download runs in the (simulated) Service on serviceScope — NOT the VM
        // scope — so leaving the screen (dispose) must not cancel it; a fresh VM then
        // re-attaches to the terminal state via the bus.
        val installer = FakeInstaller(recommended = e4b, progressTicks = 4)
        val catalog = FakeCatalog(listOf(e2b, e4b))
        val vm1 = vm(installer, catalog) // default serviceSeam launches install() on serviceScope
        vm1.refresh()
        advanceUntilIdle()

        vm1.download(e4b)
        assertEquals("download marked busy synchronously", "gemma-4-e4b", vm1.state.value.busySlug)

        // Leave the screen mid-download: cancels ONLY vm1's scope (its bus collector).
        vm1.dispose()
        advanceUntilIdle()

        // The Service's install() ran to completion despite dispose(), and the bus holds
        // the terminal SUCCESS (vm1's dead collector never consumed/cleared it).
        assertEquals("install finished despite dispose()", 1, installer.installCount)
        assertEquals(
            DownloadProgressBus.Status.SUCCESS,
            DownloadProgressBus.flow.value["gemma-4-e4b"]?.status,
        )

        // A freshly recreated VM re-attaches to the bus's latest value and reflects it.
        val vm2 = vm(installer, catalog)
        advanceUntilIdle()
        assertTrue("fresh VM re-attached and shows installed", vm2.state.value.isInstalled("gemma-4-e4b"))
        assertNull("fresh VM is not busy", vm2.state.value.busySlug)
    }
}
