"""Asterisk IVR prompts: installed by the service itself, never in the call path.

Scripts/install.sh makes Asterisk's IVR prompt dir owned by the service user
and writable through blackbox.service's Asterisk drop-in, so the runtime
writes prompts there directly: no sudo, no Asterisk reload (audit
O11-telephony-asterisk-cellular-05). It used to shell out to sudo mkdir/cp
and `sudo asterisk -rx 'core reload'`, which the service user is not granted
and which ProtectSystem=strict blocks anyway, so on every box without a
blanket sudo grant the IVR played nothing.

A box whose installer predates the drop-in still has a read-only (EROFS)
sound dir. The PIN stage once built a per-length prompt in the call path and
copied it there; from the second call on the copy raised OSError(EROFS) and
every inbound call was hung up before the greeting.

These tests drive the REAL ivr_audio functions against a tmp "sound dir",
read-only (EROFS) or writable, and pin that:
  * prompts install with no subprocess at all (no sudo, no asterisk -rx),
    atomically and readable by Asterisk (0644);
  * generate_prompt / ensure_all_prompts never raise and report what failed,
    and a read-only dir is an explicit error naming the fix;
  * the IVR picks prompts by existence only (no TTS, no write) and falls back
    to prompts every box already has, so the right PIN still gets a greeting,
    a PIN prompt and access, and refusals still speak;
  * every prompt the IVR plays resolves to a tracked WAV (itself or a
    fallback), for every PIN length, and per-box optional prompts are
    gitignored so a pull never collides with them;
  * the sound: URI points at the dir the prompts are installed into, which is
    the dir Scripts/install.sh makes writable.

Hermetic: tmp dirs only; TTS is stubbed and any subprocess is recorded and
fails the test (sudo works on the dev box, so nothing may reach the real one).
"""
import asyncio
import errno
import os
import re
import shutil
import stat
import subprocess
import tempfile
import wave
from pathlib import Path

import pytest

from Orchestrator import config
from Orchestrator.asterisk import ivr as ivr_mod
from Orchestrator.asterisk import ivr_audio
from Orchestrator.phone import pin_gate

PIN = "739104"
CHANNEL = "chan-1"
CALLER = "+15555550142"
TRACKED_CACHE = Path(ivr_audio.__file__).resolve().parent / "ivr_cache"
_git = subprocess.run  # captured before sound_env stubs subprocess.run
# What every box that has ever run the IVR already has installed.
BASE_PROMPTS = ("greeting", "pin_retry", "pin_accepted", "pin_failed", "goodbye")


def _write_wav(path: Path, seconds: float = 0.5):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x01\x00" * int(8000 * seconds))


@pytest.fixture
def sound_env(tmp_path, monkeypatch):
    """A read-only (EROFS) Asterisk sound dir plus a writable local cache, as
    under blackbox.service before the installer's drop-in; ``make_writable()``
    gives the dir the drop-in's ReadWritePaths hole."""
    sound_dir = tmp_path / "sounds" / "en" / "custom" / "ivr"
    sound_dir.mkdir(parents=True)
    cache_dir = tmp_path / "ivr_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(ivr_audio, "ASTERISK_SOUND_DIR", str(sound_dir))
    monkeypatch.setattr(ivr_audio, "LOCAL_CACHE_DIR", str(cache_dir))

    calls = {"write": [], "run": [], "tts": []}
    state = {"writable": False}
    real_mkstemp, real_access = tempfile.mkstemp, os.access

    def in_sound_dir(path):
        return path is not None and str(path).startswith(str(sound_dir))

    def mkstemp(*a, dir=None, **kw):
        if in_sound_dir(dir):
            calls["write"].append(str(dir))
            if not state["writable"]:
                raise OSError(errno.EROFS, "Read-only file system", str(dir))
        return real_mkstemp(*a, dir=dir, **kw)

    def access(path, mode, *a, **kw):
        if in_sound_dir(path) and mode & os.W_OK and not state["writable"]:
            return False
        return real_access(path, mode, *a, **kw)

    def no_subprocess(name):
        def record(*a, **kw):
            calls["run"].append((name, a))
            raise OSError(errno.EPERM, "no subprocess may run here (sudo, asterisk -rx)")
        return record

    def no_tts():
        calls["tts"].append(True)
        raise AssertionError("TTS must not run here")

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(os, "access", access)
    for name in ("run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, no_subprocess(name))
    monkeypatch.setattr(os, "system", no_subprocess("os.system"))
    monkeypatch.setattr(ivr_audio, "_get_openai_config", no_tts)
    return {"sound": sound_dir, "cache": cache_dir, "calls": calls,
            "make_writable": lambda: state.update(writable=True)}


def _install(sound_env, *names):
    for name in names:
        _write_wav(sound_env["sound"] / f"{name}.wav")


@pytest.fixture
def pin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)
    monkeypatch.setattr(config, "PHONE_PIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")
    monkeypatch.setattr(ivr_mod.asyncio, "sleep", _fast_sleep)


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **kw):
    await _real_sleep(0)


class FakeARI:
    """Records what is played; 'presses' the next entry when a PIN prompt plays
    (and ``menu_digit`` when the backend menu plays)."""

    PIN_PROMPTS = ("pin_prompt", "pin_entry", "pin_retry")

    def __init__(self, entries, menu_digit=None):
        self.entries = list(entries)
        self.menu_digit = menu_digit
        self.played = []
        self.on_dtmf = None

    async def play_sound(self, channel_id, media):
        self.played.append(media)
        digits = None
        if any(p in media for p in self.PIN_PROMPTS) and self.entries:
            digits = self.entries.pop(0)
        elif "backend_menu" in media and self.menu_digit:
            digits = self.menu_digit
        if digits:

            async def press():
                for d in digits:
                    await self.on_dtmf(channel_id, d)
                    await asyncio.sleep(0)

            asyncio.get_running_loop().create_task(press())
        return None

    async def stop_playback(self, playback_id):
        return True

    async def wait_for_playback(self, playback_id, timeout=15.0):
        return True

    # The ARI client's per-channel routing (Orchestrator/asterisk/client.py)
    def register_dtmf_handler(self, channel_id, handler):
        self.on_dtmf = handler

    def unregister_dtmf_handler(self, channel_id):
        self.on_dtmf = None

    def watch_hangup(self, channel_id):
        return asyncio.Event()  # the caller never hangs up here

    def unwatch_hangup(self, channel_id):
        pass


def _run_pin(entries, caller=CALLER):
    fake = FakeARI(entries)
    ivr = ivr_mod.AsteriskIVR(ari_client=fake, channel_id=CHANNEL, caller_id=caller)
    fake.on_dtmf = ivr._on_dtmf
    ok = asyncio.run(ivr._run_pin())
    return ok, fake


uri = ivr_audio.get_prompt_uri


# ── the blocker: a read-only sound dir must not drop the call ───────────────

def test_right_pin_still_gets_greeting_prompt_and_access_on_read_only_sounds(sound_env, pin_env):
    _install(sound_env, *BASE_PROMPTS, "pin_entry")
    # What the first call on the old code left behind: a cached, uninstalled prompt.
    _write_wav(sound_env["cache"] / f"pin_prompt_{len(PIN)}digit.wav")
    ok, fake = _run_pin([PIN])
    assert ok is True
    assert fake.played[0] == uri("greeting")
    assert uri("pin_entry") in fake.played
    assert uri("pin_accepted") in fake.played
    calls = sound_env["calls"]
    assert not calls["tts"] and not calls["write"] and not calls["run"], \
        "the call path must never generate or install audio"


def test_call_path_with_nothing_installed_never_raises_or_generates(sound_env, pin_env):
    ok, fake = _run_pin([PIN])
    assert ok is True, "a silent prompt must not cost the caller the call"
    assert fake.played[0] == uri("greeting")
    assert not sound_env["calls"]["tts"] and not sound_env["calls"]["write"]


def test_pin_prompt_prefers_the_length_specific_installed_prompt(sound_env, pin_env):
    _install(sound_env, *BASE_PROMPTS, "pin_entry", f"pin_prompt_{len(PIN)}digit")
    ok, fake = _run_pin([PIN])
    assert ok is True
    assert uri(f"pin_prompt_{len(PIN)}digit") in fake.played
    assert uri("pin_entry") not in fake.played


def test_legacy_four_digit_prompt_is_used_only_for_four_digit_pins(sound_env, pin_env, monkeypatch):
    _install(sound_env, *BASE_PROMPTS, "pin_prompt")  # the pre-2026-10 install
    ok, fake = _run_pin([PIN])
    assert ok is True
    assert uri("pin_prompt") not in fake.played, "it says '4-digit'; this PIN has 6"
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "7391")
    ok, fake = _run_pin(["7391"])
    assert ok is True
    assert uri("pin_prompt") in fake.played


def test_unconfigured_refusal_speaks_with_an_installed_prompt(sound_env, pin_env, monkeypatch):
    _install(sound_env, *BASE_PROMPTS)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    ok, fake = _run_pin([PIN])
    assert ok is False
    assert fake.played == [uri("goodbye")]
    _install(sound_env, "pin_unavailable")
    ok, fake = _run_pin([PIN])
    assert fake.played == [uri("pin_unavailable")]


def test_lockout_refusal_speaks_with_an_installed_prompt(sound_env, pin_env):
    _install(sound_env, *BASE_PROMPTS)
    for _ in range(pin_gate.max_attempts()):
        pin_gate.record_failure(CALLER)
    ok, fake = _run_pin([PIN])
    assert ok is False
    assert fake.played == [uri("pin_failed")]
    _install(sound_env, "pin_locked")
    ok, fake = _run_pin([PIN])
    assert fake.played == [uri("pin_locked")]


def test_ivr_run_completes_the_pin_stage_on_read_only_sounds(sound_env, pin_env, monkeypatch):
    """The whole IVR, not just _run_pin: the right PIN reaches operator selection."""
    _install(sound_env, *BASE_PROMPTS)
    _write_wav(sound_env["cache"] / f"pin_prompt_{len(PIN)}digit.wav")
    monkeypatch.setattr(config, "USERS_LIST", ["OperatorA"])
    monkeypatch.setattr(config, "IVR_DEFAULT_BACKEND", "openai_realtime")
    fake = FakeARI([PIN], menu_digit="3")
    ivr = ivr_mod.AsteriskIVR(ari_client=fake, channel_id=CHANNEL, caller_id=CALLER)
    fake.on_dtmf = ivr._on_dtmf
    result = asyncio.run(ivr.run())
    assert result is not None
    assert result["operator"] == "OperatorA" and result["backend"] == "openai_realtime"


# ── generate_prompt / ensure_all_prompts on a read-only sound dir ───────────

def test_generate_prompt_reports_false_instead_of_raising(sound_env):
    _write_wav(sound_env["cache"] / "pin_entry.wav")
    result = asyncio.run(ivr_audio.generate_prompt("pin_entry", "Please enter your access code."))
    assert result is False, "a cached prompt that could not be installed is not ready"
    assert not (sound_env["sound"] / "pin_entry.wav").exists()


def test_generate_prompt_installs_when_the_dir_is_writable(sound_env):
    sound_env["make_writable"]()
    _write_wav(sound_env["cache"] / "pin_entry.wav")
    assert asyncio.run(ivr_audio.generate_prompt("pin_entry", "x")) is True
    installed = sound_env["sound"] / "pin_entry.wav"
    assert installed.read_bytes() == (sound_env["cache"] / "pin_entry.wav").read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o644, "Asterisk (another user) reads it"
    assert sorted(p.name for p in sound_env["sound"].iterdir()) == ["pin_entry.wav"], \
        "no temp file left behind"
    assert not sound_env["calls"]["run"]


def test_a_root_owned_prompt_is_replaced_not_rewritten_in_place(sound_env):
    """Prompts a root-run installer left behind stay root-owned; the service
    owns the dir, so it replaces them by rename (never opens them for write)."""
    sound_env["make_writable"]()
    old = sound_env["sound"] / "greeting.wav"
    _write_wav(old, seconds=0.2)
    old.chmod(0o444)
    _write_wav(sound_env["cache"] / "greeting.wav", seconds=0.6)
    assert ivr_audio._install(str(sound_env["cache"] / "greeting.wav"), str(old)) is True
    assert old.read_bytes() == (sound_env["cache"] / "greeting.wav").read_bytes()


def test_ensure_all_prompts_installs_the_shipped_prompts_with_no_sudo(sound_env, monkeypatch, capsys):
    """A fresh box: the dir is writable, the tracked cache is all there is, and
    TTS has no credits. Every shipped prompt is installed, and nothing is run
    (the old code ran `sudo asterisk -rx 'core reload'` on every boot)."""
    sound_env["make_writable"]()
    for wav in _tracked_wavs():
        shutil.copyfile(wav, sound_env["cache"] / wav.name)
    monkeypatch.setattr(config, "USERS_LIST", ["OperatorA"])
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)

    async def no_credits(text, voice):
        return None

    monkeypatch.setattr(ivr_audio, "_fetch_tts_pcm", no_credits)
    asyncio.run(ivr_audio.ensure_all_prompts())
    assert sound_env["calls"]["run"] == [], "no sudo, no asterisk -rx"
    for wav in _tracked_wavs():
        if not wav.name.startswith("operator_menu_"):  # per box, made from its operators
            assert ivr_audio.is_installed(wav.stem), wav.name
    assert "not writable" not in capsys.readouterr().out


def test_generate_prompt_tts_path_caches_but_reports_not_installed(sound_env, monkeypatch):
    async def fake_pcm(text, voice):
        return b"\x01\x00" * 24000  # 0.5 s of 24 kHz PCM16

    monkeypatch.setattr(ivr_audio, "_fetch_tts_pcm", fake_pcm)
    assert asyncio.run(ivr_audio.generate_prompt("fresh_prompt", "Hello.")) is False
    assert (sound_env["cache"] / "fresh_prompt.wav").exists()


def _tracked_wavs():
    if not shutil.which("git"):
        pytest.skip("git not available")
    listed = _git(["git", "ls-files", "*.wav"], capture_output=True, text=True,
                  cwd=TRACKED_CACHE)
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    return [TRACKED_CACHE / name for name in listed.stdout.split()]


def test_ensure_all_prompts_survives_read_only_sounds_and_says_how_to_fix(
        sound_env, monkeypatch, capsys):
    for wav in _tracked_wavs():
        shutil.copyfile(wav, sound_env["cache"] / wav.name)
    monkeypatch.setattr(config, "USERS_LIST", ["OperatorA"])
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)
    asked = []

    async def no_credits(text, voice):
        asked.append(text)
        return None  # e.g. HTTP 429 insufficient_quota

    monkeypatch.setattr(ivr_audio, "_fetch_tts_pcm", no_credits)
    asyncio.run(ivr_audio.ensure_all_prompts())  # must not raise
    out = capsys.readouterr().out
    assert "ERROR" in out and "not writable" in out and "Scripts/install.sh" in out, \
        "the boot log must say the dir is read-only and how to fix it"
    assert sound_env["calls"]["run"] == [], "no sudo fallback"
    # Only the optional prompts need TTS; everything else ships in the cache.
    assert sorted(asked) == sorted([ivr_audio.STATIC_PROMPTS["pin_locked"],
                                    ivr_audio.STATIC_PROMPTS["pin_unavailable"],
                                    ivr_audio.pin_prompt_text(len(PIN))])
    assert "pin_locked" in out and "TTS unavailable" in out


def test_ensure_all_prompts_makes_the_configured_length_prompt(sound_env, monkeypatch):
    sound_env["make_writable"]()
    monkeypatch.setattr(config, "USERS_LIST", [])
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "73910482")

    async def fake_pcm(text, voice):
        return b"\x01\x00" * 24000

    monkeypatch.setattr(ivr_audio, "_fetch_tts_pcm", fake_pcm)
    asyncio.run(ivr_audio.ensure_all_prompts())
    assert ivr_audio.is_installed("pin_prompt_8digit")
    assert ivr_audio.pin_prompt_name(8) == "pin_prompt_8digit"


# ── what ships ──────────────────────────────────────────────────────────────

IVR_PY = Path(ivr_mod.__file__).read_text()


def _prompts_the_ivr_plays():
    names = set()
    for args in re.findall(r'(?:get_prompt_uri|_refuse)\(([^)]*)\)', IVR_PY):
        names |= set(re.findall(r'"([a-z0-9_]+)"', args))
    names |= set(re.findall(r'"(confirm_[a-z]+)"', IVR_PY))
    assert {"greeting", "pin_retry", "pin_unavailable", "pin_locked", "backend_menu"} <= names
    return names


def test_a_fresh_install_can_speak_every_prompt_the_ivr_plays(sound_env):
    """With only the tracked cache installed (a fresh box after its first boot),
    every prompt the IVR asks for resolves to an installed WAV, for every PIN length."""
    for wav in _tracked_wavs():
        with wave.open(str(wav)) as wf:
            assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 8000), wav.name
            assert wf.getnframes() > 8000 * 0.5, f"{wav.name} is too short to be speech"
        shutil.copyfile(wav, sound_env["sound"] / wav.name)
    unplayable = [n for n in sorted(_prompts_the_ivr_plays())
                  if not ivr_audio.is_installed(ivr_audio.resolve_prompt(n))]
    assert not unplayable, f"no tracked prompt (or fallback) for: {unplayable}"
    for n in range(1, 20):
        if pin_gate.PIN_RE.fullmatch("7" * n):
            assert ivr_audio.is_installed(ivr_audio.pin_prompt_name(n)), n
    assert ivr_audio.pin_prompt_name(4) == "pin_prompt_4digit"
    assert ivr_audio.pin_prompt_name(6) == "pin_entry"


def test_optional_prompts_are_gitignored_and_fallbacks_are_not():
    """Optional prompts are made per box; tracking them would let a later
    commit collide with a box's own copy on `git pull`."""
    tracked = {p.name for p in _tracked_wavs()}  # skips outside a git checkout

    def ignored(name):
        return _git(["git", "check-ignore", "-q", str(TRACKED_CACHE / name)],
                    cwd=TRACKED_CACHE).returncode == 0

    for name in ("pin_prompt_6digit.wav", "pin_locked.wav", "pin_unavailable.wav",
                 "operator_menu_0123abcd.wav"):
        assert ignored(name), name
    for name in ("pin_entry.wav", "pin_prompt_4digit.wav", "pin_failed.wav", "goodbye.wav", "greeting.wav"):
        assert name in tracked and not ignored(name), name


def test_the_installer_makes_the_dir_the_runtime_writes_writable():
    """Scripts/install.sh chowns this exact dir to the service user and opens
    it in the drop-in; a drifted copy would leave the runtime read-only."""
    install_sh = (Path(ivr_audio.__file__).resolve().parents[2] / "Scripts" / "install.sh").read_text()
    match = re.search(r'IVR_SOUND_DIR="\$\{IVR_SOUND_DIR:-([^}]+)\}"', install_sh)
    assert match, "Scripts/install.sh no longer sets up the IVR prompt dir"
    assert match.group(1) == ivr_audio.ASTERISK_SOUND_DIR


def test_prompt_uri_points_at_the_install_dir():
    """Asterisk plays sound:<rel> from <sounds>/<channel language>/<rel>. Prompts
    are installed in <sounds>/en/custom/ivr, so they play as sound:custom/ivr/<name>."""
    parts = Path(ivr_audio.ASTERISK_SOUND_DIR).parts
    rel = "/".join(parts[parts.index("sounds") + 2:])  # skip "sounds" and the language
    assert ivr_audio.get_prompt_uri("greeting") == f"sound:{rel}/greeting"
