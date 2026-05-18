"""Tests for Orchestrator/update/changes.py — file-path → action-bucket mapping.

Pure function tests, no I/O, no git fixtures needed. The categorize() output
shape is contract-stable (UI badges depend on the bucket keys) so changes
to bucket names should break these tests loudly.
"""
from Orchestrator.update.changes import categorize, all_buckets


def test_empty_input_returns_only_code_only():
    """Nothing changed → no buckets trigger except the always-on code_only."""
    result = categorize([])
    assert result["code_only"] is True
    for bucket in all_buckets():
        if bucket != "code_only":
            assert result[bucket] is False, f"empty input lit {bucket}"


def test_apt_bucket_triggered_by_system_packages_change():
    result = categorize(["Scripts/onboarding/system-packages.txt"])
    assert result["apt"] is True
    assert result["pip"] is False
    assert result["mcp_pip"] is False
    assert result["code_only"] is True  # always on


def test_pip_bucket_triggered_by_root_requirements_only():
    """Plain requirements.txt at root → pip bucket. MCP/requirements.txt
    should NOT also trigger pip (only mcp_pip)."""
    result = categorize(["requirements.txt"])
    assert result["pip"] is True
    assert result["mcp_pip"] is False


def test_mcp_pip_bucket_triggered_by_mcp_requirements():
    result = categorize(["MCP/requirements.txt"])
    assert result["mcp_pip"] is True
    assert result["pip"] is False


def test_systemd_bucket_triggered_by_install_sh_change():
    """install.sh houses the systemd unit + drop-in heredocs, so any
    change to it triggers the systemd-regen path."""
    result = categorize(["Scripts/install.sh"])
    assert result["systemd"] is True


def test_sudoers_bucket_triggered_by_sudoers_template():
    result = categorize(["installer/templates/sudoers-blackbox-system"])
    assert result["sudoers"] is True


def test_helpers_bucket_triggered_by_helper_script():
    for helper in ("installer/templates/blackbox-apt-install.sh",
                   "installer/templates/blackbox-write-systemd.sh"):
        result = categorize([helper])
        assert result["helpers"] is True, f"{helper} did not trigger helpers"


def test_unrelated_file_only_triggers_code_only():
    """A random Python or HTML file → only code_only, no system-level work."""
    result = categorize(["Orchestrator/routes/chat_routes.py", "Portal/index.html"])
    assert result["code_only"] is True
    for bucket in all_buckets():
        if bucket != "code_only":
            assert result[bucket] is False


def test_multiple_buckets_can_trigger_simultaneously():
    """A real update PR often touches several categories. Categorize must
    light up all the matching buckets, not just one."""
    result = categorize([
        "requirements.txt",
        "Scripts/install.sh",
        "Orchestrator/routes/chat_routes.py",
    ])
    assert result["pip"] is True
    assert result["systemd"] is True
    assert result["code_only"] is True
    assert result["apt"] is False  # not in the change list


def test_categorize_accepts_any_iterable():
    """Caller might pass a generator from `git diff --name-only` parsed
    line-by-line. Don't require a list."""
    def gen():
        yield "requirements.txt"
    result = categorize(gen())
    assert result["pip"] is True


def test_all_buckets_has_all_keys_categorize_returns():
    """Ensure all_buckets() is the canonical source of bucket names —
    UI badge code can iterate it instead of duplicating the list."""
    result = categorize([])
    assert set(result.keys()) == set(all_buckets())
