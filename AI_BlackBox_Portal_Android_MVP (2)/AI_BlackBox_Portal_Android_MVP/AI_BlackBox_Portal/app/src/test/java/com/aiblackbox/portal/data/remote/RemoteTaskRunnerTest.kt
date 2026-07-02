package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.NativeTool
import com.aiblackbox.portal.data.local.NativeToolCallingLlm
import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.local.ResidentTools
import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

private class FakePhone : PhoneController {
    val dispatched = mutableListOf<String>()
    override suspend fun dispatch(name: String, args: JsonObject): ToolResult {
        dispatched += name
        return ToolResult(success = true, result = JsonPrimitive("did $name"))
    }
}

private class FakeEngine(private val flow: Flow<LlmEvent> = emptyFlow()) : NativeToolCallingLlm {
    override fun generateWithToolsNative(prompt: String, tools: List<NativeTool>): Flow<LlmEvent> = flow
}

class RemoteTaskRunnerTest {

    // ── M4: no allowlist — the confirm-gate (device-side) is the safety boundary ──

    @Test fun a_gesture_tool_dispatches_through_the_controller() {
        val phone = FakePhone()
        val tap = RemoteTaskRunner.buildRemoteDeviceTools(phone).first { it.schema.name == "tap" }
        val out = tap.execute("""{"node_id":3}""")
        assertTrue(out, out.contains("did tap"))
        assertEquals(listOf("tap"), phone.dispatched)
    }

    @Test fun formerly_allowlisted_intents_now_dispatch_without_refusal() {
        // (M4.1) The static allowlist is GONE. These intents are no longer blanket-"refused"
        // before dispatch — they reach the controller, where the safety decision is made.
        //   - send_sms / send_email are HIGH-CONSEQUENCE: the device-side OverlayConfirmUi gate
        //     (inside the controller) decides in PERMISSION mode.
        //   - dial is BENIGN pre-fill (NOT in HIGH_CONSEQUENCE_INTENTS): it only opens the
        //     dialer prefilled; the user still taps Call, so it dispatches without a gate.
        // Either way, no tool is pre-refused by an allowlist and all reach the controller.
        val highConsequence = listOf("send_sms", "send_email")
        val benignPrefill = listOf("dial")
        for (name in highConsequence + benignPrefill) {
            val phone = FakePhone()
            val tool = RemoteTaskRunner.buildRemoteDeviceTools(phone).firstOrNull { it.schema.name == name }
            assertNotNull("$name must be exposed as a remote tool", tool)
            val out = tool!!.execute("{}")
            assertFalse("$name must NOT be pre-refused by an allowlist: $out", out.contains("refused"))
            assertTrue("$name must reach the controller: $out", out.contains("did $name"))
            assertEquals(listOf(name), phone.dispatched)
        }
    }

    @Test fun every_phone_and_intent_tool_reaches_the_controller() {
        // (M4.1) With the allowlist gone, ALL actuators + intents dispatch — no tool is
        // refused before reaching the PhoneController. The full exposed set == every
        // phone-actuator + intent-action name.
        val phone = FakePhone()
        val tools = RemoteTaskRunner.buildRemoteDeviceTools(phone)
        tools.forEach { it.execute("{}") }
        val exposed = (ResidentTools.phoneActuators() + ResidentTools.intentActions())
            .map { it.name }.toSet()
        assertEquals(exposed, phone.dispatched.toSet())
        // The formerly-refused high-consequence names are now all present.
        assertTrue(phone.dispatched.containsAll(listOf("send_sms", "send_email", "dial", "type", "send_intent")))
    }

    @Test fun parseArgs_tolerates_blank_and_malformed() {
        assertEquals(JsonObject(emptyMap()), RemoteTaskRunner.parseArgs(""))
        assertEquals(JsonObject(emptyMap()), RemoteTaskRunner.parseArgs("not json"))
    }

    // ── handler basics ──

    @Test fun healthz_reflects_engine_presence() {
        assertFalse(runner(engine = null).healthz())
        assertTrue(runner(engine = FakeEngine()).healthz())
    }

    @Test fun unknown_task_id_is_null() {
        assertNull(runner(engine = null).taskStatus("nope"))
    }

    @Test fun submit_with_no_installed_model_sets_error_status() {
        val r = runner(engine = null)   // warmer defaults to engineProvider -> null
        val id = r.submitTask("open maps", "Brandon")
        val st = r.taskStatus(id)
        assertEquals("error", st?.phase)
        assertTrue(st?.error.orEmpty().contains("installed"))
    }

    @Test fun cold_holder_is_woken_on_demand() = runTest {
        // holder is cold (engineProvider -> null) but the warmer loads it on demand.
        val d = StandardTestDispatcher(testScheduler)
        val engine = FakeEngine(flowOf(LlmEvent.TextDelta("woke and did it")))
        val r = RemoteTaskRunner(
            scope = CoroutineScope(d),
            engineProvider = { null },
            phoneProvider = { FakePhone() },
            ioDispatcher = d,
            engineWarmer = { engine },
        )
        val id = r.submitTask("x", "Brandon")
        advanceUntilIdle()
        val st = r.taskStatus(id)
        assertEquals("done", st?.phase)
        assertEquals("woke and did it", st?.result)
    }

    private fun runner(engine: NativeToolCallingLlm?) = RemoteTaskRunner(
        scope = CoroutineScope(Dispatchers.Unconfined),
        engineProvider = { engine },
        phoneProvider = { FakePhone() },
        ioDispatcher = Dispatchers.Unconfined,
    )

    // ── Task 7: status state machine (waking -> working -> done|error) + step + bounding ──

    @Test fun transitions_waking_then_done_with_result() = runTest {
        val d = StandardTestDispatcher(testScheduler)
        val r = testRunner(d, FakeEngine(flowOf(LlmEvent.TextDelta("Opened "), LlmEvent.TextDelta("Maps."))))
        val id = r.submitTask("open maps", "Brandon")
        assertEquals("waking", r.taskStatus(id)?.phase)   // launched coroutine hasn't run yet
        advanceUntilIdle()
        val st = r.taskStatus(id)
        assertEquals("done", st?.phase)
        assertEquals("Opened Maps.", st?.result)
    }

    @Test fun engine_fault_sets_error_status() {
        // Synchronous (Unconfined) so the throwing engine flow runs inline through the
        // .catch operator -> error. (Avoids a StandardTestDispatcher/flowOn artifact.)
        val r = runner(engine = FakeEngine(flow { throw RuntimeException("boom") }))
        val id = r.submitTask("x", "Brandon")
        assertEquals("error", r.taskStatus(id)?.phase)
    }

    @Test fun working_step_increments_on_tool_calls() = runTest {
        val d = StandardTestDispatcher(testScheduler)
        val gate = CompletableDeferred<Unit>()
        val engine = FakeEngine(flow {
            emit(LlmEvent.ToolCall("open_app", JsonObject(emptyMap())))
            gate.await()                          // pause mid-turn so we can observe `working`
            emit(LlmEvent.TextDelta("done"))
        })
        val r = testRunner(d, engine)
        val id = r.submitTask("x", "Brandon")
        advanceUntilIdle()                        // runs up to gate.await()
        val mid = r.taskStatus(id)
        assertEquals("working", mid?.phase)
        assertEquals(1, mid?.step)
        gate.complete(Unit)
        advanceUntilIdle()
        assertEquals("done", r.taskStatus(id)?.phase)
    }

    @Test fun tasks_map_is_bounded_by_lru_eviction() {
        val r = runner(engine = null)             // Unconfined -> each submit errors synchronously
        val ids = (1..RemoteTaskRunner.MAX_TASKS + 10).map { r.submitTask("t$it", "op") }
        assertNull("earliest task should be evicted", r.taskStatus(ids.first()))
        assertEquals("error", r.taskStatus(ids.last())?.phase)
    }

    private fun testRunner(d: CoroutineDispatcher, engine: NativeToolCallingLlm) = RemoteTaskRunner(
        scope = CoroutineScope(d),
        engineProvider = { engine },
        phoneProvider = { FakePhone() },
        ioDispatcher = d,
    )
}
