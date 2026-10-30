package com.aiblackbox.portal.data.wellness

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import android.util.Log
import com.aiblackbox.portal.BlackBoxNotificationManager
import com.aiblackbox.portal.BuildConfig
import com.aiblackbox.portal.data.remote.TailnetAddress
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.util.normalizeApiOrigin
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * Process-wide wiring for the Health feature: one [WellnessBinding], one sync engine, the
 * listener port. Everything it builds is a plain class tested on the JVM; this object only
 * supplies Android (Context, PackageManager, the box origin, WorkManager).
 */
object WellnessGraph {

    private const val TAG = "WellnessGraph"
    private val lock = Any()

    @Volatile private var binding: WellnessBinding? = null
    @Volatile private var state: WellnessSyncStateStore? = null
    @Volatile private var outbox: WellnessOutbox? = null
    @Volatile private var engine: WellnessSyncEngine? = null
    @Volatile private var engineOrigin: String? = null

    fun files(context: Context): WellnessFiles = WellnessFiles.of(context)

    fun binding(context: Context): WellnessBinding = binding ?: synchronized(lock) {
        binding ?: WellnessBinding(files(context), state = state(context)).also { binding = it }
    }

    /** The ONE state store of the process (the listener, the view model and the workers share it). */
    fun state(context: Context): WellnessSyncStateStore = state ?: synchronized(lock) {
        state ?: WellnessSyncStateStore(files(context).syncState).also { state = it }
    }

    /** The one outbox of the process (the engine writes it; the screen counts it). */
    fun outbox(context: Context): WellnessOutbox = outbox ?: synchronized(lock) {
        outbox ?: files(context).let { f -> WellnessOutbox(f.outbox, f.quarantine) }.also { outbox = it }
    }

    fun gateway(context: Context): HealthConnectGateway = AndroidHealthConnectGateway(context)

    /** The box origin the app already uses (Constants.KEY_ORIGIN), normalized; null when unpaired. */
    fun boxOrigin(context: Context): String? = context.applicationContext
        .getSharedPreferences(Constants.PREFS_NAME, Context.MODE_PRIVATE)
        .getString(Constants.KEY_ORIGIN, "").orEmpty()
        .takeIf { it.isNotBlank() }?.let { normalizeApiOrigin(it) }

    @Volatile private var client: Pair<String, IngestClient>? = null

    /** One client per box origin, so the clock-skew correction it learns is shared. */
    fun client(context: Context): IngestClient? {
        val origin = boxOrigin(context) ?: return null
        client?.takeIf { it.first == origin }?.let { return it.second }
        return synchronized(lock) {
            client?.takeIf { it.first == origin }?.second ?: IngestClient(origin).also { client = origin to it }
        }
    }

    fun engine(context: Context): WellnessSyncEngine? {
        val origin = boxOrigin(context) ?: return null
        engine?.takeIf { engineOrigin == origin }?.let { return it }
        return synchronized(lock) {
            engine?.takeIf { engineOrigin == origin } ?: run {
                val app = context.applicationContext
                val f = files(app)
                val client = client(app) ?: IngestClient(origin)
                WellnessSyncEngine(
                    gateway = gateway(app),
                    binding = binding(app),
                    tokens = ChangesTokenStore(f.tokens),
                    dayIndex = DayIndex(f.dayIndex),
                    outbox = outbox(app),
                    state = state(app),
                    send = { bytes, signer ->
                        val r = client.ingest(bytes, signer)
                        WellnessOutbox.SendResult(r.status, r.body, r.retryAfterS)
                    },
                    fetchStatus = { operator, signer ->
                        val r = client.status(operator, signer)
                        WellnessOutbox.SendResult(r.status, r.body, r.retryAfterS)
                    },
                    onNotices = { notices -> postNotices(app, notices) },
                    onStopped = { status, error -> onStopped(app, status, error) },
                    onReconnectNeeded = {
                        BlackBoxNotificationManager(app).showWellnessNotice("Reconnect Health",
                            "Open BlackBox, Settings, Health, and set this phone up again.", "wellness-reconnect")
                    },
                ).also {
                    engine = it
                    engineOrigin = origin
                }
            }
        }
    }

    fun probe(context: Context): CapabilityProbe {
        val app = context.applicationContext
        return CapabilityProbe(gateway(app), AndroidPackageInfoSource(app), { phoneInfo(app) })
    }

    fun phoneInfo(context: Context): PhoneInfo {
        val model = Build.MODEL ?: "Android"
        // T-8: whether One UI lets an app read the user's device name; the model stands in.
        val name = runCatching { Settings.Global.getString(context.contentResolver, "device_name") }
            .getOrNull()?.trim()?.takeIf { it.isNotEmpty() } ?: model
        return PhoneInfo(
            manufacturer = Build.MANUFACTURER ?: "",
            model = model,
            deviceName = name,
            sdkInt = Build.VERSION.SDK_INT,
            appVersion = BuildConfig.VERSION_NAME,
        )
    }

    fun tailnetIps(): List<String> = TailnetAddress.localTailnetIpv4Candidates()

    /**
     * Run the probe and send the capability report (counts only) when it is due: daily, after
     * each sync once the grants differ from the last report (a change made in Health Connect's
     * own settings), and at once with [force] (a grant made in the app).
     */
    suspend fun pushCapabilities(context: Context, force: Boolean = false): CapabilityProbe.Result? {
        val app = context.applicationContext
        val record = binding(app).current() ?: return null
        val client = client(app) ?: return null
        val st = state(app).load()
        val now = System.currentTimeMillis()
        // A grant added or withdrawn in Health Connect's own settings re-sends the report at once.
        val grants = runCatching { gateway(app).grantedPermissions() }.getOrDefault(emptySet()).sorted()
        if (!force && grants == st.lastReportedGrants && now - st.lastCapabilitiesMs < CAPABILITIES_EVERY_MS) return null
        val result = runCatching { probe(app).run() }.getOrNull() ?: return null
        applyProbe(app, result)
        val reply = client.capabilities(
            CapabilityProbe.toJson(result.report),
            OriginSigner(record.enrollmentId, record.k),
        )
        if (reply.ok) state(app).update { it.copy(lastCapabilitiesMs = now, lastReportedGrants = grants) }
        Log.i(TAG, "capability report sent (status=${reply.status}, origins=${result.report.origins.size})")
        return result
    }

    /** Remember what the probe concluded about the heart-rate origin. */
    fun applyProbe(context: Context, result: CapabilityProbe.Result) {
        state(context).update {
            it.copy(
                hrOriginAuto = result.autoHeartRateOrigin,
                hrOriginDeviceType = if (it.hrOriginChoice == "auto") result.autoHeartRateDevice?.deviceType else it.hrOriginDeviceType,
                hrOriginModel = if (it.hrOriginChoice == "auto") result.autoHeartRateDevice?.model else it.hrOriginModel,
            )
        }
    }

    /** The listener's view of the enrollment (`/wellness/bind`, `/sync`, `/unbind`). */
    fun port(context: Context): WellnessPort {
        val app = context.applicationContext
        val b = binding(app)
        return object : WellnessPort {
            override fun bindOperator(): String = b.bindOperator()
            override fun enrolledOperator(): String = b.enrolledOperator()
            override fun acceptBind(request: WellnessBindRequest): BindDecision = b.acceptBind(request)
            override fun current(): WellnessBindingRecord? = b.current()
            override fun requestSync() {
                state(app).update { it.copy(lastNudgeMs = System.currentTimeMillis()) }
                WellnessScheduler.syncNow(app)
            }
            override fun clearAll() {
                WellnessScheduler.cancelAll(app)
                b.clearAll()
            }
            override fun onBound(record: WellnessBindingRecord) {
                // Scheduling waits for the box's 200 to the enroll call (WellnessViewModel): a
                // run started now could reach the box before it has stored the key, take its
                // 401 for a refused key, and ask the person to reconnect a setup that worked.
                state(app).update { it.copy(state = WellnessRules.EnrollmentState.ACTIVE.wire, needsReconnect = false) }
            }
        }
    }

    /** Post the box's `pending_notices` once each on the private wellness channel. */
    private fun postNotices(context: Context, notices: List<JsonObject>) {
        val store = state(context)
        val posted = store.load().postedNoticeIds.toMutableList()
        val manager = BlackBoxNotificationManager(context)
        for (n in notices) {
            val id = n["id"]?.jsonPrimitive?.contentOrNull ?: continue
            if (id in posted) continue
            manager.showWellnessNotice(
                title = n["title"]?.jsonPrimitive?.contentOrNull ?: "New message from BlackBox",
                body = n["body"]?.jsonPrimitive?.contentOrNull.orEmpty(),
                key = "wellness-notice-$id",
            )
            posted += id
        }
        store.update { it.copy(postedNoticeIds = posted.takeLast(MAX_POSTED_NOTICES)) }
    }

    private fun onStopped(context: Context, status: Int, error: String?) {
        WellnessScheduler.cancelAll(context)
        val body = when (error) {
            "owner_changed" -> "This phone was given to another operator in Devices, so health syncing stopped."
            "source_moved" -> "Another phone is now your health source. This phone stopped syncing."
            else -> "Health syncing on this phone was turned off from your box."
        }
        BlackBoxNotificationManager(context).showWellnessNotice("Health setup changed", body, "wellness-stopped")
        Log.i(TAG, "enrollment ended by the box (status=$status)")
    }

    private const val CAPABILITIES_EVERY_MS = 24 * 60 * 60_000L
    private const val MAX_POSTED_NOTICES = 64
}

/** [PackageInfoSource] over PackageManager (visibility comes from the manifest's `<queries>`). */
class AndroidPackageInfoSource(context: Context) : PackageInfoSource {
    private val pm: PackageManager = context.applicationContext.packageManager

    override fun label(packageName: String): String? = runCatching {
        pm.getApplicationLabel(pm.getApplicationInfo(packageName, 0)).toString().trim().takeIf { it.isNotEmpty() }
    }.getOrNull()

    override fun versionName(packageName: String): String? = runCatching {
        pm.getPackageInfo(packageName, 0).versionName
    }.getOrNull()

    override fun isInstalled(packageName: String): Boolean = runCatching {
        pm.getPackageInfo(packageName, 0); true
    }.getOrDefault(false)
}
