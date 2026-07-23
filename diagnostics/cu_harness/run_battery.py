#!/usr/bin/env python3
"""CU ground-truth verification battery (M0, production-readiness 2026-07-23).

Runs ON the box (needs the session displays + xdotool locally). Two modes:

  clicks   — put the click_grid target board on the agent's OWN virtual
             display, tell the model to click every labeled target, judge each
             click against ground-truth pixel coordinates.
  open-app — tell the model to open a terminal from the populated desktop
             (right-click menu / taskbar); verify an xterm window exists on
             the SESSION display afterwards. Proves real desktop capability,
             not just raw clicking.

Usage:
    python3 run_battery.py --backend anthropic [--mode clicks|open-app]
    python3 run_battery.py --backend openai --mode clicks
    python3 run_battery.py --backend gemini --mode clicks

Exit 0 = pass. Prints a per-target table for the click battery. Stdlib only.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("BLACKBOX_URL", "http://localhost:9091")
OPERATOR = "cu-harness"
LOG = "/tmp/cu_harness_clicks.jsonl"
TASK_TIMEOUT_S = 420
# Grid targets are 26px-radius circles; tolerance is deliberately looser than
# the drawn radius to separate "grounding works" from "pixel-perfect".
TOLERANCE = 25

CLICK_PROMPT = (
    "You are on a test desktop showing a click-accuracy board: nine red "
    "crosshair targets in circles, labeled A through I (yellow letters near "
    "each target). Click the exact CENTER of each target once, in order "
    "A, B, C, D, E, F, G, H, I. The label letter sits OUTSIDE its target — "
    "always click the white center dot inside the red circle, never the "
    "letter. A green/orange dot appears where your click landed. If the board "
    "is not visible yet, wait 5 seconds and take a screenshot until it "
    "appears. After clicking all nine targets, say DONE."
)

# Vocabulary-fair across backends: Gemini's CU action set has NO right-click,
# so the taskbar launcher (single left click) is the primary route; the
# right-click menu is offered only as an alternative for models that can.
OPEN_APP_PROMPT = (
    "You are on a Linux desktop. Open a terminal window. The taskbar at the "
    "very bottom of the screen has a row of small app-launcher icons at its "
    "far BOTTOM-LEFT corner; the icon with a stylized X launches the XTerm "
    "terminal — click that icon once. (If your tools support right-clicking, "
    "you may instead right-click the empty desktop background and choose "
    "'Terminal' from the Applications menu.) Take a screenshot to verify a "
    "terminal window actually opened; if it did not, try again. When a "
    "terminal window is visible, say DONE."
)


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def pick_model(backend: str) -> str:
    """Resolve a model id for the backend from the live CU catalog."""
    want = {"gemini": "google"}.get(backend, backend)
    try:
        cat = _get("/models/computer-use")
        for m in cat.get("models", []):
            if m.get("backend") == want:
                return m.get("id", "")
    except Exception as e:
        print(f"[harness] catalog lookup failed ({e}); using backend fallback")
    return {"anthropic": "", "openai": "gpt-5.5",
            "gemini": "gemini-2.5-computer-use-preview-10-2025"}.get(backend, "")


def open_session() -> tuple:
    """Pre-open the operator's virtual session; return (session_id, display)."""
    out = _post("/cu/session/open", {"operator": OPERATOR})
    sid = out.get("session_id")
    if not sid:
        sys.exit(f"[harness] /cu/session/open failed: {out}")
    for _ in range(20):
        for s in _get("/cu/sessions").get("sessions", []):
            if s.get("session_id") == sid:
                return sid, s["display"]
        time.sleep(0.5)
    sys.exit("[harness] session display never appeared in /cu/sessions")


def wait_new_session_display(known: set, timeout: float = 90) -> str:
    """Gemini path: the task creates its own isolated session — watch
    /cu/sessions for a display we have not seen before."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in _get("/cu/sessions").get("sessions", []):
            if s.get("display") not in known:
                return s["display"]
        time.sleep(1)
    sys.exit("[harness] no new session display appeared for the gemini task")


def spawn_grid(display: str) -> subprocess.Popen:
    for p in (LOG, LOG + ".targets.json"):
        if os.path.exists(p):
            os.remove(p)
    grid = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "click_grid.py")
    proc = subprocess.Popen(
        ["python3", grid, "--log", LOG, "--tolerance", str(TOLERANCE)],
        env={**os.environ, "DISPLAY": display},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(20):
        if os.path.exists(LOG + ".targets.json"):
            return proc
        if proc.poll() is not None:
            err = proc.stderr.read().decode()[:400]
            sys.exit(f"[harness] click_grid died on {display}: {err}")
        time.sleep(0.5)
    sys.exit("[harness] click_grid never wrote its targets file")


def run_task(prompt: str, model: str) -> dict:
    out = _post("/browser/run", {"prompt": prompt, "operator": OPERATOR,
                                 "model": model})
    task_id = out.get("task_id")
    if not task_id:
        sys.exit(f"[harness] /browser/run failed: {out}")
    print(f"[harness] task {task_id} running (model={model or 'default'})...")
    deadline = time.time() + TASK_TIMEOUT_S
    while time.time() < deadline:
        t = _get(f"/tasks/{task_id}")
        status = (t.get("task") or t).get("status", "")
        if status in ("completed", "failed", "cancelled"):
            print(f"[harness] task finished: {status}")
            return t
        time.sleep(5)
    sys.exit(f"[harness] task {task_id} did not finish in {TASK_TIMEOUT_S}s")


def judge_clicks() -> bool:
    try:
        with open(LOG + ".targets.json") as f:
            meta = json.load(f)
    except FileNotFoundError:
        print("[harness] FAIL — targets file missing (grid never started?)")
        return False
    clicks = []
    try:
        with open(LOG) as f:
            clicks = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        pass

    best = {}  # target -> smallest distance clicked
    for c in clicks:
        t = c["target"]
        if t not in best or c["dist"] < best[t]:
            best[t] = c["dist"]

    print(f"\n  screen={meta['screen'][0]}x{meta['screen'][1]}  "
          f"tolerance={TOLERANCE}px  clicks_logged={len(clicks)}")
    print("  target  position        best-dist  verdict")
    all_pass = True
    for label, (tx, ty) in meta["targets"].items():
        d = best.get(label)
        ok = d is not None and d <= TOLERANCE
        all_pass &= ok
        dtxt = f"{d:7.1f}px" if d is not None else "  never clicked"
        print(f"    {label}    ({tx:4d},{ty:4d})   {dtxt}   "
              f"{'PASS' if ok else 'FAIL'}")
    return all_pass


def verify_window(display: str, klass: str, quiet: bool = False) -> bool:
    try:
        r = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", klass],
            env={"DISPLAY": display, "PATH": "/usr/bin:/usr/local/bin:/bin"},
            capture_output=True, text=True, timeout=6)
    except subprocess.TimeoutExpired:
        # A dead/released display makes xdotool hang — treat as "not found".
        if not quiet:
            print(f"[harness] {display} unreachable (released?)")
        return False
    ids = [w for w in r.stdout.split() if w.strip()]
    if not quiet:
        print(f"[harness] {klass} windows on {display}: {ids or 'none'}")
    return bool(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True,
                    choices=["anthropic", "openai", "gemini"])
    ap.add_argument("--mode", default="clicks", choices=["clicks", "open-app"])
    args = ap.parse_args()

    model = pick_model(args.backend)
    prompt = CLICK_PROMPT if args.mode == "clicks" else OPEN_APP_PROMPT
    grid = None
    display = None

    try:
        if args.backend in ("anthropic", "openai"):
            sid, display = open_session()
            print(f"[harness] session {sid[:8]} on {display}")
            if args.mode == "clicks":
                grid = spawn_grid(display)
            run_task(prompt, model)
        else:
            # Gemini tasks build their own isolated session — start the task,
            # catch its display as it appears, then put the board up.
            known = {s.get("display")
                     for s in _get("/cu/sessions").get("sessions", [])}
            import threading
            holder = {}

            def _worker():
                try:
                    holder.update(run_task(prompt, model))
                except SystemExit as e:
                    # threading swallows SystemExit in non-main threads —
                    # surface it or the harness hides real failures.
                    holder["fatal"] = str(e)
            t = threading.Thread(target=_worker)
            t.start()
            display = wait_new_session_display(known)
            print(f"[harness] gemini task display: {display}")
            if args.mode == "clicks":
                grid = spawn_grid(display)
                t.join(TASK_TIMEOUT_S + 30)
            else:
                # Verify DURING the run: the gemini task's teardown releases
                # its session display, so a post-join probe would always hit a
                # dead Xvfb (review find, 2026-07-23).
                live_hit = False
                while t.is_alive():
                    if verify_window(display, "xterm", quiet=True):
                        live_hit = True
                        print(f"[harness] xterm observed live on {display}")
                        break
                    time.sleep(2)
                t.join(TASK_TIMEOUT_S + 30)
            if holder.get("fatal"):
                sys.exit(f"[harness] gemini task thread failed: {holder['fatal']}")

        if args.mode == "clicks":
            ok = judge_clicks()
        elif args.backend == "gemini":
            ok = live_hit
        else:
            ok = verify_window(display, "xterm")

        print(f"\n[harness] {args.backend}/{args.mode}: "
              f"{'PASS' if ok else 'FAIL'}")
        sys.exit(0 if ok else 1)
    finally:
        if grid is not None and grid.poll() is None:
            grid.terminate()


if __name__ == "__main__":
    main()
