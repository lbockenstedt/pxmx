"""LM hub auto-discovery — DNS name + mDNS broadcast.

Locates the LM hub with zero config so a spoke/agent install (or runtime) does
not need an explicit ``--hub``/``--spoke-url``. Two independent paths, tried in
order:

1. **DNS** — resolve ``lm-hub.<dns-suffix>`` for each search domain in
   ``/etc/resolv.conf``, the host's own FQDN suffix, ``lm-hub.local``, and the
   bare ``lm-hub``. The hub installer sets the hub host's hostname to ``lm-hub``
   and admins create an ``lm-hub`` DNS record in their domain, so this resolves
   on any network where such a record exists (routed/VLAN'd environments where
   mDNS doesn't cross).
2. **mDNS** — browse ``_lm-hub._tcp.local.`` (the service the hub broadcasts via
   ``zeroconf``). LAN-scoped (same L2), zero DNS config required.

**Scheme selection (TLS):** the returned URL is ``ws://`` by default. When the
hub serves TLS it advertises a ``tls_port`` TXT record on its mDNS service; the
mDNS path reads it and returns ``wss://<ip>:<tls_port>`` for a REMOTE caller.
The DNS path has no TXT, so it always returns ``ws://`` — a cert-bearing hub
reachable only via DNS is pinned with ``--hub wss://host:443``.

**Same-box detection:** mDNS/DNS receipt only proves the hub is on the caller's
L2 segment, NOT on the same host (mDNS crosses the LAN). Before choosing the
endpoint we compare the discovered hub IP against this caller's OWN interface
IPs (``is_hub_local``); a co-located caller dials ``ws://127.0.0.1:<port>``
(loopback plaintext) regardless of TLS — the hub's plain listener is bound to
``127.0.0.1`` only, so a remote host cannot reach it. This keeps an all-in-one
install plaintext-loopback while anything off-box uses ``wss://``.

``agent_listener=True`` targets the hub box's pxmx agent listener (it reads the
``agent_port`` TXT instead of the spoke-WS service port, and uses ``wss`` on
that port when the hub advertises TLS).

This module is **standalone**: stdlib + an optional ``zeroconf`` import only — no
intra-repo dependencies — so the same source is vendored verbatim into the standalone pxmx
agent (``pxmx/agent/src/discovery.py``, also run by ``install_pxmx.sh``). Keep
the two copies in sync.

If ``zeroconf`` is not importable the mDNS branch is skipped and DNS-only is
used, so a missing optional dep never breaks discovery (or the hub broadcast).
"""

# base64 + os.urandom build the random Sec-WebSocket-Key for the upgrade probe.
import base64
# Runs blocking DNS lookups in a worker thread so they can be given a deadline.
import concurrent.futures
# Standard-library logging for discovery progress and failures.
import logging
import os
# Raw sockets for DNS resolution, interface discovery and the WS upgrade probe.
import socket
# TLS wrapping for probing wss:// listeners.
import ssl
# sys.exit() for the CLI entry point.
import sys
# Wall-clock deadlines for the DNS and mDNS discovery windows.
import time
# Type hints only; they document the shapes of values passed around.
from typing import Dict, List, Optional, Tuple

# One named logger so all discovery messages can be filtered together.
logger = logging.getLogger("HubDiscovery")

# The service the hub registers. Spokes browse this type to find the hub.
HUB_SERVICE_TYPE = "_lm-hub._tcp.local."
# The short name the hub is expected to use (the hub installer sets hostname
# `lm-hub`; admins create an `lm-hub` DNS record in their domain).
HUB_SHORT_NAME = "lm-hub"
# Default spoke-WS port (used when only DNS resolves — mDNS carries the real
# port). Under the unified-443 merge the hub serves the spoke-WS on the same
# 0.0.0.0:443 uvicorn as the WebUI/REST, on the /ws/spoke route.
DEFAULT_HUB_PORT = 443
# Path on the unified :443 uvicorn for the spoke-WS leg (handle_connection).
SPOKE_WS_PATH = "/ws/spoke"
# Path on the unified :443 uvicorn for the pxmx agent-WS leg. On an all-in-one
# hub the /ws/agent route is a dumb byte-proxy to the pxmx spoke's loopback
# agent listener (LM_PXMX_AGENT_PORT, 127.0.0.1 plaintext); on a standalone
# pxmx box the spoke serves /ws/agent directly on :443 wss. Either way an agent
# dials wss://<hub>:443/ws/agent.
AGENT_WS_PATH = "/ws/agent"
# Default pxmx agent-listener port (legacy/plain; superseded by the agent_port
# TXT record when the hub advertises TLS). Under the unified-443 merge the
# advertised agent_port is 443 (the external dial port); the hub's loopback dial
# port to the co-located pxmx spoke is LM_PXMX_AGENT_PORT (8443), a separate
# value NOT advertised.
DEFAULT_AGENT_PORT = 8766

# Agent-listener endpoints an agent may dial, in priority order — the (scheme,
# port) pairs that a cs spoke / pxmx spoke / all-in-one hub exposes ``/ws/agent``
# on. ``resolve_agent_url()`` probes these when the operator supplies only a
# spoke IP, so the scheme/port/path are auto-determined and never have to be
# typed. The order is "most likely first" so the common case resolves on probe 1:
#   443  wss — cs spoke standalone + all-in-one hub with a cert (install_cs.sh's
#              default; AGENT_WSS_PORT / the unified-443 external surface)
#   8767 ws  — cs spoke plaintext fallback (install_cs.sh when openssl is absent)
#   8443 wss — loopback/wss default (AGENT_LOOPBACK_PORT), rare externally
#   8766 ws  — pxmx legacy plaintext listener (AGENT_FALLBACK_PORT)
_AGENT_LISTENER_CANDIDATES: List[Tuple[str, int]] = [
    ("wss", 443),
    ("ws", 8767),
    ("wss", 8443),
    ("ws", 8766),
]

# Overridable for tests; production reads /etc/resolv.conf.
_RESOLV_CONF = "/etc/resolv.conf"


def _strip_to_host(value: str) -> str:
    """Reduce ``value`` to a bare host — so a pasted ``wss://1.2.3.4:443/ws/agent``
    and a bare ``1.2.3.4`` both collapse to ``1.2.3.4``. IPv4/hostnames only
    (bracketed IPv6 is unwrapped best-effort)."""
    # Treat None as empty and trim surrounding whitespace.
    host = (value or "").strip()
    # Drop a leading scheme such as "wss://".
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]          # drop any /ws/agent path
    host = host.strip()
    if host.startswith("["):              # [::1]:443 → ::1
        return host[1:].split("]", 1)[0]
    # Exactly one colon means host:port, so drop the port; more colons means a bare
    # IPv6 address, which is returned untouched.
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _probe_ws_upgrade(host: str, port: int, use_tls: bool,
                      path: str = AGENT_WS_PATH, timeout: float = 2.0) -> bool:
    """True if a WebSocket server answers an Upgrade handshake at ``host:port``
    over the given transport (TLS or plain).

    Dependency-free (raw socket + ssl, no ``websockets`` import) so it works in
    any environment discovery runs in. It stops at the HTTP ``101 Switching
    Protocols`` line and closes — it never sends the agent handshake, so it
    leaves no pending-approval registration on the spoke. Probing ``wss`` vs
    ``ws`` on the right port disambiguates the scheme: a TLS wrap only completes
    against a cert-bearing listener, and a plain GET only gets ``101`` from a
    plaintext one.
    """
    # Tracked outside the try so the finally block can always close it.
    sock = None
    try:
        # Open the TCP connection and bound every later read/write by the same timeout.
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        # For a wss:// candidate, run the TLS handshake before speaking HTTP.
        if use_tls:
            # cs/hub use self-signed certs → verify off (mirrors the agent's
            # own ssl._create_unverified_context() dial path).
            ctx = ssl._create_unverified_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # RFC 6455 requires a random 16-byte base64 key in the upgrade request.
        key = base64.b64encode(os.urandom(16)).decode()
        # Minimal HTTP/1.1 WebSocket upgrade request for the agent path.
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        # Only the status line matters; 64 bytes covers "HTTP/1.1 101 Switching ...".
        status_line = sock.recv(64)
        return b" 101 " in status_line
    # Refused, timed out, TLS mismatch, reset...: all mean "not a listener here".
    except Exception:
        return False
    # Always release the socket, ignoring errors from closing a broken connection.
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def resolve_agent_url(host: str, timeout: float = 5.0) -> Optional[str]:
    """Given only a spoke IP/host, return the full agent-WS URL to dial
    (e.g. ``wss://1.2.3.4:443/ws/agent``) by probing the known listener
    endpoints in priority order, or ``None`` if none answer.

    This is what lets an operator supply just an IP (``--spoke-ip``) and have
    the scheme + port + ``/ws/agent`` path determined automatically — the
    WebSocket contract lives here in code, not in the operator's hands.
    """
    # Accept a pasted URL as well as a bare IP/hostname.
    host = _strip_to_host(host)
    # Nothing usable to probe.
    if not host:
        return None
    # Split the overall budget across candidates, clamped to 1-2 s per probe.
    per_probe = max(1.0, min(2.0, timeout / max(1, len(_AGENT_LISTENER_CANDIDATES))))
    # Try each known (scheme, port) in priority order; the first that answers wins.
    for scheme, port in _AGENT_LISTENER_CANDIDATES:
        if _probe_ws_upgrade(host, port, use_tls=(scheme == "wss"),
                             timeout=per_probe):
            url = f"{scheme}://{host}:{port}{AGENT_WS_PATH}"
            logger.info("resolved agent listener: %s", url)
            return url
    # Every candidate failed: log what was tried so the operator can debug.
    logger.warning("no agent listener answered at %s (tried %s)", host,
                   ", ".join(f"{s}:{p}" for s, p in _AGENT_LISTENER_CANDIDATES))
    return None


def _resolv_search_domains() -> List[str]:
    """Parse the ``search`` line from /etc/resolv.conf (best-effort)."""
    domains: List[str] = []
    try:
        # Scan every line; a "search" line lists one or more domains after the keyword.
        with open(_RESOLV_CONF, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "search":
                    domains.extend(parts[1:])
    # Missing/unreadable resolv.conf (e.g. non-Linux): just use no search domains.
    except Exception:
        pass
    return domains


def _dns_candidates() -> List[str]:
    """Hostnames to try for ``lm-hub``, in priority order (deduped)."""
    cands: List[str] = []
    # Highest priority: lm-hub.<domain> for each resolv.conf search domain.
    for dom in _resolv_search_domains():
        # Strip a trailing root dot so "example.com." becomes "example.com".
        dom = dom.rstrip(".")
        if dom:
            cands.append(f"{HUB_SHORT_NAME}.{dom}")
    # The host's own domain suffix (e.g. this box is on mydomain.com → lm-hub.mydomain.com).
    try:
        fqdn = socket.getfqdn()
        # Only an FQDN with a dot has a domain suffix to borrow.
        if "." in fqdn:
            suffix = fqdn.split(".", 1)[1].rstrip(".")
            # Skip generic placeholder suffixes; ".local" is added explicitly below.
            if suffix and suffix not in ("local", "localdomain", "lan"):
                cands.append(f"{HUB_SHORT_NAME}.{suffix}")
    # Hostname lookup failure just means this candidate is skipped.
    except Exception:
        pass
    cands.append(f"{HUB_SHORT_NAME}.local")   # mDNS host name (Avahi/.local)
    cands.append(HUB_SHORT_NAME)              # bare — relies on search domains / admin DNS
    # De-duplicate while keeping the first (highest-priority) occurrence.
    seen = set()
    out: List[str] = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _resolve_host(name: str, timeout: float = 1.0) -> Optional[str]:
    """Resolve ``name`` to a non-loopback IPv4 string, or None.

    ``socket.getaddrinfo`` has no per-call timeout, so run it in a worker with a
    deadline — a hung resolver (e.g. a black-holed DNS server) must not stall the
    whole discovery window.
    """
    try:
        # IPv4-only lookup in a single worker thread; wait at most ``timeout`` seconds.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(socket.getaddrinfo, name, None, socket.AF_INET,
                            socket.SOCK_STREAM)
            infos = fut.result(timeout=timeout)
    # NXDOMAIN, resolver error or timeout: treat the name as unresolvable.
    except Exception:
        return None
    # Each getaddrinfo entry ends with a sockaddr tuple whose first item is the IP.
    for entry in infos or []:
        try:
            sockaddr = entry[4]
            ip = sockaddr[0]
        # Skip any malformed entry rather than failing the whole lookup.
        except Exception:
            continue
        # A loopback answer (e.g. /etc/hosts mapping) is not a usable remote hub address.
        if ip and not ip.startswith("127."):
            return ip
    return None


def _own_ipv4s() -> List[str]:
    """This host's own IPv4 addresses, INCLUDING loopback (127.0.0.1).

    Mirrors ``LabManagerHub._local_ipv4s`` (main.py) but does NOT exclude
    loopback — the same-box test needs to match 127.0.0.1 too. Used by
    ``is_hub_local`` to decide whether a discovered hub IP is this very box.
    """
    # Always count loopback as "this host".
    ips: List[str] = ["127.0.0.1"]
    # UDP-connect to an RFC 5737 (never-routed) address reveals the primary
    # egress interface IP without sending any packets. Falls back gracefully
    # in a sandboxed/offline environment.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # connect() on UDP only picks a route; getsockname() then shows our source IP.
            s.connect(("223.255.255.1", 1))
            ip = s.getsockname()[0]
            if ip and ip not in ips:
                ips.append(ip)
        # Close the probe socket whether or not the route lookup worked.
        finally:
            s.close()
    # No route / no network: keep whatever we have so far.
    except Exception:
        pass
    # If psutil is installed, add every IPv4 on every interface (catches secondary NICs).
    try:
        import psutil  # type: ignore
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                # getattr keeps this robust across psutil versions' address objects.
                fam = getattr(a, "family", None)
                addr = getattr(a, "address", "")
                if fam == socket.AF_INET and addr and addr not in ips:
                    ips.append(addr)
    # psutil missing (optional dependency) or failing: fall back to the list so far.
    except Exception:
        pass
    return ips


def is_hub_local(hub_ip: str) -> bool:
    """True if ``hub_ip`` is this host itself (loopback or one of its own
    interface IPs). Decides the loopback-plaintext vs remote-TLS branch in
    ``discover_hub_url``."""
    # An empty address can never be this host.
    if not hub_ip:
        return False
    # Any loopback form is by definition local; no interface scan needed.
    if hub_ip.startswith("127.") or hub_ip in ("::1", "localhost"):
        return True
    # Otherwise compare against this host's interface addresses.
    return hub_ip in _own_ipv4s()


def _mdns_discover(timeout: float) -> Optional[Tuple[str, int, Dict[str, str]]]:
    """Browse ``_lm-hub._tcp.local.`` → (host_ip, port, txt_properties) or None.

    The TXT properties (decoded to str) carry ``tls_port`` (the hub's wss port
    when TLS is enabled) and ``agent_port`` (the pxmx agent-listener port).
    Skipped silently when ``zeroconf`` is not importable (graceful degradation).
    """
    # zeroconf is optional; without it mDNS discovery is simply unavailable.
    try:
        import zeroconf  # noqa: F401  (imported for the names below)
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return None
    # Tracked outside the try so the finally block can always shut it down.
    zc = None
    try:
        # Start an mDNS responder/browser instance.
        zc = Zeroconf()
        # Shared slot the listener fills in from zeroconf's background thread.
        found: dict = {}

        class _Listener:
            """zeroconf ServiceListener that records the first hub service it resolves."""

            def add_service(self, _zc, type_, name):
                """Resolve a newly seen hub service and store its details in ``found``."""
                # Ask for the full record (addresses, port, TXT), waiting up to 2000 ms.
                try:
                    info = _zc.get_service_info(type_, name, timeout=2000)
                # A resolution failure just means we keep waiting for another answer.
                except Exception:
                    info = None
                if info is not None:
                    found["info"] = info

            def update_service(self, _zc, type_, name):
                """Treat an updated service record like a new one."""
                self.add_service(_zc, type_, name)

            def remove_service(self, _zc, type_, name):
                """Ignore service removals; we only need a one-shot lookup."""
                pass

        # Begin browsing; callbacks arrive on zeroconf's own thread.
        ServiceBrowser(zc, HUB_SERVICE_TYPE, _Listener())
        # Poll every 100 ms until a hub is found or the time budget runs out.
        deadline = time.time() + timeout
        while "info" not in found and time.time() < deadline:
            time.sleep(0.1)
        info = found.get("info")
        # No hub answered within the window.
        if info is None:
            return None
        # Decode the TXT record (zeroconf stores keys/values as bytes).
        props: Dict[str, str] = {}
        for k, v in (getattr(info, "properties", {}) or {}).items():
            try:
                kk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                vv = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
                props[kk] = vv
            # Skip a TXT entry that is not valid UTF-8 instead of failing discovery.
            except Exception:
                continue
        # zeroconf ServiceInfo.addresses is a list of packed inet (4-byte) values.
        for packed in getattr(info, "addresses", []) or []:
            # Convert to dotted-quad; skip IPv6 (16-byte) or malformed entries.
            try:
                ip = socket.inet_ntoa(packed)
            except Exception:
                continue
            # First non-loopback address wins, along with the advertised port and TXT.
            if ip and not ip.startswith("127."):
                return (ip, int(getattr(info, "port", DEFAULT_HUB_PORT)), props)
        # Service found but it only advertised loopback/unusable addresses.
        return None
    # Any zeroconf/network error degrades to "not found" (debug-level, it is optional).
    except Exception as e:
        logger.debug("mDNS discovery error: %s", e)
        return None
    # Always close zeroconf so its sockets and threads do not leak.
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass


def _int_or_none(v) -> Optional[int]:
    """Convert a TXT value to int, or return None if missing or not a number."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def discover_hub_url(timeout: float = 5.0,
                     port_override: Optional[int] = None,
                     agent_listener: bool = False) -> Optional[str]:
    """Auto-locate the LM hub → ``'ws://host:port'`` / ``'wss://host:port'`` or ``None``.

    DNS is tried first (each ``lm-hub.<suffix>`` candidate via
    ``socket.getaddrinfo`` with a short per-name timeout); on a miss, mDNS
    browses ``_lm-hub._tcp.local.``.

    ``port_override`` forces the port (used by legacy callers); when ``None`` the
    advertised/DNS-default port is used. ``agent_listener=True`` targets the hub
    box's pxmx agent listener — it reads the ``agent_port`` TXT (default 8766)
    instead of the spoke-WS service port.

    **Scheme:** ``ws://`` unless the hub advertises a ``tls_port`` TXT (mDNS
    only) AND the caller is remote — then ``wss://``. A co-located caller
    (``is_hub_local``) always gets ``ws://127.0.0.1:<port>`` (loopback plaintext).
    The DNS path has no TXT so it always returns ``ws://`` (pin ``--hub`` for a
    cert-bearing DNS-only hub). The DNS path returns the hostname (so DNS
    rotation/TTL is honored); the mDNS path returns the IP the service advertised.
    """
    # DNS gets at most 3 s (or the whole budget if smaller) so mDNS still has time.
    dns_deadline = time.time() + min(timeout, 3.0)
    # Try each lm-hub candidate name in priority order.
    for name in _dns_candidates():
        # Out of DNS time: move on to mDNS.
        if time.time() >= dns_deadline:
            break
        ip = _resolve_host(name, timeout=1.0)
        # First name that resolves wins.
        if ip is not None:
            # Explicit override, else the default port for the requested listener.
            base_port = port_override if port_override is not None else (
                DEFAULT_AGENT_PORT if agent_listener else DEFAULT_HUB_PORT)
            # Spoke leg targets the unified :443 /ws/spoke route; the agent leg
            # targets /ws/agent (hub proxy on all-in-one, direct on standalone
            # pxmx). DNS carries no TXT → no TLS inference (a cert-bearing hub
            # reachable only via DNS is pinned with --hub wss://host:443/ws/spoke
            # or wss://host:443/ws/agent). Same-box → loopback.
            spoke_path = AGENT_WS_PATH if agent_listener else SPOKE_WS_PATH
            # Hub is this very host: dial loopback instead of the network name.
            if is_hub_local(ip):
                logger.info("discovered hub via DNS (local): 127.0.0.1:%d%s", base_port, spoke_path)
                return f"ws://127.0.0.1:{base_port}{spoke_path}"
            # Remote hub: return the hostname (not the IP) so DNS changes are honoured.
            logger.info("discovered hub via DNS: %s:%d%s", name, base_port, spoke_path)
            return f"ws://{name}:{base_port}{spoke_path}"

    # DNS found nothing: fall back to browsing for the hub's mDNS broadcast.
    mdns = _mdns_discover(timeout)
    if mdns is not None:
        ip, svc_port, props = mdns
        # Optional TXT hints: TLS port (presence means TLS is on) and agent port.
        tls_port = _int_or_none(props.get("tls_port"))
        agent_port = _int_or_none(props.get("agent_port"))
        # Agent leg: use the advertised agent port (or its default) unless overridden.
        if agent_listener:
            base_port = port_override if port_override is not None else (
                agent_port or DEFAULT_AGENT_PORT)
        # Spoke leg: use the service's advertised port unless overridden.
        else:
            base_port = port_override if port_override is not None else svc_port
        # Route path on the hub's unified listener for the requested leg.
        spoke_path = AGENT_WS_PATH if agent_listener else SPOKE_WS_PATH
        # Same box as the hub → loopback. Under the unified-443 merge the hub
        # serves ONLY :443 (wss when TLS is on, plain otherwise — there is no
        # separate plaintext loopback listener anymore), so a co-located caller
        # must match the hub's scheme: wss when tls_port is advertised.
        if is_hub_local(ip):
            scheme = "wss" if tls_port else "ws"
            logger.info("discovered hub via mDNS (local): %s://127.0.0.1:%d%s", scheme, base_port, spoke_path)
            return f"{scheme}://127.0.0.1:{base_port}{spoke_path}"
        if tls_port:
            # Hub serves TLS. The spoke endpoint uses the wss port (tls_port);
            # the agent endpoint is itself wss on its own port (base_port).
            wss_port = base_port if agent_listener else tls_port
            logger.info("discovered hub via mDNS (TLS): %s:%d%s", ip, wss_port, spoke_path)
            return f"wss://{ip}:{wss_port}{spoke_path}"
        # Remote hub without TLS: plain ws:// to the advertised IP.
        logger.info("discovered hub via mDNS: %s:%d%s", ip, base_port, spoke_path)
        return f"ws://{ip}:{base_port}{spoke_path}"

    # Neither DNS nor mDNS located a hub.
    return None


def _main() -> int:
    """CLI: print ``ws://host:port`` / ``wss://host:port`` (or ``NONE``) and exit 0.

    For install scripts. ``--agent-listener`` targets the hub box's pxmx agent
    listener (reads the ``agent_port`` TXT) instead of the spoke-WS port.
    """
    # Imported lazily: only the CLI needs argument parsing.
    import argparse

    # Standalone ``python -m discovery`` run (install scripts): emit the
    # canonical format/timestamps so any logger output matches the journal.
    # Without this, root defaults to WARNING and uses Python's default format.
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    # CLI flags mirror discover_hub_url()/resolve_agent_url() parameters.
    parser = argparse.ArgumentParser(description="Auto-discover the LM hub.")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="total discovery window in seconds (default 5)")
    parser.add_argument("--port-override", type=int, default=None,
                        help="use this port instead of the advertised one")
    parser.add_argument("--agent-listener", action="store_true",
                        help="target the hub box's pxmx agent listener "
                             "(reads the agent_port TXT) instead of the spoke-WS port")
    parser.add_argument("--resolve-agent", metavar="HOST", default=None,
                        help="given only a spoke IP/host, probe its known agent-listener "
                             "endpoints and print the full ws(s)://HOST:PORT/ws/agent URL "
                             "to dial (auto-determines scheme + port + path)")
    args = parser.parse_args()
    # --resolve-agent mode: probe a known spoke host instead of discovering the hub.
    if args.resolve_agent:
        url = resolve_agent_url(args.resolve_agent, args.timeout)
        # Print the literal "NONE" on failure so shell scripts can test for it.
        print(url if url else "NONE")
        return 0
    # Default mode: discover the hub and print its URL (or "NONE").
    url = discover_hub_url(args.timeout, args.port_override,
                           agent_listener=args.agent_listener)
    print(url if url else "NONE")
    return 0


# Run the CLI when executed directly (e.g. by install scripts).
if __name__ == "__main__":
    sys.exit(_main())