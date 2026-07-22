#!/usr/bin/env python3
"""M9 smoke: two concurrent virtual CU displays coexist; the 4th trips the cap.
Run on a box WITH Xvfb installed (MS02 / any GPU box). Not a CI test — real spawns."""
import sys
from Orchestrator.browser.display import DisplayAllocator, MAX_VIRTUAL_SESSIONS

def main():
    a = DisplayAllocator()
    try:
        h1 = a.allocate("smoke-1", backend="anthropic", operator="system")
        h2 = a.allocate("smoke-2", backend="google", operator="system")
        assert h1.display == ":100" and h2.display == ":101", (h1.display, h2.display)
        assert h1.vnc_port == 5901 and h2.vnc_port == 5902
        assert (h2.width, h2.height) == (1440, 900)   # per-backend resolution
        print(f"[OK] two concurrent displays: {h1.display} + {h2.display}")
        print(f"[OK] active_sessions -> {len(a.active_sessions())} sessions")
        # Fill to cap, then assert the next allocate raises.
        for i in range(2, MAX_VIRTUAL_SESSIONS):
            a.allocate(f"smoke-fill-{i}", backend="anthropic", operator="system")
        try:
            a.allocate("smoke-over", backend="anthropic", operator="system")
            print("[FAIL] cap not enforced"); return 1
        except RuntimeError as e:
            print(f"[OK] cap enforced: {e}")
        return 0
    finally:
        a.shutdown_all()
        print("[OK] all sessions torn down")

if __name__ == "__main__":
    sys.exit(main())
