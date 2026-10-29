package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.repository.LOST_CONNECTION_MESSAGE
import com.aiblackbox.portal.data.repository.TaskPollOutcome
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Test

/** The overlay panel's text for a finished POST /chat task, read through the shared poller. */
class OverlayChatReplyTest {

    private fun done(resultData: String?, url: String? = null) = TaskPollOutcome.Completed(
        TaskStatus(
            taskId = "t",
            status = "completed",
            resultData = resultData?.let { Json.parseToJsonElement(it) },
            resultUrl = url,
        )
    )

    @Test fun `the reply is the first text key result_data carries`() {
        assertEquals("hi", OverlayChatReply.textFor(done("""{"ui_reply":"hi","reply":"x"}""")))
        assertEquals("from reply", OverlayChatReply.textFor(done("""{"ui_reply":null,"reply":"from reply"}""")))
        assertEquals("t", OverlayChatReply.textFor(done("""{"text":"t"}""")))
        assertEquals("r", OverlayChatReply.textFor(done("""{"response":"r"}""")))
        assertEquals("/ui/uploads/a.png", OverlayChatReply.textFor(done("""{"other":1}""", url = "/ui/uploads/a.png")))
        assertEquals("Task completed but no response", OverlayChatReply.textFor(done(null)))
    }

    @Test fun `a failure shows the box's reason, cancel and a lost connection say so`() {
        assertEquals("Error: quota exhausted", OverlayChatReply.textFor(TaskPollOutcome.Failed("quota exhausted")))
        assertEquals("Error: the task was cancelled", OverlayChatReply.textFor(TaskPollOutcome.Cancelled))
        assertEquals("Error: $LOST_CONNECTION_MESSAGE", OverlayChatReply.textFor(TaskPollOutcome.LostConnection))
    }
}
