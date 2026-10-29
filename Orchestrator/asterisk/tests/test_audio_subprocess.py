"""The AudioSocket child: it starts with the stdlib intact, binds the address
the parent passes it, and the parent says it started only once it answers.

Audit W14 and O11-telephony-asterisk-cellular-09. Orchestrator/asterisk/
secrets.py shadowed the stdlib secrets module. The child is launched as a
script, so Python put Orchestrator/asterisk/ first on its sys.path,
numpy.random's `from secrets import token_hex` found the local module, and the
child died at every start ([AUDIO-SUB] Traceback on every boot of both boxes)
while startup.py logged "Audio subprocess started" from a flag. The child also
listened on a literal 9092 whatever ASTERISK_AUDIOSOCKET_PORT the provisioner
wrote into the dialplan.

The tests below start the REAL child (the venv's python, an ephemeral loopback
port), so they fail if it cannot import, bind or talk IPC.
"""
import asyncio
import logging
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from Orchestrator.asterisk import audio_ipc

ASTERISK_DIR = Path(audio_ipc.__file__).resolve().parent

AS_UUID, AS_AUDIO, AS_HANGUP = 0x01, 0x10, 0x00


def _frame(kind: int, payload: bytes = b"") -> bytes:
    """One AudioSocket frame: [type:1][length:2 BE][payload]."""
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


def test_no_module_beside_the_child_shadows_the_stdlib():
    shadows = sorted(p.stem for p in ASTERISK_DIR.glob("*.py") if p.stem in sys.stdlib_module_names)
    assert shadows == [], f"these would shadow the stdlib in the audio child: {shadows}"


def test_a_script_run_from_the_package_dir_gets_the_stdlib_secrets():
    """What `python Orchestrator/asterisk/<script>.py` sees: its own directory
    first on sys.path. numpy.random imports secrets from there."""
    probe = "import sys; sys.path.insert(0, sys.argv[1]); import secrets; print(secrets.__file__)"
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(ASTERISK_DIR)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).resolve().parent != ASTERISK_DIR, out.stdout


def test_the_child_is_launched_isolated_with_the_configured_address():
    client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=19092)
    argv = client._argv(7)
    assert argv[1:3] == ["-I", "-u"], "-I keeps the script's own dir off sys.path"
    assert argv[3].endswith("audio_subprocess.py")
    assert argv[4:] == ["7", "127.0.0.1", "19092"]


def test_the_default_address_comes_from_the_asterisk_config(monkeypatch):
    from Orchestrator.asterisk import config as acfg
    monkeypatch.setattr(acfg, "ASTERISK_AUDIOSOCKET_HOST", "127.0.0.2")
    monkeypatch.setattr(acfg, "ASTERISK_AUDIOSOCKET_PORT", 19555)
    client = audio_ipc.AudioIPCClient()
    assert (client.host, client.port) == ("127.0.0.2", 19555)


def test_an_empty_address_setting_falls_back_to_loopback(tmp_path):
    """ASTERISK_AUDIOSOCKET_HOST set but empty bound the child on every
    interface ('::') and wrote AudioSocket(${CALL_UUID},:9092) into the
    dialplan. AudioSocket has no authentication beyond the call uuid, so an
    empty value means the default, loopback; an empty port means 9092."""
    import os
    repo_root = ASTERISK_DIR.parents[1]
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "from Orchestrator.asterisk import config as c; "
             "print(repr(c.ASTERISK_AUDIOSOCKET_HOST), c.ASTERISK_AUDIOSOCKET_PORT)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASTERISK_")}
    env.update(ASTERISK_AUDIOSOCKET_HOST="", ASTERISK_AUDIOSOCKET_PORT="")
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(repo_root)], cwd=tmp_path,
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["'127.0.0.1'", "9092"]


def test_the_child_starts_listens_and_carries_a_call():
    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0)
        try:
            assert await client.start() is True
            assert client.is_running and client.pid
            host, port = client.address
            assert host == "127.0.0.1" and port > 0, "the bound address comes from the child"

            call = str(uuid.uuid4())
            client.expect_channel(call)
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(_frame(AS_UUID, uuid.UUID(call).bytes))
            await writer.drain()
            assert await client.wait_for_channel(call, timeout=10)

            loop = asyncio.get_running_loop()
            heard, hung_up = loop.create_future(), loop.create_future()

            async def on_audio(channel, pcm):
                if not heard.done():
                    heard.set_result((channel, pcm))

            async def on_hangup(channel):
                if not hung_up.done():
                    hung_up.set_result(channel)

            client.register_channel(call, on_audio=on_audio, on_hangup=on_hangup)
            writer.write(_frame(AS_AUDIO, b"\x01\x00" * 160))
            await writer.drain()
            channel, pcm = await asyncio.wait_for(heard, 10)
            assert channel == call and len(pcm) == 320

            writer.write(_frame(AS_HANGUP))
            await writer.drain()
            assert await asyncio.wait_for(hung_up, 10) == call
            writer.close()
        finally:
            await client.shutdown()
        assert not client.is_running

    asyncio.run(scenario())


def test_a_channel_that_opened_before_the_wait_is_not_missed():
    """continue_in_dialplan is awaited between expect_channel and
    wait_for_channel; Asterisk can connect in between."""
    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0)
        try:
            assert await client.start()
            call = str(uuid.uuid4())
            client.expect_channel(call)
            _, writer = await asyncio.open_connection(*client.address)
            writer.write(_frame(AS_UUID, uuid.UUID(call).bytes))
            await writer.drain()
            for _ in range(200):
                if call in client.active_channels:
                    break
                await asyncio.sleep(0.02)
            assert await client.wait_for_channel(call, timeout=2)
            writer.close()
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_start_fails_and_says_why_when_the_port_is_taken():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]

    async def scenario():
        client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=port)
        try:
            assert await client.start() is False
            assert not client.is_running
            assert "in use" in client.last_error.lower(), client.last_error
        finally:
            await client.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        taken.close()


def test_ipc_message_types_match_on_both_sides():
    from Orchestrator.asterisk import audio_subprocess as child
    names = [n for n in dir(audio_ipc) if n.startswith("MSG_")]
    assert "MSG_READY" in names
    for name in names:
        assert getattr(child, name) == getattr(audio_ipc, name), name


def test_startup_does_not_report_a_dead_child_as_started(monkeypatch, caplog):
    from Orchestrator import config, startup
    from Orchestrator.asterisk import client as ari

    class _Dead:
        is_running = False
        last_error = "exited with code 1 before it listened"
        address = None
        pid = None

    async def _init():
        return _Dead()

    async def _no_ari():
        return None

    monkeypatch.setattr(config, "ASTERISK_ENABLED", True)
    monkeypatch.setattr(audio_ipc, "init_ipc_client", _init)
    monkeypatch.setattr(ari, "init_ari_client", _no_ari)
    with caplog.at_level(logging.INFO, logger="blackbox.startup"):
        asyncio.run(startup.startup_asterisk())
    text = caplog.text
    assert "started" not in text.lower(), text
    assert "not running" in text and "exited with code 1" in text, text
