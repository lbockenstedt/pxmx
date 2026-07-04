"""ProxmoxAgent._normalize_spoke_url — pinned spoke_url defaulting.

agent.py can't be imported directly in a lightweight test env (it does
package-relative imports — ``from .dep_guard import ...`` — that only resolve
inside the real ``agent`` package, mirroring the other tests in this dir).
_normalize_spoke_url has no side effects and depends only on stdlib
urllib.parse, so it's extracted from the real source file and exec'd in
isolation — this tests the actual shipped code, not a reimplementation.

Covers "assume wss:// and 443 unless otherwise stated": a pinned --spoke-url/
SPOKE_URL given as a bare IP, host:port, or a URL missing the /ws/agent
suffix must resolve to a fully-qualified wss://host:443/ws/agent (or preserve
whatever piece WAS explicitly given), since websockets.connect() dials the
URL verbatim with no rewriting of its own.
"""
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

AGENT = Path(__file__).resolve().parent.parent / "src" / "agent.py"


def _load_normalize_spoke_url():
    """Extract _normalize_spoke_url (+ its constants) from the real agent.py
    source and exec it in an isolated namespace, so this test exercises the
    actual shipped function body rather than a hand-copied duplicate."""
    src = AGENT.read_text()
    m = re.search(
        r"_AGENT_WS_PATH = .*?\n_AGENT_DEFAULT_SCHEME = .*?\n"
        r"_AGENT_DEFAULT_PORT = .*?\n\n\n"
        r"def _normalize_spoke_url\(.*?\n(?:    .*\n|\n)*",
        src,
    )
    assert m, "could not locate _normalize_spoke_url in agent.py — source shape changed?"
    ns = {"urlsplit": urlsplit, "urlunsplit": urlunsplit, "Optional": Optional}
    exec(compile(m.group(0), str(AGENT), "exec"), ns)
    return ns["_normalize_spoke_url"]


_norm = _load_normalize_spoke_url()


def test_sentinels_pass_through():
    assert _norm(None) is None
    assert _norm("") == ""
    assert _norm("auto") == "auto"


def test_bare_ip_defaults_scheme_port_and_path():
    assert _norm("172.16.1.36") == "wss://172.16.1.36:443/ws/agent"


def test_bare_ip_with_explicit_port_keeps_it_and_appends_path():
    assert _norm("172.16.1.36:8443") == "wss://172.16.1.36:8443/ws/agent"


def test_bare_ip_with_path_defaults_scheme_and_port():
    assert _norm("172.16.1.36/ws/agent") == "wss://172.16.1.36:443/ws/agent"


def test_wss_no_port_defaults_to_443():
    assert _norm("wss://172.16.1.36") == "wss://172.16.1.36:443/ws/agent"


def test_wss_with_port_no_path_appends_path():
    assert _norm("wss://172.16.1.36:443") == "wss://172.16.1.36:443/ws/agent"


def test_fully_specified_url_is_unchanged():
    assert (_norm("wss://172.16.1.36:443/ws/agent")
            == "wss://172.16.1.36:443/ws/agent")


def test_trailing_slash_is_normalized():
    assert (_norm("wss://172.16.1.36:443/ws/agent/")
            == "wss://172.16.1.36:443/ws/agent")


def test_explicit_ws_scheme_is_preserved_on_nonstandard_port():
    # An explicit ws:// (plaintext, e.g. a legacy no-cert :8766 fallback) is
    # respected — only the missing port/path get defaulted, the scheme choice
    # itself is not overridden the way BaseControlPlane upgrades ws://:443.
    assert _norm("ws://172.16.1.36:8766") == "ws://172.16.1.36:8766/ws/agent"


def test_explicit_ws_scheme_with_no_port_still_defaults_port():
    assert _norm("ws://172.16.1.36") == "ws://172.16.1.36:443/ws/agent"
