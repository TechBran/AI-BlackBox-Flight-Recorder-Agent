package com.aiblackbox.portal.ui.sms

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.intOrNull
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle
import java.util.Locale

/** Phone numbers as the box files SMS threads. */
object SmsPhone {
    /**
     * The key the box files a thread under: the number's last 10 digits (all of them when
     * it has fewer), the rule of Orchestrator/sms/message_store.py _normalize_phone. Two
     * spellings of one number are one thread.
     */
    fun threadKey(phone: String): String {
        val digits = phone.filter { it in '0'..'9' }
        return if (digits.length >= 10) digits.takeLast(10) else digits
    }
}

private val threadsJson = Json { ignoreUnknownKeys = true; isLenient = true }

/**
 * GET /sms/threads → one [SmsThread] per thread, in the box's order (newest first). A
 * phone listed twice (rows sharing its newest timestamp, from a box before the one-row
 * query) is shown once: the thread list is keyed by phone, and a duplicate key crashes
 * the LazyColumn on every poll (A14-ui-misc-10). Anything unreadable is skipped.
 */
fun parseSmsThreads(raw: String): List<SmsThread> {
    val root = try {
        threadsJson.parseToJsonElement(raw) as? JsonObject
    } catch (_: Exception) {
        null
    } ?: return emptyList()
    val rows = root["threads"] as? JsonArray ?: return emptyList()
    fun JsonObject.text(name: String) = (this[name] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: ""
    return rows.mapNotNull { it as? JsonObject }
        .map { obj ->
            SmsThread(
                phoneNumber = obj.text("phone_number"),
                contactName = obj.text("contact_name"),
                lastMessage = obj.text("last_message"),
                lastTimestamp = obj.text("last_timestamp"),
                unreadCount = (obj["unread_count"] as? JsonPrimitive)?.intOrNull ?: 0,
                direction = obj.text("direction"),
                lineNumber = obj.text("line_number"),
            )
        }
        .distinctBy { SmsPhone.threadKey(it.phoneNumber) }
}

/**
 * The compose box's text, per thread (A14-ui-misc-04). The Send button used to clear the
 * box before the request finished, so a failed SMS was silently lost. A draft is cleared
 * only once its send succeeded, and only if it was not edited meanwhile; it survives
 * leaving and reopening the thread.
 */
class SmsDrafts {
    private val _drafts = MutableStateFlow<Map<String, String>>(emptyMap())
    val drafts: StateFlow<Map<String, String>> = _drafts.asStateFlow()

    fun of(thread: String): String = _drafts.value[SmsPhone.threadKey(thread)].orEmpty()

    fun set(thread: String, text: String) {
        val key = SmsPhone.threadKey(thread)
        _drafts.update { if (text.isEmpty()) it - key else it + (key to text) }
    }

    /**
     * Send [thread]'s draft (trimmed) through [send]. The draft stays in the box until
     * [send] returns; a failure leaves it there to retry and propagates.
     * @return the text sent, or null when the draft was blank (nothing was sent)
     */
    suspend fun send(thread: String, send: suspend (String) -> Unit): String? {
        val text = of(thread).trim()
        if (text.isEmpty()) return null
        send(text)
        val key = SmsPhone.threadKey(thread)
        _drafts.update { if (it[key]?.trim() == text) it - key else it }
        return text
    }

    /** Every draft (an operator switch: they were the previous operator's). */
    fun clear() {
        _drafts.value = emptyMap()
    }
}

/**
 * When the open conversation scrolls to its newest message. It follows a new message
 * while the reader is at the newest one and leaves a reader who scrolled up to history
 * where they are. The view used to keep a flag from scroll-in-progress events, but its
 * own animateScrollToItem is a scroll in progress too and never reports its end state,
 * so after the first automatic scroll it stopped following a reader at the bottom. The
 * list as the reader last saw it is what decides now.
 */
object SmsFollow {
    /** A list item as last laid out: its key and its bottom edge (offset + size). */
    data class Seen(val key: Any, val bottom: Int)

    /**
     * @param previousNewestId the thread's newest message id before this change; null
     *   when the thread has just opened
     * @param messageIds the ids now shown, oldest first
     * @param justSent the reader just sent: their own message comes into view
     * @param seen the visible items of the last layout
     * @param viewportEnd that layout's viewport end offset
     */
    fun shouldScroll(
        previousNewestId: Int?,
        messageIds: List<Int>,
        justSent: Boolean,
        seen: List<Seen>,
        viewportEnd: Int,
    ): Boolean {
        if (messageIds.isEmpty()) return false
        if (previousNewestId == null || justSent) return true
        // A page with the previous newest gone replaced the list (another thread, a reload).
        if (previousNewestId !in messageIds) return true
        // At the bottom = the end of the previous newest message was on screen. The new
        // message is below it, laid out or not yet, so this holds either way.
        return seen.any { it.key == previousNewestId && it.bottom <= viewportEnd }
    }
}

/**
 * SMS times as the reader's wall clock (A14-ui-misc-11). The box stamps ISO-8601 with an
 * offset ("2026-10-08T14:03:22+00:00", with microseconds on some rows); the bubble used
 * to show the string's tail ("22+00") and the thread row raw UTC.
 */
object SmsTime {
    private fun parse(iso: String, zone: ZoneId): ZonedDateTime? = try {
        OffsetDateTime.parse(iso.trim()).atZoneSameInstant(zone)
    } catch (_: Exception) {
        null
    }

    private fun clockOf(t: ZonedDateTime, locale: Locale): String =
        t.format(DateTimeFormatter.ofLocalizedTime(FormatStyle.SHORT).withLocale(locale))

    /** The reader's short clock time ("10:03 AM" in en-US) in [zone]; "" for a missing or unreadable time. */
    fun clock(iso: String, zone: ZoneId = ZoneId.systemDefault(), locale: Locale = Locale.getDefault()): String =
        parse(iso, zone)?.let { clockOf(it, locale) } ?: ""

    /** A thread row's time: the clock for today, "Oct 7, 10:03 AM" before that. */
    fun threadStamp(
        iso: String,
        zone: ZoneId = ZoneId.systemDefault(),
        now: ZonedDateTime = ZonedDateTime.now(zone),
        locale: Locale = Locale.getDefault(),
    ): String {
        val t = parse(iso, zone) ?: return ""
        if (t.toLocalDate() == now.withZoneSameInstant(zone).toLocalDate()) return clockOf(t, locale)
        return t.format(DateTimeFormatter.ofPattern("MMM d", locale)) + ", " + clockOf(t, locale)
    }
}
