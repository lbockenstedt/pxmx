"""discovery.resolve_agent_url — "supply only an IP, auto-determine the rest".

The operator gives just a spoke IP (``--spoke-ip``); the agent must work out the
scheme + port + ``/ws/agent`` path by probing the spoke's known listener
endpoints. These tests exercise the real shipped ``discovery.py`` (pure stdlib,
so it imports directly) against throwaway local servers that answer the WebSocket
``101 Switching Protocols`` upgrade:

  * ``_strip_to_host`` reduces a bare IP / pasted URL / host:port to a host.
  * ``_probe_ws_upgrade`` returns True only when a WS server actually answers 101.
  * ``resolve_agent_url`` picks the first live (scheme, port) candidate and
    disambiguates TLS from plaintext (a wss probe only succeeds against a
    cert-bearing listener; a ws probe only against a plaintext one).
"""
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import discovery  # noqa: E402


def _serve_101(port, tls_cert=None):
    """A minimal server that answers one line of WebSocket upgrade (101) and
    closes. Returns the listening socket (caller closes it to stop)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            if tls_cert:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(*tls_cert)
                try:
                    conn = ctx.wrap_socket(conn, server_side=True)
                except Exception:
                    _close(conn)
                    continue
            try:
                conn.recv(1024)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\n\r\n")
            except Exception:
                pass
            _close(conn)

    threading.Thread(target=loop, daemon=True).start()
    return srv


def _close(sock):
    try:
        sock.close()
    except Exception:
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("value,expected", [
    ("172.16.1.50", "172.16.1.50"),
    ("wss://172.16.1.50:443/ws/agent", "172.16.1.50"),
    ("ws://172.16.1.50:8767", "172.16.1.50"),
    ("172.16.1.50:8767", "172.16.1.50"),
    ("  172.16.1.50  ", "172.16.1.50"),
    ("host.local", "host.local"),
    ("[::1]:443", "::1"),
])
def test_strip_to_host(value, expected):
    assert discovery._strip_to_host(value) == expected


def test_probe_ws_upgrade_true_against_101_server():
    port = _free_port()
    srv = _serve_101(port)
    try:
        assert discovery._probe_ws_upgrade("127.0.0.1", port, use_tls=False,
                                           timeout=2.0) is True
    finally:
        _close(srv)


def test_probe_ws_upgrade_false_on_closed_port():
    assert discovery._probe_ws_upgrade("127.0.0.1", _free_port(),
                                       use_tls=False, timeout=1.0) is False


def test_resolve_picks_live_plaintext_candidate(monkeypatch):
    port = _free_port()
    srv = _serve_101(port)
    # A dead port first, then the live one — resolve must skip the dead and
    # return the live plaintext endpoint with the /ws/agent path appended.
    monkeypatch.setattr(discovery, "_AGENT_LISTENER_CANDIDATES",
                        [("wss", _free_port()), ("ws", port)])
    try:
        assert discovery.resolve_agent_url("127.0.0.1", timeout=3.0) == \
            f"ws://127.0.0.1:{port}/ws/agent"
    finally:
        _close(srv)


def test_resolve_returns_none_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(discovery, "_AGENT_LISTENER_CANDIDATES",
                        [("wss", _free_port()), ("ws", _free_port())])
    assert discovery.resolve_agent_url("127.0.0.1", timeout=2.0) is None


def test_resolve_returns_none_for_empty_host():
    assert discovery.resolve_agent_url("", timeout=1.0) is None


def test_resolve_disambiguates_tls_scheme(monkeypatch):
    """A wss candidate must only match a cert-bearing listener — proving scheme
    is determined by probing, not guessed."""
    if not _have_openssl():
        pytest.skip("openssl not available to mint a self-signed cert")
    tmp = tempfile.mkdtemp()
    crt, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-keyout", key, "-out", crt, "-days", "1", "-nodes",
                    "-subj", "/CN=127.0.0.1"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = _free_port()
    srv = _serve_101(port, tls_cert=(crt, key))
    monkeypatch.setattr(discovery, "_AGENT_LISTENER_CANDIDATES", [("wss", port)])
    try:
        assert discovery.resolve_agent_url("127.0.0.1", timeout=3.0) == \
            f"wss://127.0.0.1:{port}/ws/agent"
    finally:
        _close(srv)


def _have_openssl():
    try:
        subprocess.run(["openssl", "version"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
