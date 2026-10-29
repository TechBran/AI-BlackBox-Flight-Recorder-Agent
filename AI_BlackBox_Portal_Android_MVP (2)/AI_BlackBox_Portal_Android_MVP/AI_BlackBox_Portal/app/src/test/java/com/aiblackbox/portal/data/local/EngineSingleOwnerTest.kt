package com.aiblackbox.portal.data.local

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit rank 63 (A09-data-local-model-02, -03, A04-chat-viewmodel-08, -09): the
 * on-device engine has ONE owner.
 *
 *  - [LeaseGate]: the native engine is never closed under a running generation; the
 *    last lease closes a retired engine.
 *  - [pinWarm]: every load in the process (service warm, control_phone wake, the chat's
 *    own load) is serialized, publishes the warm marker and the crash-loop flag, reuses
 *    an engine already pinned for the same model instead of loading a second copy,
 *    retires a different model BEFORE loading (never two in memory) and closes the
 *    engine it was handed when the load fails.
 *
 * Fakes stand in for LiteRtEngine (litertlm is Java-21 bytecode and cannot be
 * constructed on the host JDK-17 test JVM); wiring tests pin that the real call sites
 * use these cores.
 */
class EngineSingleOwnerTest {

    private class Res(val name: String, val cfg: String = "") {
        var closed = 0
    }

    // ── LeaseGate ────────────────────────────────────────────────────────────

    @Test fun `close while a generation holds a lease is deferred to the last release`() {
        val gate = LeaseGate<Res> { it.closed++ }
        val engine = Res("e")
        gate.install(engine)
        val lease = gate.acquire()!!

        gate.retire()
        assertEquals("not closed under the running generation", 0, engine.closed)
        assertNull("a retired engine is not handed out again", gate.acquire())

        lease.release()
        assertEquals("the last release closes it", 1, engine.closed)
        lease.release()
        assertEquals("release is idempotent", 1, engine.closed)
    }

    @Test fun `an unleased engine closes at once`() {
        val gate = LeaseGate<Res> { it.closed++ }
        val engine = Res("e")
        gate.install(engine)
        gate.retire()
        assertEquals(1, engine.closed)
        assertNull(gate.peek())
    }

    @Test fun `installing a new engine retires the old one without mixing their leases`() {
        val gate = LeaseGate<Res> { it.closed++ }
        val old = Res("old")
        val new = Res("new")
        gate.install(old)
        val oldLease = gate.acquire()!!
        gate.install(new)
        val newLease = gate.acquire()!!
        assertSame(new, newLease.value)

        newLease.release()
        assertEquals("the new engine's lease cannot close the old one", 0, old.closed)
        assertEquals("nor itself while it is live", 0, new.closed)
        oldLease.release()
        assertEquals(1, old.closed)
        assertEquals(0, new.closed)
    }

    // ── pinWarm ──────────────────────────────────────────────────────────────

    private class FakeSlot : EngineSlot<Res> {
        var held: Res? = null
        var path: String? = null
        var warming: String? = null
        val markerDuringLoad = mutableListOf<String?>()
        val retired = mutableListOf<Res>()
        override fun held() = held
        override fun heldPath() = path
        override fun retire() { held?.let { retired += it; it.closed++ }; held = null; path = null }
        override fun beginWarming(path: String) { warming = path }
        override fun endWarming(path: String) { if (warming == path) warming = null }
        override fun pin(engine: Res, path: String) { held = engine; this.path = path; warming = null }
    }

    private class FakeInflight : WarmInflightStore {
        var flag = false
        val history = mutableListOf<Boolean>()
        override fun isInflight() = flag
        override fun setInflight(value: Boolean) { flag = value; history += value }
    }

    @Test fun `a load is bracketed by the warm marker and the crash-loop flag`() = runTest {
        val slot = FakeSlot()
        val inflight = FakeInflight()
        val engine = Res("a")
        val out = pinWarm(Mutex(), slot, inflight, engine, "/m/a", load = {
            slot.markerDuringLoad += slot.warming
            assertTrue("the crash-loop flag is on disk before the load", inflight.flag)
        }, close = { it.closed++ })

        assertSame(engine, out)
        assertEquals(listOf<String?>("/m/a"), slot.markerDuringLoad)
        assertSame("pinned in the holder", engine, slot.held)
        assertNull("marker cleared", slot.warming)
        assertEquals(listOf(true, false), inflight.history)
    }

    @Test fun `a second warmer for the same model reuses the pinned engine and loads nothing`() = runTest {
        val slot = FakeSlot()
        val mutex = Mutex()
        val first = Res("first")
        val second = Res("second")
        var loads = 0
        val gate = CompletableDeferred<Unit>()
        val a = async { pinWarm(mutex, slot, FakeInflight(), first, "/m/a", load = { loads++; gate.await() }, close = { it.closed++ }) }
        yield() // the first warm holds the process lock
        val b = async { pinWarm(mutex, slot, FakeInflight(), second, "/m/a", load = { loads++ }, close = { it.closed++ }) }
        yield()
        assertEquals("the second warmer waits instead of loading in parallel", 1, loads)
        gate.complete(Unit)

        assertSame(first, a.await())
        assertSame("the second warmer gets the pinned engine", first, b.await())
        assertEquals("exactly one load", 1, loads)
        assertEquals("the unused engine is released", 1, second.closed)
        assertEquals(0, first.closed)
    }

    @Test fun `a different model is retired before the new one loads`() = runTest {
        val slot = FakeSlot()
        val old = Res("old")
        slot.held = old
        slot.path = "/m/old"
        val new = Res("new")
        pinWarm(Mutex(), slot, FakeInflight(), new, "/m/new", load = {
            assertNull("never two models in memory at once", slot.held)
            assertEquals(1, old.closed)
        }, close = { it.closed++ })
        assertSame(new, slot.held)
    }

    @Test fun `a failed load closes the engine, clears the marker and the flag, and rethrows`() = runTest {
        val slot = FakeSlot()
        val inflight = FakeInflight()
        val engine = Res("a")
        try {
            pinWarm(Mutex(), slot, inflight, engine, "/m/a", load = { error("GPU init failed") }, close = { it.closed++ })
            fail("the failure must reach the caller")
        } catch (e: IllegalStateException) {
            assertEquals("GPU init failed", e.message)
        }
        assertEquals(1, engine.closed)
        assertNull(slot.held)
        assertNull(slot.warming)
        assertFalse(inflight.flag)
    }

    // ── a cancelled caller, and a settings change (review follow-up) ─────────

    @Test fun `a STOP during the cold load keeps the loaded engine for the next send`() = runTest {
        val slot = FakeSlot()
        val inflight = FakeInflight()
        val engine = Res("a")
        val nativeLoad = CompletableDeferred<Unit>()
        val send = launch {
            pinWarm(Mutex(), slot, inflight, engine, "/m/a", load = { nativeLoad.await() },
                close = { it.closed++ }, keepIfCancelled = { true })
        }
        runCurrent()
        send.cancel()             // STOP while the native load is still running
        nativeLoad.complete(Unit) // the load cannot be interrupted and finishes
        send.join()

        assertTrue("the send itself is still cancelled", send.isCancelled)
        assertEquals("the loaded engine is not thrown away", 0, engine.closed)
        assertSame("it is pinned, so the next send reuses it", engine, slot.held)
        assertNull(slot.warming)
        assertFalse(inflight.flag)
    }

    @Test fun `a caller that no longer wants the engine closes it when cancelled`() = runTest {
        val slot = FakeSlot()
        val inflight = FakeInflight()
        val engine = Res("a")
        val nativeLoad = CompletableDeferred<Unit>()
        val stopping = launch {
            pinWarm(Mutex(), slot, inflight, engine, "/m/a", load = { nativeLoad.await() }, close = { it.closed++ })
        }
        runCurrent()
        stopping.cancel()
        nativeLoad.complete(Unit)
        stopping.join()

        assertEquals("a stopping service releases what it loaded", 1, engine.closed)
        assertNull("nothing stays pinned without an owner", slot.held)
        assertNull(slot.warming)
        assertFalse(inflight.flag)
    }

    @Test fun `a failed load is closed even when a cancelled caller would keep it`() = runTest {
        val slot = FakeSlot()
        val engine = Res("a")
        try {
            pinWarm(Mutex(), slot, FakeInflight(), engine, "/m/a", load = { error("GPU init failed") },
                close = { it.closed++ }, keepIfCancelled = { true })
            fail("the failure must reach the caller")
        } catch (e: IllegalStateException) {
            assertEquals("GPU init failed", e.message)
        }
        assertEquals(1, engine.closed)
        assertNull(slot.held)
    }

    @Test fun `an engine pinned with older settings is retired, not reused`() = runTest {
        // The race: Local Model Settings applied while the service warm was loading the
        // old window. The re-warm's new engine must win over the one pinned meanwhile.
        val slot = FakeSlot()
        val stale = Res("stale", cfg = "window=4096")
        slot.held = stale
        slot.path = "/m/a"
        val fresh = Res("fresh", cfg = "window=8192")
        var loads = 0
        val out = pinWarm(Mutex(), slot, FakeInflight(), fresh, "/m/a", load = { loads++ },
            close = { it.closed++ }, sameConfig = { held, wanted -> held.cfg == wanted.cfg })

        assertSame(fresh, out)
        assertEquals("the new settings are loaded", 1, loads)
        assertEquals("the stale engine is released", 1, stale.closed)
        assertSame(fresh, slot.held)
        assertEquals(0, fresh.closed)
    }

    @Test fun `the engine config key changes with every setting an engine is built with`() {
        val base = engineConfigKey("/m/a", 4096, SamplerSettings(), supportImage = false)
        assertEquals(base, engineConfigKey("/m/a", 4096, SamplerSettings(), supportImage = false))
        for (other in listOf(
            engineConfigKey("/m/b", 4096, SamplerSettings(), false),
            engineConfigKey("/m/a", 8192, SamplerSettings(), false),
            engineConfigKey("/m/a", 4096, SamplerSettings(topK = 40), false),
            engineConfigKey("/m/a", 4096, SamplerSettings(topP = 0.9f), false),
            engineConfigKey("/m/a", 4096, SamplerSettings(temperature = 0.2f), false),
            engineConfigKey("/m/a", 4096, SamplerSettings(), true),
        )) assertFalse(other, other == base)
    }

    @Test fun `a held engine with stale settings is rebuilt, not borrowed`() {
        assertEquals(EngineSource.USE_HOLDER, engineSourceFor(true, "/m/a", "/m/a"))
        assertEquals(EngineSource.BUILD_OWN, engineSourceFor(true, "/m/a", "/m/a", holderConfigMatches = false))
    }

    // ── wiring: every load goes through the one warm ─────────────────────────

    @Test fun `the chat keeps a cancelled load, matches config and refuses stale borrows`() {
        // The chat's engine side is LocalEngineCoordinator (out of ChatViewModel, rank 76),
        // built on the view model's own scope.
        val vm = source("$root/ui/chat/ChatViewModel.kt")
        assertTrue("scope = viewModelScope," in vm.substringAfter("LocalEngineCoordinator(").substringBefore(")\n"))
        val chatEngine = source("$root/ui/chat/LocalEngineCoordinator.kt")
        val load = chatEngine.substring(chatEngine.indexOf("suspend fun loadWired("))
        val body = load.substring(0, load.indexOf("\n    }\n"))
        assertTrue("a STOP keeps the engine while the VM lives", "keepIfCancelled = { scope.isActive }" in body)
        assertTrue("and still ends the turn", "ensureActive()" in body)
        assertTrue("the borrow checks the held engine's settings", "engineSourceFor(held != null, LocalEngineHolder.modelPath, targetPath, heldConfigMatches)" in chatEngine)
        val warm = source("$root/data/local/EngineWarm.kt")
        assertTrue("the process warm reuses only a same-config engine", "sameConfig = { held, wanted -> held.configKey == wanted.configKey }" in warm)
    }


    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel")
        return ""
    }

    private val root = "src/main/java/com/aiblackbox/portal"

    @Test fun `the service, the control_phone wake and the chat all load through pinWarmEngine`() {
        val vm = source("$root/ui/chat/ChatViewModel.kt")
        // The chat's engine side moved out of the view model (rank 76).
        val chatEngine = source("$root/ui/chat/LocalEngineCoordinator.kt")
        val service = source("$root/LocalModelService.kt")
        val wake = source("$root/data/local/EngineWarm.kt")
        // Code only: KDoc and comments may still describe engine.load(...).
        fun code(src: String) = src.lines()
            .filterNot { it.trimStart().startsWith("*") || it.trimStart().startsWith("/*") }
            .joinToString("\n") { it.substringBefore("//") }
        for ((label, src) in listOf("ChatViewModel" to vm, "LocalEngineCoordinator" to chatEngine, "LocalModelService" to service)) {
            assertFalse("$label must not load an engine itself", Regex("\\b\\w*[eE]ngine\\w*\\.load\\(").containsMatchIn(code(src)))
        }
        for ((label, src) in listOf("LocalEngineCoordinator" to chatEngine, "LocalModelService" to service)) {
            assertTrue("$label loads through the one warm", "pinWarmEngine(" in src)
        }
        assertTrue("the chat loads through its coordinator", "engines.loadWired()" in vm)
        val ensure = wake.substring(wake.indexOf("suspend fun ensureWarmEngine("))
        assertTrue("the control_phone wake loads through the one warm", "pinWarmEngine(" in ensure.substring(0, ensure.indexOf("\n}\n")))
        assertFalse("the wake no longer builds and loads on its own", "engine.load(" in wake)
    }

    @Test fun `the chat's engine wiring is single-flight and a re-warm waits for the old warm`() {
        val engines = source("$root/ui/chat/LocalEngineCoordinator.kt")
        val wire = engines.substring(engines.indexOf("suspend fun providerOrWire("))
        assertTrue("wiring runs under one mutex", "engineWiringMutex.withLock" in wire.substring(0, 400))
        val rewarm = engines.substring(engines.indexOf("fun reWarm("))
        assertTrue("a re-warm joins the previous warm first", "cancelAndJoin()" in rewarm.substring(0, 1200))
        val apply = engines.substring(engines.indexOf("fun applySettings("))
        assertTrue("applying settings mid-turn defers the teardown", "turnInFlight()" in apply.substring(0, apply.indexOf("\n    }\n")))
        // ...and "mid-turn" is the chat's own STREAMING/THINKING state.
        val vm = source("$root/ui/chat/ChatViewModel.kt")
        assertTrue("turnInFlight = { isLocalTurnInFlight() }" in vm)
    }

    @Test fun `close takes the load lock and generations lease the native engine`() {
        val engine = source("$root/data/local/LiteRtEngine.kt")
        val close = engine.substring(engine.indexOf("override fun close()"))
        assertTrue("close() is serialized against load()", "loadLock.tryLock()" in close.substring(0, 800))
        for (fn in listOf("generate", "generateWithImage", "generateWithTools", "generateWithToolsNative")) {
            val body = engine.substring(engine.indexOf("override fun $fn("))
            assertTrue("$fn leases the engine", "withLeasedEngine(\"$fn\")" in body.substring(0, 300))
        }
        val lease = engine.substring(engine.indexOf("private inline fun <R> withLeasedEngine("))
        assertTrue("the lease comes from the gate", "native.acquire()" in lease.substring(0, 300))
        assertTrue("and is always released", "lease?.release()" in lease.substring(0, 700))
    }
}
