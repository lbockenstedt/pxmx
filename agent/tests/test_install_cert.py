"""Tests for ProxmoxAgent.install_cert — the hypervisor INSTALL_CERT target.

The le spoke (via the hub) pushes a Let's Encrypt cert here; the agent writes
the PEM fullchain + unencrypted privkey to root-only (0600) temp files and runs
``pvenode cert set <cert> <key> --force --restart``, which installs the cert on
this node's pveproxy and restarts it. The agent runs ON a node, so it installs
on its local node only; the spoke routes INSTALL_CERT to the agent that owns
the target node. The private key is written to a 0600 temp file pvenode reads
then unlinked — never logged.

Self-contained: loads agent.py as a synthetic package so its relative imports
(``from .dep_guard``, ``from . import cs_commands``) resolve, with dep-guard
self-heal disabled (``LM_DEP_GUARD_DISABLE=1``) so the test interpreter isn't
pip-installed into. The agent's install-time INSTALL_UUID write is stubbed (no
.env side effect). Mirrors the le/opnsense install_cert test style.
"""
import os
# Disable dep-guard self-heal BEFORE importing the agent module so the test
# interpreter isn't pip-installed into. dep_guard honors LM_DEP_GUARD_DISABLE=1.
os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"
# Synthetic package so agent.py's relative imports resolve.
_pkg = types.ModuleType("pxmx_agent_src")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "pxmx_agent_src.agent", SRC / "agent.py",
    submodule_search_locations=[str(SRC)])
agent = importlib.util.module_from_spec(_spec)
sys.modules["pxmx_agent_src.agent"] = agent
_spec.loader.exec_module(agent)
ProxmoxAgent = agent.ProxmoxAgent
# Stub the install-time INSTALL_UUID write so construction has no .env side
# effect (a plain function assigned as a class attr is bound as a method).
ProxmoxAgent._ensure_install_uuid = lambda self: ""  # noqa: E731

_FC = "-----BEGIN CERTIFICATE-----\nLEAF\n-----END CERTIFICATE-----\n"
_FK = "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b"", on_communicate=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._on_communicate = on_communicate

    async def communicate(self):
        if self._on_communicate:
            self._on_communicate()
        return self._stdout, self._stderr


def _patch_exec(monkeypatch_fn):
    """Redirect agent.asyncio.create_subprocess_exec to ``monkeypatch_fn`` for
    one call. Restores the real one after so other tests aren't affected."""
    real = agent.asyncio.create_subprocess_exec
    agent.asyncio.create_subprocess_exec = monkeypatch_fn
    return real


def test_install_cert_runs_pvenode_force_restart_writes_0600_then_unlinks():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    captured = {}

    async def fake_exec(bin_, *args, **kwargs):
        captured["argv"] = [bin_, *args]
        cert_path, key_path = args[2], args[3]
        captured["cert_path"], captured["key_path"] = cert_path, key_path
        with open(cert_path) as f:
            captured["cert"] = f.read()
        with open(key_path) as f:
            captured["key"] = f.read()
        captured["cert_mode"] = os.stat(cert_path).st_mode & 0o777
        captured["key_mode"] = os.stat(key_path).st_mode & 0o777
        return _FakeProc(0)

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, _FK))
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert res["status"] == "SUCCESS"
    # pvenode cert set <cert> <key> --force --restart
    assert captured["argv"][:3] == ["pvenode", "cert", "set"]
    assert captured["argv"][3] == captured["cert_path"]
    assert captured["argv"][4] == captured["key_path"]
    assert "--force" in captured["argv"]
    assert "--restart" in captured["argv"]
    assert captured["cert"] == _FC.strip()
    assert captured["key"] == _FK.strip()
    # Temp files are root-only (0600) while pvenode reads them...
    assert captured["cert_mode"] == 0o600
    assert captured["key_mode"] == 0o600
    # ...and unlinked once pvenode returns (key not left on disk).
    assert not os.path.exists(captured["cert_path"])
    assert not os.path.exists(captured["key_path"])


def test_install_cert_missing_fullchain_errors_without_subprocess():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    called = []

    async def fake_exec(*a, **k):
        called.append(True)
        return _FakeProc(0)

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert("", _FK))
        assert res["status"] == "ERROR"
        assert "fullchain" in res["message"]
        res2 = _run(a.install_cert("not a cert", _FK))
        assert res2["status"] == "ERROR"
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert called == []  # no subprocess invoked on bad input


def test_install_cert_missing_privkey_errors_without_subprocess():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    called = []

    async def fake_exec(*a, **k):
        called.append(True)
        return _FakeProc(0)

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, ""))
        assert res["status"] == "ERROR"
        assert "private key" in res["message"]
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert called == []


def test_install_cert_pvenode_nonzero_exit_reports_stderr_and_cleans_up():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    paths = {}

    async def fake_exec(bin_, *args, **kwargs):
        paths["cert"], paths["key"] = args[2], args[3]
        return _FakeProc(1, stderr=b"error: invalid certificate format")

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, _FK))
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    assert "invalid certificate format" in res["message"]
    # Temp files still cleaned up on the error path.
    assert not os.path.exists(paths["cert"])
    assert not os.path.exists(paths["key"])


def test_install_cert_pvenode_missing_reports_clear_error():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")

    async def fake_exec(bin_, *args, **kwargs):
        raise FileNotFoundError(2, "No such file", bin_)

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, _FK))
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    assert "pvenode not found" in res["message"]