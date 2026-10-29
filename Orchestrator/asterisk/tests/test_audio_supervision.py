"""The audio subprocess is supervised: a dead child is restarted with backoff,
the calls it carried end at once, and after N failures in a row the
supervisor gives up loudly (audit O11-telephony-asterisk-cellular-10).

Before, _ipc_listener set _running=False when the child died and nothing
respawned it; the singleton kept pointing at the dead object, and the call
handlers only tested `if not ipc_client`, so every later call answered, ran
the IVR and timed out at wait_for_channel until the service restarted.

The children here are real processes: the audio subprocess itself, or a
one-line stand-in that says READY over the IPC fd and exits.
"""
import asyncio
import os
import signal
import sys
import uuid

from Orchestrator.asterisk import audio_ipc

FAST = dict(backoff_initial_s=0.05, backoff_max_s=0.2)

# Says READY on the IPC fd it is handed, then exits 3: a child that dies young.
_DIES_YOUNG = (
    "import socket, struct, sys\n"
    "s = socket.socket(fileno=int(sys.argv[1]))\n"
    "p = b'127.0.0.1:1'\n"
    "s.sendall(struct.pack('<BI', 7, len(p)) + p)\n"
    "print('stand-in child exiting')\n"
    "sys.exit(3)\n"
)


def _frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def _until(predicate, timeout=15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def test_a_killed_child_is_restarted_and_its_calls_end():
    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0, **FAST)
        try:
            assert await client.start()
            first_pid = client.pid

            call = str(uuid.uuid4())
            client.expect_channel(call)
            _, writer = await asyncio.open_connection(*client.address)
            writer.write(_frame(0x01, uuid.UUID(call).bytes))
            await writer.drain()
            assert await client.wait_for_channel(call, timeout=10)
            ended = asyncio.get_running_loop().create_future()

            async def on_hangup(channel):
                if not ended.done():
                    ended.set_result(channel)

            client.register_channel(call, on_hangup=on_hangup)
            waiting = str(uuid.uuid4())
            client.expect_channel(waiting)
            pending = asyncio.ensure_future(client.wait_for_channel(waiting, timeout=30))

            os.kill(first_pid, signal.SIGKILL)

            assert await asyncio.wait_for(ended, 10) == call, "the call it carried ends"
            assert await asyncio.wait_for(pending, 10) is False, "a waiter fails at once"
            await _until(lambda: client.is_running and client.pid != first_pid)
            assert client.restarts == 1 and not client.gave_up
            assert client.status()["pid"] == client.pid
            writer.close()
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_the_supervisor_gives_up_loudly_after_n_failed_starts(capsys):
    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0, max_restarts=3, **FAST)
        attempts = []

        async def failing_spawn():
            attempts.append(1)
            client.last_error = "exited with code 1 before it listened"
            return False

        client._spawn = failing_spawn
        try:
            assert await client.start() is False
            await _until(lambda: client.gave_up)
            assert len(attempts) == 3
            assert not client.is_running
        finally:
            await client.shutdown()

    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "giving up" in out and "exited with code 1" in out


def test_a_child_that_keeps_dying_young_is_not_restarted_forever(tmp_path, capsys):
    stand_in = tmp_path / "dies_young.py"
    stand_in.write_text(_DIES_YOUNG)

    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0, max_restarts=3, **FAST)
        client._argv = lambda fd: [sys.executable, "-I", str(stand_in), str(fd), "h", "0"]
        try:
            assert await client.start() is True
            await _until(lambda: client.gave_up)
            assert client.restarts == 2, "the first start, two restarts, then it gives up"
            assert client.status()["gave_up"] is True
        finally:
            await client.shutdown()

    asyncio.run(scenario())
    assert "giving up" in capsys.readouterr().out


def test_shutdown_stops_the_supervisor_without_a_restart():
    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0, **FAST)
        assert await client.start()
        pid = client.pid
        await client.shutdown()
        await asyncio.sleep(0.3)
        assert not client.is_running and client.restarts == 0
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("the child outlived shutdown")

    asyncio.run(scenario())


# ── the call handlers refuse at once when the child is not running ─────────

class _ARI:
    is_connected = True

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def record(*args, **kwargs):
            self.calls.append(name)
            return None
        return record


class _DeadIPC:
    is_running = False
    last_error = "exited with code 1 before it listened"


def test_an_inbound_call_is_hung_up_when_the_audio_child_is_down(monkeypatch, capsys):
    from Orchestrator.asterisk import client as ari_mod
    from Orchestrator.routes import asterisk_routes as ar

    ari = _ARI()
    monkeypatch.setattr(ari_mod, "get_ari_client", lambda: ari)
    monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _DeadIPC())
    monkeypatch.setattr(ar, "PHONE_SESSIONS", {})
    asyncio.run(ar.handle_inbound_call("chan-1", "+15555550142", "s", []))
    assert "hangup_channel" in ari.calls
    assert "answer_channel" not in ari.calls, "no IVR without the audio path"
    assert "audio subprocess is not running" in capsys.readouterr().out


def test_an_outbound_call_fails_fast_when_the_audio_child_is_down(monkeypatch):
    from Orchestrator.asterisk import client as ari_mod
    from Orchestrator.phone.session import PhoneSession, PhoneStatus, CallDirection
    from Orchestrator.routes import asterisk_routes as ar

    ari = _ARI()
    monkeypatch.setattr(ari_mod, "get_ari_client", lambda: ari)
    monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _DeadIPC())
    session = PhoneSession(session_id="ast-out-1", direction=CallDirection.OUTBOUND,
                           status=PhoneStatus.RINGING)
    asyncio.run(ar._handle_outbound_call(session, "+15555550142", "tg-test"))
    assert "originate" not in ari.calls, "never dial a call nobody can hear"
    assert session.status == PhoneStatus.FAILED


def test_status_reports_the_audio_subprocess(monkeypatch):
    from Orchestrator.asterisk import client as ari_mod
    from Orchestrator.routes import asterisk_routes as ar

    class _IPC(_DeadIPC):
        def status(self):
            return {"running": False, "pid": None, "address": None, "restarts": 2,
                    "gave_up": True, "last_error": self.last_error}

    monkeypatch.setattr(ari_mod, "get_ari_client", lambda: None)
    monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _IPC())
    status = asyncio.run(ar.asterisk_status())
    assert status["audiosocket_running"] is False
    assert status["audio_subprocess"]["gave_up"] is True
    assert status["audio_subprocess"]["restarts"] == 2
