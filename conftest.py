"""Root conftest — puts repo root on sys.path so `from Orchestrator...` works."""
import os
import sys
import time
from pathlib import Path

import pytest

_repo_root = str(Path(__file__).parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Tests run against THIS checkout. install.sh appends BLACKBOX_ROOT=<install> to
# the service's .env (and the service's child processes inherit it), and
# config.py's load_dotenv() walks up from a nested .claude/worktrees/* checkout
# into that .env, so without this pin a worktree run would open the LIVE task DB
# and run the tasks-DB migration on the live tree. load_dotenv never overrides a
# variable that is already set.
os.environ["BLACKBOX_ROOT"] = _repo_root

# ---------------------------------------------------------------------------
# Cross-suite settle: live-service tests (chat stream regression, agent WS)
# each leave background Gemini inferences running. Without a brief pause the
# next test file starts while the service event-loop is still saturated and
# WS handshakes time out.  3 seconds is enough for FastAPI to drain the SSE
# task from the previous test before a new WS connection arrives.
# ---------------------------------------------------------------------------
_LIVE_SERVICE_MODULES = {
    "tests/test_chat_stream_retrieval_regression.py",
    "tests/test_agent_route_retrieval.py",
    "tests/test_voice_route_retrieval.py",
}

@pytest.fixture(autouse=True, scope="module")
def _settle_between_live_tests(request):
    yield
    if request.fspath.relto(_repo_root) in _LIVE_SERVICE_MODULES:
        time.sleep(3)
