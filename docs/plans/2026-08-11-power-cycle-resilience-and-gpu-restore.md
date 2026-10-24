# Power-Cycle Resilience & MS02 GPU Restore — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore the dead NVIDIA driver on MS02 (which has silently crippled on-box TTS/STT since Aug 7), then make the stack physically incapable of hiding the same failure again.

**Architecture:** Two tracks that must not be confused. **Track A (Operator)** is everything on MS02 that needs a password-gated `sudo` or a physical console — no agent can do it (`sudo -n` fails for `bbx`). **Track B (Code)** is repo work done on the dev box, committed, pushed, and pulled by MS02 — never `scp`. The unifying principle: today every health signal reports *process liveness* (llama-swap `/health` returns 200, lspci still sees the card), so a driver-dead GPU box advertises itself as healthy while every synthesis 500s. We add a *capability* signal alongside the existing *liveness* signal, without breaking the documented anti-flap invariant.

**Tech Stack:** Ubuntu/systemd + Secure Boot MOK, NVIDIA 595-open, DKMS; Python 3.12 / FastAPI orchestrator; llama-swap proxy fronting Qwen3-TTS + Speaches (faster-whisper) members; pytest (baseline 4146 passing) + 6 bash perimeter tests.

---

## Context: What Actually Broke

Confirmed by reproduction, not inference (full detail in **SNAP-20260811-9039**):

- **Root cause:** MS02's NVIDIA kernel module has not loaded since **Aug 7 17:25**. Unattended-upgrades installed kernel `7.0.0-28-generic` on Jul 17-18, but the box kept running `6.17.0-35` for three weeks. The Canonical-**signed** nvidia modules package was only ever installed for `6.17.0-35` (once, on Jul 3, versioned — the kernel-tracking **meta package was never installed**). The Aug 7 storm forced the first boot into `7.0.0-28`, where the only candidate is the DKMS build signed by an **unenrolled** machine MOK key. Secure Boot is ON, so every boot logs `modprobe: ERROR: could not insert 'nvidia': Key was rejected by service`.
- **The Aug 10 storm did not cause this** — it is when Brandon *noticed*. The GPU was already dead for 3 days.
- **TTS 500s:** `qwen_tts_server` hard-pins `device_map='cuda:0'` with no CPU fallback. The member's `/health` passes *without* CUDA (zero-weight check), so llama-swap marks it ready, then every synthesis 500s with a bare 21-byte `Internal Server Error`.
- **STT slow/flaky:** Speaches falls back to CPU — 10-21s per utterance vs 0.6-2s on GPU — and `resolve_whisper_fit()` still selects the **flagship** `large-v3` because an lspci-only probe returns `vram_mb=None`, which the code reads as "defer to flagship". Clients disconnect before finals arrive, which is the "only transcribes occasionally" symptom.
- **Not the cause:** llama-swap (healthy, faithfully relaying 500s), Ollama (untouched since Jun 12, always CPU-only, serves nothing live), the dev box (has no localstack at all and is fine), gemma-box at `192.168.1.50` (a **third** machine, healthy), filesystem corruption (only EFI dirty-bit + journald self-heal), and the install script re-run (cannot repair a kernel module signature).

**No 501 exists** anywhere in the code or journals. Every failure is 500 (or 502/503 at the orchestrator boundary).

**"Did on-box audio ever actually work?" — YES, with dated proof.** See **Track D** for the full evidence. Short version: on-box STT ran 312/312 HTTP 200 with a 0.61s median through the whole GPU era, and on-box TTS produced 3.4 hours of valid audio across Jul 23-25. **This is a restoration, not a first-time bring-up** — do not let the work drift into rebuilding on-box audio from scratch. What corrupted the mental model is a genuine, separate, still-live bug: a silent provider substitution that swaps an explicitly pinned provider for OpenAI and logs nothing (**D-1**).

---

## Decision Gates — Answer Before Executing

These change what gets run. They are called out again inline at the relevant task.

**LOCKED 2026-08-11 (Brandon):** **D1** = signed-package route, confirmed ("we don't want it failing again"). **D2** = accepted; cache the 595.71.05 debs first (Step 1.3b), then proceed even though the 6.17 fallback is destroyed. **D5** = DONE; runaway `blackbox-setup` PID 26235 killed at 16:36, load 5.11 -> 4.70 and falling, no unsaved wizard state lost. **D3/D4** stand as recommended.

| # | Decision | Recommendation | Consequence |
|---|---|---|---|
| **D1** | Signed-package install vs MOK enrollment | **Signed package** (Task 1.4) | Needs no console and no reboot; keeps trust with Canonical only. MOK enrollment (Task 1.8) needs a physical keyboard at the blue MokManager screen — viable since MS02 is on the desk, but it widens the Secure Boot trust boundary to every locally-built module. |
| **D2** | Accept losing the "boot 6.17 kernel" rollback? | **Cache the .debs first**, then accept | The meta package **removes** `linux-modules-nvidia-595-open-6.17.0-35-generic`. Task 1.3b downloads the 595.71.05 debs to `/var/cache/apt/archives` first so ROLLBACK B stays available offline. |
| **D3** | Should `driver_ok=False` demote `derive_tier()` to LOW? | **No** — keep tier as-is for now | Demoting would make `rerank_routes.py:87-94` start returning HTTP 400 for GPU reranker selections on a driver-dead box. Arguably correct, but it is a behavior change with a wider blast radius than this incident needs. Revisit separately. |
| **D4** | CPU fallback for Qwen TTS? | **No — hard-fail loudly** | Estimated CPU RTF 10-40x means a 30s reply takes 5-20 minutes, blowing the 300s timeout anyway, while `QWEN_SYNTH_LOCK` starves all other on-box TTS. A silent CPU fallback is *precisely* the failure mode that hid this outage for 4 days. Gate any future CPU path behind `QWEN_TTS_ALLOW_CPU=1`, default off. |
| **D5** | Kill the runaway WebKit process on MS02? | **Ask Brandon first** | PID 26364 under `/usr/bin/blackbox-setup` is burning 514% CPU (17h+ wall). It is the Tauri setup wizard left open on the desktop — killing it may discard unsaved onboarding input. Not owned by systemd or autostart, so no config change fixes it. |

---

# TRACK A — Operator Actions on MS02 — ✅ COMPLETE 2026-08-11 19:31

**Executed and verified.** Brandon ran the operator steps; acceptance measured from the dev box.

| Check | Before | After |
|---|---|---|
| `nvidia-smi` | "couldn't communicate with the NVIDIA driver" | **RTX 2000 Ada, driver 595.84** |
| Driver at boot (no manual `modprobe`) | rejected every boot since Aug 7 | **loads automatically** ✓ |
| On-box TTS | HTTP **500** every time | HTTP **200**, valid 24 kHz mono WAV |
| STT warm transcription | 10.5-21.4 s (median 11.68) | **0.74 s** |
| `vllm-reranker` | crash-loop, 3366+ restarts | `active`, **NRestarts=0** |
| X display output | `None-1 disconnected` (dummy) | `DP-0 connected 3440x1440` |
| nouveau | driving X | unloaded |

**Round-trip proof:** synthesized *"Boot verified. Flight recorder fully restored."* and Whisper transcribed it back correctly.

**Two things worth recording for the runbook:**

1. **The display freeze was a boot race, not a driver fault.** X had started at the 18:06 boot *before* any working driver existed, failed `[drm] Failed to open DRM device for pci:0000:02:00.0: -19` (ENODEV), fell back to fbdev, failed that too, and attached to a dummy screen. It then drew into nothing for 80 minutes. Loading the driver by hand does **not** rescue an X server that already lost the race — **X never re-probes for a GPU that appears late.** A reboot is the fix, and it is also the only way to prove the boot path.
2. **A manual `modprobe` proves nothing about durability.** The first two boots (18:04, 18:06) *preceded* the package install at 19:16, so the driver was only running because it was loaded by hand. Durability was only established by the 19:30 reboot. Do not close a driver repair without a clean boot.

**Not yet done in Track A:** `/etc/default/grub` still has `GRUB_TIMEOUT=0` + `GRUB_TIMEOUT_STYLE=hidden` (Step 1.2 was skipped), so a future bad boot still needs physical Esc at power-on. The 595.71.05 rollback debs were cached, and the 6.17 module package is now removed as expected.

---

## Original Track A instructions (retained for the runbook)


> **Every task in this track requires an interactive `ssh bbx@192.168.1.153` session and a typed sudo password.** `bbx` has full `(ALL : ALL) ALL` sudo but it is password-gated; `sudo -n true` fails. Nothing here can be scripted or run by an agent.

## Task 1: Restore the NVIDIA Driver

**Files:** none (system state only)

### Step 1.1: Capture the baseline and the GRUB titles

The rollback command depends on exact GRUB menu titles, and `/boot/grub/grub.cfg` is `0600 root` so recon could not read them.

```bash
ssh bbx@192.168.1.153
uname -r
dpkg -l | grep -E 'linux-modules-nvidia|nvidia-kernel-common|linux-image-'
sudo grep -nE "^[[:space:]]*(menuentry|submenu)" /boot/grub/grub.cfg
sudo grep -n "next_entry" /boot/grub/grub.cfg
```

Record the submenu title and the `6.17.0-35` menuentry title **verbatim**.

**Expected:** `uname -r` = `7.0.0-28-generic`. The second grep must print the `if [ "${next_entry}" ]` boilerplate — if it does not, `grub-reboot` will silently no-op and rollback must use `grub-set-default` or a physical Esc at power-on.

### Step 1.2: Make the box recoverable from a bad boot

`/etc/default/grub` currently has `GRUB_TIMEOUT=0` + `GRUB_TIMEOUT_STYLE=hidden`, so **no boot menu is shown at all**.

```bash
sudo cp /etc/default/grub /etc/default/grub.bak-$(date +%Y%m%d)
sudo sed -i 's/^GRUB_TIMEOUT_STYLE=hidden/GRUB_TIMEOUT_STYLE=menu/; s/^GRUB_TIMEOUT=0/GRUB_TIMEOUT=10/' /etc/default/grub
sudo update-grub
grep -E '^GRUB_(TIMEOUT|TIMEOUT_STYLE)' /etc/default/grub
```

**Expected:** `GRUB_TIMEOUT_STYLE=menu` and `GRUB_TIMEOUT=10`. Revert from the `.bak` once the fix is proven.

### Step 1.3: Re-run the apt simulation, then cache the rollback debs (D2)

```bash
sudo apt-get update
apt-get -s install linux-modules-nvidia-595-open-generic-hwe-24.04
```

**Expected:** output contains `linux-modules-nvidia-595-open-7.0.0-28-generic (7.0.0-28.28~24.04.1+3` and the summary `15 upgraded, 3 newly installed, 1 to remove`.

**STOP and re-plan if** the removal line is anything other than `linux-modules-nvidia-595-open-6.17.0-35-generic`, or if any `linux-image-*` appears in the removals, or if `apt-get update` pulled a newer kernel ABI (the versioned package name then changes and everything must be recomputed against the new `uname -r`).

**Step 1.3b — cache the downgrade path before destroying it:**

```bash
sudo apt-get install --download-only --allow-downgrades \
  linux-modules-nvidia-595-open-6.17.0-35-generic=6.17.0-35.35~24.04.1 \
  nvidia-kernel-common-595=595.71.05-0ubuntu0.24.04.1 \
  nvidia-driver-595-open=595.71.05-0ubuntu0.24.04.1 \
  nvidia-dkms-595-open=595.71.05-0ubuntu0.24.04.1 \
  nvidia-kernel-source-595-open=595.71.05-0ubuntu0.24.04.1 \
  nvidia-utils-595=595.71.05-0ubuntu0.24.04.1 \
  nvidia-compute-utils-595=595.71.05-0ubuntu0.24.04.1
ls /var/cache/apt/archives/*595*.deb | wc -l
```

This only writes to the apt cache. It guarantees ROLLBACK B works even if 595.71.05 later ages out of `noble-updates`.

### Step 1.4: Install the kernel-tracking META package (D1)

This is the step that permanently closes the defect — the meta makes every future kernel bump auto-pull a matching Canonical-signed module.

```bash
sudo apt-get install linux-modules-nvidia-595-open-generic-hwe-24.04
dpkg -l linux-modules-nvidia-595-open-7.0.0-28-generic linux-modules-nvidia-595-open-generic-hwe-24.04
dpkg -l nvidia-kernel-common-595
```

**Expected:** both module packages `ii`; `nvidia-kernel-common-595` at `595.84-0ubuntu0.24.04.1`. Userspace upgrades 595.71.05 → 595.84 (15 packages).

**Note:** installing the *versioned* package alone avoids nothing — it carries the identical `nvidia-kernel-common-595 (>= 595.84)` dependency and the same removal.

### Step 1.5: Verify the signed module WINS over the stale DKMS build

This is the step most likely to be skipped and most likely to bite: `/etc/depmod.d` searches `updates` **before** `ubuntu`, so a leftover DKMS module shadows the signed one and `modprobe` fails identically.

```bash
ls -la /lib/modules/7.0.0-28-generic/updates/dkms/ 2>&1   # expect: No such file or directory
modinfo -n nvidia         # MUST be .../kernel/nvidia-595-open/nvidia.ko
modinfo -F signer nvidia  # MUST be: Canonical Ltd. Kernel Module Signing
modinfo -F version nvidia # expect 595.84
```

**If** `updates/dkms/nvidia.ko` still exists or the signer still reads `bbx-MS-02-Ultra Secure Boot Module Signature key`:

```bash
dkms status                                          # get the exact version string
sudo dkms remove nvidia/595.84 -k 7.0.0-28-generic
sudo depmod -a 7.0.0-28-generic
modinfo -n nvidia                                    # re-check
```

### Step 1.6: Load the driver live — no reboot needed

No nvidia module is currently resident, so `modprobe` can succeed immediately with zero boot risk.

```bash
sudo modprobe nvidia
sudo modprobe nvidia_uvm     # required for CUDA (torch, llama.cpp, vLLM all need it)
nvidia-smi
lsmod | grep -i nvidia
sudo dmesg | tail
```

**Expected:** `nvidia-smi` prints the RTX 2000 Ada table with driver 595.84; `lsmod` lists `nvidia`, `nvidia_uvm`, `nvidia_modeset`, `nvidia_drm`; `dmesg` shows **no** `Key was rejected by service`.

**If modprobe fails here, do NOT reboot hoping it resolves** — the boot-time failure mode is identical. Go back to Step 1.5.

`nvidia_drm` may be refused while X/gdm3 holds the console; that is cosmetic and does not block CUDA.

### Step 1.7: Restart dependent services in dependency order

```bash
sudo systemctl restart blackbox-models.service
sudo systemctl reset-failed vllm-reranker.service
sudo systemctl restart vllm-reranker.service
sudo systemctl restart blackbox.service
systemctl is-active blackbox-models.service vllm-reranker.service blackbox.service
systemctl show vllm-reranker.service -p NRestarts     # note value; re-read after 60s
```

**Expected:** three × `active` — `vllm-reranker` must be `active`, **not** `activating`. `NRestarts` must stop increasing (baseline before the fix: 3366+).

Weights are already cached at `/home/bbx/.cache/huggingface/hub/models--Qwen--Qwen3-Reranker-0.6B`, so expect `:8091` to bind within ~60s despite `TimeoutStartSec=600`. `blackbox-models.service` uses `--watch-config`, so do not edit `llama-swap-config.yaml` during this window.

### Step 1.8: (Alternative — D1) MOK enrollment, if the signed-package route is rejected

Fixes the GPU with **zero package changes**, preserving both the 6.17 fallback and the current 595.71.05 stack. **Requires physical keyboard and monitor.**

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der   # prompts for a one-time password, twice
mokutil --list-new                                       # MUST now list the pending key (empty today)
sudo reboot
```

At the next boot the blue MokManager screen appears: **Enroll MOK → Continue → Yes → enter the one-time password → Reboot**. MokManager runs before any network or SSH exists and cannot be driven remotely. If it is not confirmed at that very next boot, the request is **discarded** and must be re-imported.

**After reboot:** `mokutil --test-key /var/lib/shim-signed/mok/MOK.der` reports enrolled; `mokutil --list-enrolled | grep -c 'Subject:'` goes 1 → 2; `sudo modprobe nvidia && nvidia-smi` succeeds.

---

## Task 2: Functional Acceptance — Prove the Chain End to End

All paths taken from repo source, not guessed. Members lazy-load on first request. **The `audio` and `retrieval` groups are `exclusive: true`**, so a TTS call evicts the embedder/reranker and vice versa — sequence these and allow the `ttl:600` idle window between groups, or you will measure a cold swap (~6-12s) and misread it as a regression.

```bash
# 1. Front door
curl -s -o /dev/null -w 'health=%{http_code} t=%{time_total}\n' http://127.0.0.1:9098/health          # 200

# 2. Members
curl -s http://127.0.0.1:9098/v1/models    # embed-qwen3-8b, qwen-tts, rerank-qwen3-8b, speaches

# 3. TTS — the call that 500s today
curl -s -X POST http://127.0.0.1:9098/upstream/qwen-tts/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-tts","input":"Flight recorder GPU restored.","voice":"Vivian","response_format":"wav"}' \
  -o /tmp/gpu-check.wav -w 'http=%{http_code} t=%{time_total}\n'
file /tmp/gpu-check.wav     # expect: RIFF (little-endian) data, WAVE audio

# 4. STT timing — the 10-21s CPU-fallback symptom
time curl -s -X POST http://127.0.0.1:9098/upstream/speaches/v1/audio/transcriptions \
  -F 'model=Systran/faster-whisper-large-v3' -F 'file=@/tmp/gpu-check.wav'

# 5. Rerank (run separately — different exclusive group)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8091/health
sudo journalctl -u blackbox.service --since '5 min ago' | grep RERANK

# 6. Confirm the GPU is actually being used (second shell, during step 3)
nvidia-smi
```

**Expected:** step 3 `http=200` (not 500) and a non-empty RIFF wav; step 4 a JSON `text` field with a **warm second call under ~2s**; step 5 HTTP 200 and the `preflight failed (probe latency ...ms over the 500ms ceiling)` line stops appearing; step 6 non-zero GPU memory used by a python/llama-server process.

`voice` is **required** (422 without it) and `response_format` must be `wav` or `pcm` (400 otherwise).

### Numeric acceptance gates — "fixed" must be measurable

These are the box's own known-good numbers from the GPU era, not aspirations:

| Signal | Known-good (GPU era) | Broken (CPU era) |
|---|---|---|
| `whisper primed in Ns` | 0.6s warm / 1.9-2.3s cold | 10.8-20.2s |
| Transcription duration | 0.55-0.61s median (max 2.28s) | 10.51-21.42s (median 11.68s) |
| `stt_final` stop-to-final | 554-868ms | 11,133ms, then WebSocketDisconnect |
| `stt_final delivery FAILED` | **0** occurrences ever | 10 occurrences, all after Aug 7 17:40 |
| On-box TTS | a new valid `ttsq-*.wav`, 24kHz mono | HTTP 500 |

**Anything in the 10-21s band means you are still on CPU.** The `stt_final delivery FAILED (WebSocketDisconnect)` count is the honest user-impact metric — zero before Aug 7 17:40, ten after — and makes a good regression alarm.

### Do NOT assume the driver fix repairs TTS

The CUDA mechanism is proven (`variant_manager.py` raises when `torch.cuda.is_available()` is False; the member venv confirms `torch.cuda.is_available() == False`). But on-box TTS was **never invoked once** between Jul 25 20:40 and Aug 10 22:47 — a 16-day gap — so there is no observation of it working *or* failing on the new kernel while a driver was present. The Aug 10 500s are proven **concurrent** with the driver loss, not proven to be its only cause. **Re-test TTS independently after the driver is up and budget for a second defect** rather than closing it out as automatic collateral.

---

## Task 3: Rollback Paths

**ROLLBACK A — pre-install or mid-install failure.** Available only *before* Task 1.4 removes the 6.17 module package.

```bash
sudo grub-reboot "Advanced options for Ubuntu>Ubuntu, with Linux 6.17.0-35-generic"   # exact titles from Step 1.1
sudo reboot
# after reboot:
uname -r                     # 6.17.0-35-generic
nvidia-smi                   # prints the GPU table
modinfo -F signer nvidia     # Canonical Ltd. Kernel Module Signing
```

Verified on disk today: `/lib/modules/6.17.0-35-generic/kernel/nvidia-595-open/nvidia.ko`, Canonical-signed, 595.71.05. `grub-reboot` is one-shot and depends on the `next_entry` block; if absent use `grub-set-default` and remember to reset it to `0`.

**ROLLBACK B — post-install failure.** Pinned downgrade of the whole 595 stack to 595.71.05 (uses the debs cached in Step 1.3b), then ROLLBACK A.

```bash
apt-get -s install --allow-downgrades \
  linux-modules-nvidia-595-open-6.17.0-35-generic=6.17.0-35.35~24.04.1 \
  nvidia-kernel-common-595=595.71.05-0ubuntu0.24.04.1 \
  nvidia-driver-595-open=595.71.05-0ubuntu0.24.04.1 \
  nvidia-dkms-595-open=595.71.05-0ubuntu0.24.04.1 \
  nvidia-kernel-source-595-open=595.71.05-0ubuntu0.24.04.1 \
  nvidia-utils-595=595.71.05-0ubuntu0.24.04.1 \
  nvidia-compute-utils-595=595.71.05-0ubuntu0.24.04.1
# inspect the removal list, then re-run without -s
```

---

## Task 4: Stop the Collateral Bleed

### Step 4.1: The reranker crash-loop

Immediate relief is available to the service itself — `installer/templates/sudoers-blackbox-system:135-136` already grants `NOPASSWD: systemctl disable --now vllm-reranker.service` and `stop`. There is **no** `start`/`restart` grant, so this is a one-way door: once disabled, only an operator or the installer re-enables it. Say so out loud rather than silently leaving the reranker off after the GPU is repaired.

Note the unit is the **legacy M11** reranker, superseded by the in-stack `rerank-qwen3-8b` llama-swap member (which works, even on CPU, at 3.7-6.0s). Per the local-model-stack design §5.2 it must never co-run with the retrieval group on the same GPU anyway. **Consider disabling it permanently** rather than restarting it in Step 1.7.

### Step 4.2: The runaway WebKit process (D5) — DONE 2026-08-11 16:36

Killed after a guarded check confirmed the tree was unchanged (`/usr/bin/blackbox-setup` PID 26235, 17h44m elapsed, child WebKit at 514%). All three PIDs gone; load average 5.11 -> 4.70 immediately. Remaining top consumers are `gnome-shell` 36% and `Xorg` 19% — the NOUVEAU software-rendering tax, which Task 1 removes.

```bash
ps -o pid,ppid,etime,%cpu,cmd -p 26235,26254,26364    # confirm the tree is unchanged
```

Tree is `WebKitWebProcess (26364) <- /usr/bin/blackbox-setup (26235) <- gnome-shell (2864)`. It is owned by **nothing** — `grep -rl blackbox-setup /etc/systemd/system/ /etc/xdg/autostart/ ~/.config/autostart/` returns nothing — so no config change can fix it. Correct action: close the BlackBox Setup window at the GNOME desktop, or `kill 26235` (reaps both WebKit children) **after asking Brandon**, since the wizard may hold unsaved onboarding state.

**Expected after:** load average falls toward ~1; `ps -eo %cpu,cmd --sort=-%cpu | head -3` no longer lists WebKitWebProcess.

---

# TRACK B — Repo Code (dev box → git push → MS02 git pull)

> MS02 is on `main` at `db59da01`, identical to the dev box, clean apart from 3 untracked files. Deploy is `git -C "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main" pull`. **Never scp a tracked file** — hand-copies wedge future pulls.
>
> Test runner throughout: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest <paths> -q`

## Task 5: Teach the Hardware Probe to Tell Driver-Dead from Card-Present

**Files:**
- Modify: `Orchestrator/hardware.py:49-60` (`_run`), `:76-101` (`_probe_gpu`), `:122-143` (probe dict at 133-140)
- Test: `Orchestrator/tests/test_hardware.py`

The bug: `_probe_gpu` line 92 falls through to lspci whenever `nvidia-smi` fails for *any* reason, returning `(True, name, None, 'lspci')`. It cannot distinguish "card present, driver dead" from "card present, nvidia-smi simply absent". MS02 therefore reports `gpu=True, vram_mb=None, source='lspci'` — and `derive_tier` line 115 promotes that to `HIGH`.

**Step 1: Write the failing tests.** Use the existing `_fake_run` seam (helper ~line 41) and `LSPCI_NVIDIA` fixture (lines 26-29); the autouse `fresh_probe` fixture (30-38) nulls the cache.

```python
def test_nvidia_smi_fails_but_card_present_reports_driver_dead(monkeypatch):
    _fake_run(monkeypatch, {"nvidia-smi": None, "lspci": LSPCI_NVIDIA})
    hw = hardware.probe()
    assert hw["gpu"] is True and hw["source"] == "lspci"
    assert hw["driver_ok"] is False
    assert "driver" in (hw["gpu_reason"] or "").lower()

def test_nvidia_smi_success_reports_driver_ok(monkeypatch):
    _fake_run(monkeypatch, {"nvidia-smi": "RTX 2000 Ada, 16380"})
    hw = hardware.probe()
    assert hw["driver_ok"] is True and hw["vram_mb"] == 16380
```

**Step 2: Run to verify they fail.** `pytest Orchestrator/tests/test_hardware.py -q` → `KeyError: 'driver_ok'`.

**Step 3: Implement.** Add a `_run_full()` sibling (or widen `_run` to return `(stdout, rc, stderr)`) so the *reason* survives. On the lspci rung set `driver_ok=False` when nvidia-smi was **run and failed**, and `driver_ok=None` when the binary is simply absent (`shutil.which('nvidia-smi') is None`). Add `driver_ok: bool|None` and `gpu_reason: str|None` to the probe dict at 133-140.

**Keep `gpu` / `vram_mb` / `source` byte-identical on the nvidia-smi-success path** so the six existing consumers are untouched.

**Step 4: Run to verify pass**, plus the consumer suites: `pytest Orchestrator/tests/test_hardware.py Orchestrator/tests/test_local_models_status.py Orchestrator/tests/test_embeddings_routes.py -q`

**Step 5: Commit.** `git add Orchestrator/hardware.py Orchestrator/tests/test_hardware.py && git commit -m "feat(hardware): distinguish driver-dead GPU from card-present in the probe"`

**Do NOT change `derive_tier` in this task** (D3) — `rerank_routes.py:87-94` raises HTTP 400 when the requested model's tier list excludes the box's tier, so demoting to LOW would start rejecting GPU reranker selections. Separate decision, separate commit.

---

## Task 6: Add `gpu_ready()` Without Breaking the Anti-Flap Invariant

**Files:**
- Modify: `Orchestrator/local_stack.py` — **new function inserted after `llama_swap_health()` ends at line 148, before `is_healthy()` at line 151**
- Test: `Orchestrator/tests/test_local_stack.py`

`is_healthy()` (lines 151-158) documents a deliberate invariant in its docstring at 152-155: it keys on install + config + **process liveness of llama-swap itself**, "never live per-member VRAM residency (correction [30])". Every consumer treats a `False` as "the whole stack is down". Folding a CUDA check in there would silently disable on-box embeddings and rerank too — which still work on CPU.

So: add an **additive second signal** and leave `is_healthy()` alone.

**Step 1: Write the failing test.** Use the existing `cfg` / `installed_cfg` fixtures (lines 29-37, 109-113).

```python
def test_gpu_ready_false_when_driver_dead_but_is_healthy_unchanged(monkeypatch, installed_cfg):
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "vram_mb": None, "source": "lspci", "driver_ok": False,
        "gpu_reason": "nvidia-smi failed: driver not loaded", "ram_mb": 128000, "tier": "HIGH"})
    monkeypatch.setattr(local_stack, "llama_swap_health", lambda *a, **k: {"reachable": True, "status_code": 200})
    assert local_stack.is_healthy() is True          # invariant preserved
    assert local_stack.gpu_ready()["ok"] is False
    assert "driver" in local_stack.gpu_ready()["reason"].lower()

def test_gpu_ready_fails_open_on_cpu_box(monkeypatch, installed_cfg):
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {"gpu": False, "driver_ok": None, ...})
    assert local_stack.gpu_ready()["ok"] is True     # absent nvidia-smi must not disable anything
```

**Step 2: Run to verify fail.** → `AttributeError: module has no attribute 'gpu_ready'`

**Step 3: Implement.** `def gpu_ready() -> dict:` returning `{'ok': bool, 'reason': str|None}`, sourced from the new `driver_ok`. Import `hardware` lazily inside the function exactly as `resolve_whisper_fit` does at line 420 (cycle-proof). **Fail open:** `ok=True` whenever `driver_ok is not False` — a CPU-only box with no nvidia-smi must not have anything disabled. Never raise.

**Step 4: Run.** `pytest Orchestrator/tests/test_local_stack.py -q`

**Step 5: Commit.** `git commit -m "feat(local-stack): add additive gpu_ready() capability signal"`

**Note for a later cleanup (not this plan):** `should_route_onbox()` at line 161 is **dead code** — `grep` finds only its definition; every real resolver re-implements it inline (`qwen_tts.py:73`, `stt/resolve.py:63`, `rerank.py:524`). A fix that only touched it would change nothing at runtime.

---

## Task 7: Stop Pinning the Flagship Whisper Model onto a CPU

**Files:**
- Modify: `Orchestrator/local_stack.py:412-435` (`resolve_whisper_fit`), specifically the `if not isinstance(vram, int): return None` branch at 427-431
- Test: `Orchestrator/tests/test_local_stack_stt_fit.py`

This is the second lie, and it is why MS02's STT is 10-21s rather than merely "CPU-slow": with `vram_mb=None` the function returns `None`, and `_resolve_stt_id()` line 451 then falls through to the **flagship constants** (`large-v3-turbo` / `large-v3`) instead of the `WHISPER_FIT` CPU floor tier (`small` / `base`, int8, lines 385-391).

**Step 1: Write the failing test** using the `_fresh(monkeypatch, tmp_path)` helper (lines 16-19):

```python
def test_driver_dead_gpu_falls_back_to_cpu_floor_tier(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 2000 Ada (lspci)", "vram_mb": None,
        "ram_mb": 128000, "source": "lspci", "tier": "HIGH", "driver_ok": False})
    assert local_stack.resolve_whisper_fit()["tier"] == "cpu"
    assert local_stack.stt_stream_model() == "Systran/faster-whisper-small"
```

**Step 2: Run to verify fail** — currently returns `None` → flagship constants.

**Step 3: Implement.** Insert between line 423 (`if not hw.get('gpu')`) and line 427:

```python
if hw.get("driver_ok") is False:
    return WHISPER_FIT[-1]      # int8 small/base CPU floor
```

**Step 4: Run.** `pytest Orchestrator/tests/test_local_stack_stt_fit.py -q`

**Critical:** the existing `test_gpu_with_unverifiable_vram_falls_back_to_flagship_constants` (lines 83-92) must stay green. Its fixture dict (87-89) has **no** `driver_ok` key, so `hw.get()` returns `None` and the old path holds — which is exactly why the branch tests `is False` rather than falsiness.

**Step 5: Commit.** `git commit -m "fix(stt): drop to the CPU floor whisper tier when the GPU driver is dead"`

---

## Task 8: Make the Qwen TTS Failure Loud (Server Side)

**Files:**
- Modify: `LocalModels/qwen_tts_server/variant_manager.py` (new exception at ~:32; guard after `import torch` at :297), `LocalModels/qwen_tts_server/app.py` (:28 import, handler after :39, except clauses at :156-158, :164-167, :205-208, :293-295)
- Test: `Orchestrator/tests/test_qwen_tts_variant_manager.py`, `Orchestrator/tests/test_qwen_tts_server.py`

This code runs **on MS02** under its own venv and is deployed by git pull; the dev box never exercises it at runtime. Its `/health` (app.py:47-51) is a zero-weight one-time startup gate that llama-swap never re-probes — which is why the member is marked ready while every synthesis 500s.

**Step 1: Write the failing tests.**

```python
# test_qwen_tts_variant_manager.py — FakeBackend style, events list
def test_no_gpu_error_propagates_and_is_not_swallowed_by_free_before_load():
    backend = FakeBackend(free_vram=None, load_raises=NoGpuError("no cuda"))
    with pytest.raises(NoGpuError):
        VariantManager(backend).ensure_loaded("base")

# test_qwen_tts_server.py — dependency_overrides FakeManager
def test_bare_exception_reaches_the_client_not_internal_server_error(client_with_raising_manager):
    r = client_with_raising_manager.post("/v1/audio/speech", json={...})
    assert r.text != "Internal Server Error"
    assert "No CUDA GPUs are available" in r.text

def test_no_gpu_error_maps_to_503(client_with_nogpu_manager):
    assert client_with_nogpu_manager.post("/v1/audio/speech", json={...}).status_code == 503
```

**Step 2: Run to verify fail.**

**Step 3: Implement.**

- `variant_manager.py`: add `class NoGpuError(RuntimeError)` beside `VramError` (:32) — **not** a subclass of `VramError`, or `app.py:166` maps it to a factually wrong VRAM message. Guard at the top of `TorchQwenBackend.load()`, right after `import torch` (:297) and before :304:

  ```python
  if not torch.cuda.is_available():
      raise NoGpuError(
          "qwen-tts needs a CUDA GPU; torch.cuda.is_available() is False. "
          "The NVIDIA kernel module is not loaded (check nvidia-smi)."
      )
  ```

  **Keep the message under ~180 characters** — `qwen_tts.py:285` truncates at 200 and `tts_routes.py:268` at 400, so a long message loses its actionable half. Remediation first, detail second.

- `app.py`: import `NoGpuError` at :28; add `except NoGpuError as exc: raise HTTPException(503, str(exc))` alongside each existing `except VramError`; and register a catch-all after :39 that logs `log.exception(...)` and returns `JSONResponse(500, {"detail": f"{type(exc).__name__}: {exc}"})`. **Never leak a traceback into the HTTP body.**

**Step 4: Run.** `pytest Orchestrator/tests/test_qwen_tts_server.py Orchestrator/tests/test_qwen_tts_variant_manager.py -q` (baseline for the qwen suites: 92 passing)

**Step 5: Commit.**

**Why this one change fixes the user-facing string with zero orchestrator edits:** `qwen_tts.py:285` already interpolates `{r.text[:200]}` and `tts_routes.py:647` already interpolates `{err}`, so the chat-path 502 becomes `Qwen TTS batch failed (HTTP 503): {"detail":"qwen-tts needs a CUDA GPU..."}` instead of `(HTTP 500): Internal Server Error`.

---

## Task 9: Get the Failure into journald, and Stop Retrying a Hardware Fault

**Files:**
- Modify: `Orchestrator/qwen_tts.py` (before the raises at :284-285 and :288-291), `Orchestrator/routes/tts_routes.py:597-598` (`_qwen_one`), `Orchestrator/tts_queue.py:124-130` (`_is_transient`)
- Test: `Orchestrator/tests/test_tts_queue.py`, `Orchestrator/tests/test_qwen_tts_routes.py`

**Verified:** llama-swap v241 forwards **no** member stdout/stderr to journald and exposes no member-log endpoint (`/logs/upstream`, `/logs/qwen-tts`, `/api/logs` all 404). The orchestrator runs under `blackbox.service`, so **its** stdout is journald — the logging must happen there. Do not add a file-logging handler inside `qwen_tts_server`; it writes to a path nobody tails.

**Step 1: Write the failing tests.**

```python
# test_tts_queue.py — modelled on test_transient_failure_twice_marks_failed_retryable (:199)
def test_capability_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(qwen_tts, "synthesize_batch", _raise(QwenCapabilityUnavailable("no cuda")))
    job = enqueue(...)
    assert job_status(job)["auto_retries"] == 0
    assert job_status(job)["retryable"] is False

# test_qwen_tts_routes.py — reuse the _Resp stub at :232
def test_503_detail_carries_cuda_into_the_502(monkeypatch):
    ...  # _Resp(status=503, text='{"detail":"qwen-tts needs a CUDA GPU..."}')
    assert "CUDA" in exc.value.detail
```

**Step 2: Run to verify fail** — `_is_transient` (:129) returns `True` for any `RuntimeError`, so the CUDA fault is auto-retried today.

**Step 3: Implement.** Raise a typed `QwenCapabilityUnavailable(RuntimeError)` from `qwen_tts.py` on a 503 and exclude that type in `_is_transient` — **do not substring-match `r.text`**. Add `print(f"[QWEN-TTS] synth failed HTTP {r.status_code}: {r.text[:400]}")` before the raise at :285, matching the existing `[TTS QUEUE]` / `[TTS Batch]` prefix convention.

**Critical:** `VramError` is *also* a `RuntimeError` and *also* surfaces as 503 from `app.py:167` — but a VRAM shortage **is** legitimately retryable (another member may unload). Distinguish by type, or transient VRAM contention becomes permanently fatal.

**Step 4: Run.** `pytest Orchestrator/tests/test_tts_queue.py Orchestrator/tests/test_qwen_tts_routes.py -q`

**Step 5: Commit.**

**Acceptance on MS02 after deploy:** `journalctl -u blackbox.service | grep '\[QWEN-TTS\]'` shows the CUDA line after one failed synth, and llama-swap logs a 503 with a body much larger than 21 bytes.

---

## Task 10: Degrade the Catalogs Honestly

**Files:**
- Modify: `Orchestrator/qwen_tts.py:69-75` (`_tts_available`), `Orchestrator/stt/catalog.py:57,64-69`, `Orchestrator/routes/local_models_routes.py:42-54` (`_routing_decision`) and `:57-125`
- Test: `Orchestrator/tests/test_qwen_tts_module.py`, `test_stt_catalog.py`, `test_local_models_status.py`

**TTS and STT get opposite treatment, deliberately:**

- **TTS is dead without CUDA** → remove the group. One edit at `qwen_tts.py:73` adds `and local_stack.gpu_ready()['ok']`, which propagates to `catalog_group()` (:151-171, early-returns `None`), the `/tts/queue` 503 gate (`tts_routes.py:372-374`), and `Portal/voice-lab.js:828-836` which hides the Qwen zone. Also add the gate to the `/tts` single-shot qwen branch (`tts_routes.py:244-276`, currently has **no** availability check) so it 503s honestly instead of returning `{"status":"fallback"}`.

  Use `getattr(local_stack, 'gpu_ready', None)` — `_tts_available` is wrapped in a bare `except: return False` (:74-75), so an `AttributeError` from a half-pulled deploy would silently kill on-box TTS.

- **STT still works on CPU**, just slowly → keep it available but mark it. Add `'degraded': True` plus a blurb to the catalog entry at `stt/catalog.py:64-69`, driven by `gpu_ready()`. Pair with Task 7 so the CPU path actually uses the int8 small/base tier.

- **`/local-models/status`** gets two additive changes: a top-level `"gpu": local_stack.gpu_ready()` block beside `"hardware": hw` (:116), and a third `_routing_decision` outcome — seeded **and** healthy **and not** gpu_ready → `'gpu-unavailable'` with a reason string. Existing frontends treat anything `!= 'on-box'` as inactive, so this is backward-compatible.

**Test-fixture gotchas that will bite:**
- `test_local_models_status.py` locks **exact key sets** (`STATUS_KEYS` / `ROUTING_KEYS`, lines 15-23) as a deliberate binding contract — they must be edited in the same commit. That failure is the intended TDD red step, not a surprise.
- `stt/catalog.py` imports its gates **by name** (lines 2-7), so tests must patch `stt_catalog.<name>`, not `stt.resolve.<name>`.
- **Do not** probe the member over `/upstream/qwen-tts/...` from the catalog render path: the `audio` group is `exclusive: true`, so waking it **unloads the retrieval group** — the exact cross-group swap `qwen_tts.py:17-18` says catalog building must never cause. `gpu_ready()` reads a 60s-cached local probe, which is why it is safe here.

**Open question to settle before shipping (D4 counter-argument):** on a box with **no** cloud TTS keys, hiding the qwen group leaves *zero* voices. Check what Portal/Android actually do with `{"status":"fallback"}` — if they fall back to browser `speechSynthesis`, this is genuinely graceful; if they render silence, this task needs a user-visible notice too.

**Run:** `pytest Orchestrator/tests/test_qwen_tts_module.py Orchestrator/tests/test_stt_catalog.py Orchestrator/tests/test_stt_resolve.py Orchestrator/tests/test_local_models_status.py Orchestrator/tests/test_local_stack_routes.py -q`

**Follow-on (all 3 surfaces, per `feedback_frontend_three_surfaces`):** teach Portal + Android to render `degraded` and the `gpu-unavailable` decision. `Portal/onboarding/steps/transcription.js` fails open on unknown fields, so the backend change is safe to land first.

---

## Task 11: Make the Units Fail Fast Instead of Looping Forever

**Files:**
- Modify: `installer/templates/vllm-reranker.service` (`[Unit]` and `[Service]`, lines 26-33), `installer/templates/blackbox-models.service:26-33`
- Test: `scripts/tests/test_install_perimeter_invariants.sh` (must stay green)

The 3366+ restarts are **not** just a GPU symptom — the unit is mathematically incapable of rate-limiting itself: `RestartSec=15` exceeds the default `StartLimitIntervalUSec=10s`, so at most one restart ever lands inside the window and the limiter can never trip.

**Step 1:** Add to `[Unit]` (systemd ≥229 puts these in `[Unit]`, not `[Service]`): `StartLimitIntervalSec=300`, `StartLimitBurst=3`. The unit then reaches `failed` after ~45s of looping.

**Step 2:** Add a runtime GPU gate mirroring the install-time check already in `blackbox-install-reranker.sh:71-93`:
- `vllm-reranker.service` (GPU-only, template lines 11-12): `ExecStartPre=/usr/bin/nvidia-smi -L` **unprefixed** — a driverless box should fail immediately rather than burn a 600s `TimeoutStartSec`.
- `blackbox-models.service`: prefix with `-` (soft gate) or omit entirely. Its template header (lines 15-19) states "GPU and CPU boxes both run this unit" — **a hard gate here bricks every CPU box.**

**Step 3 — the ordering trap that will waste an afternoon:** `blackbox-install-reranker.sh:71-93` hard-gates on `nvidia-smi` and `exit 0`s when it is absent — MS02's exact state *before* Task 1. **Re-running that installer before the driver is fixed silently does not rewrite the unit.** Sequence: fix the driver (Task 1) → `git pull` on MS02 → operator runs

```bash
sudo bash "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/installer/templates/blackbox-install-reranker.sh" \
     bbx /home/bbx "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main"
sudo systemctl daemon-reload
systemctl show vllm-reranker.service -p StartLimitIntervalUSec -p StartLimitBurst   # expect 300s / 3
```

**Compliance note (Jul 27 perimeter):** editing the repo template is the compliant path — these units are written only by operator-run installers under real sudo, never by `blackbox-write-systemd` and never by the service. A `/etc/systemd/system/vllm-reranker.service.d/override.conf` drop-in is compliant **only** if a human writes it with real sudo, and it is invisible to git, so MS02 would drift from its checkout. Use it only as a stated interim that the template edit supersedes.

**Step 4: Run the perimeter gate — there is no CI, so this is by hand or not at all:**

```bash
for t in "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/scripts/tests/"test_*.sh; do
  echo "== $t"; bash "$t" || echo "FAILED: $t"
done
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_update/ -q
```

All 6 bash tests are green at `db59da01`. `.github/workflows/` does not exist and there are no git hooks — a perimeter regression ships silently if this is skipped.

---

## Task 12: Close the Original Defect Class

The structural fix is Task 1.4 (the meta package). Two things remain:

**12a — A post-boot capability sweep.** Nothing today asserts "if this box has nvidia module packages installed, `nvidia-smi -L` must succeed." That check is why the outage ran undetected Aug 7 → Aug 11. Surface it the way `[RERANK] preflight failed` already logs, and include `ss -tlnp | grep 9092` for the telephony subprocess (Task 15).

**12b — Do NOT disable unattended-upgrades.** That trades a driver outage for an unpatched security posture. The meta package is the correct lever. For the record, MS02 has `Automatic-Reboot` and `Remove-Unused-Kernel-Packages` both commented out (defaults) — which is precisely why it ran `6.17.0-35` for three weeks after `7.0.0-28` landed, and only broke on the forced reboot.

Also: `apt list --upgradable | wc -l` is **99** on MS02. Do not run a broad `apt upgrade` alongside the nvidia work, or any regression becomes impossible to attribute.

---

# TRACK C — Dev-Box Follow-Ups (independent of MS02)

These are real, confirmed, and unrelated to the GPU. Each is separable.

## Task 13: The gemma-box Streaming Regression

**Symptom:** `/stt/catalog` resolves default `local` but reports `streaming: null`, so live-mic streaming silently cannot use gemma-box even though its `/v1/realtime` is reachable (it worked on Jul 24; the Aug 3 re-validation cleared it).

**13a — Immediate unblock (no secrets typed, no code change):**

```bash
curl -sS -X POST http://localhost:9091/onboarding/validate \
  -H 'Content-Type: application/json' \
  -d '{"provider":"custom","credentials":{"server_id":"srv-4d70fe99"}}'
curl -s http://localhost:9091/stt/catalog | python3 -m json.tool
```

`_resolve_stored_creds` pulls base_url + api_key from the registry. **Expected:** `local` provider shows `"streaming": "realtime"`. Note this **writes** `credentials/custom_models.json` and resets `model_context` to `{}` by design (auto-learn re-derives it).

**13b — Root cause:** `Orchestrator/onboarding/validators.py:171-180` has a bare `except Exception: return False`, so a transient probe failure is persisted as an **authoritative** capability `false`. Make `_has()` tri-state — `True` on a definite non-404, `False` **only** on a definite 404, `None` on any exception — then at `onboarding_routes.py:535` replace the blanket `merged_audio.update(probed_audio)` with a None-filtering merge so an unreachable box leaves stored capabilities untouched.

`custom_servers._validate_field_types()` (:182-190) rejects non-bools, so `None` must be filtered **before** `update_server` or the stamp raises and is swallowed by the warning at :544-548.

Do **not** add a separate realtime probe — the `status_code != 404` test is already correct for a WebSocket endpoint (measured: `/v1/realtime` returns 307).

**Tests:** `test_local_audio.py` (lines 17-18, 32 already lock this contract), `test_local_stt.py`, `test_custom_validator.py` (natural home for the "probe exception must not persist false" regression).

## Task 14: Stale Download Bookkeeping

`Manifest/local_models/downloads.json` claims 5 members are downloaded; only `silero_vad_v5.onnx` exists on disk.

- **Operator remedy now:** `rm "Manifest/local_models/downloads.json"` — gitignored, pure bookkeeping, rewritten by `record_download_state()` on the next download, and silero-vad self-heals via `_member_gguf_present`.
- **Code fix:** where a presence check is possible (`kind=='file'` or `_SELFCONVERT_GGUF`), make **absence authoritative** — return `False` without consulting the state file. Keep the state-file fallback only for `hf_snapshot` members that have no single-file check.

Deleting the file without the code fix just lets the drift recur. Tests monkeypatch `model_downloaded` (see its own docstring at :262-263), so assert against `_member_gguf_present` in the new tests.

## Task 15: Google Workspace OAuth + Telephony Diagnostics

**15a — Re-auth (operator, browser-driven):** open `http://localhost:9091/auth/gmail/authorize?operator=Brandon`, complete sign-in, repeat for `operator=Anna`. The flow already sets `prompt=consent` + `access_type=offline`, so a new refresh token is written to `Manifest/gmail_tokens/<Operator>.json`. **Acceptance:** `curl -s http://localhost:9091/gmail/status/Brandon` returns `connected:true` **and a non-empty `email`** — `connected:true` with a blank email is the invalid_grant signature. Do not delete token files first.

**15b — Surface it:** in `gmail/service.py:93-96`, detect `invalid_grant` and mark the token as needing re-auth; have `gmail_status` return `connected:false` plus a `reauth_url` so the Portal renders a Reconnect button instead of a green badge with a blank email.

**15c — Recover the [AUDIO-SUB] traceback.** The telephony AudioSocket child has died at every service start since Jul 25, and the journal shows only the first line because `_log_forwarder` (`asterisk/audio_ipc.py:351-362`) stops draining when `_running` flips false. Change the guard at :354 to drain until EOF. Capture the full traceback out-of-band first (safe — the crash precedes the 9092 bind, and nothing listens there):

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python3 -c 'import socket, subprocess, sys
parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
p = subprocess.run(["Orchestrator/venv/bin/python3", "Orchestrator/asterisk/audio_subprocess.py", str(child.fileno())], pass_fds=(child.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
sys.stdout.write(p.stdout.decode(errors="replace"))
print("exit code:", p.returncode)'
```

**15d — Stop reporting a dead subprocess as started.** `audio_ipc.py:149-150` returns `True` immediately; `startup.py:755-760` then logs INFO "Audio subprocess started". Confirm liveness (short, non-fatal poll) and log WARNING when it is not.

## Task 16: The Dev-Box Perimeter Drift (rc=2)

`startup.py:469` sends the correct **1-arg** form to `/usr/local/sbin/blackbox-write-systemd`, but the dev box still carries the **pre-M0 2-arg** helper from May 24 and has **no** `/etc/blackbox/templates/` at all. Drift-heal has therefore been failing `rc=2` silently since Jul 29.

**There is no code fix** — `startup.py` drift-heals only `sudoers-system` and `apt-install-helper`, never the write-systemd helper, so it cannot heal itself, and even a correct helper would `exit 6` with no store. The only remedy is the operator step:

```bash
sudo bash "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Scripts/install.sh"
```

**Acceptance:** `/etc/blackbox/templates/` contains 4 kinds (modes 440/644/644/644); `grep -c SOURCE_FILE /usr/local/sbin/blackbox-write-systemd` returns 0; the next restart logs `[sudoers] ... already matches the trusted store` instead of `drift-heal helper failed rc=2`.

**Before running it:** `install.sh` Steps 2d/2e/2f invoke the reranker + localstack installers. The reranker skips cleanly on this GPU-less box, but **localstack will write and start `blackbox-models.service`** — the dev box has no localstack today, so confirm that is wanted. `install.sh` also fires a guarded `sudo systemctl restart blackbox.service`.

**The one genuine code change here:** add an `elif result.returncode == 2:` branch at `startup.py:482`, mirroring the existing `rc==6` branch (475-481), that names the stale 2-arg helper and `sudo bash install.sh` as the remedy. This does **not** restore drift-heal — it converts a two-week-silent generic warning into an actionable one. Do not present it as a fix.

---

# Execution Order

```
Track A (operator, MS02)          Track B (code, dev box)         Track C (dev box)
─────────────────────────         ───────────────────────         ─────────────────
Task 1  restore driver     ─┐     Task 5  hardware probe          Task 13 streaming
Task 2  acceptance          │     Task 6  gpu_ready()             Task 14 downloads
Task 4  stop the bleed      │     Task 7  whisper CPU tier        Task 15 oauth/audio
                            │     Task 8  TTS loud failure        Task 16 perimeter
                            │     Task 9  journald + no-retry
                            └───→ Task 10 honest catalogs   (needs 5-7)
                                  Task 11 unit rate limits   (needs Task 1 done first)
                                  Task 12 defect class

Track D (silent substitution)     Standalone
─────────────────────────────     ──────────
D-1 resolver override logging     Task 18 local TTS collapse (open, needs gemma-box logs)
D-2 move the guard up             Task 19 journald retention + preserve the 37 WAVs
D-3 catalog vs mic mismatch
D-4 tri-state probe
D-5 unprefixed voice -> cloud
```

**Priority note:** Track A is what unblocks Brandon today. But **Track D is what stops the next outage from being invisible** — it is the reason four days passed before anyone noticed, and D-1 is fixable without touching MS02 at all.

**Track A is the only thing standing between Brandon and working TTS/STT — do it first.** Track B can proceed in parallel on the dev box, but Task 11's *deployment* and Task 10's *verification on MS02* both require the driver to be alive.

**Full-suite gate before any push:**

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -q          # baseline 4146 passing
for t in scripts/tests/test_*.sh; do bash "$t" || echo "FAILED: $t"; done
```

---

# EXECUTION LOG — 2026-08-11/12

**Gates:** Python **4151 → 4202** passed / 0 failed. Android **2075 → 2093** passed / 0 failed. Nothing committed; everything sits in the working tree with the pre-existing v32/v33/v34 Android work, per Brandon's "it all commits together".

| Item | State |
|---|---|
| **Track A** — MS02 GPU restore | ✅ **DONE + boot-verified.** Driver 595.84 loads unassisted; TTS 200; STT warm 0.66-0.80s; GRUB menu restored |
| **Track D** — silent substitution family (D-1…D-5) | ✅ **DONE, committed f360af4e (2026-10-11).** Resolver announces overrides, WS tells the client, catalog reports the streaming resolution, tri-state probe, loud TTS failures; bare local voices also route local on /tts/batch |
| **Rerank client** — swap resilience | ✅ **DONE, committed de14392d (2026-10-11).** Read deadline later bounded to 90s with its own `[rerank] localstack_timeout_s` knob (600s froze the event loop on a wedged member); fallthrough counted every time, including retrieval's decode/stage failures; status reports the scoring URL |
| **Reranker batch-size defect** | ✅ **DONE + verified on MS02.** Production-size rerank (40×4096 chars) returns 200 in 21.3s; real path scores `[0.8539, 0.0, 0.0014]` |
| **vllm-reranker** | ✅ stopped **and disabled** |
| **Waveform M0** — measurement floor | ✅ **DONE.** +18 tests; 2× read defect pinned (mutation-verified) |
| Waveform M1-M8 | ✅ implemented 2026-08-12 (SNAP-20260812-9042), uncommitted pending the Fold walk of a rebuilt APK |
| Preflight → real-traffic evidence | ✅ **DONE, committed de14392d** |
| **Track B** — Tasks 5-12a (audit rank 69, M4 unit U33, 2026-10-23) | ✅ **DONE in code**: probe `driver_ok`/`gpu_reason`; `blackbox-gpu-preflight.sh` as the models unit's ExecStartPre + StartLimit on both model units; `gpu_ready()` + per-capability breaker gating the TTS/STT gates; CPU-floor whisper on a dead driver; coded qwen-tts 503s (`no_gpu`/`vram`) and a no-retry `QwenCapabilityUnavailable`; honest `/local-models/status` (`gpu`, `reason`, `breaker`, `gpu-unavailable`/`degraded`). Plus one llama-swap config renderer re-run at every boot, the reranker GGUF build script with pins, the `/rerank/select` built-member gate, and pinned qwen-tts venv. **Operator step:** re-run `blackbox-install-localstack.sh` on each box to install the new unit. Open: Portal/Android rendering of the new reasons; the :9092 telephony check of 12a |
| Task 18 / Task 19 / chat_routes defects | not started (see docs/plans/2026-10-09-unfinished-work-audit.md W04, W37) |

## The VRAM arbitration fault (new, resolved)

Embeddings broke with `cudaMalloc failed` because **three runtimes held GPU memory independently** and llama-swap can only evict what it started: vLLM 3.3 GB + Ollama's `qwen3-embedding:8b` pinned `keep_alive: -1` at 6.4-7.2 GB. The llama-swap embedder needs **13.9 GB resident** (not the 8 GB of weights — `-c/-b/-ub 8192` allocates large compute buffers), so it could not fit.

**Brandon's design rule, recorded:** llama-swap must be the **sole GPU arbiter**; jobs queue behind a load; the server handles swapping so there is always enough VRAM. Consequence: *every* client of `:9098` must adopt llama-swap's timing contract — long READ, short CONNECT.

**Arbitration then proven** with a full cycle (embed → rerank → TTS → STT → embed): zero OOM, zero non-200, VRAM sampled down to **317 MiB (a bare card)** between every transition. Embed cold 2.46s / warm 0.055s; intra-group swap 3.18s; cross-group 2.88-8.52s. The config's "~6-12s per-search swap" overstates the swap *primitive* (2.5-3.2s); 6-12s matches a whole search cycle.

## Gotchas earned the hard way

- **A manual `modprobe` proves nothing about durability.** The 18:04/18:06 boots *preceded* the 19:16 install; only the 19:30 reboot proved the boot path.
- **An X server that loses the boot race never re-probes for a late GPU.** It sat on a dummy `None-1 disconnected` screen for 80 minutes while the machine was perfectly healthy. Only a reboot fixes the display *and* proves the boot path.
- **`libllama-server-impl.so: cannot open shared object file` is a false lead.** The binary has no RPATH; the unit sets `LD_LIBRARY_PATH`. Reproduce member commands *with* that env var or you will chase a phantom.
- **`cmd: |` is a YAML literal block.** Lines indented less than its first line terminate the block. A `sed` replacement whose leading spaces are lost to copy/paste silently produces invalid YAML — and `--watch-config` validates before swapping, so llama-swap keeps serving on the last-good copy and the breakage only appears at the next reboot. **Prefer a substring `s///` that never rewrites the indentation.**
- **The AGC correction (M0, mutation-verified):** the frame-counted release does *not* make the time constant vary with chunk size. It shifts the **equilibrium** — per-chunk decay `d` opposes time-based rise `s`, parking the reference at `s*v/(1-d+s*d)`. M1 tests equilibrium invariance, not time-constant invariance.

---

# TRACK D — The Silent Substitution Family

> **Added 2026-08-11 after log archaeology.** Brandon's instinct ("I'm not sure on-box ever really worked — it always fell back to OpenAI") is **false on the facts and right about the mechanism.** This track is the mechanism. It is independent of the GPU outage and is arguably the more valuable half of this plan.

## What the logs actually prove

**On-box STT worked, conclusively.** 312 transcriptions through MS02's llama-swap across the whole retained journal, **312 × HTTP 200, zero failures**. GPU era (before Aug 7 17:25): n=266, median **0.61s**, max 2.28s. CPU era (after): n=46, median **11.68s**, min 10.51s. The two distributions have **zero overlap** — a clean step function at the exact moment the driver died. 101 `stt_final delivered` lines carried real transcript text; 44 `whisper primed in Ns` lines show 0.6s warm primes throughout the GPU era. The path was mature enough to have its own hallucination filter firing.

**On-box TTS worked too — but only disk proves it.** 37 `ttsq-*.wav` files on MS02 (`Portal/uploads/`), dated Jul 23-25, all valid 24kHz mono 16-bit, totalling **3.4 hours of synthesized speech** (largest single job 510s / 106MB). The `ttsq-` prefix is generated **only** by `tts_queue.py:337`, and that worker calls only `qwen_tts.synthesize_batch` and raises on failure — no cloud provider can produce one. Git bounds it: commits `511e3593` → `a110d544` → `6eac674e` (native GPU batching, 112.7s → 20.5s) → `f04ee178`, Jul 21-23, with a measured warm RTF of 0.178 on the RTX 2000 Ada.

**The entire TTS success era predates the journal horizon.** MS02's earliest retained log line is **Jul 26 19:16**; the dev box's is **Jul 24 18:25**. Both boxes run a completely default `journald.conf` (a bare `[Journal]` header, no `Storage=`, no `SystemMaxUse=`). Judged on logs alone the verdict would have been a confident, wrong *"on-box TTS never worked."* Those 37 WAV files are now the **only surviving proof**.

## D-1: The STT resolver silently discards an explicit provider pick

**`Orchestrator/stt/resolve.py:109-114`** — measured live on the dev box **right now**:

```python
avail = {"openai": openai_ok, "google": google_ok, "elevenlabs": ..., "onbox": ..., "local": ...}
if provided in avail and avail[provided]:
    return provided                       # <- 'local' IS a key, but its value is False
live = [p for p, ok in avail.items() if ok]
return live[0] if live else None          # <- dict insertion order puts "openai" FIRST
```

`.env` says `STT_PROVIDER=local`. `resolve_stt_provider('', local_ok=local_streaming_stt_available())` returns **`'openai'`**. **No log line records the substitution** — the only output is `[STT/WS] start provider=openai`, which is indistinguishable from a deliberate choice.

**Proven to have fired on MS02** at 2026-08-10 21:14:24, 21:15:18 and 21:21:50: `.env STT_PROVIDER=onbox`, yet all three connected to `wss://api.openai.com/v1/realtime` (`model=gpt-realtime-whisper`) with finals at **373 / 499 / 524 ms**. The first two land 2-3 minutes after boot — exactly the window where the local stack has not come up yet.

**The perverse part:** the silent fallback target is OpenAI's frontier realtime model, which is *genuinely excellent*. It produces a **better** felt experience than healthy on-box CPU-era STT (373ms vs 11,680ms). **Any monitoring based on perceived quality will systematically miss on-box failure.** Instrument on *provider-actually-used* and *prime-time*, never on how it feels.

**Fix:** an explicitly pinned provider must never be silently replaced. Log the override at minimum (`STT provider 'local' requested but unavailable (streaming=false); falling back to openai`); better, surface it as a degraded-state signal to the client.

## D-2: The anti-fallback guard exists but is unreachable

`Orchestrator/routes/stt_ws_routes.py:186-188` carries the comment **"No silent OpenAI fallback: an unknown/local provider must fail loudly"** and raises accordingly — and `:538` / `:566` repeat the invariant ("NEVER a silent provider switch (§5.3 / D10)"). But the guard sits in **dispatch**, downstream of the resolver at `:131-132` which has *already* rewritten `local` → `openai`. By the time dispatch runs, `provider == 'openai'` and the guard can never fire.

The invariant was enforced one layer too late. Move it up to the resolver, or have the resolver return `(provider, was_substituted)`.

## D-3: The catalog reports a different provider than the mic will use

`GET /stt/catalog` reports `resolved: 'local'` while the realtime path resolves `openai`. Both call `resolve_stt_provider`, but with **different `local_ok` arguments** — the catalog passes `local_stt_available()` (file transcription, True) and the mic passes `local_streaming_stt_available()` (streaming, False). The UI therefore tells the user "local is active" while the microphone is on OpenAI.

**Fix:** report the realtime resolution, or report both explicitly. Without this, Portal and Android keep showing the wrong provider even after Task 13 lands.

## D-4: A transient probe failure is persisted as authoritative fact

`Orchestrator/onboarding/validators.py:171-180`:

```python
def _has(path):
    try:
        r = httpx.get(f"{base_url}{path}", headers=_hdr, timeout=5.0, follow_redirects=False)
        return r.status_code != 404
    except Exception:
        return False          # <- a 5s timeout is indistinguishable from a real 404
```

The in-code comment even says *"Best-effort + fail-soft: a probe failure just means not detected"* — but `onboarding_routes.py:533-535` then does `merged_audio.update(probed_audio)`, treating it as authoritative. Note the **asymmetry in the very same block**: `model_modalities` merges with *existing winning*, and `stt_model`/`tts_model` are explicitly preserved (`:536-539`). Only the capability booleans get clobbered.

**Exact sequence on Aug 3**, from the journal: `15:17:29 onboarding /save keys=[STT_PROVIDER]` (Brandon picks local) → `15:17:59 validation recorded` → `15:18:10.097 custom-servers patch fields=[audio]` (matches `custom_models.json` mtime to the sub-second) → `15:18:13` wizard-triggered restart → `15:21:34 [STT/WS] start provider=openai`. **He asked for local and silently got OpenAI four minutes later.** That `fields=[audio]` patch is the *only* one in the entire retained journal.

**gemma-box is healthy right now** — `/realtime` → HTTP 307, `/audio/transcriptions` → 405, `/audio/speech` → 405, `/models` → 200. The persisted `streaming=false` is **stale and wrong**; re-running the identical probe today sets it back to true.

**Fix:** make `_has()` tri-state (True / False / **None** on exception) and have the merge skip `None`. One-file consistency fix that brings the booleans in line with how the same block already treats models.

## D-5: An unprefixed local voice silently routes to the cloud (found by test, 2026-08-11)

Measured against the live orchestrator:

| Request | Result |
|---|---|
| `voice: "am_onyx"` | **HTTP 200** body `{"status":"fallback","api_status":400}` — routed to **OpenAI**, which rejected it (`Input should be 'nova','shimmer','echo','onyx',...`) |
| `voice: "local:am_onyx"` | `{"status":"success"}`, Kokoro, 6860 bytes, **2.17s** |
| `voice: "local:af_heart"` | `{"status":"success"}`, Kokoro, 5132 bytes, **0.70s** |

An unprefixed local voice id is not recognised as local, falls through to the cloud provider, gets a 400, and the orchestrator **swallows it into an HTTP 200 `"fallback"` envelope**. Same family as D-1: a real failure rendered as a success-shaped response.

**This refutes the voice-id hypothesis for the 195 local-TTS timeouts** — see Task 18. `am_onyx` is a perfectly valid Kokoro voice, and the batch path *did* resolve it to local (the logs read `[TTS Batch] local | voice=am_onyx`). Those failures were genuine 120s read timeouts against gemma-box, not voice-id rejections. Do not conflate them.

---

# Task 18: The Dev-Box Local TTS Collapse (separate defect, still open)

**This is not caused by MS02 and not caused by the streaming flag** — `audio.tts` was never cleared and is still `true`.

Per-day local TTS batch success on the dev box: Jul 24 **15/16** → Jul 25 **23/45** → Jul 26 **13/13 (perfect)** → Jul 27 **3/21** → **Jul 28 through Aug 3: 0/213**. Every single one of the 195 errors is `HTTPConnectionPool(host='192.168.1.50', port=8080): Read timed out` (the timeout was raised to 120.0s on Jul 27 12:17 in response). The failure mode *migrated*: Jul 24 one error at chunk 9/13 → Jul 25 mixed partials → Jul 28 onward **100% "ERROR on chunk 0/N"**, i.e. the very first chunk hangs and the request is never served at all.

Yet **local TTS works today**: 1.69s on Aug 11, and 0.70-2.17s in the tests above. So gemma-box recovered at some point, or the trigger is load//content-dependent.

**Suggested first moves:** get gemma-box's own logs for Jul 26-28 (SSH is closed from this box — that itself is worth fixing), test a *long multi-chunk* batch rather than a one-liner (all the passing tests were short), and check whether Kokoro serializes badly under concurrent chunk submission.

**Why it matters for the narrative:** the Jul 27 local-TTS collapse is almost certainly what drove Brandon to cloud STT at 15:59:13 that day (113 seconds after his last local session) and to gemini-flash TTS on Aug 3. Two independent regressions, six days apart, got remembered as one story — and neither was the MS02 GPU.

---

# Task 19: Stop Destroying the Evidence

Both boxes run journald on pure defaults, and it has already vacuumed away the entire on-box TTS success record (Jul 23-25 on MS02) within about three days of it happening. That nearly produced a confidently wrong verdict in this very investigation.

- Set an explicit `Storage=persistent` and a generous `SystemMaxUse=` in `/etc/systemd/journald.conf` on **both** MS02 and the dev box (currently 2.9G and 3.9G on disk respectively, both size-capped by default heuristics).
- **Preserve the 37 `ttsq-*.wav` artifacts** on MS02 — archive or checksum them. They are the only surviving proof that on-box TTS ever worked. `Portal/uploads/` root is not swept today (files from 2026-05-17 persist), but that is luck, not policy. **Do not run `POST /upload/cleanup` against that directory.**

---

# Task 17: Waveform Upgrade Pass (Android)

## 17A: Waveform Upgrade Pass — Brandon's Feedback (captured 2026-08-11)

**Framing, in his words:** *"We just set these up, so we never got to actually fine tune them. That's what we need — an upgrade pass across all of them."* This is a quality/tuning pass over a shipped catalogue, not a rebuild.

**State of the code:** Android waveform **v32/v33/v34 are still UNCOMMITTED** (45 dirty paths on the dev box). v34 `1.11.0-waveform-styles` added the `WaveformRegistry` catalogue (10 styles), `VoiceVisualizer` at all 3 call sites via `LocalWaveformStyle`, `WaveformFeed` with per-speaker 50Hz-decimated 3s history, and the Settings live preview driven by `WaveformDemoDriver`. M4 (on-device catalogue walk) was the open item — **this feedback IS M4**. Existing plans: `docs/plans/2026-08-02-waveform-style-modules.md`, `docs/plans/2026-08-01-aurora-agsl-shader-renderer.md`.

### THE CROSS-CUTTING REQUIREMENT: full spectrum

> *"We need the full spectrum equalizer here. Full spectrum. That's what we want to measure for all of these inputs — full spectrum."*

Every single per-style note reduces to the same two asks: **more spectral resolution** and **more reactivity to the actual voice** (both mic input and TTS output). Treat this as one shared analyser upgrade feeding all ten renderers, not ten separate fixes.

### THE KEY DIAGNOSTIC CLUE

> *"In the waveform previewer it looks a lot better. It looks pretty much almost like what we were expecting there... the waveform preview looks much better than the actual what it looks like in practice."*

**Said twice, unprompted, about two different styles.** The Settings preview runs off `WaveformDemoDriver` (a deterministic synthetic conversation loop); live rendering runs off `WaveformFeed` fed by the real analyser. Same renderers, different data source, visibly different quality — **so the deficit is in the live analyser/feed path, not in the renderers.** Chase this first; it likely explains most of the ten complaints at once.

### Per-style notes (verbatim intent)

| Style | Brandon's note | Implied work |
|---|---|---|
| **Aurora** | "Pretty good." Needs more equalizer/spectrum reactivity; **mountains can be sharper** | Crest/peak definition + band reactivity |
| **Classic lines** | "Pretty simple, like we used to have it" — but preview >> practice | Mostly the analyser fix |
| **Ribbon** | Should move **fluently**; "much more fluid motion." The **glowing effect is really good — can get a lot more out of it** | Motion interpolation + bloom driven by spectrum |
| **Mirrored pulse** | "Interesting." Needs **more spectrum movement, like an actual waveform on an old stereo system** | VU/stereo-meter character |
| **Segmented EQ** | **Bar width too wide**; more reactivity; **full-spectrum equalizer** | More, narrower bins mapped to real FFT bands |
| **Soft blob** | "A bit bland" — more movement, more spectrum movement | Deform by band energy, not just level |
| **Sharp reactive line** | "Just seems to do one simple thing and that's kind of it" — more reactive, more spectrum | Likely consuming level only, ignoring bands |
| **Orb** | **"I like this one"** — can get more from the spectrum | Radial band mapping |
| **Particle wave** | *(not yet covered)* | — |
| **Cinematic** | *(not yet covered)* | — |

### Engineering leads already in hand

- **Band count is the prime suspect.** The v32 design is *one band per ribbon* with 4 colour-coded bands (red/blue/green/amber). Four bands is plenty for aurora ribbons and **starves** a segmented EQ, an orb, or anything asking for "full spectrum." Likely shape: a real FFT producing 24-64 log-spaced bins, with the existing 4-band aggregation derived from it so aurora stays pixel-identical by default.
- **The v33 AGC tuning is explicitly a reactivity-vs-contrast trade, and it was tuned toward contrast.** `STEADY_RISE_TAU_SEC=6.0` is a very slow rise; with `HEADROOM=2.5`, `GAMMA=0.82` and `softKnee(KNEE=0.80)` the result is deliberately smooth. The v33 notes record the documented escape hatch: *"if Brandon says whispers stopped reading, flip `STEADY_RISE_TAU_SEC`→~0.5 + `HEADROOM`→~1.3."* **He is now effectively saying exactly that.** A ~1000-config sweep proved one AGC reference cannot serve both goals — so the real answer is probably **two references** (fast for visual reactivity, slow for sustained contrast) rather than re-picking a single compromise.
- **Mic-in and TTS-out are different paths** with different characteristics (the offline/voice-file path peak-normalises, which previously amplified HIGH-band noise to 0.443 mean during silence and needed `GATE 0.13` + `NOISE_REF` denominators). Both must be tuned; do not assume a fix on one transfers.
- **Invariants to preserve:** aurora must stay the pixel-identical default unless deliberately changed; keep the registry audibility invariant test (a style reading audibility from `feed.level()` instead of the passed voices **park-deadlocks**, which bit Cinematic); persisted style ids are forever.

### Open

- ~~Particle wave and cinematic notes still to come.~~ **RESOLVED 2026-08-11:** *"exactly the same thing we've been talking about — it's mainly the data that we're getting. We can do more with it."* Both are covered by the Tier 1 analyser work; no style-specific notes needed.
- ~~Decide whether v32/v33/v34 get committed first.~~ **RESOLVED 2026-08-11:** *"The timing and enhancements go along with those commits."* The tuning lands **on top** of the uncommitted v32/v33/v34 work and **it all commits together** — there is no separate pre-commit of the catalogue. Practical consequence: the working tree stays dirty for the duration of this pass, so each milestone must leave `./gradlew :app:testDebugUnitTest --offline` green rather than relying on a clean revert point.
- ~~STILL OPEN: the Gemini recorder's upload format.~~ **DELEGATED 2026-08-11:** *"You could do whatever works best for the BlackBox production. You can look at their docs to figure out what size requirements and format sizes they need."* Resolution recorded in **17F**; M6 (the deliberate aurora default change) is **APPROVED to proceed** — *"M6 is good to go."*

## 17B: Groundwork Findings — Why It Looks Like That (measured 2026-08-11)

### THE HEADLINE: there is no spectrum to show

`AuroraAnalyser` does **not** compute a spectrum. It runs **8 single-bin Goertzel probes** at fixed frequencies — 120/220, 500/800, 1600/2600, 5000/6800 Hz (`AuroraAnalyser.kt:215-220`) — and averages each pair into **4 numbers** (`:209 BANDS = 4`, `:151-157`). Total passband is roughly **100-230 Hz out of an 8 kHz range: under 2% of the audible band.** Which needles a voice happens to land on depends on the speaker's pitch — a pitch lottery, papered over by per-band `GAINS` at `:223`.

**"Full spectrum" is therefore not a tuning request. It is a missing capability.** Everything else below is downstream of this.

### The preview mystery, solved arithmetically

The preview **never touches the analyser**. `WaveformStylePreview.kt:45-67` calls `driver.voicesAt(timeSec)` straight into `VoiceVisualizer` — no analyser, no AGC, no PCM. Same renderers, same feed, same frame loop. **Only the numbers differ**, which means every visual difference you see is a data difference, not a drawing difference.

Measured, from replicating both code paths:

| | Preview (demo driver) | Live (mic, 80ms chunks) |
|---|---|---|
| Band peak-to-typical swing | **17×** (p50 0.035, p90 0.59) | **1.14×** (p50 0.873, p90 0.995) |
| Band spread (loudest ÷ quietest) | 1.42× | **18.6×** |
| Bands 1/2/3 typical | 0.194 / 0.170 / 0.137 | **0.163 / 0.075 / 0.037** |
| Update rate | every frame (60-120 Hz) | **6-31 Hz, held flat between** |
| Silence between syllables | true zero, 38% of each syllable | never reaches zero |
| Band correlation | decorrelated (phase-staggered) | all ride the same envelope |

**Read that as pictures:** the preview draws four comparable sheets spiking and snapping back to silence 4.4 times a second, out of phase with each other. Live draws **one saturated sheet pinned near the ceiling plus three nearly flat ones**, updated a handful of times a second and held frozen in between. That is the entire "preview looks better than practice" gap.

### Five independent causes, each separately fixable

1. **No spectrum** — 8 needles, 4 bands (above).
2. **Update rate is whatever the audio buffer happens to be.** `analyze()` runs Goertzel over the **entire chunk** (`:108`, `:151-157`), so one band value = one boxcar average of the whole read. Worse, both mic paths compute `getMinBufferSize(...) * 2` **bytes** and then allocate `ShortArray(bufferSize / 2)` — which holds `getMinBufferSize` **samples**, i.e. **twice** the intended duration (`SttStreamClient.kt:341-343, 382`; `VoiceScreen.kt:653-655, 701`). There is **no interpolation anywhere** in the pipeline, so the renderer draws a staircase at 60-120 fps.
3. **Double compression.** `HEADROOM = 2.5` parks typical speech at ~0.47 *by the constant's own comment* (`:273`), `GAMMA = 0.82` lifts the floor (`:282`), and then the **renderer applies `sqrt` again** (`AuroraWaveform.kt:1009`, inherited, "Straight from the original"). Effective exponent: **0.41 live, 0.275 on the player bar.**
4. **The AGC is slower than speech, and its release is frame-counted.** `STEADY_RISE_TAU_SEC = 6.0` (`:261`). The release at `:183` is `ref * STEADY_DECAY` with **no `dtSec`** — so its real time constant swings between roughly **6 and 40 seconds** depending on buffer size. The rise at `:149` is correctly time-based; the release is the last frame-counted constant in the file.
5. **The feed's only history is one scalar.** `WaveformFeed` keeps a single `EnvelopeRing` per **speaker** (`:120-121`), and `levelOf` is a weighted **max** (`:218, 226-234`). With live bands at [0.87, 0.16, 0.075, 0.037] the max **is band 0 on essentially every frame** — so every scrolling style is drawing the 120/220 Hz probe pair and nothing else.

### Your per-style notes, confirmed against code

| Style | Verdict | Evidence |
|---|---|---|
| **Sharp line** — "just does one simple thing" | **CONFIRMED** | Reads one scalar; **not a single `feed.band()` call in all 443 lines** (`SharpLine.kt:127`) |
| **Segmented EQ** — bars too wide, want full spectrum | **CONFIRMED, and it's fake** | `SEGMENT_COLUMNS = 16` hardcoded, `pitch = w/16` → ~14.9 dp bars (`:99, 427-428`). `fineBin()` **lerps 4 values into 16 columns plus a sine jitter**, self-documented at `:212-215` as *"NOT a real N-point measurement"*. On live data it degenerates into a monotonic descending ramp. |
| **Orb** — "I like this one," wants more spectrum | **CONFIRMED, 12× redundant** | `bandForTick(i) = i % BANDS` over `TICK_COUNT = 48` — 48 spokes carrying **4 distinct lengths** (`:497-501, 455`). Once bins exist this is a **one-line change** and the highest visual return per line in the whole set. |
| **Ribbon** — wants fluid motion; glow underused | **CONFIRMED, two separate constants** | Crest travel hard-capped at `SPEED_MIN/MAX = 0.25/0.37` **widths per second** → a crest takes **2.7-4.0 seconds** to cross (`:364-365`). And `wave` is a **pure sine of the wall clock** (`:400-403`) — audio only changes amplitude and colour, never shape. Glow: `GLOW_ALPHA_MAX = 0.08` vs `CORE_ALPHA_MAX = 0.30`. |
| **Mirrored pulse** — "like an old stereo system" | **SEMANTIC MISMATCH** | It has ~100 columns, but its **X axis is time, not frequency** — every column is the same scalar sampled at a different moment (`:130-141, 366-380`). To read as a stereo spectrum display it needs **per-band history**, not more columns. |
| **Soft blob** — "bland" | **PARTIALLY REFUTED** | It *does* deform per band (`:152-158`). Blandness is elsewhere: `FULL_LOBES` **reuses band 3 twice** (`:326-333`), and a third smoothing envelope stacks on top (`:397, 404`). |
| **Aurora** — mountains should be sharper | **CONFIRMED** | **Nothing in the file shapes the crest at all** — it is a raw sine at `AuroraWaveform.kt:693`. Add waveshaping (raise `|sin|` to a power / blend toward a cusp). Also `BAND_RELEASE_TAUS_SEC = [0.295, 0.188, ...]` (`:456`) **exceed the ~230 ms syllable period**, so the front sheets physically cannot fall between syllables. |
| **Classic** — preview >> practice | **CONFIRMED** | Scalar-only (`:85`); fixed by the analyser work alone. |

### Three things you did not ask about, but should see

1. **The composer's Gemini raw-audio ribbon is frozen.** It is driven by `floatArrayOf(rawAudioRecorder.getMaxAmplitude() / 32767f)` (`NativeMainActivity.kt:1292-1298`) — one scalar, and **nothing recomposes it while recording**. It is not reacting to anything.
2. **The audio player bar shows a fake pulse.** Until the offline decode lands it renders `0.12f + breathe * 0.18f`, a 2-second `infiniteRepeatable` tween (`AudioPlayerBar.kt:190-194`). That is a synthetic breathing animation, **not your audio**.
3. **Both mic paths run `VOICE_COMMUNICATION` with AEC and noise suppression explicitly attached** (`SttStreamClient.kt:346-352`, `VoiceScreen.kt:658-664`). That hardware voice-processing chain **flattens dynamics and strips spectrum before the analyser ever sees a sample.** The composer STT mic has nothing playing, so it does not need AEC — `MIC` or `UNPROCESSED` there would hand the analyser far richer audio for free.

Also: on the voice screen the AI ribbon can **lead the sound by up to a third of a second** (`VoiceScreen.kt:917` writes, `:928` analyses).

### Shape of the work (to be turned into tasks once the last two styles are reviewed)

**Tier 1 — the analyser, which fixes most styles at once**
- Real short-time FFT (256/512-point, Hann-windowed, hand-rolled radix-2 in pure Kotlin — `AuroraAnalyser` deliberately imports no `android.*` so it stays JVM-testable, and no DSP dependency exists or is wanted). Under a millisecond per hop.
- **Decouple analysis from chunk arrival**: ring-buffer PCM, analyse on a fixed ~20 ms hop with 50% overlap. Raises the live update rate 4-15× on its own.
- Additive contract: publish `bins[]` **plus** a derived `bands4[]` so aurora stays pixel-identical by default. `AuroraVoice.band()` already clamps out-of-range indices (`AuroraWaveform.kt:159-163`), so a producer emitting more bands is backward-safe.
- Two AGC references (fast ~0.3-0.5 s for display, slow 6 s for sustained contrast) rather than re-picking one compromise — the ~1000-config sweep already proved one reference cannot do both.
- Make `STEADY_DECAY` time-based; reduce the double compression.

**Tier 2 — per-band history in the feed** (`WaveformFeed.kt:121`), additive alongside the existing scalar ring (~38 kB for 32 bins × 150 samples × 2 speakers). This is the **only** way mirrored pulse, sharp line, classic and cinematic can ever show spectrum.

**Tier 3 — renderer touch-ups**, most of them one-liners once bins exist: orb `i % BANDS` → direct bin index; ribbon speed and glow constants; segmented EQ column count/width derived from surface width with `fineBin` becoming a real read; aurora crest waveshaping; soft blob lobe table.

**Tier 4 — honesty and validation:** re-base `WaveformDemoDriver` on measured live statistics once the analyser changes, or the preview-vs-practice gap silently returns. `AuroraDynamicsDiagnostic.kt` already exists as an unasserted printer whose stated hypothesis is exactly this complaint — extend it to assert.

**Measure first:** log the real device `getMinBufferSize`, actual read size, inter-publish interval and a per-band histogram over 30 s of speech, on the Fold. Every number above is computed from constants; the device numbers should confirm them before tuning begins.

## 17C: Architectural Direction (Brandon, 2026-08-11) — the device is the analyser

> *"Our analysers should be strictly from the device. If we're on the Android app, the waveform should be derived directly from the Android device... TTS comes from the BlackBox but gets pushed to the phone, and all of the waveform stuff should be handled on the device. And before noise suppression — so our waveform gets the most movement and live feeling, so we can get the full spectrum."*

**This is the governing principle for the whole pass.** It resolves several open questions at once:

1. **All analysis is device-local.** The server never computes or ships visualisation data. The BlackBox supplies *audio*; the phone derives *everything visual* from that audio locally. (This is already true structurally — `AuroraAnalyser` imports no `android.*` and runs on real PCM — but it must stay true, and the paths that currently cheat must be fixed.)
2. **Tap the microphone before noise suppression.** Brandon's read of the symptom is exactly right: *"it looks like a flattened picture of what's actually supposed to be happening."* `VOICE_COMMUNICATION` + `AcousticEchoCanceler` + `NoiseSuppressor` is a dynamics-flattening, spectrum-stripping chain applied **before** the analyser sees a sample.
3. **Particle wave and cinematic need no separate notes** — *"exactly the same thing we've been talking about. It's mainly the data that we're getting. We can do more with it."* Both are covered by the Tier 1 analyser work.
4. **The Gemini recording ribbon must follow the same waveform pattern** as every other surface — real PCM through the shared analyser and the style registry, not the frozen `getMaxAmplitude()` scalar.
5. **The audio player bar must be live** — *"real true status and everything production quality."* The synthetic breathing tween is out; it renders real audio or it does not render.

### The one real tradeoff to resolve before coding

Stripping the processing chain is free on some surfaces and **load-bearing on others**:

| Surface | Anything playing? | AEC needed? | Direction |
|---|---|---|---|
| **Composer STT mic** (push-to-talk) | No | **No** | Switch to `UNPROCESSED` (fallback `VOICE_RECOGNITION` → `MIC`). Free win — nothing to echo. |
| **Voice-screen mic** | Yes, the AI is speaking | **Yes** — barge-in without AEC means the model transcribes its own voice | Cannot simply strip. Needs a decision: separate analysis tap, or accept processed audio here, or drop `NoiseSuppressor` while keeping `AcousticEchoCanceler`. |
| **AI / TTS playback** | n/a | n/a | Already raw device-side PCM before `AudioTrack.write`. Just needs the better analyser and the ~⅓ s lead fixed. |
| **Gemini raw recorder** | No | No | Replace `MediaRecorder` with `AudioRecord` so real PCM exists at all. |
| **Player bar** | n/a | n/a | Decode real audio; delete the fake pulse. |

**Two unknowns that decide the architecture, to be settled by measurement, not assumption:**
- Does dropping NS/AEC on the composer mic **degrade STT accuracy**? (Modern ASR often does *better* on unprocessed audio, but this must be tested, not assumed — it is Brandon's primary input path.)
- Can a **second concurrent `AudioRecord`** be opened purely for analysis on the voice screen, or does Android's concurrent-capture policy forbid it? If forbidden, the voice-screen visual either shares the processed stream or gets a compensating expansion stage.

`UNPROCESSED` also requires the device to advertise `PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED` — needs a capability check with a graceful fallback chain, verified on the Fold.

## 17D: Audio-Source Design, Settled (measured 2026-08-11)

### CORRECTION to 17C

**The composer STT mic does not attach AEC or NoiseSuppressor at all.** `grep -c 'audiofx|AcousticEchoCanceler|NoiseSuppressor'` over `SttStreamClient.kt` returns **0** — the lines cited earlier are the `AudioRecord` *constructor*, not effect attachment. **Only `VoiceScreen.kt` attaches effects** (`:677-686`).

This *improves* the picture: on the composer, the `AudioSource` constant is the **entire** lever, and there are no effect objects to unwind.

### The STT-accuracy risk is much smaller than assumed

Two pieces of in-repo prior art, both shipping today:

- `OverlayService.kt:1415-1432` already captures on `VOICE_RECOGNITION` with **no effects**, and that audio is transcribed.
- The composer's "record audio" button uses `AudioRecorderManager` on `AudioSource.MIC` (`:36-38`), and `NativeMainActivity.kt:1223-1265` uploads it to `POST /stt` for Whisper **in production, every day.**

**MIC-source audio already transcribes through this system's ASR.** The A/B is still worth running, but this is no longer a leap into the dark.

### The API rule that would have burned us

**`UNPROCESSED` fails *silently*.** Per AOSP `MediaRecorder.java:357-359`: *"behaves like DEFAULT otherwise."* No exception, no `STATE_INITIALIZED` change. **An exception-driven or state-driven fallback chain cannot detect it** — and `OverlayService.kt:1415-1432` uses exactly that wrong pattern today.

The only correct detection is the capability property:

```kotlin
"true".equalsIgnoreCase(am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED))
```

(`getProperty` returns `null` for an unknown key; `null` and `"false"` both mean unsupported.)

**Build levels:** `compileSdk 36`, `minSdk 26`, `targetSdk 36`, `versionCode 34` / `1.11.0-waveform-styles`. `UNPROCESSED` and the capability property are both **API 24**, so they are available unconditionally — **no `Build.VERSION` guards needed.** Only `setPrivacySensitive` (API 30) needs one.

### Per-surface decision

| Surface | Duplex? | Decision |
|---|---|---|
| **Composer STT mic** | No | Switch source via a capability-probed policy (`UNPROCESSED` → `VOICE_RECOGNITION` → `MIC`). One stream still feeds both socket and analyser — the analyser fork is **upstream** of the socket on identical bytes (`:397` before `:398-406`), so ribbon and transcript change together. |
| **Voice-screen mic** | **Yes** — AI speaking | **Keep `VOICE_COMMUNICATION` + AEC.** Subtract AGC and NS only. Losing AEC means the model transcribes its own voice during barge-in: a functional regression, not a cosmetic one. |
| **AI/TTS playback** | n/a | Already device-side PCM. Fix the lead (below). |
| **Gemini recorder** | No | `AudioRecord` + same source policy. |
| **Player bar** | n/a | Decode-ahead (below). |

**The voice-screen ribbon will always be less lively than the composer's. That asymmetry is correct** — do not "fix" it by dropping AEC.

### Two bugs found along the way

1. **Dropped effect objects.** `VoiceScreen.kt:678, :683` create `aec` and `ns` as **local vals** that are never retained and never released. Their native engines are reclaimed by finalization at an arbitrary later time, so **effect state across mic sessions is nondeterministic.** Any change here must fix ownership and release.
2. **The device-wide comms mode is a bigger hammer than the effects.** `VoiceScreen.kt:524` sets `audioManager.mode = MODE_IN_COMMUNICATION` for the whole session. On devices where the processing lives in the HAL/DSP, `setEnabled(false)` may return SUCCESS and change nothing — there is no API to force it.

### THE SEQUENCING TRAP — read before tuning anything

> Every measured constant in `AuroraAnalyser` — `PEAK_INIT`, `PEAK_FLOOR`, `NOISE_LIVE` (from the 2026-07-18 Fold 6 speaker→mic loop), `HEADROOM`, `GAMMA`, and the AGC taus — **was calibrated against an AGC-flattened stream.** Changing the audio source invalidates all of them simultaneously.

**Therefore: telemetry first, then source change, then re-calibrate, then tune.** Doing the source switch and the constant re-tune in one step makes any regression impossible to attribute — the exact pattern the *telemetry-before-more-fixes* rule exists to prevent.

### The analysis seam

New `AudioAnalysisTap.kt` beside `AuroraAnalyser` — a PCM-only entry point owning a ring buffer and a **sample-domain hop scheduler**, so analysis rate is decoupled from chunk arrival. It publishes the legacy 4 bands **and** new bins on the same frame.

- `AuroraAnalyser.BINS = 24` as a **new** const; `BANDS` untouched. Bins ride a **new channel**, not inside `AuroraVoice` (which stays exactly 4 wide), so `AuroraWaveform.kt:139-183` is unchanged.
- Windowing: **rectangular** for the legacy Goertzel path, **Hann** for the new FFT path. Budget: a 1024-point real FFT is ~25 kflop; at 50 Hz across 3 taps this is comfortably under budget.
- `WaveformFeed`: per-band rings always, per-bin rings **lazily**, plus an additive `step()` overload. Existing `step()`, `levelOf` and `LEVEL_WEIGHTS` untouched, so their pinned tests stay green.
- **AI ribbon lead:** fix with a presentation-time delay line on playback taps only, using `AudioTrack.getTimestamp` with a buffer-size clamp fallback (the API is best-effort and stale right after `play()`).

### The two broken surfaces

**Gemini recorder:** replace `MediaRecorder` with `AudioRecord` + encoder in a **new** class; leave `AudioRecorderManager` alone (it serves the working Whisper path). Size reads in **frames**, not by doubling `getMinBufferSize`. Publish silence as the shared `AURORA_SILENT_BANDS`, not a fresh array. **The upload contract changes** (`.m4a` → WAV or our own AAC) — confirm the server `/upload` + `/stt` and Gemini's attachment path accept it, and check the size limit (60 s of 44.1 kHz mono PCM16 ≈ 5.3 MB vs ~1 MB at AAC).

**Player bar — recommendation: decode-ahead, NOT a live PCM tap.** MediaPlayer exposes no PCM sink; a live tap means replacing the player. Decode-ahead gives correct bands from frame one and keeps determinism.

> **Ship B1 and B2 together.** Deleting the fake breathing pulse *alone* trades a lying ribbon for a flat one and looks **worse** until decode-ahead lands. The honesty fix and the gap fix are one change set.

Bounded by an **LRU envelope cache** — `ChatBubble.kt:467` renders one bar per TTS message, so unbounded prefetch across a long thread means dozens of concurrent whole-file `MediaCodec` decodes: a straight regression. Decode must also become **cooperatively cancellable** (`AudioEnvelope.kt:104-162`), and an early return must still hit the `finally` at `:177-181` or a cancelled decode **leaks a hardware decoder instance**, which on some devices is a global resource.

"Real true status" = a rendered `PlaybackStatus` state machine (idle / downloading / preparing / playing / error), **without restructuring the existing scrub geometry** (`:256-294`) — that affordance works.

**Note the interaction:** the player bar's effective amplitude exponent is **0.275** (offline `LIFT 0.55` then the renderer's `sqrt`) versus **0.41** live. Whoever fixes the double compression and whoever fixes the offline shaping **collide on this exact surface** — same owner, same commit.

## 17E: The Milestone Plan (synthesized from a 3-way design panel, 2026-08-11)

Three planners worked the same evidence independently from different framings (risk-first, user-value-first, dependency-first). **They converged on the ordering**, which is the strongest signal available that it is right. Two corrections and one honest disagreement came out of it.

### Corrections to 17D

**C1 — The player-bar decode-ahead ALREADY EXISTS.** `AudioPlaybackManager.kt:42-46, 235-246` already decodes RMS + per-band envelopes and samples them at the live playback position; `AudioPlayerBar.kt:98-99` reads them. **The synthetic breathing tween at `:190-194` is only the pre-decode fallback.** So this is not "build decode-ahead" — it is *"make the existing decode land before audio is audible, cache it, cancel it safely, then delete the fallback."* Materially smaller and lower risk than 17D implied.

**C2 — The style files are already JVM-testable.** They use the pure-math-object pattern (`SegmentedEqMath`, `SharpLineMath`) with no Compose or `android.*` in the math, so every per-style change tests under the existing gate with **no new test infrastructure**.

### The honest disagreement, worth your attention

You asked for "before noise suppression," and it is worth doing. But the panel's risk-first planner argued for **cutting it** on measured grounds, and the argument deserves a hearing:

> *Live p50 0.873 / p90 0.995 is a signal parked against a ceiling. That is the double compression (effective exponent 0.41), not the noise suppression. The source change is a quality nicety; it is not what makes the ribbon dead.*

**Both are real, but they are not equal.** The double compression (M6) is the dominant cause of the flat look; the mic source (M5) is a genuine but secondary improvement that carries the plan's only production-risk (composer STT feeds Whisper daily). The plan keeps both, ordered so the big win lands first and the risky one is contained.

### The organising principle

> **Convert each invalidating change into a non-invalidating one before landing it.**

Three pending changes each independently invalidate every measured constant: the mic source, the buffer-length fix, and the perceptual-curve fix. Landing any two together makes attribution impossible. So:

- The buffer fix is dangerous *only because* the AGC release is frame-counted. **Make the release dt-based first**, prove rate-invariance with a property test, and the buffer fix becomes provably free.
- The source change is dangerous *because one constant set serves all sources*. **Make the constants a per-source calibration profile whose default is byte-identical to today**, and the change is contained to the one source that moved.
- The curve fix visibly changes the shipped default. So it is the **one place I1 (aurora pixel-identity) is broken deliberately** — with a reviewed side-by-side and a golden test edited on purpose, never loosened.

### Milestones

| # | Title | Depends | Ships |
|---|---|---|---|
| **M0** | Measurement floor | — | Nothing visible. Every number this plan intends to move becomes a JVM assertion + a device CSV dump. |
| **M1** | Rate honesty | M0 | dt-based AGC release, rate-invariance property test, then the 2× read fix **free**. Update rate roughly doubles. Cheap un-freeze of the Gemini ribbon (ticker only, **no format change**). |
| **M2** | Spectrum core + tap | M1 | Hann FFT, 24 log bins, `AudioAnalysisTap` with PCM ring + sample-domain hop. **Pure and unused** — revertible by deleting files. |
| **M3** | Additive contracts | M2 | Feed overload, frame field, bins channel. **Exit criterion IS zero edits to any pre-existing test.** |
| **M4** | Wire the tap in | M3 | First behaviour change. Both mic loops through the tap; AI-ribbon lead fixed. |
| **M5** | Mic source | M4 | Capability-probed chain + per-source calibration profiles (default byte-identical). Composer only; voice screen keeps AEC. |
| **M6** | Recalibrate | M5 | One perceptual curve with one owner, `HEADROOM` refit, dual-reference AGC, band release taus under the syllable period, crest shaping. **Deliberate default change — needs your sign-off.** |
| **M7** | Spectrum-aware styles | M6 | One renderer + one test file each, fully parallelisable. |
| **M8** | Broken surfaces | M6 | Player-bar fallback deleted (per C1, smaller than thought); Gemini recorder **blocked on the upload-format question**. |

### Why this order

**M0 first** because everything after is judged by numbers that today exist only as prose. Until they are asserted, "better" is an opinion and a regression is invisible.

**M1 before everything** because rate-invariance is the precondition that makes a whole class of later changes safe. While the release is frame-counted, *any* change to chunking silently retunes the AGC.

**M2/M3 are free options.** Purely additive, zero pixels move, so they cannot regress anything and can be abandoned in place.

**M6 before M7, always.** Fixing SegmentedEq's cosmetic 4-into-16 lerp while the source is still four needles just **makes a prettier lie.**

**M8 last** because the Gemini half is blocked on the unanswered server-format question — but M1 already ships the cheap half of that fix, so slipping costs nothing that currently works.

### Every boundary is abandonable

Since everything commits together with v32/v33/v34, "boundary" means *the working tree is coherent and the gate is green*, not *a commit lands*. But each still leaves the app strictly better than today:

- **M0 alone** ships a regression net that does not exist at all today.
- **M1 alone** doubles the update rate and un-freezes a ribbon that is frozen in production.
- **M3 alone** adds a spectrum nothing yet draws.

### Key exit criteria (measurable, not subjective)

- Gate green at **≥ 2075** tests throughout (`./gradlew :app:testDebugUnitTest --offline`).
- **M1:** analyser update rate **≥ 45 Hz** at both mic sites, measured from telemetry not inferred; per-frame `dtSec` constant within 1% across a 10 s capture; rate-invariance holds across 512/2048/4096-sample chunks.
- **M3:** all 23 `AuroraAnalyserTest` and all 12-13 `WaveformFeedTest` cases pass **with zero edits** — that is the additive proof. Zero pixel change on all ten styles.
- **M5:** composer transcripts on a **fixed 20-phrase script** are no worse than the `VOICE_COMMUNICATION` baseline. Voice-screen output byte-identical (its profile is the unchanged default). Barge-in still works.
- **M6:** peak-to-typical swing **≥ 4×** (from 1.14×), p50 in 0.30-0.55; whisper stays visible **and** true silence stays flat; the sustained-contrast and whisper-separation gates pass **simultaneously** — the thing the ~1000-config sweep proved impossible with one AGC reference.
- **M7:** every style has a test proving it responds to spectral content — two spectra with **equal overall level** must produce different output. Orb yields ≥ 24 distinct spoke lengths (from 4).
- **M8:** no surface draws synthetic data; a cancelled decode provably releases its codec exactly once; one decode per URL across 20+ TTS bars in a long thread.

### The single biggest risk

**M5 silently degrading production STT while the ribbon looks better** — a failure mode invisible on the surface being improved. The ribbon will look *more* alive on a rawer stream at the same moment transcripts get worse, so the visible evidence actively misleads. Hence the fixed 20-phrase A/B as a hard gate rather than a subjective listen, and per-source profiles so a fallback rung costs nothing.

**Secondary:** M6 is a deliberate break of aurora pixel-identity. That needs your explicit side-by-side approval before it starts, not after.

## 17F: Upload Format — DECIDED (from live provider docs, 2026-08-11)

### The decision: **16 kHz mono PCM16 WAV**

`recording_<timestamp>.wav`, written directly from the `AudioRecord` buffers — a 44-byte RIFF header over the same samples the analyser draws. Drop `MediaRecorder` for the composer recorder.

Two agents researched this independently (provider docs vs. on-device encoding) and **converged on the same answer**.

### Why, strongest argument first

**1. Everything above 16 kbps is thrown away before the model sees it.** Gemini's own docs: *"Resolution: Downsampled to 16 Kbps"* and *"Channels: Multi-channel audio combined to single channel."* Whisper-class STT resamples to 16 kHz mono. Today's 44.1 kHz / 128 kbps AAC buys the model **literally nothing** — we spend CPU and code surface preserving fidelity every consumer discards. Cost is **32 tokens per second of audio regardless of format**, so encoding cannot change the bill (~$0.003/minute).

**2. WAV is the only format the whole pipeline agrees on.** The live docs are tighter than expected in both directions:

| Provider | Documented accept list | Notably absent |
|---|---|---|
| Gemini (ai.google.dev) | wav, mp3, aiff, aac, ogg-vorbis, flac | **no m4a, no mp4, no webm, no opus** |
| OpenAI STT (live) | mp3, mp4, mpeg, mpga, m4a, wav, webm | **no flac, no ogg, no opus, no bare aac** |
| ElevenLabs Scribe | very wide (3 GB, 10 h) | never the constraint |

**The intersection is essentially WAV.** And `.wav → audio/wav` is already mapped correctly in every MIME map in this repo, so **zero Orchestrator changes are required** — which matters, because the server does **not** transcode on the `/stt` or chat paths (bytes go to the provider exactly as uploaded).

**3. 16 kHz is the exact floor the analyser needs — and this is a code fact, not a preference.** `AuroraAnalyser` drops any probe not `< sampleRate * 0.45` (`:338`, `:409-412`), and the top probe is 6800 Hz. **6800 / 0.45 = 15,111 Hz.** So 16 kHz keeps all 8 probes; 12 kHz loses the sibilance probe; 8 kHz loses the entire HIGH band. 16 kHz is simultaneously the analyser's floor and the providers' ceiling — they meet exactly.

**4. 44.1 kHz is not "bigger", it is broken at real durations.** Measured against the actual corpus on this box (216 phone recordings in `Portal/uploads`, average ~56 s, longest ~5.7 min): at 44.1 kHz mono PCM16 the longest real recording becomes **~30 MB**, which **exceeds OpenAI's hard 25 MB cap** and Gemini's 20 MB request budget. At 16 kHz the same recordings are ~1.8 MB average, ~11 MB worst case — inside every documented ceiling with margin.

**5. WAV has no silent-failure class.** No codec, no muxer, no `csd-0`, no track index, no presentation timestamps. The entire category of *"file looks fine on the phone, provider rejects it"* disappears, and the header is a pure function that unit-tests on the JVM.

**6. `minSdk 26` removes every good compressed alternative anyway.** Opus encoder is **API 29+**, `MUXER_OUTPUT_OGG` is **API 29+**, FLAC-in-MP4 is **API 29+**, and `MediaMuxer` cannot write a raw `.flac` at all. Only WAV and AAC-in-MP4 are universally available — and the exotic formats lose on the provider side too.

**7. It matches what the Portal already does.** `Portal/modules/gemini-recorder.js:360-369` captures WebM then **re-encodes to 16-bit PCM WAV before upload.** The web client reached this conclusion already; this aligns Android with it.

### Size table (mono, MB = 10⁶ bytes; parenthesis = base64-inflated for Gemini inline)

| Format | 30 s | 60 s | 5 min | Gemini 20 MB budget | OpenAI 25 MB cap |
|---|---|---|---|---|---|
| **WAV 16 kHz** | 0.96 (1.28) | 1.92 (2.56) | 9.60 (12.80) | **~7.8 min** | **~13 min** |
| WAV 24 kHz | 1.44 (1.92) | 2.88 (3.84) | 14.40 (19.20) | ~5.2 min | ~8.7 min |
| WAV 44.1 kHz (today's rate) | 2.65 | 5.29 | 26.5 | **~2.8 min ✗** | **~4.7 min ✗** |
| AAC 64 kbps | 0.24 | 0.48 | 2.40 | ~31 min | ~52 min |

AAC stays available as a **later, measured** optimisation if long recordings ever become common — it saves under 1 MB/minute against a 500 MB `/upload` limit, which does not justify the muxer's silent-failure surface today.

### Three cautions for the implementer

- **`AudioRecorderManager` is shared with Voice Lab's ElevenLabs cloning path** (`VoiceLabViewModel.kt:106`). Do **not** change it in place — a format change would silently alter what gets cloned. Build a new `PcmAudioRecorder` and leave the existing class serving Voice Lab.
- **The filename extension is the entire contract.** The Android uploader hardcodes `application/octet-stream` (`BlackBoxApi.kt:200-204`) and `/upload` preserves only the suffix (`admin_routes.py:57`), so every downstream MIME decision keys off `.wav`.
- **`AudioRecord` guarantees only 44100 Hz on all devices.** 16000 Hz must be requested defensively — check `getState()` and read back `getSampleRate()`, with a resample or a documented fallback if the device refuses.

### Three separate defects this research uncovered

These are **real production bugs**, unrelated to the format choice, found while tracing the upload path:

1. **The Android app tags every uploaded file as an image.** `ChatRepository.kt:78-83` emits every URL as an `image_url` part — including audio recordings. It only works today because the Gemini handlers accept `image_url`/`audio_url` alike and key the MIME off the file suffix. On the OpenAI path the same file falls through to an image MIME.
2. **Non-streaming Gemini silently drops every attachment.** `call_gemini` (`chat_routes.py:1216-1768`) has a function-scoped `from pathlib import Path` at `:1490` inside the `generate_video` tool branch. That makes `Path` a **local for the entire function**, so the media block's `Path("Portal")` raises `UnboundLocalError` — and attachments vanish. A classic Python scoping trap.
3. **The audio auto-route exists only on the non-stream path.** The `has_audio → google` switch lives solely at `tasks.py:2314-2349`; `/chat/stream` has no equivalent.

Also worth knowing: **Google Chirp 2 synchronous recognition is capped at 60 seconds or 10 MB, whichever comes first** — a hard duration ceiling for any box whose STT resolves to `google`, independent of format. And the phone currently **uploads the same file twice** (`/upload` then `/stt`) over Tailscale against a 60 s OkHttp write timeout.

---

### If time runs short

The two planners who were asked disagreed usefully. Value-first would ship **M1+M2+M3** and cut the rest. Risk-first would cut **M5** (mic source) and the M7 tail, keeping M0, M1, M3-minimal, M6, and M8's player bar. **Both keep the analyser work and both cut the styles** — which is the real signal: the data fix is the plan, and the renderers are the polish.
