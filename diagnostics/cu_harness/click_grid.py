#!/usr/bin/env python3
"""Click-grid target board for the CU click-accuracy harness (M0, 2026-07-23).

Runs fullscreen on a CU session's virtual display and logs every ButtonPress
with ground truth: nine labeled crosshair targets (corners, edge midpoints,
center — the anisotropic-squish failure mode shows up at the horizontal
extremes), each click recorded as a JSON line with the nearest target and the
pixel distance to its center.

Zero non-stdlib deps beyond python3-tk. Launch (on the box, targeting the
session display):

    DISPLAY=:100 python3 click_grid.py --log /tmp/cu_clicks.jsonl

On startup writes <log>.targets.json with the exact target centers + screen
size so the runner (run_battery.py) can print a per-target pass/fail table.
"""
import argparse
import json
import math
import time
import tkinter as tk

INSET = 90          # px from each screen edge to corner/edge target centers
RADIUS = 26         # target circle radius
LABELS = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]


def target_positions(w: int, h: int) -> dict:
    """Nine targets: 4 corners, 4 edge midpoints, center — labeled A..I
    left-to-right, top-to-bottom."""
    xs = (INSET, w // 2, w - INSET)
    ys = (INSET, h // 2, h - INSET)
    coords = [(x, y) for y in ys for x in xs]
    return {label: coords[i] for i, label in enumerate(LABELS)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="JSONL click log path")
    ap.add_argument("--tolerance", type=int, default=10,
                    help="hit radius in px (informational; runner re-judges)")
    args = ap.parse_args()

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(bg="#101014")
    root.update_idletasks()
    w, h = root.winfo_screenwidth(), root.winfo_screenheight()

    targets = target_positions(w, h)
    with open(args.log + ".targets.json", "w") as f:
        json.dump({"screen": [w, h], "tolerance": args.tolerance,
                   "targets": {k: list(v) for k, v in targets.items()}}, f)

    canvas = tk.Canvas(root, width=w, height=h, bg="#101014",
                       highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    canvas.create_text(
        w // 2, 34, fill="#8888aa", font=("sans", 14),
        text="CU click-accuracy board — click the CENTER of each red target")

    for label, (x, y) in targets.items():
        canvas.create_oval(x - RADIUS, y - RADIUS, x + RADIUS, y + RADIUS,
                           outline="#ff4444", width=3)
        canvas.create_line(x - RADIUS - 8, y, x + RADIUS + 8, y,
                           fill="#ff4444", width=1)
        canvas.create_line(x, y - RADIUS - 8, x, y + RADIUS + 8,
                           fill="#ff4444", width=1)
        canvas.create_oval(x - 2, y - 2, x + 2, y + 2,
                           fill="#ffffff", outline="")
        # Label OFFSET from the click point so the letter itself is never the
        # thing the model aims at.
        ly = y - RADIUS - 22 if y > 60 else y + RADIUS + 22
        canvas.create_text(x, ly, text=label, fill="#ffd966",
                           font=("sans", 20, "bold"))

    seq = {"n": 0}

    def on_click(event):
        seq["n"] += 1
        nearest, dist = None, float("inf")
        for label, (tx, ty) in targets.items():
            d = math.hypot(event.x - tx, event.y - ty)
            if d < dist:
                nearest, dist = label, d
        rec = {"seq": seq["n"], "ts": time.time(), "x": event.x, "y": event.y,
               "target": nearest, "dist": round(dist, 1),
               "hit": dist <= args.tolerance}
        with open(args.log, "a") as f:
            f.write(json.dumps(rec) + "\n")
        # Visual feedback so the model can SEE a registered click.
        color = "#3ecf6a" if rec["hit"] else "#d9a53a"
        canvas.create_oval(event.x - 5, event.y - 5, event.x + 5, event.y + 5,
                           fill=color, outline="")

    canvas.bind("<ButtonPress-1>", on_click)
    root.mainloop()


if __name__ == "__main__":
    main()
