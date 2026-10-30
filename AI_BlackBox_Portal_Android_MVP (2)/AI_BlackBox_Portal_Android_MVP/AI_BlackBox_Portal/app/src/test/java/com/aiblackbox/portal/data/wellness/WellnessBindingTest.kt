package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WellnessBindingTest {

    private var now = 5_000_000L
    private val files = WellnessFiles(WellnessFixtures.tempDir("binding"))
    private val binding = WellnessBinding(files, clock = { now })
    private val key = OriginSigner.encodeKey(ByteArray(32) { 1 })

    private fun bind(enr: String = "en_0badc0de", epoch: Int = 1): BindDecision {
        val intent = binding.beginIntent(WellnessFixtures.OPERATOR)
        return binding.acceptBind(WellnessBindRequest(WellnessFixtures.OPERATOR, enr, epoch, intent.nonce, key))
    }

    @Test
    fun `the intent nonce lives two minutes and is spent by a bind`() {
        val intent = binding.beginIntent(WellnessFixtures.OPERATOR)
        assertTrue(OriginSigner.isWellFormedNonce(intent.nonce))
        assertEquals(WellnessFixtures.OPERATOR, binding.bindOperator())
        now += WellnessBinding.INTENT_LIFE_MS - 1
        assertNotNull(binding.liveIntent())
        now += 1
        assertNull(binding.liveIntent())
        assertEquals("", binding.bindOperator())
        assertTrue(bind() is BindDecision.Accepted)
        assertNull(binding.liveIntent())
        assertEquals(WellnessFixtures.OPERATOR, binding.enrolledOperator())
    }

    @Test
    fun `the install id is stable and survives clearAll`() {
        val id = binding.installId()
        assertEquals(id, binding.installId())
        bind()
        binding.clearAll()
        assertNull(binding.current())
        assertEquals(id, WellnessBinding(files).installId())
    }

    @Test
    fun `a new epoch starts collection over but keeps the person's settings`() {
        assertTrue(bind(epoch = 1) is BindDecision.Accepted)
        ChangesTokenStore(files.tokens).seed(HcType.SLEEP, "old-token")
        DayIndex(files.dayIndex).apply { record("id-1", HcType.SLEEP, "2026-11-01"); save() }
        WellnessOutbox(files.outbox).enqueue("{}")
        val store = WellnessSyncStateStore(files.syncState)
        store.update { it.copy(intervalMin = 30, hrOriginChoice = "com.example.ring", needsReconnect = true,
            backfill = BackfillCursor(cursorMs = 1, floorMs = 0, pagesDone = 3, pagesTotal = 3, done = true)) }

        assertTrue(bind(epoch = 2) is BindDecision.Accepted)
        assertNull(ChangesTokenStore(files.tokens).get(HcType.SLEEP))
        assertEquals(0, DayIndex(files.dayIndex).size)
        assertEquals(0, WellnessOutbox(files.outbox).size())
        val st = store.load()
        assertFalse(st.backfill.done)
        assertFalse(st.needsReconnect)
        assertEquals(30, st.intervalMin)
        assertEquals("com.example.ring", st.hrOriginChoice)
        assertEquals(2, binding.current()!!.epoch)
    }

    @Test
    fun `a corrupt binding file reads as not bound and the signer needs a binding`() {
        assertNull(binding.signer())
        files.root.mkdirs()
        files.binding.writeText("{broken")
        assertNull(binding.current())
        bind()
        assertNotNull(binding.signer())
    }
}
