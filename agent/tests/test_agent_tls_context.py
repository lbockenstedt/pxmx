"""Regression guard for the pxmx agent ``wss://`` TLS context build.

Bug (2026-07-03): ``ProxmoxAgent._connect_once`` called
``ssl.create_unverified_context()``, which is NOT a stdlib API — the public
name is ``ssl.create_default_context()`` and the unverified builder is the
PRIVATE ``ssl._create_unverified_context()`` (leading underscore). The
AttributeError was caught by the surrounding try/except, ``ssl_ctx`` fell to
``None``, and ``websockets`` then refused ``ssl=None`` on a ``wss://`` URI
("ssl=None is incompatible with a wss:// URI"), so the agent retry-looped
forever (5s → 10s → 20s …) and never connected:

    WARNING - wss SSL context build failed: module 'ssl' has no attribute
              'create_unverified_context'; connecting without TLS
    ERROR   - Unexpected error: ssl=None is incompatible with a wss:// URI

The fix mirrors ``BaseControlPlane._client_ssl_ctx`` in lm core
(``ssl._create_unverified_context()``). This test pins both the stdlib API
fact AND the agent source so the bad public name can't silently come back —
no websockets/network needed.
"""
import re
import ssl
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "src" / "agent.py"

import os  # noqa: E402
os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

import importlib.util  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"
# Synthetic package so agent.py's relative imports resolve (mirrors
# test_install_cert.py); load once at import time so the behavioral tests can
# call ProxmoxAgent._wss_ssl_context unbound (it touches no instance state).
_pkg = types.ModuleType("pxmx_agent_src_tls")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_tls"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "pxmx_agent_src_tls.agent", SRC / "agent.py",
    submodule_search_locations=[str(SRC)])
_agent_mod = importlib.util.module_from_spec(_spec)
sys.modules["pxmx_agent_src_tls.agent"] = _agent_mod
_spec.loader.exec_module(_agent_mod)
ProxmoxAgent = _agent_mod.ProxmoxAgent


def test_stdlib_ssl_has_private_unverified_builder_not_public():
    # The unverified builder exists ONLY under the private underscore name.
    assert hasattr(ssl, "_create_unverified_context"), (
        "ssl._create_unverified_context must exist (the unverified builder)")
    assert not hasattr(ssl, "create_unverified_context"), (
        "ssl.create_unverified_context is NOT a real stdlib API — calling it "
        "raises AttributeError and breaks the wss:// connect (the bug this guards)")
    # The verified path the agent also uses must exist.
    assert hasattr(ssl, "create_default_context")


def test_unverified_context_is_cert_none_check_hostname_false():
    ctx = ssl._create_unverified_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_agent_source_uses_private_unverified_builder():
    src = AGENT.read_text()
    # The agent must call the private underscore builder for the unverified
    # path (matches BaseControlPlane._client_ssl_ctx in lm core).
    assert re.search(r"_ssl\._create_unverified_context\(\)", src), (
        "agent.py must call ssl._create_unverified_context() (private) for the "
        "unverified wss:// path, not the non-existent ssl.create_unverified_context()")
    # And must NOT actually INVOKE the bad public name. A real call is
    # `_ssl.create_unverified_context(` (no underscore between dot and `create`).
    # The explanatory comment mentions the bad name but never invokes it, so
    # there must be zero call sites. (The regex intentionally does not match
    # the correct `_ssl._create_unverified_context(` — the extra `_` after the
    # dot breaks the match.)
    bad_calls = re.findall(r"(?<![A-Za-z_])_ssl\.create_unverified_context\(", src)
    assert not bad_calls, (
        f"agent.py invokes the non-existent ssl.create_unverified_context() "
        f"({len(bad_calls)} call site(s)) — must be _create_unverified_context()")


# ── _wss_ssl_context — behavioral (verify path selection) ──────────────────
# The decision was inlined in _connect_once; it's now a method so the LE
# system-store fallback + the refuse-to-downgrade-on-missing-CA rule are
# unit-testable without websockets/network.

def _ctx(monkeypatch, url, verify=None, ca=None):
    for k in ("LM_HUB_TLS_VERIFY", "LM_HUB_CA_CERT"):
        monkeypatch.delenv(k, raising=False)
    if verify is not None:
        monkeypatch.setenv("LM_HUB_TLS_VERIFY", verify)
    if ca is not None:
        monkeypatch.setenv("LM_HUB_CA_CERT", ca)
    return ProxmoxAgent._wss_ssl_context(object(), url)


def _real_ca_pem(tmp_path):
    """A valid self-signed CA PEM so create_default_context(cafile=…) succeeds."""
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
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lm-test-ca")])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    p = tmp_path / "ca.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(p)


def test_wss_verify_off_returns_unverified_context(monkeypatch):
    ctx, mode = _ctx(monkeypatch, "wss://hub:443/ws/agent", verify="0")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert "unverified" in mode


def test_wss_verify_on_no_ca_uses_system_trust_store(monkeypatch):
    """LM_HUB_TLS_VERIFY=1 with no LM_HUB_CA_CERT → system trust store (the LE
    fix): an LE-signed spoke cert verifies without a pinned CA file. Previously
    the agent fell to unverified here, so the agent→spoke leg couldn't verify
    an LE cert without explicitly setting LM_HUB_CA_CERT."""
    ctx, mode = _ctx(monkeypatch, "wss://hub:443/ws/agent", verify="1")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode != ssl.CERT_NONE  # verification is ON
    assert "system trust store" in mode


def test_wss_verify_on_with_ca_pins_cafile(monkeypatch, tmp_path):
    ca = _real_ca_pem(tmp_path)
    ctx, mode = _ctx(monkeypatch, "wss://hub:443/ws/agent", verify="1", ca=ca)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode != ssl.CERT_NONE
    assert f"CA={ca}" in mode


def test_wss_verify_on_missing_ca_path_refuses_downgrade(monkeypatch, tmp_path):
    """An operator who asked for verification must NOT be silently downgraded
    to unverified when the pinned CA path doesn't exist — return None (websockets
    refuses ssl=None on wss:// → agent retry-loops with the error logged)."""
    ctx, mode = _ctx(monkeypatch, "wss://hub:443/ws/agent",
                     verify="1", ca=str(tmp_path / "nope.pem"))
    assert ctx is None
    assert "CA path missing" in mode


def test_ws_plaintext_url_returns_none_context(monkeypatch):
    ctx, mode = _ctx(monkeypatch, "ws://127.0.0.1:8443/ws/agent", verify="1")
    assert ctx is None
    assert "plaintext" in mode