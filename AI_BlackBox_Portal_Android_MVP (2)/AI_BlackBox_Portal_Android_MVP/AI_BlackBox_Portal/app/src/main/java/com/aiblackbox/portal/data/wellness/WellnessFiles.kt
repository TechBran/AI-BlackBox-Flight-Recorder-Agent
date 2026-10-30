package com.aiblackbox.portal.data.wellness

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Where the phone keeps its wellness state: ALWAYS under `noBackupFilesDir/wellness/`.
 *
 * The app sets `allowBackup="true"`, so anything under `filesDir` can be copied into a cloud
 * backup or a device transfer. The device key K, the Changes tokens, the backfill cursor, the
 * values-free id index and the outbox (which DOES hold health values until the box accepts
 * them) must never travel that way, the same rule as the pairing secret
 * (RemoteControlPairing). Every wellness path is built from [root];
 * WellnessNoBackupPathTest asserts each one resolves under the no-backup dir.
 */
class WellnessFiles(noBackupDir: File) {

    val root: File = File(noBackupDir, DIR)

    val binding: File get() = File(root, "binding.json")
    val intent: File get() = File(root, "intent.json")
    val installId: File get() = File(root, "install_id")
    val tokens: File get() = File(root, "tokens.json")
    val dayIndex: File get() = File(root, "day_index.tsv")
    val backfill: File get() = File(root, "backfill.json")
    val syncState: File get() = File(root, "sync_state.json")
    val statusCache: File get() = File(root, "status_cache.json")
    val probeLog: File get() = File(root, "probe_log.json")
    val outbox: File get() = File(root, "outbox")
    val quarantine: File get() = File(outbox, "quarantine")

    /** Every file and directory above (the no-backup test walks this list). */
    fun all(): List<File> = listOf(
        root, binding, intent, installId, tokens, dayIndex, backfill, syncState, statusCache,
        probeLog, outbox, quarantine,
    )

    /** Erase everything the phone holds for wellness (unbind, erase, a 410). */
    fun wipe() {
        root.deleteRecursively()
    }

    companion object {
        const val DIR = "wellness"

        fun of(context: Context): WellnessFiles = WellnessFiles(context.applicationContext.noBackupFilesDir)
    }
}

/** Crash-safe whole-file writes for wellness state (text and gzip bytes). */
internal object WellnessAtomic {

    fun writeText(file: File, text: String) = writeBytes(file, text.toByteArray(Charsets.UTF_8))

    fun writeBytes(file: File, bytes: ByteArray) {
        val dir = file.absoluteFile.parentFile ?: throw IllegalArgumentException("no parent for $file")
        dir.mkdirs()
        val tmp = File(dir, "${file.name}.tmp")
        try {
            FileOutputStream(tmp).use { out ->
                out.write(bytes)
                out.flush()
                out.fd.sync()
            }
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (e: Exception) {
            tmp.delete()
            throw e
        }
    }

    fun readTextOrNull(file: File): String? = try {
        if (file.exists()) file.readText(Charsets.UTF_8) else null
    } catch (_: Exception) {
        null
    }
}
