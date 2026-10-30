package com.aiblackbox.portal.data.store

import android.content.Context
import android.util.Log
import com.aiblackbox.portal.data.model.UiMessage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Persistent chat history storage — matches Portal's localStorage pattern.
 *
 * Stores per-operator chat history as JSON files in app internal storage.
 * Fast file I/O, no database overhead, survives app kills.
 *
 * Portal equivalent: localStorage key "bb_history_{operator}"
 * Max items: 100 (matches Portal MAX_HISTORY_ITEMS)
 *
 * Writes are atomic ([AtomicTextFile]: temp sibling, fsync, rename), so a kill
 * mid-save can no longer truncate the file, and a file that does not parse is
 * moved aside as `history_<op>.json.corrupt-<millis>` instead of loading as
 * empty and then being overwritten by the next save (audit
 * A09-data-local-model-11).
 *
 * (M7) A history that holds a turn which used the person's health data lives
 * under noBackupFilesDir ([privateDirProvider]), never under filesDir: the app
 * sets allowBackup="true", so Auto Backup and device-to-device transfer copy
 * filesDir, and a credentialed reply carries the numbers (spec §3.2: nothing
 * of Health sits where a backup copies). Once an operator's history is there
 * it stays there until it is cleared, so a reply never moves back into a
 * backed-up file; [load] reads it first.
 */
class ChatHistoryStore internal constructor(
    private val dirProvider: () -> File,
    private val privateDirProvider: () -> File,
) {

    constructor(context: Context) : this(
        { File(context.filesDir, DIR_NAME) },
        { File(context.noBackupFilesDir, DIR_NAME) },
    )

    /** Tests: [dirProvider] plus a private sibling directory. */
    internal constructor(dirProvider: () -> File) : this(
        dirProvider,
        { File(dirProvider().absoluteFile.parentFile, "$DIR_NAME-no-backup") },
    )

    companion object {
        private const val TAG = "ChatHistory"
        private const val DIR_NAME = "chat_history"
        const val MAX_HISTORY_ITEMS = 100

        /** Does [messages] hold a turn that used the person's health data (the no-backup rule)? */
        fun holdsHealth(messages: List<UiMessage>): Boolean =
            messages.any { it.wellnessUsed != null || it.wellnessRef != null }
    }

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
    }

    private val historyDir: File
        get() = dirProvider().also { it.mkdirs() }

    private val privateDir: File
        get() = privateDirProvider().also { it.mkdirs() }

    /** One file operation at a time: the temp sibling has a fixed name, and a
     *  quarantine or clear must not interleave with a save. Not reentrant. */
    private val fileLock = Mutex()

    private fun fileName(operator: String): String =
        "history_${operator.replace(Regex("[^a-zA-Z0-9_-]"), "_")}.json"

    private fun fileForOperator(operator: String): File = File(historyDir, fileName(operator))

    /** The no-backup copy (a history that used health data). */
    private fun privateFileForOperator(operator: String): File = File(privateDir, fileName(operator))

    /** The file [operator]'s history is in now: the no-backup one once it exists. */
    private fun currentFile(operator: String): File =
        privateFileForOperator(operator).takeIf { it.exists() } ?: fileForOperator(operator)

    /**
     * Load chat history for an operator. Returns empty list if none exists.
     */
    suspend fun load(operator: String): List<UiMessage> = withContext(Dispatchers.IO) {
        try {
            val file = currentFile(operator)
            if (!file.exists()) return@withContext emptyList()

            val text = file.readText()
            if (text.isBlank()) return@withContext emptyList()

            val messages = try {
                json.decodeFromString<List<UiMessage>>(text)
            } catch (e: Exception) {
                // Truncated, or an older schema (e.g. provenance once a String):
                // keep the bytes aside so the next save cannot destroy them --
                // unless a save replaced them since they were read.
                val moved = fileLock.withLock {
                    if (file.exists() && file.readText() == text) AtomicTextFile.quarantine(file) else null
                }
                Log.e(TAG, "History for $operator does not parse (${e.message}); moved aside to ${moved?.name}")
                return@withContext emptyList()
            }
            Log.d(TAG, "Loaded ${messages.size} messages for $operator")

            // Filter out any streaming/thinking artifacts from a crash
            messages.filter { !it.isStreaming && !it.isThinking }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load history for $operator: ${e.message}")
            emptyList()
        }
    }

    /**
     * Save chat history for an operator. Trims to MAX_HISTORY_ITEMS.
     */
    suspend fun save(operator: String, messages: List<UiMessage>) = withContext(Dispatchers.IO) {
        try {
            // Only persist completed messages (not streaming/thinking)
            val toSave = messages
                .filter { !it.isStreaming && !it.isThinking }
                .takeLast(MAX_HISTORY_ITEMS)

            val text = json.encodeToString(toSave)
            fileLock.withLock {
                val noBackup = privateFileForOperator(operator)
                if (holdsHealth(toSave) || noBackup.exists()) {
                    // Written first, then the backed-up copy goes: a kill in between leaves
                    // both, and load reads the no-backup one.
                    AtomicTextFile.write(noBackup, text)
                    fileForOperator(operator).delete()
                } else {
                    AtomicTextFile.write(fileForOperator(operator), text)
                }
            }
            Log.d(TAG, "Saved ${toSave.size} messages for $operator")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to save history for $operator: ${e.message}")
        }
    }

    /**
     * Clear history for an operator.
     */
    suspend fun clear(operator: String) = withContext(Dispatchers.IO) {
        try {
            fileLock.withLock {
                fileForOperator(operator).delete()
                privateFileForOperator(operator).delete()
            }
            Log.d(TAG, "Cleared history for $operator")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to clear history for $operator: ${e.message}")
        }
    }

    /**
     * Clear all history for all operators.
     */
    suspend fun clearAll() = withContext(Dispatchers.IO) {
        try {
            fileLock.withLock {
                historyDir.listFiles()?.forEach { it.delete() }
                privateDir.listFiles()?.forEach { it.delete() }
            }
            Log.d(TAG, "Cleared all history")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to clear all history: ${e.message}")
        }
    }
}
