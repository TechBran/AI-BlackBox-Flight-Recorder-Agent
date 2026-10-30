package com.aiblackbox.portal.data.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M7) The mesh row's owner-only health fields: parsed additively (an older box omits them),
 * shown only on the viewer's own rows, and "Use for health" offered only on the viewer's own
 * Android rows that are not the source yet.
 */
class MeshDeviceHealthTest {

    private val me = "wellness-fixture"

    @Test
    fun `health fields are additive and default off`() {
        val rows = parseMeshDevices("""{"devices":[
            {"id":"a","name":"Phone","type":"android","owner":"$me","health_source":true,"health_state":"active"},
            {"id":"b","name":"Tablet","type":"android","owner":"$me"}]}""")
        assertTrue(rows[0].healthSource)
        assertEquals("active", rows[0].healthState)
        assertFalse(rows[1].healthSource)
        assertNull(rows[1].healthState)
    }

    @Test
    fun `merge copies health fields onto the viewer's rows only`() {
        val all = parseMeshDevices("""{"devices":[
            {"id":"a","type":"android","owner":"$me"},
            {"id":"b","type":"android","owner":"someone-else","health_source":true,"health_state":"active"},
            {"id":"c","type":"linux"}]}""")
        val scoped = parseMeshDevices("""{"devices":[
            {"id":"a","type":"android","owner":"$me","health_source":true,"health_state":"paused"},
            {"id":"c","type":"linux"}]}""")
        val merged = mergeHealthFields(all, scoped, me)
        assertTrue(merged[0].healthSource)
        assertEquals("paused", merged[0].healthState)
        // Another operator's row never shows a health badge, even if a box sent one (W17).
        assertFalse(merged[1].healthSource)
        assertNull(merged[1].healthState)
        assertFalse(merged[2].healthSource)
        assertEquals(all, mergeHealthFields(all, scoped, ""))
    }

    @Test
    fun `use for health only on my own android rows that are not the source yet`() {
        assertTrue(MeshDevice(id = "a", type = "android", owner = me).canUseForHealth(me))
        assertFalse(MeshDevice(id = "a", type = "android", owner = me, healthSource = true).canUseForHealth(me))
        assertFalse(MeshDevice(id = "a", type = "android", owner = "someone-else").canUseForHealth(me))
        assertFalse(MeshDevice(id = "a", type = "linux", owner = me).canUseForHealth(me))
        assertFalse(MeshDevice(id = "a", type = "android", owner = null).canUseForHealth(me))
        assertFalse(MeshDevice(id = "a", type = "android", owner = me).canUseForHealth(""))
    }
}
