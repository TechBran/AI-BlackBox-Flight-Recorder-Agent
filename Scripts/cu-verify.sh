#!/usr/bin/env bash
# CU production-readiness verification (plan 2026-07-23, M0 deliverable).
#
#   Scripts/cu-verify.sh                 # unit gate only (CI-safe, headless)
#   Scripts/cu-verify.sh --live          # + live battery, all three backends
#   Scripts/cu-verify.sh --live gemini   # + live battery, one backend
#
# The live battery must run ON the box (it spawns the target board on the
# agent's virtual display and verifies windows with xdotool). Each backend
# runs the click battery then the open-app E2E; see diagnostics/cu_harness/.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="Orchestrator/venv/bin/python"

echo "════════ CU unit gate ════════"
"$PY" -m pytest -q \
    Orchestrator/tests/test_cu_display_coherence.py \
    Orchestrator/tests/test_cu_413_guard.py \
    Orchestrator/tests/test_cu_desktop_population.py \
    Orchestrator/tests/test_cu_actions.py \
    Orchestrator/tests/test_cu_display_wiring.py \
    Orchestrator/tests/test_cu_display_allocator.py \
    Orchestrator/tests/test_cu_virtual_default.py \
    Orchestrator/tests/test_cu_headless_runner.py \
    Orchestrator/tests/test_cu_golden_browser_run.py

if [[ "${1:-}" != "--live" ]]; then
    echo "Unit gate green. Add --live [backend ...] for the on-box battery."
    exit 0
fi
shift
BACKENDS=("$@")
[[ ${#BACKENDS[@]} -eq 0 ]] && BACKENDS=(anthropic openai gemini)

overall=0
for be in "${BACKENDS[@]}"; do
    echo
    echo "════════ live battery: $be / clicks ════════"
    python3 diagnostics/cu_harness/run_battery.py --backend "$be" --mode clicks \
        || overall=1
    echo
    echo "════════ live battery: $be / open-app ════════"
    python3 diagnostics/cu_harness/run_battery.py --backend "$be" --mode open-app \
        || overall=1
done

echo
if [[ $overall -eq 0 ]]; then
    echo "CU VERIFY: ALL GREEN"
else
    echo "CU VERIFY: FAILURES ABOVE"
fi
exit $overall
