package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.local.InstalledModel
import com.aiblackbox.portal.data.local.LocalModelInstaller
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.data.model.LocalBundle
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * Task 1.6 — picker gating for the on-device `local` provider.
 *
 * Hermetic plain-JUnit (mirrors [com.aiblackbox.portal.ui.settings.LocalModelViewModelTest]):
 * fakes stand in for the manager ([LocalModelInstaller]) and the attester
 * ([LocalModelDownloader]) so there is NO Context / disk / network. Coroutines
 * run on a [StandardTestDispatcher] so [advanceUntilIdle] drains them.
 *
 * Locks the gating decision:
 *   - LOCAL is offered ONLY when [LocalModelInstaller.installedModels] is
 *     non-empty (disk-present + sha-verified). Empty → not selectable.
 *   - The on-open re-attest is best-effort/fire-and-forget: it MUST NOT gate
 *     availability — a model present on disk stays selectable even when attest
 *     fails (the offline-first rationale).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ProviderPickerViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before fun setUp() {
        kotlinx.coroutines.Dispatchers.setMain(dispatcher)
    }

    @After fun tearDown() {
        kotlinx.coroutines.Dispatchers.resetMain()
    }

    // ── Fakes ────────────────────────────────────────────────────────────────

    private class FakeInstaller(
        var installed: MutableList<InstalledModel> = mutableListOf(),
    ) : LocalModelInstaller {
        override suspend fun installedModels(): List<InstalledModel> = installed.toList()
        override suspend fun recommendForDevice(bundles: List<LocalBundle>): LocalBundle =
            bundles.first()
        override suspend fun install(
            bundle: LocalBundle,
            operator: String,
            delegate: String,
            onProgress: (Long, Long) -> Unit,
        ): Result<InstalledModel> = Result.failure(UnsupportedOperationException("not used"))
        override suspend fun delete(slug: String): Boolean = false
    }

    /** Records every attest call; configurable to succeed or fail. */
    private class FakeAttester(private val ok: Boolean = true) : LocalModelDownloader {
        val attested = mutableListOf<AttestRequest>()
        override suspend fun download(
            bundle: LocalBundle,
            destFile: File,
            onProgress: (Long, Long) -> Unit,
        ): Result<File> = Result.failure(UnsupportedOperationException("not used"))
        override suspend fun attest(req: AttestRequest): Boolean {
            attested.add(req)
            return ok
        }
    }

    private fun model(slug: String) = InstalledModel(slug, File("/tmp/$slug"), 1L)

    private fun vm(
        installer: LocalModelInstaller,
        attester: LocalModelDownloader = FakeAttester(),
        operator: String = "Brandon",
        deviceId: String = "pixel-9",
    ) = ProviderPickerViewModel(
        installer = installer,
        attester = attester,
        operatorProvider = { operator },
        deviceId = deviceId,
        ioDispatcher = dispatcher,
    )

    // ── gating ─────────────────────────────────────────────────────────────

    @Test fun `localAvailable defaults to false before refresh`() {
        val vm = vm(FakeInstaller(installed = mutableListOf(model("gemma-4-e2b"))))
        // No refresh yet → must default false (don't flash LOCAL before we know).
        assertFalse(vm.localAvailable.value)
    }

    @Test fun `empty installed models hides LOCAL from the picker`() = runTest(dispatcher) {
        val vm = vm(FakeInstaller(installed = mutableListOf()))
        vm.refresh()
        advanceUntilIdle()

        assertFalse(vm.localAvailable.value)
        val providers = ChatProvider.entries.filter { vm.isSelectable(it) }
        assertFalse("LOCAL must be absent with no installed model", providers.contains(ChatProvider.LOCAL))
        // Non-local providers are always selectable.
        assertTrue(providers.contains(ChatProvider.GEMINI))
    }

    @Test fun `installed model shows LOCAL in the picker`() = runTest(dispatcher) {
        val vm = vm(FakeInstaller(installed = mutableListOf(model("gemma-4-e2b"))))
        vm.refresh()
        advanceUntilIdle()

        assertTrue(vm.localAvailable.value)
        val providers = ChatProvider.entries.filter { vm.isSelectable(it) }
        assertTrue("LOCAL must appear with a verified model installed", providers.contains(ChatProvider.LOCAL))
    }

    // ── best-effort re-attest does NOT gate ───────────────────────────────────

    @Test fun `refresh fires best-effort re-attest for each installed model`() = runTest(dispatcher) {
        val attester = FakeAttester(ok = true)
        val vm = vm(
            FakeInstaller(installed = mutableListOf(model("gemma-4-e2b"), model("gemma-4-e4b"))),
            attester = attester,
        )
        vm.refresh()
        advanceUntilIdle()

        assertEquals("one re-attest per installed model", 2, attester.attested.size)
        assertTrue(attester.attested.any { it.modelSlug == "gemma-4-e2b" })
        assertTrue(attester.attested.any { it.modelSlug == "gemma-4-e4b" })
        // Re-attest uses the current operator + device id.
        assertTrue(attester.attested.all { it.operator == "Brandon" && it.deviceId == "pixel-9" })
    }

    @Test fun `attest failure does NOT hide an installed LOCAL model`() = runTest(dispatcher) {
        // Offline-first: a server-rejected attest still leaves a verified model
        // on disk → LOCAL stays selectable. Gating is disk-presence, NOT server.
        val vm = vm(
            FakeInstaller(installed = mutableListOf(model("gemma-4-e2b"))),
            attester = FakeAttester(ok = false),
        )
        vm.refresh()
        advanceUntilIdle()

        assertTrue("disk-present model stays available even when attest fails", vm.localAvailable.value)
    }
}
