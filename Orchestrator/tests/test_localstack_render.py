"""Orchestrator/localstack_render.py — the ONE writer of the live llama-swap
config (audit O19-local-stack-09, 2026-08-11 plan Track B).

Before it, two writers fought over ~/.blackbox/localstack/llama-swap-config.yaml
with no coordination: the installer re-ran a sed over the template on every
run (resetting every keep-warm member to ttl 600), set_member_ttl rewrote the
file through yaml.safe_dump (dropping every comment), and a committed template
fix reached a box only when someone re-ran the installer by hand. Now the
installer, set_member_ttl and the Orchestrator's boot reconcile all render
template + keep-warm overrides through the same function.

The golden fixture is the config deployed on MS02 (fetched read-only,
2026-10-23): rendering today's template for that box must reproduce it byte
for byte, so landing this renderer rewrites nothing there.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from Orchestrator import localstack_render as lr

REPO = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "fixtures" / "llama-swap-config.ms02.yaml"
MS02_HOME = "/home/bbx/.blackbox/localstack"
MS02_ROOT = "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main"
MEMBERS = ("embed-qwen3-8b", "rerank-qwen3-8b", "speaches", "qwen-tts")


def _template() -> str:
    return lr.template_path(str(REPO)).read_text(encoding="utf-8")


def test_golden_render_equals_the_config_deployed_on_ms02():
    assert lr.render(_template(), MS02_HOME, MS02_ROOT) == GOLDEN.read_text(encoding="utf-8")


def test_render_matches_the_installer_sed_it_replaces():
    """Same six substitutions, same order, nothing else: ${PORT},
    ${llama-server} and ${models-dir} stay for llama-swap."""
    out = lr.render(_template(), "/h/.blackbox/localstack", "/r")
    assert "${LOCALSTACK" not in out and "${SPEACHES_VENV}" not in out
    assert "${QWEN_TTS_VENV}" not in out and "${LOCALMODELS}" not in out
    assert "${PORT}" in out and "${llama-server}" in out and "${models-dir}" in out
    assert "PYTHONPATH=/r/LocalModels exec /h/.blackbox/localstack/qwen-tts-venv/bin/uvicorn" in out


def test_rendered_yaml_parses_and_lists_every_member():
    cfg = yaml.safe_load(lr.render(_template(), MS02_HOME, MS02_ROOT))
    assert tuple(cfg["models"]) == MEMBERS
    assert {m: v["ttl"] for m, v in cfg["models"].items()} == dict.fromkeys(MEMBERS, 600)


def test_template_defaults_are_read_from_the_template():
    assert lr.member_ttls(_template()) == dict.fromkeys(MEMBERS, 600)


def test_ttl_override_edits_only_that_member_and_keeps_every_comment():
    base = lr.render(_template(), MS02_HOME, MS02_ROOT)
    warm = lr.render(_template(), MS02_HOME, MS02_ROOT, {"embed-qwen3-8b": 0})
    cfg = yaml.safe_load(warm)
    assert cfg["models"]["embed-qwen3-8b"]["ttl"] == 0
    assert all(cfg["models"][m]["ttl"] == 600 for m in MEMBERS[1:])
    changed = [(a, b) for a, b in zip(base.splitlines(), warm.splitlines()) if a != b]
    assert len(changed) == 1                       # one line, nothing else touched
    old, new = changed[0]
    assert old.split("#")[1] == new.split("#")[1]  # its comment survives
    assert old.index("#") == new.index("#")        # ... in the same column
    assert base.count("#") == warm.count("#")      # safe_dump used to drop them all


def test_unknown_member_or_bad_ttl_is_refused():
    with pytest.raises(ValueError):
        lr.render(_template(), MS02_HOME, MS02_ROOT, {"not-a-member": 0})
    for bad in (-1, "warm", 1.5, True):
        with pytest.raises(ValueError):
            lr.render(_template(), MS02_HOME, MS02_ROOT, {"embed-qwen3-8b": bad})


# ── reconcile: what the installer, set_member_ttl and boot all run ───────────

@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home" / ".blackbox" / "localstack"
    home.mkdir(parents=True)
    return home


def test_reconcile_writes_a_fresh_box(box):
    res = lr.reconcile(str(box), str(REPO))
    dest = box / "llama-swap-config.yaml"
    assert res["changed"] is True and Path(res["path"]) == dest
    assert dest.read_text(encoding="utf-8") == lr.render(_template(), str(box), str(REPO))
    assert oct(dest.stat().st_mode & 0o777) == "0o644"
    assert not list(box.glob("*.tmp"))


def test_reconcile_is_a_no_op_when_the_live_file_already_matches(box):
    lr.reconcile(str(box), str(REPO))
    dest = box / "llama-swap-config.yaml"
    before = dest.stat().st_mtime_ns
    assert lr.reconcile(str(box), str(REPO))["changed"] is False
    # --watch-config restarts the whole proxy on ANY write, so an unchanged
    # render must not touch the file.
    assert dest.stat().st_mtime_ns == before


def test_reconcile_reverts_a_hand_patch_to_the_template(box):
    """The Aug 2026 fixes were sed'ed into live files; a hand edit that breaks
    the `cmd: |` block survived until the next reboot. The template wins."""
    lr.reconcile(str(box), str(REPO))
    dest = box / "llama-swap-config.yaml"
    dest.write_text(dest.read_text(encoding="utf-8").replace("-b 8192", "-b 512"), encoding="utf-8")
    res = lr.reconcile(str(box), str(REPO))
    assert res["changed"] is True and res["lines_changed"] >= 1
    assert "-b 512" not in dest.read_text(encoding="utf-8")


def test_reconcile_keeps_keep_warm_choices_across_re_renders(box):
    lr.set_ttl_override(str(box), "speaches", 0)
    res = lr.reconcile(str(box), str(REPO))
    cfg = yaml.safe_load((box / "llama-swap-config.yaml").read_text(encoding="utf-8"))
    assert cfg["models"]["speaches"]["ttl"] == 0
    assert res["ttl_overrides"] == {"speaches": 0}
    # back to the template default removes the override instead of pinning 600
    lr.set_ttl_override(str(box), "speaches", 600)
    assert json.loads((box / lr.OVERRIDES_NAME).read_text())["ttl"] == {}


def test_first_reconcile_adopts_keep_warm_from_an_old_live_config(box):
    """A box whose keep-warm was set through the old yaml.safe_dump path has
    no overrides file: its ttl choices are harvested, not reset to 600 (the
    installer re-run used to do exactly that)."""
    old = yaml.safe_load(lr.render(_template(), str(box), str(REPO)))
    old["models"]["embed-qwen3-8b"]["ttl"] = 0
    (box / "llama-swap-config.yaml").write_text(yaml.safe_dump(old, sort_keys=False),
                                                encoding="utf-8")
    res = lr.reconcile(str(box), str(REPO))
    assert res["ttl_overrides"] == {"embed-qwen3-8b": 0}
    cfg = yaml.safe_load((box / "llama-swap-config.yaml").read_text(encoding="utf-8"))
    assert cfg["models"]["embed-qwen3-8b"]["ttl"] == 0
    assert "#" in (box / "llama-swap-config.yaml").read_text(encoding="utf-8")  # comments back
    assert json.loads((box / lr.OVERRIDES_NAME).read_text())["ttl"] == {"embed-qwen3-8b": 0}


def test_a_corrupt_overrides_file_is_not_silently_treated_as_empty(box):
    (box / lr.OVERRIDES_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        lr.reconcile(str(box), str(REPO))
    assert not (box / "llama-swap-config.yaml").exists()


# ── the Orchestrator side: keep-warm + the boot step ─────────────────────────

@pytest.fixture
def installed(box, monkeypatch):
    from Orchestrator import local_stack
    lr.reconcile(str(box), str(REPO))
    dest = box / "llama-swap-config.yaml"
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: dest)
    return local_stack, dest


def test_keep_warm_survives_an_installer_re_run(installed, box):
    """O19-local-stack-09: the installer's step 5 used to regenerate the file
    from the template and silently reset every keep-warm member to 600."""
    local_stack, dest = installed
    local_stack.set_member_ttl("embed-qwen3-8b", local_stack.TTL_WARM)
    assert local_stack.get_member_ttl("embed-qwen3-8b") == 0
    lr.main(["--localstack-home", str(box), "--blackbox-root", str(REPO)])  # the installer
    assert local_stack.get_member_ttl("embed-qwen3-8b") == 0
    assert "# keep-warm" in dest.read_text(encoding="utf-8")       # comments kept


def test_boot_reconcile_lands_a_template_change(installed, monkeypatch, caplog):
    local_stack, dest = installed
    dest.write_text(dest.read_text(encoding="utf-8").replace("-ub 8192", "-ub 512"),
                    encoding="utf-8")
    caplog.set_level("WARNING", logger=local_stack.logger.name)
    res = local_stack.reconcile_llama_swap_config()
    assert res["changed"] is True
    assert "-ub 512" not in dest.read_text(encoding="utf-8")
    assert any("re-rendered from the template" in r.getMessage() for r in caplog.records)
    assert local_stack.reconcile_llama_swap_config()["changed"] is False


def test_boot_reconcile_never_raises_and_leaves_the_file_alone(installed, box, caplog):
    local_stack, dest = installed
    before = dest.read_text(encoding="utf-8")
    (box / lr.OVERRIDES_NAME).write_text("{broken", encoding="utf-8")
    assert local_stack.reconcile_llama_swap_config() is None
    assert dest.read_text(encoding="utf-8") == before
    assert any("could not re-render" in r.getMessage() for r in caplog.records)


def test_boot_reconcile_is_a_no_op_without_the_stack(monkeypatch):
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: None)
    assert local_stack.reconcile_llama_swap_config() is None


def test_boot_sweep_says_loudly_when_the_driver_is_dead(monkeypatch, caplog):
    from Orchestrator import hardware, local_stack
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: None)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "driver_ok": False,
        "gpu_reason": "NVIDIA driver not responding: RTX 2000 Ada is on the PCI bus"})
    caplog.set_level("WARNING", logger=local_stack.logger.name)
    local_stack.boot_sweep()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert errors[0].getMessage().startswith("[GPU] preflight failed: NVIDIA driver not responding")


def test_boot_sweep_is_quiet_on_a_healthy_or_cpu_box(monkeypatch, caplog):
    from Orchestrator import hardware, local_stack
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: None)
    for hw in ({"gpu": True, "driver_ok": True, "gpu_reason": None},
               {"gpu": False, "driver_ok": None, "gpu_reason": None}):
        monkeypatch.setattr(hardware, "probe", lambda *a, _hw=hw, **k: dict(_hw))
        caplog.clear()
        caplog.set_level("WARNING", logger=local_stack.logger.name)
        local_stack.boot_sweep()
        assert not caplog.records


def test_startup_hook_runs_the_sweep_off_the_event_loop(monkeypatch):
    """A boot hook (skipped under BLACKBOX_TESTING) whose nvidia-smi + file
    IO never blocks the event loop."""
    import asyncio
    import threading
    from Orchestrator import local_stack, startup
    from Orchestrator.utils import testing
    seen = {}
    monkeypatch.setattr(local_stack, "boot_sweep",
                        lambda: seen.setdefault("thread", threading.current_thread()))
    asyncio.run(startup.startup_local_stack_sweep())
    assert seen["thread"] is not threading.main_thread()
    hooks = [h for h in startup.app.router.on_startup
             if getattr(h, "__wrapped__", None) is startup.startup_local_stack_sweep]
    assert len(hooks) == 1 and getattr(hooks[0], testing.BOOT_HOOK_ATTR, False)


def test_cli_is_stdlib_only_and_runs_isolated(box):
    """The installer runs it with the system python3 -I, before any venv
    exists, as the box's user."""
    src = (REPO / "Orchestrator" / "localstack_render.py").read_text(encoding="utf-8")
    assert "import yaml" not in src and "from Orchestrator" not in src
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-I", str(REPO / "Orchestrator" / "localstack_render.py"),
                        "--localstack-home", str(box), "--blackbox-root", str(REPO)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "wrote" in r.stdout
    assert (box / "llama-swap-config.yaml").read_text(encoding="utf-8") == \
        lr.render(_template(), str(box), str(REPO))
    r = subprocess.run([sys.executable, "-I", str(REPO / "Orchestrator" / "localstack_render.py"),
                        "--localstack-home", str(box), "--blackbox-root", str(REPO)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "unchanged" in r.stdout


# ── one owner: the checkout the installer rendered the live config for ───────
#
# ~/.blackbox/localstack/llama-swap-config.yaml is ONE file per box, but any
# Orchestrator can boot on that box: the service from the main checkout, and a
# test backend started from a worktree (`cd <worktree> && uvicorn ... --port
# 9099`). Rendering with whichever checkout happens to be running repointed the
# qwen-tts member's PYTHONPATH into the worktree, swapped in that branch's
# template, and restarted the whole proxy (--watch-config), until the service's
# next boot flipped it back.

def _checkout(tmp_path, name, *, marker=None):
    """A second checkout with its own template (`marker` replaces the
    embedder's "-b 8192", so a render from it is recognisable)."""
    root = tmp_path / name
    tpl = lr.template_path(str(root))
    tpl.parent.mkdir(parents=True)
    text = _template()
    if marker:
        assert "-b 8192" in text
        text = text.replace("-b 8192", marker, 1)
    tpl.write_text(text, encoding="utf-8")
    return root


def _sidecar(box):
    return json.loads((box / lr.OVERRIDES_NAME).read_text(encoding="utf-8"))


def test_reconcile_records_the_checkout_it_rendered_for(box):
    lr.reconcile(str(box), str(REPO))
    assert _sidecar(box)["blackbox_root"] == str(REPO)
    assert lr.config_owner(str(box)) == str(REPO)


def test_reconcile_from_another_checkout_leaves_the_live_config_alone(box, tmp_path):
    lr.reconcile(str(box), str(REPO))
    dest = box / lr.CONFIG_NAME
    before, mtime = dest.read_bytes(), dest.stat().st_mtime_ns
    side_before = (box / lr.OVERRIDES_NAME).read_bytes()
    worktree = _checkout(tmp_path, "worktree", marker="-b 512")
    res = lr.reconcile(str(box), str(worktree))
    assert res["changed"] is False and res["owner"] == str(REPO)
    assert res["skipped"] and str(REPO) in res["skipped"]
    assert dest.read_bytes() == before and dest.stat().st_mtime_ns == mtime
    assert (box / lr.OVERRIDES_NAME).read_bytes() == side_before


def test_the_owner_of_a_pre_sidecar_config_is_its_qwen_tts_path(box):
    """MS02 and the dev box were rendered before the sidecar existed: the
    owner is the checkout the qwen-tts member's PYTHONPATH names."""
    golden = GOLDEN.read_text(encoding="utf-8")
    assert lr.config_owner(str(box), golden) == MS02_ROOT
    (box / lr.CONFIG_NAME).write_text(golden, encoding="utf-8")
    assert lr.config_owner(str(box)) == MS02_ROOT


def test_a_pre_sidecar_config_is_adopted_only_by_its_own_checkout(box, tmp_path):
    dest = box / lr.CONFIG_NAME
    dest.write_text(lr.render(_template(), str(box), str(REPO)), encoding="utf-8")
    before = dest.read_bytes()
    worktree = _checkout(tmp_path, "worktree", marker="-b 512")
    res = lr.reconcile(str(box), str(worktree))
    assert res["skipped"] and res["owner"] == str(REPO)
    assert dest.read_bytes() == before
    assert not (box / lr.OVERRIDES_NAME).exists()      # nothing claimed for the worktree
    res = lr.reconcile(str(box), str(REPO))
    assert res["skipped"] is None and res["changed"] is False
    assert dest.read_bytes() == before
    assert _sidecar(box) == {"blackbox_root": str(REPO), "ttl": {}}


def test_a_config_whose_owner_cannot_be_told_is_not_rewritten_at_boot(box):
    """No sidecar and no qwen-tts path to read the checkout from: a boot
    reconcile must not guess (it could be a worktree's)."""
    dest = box / lr.CONFIG_NAME
    dest.write_text("models:\n  \"embed-qwen3-8b\":\n    ttl: 600\n", encoding="utf-8")
    res = lr.reconcile(str(box), str(REPO))
    assert res["changed"] is False and res["owner"] is None and res["skipped"]
    assert "re-run" in res["skipped"]
    assert dest.read_text(encoding="utf-8").startswith("models:")


def test_the_installer_moves_the_config_to_its_checkout(box, tmp_path):
    """An installer run is the operator saying "this checkout": it claims the
    config, whoever owned it."""
    lr.reconcile(str(box), str(REPO))
    lr.set_ttl_override(str(box), "speaches", 0, blackbox_root=str(REPO))
    moved = _checkout(tmp_path, "moved")
    assert lr.main(["--localstack-home", str(box), "--blackbox-root", str(moved)]) == 0
    text = (box / lr.CONFIG_NAME).read_text(encoding="utf-8")
    assert f"PYTHONPATH={moved}/LocalModels exec" in text
    assert _sidecar(box) == {"blackbox_root": str(moved), "ttl": {"speaches": 0}}


def test_a_symlinked_path_to_the_owner_is_the_same_checkout(box, tmp_path):
    lr.reconcile(str(box), str(REPO))
    before = (box / lr.CONFIG_NAME).read_bytes()
    link = tmp_path / "repo-link"
    link.symlink_to(REPO)
    res = lr.reconcile(str(box), str(link))
    assert res["skipped"] is None and res["changed"] is False
    assert (box / lr.CONFIG_NAME).read_bytes() == before


def test_set_ttl_override_keeps_the_recorded_owner(box):
    lr.reconcile(str(box), str(REPO))
    lr.set_ttl_override(str(box), "qwen-tts", 0, blackbox_root=str(REPO))
    assert _sidecar(box) == {"blackbox_root": str(REPO), "ttl": {"qwen-tts": 0}}


def test_an_owner_that_is_not_a_path_makes_the_sidecar_unreadable(box):
    (box / lr.OVERRIDES_NAME).write_text('{"ttl": {}, "blackbox_root": 7}', encoding="utf-8")
    with pytest.raises(ValueError):
        lr.reconcile(str(box), str(REPO))


@pytest.fixture
def worktree_process(installed, tmp_path, monkeypatch):
    """An Orchestrator booted from a worktree on a box whose live config the
    main checkout (REPO here) installed."""
    from Orchestrator.utils import paths
    worktree = _checkout(tmp_path, "worktree", marker="-b 512")
    monkeypatch.setattr(paths, "blackbox_root", lambda: worktree)
    return installed[0], installed[1], worktree


def test_boot_reconcile_from_a_worktree_never_rewrites_the_live_config(worktree_process, caplog):
    local_stack, dest, worktree = worktree_process
    before, mtime = dest.read_bytes(), dest.stat().st_mtime_ns
    caplog.set_level("WARNING", logger=local_stack.logger.name)
    res = local_stack.reconcile_llama_swap_config()
    assert res["changed"] is False and res["skipped"]
    assert dest.read_bytes() == before and dest.stat().st_mtime_ns == mtime
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert str(REPO) in warnings[0] and str(worktree) in warnings[0]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_keep_warm_from_a_worktree_renders_for_the_owning_checkout(worktree_process):
    local_stack, dest, worktree = worktree_process
    local_stack.set_member_ttl("embed-qwen3-8b", local_stack.TTL_WARM)
    assert local_stack.get_member_ttl("embed-qwen3-8b") == 0
    text = dest.read_text(encoding="utf-8")
    assert f"PYTHONPATH={REPO}/LocalModels exec" in text
    assert str(worktree) not in text
    assert "-b 512" not in text                       # REPO's template, not the worktree's
    assert dest.read_text(encoding="utf-8") == lr.render(
        _template(), str(dest.parent), str(REPO), {"embed-qwen3-8b": 0})
