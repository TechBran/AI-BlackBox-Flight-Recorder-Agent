#!/usr/bin/env python3
"""diagnostics/localstack/vram.py — sample nvidia-smi memory.used while a
command runs and report PEAK used-MiB. Used by G1 (re-embed batch peak),
G3 (TTS variant-transition peak), G5 (swap peak). Run on MS02 only."""
from __future__ import annotations
import argparse, json, subprocess, sys, threading, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from diagnostics.localstack.metrics import parse_nvidia_smi_used_mib  # noqa: E402

BUDGET_MIB = 16380  # RTX 2000 Ada


def sample_used_mib(gpu: int) -> int:
    out = subprocess.check_output(
        ["nvidia-smi", f"--id={gpu}", "--query-gpu=memory.used",
         "--format=csv,noheader,nounits"], text=True, timeout=10)
    return parse_nvidia_smi_used_mib(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--duration", type=float, default=10.0,
                    help="seconds to sample when no command is given")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- <command> to run while sampling (optional)")
    args = ap.parse_args(argv)

    samples, stop = [], threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                samples.append(sample_used_mib(args.gpu))
            except Exception as e:  # noqa: BLE001
                print(f"[vram] sample error: {e}", file=sys.stderr)
            stop.wait(args.interval)

    baseline = sample_used_mib(args.gpu)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    t0 = time.time()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    rc = subprocess.call(cmd) if cmd else (time.sleep(args.duration) or 0)
    stop.set()
    t.join(timeout=2)

    peak = max(samples) if samples else baseline
    summary = {"label": args.label, "gpu": args.gpu,
               "baseline_mib": baseline, "peak_mib": peak,
               "delta_mib": peak - baseline, "n_samples": len(samples),
               "elapsed_s": round(time.time() - t0, 2),
               "budget_mib": BUDGET_MIB, "headroom_mib": BUDGET_MIB - peak,
               "fits_budget": peak < BUDGET_MIB, "command_rc": rc}
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, indent=2))
    return 0 if summary["fits_budget"] else 2


if __name__ == "__main__":
    sys.exit(main())
