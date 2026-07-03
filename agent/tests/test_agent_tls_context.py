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