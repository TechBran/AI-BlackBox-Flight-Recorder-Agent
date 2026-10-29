#!/usr/bin/env python3
"""
audio_subprocess.py — Dedicated AudioSocket I/O Process

Runs with its own clean asyncio event loop. Handles:
1. AudioSocket TCP server on the host/port the parent passes (from
   ASTERISK_AUDIOSOCKET_HOST/PORT, the address the provisioned dialplan dials)
2. 20ms frame pacing for AI→phone audio (proven perfect in TTS test)
3. Resampling 24kHz→8kHz via scipy resample_poly
4. IPC with main BlackBox process via unix socket

This subprocess has NOTHING else running — just audio I/O.
The clean event loop guarantees precise 20ms frame timing that the
loaded main process cannot maintain.

Usage: python3 -I -u audio_subprocess.py <ipc_fd> <host> <port>

The parent (audio_ipc.py) runs it with -I, so this directory is not put first
on sys.path: nothing here may shadow the stdlib for numpy and scipy (audit
W14). It sends READY with the bound address once it listens; the parent
reports the AudioSocket server started only then.

It exits on its own when the parent dies without sending SHUTDOWN (SIGKILL,
the OOM killer; blackbox.service's KillMode=process leaves it running): the IPC
socket reaches EOF, and a reparent check backs that up. Both used to be
defeated by the log pipe: stdout is a pipe the parent reads, so once the parent
was gone the next print raised BrokenPipeError from the very branch that set
_shutdown, and the orphan kept the AudioSocket port (the unit's
`pkill -9 -f audio_subprocess.py` ExecStartPre was the backstop). Writes to the
dead pipe are now dropped (_QuietStream) and _shutdown is set before logging.
"""

import asyncio
import collections
import os
import signal
import socket
import struct
import sys
import time
import uuid as uuid_mod

import numpy as np
from scipy.signal import resample_poly

# ---------------------------------------------------------------------------
# IPC message types (between this subprocess and the main process)
# ---------------------------------------------------------------------------
MSG_PHONE_AUDIO = 0x01    # subprocess→main: uuid(36) + 8kHz PCM16
MSG_AI_AUDIO = 0x02       # main→subprocess: uuid(36) + 24kHz PCM16
MSG_CHANNEL_OPEN = 0x03   # subprocess→main: uuid(36)
MSG_CHANNEL_CLOSE = 0x04  # subprocess→main: uuid(36)
MSG_CLEAR_BUFFER = 0x05   # main→subprocess: uuid(36)
MSG_SHUTDOWN = 0x06       # main→subprocess: clean exit
MSG_READY = 0x07          # subprocess→main: "host:port" once AudioSocket listens

# IPC header: [type:1][length:4 LE uint32]
IPC_HEADER_SIZE = 5
IPC_HEADER_STRUCT = struct.Struct("<BI")  # 1 byte type + 4 byte LE uint32 length

# ---------------------------------------------------------------------------
# AudioSocket protocol constants
# ---------------------------------------------------------------------------
AS_HEADER_SIZE = 3        # [type:1][length:2 BE uint16]
AS_TYPE_HANGUP = 0x00
AS_TYPE_UUID = 0x01
AS_TYPE_AUDIO = 0x10
AS_FRAME_BYTES = 320      # 20ms at 8kHz, 16-bit mono (160 samples × 2 bytes)

# Pre-buffer threshold before starting drain playback
# Reduced from 50 (1000ms) to 5 (100ms) for interactive voice latency.
# 100ms absorbs jitter without adding perceptible delay.
PRE_BUFFER_FRAMES = 5     # 5 × 20ms = 100ms




class _QuietStream:
    """stdout/stderr that drops a write once the parent's log pipe is gone,
    instead of raising BrokenPipeError into whatever was logging."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        try:
            return self._stream.write(text)
        except (OSError, ValueError):
            return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


# ---------------------------------------------------------------------------
# OutputChannel — per-channel audio output with 20ms frame pacing
# ---------------------------------------------------------------------------
class OutputChannel:
    """
    Per-channel audio output buffer with precise 20ms drain pacing.

    AI audio arrives as 24kHz PCM16 in bulk. We resample to 8kHz,
    chunk into 320-byte (20ms) frames, and drain one frame every 20ms
    over the AudioSocket TCP connection.
    """

    def __init__(self, channel_id: str, writer: asyncio.StreamWriter):
        self.channel_id = channel_id
        self._writer = writer
        self._frames: collections.deque = collections.deque()
        self._drain_task: asyncio.Task | None = None
        self._draining = False

    def push_24k(self, pcm16_data: bytes) -> None:
        """
        Receive 24kHz PCM16 audio, resample to 8kHz, chunk into 320-byte
        frames, and queue for paced output. Starts drain if not running.
        """
        if not pcm16_data:
            return

        # Convert to numpy int16 array
        samples_24k = np.frombuffer(pcm16_data, dtype=np.int16)
        if len(samples_24k) == 0:
            return

        # Resample 24kHz → 8kHz (ratio 1:3)
        samples_8k = resample_poly(samples_24k, up=1, down=3).astype(np.int16)

        # Convert back to bytes
        raw_8k = samples_8k.tobytes()

        # Chunk into 320-byte (20ms) frames
        offset = 0
        while offset + AS_FRAME_BYTES <= len(raw_8k):
            self._frames.append(raw_8k[offset:offset + AS_FRAME_BYTES])
            offset += AS_FRAME_BYTES

        # Handle remainder — pad with silence to fill a complete frame
        if offset < len(raw_8k):
            remainder = raw_8k[offset:]
            padded = remainder + b"\x00" * (AS_FRAME_BYTES - len(remainder))
            self._frames.append(padded)

        # Start drain task if not already running
        if not self._draining and self._drain_task is None:
            self._drain_task = asyncio.ensure_future(self._drain())

    def clear(self) -> None:
        """Barge-in: clear all queued frames immediately."""
        self._frames.clear()
        print(f"[OutputChannel] Buffer cleared: {self.channel_id}")

    def stop(self) -> None:
        """Stop the drain task and clear buffers."""
        self._frames.clear()
        self._draining = False
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            self._drain_task = None

    async def _drain(self) -> None:
        """
        Drain loop: send one 320-byte frame every 20ms over AudioSocket.

        Pre-buffers PRE_BUFFER_FRAMES (1000ms) before starting playback
        to absorb jitter. This is the SAME pattern as the perfect TTS test.
        """
        self._draining = True
        loop = asyncio.get_event_loop()

        try:
            # Pre-buffer: wait until we have enough frames queued
            while len(self._frames) < PRE_BUFFER_FRAMES:
                await asyncio.sleep(0.02)  # Check every 20ms
                if not self._draining:
                    return

            print(f"[OutputChannel] Drain started: {self.channel_id} "
                  f"({len(self._frames)} frames buffered)")

            next_send = loop.time()

            while self._frames:
                if not self._draining:
                    return

                frame = self._frames.popleft()

                # Build AudioSocket frame: [type:1][length:2 BE uint16][payload]
                header = bytes([
                    AS_TYPE_AUDIO,
                    (AS_FRAME_BYTES >> 8) & 0xFF,
                    AS_FRAME_BYTES & 0xFF,
                ])

                try:
                    self._writer.write(header + frame)
                    await self._writer.drain()
                except (ConnectionError, BrokenPipeError, OSError):
                    print(f"[OutputChannel] Write failed, connection lost: {self.channel_id}")
                    self._frames.clear()
                    return

                # Pace at exactly 20ms intervals using event loop clock
                next_send += 0.02
                now = loop.time()
                sleep_time = next_send - now
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                else:
                    # We're behind — skip sleep but don't accumulate debt
                    # beyond 100ms (5 frames)
                    if sleep_time < -0.1:
                        next_send = loop.time()

            print(f"[OutputChannel] Drain finished: {self.channel_id}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[OutputChannel] Drain error: {e}")
        finally:
            self._draining = False
            self._drain_task = None


# ---------------------------------------------------------------------------
# AudioSubprocess — main class
# ---------------------------------------------------------------------------
class AudioSubprocess:
    """
    Standalone AudioSocket I/O process.

    Manages:
    - AudioSocket TCP server (Asterisk connects here per-call)
    - IPC with main BlackBox process via unix socketpair
    - Per-channel OutputChannel instances for paced audio output
    """

    def __init__(self, ipc_fd: int, host: str, port: int):
        self._ipc_fd = ipc_fd
        self._host = host
        self._port = port
        self._ipc_sock: socket.socket | None = None
        self._ipc_reader: asyncio.StreamReader | None = None
        self._ipc_writer: asyncio.StreamWriter | None = None
        self._channels: dict[str, OutputChannel] = {}
        self._as_writers: dict[str, asyncio.StreamWriter] = {}
        self._shutdown = False
        self._server: asyncio.Server | None = None
        # Who started us. A different getppid() later means we were reparented:
        # the parent died without SHUTDOWN (see the module docstring).
        self._parent_pid = os.getppid()

    async def run(self) -> int:
        """Main entry point — set up IPC, AudioSocket server, and run forever.
        Returns the exit code (2 when the AudioSocket address cannot be bound)."""
        loop = asyncio.get_event_loop()

        # --- Set up IPC socket as asyncio streams ---
        self._ipc_sock = socket.fromfd(self._ipc_fd, socket.AF_UNIX, socket.SOCK_STREAM)
        # Prevent the fd from being closed when socket object is GC'd
        # (we own it, passed via command line)
        self._ipc_sock.setblocking(False)

        ipc_reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(ipc_reader)
        transport, _ = await loop.create_connection(
            lambda: protocol, sock=self._ipc_sock
        )
        self._ipc_reader = ipc_reader
        # Create a writer from the transport
        self._ipc_writer = asyncio.StreamWriter(
            transport, protocol, ipc_reader, loop
        )

        print(f"[AudioSubprocess] IPC connected (fd={self._ipc_fd})")

        # --- Signal handling ---
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)

        # --- Start AudioSocket TCP server ---
        try:
            self._server = await asyncio.start_server(
                self._handle_audiosocket,
                self._host,
                self._port,
            )
        except OSError as e:
            # Most often an orphaned child of an earlier run still holds the port.
            print(f"[AudioSubprocess] Cannot listen on {self._host}:{self._port}: {e}")
            return 2
        addr = self._server.sockets[0].getsockname()
        print(f"[AudioSubprocess] AudioSocket server listening on {addr[0]}:{addr[1]}")
        # Liveness: the parent reports the server started only on this message.
        self._send_ipc(MSG_READY, f"{addr[0]}:{addr[1]}".encode("ascii"))

        # --- Start IPC listener ---
        ipc_task = asyncio.ensure_future(self._ipc_listener())

        # --- Run until shutdown ---
        try:
            while not self._shutdown:
                await asyncio.sleep(1.0)
                if os.getppid() != self._parent_pid:
                    # The IPC EOF normally says this first; this covers an IPC
                    # end something else still holds.
                    self._shutdown = True
                    print("[AudioSubprocess] Parent gone (reparented), shutting down")
        except asyncio.CancelledError:
            pass

        # --- Cleanup ---
        print("[AudioSubprocess] Shutting down...")
        ipc_task.cancel()
        try:
            await ipc_task
        except asyncio.CancelledError:
            pass

        # Stop all output channels
        for ch in self._channels.values():
            ch.stop()
        self._channels.clear()

        # Close all AudioSocket connections
        for writer in self._as_writers.values():
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._as_writers.clear()

        # Close server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Close IPC
        if self._ipc_writer:
            try:
                self._ipc_writer.close()
                await self._ipc_writer.wait_closed()
            except Exception:
                pass

        print("[AudioSubprocess] Exited cleanly.")
        return 0

    def _signal_handler(self) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        self._shutdown = True
        print("[AudioSubprocess] Signal received, shutting down...")

    # ------------------------------------------------------------------
    # AudioSocket connection handler
    # ------------------------------------------------------------------

    async def _handle_audiosocket(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single AudioSocket connection from Asterisk."""
        peer = writer.get_extra_info("peername")
        channel_id = None

        try:
            print(f"[AudioSubprocess] AudioSocket connection from {peer}")

            # --- Read UUID frame (must be first) ---
            header = await reader.readexactly(AS_HEADER_SIZE)
            frame_type = header[0]
            length = (header[1] << 8) | header[2]

            if frame_type != AS_TYPE_UUID:
                print(f"[AudioSubprocess] Expected UUID frame, got type={frame_type:#x}")
                writer.close()
                return

            payload = await reader.readexactly(length) if length > 0 else b""

            # Asterisk sends UUID as 16-byte binary
            if len(payload) == 16:
                channel_id = str(uuid_mod.UUID(bytes=payload))
            else:
                channel_id = payload.decode("ascii", errors="replace").strip()

            print(f"[AudioSubprocess] Channel opened: {channel_id}")

            # Store writer and create output channel
            self._as_writers[channel_id] = writer
            output_ch = OutputChannel(channel_id, writer)
            self._channels[channel_id] = output_ch

            # Notify main process: CHANNEL_OPEN
            self._send_ipc(MSG_CHANNEL_OPEN, channel_id.encode("ascii"))

            # --- Audio read loop ---
            while not self._shutdown:
                try:
                    header = await reader.readexactly(AS_HEADER_SIZE)
                except (asyncio.IncompleteReadError, ConnectionError):
                    print(f"[AudioSubprocess] Connection lost: {channel_id}")
                    break

                frame_type = header[0]
                length = (header[1] << 8) | header[2]

                if length > 0:
                    try:
                        payload = await reader.readexactly(length)
                    except (asyncio.IncompleteReadError, ConnectionError):
                        print(f"[AudioSubprocess] Incomplete read: {channel_id}")
                        break
                else:
                    payload = b""

                if frame_type == AS_TYPE_AUDIO:
                    # Forward phone audio to main process via IPC
                    # Payload: uuid(36 bytes ASCII) + audio data
                    ipc_payload = channel_id.encode("ascii") + payload
                    self._send_ipc(MSG_PHONE_AUDIO, ipc_payload)

                elif frame_type == AS_TYPE_HANGUP:
                    print(f"[AudioSubprocess] Hangup: {channel_id}")
                    break

                elif frame_type == 0xFF:
                    print(f"[AudioSubprocess] Error frame: {channel_id}")
                    break

        except asyncio.IncompleteReadError:
            print(f"[AudioSubprocess] EOF during handshake: {peer}")
        except Exception as e:
            print(f"[AudioSubprocess] Connection error: {e}")
        finally:
            # Cleanup
            if channel_id:
                # Stop output channel
                ch = self._channels.pop(channel_id, None)
                if ch:
                    ch.stop()
                self._as_writers.pop(channel_id, None)

                # Notify main process: CHANNEL_CLOSE
                self._send_ipc(MSG_CHANNEL_CLOSE, channel_id.encode("ascii"))
                print(f"[AudioSubprocess] Channel closed: {channel_id}")

            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # IPC listener — reads messages from main process
    # ------------------------------------------------------------------

    async def _ipc_listener(self) -> None:
        """Read IPC messages from the main BlackBox process."""
        try:
            while not self._shutdown:
                # Read IPC header: [type:1][length:4 LE uint32]
                try:
                    header_bytes = await self._ipc_reader.readexactly(IPC_HEADER_SIZE)
                except (asyncio.IncompleteReadError, ConnectionError):
                    self._shutdown = True
                    print("[AudioSubprocess] IPC disconnected (parent gone?)")
                    return

                msg_type, length = IPC_HEADER_STRUCT.unpack(header_bytes)

                # Read payload
                if length > 0:
                    try:
                        payload = await self._ipc_reader.readexactly(length)
                    except (asyncio.IncompleteReadError, ConnectionError):
                        self._shutdown = True
                        print("[AudioSubprocess] IPC read error")
                        return
                else:
                    payload = b""

                # Dispatch
                if msg_type == MSG_AI_AUDIO:
                    # payload = uuid(36 bytes) + 24kHz PCM16 audio
                    if len(payload) < 36:
                        continue
                    chan_id = payload[:36].decode("ascii", errors="replace")
                    audio_data = payload[36:]
                    ch = self._channels.get(chan_id)
                    if ch:
                        ch.push_24k(audio_data)
                    else:
                        print(f"[AudioSubprocess] AI audio for unknown channel: {chan_id}")

                elif msg_type == MSG_CLEAR_BUFFER:
                    # payload = uuid(36 bytes)
                    chan_id = payload[:36].decode("ascii", errors="replace")
                    ch = self._channels.get(chan_id)
                    if ch:
                        ch.clear()

                elif msg_type == MSG_SHUTDOWN:
                    self._shutdown = True
                    print("[AudioSubprocess] Shutdown command received")
                    return

                else:
                    print(f"[AudioSubprocess] Unknown IPC message type: {msg_type:#x}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._shutdown = True
            print(f"[AudioSubprocess] IPC listener error: {e}")

    # ------------------------------------------------------------------
    # IPC send helper
    # ------------------------------------------------------------------

    def _send_ipc(self, msg_type: int, payload: bytes) -> None:
        """
        Send an IPC message to the main process.

        Format: [type:1][length:4 LE uint32][payload]
        Uses synchronous socket write — safe because IPC messages are small
        and unix sockets have large kernel buffers.
        """
        if self._ipc_writer is None:
            return

        header = IPC_HEADER_STRUCT.pack(msg_type, len(payload))
        try:
            self._ipc_writer.write(header + payload)
            # Don't await drain here — called from sync context (push callbacks)
            # and from async context (IPC listener). The event loop will flush.
        except (ConnectionError, BrokenPipeError, OSError):
            self._shutdown = True
            print("[AudioSubprocess] IPC write failed (parent gone?)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: audio_subprocess.py <ipc_fd> <host> <port>")
        sys.exit(1)

    # stdout/stderr are the parent's log pipe: never let it dying raise here.
    sys.stdout = _QuietStream(sys.stdout)
    sys.stderr = _QuietStream(sys.stderr)
    sub = AudioSubprocess(int(sys.argv[1]), sys.argv[2], int(sys.argv[3]))

    try:
        sys.exit(asyncio.run(sub.run()))
    except KeyboardInterrupt:
        pass
