"""G2-T9 (M2.1) — headless CLI-agent runner + TaskType.CLI_AGENT.

Builds the runner that makes `claude`, `gemini`, `codex` callable as background
tasks (T10 will wrap them as YOLO ToolVault tools). This suite proves, WITHOUT
ever invoking a real agent (fake #!/bin/sh executables only):

  * The exact per-provider argv (flags verified from --help).
  * A REAL per-provider environment strip — proven by spawning a child and
    grepping its env for the secret (mutation target b).
  * Spawn in its own process group (start_new_session=True) so the T8
    process-group cancel actually reaps the agent's children (mutation target a).
  * Bounded tail / bounded mint — raw agent stdout never reaches the immutable
    ledger unbounded (mutation target c).
  * TaskType.CLI_AGENT is dispatched by process_task (mutation target d).
  * Per-type concurrency budget: CLI agents get their own slice (>1) that never
    starves image/TTS/video, and vice versa.

Fully offline: no real claude/gemini/codex run, no network, in-memory fake db.
"""
import os
import signal
import stat
import subprocess
import threading
import time

import pytest

import Orchestrator.tasks as tasks_mod
from Orchestrator.models import Task, TaskStatus, TaskType
from Orchestrator.volume import now_utc_iso
from Orchestrator.cli_agent import headless


# ---------------------------------------------------------------------------
# Fixtures (mirror test_task_cancellation.py)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_task_db(monkeypatch):
    store = {}

    class FakeDB:
        def get_task(self, task_id):
            return store.get(task_id)

        def save_task(self, task):
            store[task.task_id] = task

        def get_all_tasks(self, operator=None):
            vals = list(store.values())
            if operator:
                vals = [t for t in vals if t.operator == operator]
            return vals

    monkeypatch.setattr(tasks_mod, "task_db", FakeDB())
    return store


@pytest.fixture(autouse=True)
def _clean_registry():
    tasks_mod._cancel_handles.clear()
    tasks_mod._cancel_requested.clear()
    yield
    tasks_mod._cancel_handles.clear()
    tasks_mod._cancel_requested.clear()


def _seed(store, task_id="c-1", task_type=TaskType.CLI_AGENT,
          status=TaskStatus.PROCESSING, operator="Brandon", prompt="refactor x",
          result_data=None):
    now = now_utc_iso()
    task = Task(task_id=task_id, task_type=task_type, status=status,
                created_at=now, updated_at=now, operator=operator, prompt=prompt,
                result_data=result_data or {})
    store[task_id] = task
    return task


def _make_exe(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# ===========================================================================
# 1. TaskType.CLI_AGENT
# ===========================================================================
def test_tasktype_has_cli_agent():
    assert TaskType.CLI_AGENT.value == "cli_agent"


# ===========================================================================
# 2. argv builders — flags verified from --help
# ===========================================================================
def test_claude_argv():
    argv = headless.build_argv("claude", "do the thing", "opus", "/work",
                               permission_mode="yolo", bin_path="/bin/claude")
    assert argv == [
        "/bin/claude", "-p", "--output-format", "stream-json", "--verbose",
        "--model", "opus", "--dangerously-skip-permissions", "do the thing",
    ]


def test_gemini_argv_default_omits_model():
    # gemini is NOT a class resolver — default (model=None) omits --model so the
    # CLI uses its own configured default.
    argv = headless.build_argv("gemini", "do the thing", None, "/work",
                               permission_mode="yolo", bin_path="/bin/gemini")
    assert argv == [
        "/bin/gemini", "--output-format", "stream-json",
        "--approval-mode", "yolo", "-p", "do the thing",
    ]
    assert "--model" not in argv


def test_gemini_argv_concrete_model_passthrough():
    argv = headless.build_argv("gemini", "p", "gemini-2.5-pro", "/work",
                               permission_mode="yolo", bin_path="/bin/gemini")
    assert argv == [
        "/bin/gemini", "--output-format", "stream-json",
        "--model", "gemini-2.5-pro", "--approval-mode", "yolo", "-p", "p",
    ]


def test_codex_argv_default_omits_model():
    argv = headless.build_argv("codex", "do the thing", None, "/work",
                               permission_mode="yolo", bin_path="/bin/codex")
    assert argv == [
        "/bin/codex", "exec", "--json", "--skip-git-repo-check", "-C", "/work",
        "--dangerously-bypass-approvals-and-sandbox", "do the thing",
    ]
    assert "--model" not in argv


def test_codex_argv_concrete_model_passthrough():
    argv = headless.build_argv("codex", "p", "gpt-5-codex", "/work",
                               permission_mode="yolo", bin_path="/bin/codex")
    assert argv == [
        "/bin/codex", "exec", "--json", "--skip-git-repo-check", "-C", "/work",
        "--model", "gpt-5-codex",
        "--dangerously-bypass-approvals-and-sandbox", "p",
    ]


def test_argv_default_mode_omits_dangerous_flags():
    for prov, model, bad in (("claude", "opus", "--dangerously-skip-permissions"),
                             ("gemini", None, "yolo"),
                             ("codex", None, "--dangerously-bypass-approvals-and-sandbox")):
        argv = headless.build_argv(prov, "p", model, "/w",
                                   permission_mode="default", bin_path="/b")
        assert bad not in argv, f"{prov} default mode leaked {bad}"


def test_argv_rejects_unknown_provider():
    with pytest.raises(ValueError):
        headless.build_argv("bard", "p", None, "/w", bin_path="/b")


def test_claude_rejects_unknown_model_class():
    # claude IS a class resolver — validate the alias, never invent a version.
    with pytest.raises(ValueError):
        headless.build_argv("claude", "p", "pro", "/w", bin_path="/b")


def test_gemini_codex_accept_any_concrete_model():
    # gemini/codex are NOT class resolvers — any non-empty string forwards verbatim.
    g = headless.build_argv("gemini", "p", "some-future-model", "/w", bin_path="/b")
    assert "some-future-model" in g
    c = headless.build_argv("codex", "p", "another-model", "/w", bin_path="/b")
    assert "another-model" in c


def test_argv_rejects_non_string_model():
    with pytest.raises(ValueError):
        headless.build_argv("gemini", "p", 123, "/w", bin_path="/b")


def test_claude_model_classes_match_spec():
    assert set(headless.CLAUDE_MODEL_CLASSES) == {"fable", "opus", "sonnet", "haiku"}
    # gemini/codex have no validated class set (they are not resolvers).
    assert not hasattr(headless, "MODEL_CLASSES")


# ===========================================================================
# 3. env strip — PROVEN with a real child (mutation target b)
# ===========================================================================
def test_gemini_env_strip_child_cannot_see_google_keys():
    base = dict(os.environ)
    base["GOOGLE_API_KEY"] = "SEKRET_GOOGLE"
    base["GEMINI_API_KEY"] = "SEKRET_GEMINI"
    env = headless.build_child_env("gemini", base_env=base)
    out = subprocess.run(["/usr/bin/env"], env=env,
                         capture_output=True, text=True).stdout
    assert "SEKRET_GOOGLE" not in out
    assert "SEKRET_GEMINI" not in out
    assert "GOOGLE_API_KEY" not in out
    assert "GEMINI_API_KEY" not in out


def test_gemini_env_keeps_application_credentials():
    """Control + Vertex decision: GOOGLE_APPLICATION_CREDENTIALS is KEPT (matches
    the zellij denylist which keeps it for Vertex). Also proves the child DOES
    see env, so the strips above are real, not a broken child."""
    base = dict(os.environ)
    base["GOOGLE_API_KEY"] = "SEKRET_GOOGLE"
    base["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/vertex-creds.json"
    env = headless.build_child_env("gemini", base_env=base)
    out = subprocess.run(["/usr/bin/env"], env=env,
                         capture_output=True, text=True).stdout
    assert "/tmp/vertex-creds.json" in out
    assert "SEKRET_GOOGLE" not in out


def test_codex_env_strip_child_cannot_see_openai_key():
    """PROVEN via codex doctor: a live OPENAI_API_KEY flips codex to API-key
    billing and overrides the ChatGPT subscription. Strip it."""
    base = dict(os.environ)
    base["OPENAI_API_KEY"] = "SEKRET_OPENAI"
    env = headless.build_child_env("codex", base_env=base)
    out = subprocess.run(["/usr/bin/env"], env=env,
                         capture_output=True, text=True).stdout
    assert "SEKRET_OPENAI" not in out
    assert "OPENAI_API_KEY" not in out


def test_claude_env_strip_child_cannot_see_anthropic_key():
    base = dict(os.environ)
    base["ANTHROPIC_API_KEY"] = "SEKRET_ANTHROPIC"
    env = headless.build_child_env("claude", base_env=base)
    out = subprocess.run(["/usr/bin/env"], env=env,
                         capture_output=True, text=True).stdout
    assert "SEKRET_ANTHROPIC" not in out
    assert "ANTHROPIC_API_KEY" not in out


def test_env_strip_is_scoped_per_provider():
    """claude strips only ANTHROPIC; gemini's GOOGLE key survives in claude's env
    (each provider strips only what IT would bill on)."""
    base = dict(os.environ)
    base["ANTHROPIC_API_KEY"] = "A"
    base["GOOGLE_API_KEY"] = "G"
    base["OPENAI_API_KEY"] = "O"
    claude_env = headless.build_child_env("claude", base_env=base)
    assert "ANTHROPIC_API_KEY" not in claude_env
    assert claude_env.get("GOOGLE_API_KEY") == "G"
    assert claude_env.get("OPENAI_API_KEY") == "O"


def test_child_env_has_augmented_path():
    """The spawn PATH must include the nvm node bin dir so an nvm-installed CLI's
    `#!/usr/bin/env node` shebang resolves."""
    from Orchestrator.cli_agent.path_extension import nvm_node_bin_dirs
    env = headless.build_child_env("gemini", base_env=dict(os.environ))
    dirs = nvm_node_bin_dirs()
    if dirs:
        assert dirs[0] in env["PATH"]


# ===========================================================================
# 4. spawn in own process group + cancel kills it (mutation target a)
# ===========================================================================
def _run_in_thread(**kwargs):
    outcome = {}

    def worker():
        outcome["result"] = headless.run_cli_agent(**kwargs)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t, outcome


def test_run_cli_agent_child_is_own_group_leader_and_cancel_kills(fake_task_db, tmp_path):
    """start_new_session=True makes the child its OWN group leader (pgid == pid).
    If that flag is removed (the mutation), the child inherits the worker's
    process group and getpgid(pid) != pid → this assertion fails BEFORE we ever
    call cancel_task (so the mutation can never nuke the test runner's group)."""
    exe = _make_exe(tmp_path, "sleeper",
                    'echo \'{"type":"start"}\'\nsleep 300\n')
    _seed(fake_task_db, "c-kill")
    t, outcome = _run_in_thread(provider="claude", prompt="p", model="opus",
                                cwd=str(tmp_path), permission_mode="yolo",
                                task_id="c-kill", bin_path=exe, timeout=60)

    # Wait for the runner to register the process handle (with a pid).
    pid = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        h = tasks_mod._cancel_handles.get("c-kill")
        if h and h.get("pid"):
            pid = h["pid"]
            break
        time.sleep(0.02)
    assert pid is not None, "runner never registered a process cancel handle"

    try:
        # MUTATION GUARD (a): child must be its own group leader.
        assert os.getpgid(pid) == pid, "child is not its own process-group leader"

        # Now the real cancel — group kill reaps the agent.
        res = tasks_mod.cancel_task("c-kill")
        assert res["cancelled"] is True
        assert res["detail"] == "process-group-kill"
        assert fake_task_db["c-kill"].status == TaskStatus.CANCELLED

        t.join(6)
        assert not t.is_alive(), "runner thread did not exit after cancel"
    finally:
        # Mutation-safe cleanup: if start_new_session were removed (mutation a),
        # the child inherits pytest's group; killpg would nuke the test runner.
        # Only group-kill a genuine leader; otherwise kill the bare pid.
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_run_cli_agent_kills_grandchild_process(fake_task_db, tmp_path):
    """Process-GROUP kill (not bare-pid) is what reaps the agent's CHILDREN."""
    # The prompt (child_file path) is the LAST positional arg, not $1.
    exe = _make_exe(tmp_path, "forker",
                    'for a in "$@"; do last="$a"; done\n'
                    'sleep 300 &\necho "$!" > "$last"\nwait\n')
    _seed(fake_task_db, "c-gc")
    child_file = tmp_path / "childpid.txt"
    t, outcome = _run_in_thread(provider="claude", prompt=str(child_file),
                                model="opus", cwd=str(tmp_path),
                                permission_mode="yolo", task_id="c-gc",
                                bin_path=exe, timeout=60)
    # wait for grandchild pid file
    gc_pid = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if child_file.exists():
            txt = child_file.read_text().strip()
            if txt:
                gc_pid = int(txt)
                break
        time.sleep(0.02)
    assert gc_pid is not None, "fake agent never spawned a grandchild"

    try:
        tasks_mod.cancel_task("c-gc")

        def _alive(p):
            try:
                os.kill(p, 0)
                return True
            except ProcessLookupError:
                return False
        deadline = time.monotonic() + 5
        while _alive(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(gc_pid), "grandchild survived the group kill"
        t.join(6)
    finally:
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ===========================================================================
# 5. JSONL read + completion + bounded tail + timeout
# ===========================================================================
def test_run_cli_agent_reads_jsonl_success(fake_task_db, tmp_path):
    exe = _make_exe(tmp_path, "ok",
                    'echo \'{"type":"assistant","text":"working"}\'\n'
                    'echo \'{"type":"result","result":"all done"}\'\n'
                    'exit 0\n')
    _seed(fake_task_db, "c-ok")
    res = headless.run_cli_agent(provider="claude", prompt="p", model="opus",
                                 cwd=str(tmp_path), permission_mode="yolo",
                                 task_id="c-ok", bin_path=exe, timeout=30)
    assert res["success"] is True
    assert res["exit_code"] == 0
    assert res["cancelled"] is False
    assert res["timed_out"] is False
    assert "all done" in res["result_text"]


def test_run_cli_agent_nonzero_exit_is_failure(fake_task_db, tmp_path):
    exe = _make_exe(tmp_path, "boom",
                    'echo \'{"type":"error","text":"kaboom"}\'\nexit 3\n')
    _seed(fake_task_db, "c-fail")
    res = headless.run_cli_agent(provider="claude", prompt="p", model="opus",
                                 cwd=str(tmp_path), permission_mode="yolo",
                                 task_id="c-fail", bin_path=exe, timeout=30)
    assert res["success"] is False
    assert res["exit_code"] == 3


def test_run_cli_agent_tail_is_bounded(fake_task_db, tmp_path):
    exe = _make_exe(tmp_path, "flood",
                    'i=0\nwhile [ $i -lt 5000 ]; do '
                    'echo "{\\"type\\":\\"log\\",\\"n\\":$i}"; i=$((i+1)); done\n'
                    'exit 0\n')
    _seed(fake_task_db, "c-flood")
    res = headless.run_cli_agent(provider="claude", prompt="p", model="opus",
                                 cwd=str(tmp_path), permission_mode="yolo",
                                 task_id="c-flood", bin_path=exe, timeout=30)
    assert res["success"] is True
    assert len(res["tail"]) <= headless.TAIL_MAX_CHARS
    # The DB tail must be bounded too (poll-visible progress, T11 makes it nicer).
    rd = fake_task_db["c-flood"].result_data or {}
    if "tail" in rd:
        assert len(rd["tail"]) <= headless.TAIL_MAX_CHARS


def test_run_cli_agent_timeout_kills(fake_task_db, tmp_path):
    exe = _make_exe(tmp_path, "hang",
                    'echo \'{"type":"start"}\'\nsleep 300\n')
    _seed(fake_task_db, "c-to")
    start = time.monotonic()
    res = headless.run_cli_agent(provider="claude", prompt="p", model="opus",
                                 cwd=str(tmp_path), permission_mode="yolo",
                                 task_id="c-to", bin_path=exe, timeout=1.0)
    elapsed = time.monotonic() - start
    assert res["timed_out"] is True
    assert res["success"] is False
    assert elapsed < 20, "timeout did not fire promptly"


def test_run_cli_agent_surfaces_stderr_on_failure(fake_task_db, tmp_path):
    """Finding 2: gemini with its API keys stripped and no OAuth prints an auth
    error to STDERR and exits non-zero. run_cli_agent folds stderr into the
    stream, so the CLI's own error must surface in tail + result_text (not be
    swallowed)."""
    err = "When using Gemini API, you must specify the GEMINI_API_KEY environment variable."
    exe = _make_exe(tmp_path, "autherr", f'echo "{err}" 1>&2\nexit 1\n')
    _seed(fake_task_db, "c-se")
    res = headless.run_cli_agent(provider="gemini", prompt="p", model=None,
                                 cwd=str(tmp_path), permission_mode="yolo",
                                 task_id="c-se", bin_path=exe, timeout=30)
    assert res["success"] is False
    assert "GEMINI_API_KEY" in res["tail"]
    assert "GEMINI_API_KEY" in res["result_text"]


# ===========================================================================
# 6. process_cli_agent worker — mint hygiene (mutation target c)
# ===========================================================================
def _patch_run(monkeypatch, result):
    monkeypatch.setattr(headless, "run_cli_agent",
                        lambda **kwargs: result, raising=True)


def test_process_cli_agent_success_mints_bounded(fake_task_db, monkeypatch):
    """A successful CLI agent mints — but raw stdout must be BOUNDED before it
    reaches the immutable ledger (mutation target c)."""
    huge = "X" * 50000
    _seed(fake_task_db, "c-mint", prompt="P" * 5000,
          result_data={"provider": "claude", "model": "opus"})
    _patch_run(monkeypatch, {
        "success": True, "exit_code": 0, "cancelled": False, "timed_out": False,
        "result_text": huge, "tail": huge, "events": 3, "provider": "claude",
    })

    posted = []

    class _Resp:
        def raise_for_status(self):
            pass
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: (posted.append((a, k)), _Resp())[1])

    tasks_mod.process_cli_agent(fake_task_db["c-mint"])

    assert len(posted) == 1, "a successful CLI agent must auto-snapshot"
    body = posted[0][1]["json"]
    resp_text = body["assistant_response"]
    # Bounded: the 50k blob must NOT slip into the ledger whole.
    assert huge not in resp_text
    assert len(resp_text) < 3000, "mint payload is not bounded"
    assert fake_task_db["c-mint"].status == TaskStatus.COMPLETED


def test_process_cli_agent_cancelled_does_not_mint(fake_task_db, monkeypatch):
    """A cancelled CLI agent must NOT mint (precedent: the Android error-mint
    bug; mirrors process_browser_use)."""
    _seed(fake_task_db, "c-cx", result_data={"provider": "claude", "model": "opus"})
    _patch_run(monkeypatch, {
        "success": True, "exit_code": 0, "cancelled": True, "timed_out": False,
        "result_text": "partial", "tail": "partial", "events": 1, "provider": "claude",
    })
    posted = []
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: posted.append((a, k)))

    tasks_mod.request_cooperative_cancel("c-cx")
    tasks_mod.process_cli_agent(fake_task_db["c-cx"])

    assert posted == [], "cancelled CLI agent minted — must not"
    assert fake_task_db["c-cx"].status == TaskStatus.CANCELLED


def test_process_cli_agent_failure_sets_failed_no_mint(fake_task_db, monkeypatch):
    _seed(fake_task_db, "c-f", result_data={"provider": "codex", "model": "gpt"})
    _patch_run(monkeypatch, {
        "success": False, "exit_code": 2, "cancelled": False, "timed_out": False,
        "result_text": "err", "tail": "err", "events": 1, "provider": "codex",
    })
    posted = []
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: posted.append((a, k)))
    tasks_mod.process_cli_agent(fake_task_db["c-f"])
    assert fake_task_db["c-f"].status == TaskStatus.FAILED
    assert posted == [], "a failed CLI agent should not mint"


def test_process_cli_agent_failure_surfaces_cli_error(fake_task_db, monkeypatch):
    """Finding 2: a failed gemini (keys stripped, no OAuth) must land its own
    auth error in the task's error_message — not a generic 'failed' string that
    hides why. T10 turns this into a fail-fast 'authenticate the CLI' message."""
    err = "When using Gemini API, you must specify the GEMINI_API_KEY environment variable."
    _seed(fake_task_db, "c-auth", result_data={"provider": "gemini"})
    _patch_run(monkeypatch, {
        "success": False, "exit_code": 1, "cancelled": False, "timed_out": False,
        "result_text": err, "tail": err, "events": 0, "provider": "gemini",
    })
    posted = []
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: posted.append((a, k)))
    tasks_mod.process_cli_agent(fake_task_db["c-auth"])
    t = fake_task_db["c-auth"]
    assert t.status == TaskStatus.FAILED
    assert "GEMINI_API_KEY" in (t.error_message or ""), "CLI auth error was swallowed"
    assert posted == [], "a failed agent must not mint"


# ===========================================================================
# 7. process_task dispatch (mutation target d)
# ===========================================================================
def test_process_task_dispatches_cli_agent(fake_task_db, monkeypatch):
    """A CLI_AGENT task must be routed to process_cli_agent by process_task's
    if/elif switch. Remove the elif (the mutation) and this spy never fires."""
    called = {}
    monkeypatch.setattr(tasks_mod, "process_cli_agent",
                        lambda task: called.setdefault("hit", task.task_id))
    _seed(fake_task_db, "c-disp", status=TaskStatus.PENDING)
    tasks_mod.process_task(fake_task_db["c-disp"])
    assert called.get("hit") == "c-disp", "CLI_AGENT task was never dispatched"


# ===========================================================================
# 8. per-type concurrency budget
# ===========================================================================
def test_cli_concurrency_is_greater_than_one():
    """Brandon requires concurrent CLI agents ('at the same exact time')."""
    assert tasks_mod.MAX_CONCURRENT_CLI_AGENT > 1


def test_cli_budget_independent_of_media_budget():
    """A full media load (MAX_CONCURRENT of them) must NOT block a CLI agent —
    the CLI slice is its own budget."""
    active = [TaskType.IMAGE_GENERATION] * tasks_mod.MAX_CONCURRENT
    candidates = [("cli-a", TaskType.CLI_AGENT)]
    sel = tasks_mod._select_runnable(candidates, active)
    assert sel == "cli-a", "CLI agent blocked by a full media budget"


def test_media_budget_independent_of_cli_budget():
    """A full CLI slice must NOT block image/TTS/video."""
    active = [TaskType.CLI_AGENT] * tasks_mod.MAX_CONCURRENT_CLI_AGENT
    candidates = [("img-a", TaskType.IMAGE_GENERATION)]
    sel = tasks_mod._select_runnable(candidates, active)
    assert sel == "img-a", "media task blocked by a full CLI budget"


def test_cli_budget_caps_concurrent_cli_agents():
    active = [TaskType.CLI_AGENT] * tasks_mod.MAX_CONCURRENT_CLI_AGENT
    candidates = [("cli-x", TaskType.CLI_AGENT)]
    assert tasks_mod._select_runnable(candidates, active) is None


def test_no_head_of_line_blocking():
    """A blocked CLI agent at the head of the queue must not stop a runnable
    media task behind it (and vice versa) — the selector scans past it."""
    active = [TaskType.CLI_AGENT] * tasks_mod.MAX_CONCURRENT_CLI_AGENT
    candidates = [("cli-head", TaskType.CLI_AGENT), ("img-behind", TaskType.IMAGE_GENERATION)]
    assert tasks_mod._select_runnable(candidates, active) == "img-behind"


def test_selector_preserves_fifo_within_budget():
    candidates = [("a", TaskType.IMAGE_GENERATION), ("b", TaskType.VIDEO_GENERATION)]
    assert tasks_mod._select_runnable(candidates, []) == "a"


# ===========================================================================
# 9. THE REAL background_worker LOOP — budget independence end-to-end
# ===========================================================================
def test_background_worker_cli_budget_does_not_starve_media(fake_task_db, monkeypatch):
    """Drive the ACTUAL background_worker loop (not just _select_runnable):
    saturate the CLI budget with blocking CLI_AGENT tasks and assert an IMAGE
    task STILL runs. Proves the two budgets are genuinely independent and there
    is no head-of-line blocking.

    MUTATION (collapse the budgets into one shared pool): set
    _CLI_AGENT_TASK_TYPES = () so CLI counts against the shared MAX_CONCURRENT
    budget — the image task then starves and this test times out (fails)."""
    import Orchestrator.models as models_module

    if models_module.worker_running:
        pytest.skip("a background worker is already running in this process")

    with tasks_mod.task_lock:
        tasks_mod.task_queue.clear()

    release = threading.Event()
    cli_saturated = threading.Event()
    image_ran = threading.Event()
    counter = {"cli": 0}
    clock = threading.Lock()

    def fake_process_task(task):
        if task.task_type == TaskType.CLI_AGENT:
            with clock:
                counter["cli"] += 1
                if counter["cli"] >= tasks_mod.MAX_CONCURRENT_CLI_AGENT:
                    cli_saturated.set()
            release.wait(timeout=15)   # occupy the worker slot
        elif task.task_type == TaskType.IMAGE_GENERATION:
            image_ran.set()
        tasks_mod.update_task(task.task_id, status=TaskStatus.COMPLETED)

    monkeypatch.setattr(tasks_mod, "process_task", fake_process_task)

    # Enqueue MORE CLI agents than the CLI budget, then one image behind them.
    for i in range(tasks_mod.MAX_CONCURRENT_CLI_AGENT + 1):
        tasks_mod.create_task(TaskType.CLI_AGENT, operator="Brandon", prompt=f"cli {i}")
    tasks_mod.create_task(TaskType.IMAGE_GENERATION, operator="Brandon", prompt="img")

    worker = threading.Thread(target=tasks_mod.background_worker, daemon=True)
    worker.start()
    try:
        assert cli_saturated.wait(10), "CLI budget never saturated"
        assert image_ran.wait(10), \
            "IMAGE task starved by CLI agents — budgets are not independent"
    finally:
        release.set()
        models_module.worker_running = False
        worker.join(10)
        with tasks_mod.task_lock:
            tasks_mod.task_queue.clear()
