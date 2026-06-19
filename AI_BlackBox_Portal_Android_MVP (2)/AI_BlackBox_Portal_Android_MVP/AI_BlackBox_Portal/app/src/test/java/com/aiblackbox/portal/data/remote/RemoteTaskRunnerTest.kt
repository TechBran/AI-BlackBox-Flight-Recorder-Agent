package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.NativeTool
import com.aiblackbox.portal.data.local.NativeToolCallingLlm
import com.aiblackbox.portal.data.local.PhoneController
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

    // ── the allowlist filter (the security-critical seam) ──

    @Test fun allowlisted_tool_dispatches_through_controller() {
        val phone = FakePhone()
        val tap = RemoteTaskRunner.buildRemoteDeviceTools(phone).first { it.schema.name == "tap" }
        val out = tap.execute("""{"node_id":3}""")
        assertTrue(out, out.contains("did tap"))
        assertEquals(listOf("tap"), phone.dispatched)
    }

    @Test fun refused_tool_does_not_dispatch_and_returns_refusal() {
        val phone = FakePhone()
        val sms = RemoteTaskRunner.buildRemoteDeviceTools(phone).first { it.schema.name == "send_sms" }
        val out = sms.execute("""{"to":"x"}""")
        assertTrue(out, out.contains("refused"))
        assertTrue("controller must NOT run for a refused tool", phone.dispatched.isEmpty())
    }

    @Test fun exactly_the_allowlisted_tools_reach_the_controller() {
        val phone = FakePhone()
        RemoteTaskRunner.buildRemoteDeviceTools(phone).forEach { it.execute("{}") }
        assertEquals(RemoteAllowlist.SAFE_REMOTE, phone.dispatched.toSet())
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

    @Test fun submit_without_engine_sets_error_status() {
        val r = runner(engine = null)
        val id = r.submitTask("open maps", "Brandon")
        val st = r.taskStatus(id)
        assertEquals("error", st?.phase)
        assertTrue(st?.error.orEmpty().contains("not loaded"))
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
