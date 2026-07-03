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
intra-repo dependencies — so the same source is vendored verbatim into the pxmx
spoke (``pxmx/src/discovery.py``) and the standalone pxmx agent
(``pxmx/agent/src/discovery.py``). Keep the three copies in sync.

If ``zeroconf`` is not importable the mDNS branch is skipped and DNS-only is
used, so a missing optional dep never breaks discovery (or the hub broadcast).
"""

import concurrent.futures
import logging
import os
import socket
import sys
import time
from typing import Dict, List, Optional, Tuple

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

# Overridable for tests; production reads /etc/resolv.conf.
_RESOLV_CONF = "/etc/resolv.conf"


def _resolv_search_domains() -> List[str]:
    """Parse the ``search`` line from /etc/resolv.conf (best-effort)."""
    domains: List[str] = []
    try:
        with open(_RESOLV_CONF, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "search":
                    domains.extend(parts[1:])
    except Exception:
        pass
    return domains


def _dns_candidates() -> List[str]:
    """Hostnames to try for ``lm-hub``, in priority order (deduped)."""
    cands: List[str] = []
    for dom in _resolv_search_domains():
        dom = dom.rstrip(".")
        if dom:
            cands.append(f"{HUB_SHORT_NAME}.{dom}")
    # The host's own domain suffix (e.g. this box is on mydomain.com → lm-hub.mydomain.com).
    try:
        fqdn = socket.getfqdn()
        if "." in fqdn:
            suffix = fqdn.split(".", 1)[1].rstrip(".")
            if suffix and suffix not in ("local", "localdomain", "lan"):
                cands.append(f"{HUB_SHORT_NAME}.{suffix}")
    except Exception:
        pass
    cands.append(f"{HUB_SHORT_NAME}.local")   # mDNS host name (Avahi/.local)
    cands.append(HUB_SHORT_NAME)              # bare — relies on search domains / admin DNS
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(socket.getaddrinfo, name, None, socket.AF_INET,
                            socket.SOCK_STREAM)
            infos = fut.result(timeout=timeout)
    except Exception:
        return None
    for entry in infos or []:
        try:
            sockaddr = entry[4]
            ip = sockaddr[0]
        except Exception:
            continue
        if ip and not ip.startswith("127."):
            return ip
    return None


def _own_ipv4s() -> List[str]:
    """This host's own IPv4 addresses, INCLUDING loopback (127.0.0.1).

    Mirrors ``LabManagerHub._local_ipv4s`` (main.py) but does NOT exclude
    loopback — the same-box test needs to match 127.0.0.1 too. Used by
    ``is_hub_local`` to decide whether a discovered hub IP is this very box.
    """
    ips: List[str] = ["127.0.0.1"]
    # UDP-connect to an RFC 5737 (never-routed) address reveals the primary
    # egress interface IP without sending any packets. Falls back gracefully
    # in a sandboxed/offline environment.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.255.255.1", 1))
            ip = s.getsockname()[0]
            if ip and ip not in ips:
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        import psutil  # type: ignore
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                fam = getattr(a, "family", None)
                addr = getattr(a, "address", "")
                if fam == socket.AF_INET and addr and addr not in ips:
                    ips.append(addr)
    except Exception:
        pass
    return ips


def is_hub_local(hub_ip: str) -> bool:
    """True if ``hub_ip`` is this host itself (loopback or one of its own
    interface IPs). Decides the loopback-plaintext vs remote-TLS branch in
    ``discover_hub_url``."""
    if not hub_ip:
        return False
    if hub_ip.startswith("127.") or hub_ip in ("::1", "localhost"):
        return True
    return hub_ip in _own_ipv4s()


def _mdns_discover(timeout: float) -> Optional[Tuple[str, int, Dict[str, str]]]:
    """Browse ``_lm-hub._tcp.local.`` → (host_ip, port, txt_properties) or None.

    The TXT properties (decoded to str) carry ``tls_port`` (the hub's wss port
    when TLS is enabled) and ``agent_port`` (the pxmx agent-listener port).
    Skipped silently when ``zeroconf`` is not importable (graceful degradation).
    """
    try:
        import zeroconf  # noqa: F401  (imported for the names below)
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return None
    zc = None
    try:
        zc = Zeroconf()
        found: dict = {}

        class _Listener:
            def add_service(self, _zc, type_, name):
                try:
                    info = _zc.get_service_info(type_, name, timeout=2000)
                except Exception:
                    info = None
                if info is not None:
                    found["info"] = info

            def update_service(self, _zc, type_, name):
                self.add_service(_zc, type_, name)

            def remove_service(self, _zc, type_, name):
                pass

        ServiceBrowser(zc, HUB_SERVICE_TYPE, _Listener())
        deadline = time.time() + timeout
        while "info" not in found and time.time() < deadline:
            time.sleep(0.1)
        info = found.get("info")
        if info is None:
            return None
        # Decode the TXT record (zeroconf stores keys/values as bytes).
        props: Dict[str, str] = {}
        for k, v in (getattr(info, "properties", {}) or {}).items():
            try:
                kk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                vv = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
                props[kk] = vv
            except Exception:
                continue
        # zeroconf ServiceInfo.addresses is a list of packed inet (4-byte) values.
        for packed in getattr(info, "addresses", []) or []:
            try:
                ip = socket.inet_ntoa(packed)
            except Exception:
                continue
            if ip and not ip.startswith("127."):
                return (ip, int(getattr(info, "port", DEFAULT_HUB_PORT)), props)
        return None
    except Exception as e:
        logger.debug("mDNS discovery error: %s", e)
        return None
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass


def _int_or_none(v) -> Optional[int]:
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
    dns_deadline = time.time() + min(timeout, 3.0)
    for name in _dns_candidates():
        if time.time() >= dns_deadline:
            break
        ip = _resolve_host(name, timeout=1.0)
        if ip is not None:
            base_port = port_override if port_override is not None else (
                DEFAULT_AGENT_PORT if agent_listener else DEFAULT_HUB_PORT)
            # Spoke leg targets the unified :443 /ws/spoke route; the agent leg
            # targets /ws/agent (hub proxy on all-in-one, direct on standalone
            # pxmx). DNS carries no TXT → no TLS inference (a cert-bearing hub
            # reachable only via DNS is pinned with --hub wss://host:443/ws/spoke
            # or wss://host:443/ws/agent). Same-box → loopback.
            spoke_path = AGENT_WS_PATH if agent_listener else SPOKE_WS_PATH
            if is_hub_local(ip):
                logger.info("discovered hub via DNS (local): 127.0.0.1:%d%s", base_port, spoke_path)
                return f"ws://127.0.0.1:{base_port}{spoke_path}"
            logger.info("discovered hub via DNS: %s:%d%s", name, base_port, spoke_path)
            return f"ws://{name}:{base_port}{spoke_path}"

    mdns = _mdns_discover(timeout)
    if mdns is not None:
        ip, svc_port, props = mdns
        tls_port = _int_or_none(props.get("tls_port"))
        agent_port = _int_or_none(props.get("agent_port"))
        if agent_listener:
            base_port = port_override if port_override is not None else (
                agent_port or DEFAULT_AGENT_PORT)
        else:
            base_port = port_override if port_override is not None else svc_port
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
        logger.info("discovered hub via mDNS: %s:%d%s", ip, base_port, spoke_path)
        return f"ws://{ip}:{base_port}{spoke_path}"

    return None


def _main() -> int:
    """CLI: print ``ws://host:port`` / ``wss://host:port`` (or ``NONE``) and exit 0.

    For install scripts. ``--agent-listener`` targets the hub box's pxmx agent
    listener (reads the ``agent_port`` TXT) instead of the spoke-WS port.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Auto-discover the LM hub.")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="total discovery window in seconds (default 5)")
    parser.add_argument("--port-override", type=int, default=None,
                        help="use this port instead of the advertised one")
    parser.add_argument("--agent-listener", action="store_true",
                        help="target the hub box's pxmx agent listener "
                             "(reads the agent_port TXT) instead of the spoke-WS port")
    args = parser.parse_args()
    url = discover_hub_url(args.timeout, args.port_override,
                           agent_listener=args.agent_listener)
    print(url if url else "NONE")
    return 0


if __name__ == "__main__":
    sys.exit(_main())