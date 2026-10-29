package com.aiblackbox.portal.ui.generation

import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * A11-settings-generation-03 / -04: the box stores result_url as a box-relative path
 * ("/ui/uploads/<file>"). Coil and MediaPlayer read a scheme-less string as a FILE, so
 * the generated image, video, song and SSML audio never rendered, and only the first of
 * N images (result_data.all_urls) was ever shown. URLs are now made absolute against the
 * paired origin and every image is listed.
 */
class GenerationMediaUrlsTest {

    private val lan = "http://192.168.1.50:9091"
    private val tailnet = "https://box.example-tailnet.ts.net"

    @Test fun `a box-relative result url is made absolute against the origin`() {
        assertEquals("$lan/ui/uploads/a.png", absoluteMediaUrl(lan, "/ui/uploads/a.png"))
        assertEquals("$tailnet/ui/uploads/b.mp4", absoluteMediaUrl("$tailnet/", "/ui/uploads/b.mp4"))
    }

    @Test fun `an absolute url passes through untouched`() {
        assertEquals("https://cdn.example.com/x.wav", absoluteMediaUrl(lan, "https://cdn.example.com/x.wav"))
    }

    @Test fun `blank or unusable input gives null instead of a broken url`() {
        assertNull(absoluteMediaUrl(lan, null))
        assertNull(absoluteMediaUrl(lan, "  "))
        assertNull("no origin, no way to make it absolute", absoluteMediaUrl("", "/ui/uploads/a.png"))
    }

    private fun status(resultData: String?, url: String?) = TaskStatus(
        taskId = "t", status = "completed", resultUrl = url,
        resultData = resultData?.let { Json.parseToJsonElement(it) },
    )

    @Test fun `every image of a multi-image result is listed, absolute`() {
        val s = status("""{"all_urls":["/ui/uploads/1.png","/ui/uploads/2.png"]}""", "/ui/uploads/1.png")
        assertEquals(listOf("$lan/ui/uploads/1.png", "$lan/ui/uploads/2.png"), mediaUrlsFor(s, lan))
    }

    @Test fun `a single result without all_urls is still listed`() {
        assertEquals(listOf("$lan/ui/uploads/song.wav"), mediaUrlsFor(status(null, "/ui/uploads/song.wav"), lan))
        assertEquals(emptyList<String>(), mediaUrlsFor(null, lan))
    }

    @Test fun `the result screens render players for the absolute urls`() {
        val root = generationDir()
        val video = File(root, "VideoGenScreen.kt").readText()
        assertTrue("the video result plays", "InlineVideoPlayer(" in video)
        val music = File(root, "MusicGenScreen.kt").readText()
        assertTrue("the music result plays", "AudioPlayerBar(" in music.substring(music.indexOf("\"Music Ready\"")))
        val image = File(root, "ImageGenScreen.kt").readText()
        assertTrue("every generated image is shown", "resultUrls" in image)
        val ssml = File(root, "GoogleSsmlScreen.kt").readText()
        assertTrue("SSML voices come from the Google route", "/tts/google/voices" in ssml)
        assertTrue("SSML audio is absolute", "absoluteMediaUrl(" in ssml)
    }

    private fun generationDir(): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (rel in listOf("src/main/java/com/aiblackbox/portal/ui/generation", "app/src/main/java/com/aiblackbox/portal/ui/generation")) {
                val f = File(dir, rel)
                if (f.isDirectory) return f
            }
            dir = dir.parentFile
        }
        fail("generation sources not found")
        return File(".")
    }
}
