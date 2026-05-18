from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from Orchestrator.cli_agent.session_manager import (
    POST_ATTACH_HOOKS,
    _post_attach_hook_for,
)


def test_claude_post_attach_hook_sends_tui_fullscreen():
    hook = _post_attach_hook_for("claude")
    assert hook is not None
    assert hook == ["/tui fullscreen", "Enter"]


def test_gemini_post_attach_hook_is_noop():
    assert _post_attach_hook_for("gemini") is None


def test_codex_post_attach_hook_is_noop():
    assert _post_attach_hook_for("codex") is None


def test_unknown_provider_post_attach_hook_is_noop():
    assert _post_attach_hook_for("definitely-unknown") is None


def test_post_attach_hooks_keys_are_subset_of_supported_providers():
    from Orchestrator.routes.cli_agent_routes import SUPPORTED_PROVIDERS
    assert set(POST_ATTACH_HOOKS.keys()) <= set(SUPPORTED_PROVIDERS), (
        "POST_ATTACH_HOOKS contains a provider not in SUPPORTED_PROVIDERS"
    )


def test_attach_or_create_schedules_send_keys_for_claude(tmp_path):
    from Orchestrator.cli_agent.path_validator import PathValidator
    from Orchestrator.cli_agent.operator_config import OperatorConfig
    from Orchestrator.cli_agent.session_manager import TmuxSessionManager

    apps_root = tmp_path / "Apps"
    apps_root.mkdir()
    cfg_root = tmp_path / "cfg"
    pv = PathValidator(apps_root=apps_root)
    oc = OperatorConfig(root=cfg_root)
    mgr = TmuxSessionManager(path_validator=pv, operator_config=oc)

    with patch.object(mgr, "_schedule_send_keys") as mock_schedule, \
         patch("Orchestrator.cli_agent.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(mgr, "has_session", return_value=False):
            mgr.attach_or_create(
                operator="testop", provider="claude",
                app="", command=["/bin/true"],
            )
    mock_schedule.assert_called_once()
    args, _ = mock_schedule.call_args
    assert args[1] == ["/tui fullscreen", "Enter"]


def test_attach_or_create_does_not_send_keys_for_gemini(tmp_path):
    from Orchestrator.cli_agent.path_validator import PathValidator
    from Orchestrator.cli_agent.operator_config import OperatorConfig
    from Orchestrator.cli_agent.session_manager import TmuxSessionManager

    apps_root = tmp_path / "Apps"
    apps_root.mkdir()
    cfg_root = tmp_path / "cfg"
    pv = PathValidator(apps_root=apps_root)
    oc = OperatorConfig(root=cfg_root)
    mgr = TmuxSessionManager(path_validator=pv, operator_config=oc)

    with patch.object(mgr, "_schedule_send_keys") as mock_schedule, \
         patch("Orchestrator.cli_agent.session_manager.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(mgr, "has_session", return_value=False):
            mgr.attach_or_create(
                operator="testop", provider="gemini",
                app="", command=["/bin/true"],
            )
    mock_schedule.assert_not_called()


def test_augmented_spawn_path_includes_nvm_dirs(tmp_path, monkeypatch):
    from Orchestrator.cli_agent.session_manager import _augmented_spawn_path

    base = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v18.20.0", "v22.1.0"):
        bin_dir = base / ver / "bin"
        bin_dir.mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = _augmented_spawn_path()
    parts = result.split(":")
    assert "/usr/bin" in parts
    assert any(p.endswith("/v22.1.0/bin") for p in parts), result
    assert any(p.endswith("/v18.20.0/bin") for p in parts), result
    # newest must come before older (PATH search order)
    v22_idx = next(i for i, p in enumerate(parts) if p.endswith("/v22.1.0/bin"))
    v18_idx = next(i for i, p in enumerate(parts) if p.endswith("/v18.20.0/bin"))
    assert v22_idx < v18_idx, f"newest nvm dir not first: {parts}"


def test_augmented_spawn_path_works_without_nvm(tmp_path, monkeypatch):
    from Orchestrator.cli_agent.session_manager import _augmented_spawn_path
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = _augmented_spawn_path()
    assert "/usr/bin" in result.split(":")
