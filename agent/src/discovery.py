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
from typing import List, Optional, Tuple

logger = logging.getLogger("HubDiscovery")

# The service the hub registers. Spokes browse this type to find the hub.
HUB_SERVICE_TYPE = "_lm-hub._tcp.local."
# The short name the hub is expected to use (the hub installer sets hostname
# `lm-hub`; admins create an `lm-hub` DNS record in their domain).
HUB_SHORT_NAME = "lm-hub"
# Default spoke-WS port (used when only DNS resolves — mDNS carries the real port).
DEFAULT_HUB_PORT = 8765

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


def _mdns_discover(timeout: float) -> Optional[Tuple[str, int]]:
    """Browse ``_lm-hub._tcp.local.`` → (host_ip, port) or None.

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
        # zeroconf ServiceInfo.addresses is a list of packed inet (4-byte) values.
        for packed in getattr(info, "addresses", []) or []:
            try:
                ip = socket.inet_ntoa(packed)
            except Exception:
                continue
            if ip and not ip.startswith("127."):
                return (ip, int(getattr(info, "port", DEFAULT_HUB_PORT)))
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


def discover_hub_url(timeout: float = 5.0,
                     port_override: Optional[int] = None) -> Optional[str]:
    """Auto-locate the LM hub → ``'ws://host:port'`` or ``None``.

    DNS is tried first (each ``lm-hub.<suffix>`` candidate via
    ``socket.getaddrinfo`` with a short per-name timeout); on a miss, mDNS
    browses ``_lm-hub._tcp.local.``. ``port_override`` lets the pxmx agent target
    the hub box's agent-listener (8766) instead of the spoke-WS port 8765; when
    ``None`` the discovered/advertised port is used. The DNS path returns the
    hostname (so DNS rotation/TTL is honored); the mDNS path returns the IP the
    service advertised.
    """
    dns_deadline = time.time() + min(timeout, 3.0)
    for name in _dns_candidates():
        if time.time() >= dns_deadline:
            break
        if _resolve_host(name, timeout=1.0) is not None:
            port = port_override if port_override is not None else DEFAULT_HUB_PORT
            logger.info("discovered hub via DNS: %s:%d", name, port)
            return f"ws://{name}:{port}"

    mdns = _mdns_discover(timeout)
    if mdns is not None:
        ip, svc_port = mdns
        port = port_override if port_override is not None else svc_port
        logger.info("discovered hub via mDNS: %s:%d", ip, port)
        return f"ws://{ip}:{port}"

    return None


def _main() -> int:
    """CLI: print ``ws://host:port`` (or ``NONE``) and exit 0. For install scripts."""
    import argparse

    parser = argparse.ArgumentParser(description="Auto-discover the LM hub.")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="total discovery window in seconds (default 5)")
    parser.add_argument("--port-override", type=int, default=None,
                        help="use this port instead of the advertised 8765 "
                             "(pxmx agent targets 8766)")
    args = parser.parse_args()
    url = discover_hub_url(args.timeout, args.port_override)
    print(url if url else "NONE")
    return 0


if __name__ == "__main__":
    sys.exit(_main())