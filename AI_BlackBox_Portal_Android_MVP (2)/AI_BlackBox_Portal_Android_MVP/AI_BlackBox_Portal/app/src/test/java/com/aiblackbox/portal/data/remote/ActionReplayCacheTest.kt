package com.aiblackbox.portal.data.remote

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (audit rank 64) POST /action runs each (task_id, action_id) ONCE.
 *
 * The box retries an /action whose response it never got — the device acted and the
 * reply was lost, or the user was still looking at the confirm card when the box's
 * per-action timeout fired. Before the idempotency key that retry dispatched the same
 * tap again (a second "Send"). A duplicate now waits for, or is answered from, the
 * first run; a frame without an action_id (an older box) still dispatches every time.
 */
class ActionReplayCacheTest {

    private class CountingDispatcher(
        private val gate: CompletableDeferred<Unit>? = null,
    ) : RemoteActionDispatcher {
        var calls = 0
        override suspend fun dispatch(body: String, taskId: String, operator: String): ActionResultEnvelope {
            calls++
            gate?.await()
            return ActionResultEnvelope(success = true, detail = "tapped node[send] #$calls")
        }
    }

    private fun frame(actionId: String?, taskId: String = "t1"): String {
        val id = if (actionId == null) "" else ""","action_id":"$actionId""""
        return """{"msg":"action","task_id":"$taskId","operator":"op"$id,"type":"element_click","resource_id":"send"}"""
    }

    @Test fun a_retried_action_is_answered_from_the_first_run() = runBlocking {
        val d = CountingDispatcher()
        val cache = ActionReplayCache()
        val first = handleActionRequest("POST", frame("a1"), d, cache)
        val retry = handleActionRequest("POST", frame("a1"), d, cache)
        assertEquals(1, d.calls)
        assertEquals(200, retry.status)
        assertEquals(first.json, retry.json)
        assertTrue(retry.json, retry.json.contains("#1"))
    }

    @Test fun a_retry_during_the_first_run_waits_for_it_instead_of_acting_again() = runBlocking {
        val gate = CompletableDeferred<Unit>()
        val d = CountingDispatcher(gate)
        val cache = ActionReplayCache()
        val first = async { handleActionRequest("POST", frame("a1"), d, cache) }
        yield()                                   // the first run is now waiting on the user
        val retry = async { handleActionRequest("POST", frame("a1"), d, cache) }
        yield()
        assertEquals("the retry must not dispatch while the first is in flight", 1, d.calls)
        gate.complete(Unit)
        assertEquals(first.await().json, retry.await().json)
        assertEquals(1, d.calls)
    }

    @Test fun distinct_actions_each_run() = runBlocking {
        val d = CountingDispatcher()
        val cache = ActionReplayCache()
        handleActionRequest("POST", frame("a1"), d, cache)
        handleActionRequest("POST", frame("a2"), d, cache)
        handleActionRequest("POST", frame("a1", taskId = "t2"), d, cache)   // same id, other task
        assertEquals(3, d.calls)
    }

    @Test fun a_frame_without_an_action_id_always_dispatches() = runBlocking {
        val d = CountingDispatcher()
        val cache = ActionReplayCache()
        handleActionRequest("POST", frame(null), d, cache)
        handleActionRequest("POST", frame(null), d, cache)
        handleActionRequest("POST", frame("  "), d, cache)
        assertEquals(3, d.calls)
    }

    @Test fun a_run_that_throws_is_not_remembered() = runBlocking {
        var calls = 0
        val flaky = object : RemoteActionDispatcher {
            override suspend fun dispatch(body: String, taskId: String, operator: String): ActionResultEnvelope {
                calls++
                if (calls == 1) throw IllegalStateException("actuator crashed")
                return ActionResultEnvelope(success = true, detail = "ok")
            }
        }
        val cache = ActionReplayCache()
        val failed = runCatching { handleActionRequest("POST", frame("a1"), flaky, cache) }
        assertTrue(failed.isFailure)
        val retry = handleActionRequest("POST", frame("a1"), flaky, cache)
        assertEquals(2, calls)
        assertTrue(retry.json, retry.json.contains("\"success\":true"))
    }

    @Test fun completed_runs_are_evicted_oldest_first_past_capacity() = runBlocking {
        val d = CountingDispatcher()
        val cache = ActionReplayCache(capacity = 2)
        handleActionRequest("POST", frame("a1"), d, cache)
        handleActionRequest("POST", frame("a2"), d, cache)
        handleActionRequest("POST", frame("a3"), d, cache)           // evicts a1
        handleActionRequest("POST", frame("a3"), d, cache)           // still remembered
        assertEquals(3, d.calls)
        handleActionRequest("POST", frame("a1"), d, cache)           // forgotten -> runs again
        assertEquals(4, d.calls)
    }

    @Test fun the_envelope_reads_the_action_id() {
        val env = kotlinx.serialization.json.Json { ignoreUnknownKeys = true }
            .decodeFromString(ActionEnvelope.serializer(), frame("abc123"))
        assertEquals("abc123", env.actionId)
        assertEquals("t1", env.taskId)
    }
}
