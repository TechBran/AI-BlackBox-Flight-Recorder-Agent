package com.aiblackbox.portal.data.remote

import android.content.Context
import android.os.Build
import android.util.Log
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.util.DeviceId
import com.aiblackbox.portal.util.normalizeApiOrigin
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.SecureRandom

/**
 * The pairing secret the control listener requires (audit X02/X04).
 *
 * The phone mints a random 256-bit secret the first time its listener starts, keeps it in
 * the app's NO-BACKUP files dir (so it is never copied into a cloud backup or a device
 * transfer), and registers it with its box via `POST /devices/pairing` whenever the listener
 * starts — app launch or boot. The box sends it back as `Authorization: Bearer <secret>` on
 * every call to :8765 ([gateBeforeBody] enforces it). Another app on the phone can reach the
 * port but cannot read this app's private storage, so it cannot present the secret.
 *
 * One secret per install: every box this phone registers with can drive it. Reinstalling the
 * app (or clearing its data) mints a new secret; the next launch re-registers it, and until
 * then the box gets a 401 it reports as "open the app once to re-pair".
 */
object RemoteControlPairing {

    private const val TAG = "RemoteControlPairing"
    const val SECRET_FILE_NAME = "remote_control_pairing_secret"

    private val random = SecureRandom()
    private val lock = Any()

    @Volatile private var cached: String? = null

    private val SECRET_RE = Regex("[0-9a-f]{64}")

    /** 32 random bytes as 64 lowercase hex chars. */
    fun newSecret(rng: SecureRandom = random): String {
        val bytes = ByteArray(32)
        rng.nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }

    fun isWellFormed(value: String): Boolean = SECRET_RE.matches(value)

    /**
     * Load the secret from [file], minting (and atomically persisting) a new one when the file
     * is missing or does not hold a well-formed secret. Throws on an I/O failure.
     */
    internal fun loadOrCreate(file: File, rng: SecureRandom = random): String {
        if (file.exists()) {
            val stored = file.readText(Charsets.UTF_8).trim()
            if (isWellFormed(stored)) return stored
            Log.w(TAG, "stored pairing secret is malformed; minting a new one")
        }
        val fresh = newSecret(rng)
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, "${file.name}.tmp")
        tmp.writeText(fresh, Charsets.UTF_8)
        Files.move(tmp.toPath(), file.toPath(),
            StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        return fresh
    }

    /** This install's secret, or null if private storage could not be read or written (the
     *  listener then fails closed). Cached after the first successful load. */
    fun secret(context: Context): String? {
        cached?.let { return it }
        return synchronized(lock) {
            cached ?: try {
                loadOrCreate(File(context.applicationContext.noBackupFilesDir, SECRET_FILE_NAME))
                    .also { cached = it }
            } catch (e: Exception) {
                Log.w(TAG, "could not load or mint the pairing secret (${e.javaClass.simpleName})")
                null
            }
        }
    }

    @Serializable
    private data class Registration(
        @SerialName("device_id") val deviceId: String,
        @SerialName("tailnet_ip") val tailnetIp: String,
        val secret: String,
        @SerialName("display_name") val displayName: String,
        @SerialName("device_kind") val deviceKind: String = "android",
    )

    private val JSON = Json { encodeDefaults = true }

    /** The `POST /devices/pairing` body (serializer-built: the model name is OEM data). */
    fun registrationBody(deviceId: String, tailnetIp: String, secret: String, displayName: String): String =
        JSON.encodeToString(Registration(deviceId, tailnetIp, secret, displayName))

    /**
     * Register this install's secret with the paired box. False when there is nothing to do
     * yet (no box origin, not on the tailnet, no secret) or the box could not be reached —
     * the caller retries ([pairingRetryDelayMs]).
     */
    suspend fun registerWithBox(context: Context): Boolean {
        val app = context.applicationContext
        val origin = app.getSharedPreferences(Constants.PREFS_NAME, Context.MODE_PRIVATE)
            .getString(Constants.KEY_ORIGIN, "").orEmpty()
        if (origin.isBlank()) return false
        val tailnetIp = TailnetAddress.localTailnetIpv4() ?: return false
        val secret = secret(app) ?: return false
        return try {
            BlackBoxApi(normalizeApiOrigin(origin)).post(
                "/devices/pairing",
                registrationBody(DeviceId.stable(app), tailnetIp, secret, Build.MODEL ?: "Android device"),
            )
            Log.d(TAG, "pairing secret registered with the box")
            true
        } catch (e: Exception) {
            Log.w(TAG, "pairing registration failed (${e.javaClass.simpleName}); will retry")
            false
        }
    }
}

/** Retry delay after the [attempt]-th failed registration: 30 s doubling, capped at 15 min. */
fun pairingRetryDelayMs(attempt: Int): Long {
    val base = 30_000L
    val cap = 15 * 60_000L
    if (attempt >= 5) return cap
    return minOf(cap, base shl attempt.coerceAtLeast(0))
}
