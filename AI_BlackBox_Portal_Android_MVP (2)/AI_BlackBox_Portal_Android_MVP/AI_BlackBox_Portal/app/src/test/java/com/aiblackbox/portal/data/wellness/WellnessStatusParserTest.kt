package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WellnessStatusParserTest {

    @Test
    fun `the uncredentialed status carries only state, label, last sync and enrolled`() {
        val s = WellnessStatusParser.parse(
            """{"state":"active","source_label":"Fixture phone (SM-TEST0)","last_sync":"2026-11-03T12:00:00Z","enrolled":true}""",
        )!!
        assertEquals(WellnessRules.EnrollmentState.ACTIVE, s.state)
        assertTrue(s.enrolled)
        assertFalse(s.credentialed)
        assertEquals("Fixture phone (SM-TEST0)", s.sourceLabel)
        assertNull(s.categories)
        assertTrue(s.troubles.isEmpty())
        // Settings fall back to the catalog defaults, never to "everything on".
        assertEquals(WellnessCatalog.DEFAULT_CONSENT, s.settings().categories)
    }

    @Test
    fun `the credentialed status fills consent, providers, wear, sync and the prompt`() {
        val s = WellnessStatusParser.parse("""
            {"state":"needs_attention","source_label":"x","last_sync":null,"enrolled":true,
             "troubles":["stale_sync","foreground_only"],
             "categories":{"sleep":"proactive","heart":"ask","activity":"off","blood_pressure":"ask"},
             "local_only":{"sleep":false,"heart":false,"activity":false,"blood_pressure":true},
             "providers_allowed":["anthropic","custom:lan-box"],
             "wear":{"hr_mode":"continuous","nights_worn_7":6},
             "sync":{"last_push_at":"2026-11-03T11:00:00Z","outbox_hint":3},
             "instinct_prompt":"Check my sleep first.","policy_version":"2026-10-29","disclosures_count":4,
             "unknown_future_field":{"a":1}}
        """.trimIndent())!!
        assertTrue(s.credentialed)
        assertEquals(WellnessRules.EnrollmentState.NEEDS_ATTENTION, s.state)
        assertEquals(listOf("stale_sync", "foreground_only"), s.troubles)
        assertEquals(WellnessConsent.PROACTIVE, s.categories!![WellnessCategory.SLEEP])
        assertEquals(WellnessConsent.OFF, s.categories!![WellnessCategory.ACTIVITY])
        assertTrue(s.localOnly!!.getValue(WellnessCategory.BLOOD_PRESSURE))
        assertEquals(setOf("anthropic", "custom:lan-box"), s.providersAllowed)
        assertEquals("continuous", s.hrMode)
        assertEquals(6, s.nightsWorn7)
        assertEquals("3", s.outboxHint)
        assertNull(s.lastSync)
        assertEquals("Check my sleep first.", s.instinctPrompt)
        assertEquals(4, s.disclosuresCount)
    }

    @Test
    fun `garbage is null and unknown states read as not set up`() {
        assertNull(WellnessStatusParser.parse("not json"))
        assertEquals(WellnessRules.EnrollmentState.NOT_SET_UP, WellnessStatusParser.parse("""{"state":"weird"}""")!!.state)
    }

    @Test
    fun `disclosures parse bare or wrapped, newest first as given, capped at 200`() {
        val one = """{"time":"2026-11-03T07:10:00Z","surface":"android","provider":"anthropic","model":"m","tool":"wellness_summary","categories":["sleep","heart"]}"""
        assertEquals(1, WellnessStatusParser.parseDisclosures("[$one]").size)
        val wrapped = WellnessStatusParser.parseDisclosures("""{"disclosures":[$one,{"ts":"t2","categories":"sleep, activity"}]}""")
        assertEquals(2, wrapped.size)
        assertEquals(listOf("sleep", "heart"), wrapped[0].categories)
        assertEquals("t2", wrapped[1].time)
        assertEquals(listOf("sleep", "activity"), wrapped[1].categories)
        val many = (1..250).joinToString(",", "[", "]") { one }
        assertEquals(200, WellnessStatusParser.parseDisclosures(many).size)
        assertTrue(WellnessStatusParser.parseDisclosures("{}").isEmpty())
    }

    @Test
    fun `the settings body carries the operator and only what changed`() {
        val old = WellnessRules.Settings()
        val narrowed = old.copy(categories = old.categories + (WellnessCategory.HEART to WellnessConsent.OFF))
        val body = Json.parseToJsonElement(WellnessStatusParser.settingsBody(WellnessFixtures.OPERATOR, old, narrowed)).jsonObject
        assertEquals(setOf("operator", "categories"), body.keys)
        assertEquals("off", body.getValue("categories").jsonObject.getValue("heart").jsonPrimitive.content)

        val prompt = Json.parseToJsonElement(WellnessStatusParser.settingsBody(WellnessFixtures.OPERATOR, old,
            old.copy(instinctPrompt = "Check\u0000 sleep"))).jsonObject
        assertEquals(setOf("operator", "instinct_prompt"), prompt.keys)
        assertEquals("Check sleep", prompt.getValue("instinct_prompt").jsonPrimitive.content)

        val providers = Json.parseToJsonElement(WellnessStatusParser.settingsBody(WellnessFixtures.OPERATOR, old,
            old.copy(providersAllowed = setOf("openai", "anthropic")))).jsonObject
        assertEquals(listOf("anthropic", "openai"), providers.getValue("providers_allowed").jsonArray.map { it.jsonPrimitive.content })

        // Setup states its provider and local-only choices even when they equal the defaults.
        val setup = Json.parseToJsonElement(WellnessStatusParser.settingsBody(WellnessFixtures.OPERATOR, old, old,
            WellnessStatusParser.SETUP_FIELDS)).jsonObject
        assertEquals(setOf("operator", "local_only", "providers_allowed", "google_paid_key_confirmed"), setup.keys)
        assertTrue(setup.getValue("providers_allowed").jsonArray.isEmpty())

        val reset = WellnessStatusParser.settingsBody(WellnessFixtures.OPERATOR, old.copy(instinctPrompt = "x"), old)
        assertTrue(reset.contains("\"instinct_prompt\":null"))
        assertEquals("""{"operator":"wellness-fixture"}""", WellnessStatusParser.settingsBody(WellnessFixtures.OPERATOR, old, old))
    }
}
