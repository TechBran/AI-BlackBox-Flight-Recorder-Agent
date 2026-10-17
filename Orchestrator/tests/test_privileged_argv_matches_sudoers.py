"""The service's privileged argv must be exactly what the sudoers grants match.

2026-10-09 audit X04-security-clients-infra-02 (rank 17): the
`tailscale up --accept-dns=true --hostname=*` and `tailscale cert *` grants
ended in a glob. sudoers matches the arguments as ONE space-joined string, so
the `*` also matched extra root-run flags (`--login-server=…`, `--ssh`,
`--cert-file=/etc/…`). The template now anchors both with `^…$` regexes; the
`journalctl -u blackbox.service *` grant is gone (the service reads its
journal as a systemd-journal member).

These tests read the REAL template and check both directions:
  * the argv tailscale_actuator builds is permitted by a grant, for every
    hostname / DNSName shape the box can produce;
  * the hostile variants a prompt-injected caller would send are not;
  * request_cert refuses a DNSName that is not a plain *.ts.net name before
    it ever reaches sudo;
  * /onboarding/logs/stream reads the journal WITHOUT sudo, and tells the
    operator how to fix it when the service user cannot read system entries.

Matching mirrors sudo: a `^…$` argument is a POSIX ERE over the whole joined
argument string (the patterns here use only the subset where Python `re`
fullmatch is identical); any other argument string is a glob (fnmatch, no
FNM_PATHNAME), which is exactly how the old wildcard grants matched.
"""
import asyncio
import fnmatch
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from Orchestrator.onboarding import tailscale_actuator as ta

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "installer" / "templates" / "sudoers-blackbox-system"
TAILSCALE = "/usr/bin/tailscale"


def _grants() -> list[tuple[str, str]]:
    grants = []
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.fullmatch(r"REAL_USER_PLACEHOLDER ALL=\(root\) NOPASSWD: (/\S+)(?: (.+))?", s)
        assert m, f"unrecognised sudoers line: {line!r}"
        grants.append((m.group(1), m.group(2) or ""))
    return grants


def _permitted(cmd: str, args: list[str]) -> bool:
    """Would sudo let the service run `cmd args…` as root under the template?"""
    joined = " ".join(args)
    for g_cmd, g_args in _grants():
        if g_cmd != cmd:
            continue
        if not g_args:                       # no args in sudoers = ANY args
            return True
        if g_args == '""':
            if not args:
                return True
        elif g_args.startswith("^") and g_args.endswith("$"):
            if re.fullmatch("(?:" + g_args[1:-1] + ")", joined):
                return True
        elif fnmatch.fnmatchcase(joined, g_args):
            return True
    return False


# ── tailscale up ──────────────────────────────────────────────────────────────

class _FakeUpProc:
    """`sudo tailscale up` stand-in: prints the login URL, then stays up."""

    def __init__(self):
        self.returncode = None
        lines = [b"To authenticate, visit:\n",
                 b"\thttps://login.tailscale.com/a/abc123XYZ\n"]

        async def readline():
            return lines.pop(0) if lines else b""

        self.stdout = SimpleNamespace(readline=readline)

    def terminate(self):
        pass


def _capture_up_argv(monkeypatch, hostname: str) -> list[str]:
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _FakeUpProc()

    monkeypatch.setattr(ta.socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(ta.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout="{}", stderr=""))
    monkeypatch.setattr(ta.subprocess, "Popen", lambda *a, **k: None)   # xdg-open
    monkeypatch.setattr(ta.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ta, "_active_up_process", None)
    monkeypatch.setattr(ta, "_active_up_login_url", None)
    url = asyncio.run(ta.start_up())
    assert url == "https://login.tailscale.com/a/abc123XYZ"
    assert len(calls) == 1
    return calls[0]


@pytest.mark.parametrize("hostname", [
    "ai-black-box-fc-A620AI-WiFi",          # this dev box
    "bbx-MS02.local",                        # dots and case
    "My_Box",                                # underscore
    "---",                                   # sanitises to nothing -> fallback
    "x" * 64,                                # Linux HOST_NAME_MAX
    "a" * 62 + "-b",                         # truncation lands on a hyphen
])
def test_tailscale_up_argv_is_permitted_by_the_anchored_grant(monkeypatch, hostname):
    argv = _capture_up_argv(monkeypatch, hostname)
    assert argv[:3] == ["sudo", "-n", TAILSCALE]
    assert argv[3:5] == ["up", "--accept-dns=true"]
    assert _permitted(TAILSCALE, argv[3:]), f"sudoers would refuse {argv}"


@pytest.mark.parametrize("args", [
    "up --accept-dns=true --hostname=box --login-server=https://evil.example",
    "up --accept-dns=true --hostname=box --ssh",
    "up --accept-dns=true --hostname=box --auth-key=tskey-auth-x",
    "up --accept-dns=true --hostname=box --reset --advertise-exit-node",
    "up --accept-dns=true --hostname=box --operator=nobody",
    "up --accept-dns=true --hostname=",
])
def test_tailscale_up_grant_rejects_extra_root_flags(args):
    assert not _permitted(TAILSCALE, args.split(" ")), args


# ── tailscale cert ────────────────────────────────────────────────────────────

def _run_request_cert(monkeypatch, dns_name: str):
    calls = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"Wrote public cert\n", b""

    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _Proc()

    status = json.dumps({"Self": {"DNSName": dns_name}})
    monkeypatch.setattr(ta.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout=status, stderr=""))
    monkeypatch.setattr(ta.asyncio, "create_subprocess_exec", fake_exec)
    return asyncio.run(ta.request_cert()), calls


@pytest.mark.parametrize("dns_name", [
    "ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net.",
    "bbx-ms02.tail-net.ts.net.",
])
def test_tailscale_cert_argv_is_permitted_by_the_anchored_grant(monkeypatch, dns_name):
    res, calls = _run_request_cert(monkeypatch, dns_name)
    assert res["ok"] is True
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == ["sudo", "-n", TAILSCALE, "cert"]
    assert argv[4] == dns_name.rstrip(".")
    assert _permitted(TAILSCALE, argv[3:]), f"sudoers would refuse {argv}"


@pytest.mark.parametrize("dns_name", [
    "box.tail1.ts.net --cert-file=/etc/shadow",
    "--cert-file=/etc/sudoers.d/zz",
    "box.tail1.ts.net.evil.example.",
    "../../etc/passwd",
    "BOX.tail1.ts.net",
])
def test_tailscale_cert_refuses_an_unexpected_dnsname_before_sudo(monkeypatch, dns_name):
    res, calls = _run_request_cert(monkeypatch, dns_name)
    assert res["ok"] is False
    assert calls == [], f"request_cert spawned {calls} for DNSName {dns_name!r}"


@pytest.mark.parametrize("args", [
    "cert --cert-file=/etc/sudoers.d/zz box.tail1.ts.net",
    "cert box.tail1.ts.net --cert-file=/etc/shadow",
    "cert box.tail1.ts.net --key-file=/root/.ssh/authorized_keys",
    "cert /etc/shadow",
])
def test_tailscale_cert_grant_rejects_output_file_flags(args):
    assert not _permitted(TAILSCALE, args.split(" ")), args


# ── journal access ────────────────────────────────────────────────────────────

def test_sudoers_template_no_longer_grants_journalctl():
    assert not [g for g in _grants() if g[0].endswith("/journalctl")]


class _FakeJournalProc:
    def __init__(self, lines):
        self._lines = list(lines)
        self.returncode = None

        async def readline():
            return self._lines.pop(0) if self._lines else b""

        self.stdout = SimpleNamespace(readline=readline)

    def terminate(self):
        pass


def _stream_logs(monkeypatch, *, groups, euid=1000):
    import grp
    import os

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from Orchestrator.routes import onboarding_routes as ob

    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _FakeJournalProc([b"Oct 17 07:01:10 box blackbox[1]: hello\n"])

    gids = {"systemd-journal": 999, "adm": 4}

    def fake_getgrnam(name):
        if name not in gids:
            raise KeyError(name)
        return SimpleNamespace(gr_gid=gids[name], gr_name=name)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    monkeypatch.setattr(os, "getegid", lambda: 1000)
    monkeypatch.setattr(os, "getgroups", lambda: list(groups))
    monkeypatch.setattr(grp, "getgrnam", fake_getgrnam)

    app = FastAPI()
    app.include_router(ob.router)
    body = TestClient(app).get("/onboarding/logs/stream?lines=50").text
    return body, calls


def test_logs_stream_reads_the_journal_without_sudo(monkeypatch):
    body, calls = _stream_logs(monkeypatch, groups=[1000, 999])
    assert len(calls) == 1
    argv = calls[0]
    assert "sudo" not in argv, f"logs stream still escalates: {argv}"
    assert Path(argv[0]).name == "journalctl"
    assert argv[1:3] == ["-u", "blackbox.service"]
    assert "--follow" in argv and "--no-pager" in argv
    assert argv[argv.index("--lines") + 1] == "50"
    assert "hello" in body
    assert "systemd-journal" not in body          # member: no hint


def test_logs_stream_names_the_fix_when_the_journal_is_unreadable(monkeypatch):
    body, calls = _stream_logs(monkeypatch, groups=[1000])
    assert len(calls) == 1 and "sudo" not in calls[0]
    assert "systemd-journal" in body
    assert "Scripts/install.sh" in body
    assert "hello" in body                         # still streams what it can


def test_logs_stream_accepts_adm_membership(monkeypatch):
    body, _ = _stream_logs(monkeypatch, groups=[1000, 4])
    assert "systemd-journal" not in body
