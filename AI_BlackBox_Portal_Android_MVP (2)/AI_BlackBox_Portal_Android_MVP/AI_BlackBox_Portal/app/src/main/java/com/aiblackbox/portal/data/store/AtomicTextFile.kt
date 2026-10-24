package com.aiblackbox.portal.data.store

import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Crash-safe whole-file writes for small app-private stores (audit
 * A09-data-local-model-11), the app-side twin of the backend's
 * Orchestrator/utils/atomic_json.py.
 *
 * [write] puts the new text in a temp sibling, fsyncs it, then renames it over
 * the target in one atomic step, so a process kill mid-write leaves the old
 * file or the new one, never a truncated one. [quarantine] moves a file that no
 * longer parses aside as `<name>.corrupt-<millis>` so the next save cannot
 * destroy what was in it. Callers serialize their own writers per file (the
 * temp name is fixed: `<name>.tmp`).
 */
internal object AtomicTextFile {

    fun write(file: File, text: String) {
        val dir = file.absoluteFile.parentFile
            ?: throw IllegalArgumentException("no parent directory for $file")
        dir.mkdirs()
        val tmp = File(dir, "${file.name}.tmp")
        try {
            FileOutputStream(tmp).use { out ->
                out.write(text.toByteArray(Charsets.UTF_8))
                out.flush()
                out.fd.sync()
            }
            Files.move(tmp.toPath(), file.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (e: Exception) {
            tmp.delete()
            throw e
        }
    }

    /** Rename [file] to `<name>.corrupt-<millis>[-n]`; null when it could not be moved. */
    fun quarantine(file: File, nowMillis: Long = System.currentTimeMillis()): File? {
        if (!file.exists()) return null
        var dest = File(file.parentFile, "${file.name}.corrupt-$nowMillis")
        var n = 1
        while (dest.exists()) {
            dest = File(file.parentFile, "${file.name}.corrupt-$nowMillis-$n")
            n++
        }
        return if (file.renameTo(dest)) dest else null
    }
}
