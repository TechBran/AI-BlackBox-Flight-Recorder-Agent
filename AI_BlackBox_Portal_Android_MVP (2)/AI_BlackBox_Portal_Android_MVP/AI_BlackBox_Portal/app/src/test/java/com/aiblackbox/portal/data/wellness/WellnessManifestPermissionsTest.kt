package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

/**
 * The manifest asks for EXACTLY the v1 health read permissions plus the two special grants:
 * no write permission, no exercise route, nothing a later edit could slip in. Also pins the
 * rationale entry points Health Connect needs (Android 16 revokes health permissions from an
 * app without them) and the package visibility the probe relies on.
 *
 * Parses the source manifest, and the merged one when the build has produced it (libraries
 * add normal permissions such as ACCESS_NETWORK_STATE, never a health one).
 */
class WellnessManifestPermissionsTest {

    private val androidNs = "http://schemas.android.com/apk/res/android"

    private fun manifest(): org.w3c.dom.Document {
        val f = listOf(File("src/main/AndroidManifest.xml"), File("app/src/main/AndroidManifest.xml")).first { it.exists() }
        val factory = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        return factory.newDocumentBuilder().parse(f)
    }

    private fun elements(doc: org.w3c.dom.Document, tag: String): List<Element> {
        val nodes = doc.getElementsByTagName(tag)
        return (0 until nodes.length).map { nodes.item(it) as Element }
    }

    private fun Element.androidAttr(name: String): String = getAttributeNS(androidNs, name)

    @Test
    fun `health permissions are exactly the read set plus history and background`() {
        val doc = manifest()
        val health = elements(doc, "uses-permission").map { it.androidAttr("name") }
            .filter { it.startsWith("android.permission.health.") }
            .toSortedSet()
        val expected = (WellnessCatalog.ALL_READ_PERMISSIONS +
            WellnessCatalog.PERMISSION_HISTORY + WellnessCatalog.PERMISSION_BACKGROUND).toSortedSet()
        assertEquals(expected, health)
        assertEquals(14, health.size)
        assertTrue(health.none { "WRITE" in it || "EXERCISE_ROUTE" in it || "ROUTE" in it })
    }

    /**
     * The MERGED manifest (what the APK carries) adds no health permission of its own: no
     * library may widen the set. Skipped only when the build has not merged one yet.
     */
    @Test
    fun `the merged manifest carries the same health permissions and the dataSync worker service`() {
        val merged = listOf(
            File("build/intermediates/merged_manifest/debug/processDebugMainManifest/AndroidManifest.xml"),
            File("app/build/intermediates/merged_manifest/debug/processDebugMainManifest/AndroidManifest.xml"),
        ).firstOrNull { it.exists() }
        org.junit.Assume.assumeTrue("no merged manifest yet", merged != null)
        val factory = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        val doc = factory.newDocumentBuilder().parse(merged)
        val health = elements(doc, "uses-permission").map { it.androidAttr("name") }
            .filter { it.startsWith("android.permission.health.") }.toSortedSet()
        assertEquals((WellnessCatalog.ALL_READ_PERMISSIONS + WellnessCatalog.PERMISSION_HISTORY +
            WellnessCatalog.PERMISSION_BACKGROUND).toSortedSet(), health)
        val fgs = elements(doc, "service").single {
            it.androidAttr("name") == "androidx.work.impl.foreground.SystemForegroundService"
        }
        assertEquals("dataSync", fgs.androidAttr("foregroundServiceType"))
    }

    @Test
    fun `health connect and samsung health are visible packages`() {
        val packages = elements(manifest(), "package").map { it.androidAttr("name") }
        assertTrue(WellnessCatalog.HEALTH_CONNECT_PACKAGE in packages)
        assertTrue(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE in packages)
    }

    @Test
    fun `the rationale screen is reachable on Android 13 and on 14+`() {
        val doc = manifest()
        val rationale = elements(doc, "activity").single { it.androidAttr("name") == ".WellnessRationaleActivity" }
        val actions13 = rationale.getElementsByTagName("action")
        assertTrue((0 until actions13.length).any {
            (actions13.item(it) as Element).androidAttr("name") == "androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE"
        })
        val alias = elements(doc, "activity-alias").single { it.androidAttr("targetActivity") == ".WellnessRationaleActivity" }
        assertEquals("android.permission.START_VIEW_PERMISSION_USAGE", alias.androidAttr("permission"))
        assertEquals("true", alias.androidAttr("exported"))
        val actions = alias.getElementsByTagName("action")
        val categories = alias.getElementsByTagName("category")
        assertEquals("android.intent.action.VIEW_PERMISSION_USAGE", (actions.item(0) as Element).androidAttr("name"))
        assertEquals("android.intent.category.HEALTH_PERMISSIONS", (categories.item(0) as Element).androidAttr("name"))
    }

    @Test
    fun `the backfill worker runs as a dataSync foreground service`() {
        val doc = manifest()
        val fgs = elements(doc, "service").single {
            it.androidAttr("name") == "androidx.work.impl.foreground.SystemForegroundService"
        }
        assertEquals("dataSync", fgs.androidAttr("foregroundServiceType"))
        val perms = elements(doc, "uses-permission").map { it.androidAttr("name") }
        assertTrue("android.permission.FOREGROUND_SERVICE_DATA_SYNC" in perms)
    }
}
