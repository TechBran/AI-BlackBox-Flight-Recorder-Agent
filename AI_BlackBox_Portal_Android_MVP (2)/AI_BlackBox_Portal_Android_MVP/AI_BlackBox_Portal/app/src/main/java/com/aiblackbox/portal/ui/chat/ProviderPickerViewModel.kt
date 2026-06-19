package com.aiblackbox.portal.ui.chat

import android.content.Context
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.local.LocalModelInstaller
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.ChatProvider
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Gating for the on-device `local` (Gemma) provider in the model/provider picker
 * (Task 1.6).
 *
 * **Plain holder, not AndroidViewModel** — same testable convention as
 * [com.aiblackbox.portal.ui.settings.LocalModelViewModel]: every framework fact
 * is a constructor seam ([installer], [attester], [operatorProvider], [deviceId],
 * [ioDispatcher]) so the gating decision unit-tests with plain JUnit + `runTest`
 * and in-memory fakes — no Context / disk / network. The production wiring is
 * built in [fromContext].
 *
 * ## Gating decision: disk-presence, NOT server availability
 *
 * The picker offers LOCAL iff **this device** has a **usable on-device
 * model** — i.e. [LocalModelInstaller.installedModels] is non-empty. Those are
 * the disk-present, sha-verified bundles (`LocalModelManager.install` DELETES a
 * file that fails verification, so anything `installedModels()` returns is
 * usable).
 *
 * Gating is DEVICE-scoped, not per-operator: [LocalModelInstaller.installedModels]
 * scans one device-global directory and sidecars don't record an operator, so a
 * model installed under any operator gates LOCAL for all. Per-operator isolation
 * is deferred to Task 5.4 (multi-tenant).
 *
 * This is deliberately **disk-presence gating, not a hard requirement on
 * `/local/device/status.available`**. The whole point of the on-device model is
 * to reason OFFLINE, without the mesh. An attest-failure (the server rejected or
 * never received the binding) still leaves a verified, runnable model on the
 * phone — gating on the server would hide a perfectly usable offline model.
 * **Do not "fix" this to require server availability.**
 *
 * On [refresh] we DO fire a best-effort re-attest for each installed model so the
 * BlackBox's binding record stays current ("the BlackBox verifies it's there"),
 * but that re-attest is **fire-and-forget**: its result is ignored and it never
 * gates [localAvailable].
 */
class ProviderPickerViewModel(
    private val installer: LocalModelInstaller,
    private val attester: LocalModelDownloader,
    private val operatorProvider: () -> String,
    private val deviceId: String,
    ioDispatcher: CoroutineDispatcher = Dispatchers.Main,
    private val delegate: String = "cpu",
) {
    private val scope = CoroutineScope(SupervisorJob() + ioDispatcher)

    /**
     * True when the current operator has at least one usable on-device model on
     * disk. Defaults to false until [refresh] has loaded — we never flash LOCAL
     * in the picker before we know it's installed.
     */
    private val _localAvailable = MutableStateFlow(false)
    val localAvailable: StateFlow<Boolean> = _localAvailable.asStateFlow()

    /**
     * Whether [provider] should appear in the picker. Every provider except
     * LOCAL is always selectable; LOCAL is gated on [localAvailable].
     */
    fun isSelectable(provider: ChatProvider): Boolean =
        if (provider.isLocal) _localAvailable.value else true

    /**
     * Refresh gating from disk and fire the best-effort re-attest. Call on init
     * and whenever the picker is opened. Best-effort: a disk read miss leaves
     * availability false without throwing.
     */
    fun refresh() {
        scope.launch {
            val installed = runCatching { installer.installedModels() }.getOrDefault(emptyList())
            _localAvailable.value = installed.isNotEmpty()

            // Fire-and-forget re-attest so the hub's binding record stays current.
            // MUST NOT gate availability: a rejected/failed attest still leaves a
            // verified model on disk (offline-first). We launch it as a detached
            // child and ignore the result.
            if (installed.isNotEmpty()) {
                val operator = operatorProvider()
                for (model in installed) {
                    scope.launch {
                        runCatching {
                            attester.attest(
                                AttestRequest(
                                    operator = operator,
                                    deviceId = deviceId,
                                    modelSlug = model.slug,
                                    version = LocalModelManager.BUNDLE_VERSION,
                                    // TODO(catalog-sha): re-attest sends sha256="" — once mirror catalog shas are populated (Task 1.2 real HF fetch), persist the verified sha in the sidecar/InstalledModel and forward it here, or the upsert will clobber the server's good checksum.
                                    sha256 = "",
                                    delegate = delegate,
                                    tailnetName = com.aiblackbox.portal.data.remote.TailnetAddress.localTailnetIpv4(),
                                    // autonomyMode left at its "permission" default.
                                )
                            )
                        }
                    }
                }
            }
        }
    }

    /** Tear down the holder's coroutine scope (call from the host's disposal). */
    fun dispose() {
        scope.cancel()
    }

    companion object {
        /**
         * Production wiring. Builds the real [LocalModelApi] + [LocalModelManager]
         * from a [BlackBoxApi] and a [Context]. All framework access lives here,
         * mirroring [LocalModelManager.fromContext] / [LocalModelViewModel.fromContext].
         */
        fun fromContext(
            context: Context,
            api: BlackBoxApi,
            operatorProvider: () -> String,
            ioDispatcher: CoroutineDispatcher = Dispatchers.Main,
        ): ProviderPickerViewModel {
            val deviceId = stableDeviceId(context)
            val localApi = LocalModelApi(api)
            val manager = LocalModelManager.fromContext(context, localApi, deviceId)
            return ProviderPickerViewModel(
                installer = manager,
                attester = localApi,
                operatorProvider = operatorProvider,
                deviceId = deviceId,
                ioDispatcher = ioDispatcher,
            )
        }

        /** Stable per-device id: ANDROID_ID, falling back to a constant. */
        @Suppress("HardwareIds")
        private fun stableDeviceId(context: Context): String {
            val androidId = runCatching {
                android.provider.Settings.Secure.getString(
                    context.contentResolver,
                    android.provider.Settings.Secure.ANDROID_ID,
                )
            }.getOrNull()
            return androidId?.takeIf { it.isNotBlank() } ?: "android-device"
        }
    }
}
