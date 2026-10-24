package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * Audit W13 — the phone's chat attachments.
 *
 * sendStreamMultimodal tagged every uploaded attachment `image_url`, so a .wav
 * recording or a PDF reached the OpenAI/Anthropic paths as a JPEG and the
 * backend's audio/video auto-route (which keys on `audio_url`/`video_url`)
 * never fired for a phone turn. Parts are now typed from the upload's
 * extension, which [uploadFileName] keeps in line with the attachment's MIME
 * type. And the recorder no longer uploads each take twice: /stt transcribes
 * the stored upload by URL, falling back to the file for older boxes.
 */
class ChatAttachmentPartsTest {

    // -------------------------------------------------------------------------
    // part typing
    // -------------------------------------------------------------------------

    @Test
    fun `audio video image and document uploads get their own part types`() {
        val cases = mapOf(
            "/ui/uploads/9f1c.wav" to "audio_url",
            "http://100.64.0.1:9091/ui/uploads/take.MP3" to "audio_url",
            "/ui/uploads/memo.m4a" to "audio_url",
            "/ui/uploads/clip.mp4" to "video_url",
            "/ui/uploads/clip.mov" to "video_url",
            "/ui/uploads/clip.webm" to "video_url",
            "/ui/uploads/cat.jpg" to "image_url",
            "/ui/uploads/cat.HEIC" to "image_url",
            "/ui/uploads/spec.pdf" to "document_url",
            "/ui/uploads/notes.txt" to "document_url",
            "/ui/uploads/report.docx" to "document_url",
            "/ui/uploads/archive.zip" to "document_url",
            "/ui/uploads/0b7e.tmp" to "document_url",
            "/ui/uploads/take.wav?v=2#t" to "audio_url",
        )
        for ((url, type) in cases) assertEquals(url, type, attachmentPartType(url))
    }

    @Test
    fun `a part carries its url under its own type key`() {
        val part = attachmentPart("/ui/uploads/take.wav")
        assertEquals("audio_url", part["type"]!!.jsonPrimitive.content)
        assertEquals("/ui/uploads/take.wav", part["audio_url"]!!.jsonObject["url"]!!.jsonPrimitive.content)
        assertFalse(part.containsKey("image_url"))
    }

    @Test
    fun `the user turn is the text then one typed part per attachment`() {
        val content = multimodalUserContent(
            "what is in these?",
            listOf("/ui/uploads/a.png", "/ui/uploads/b.wav", "/ui/uploads/c.pdf"),
        )
        val types = content.map { it.jsonObject["type"]!!.jsonPrimitive.content }
        assertEquals(listOf("text", "image_url", "audio_url", "document_url"), types)
        assertEquals("what is in these?", content[0].jsonObject["text"]!!.jsonPrimitive.content)
    }

    @Test
    fun `an upload name gains the extension its mime type implies`() {
        assertEquals("IMG_1234.jpg", uploadFileName("IMG_1234", "image/jpeg"))
        assertEquals("Recording.m4a", uploadFileName("Recording", "audio/mp4"))
        assertEquals("file.pdf", uploadFileName("", "application/pdf"))
        // A name that already has one keeps it; an unknown type changes nothing.
        assertEquals("song.flac", uploadFileName("song.flac", "audio/mpeg"))
        assertEquals("blob", uploadFileName("blob", "application/octet-stream"))
        assertEquals("take.wav", uploadFileName("take", "audio/x-wav; codecs=1"))
    }

    // -------------------------------------------------------------------------
    // one upload per recording
    // -------------------------------------------------------------------------

    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi
    private lateinit var take: File

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        api = BlackBoxApi(server.url("").toString().trimEnd('/'))
        take = File.createTempFile("take", ".wav").apply { writeBytes("RIFF-recording-bytes".toByteArray()) }
    }

    @After fun tearDown() {
        server.close()
        take.delete()
    }

    @Test
    fun `a stored upload is transcribed by url without sending the bytes again`() = runTest {
        server.enqueue(MockResponse.Builder().code(200).body("{\"text\":\"hello\"}").build())

        val body = transcribeUploadedRecording(api, "/ui/uploads/9f1c.wav", take)

        assertEquals("{\"text\":\"hello\"}", body)
        assertEquals(1, server.requestCount)
        val request = server.takeRequest()
        assertEquals("/stt", request.url.encodedPath)
        val sent = request.body!!.utf8()
        assertEquals("url=%2Fui%2Fuploads%2F9f1c.wav", sent)
        assertFalse(sent.contains("RIFF-recording-bytes"))
    }

    @Test
    fun `a box without the url form gets the file as before`() = runTest {
        server.enqueue(MockResponse.Builder().code(422)
            .body("{\"detail\":[{\"loc\":[\"body\",\"file\"],\"msg\":\"field required\"}]}").build())
        server.enqueue(MockResponse.Builder().code(200).body("{\"text\":\"hi\"}").build())

        val body = transcribeUploadedRecording(api, "/ui/uploads/9f1c.wav", take)

        assertEquals("{\"text\":\"hi\"}", body)
        assertEquals(2, server.requestCount)
        server.takeRequest()
        val retry = server.takeRequest().body!!.utf8()
        assertTrue(retry.contains("filename=\"${take.name}\""))
        assertTrue(retry.contains("RIFF-recording-bytes"))
    }

    @Test
    fun `a take whose upload failed is sent as a file`() = runTest {
        server.enqueue(MockResponse.Builder().code(200).body("{\"text\":\"x\"}").build())

        transcribeUploadedRecording(api, null, take)

        assertTrue(server.takeRequest().body!!.utf8().contains("RIFF-recording-bytes"))
    }

    @Test
    fun `other refusals surface instead of re-uploading`() = runTest {
        server.enqueue(MockResponse.Builder().code(404).body("{\"detail\":\"gone\"}").build())

        try {
            transcribeUploadedRecording(api, "/ui/uploads/9f1c.wav", take)
            fail("expected ApiHttpException")
        } catch (e: ApiHttpException) {
            assertEquals(404, e.code)
        }
        assertEquals(1, server.requestCount)
    }
}
