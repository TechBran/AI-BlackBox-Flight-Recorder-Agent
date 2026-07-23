# Vendored noVNC (pinned)

- **Origin:** https://github.com/novnc/noVNC
- **Tag:** `v1.5.0` (pinned; `git clone --depth 1 --branch v1.5.0`)
- **Vendored:** 2026-07-23 (CU live-view revamp, design doc
  `docs/plans/2026-07-23-cu-live-view-splashtop-design.md` §10-D3)
- **License:** MPL-2.0 (see `LICENSE.txt`, copied from the upstream repo)

## What is vendored (and why only this)

Only the RFB client module and its single dependency closure:

- `core/` — the ES-module RFB client (`core/rfb.js` is the entry point the
  CU viewer imports as `/cu/novnc/core/rfb.js`)
- `vendor/pako/` — the only `core/` dependency outside `core/` itself
  (`core/inflator.js` / `core/deflator.js` import
  `../vendor/pako/lib/zlib/*.js`)

The upstream app shell (`app/`, `vnc.html`, tests, docs, po, snap, etc.) is
deliberately NOT vendored — the BlackBox viewer page constructs `RFB`
directly.

## Why vendored at all (D3)

Fresh-box robustness: apt `novnc` is SHOULD_HAVE and demonstrably missing on
dev boxes, and apt ships 1.3.0 whose RFB module lacks 1.5.x touch/gesture
affordances. The Orchestrator mounts this directory at `/cu/novnc`
(`Orchestrator/app.py`), falling back to `/usr/share/novnc` only when this
copy is absent.

## Updating

Re-clone at the new tag, re-copy `core/` + `vendor/pako/` + `LICENSE.txt`,
and update the tag + date above. Never hand-edit files in this tree.
