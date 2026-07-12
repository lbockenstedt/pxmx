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
    # pvenode exited non-zero AND the cert is not on disk → a genuine failure.
    a._pveproxy_cert_matches = lambda fc: False
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


def test_install_cert_pvenode_nonzero_exit_but_cert_on_disk_reports_success():
    # The "cert deployed but UI shows failed" bug: pvenode writes the cert files
    # BEFORE restarting pveproxy, so a slow/among-warning restart can make pvenode
    # exit non-zero (e.g. "command 'systemctl restart pveproxy' failed") while the
    # cert IS already on disk. The fingerprint check is authoritative — deployed
    # cert → SUCCESS, so the spoke/hub don't mark a successful deploy as failed.
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    a._pveproxy_cert_matches = lambda fc: True

    async def fake_exec(bin_, *args, **kwargs):
        return _FakeProc(24, stderr=b"command 'systemctl restart pveproxy' failed: exit code 1")

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, _FK))
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert res["status"] == "SUCCESS"
    assert "verified on disk" in res["message"]


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


# ── slow pveproxy restart: verify deployed cert by fingerprint ───────────────
# pvenode writes the cert files BEFORE restarting pveproxy; on a loaded node the
# restart can outlive our wait. Success can't hinge on a fixed timeout (we can't
# predict install/restart time), so on timeout the agent verifies the deployed
# cert by fingerprint — SUCCESS if it's on disk, ERROR only if it genuinely isn't.

class _SlowProc:
    """A fake pvenode whose communicate() never returns within the wait window,
    simulating a pveproxy restart that outlives the timeout."""
    def __init__(self, delay=10.0):
        self._delay = delay
        self.returncode = None

    async def communicate(self):
        await asyncio.sleep(self._delay)
        return b"", b""

    def kill(self):
        pass


def _patch_wait_timeout(agent_instance, seconds=0.05):
    """Lower the pvenode wait window so the timeout path fires fast in tests."""
    agent_instance._PVENODE_WAIT_TIMEOUT = seconds


def test_install_cert_slow_restart_with_cert_on_disk_reports_success():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    _patch_wait_timeout(a)
    # Simulate "pvenode wrote the cert, restart still going" → fingerprint match.
    a._pveproxy_cert_matches = lambda fc: True

    async def fake_exec(*args, **kwargs):
        return _SlowProc(delay=10.0)

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, _FK))
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert res["status"] == "SUCCESS"
    assert "verified" in res["message"]


def test_install_cert_slow_restart_without_cert_reports_error():
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    _patch_wait_timeout(a)
    # pvenode timed out AND the cert never landed on disk.
    a._pveproxy_cert_matches = lambda fc: False

    async def fake_exec(*args, **kwargs):
        return _SlowProc(delay=10.0)

    real = _patch_exec(fake_exec)
    try:
        res = _run(a.install_cert(_FC, _FK))
    finally:
        agent.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    assert "timed out" in res["message"]


def test_install_cert_wait_is_generous_ten_minutes():
    # Regression guard: the wait must stay generous (10 min) so a slow pveproxy
    # restart isn't falsely failed. A typo to e.g. 60s would re-introduce the
    # "cert deployed but reported ERROR" bug.
    assert ProxmoxAgent._PVENODE_WAIT_TIMEOUT == 600.0


def test_leaf_der_fingerprint_garbage_returns_none():
    # No PEM cert block at all → no regex match → None (must not raise).
    assert ProxmoxAgent._leaf_der_fingerprint("not a pem at all") is None
    assert ProxmoxAgent._leaf_der_fingerprint("") is None
    assert ProxmoxAgent._leaf_der_fingerprint("-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n") is None


def _real_cert_pair():
    """Generate a real self-signed cert+key PEM (for fingerprint round-trip).
    Skips the test if `cryptography` isn't installed."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime
    except Exception:
        import pytest
        pytest.skip("cryptography not installed")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lm-test")])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    fullchain = cert.public_bytes(serialization.Encoding.PEM).decode()
    privkey = key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    return fullchain, privkey


def test_pveproxy_cert_matches_real_cert_round_trip(tmp_path):
    fullchain, _ = _real_cert_pair()
    a = ProxmoxAgent(spoke_url="ws://x", agent_id="a1", secret="s")
    # Point the helper at a temp file and confirm a matching cert → True,
    # a different cert → False.
    deployed = tmp_path / "pveproxy-ssl.pem"
    deployed.write_text(fullchain)
    a._PVEPROXY_CERT_PATH = str(deployed)
    assert a._pveproxy_cert_matches(fullchain) is True
    # A different cert on disk → no match.
    other, _ = _real_cert_pair()
    deployed.write_text(other)
    assert a._pveproxy_cert_matches(fullchain) is False
    # Missing file → False (not an exception).
    a._PVEPROXY_CERT_PATH = str(tmp_path / "does-not-exist.pem")
    assert a._pveproxy_cert_matches(fullchain) is False