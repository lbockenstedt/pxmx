"""VM/CT inventory + per-interface enrichment for the unified pxmx agent.

Free-function extraction of ``ProxmoxAgent``'s read-only inventory helpers:
the VM list (``get_vm_list`` via ``/cluster/resources`` with a per-node
fallback), guest network-interface enrichment (QGA / lxc /interfaces with a
qm/pct config MAC fallback), the pool map, and the create-VM UI's
pool/ISO/storage enumerations. Functions take the ``agent`` instance as their
first argument (the cs_commands/usb_provision pattern). ``ProxmoxAgent`` keeps
thin wrapper methods for the externally-called entry points (list_pools,
list_node_isos, list_node_storages, get_vm_list) so the dispatch chain and
``agent.get_vm_list()`` callers are untouched.
"""

# asyncio runs the per-VM probes in parallel; re matches MACs; time stamps the interface cache.
import asyncio
import re
import time
from typing import Any, Dict, List, Optional  # noqa: F401 — kept for signature parity

# logging reports inventory problems.
import logging

# Share the main agent logger so inventory messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# Per-VM interface cache TTLs (annotate_vm_interfaces). The QGA
# ``network-get-interfaces`` guest round-trip dominates get_vm_list on a busy
# host — an unresponsive/booting guest rides the 4s timeout EVERY telemetry tick
# (every 3s while provisioning). IPs/MACs barely change, so cache them:
_IFACE_TTL_OK = 120.0    # fully resolved (has an IP, or a stopped VM's config MAC): 2 min
                         # (was 300 — a running VM's IP change went unseen for 5 min)
# Backoff ceiling for a VM that keeps failing to yield an IP. Some guests can
# NEVER yield one -- a client running ssidpw_fail / assoc_fail / auth_fail is
# unable to associate BY DESIGN -- so a fixed short retry means those VMs are
# re-probed forever, every tick, at full concurrency. Measured on a node with a
# large sim fleet: ~4.4 subprocess spawns/sec sustained, 86% of all syscall time
# in futex contention between the spawning threads, and the agent pinned at 900%+
# CPU. Each miss doubles the retry interval up to this cap, so a permanently
# IP-less VM settles to one probe every few minutes instead of every tick.
_IFACE_TTL_MISS_MAX = 900.0
_IFACE_TTL_MISS = 60.0   # running VM with no IP yet (guest agent booting / not answering):
                         # retry every 60s instead of riding the 4s timeout each tick

# Matches a MAC in either colon or dash form (case-insensitive): aa:bb:cc:dd:ee:ff
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)


def _looks_like_mac(s: str) -> bool:
    """True if ``s`` is a 6-octet MAC (colon or dash separators)."""
    # Strip whitespace and test against the MAC pattern.
    return bool(_MAC_RE.match((s or "").strip()))


async def vm_interfaces(agent, node: str, vmid: Any, rtype: str,
                        status: str) -> List[Dict[str, Any]]:
    """Best-effort per-network-interface record for one VM/CT:
    ``[{"name", "mac", "ips": [..]}]``.

    Running qemu uses the guest-agent ``network-get-interfaces`` endpoint
    (yields the guest-visible IPs AND the MAC); running lxc uses
    ``/interfaces`` (container netns — no guest agent needed). When the
    guest source is absent/unresponsive OR the VM is stopped, fall back to
    ``qm``/``pct config`` netN lines for the configured MACs (no guest IPs —
    MACs are config, available in any state). Stopped VMs therefore still
    get their MACs. Never raises; returns [] on any failure. Read-only
    pvesh GET, safe for any VMID (no execution guard).
    """
    # Without a node and VMID there is nothing to look up.
    if not node or vmid in (None, ""):
        return []
    # Map the resource type to the Proxmox API path segment.
    kind = "qemu" if rtype == "qemu" else "lxc"
    interfaces: List[Dict[str, Any]] = []
    # Running guests: ask the guest itself for interfaces and IPs.
    if status == "running":
        try:
            # 2s (was 4s): a responsive guest agent answers in well under a
            # second; an unresponsive one just costs the timeout, so a shorter
            # bound halves the per-VM miss cost on a busy host. The interface
            # cache means a resolved VM isn't re-queried for 5 min anyway.
            if kind == "qemu":
                data = await asyncio.wait_for(
                    agent._pvesh(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"),
                    timeout=2)
            else:
                data = await asyncio.wait_for(
                    agent._pvesh(f"/nodes/{node}/lxc/{vmid}/interfaces"),
                    timeout=2)
            # Turn the raw response into name/MAC/IP records.
            interfaces = parse_guest_ifaces(data)
        # Guest agent missing or too slow: fall through to the config fallback.
        except Exception:
            interfaces = []
    # Fall back to configured MACs when the guest source gave nothing (QGA
    # absent, stopped VM, or empty result) — MACs are config so always
    # available regardless of power state.
    if not interfaces:
        try:
            interfaces = await vm_net_macs(agent, node, vmid, kind)
        # Config lookup failed too: return nothing.
        except Exception:
            interfaces = []
    return interfaces


def parse_guest_ifaces(data: Any) -> List[Dict[str, Any]]:
    """Normalize QGA ``network-get-interfaces`` / lxc ``/interfaces`` into
    ``[{"name", "mac", "ips"}]``. QGA MAC is ``hardware-address``; lxc is
    ``hwaddr``. Loopback/link-local IPs are excluded; per-interface IPs are
    deduped. PVE wraps agent responses inconsistently (result/data/lists)
    — unwrapped here."""
    # Unwrap the response: Proxmox nests results under "result" or "data".
    result = data
    if isinstance(data, dict):
        result = data.get("result", data.get("data", data))
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    # Anything other than a list means there are no interfaces.
    out: List[Dict[str, Any]] = []
    if not isinstance(result, list):
        return out
    # Interface names already emitted (avoids duplicates).
    seen_names: set = set()
    for iface in result:
        # Ignore malformed entries.
        if not isinstance(iface, dict):
            continue
        # Interface name and lower-cased MAC, accepting both the qemu and lxc field names.
        name = str(iface.get("name") or iface.get("netdev") or "").strip()
        mac = str(iface.get("hardware-address") or iface.get("hwaddr") or "").strip().lower()
        # Skip the loopback / all-zeros-MAC pseudo-interfaces so they don't
        # become NetBox vminterfaces.
        if name.lower() == "lo" or mac == "00:00:00:00:00:00":
            continue
        ips: List[str] = []
        # qemu guest-agent: {"ip-addresses": [{"ip-address","ip-address-type"}]}
        for entry in (iface.get("ip-addresses") or []):
            if str(entry.get("ip-address-type", "")).lower() == "ipv4":
                ip = entry.get("ip-address")
                if isinstance(ip, str) and ip and not ip.startswith(("127.", "169.254.")):
                    ips.append(ip)
        # lxc /interfaces: {"inet": "1.2.3.4/24" | ["1.2.3.4/24", ...]}
        # lxc may give a single string or a list of CIDR strings.
        inet = iface.get("inet")
        addrs = inet if isinstance(inet, list) else (
            [inet] if isinstance(inet, str) and inet else [])
        # Keep the address without its prefix, skipping loopback and link-local.
        for addr in addrs:
            ip = str(addr).split("/")[0]
            if ip and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
        # Remove duplicate IPs while keeping their order.
        seen, uips = set(), []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                uips.append(ip)
        # Emit each interface once, keyed by name (or MAC when unnamed).
        key = name or mac or f"iface{len(out)}"
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append({"name": name, "mac": mac, "ips": uips})
    return out


async def vm_net_macs(agent, node: str, vmid: Any,
                      kind: str) -> List[Dict[str, Any]]:
    """Parse ``qm``/``pct config`` netN lines for the configured MACs — the
    fallback when the guest agent is absent or the VM is stopped (no guest
    IPs, but MACs are config so always available). Returns
    ``[{"name", "mac", "ips": []}]``."""
    # Fetch the VM/CT config with a short timeout.
    try:
        data = await asyncio.wait_for(
            agent._pvesh(f"/nodes/{node}/{kind}/{vmid}/config"), timeout=4)
    # Config unavailable: no MACs to report.
    except Exception:
        return []
    # Unwrap the config dict and parse its netN lines.
    cfg = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(cfg, dict):
        return []
    return parse_config_nets(cfg)


def parse_config_nets(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse a qm/pct config dict for ``netN`` entries →
    ``[{"name", "mac", "ips": []}]``.

    qemu: ``net0: "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0[,...]"`` — the MAC
          is the hex after the model (``virtio=``/``e1000=``/…).
    lxc:  ``net0: "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF[,...]"``.
    """
    # Collect one record per netN entry.
    out: List[Dict[str, Any]] = []
    for key, val in cfg.items():
        # Only netN keys with string values describe NICs.
        if not key.startswith("net") or not isinstance(val, str):
            continue
        # Walk the comma-separated key=value pairs.
        mac, name = "", ""
        for token in val.split(","):
            token = token.strip()
            if not token or "=" not in token:
                continue
            # Split each pair and normalise the key and value.
            k, v = token.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
            # lxc names the MAC "hwaddr"; qemu puts it after the NIC model name.
            if k == "hwaddr" and _looks_like_mac(v):
                mac = v.lower()
            elif k == "name":
                name = v
            elif _looks_like_mac(v):
                mac = v.lower()   # qemu: <model>=<MAC>
        # Keep any NIC that has a MAC or a name.
        if mac or name:
            out.append({"name": name or key, "mac": mac, "ips": []})
    return out


async def annotate_vm_interfaces(agent, vms: List[Dict[str, Any]]) -> None:
    """Populate ``vm["interfaces"]`` (and the derived flat ``vm["ips"]``)
    in parallel — best-effort, bounded by a semaphore (16 concurrent pvesh
    calls) and a 12s deadline so a hung guest agent can't stall the 60s
    telemetry tick. Running VMs get guest IPs + MACs (QGA/LXC); stopped VMs
    get their configured MACs via qm/pct config.

    CACHED per VM (agent._iface_cache): the QGA guest round-trip is the dominant
    cost of get_vm_list (it showed 3.7s on 8 VMs — an unresponsive/booting guest
    rides the 4s timeout every tick). IPs/MACs barely change, so a resolved VM is
    re-queried at most every _IFACE_TTL_OK (5 min) and a running-but-no-IP VM
    backs off _IFACE_TTL_MISS (60s) instead of costing 4s every 3s tick. A
    status change (stopped<->running) forces a refresh so a freshly-booted VM's
    IPs appear promptly. The cache is pruned to the live VMID set each call."""
    # VMs with a node and VMID that can be probed.
    targets = [v for v in vms if v.get("node") and v.get("vmid") not in (None, "")]
    if not targets:
        return
    # Enrich only THIS node's VMs. get_vm_list sources /cluster/resources
    # (cluster-WIDE), so on a multi-node cluster ``targets`` also carries VMs
    # owned by OTHER nodes — and a guest-agent/config probe for a remote VM makes
    # pvesh PROXY the call over SSH to that node. On a fresh node that just joined
    # a populated cluster that meant dozens of SSH round-trips per tick, riding
    # the deadline and stalling the whole vm_list phase (the "telemetry collect
    # exceeded 25s" symptom). Each remote VM is enriched by ITS OWN owning agent
    # and the hub aggregates per node, so skipping them here loses nothing —
    # their interfaces are filled by the frame from the node that hosts them.
    # Fail OPEN: if the local node name can't be resolved, or NO target matches
    # it (name mismatch / older list without ``node``), enrich the full list —
    # a slower tick beats blank IPs. Mirrors the _cs_telemetry_body node filter.
    # Narrow to this node's VMs, if we know our node name and some VMs match it.
    _local = str(getattr(agent, "_local_node", "") or getattr(agent, "hostname", "") or "").strip()
    if _local:
        _mine = [v for v in targets if str((v.get("node") or "")).strip() == _local]
        if _mine:
            targets = _mine
    # Per-VM interface cache, created on the agent the first time.
    cache = getattr(agent, "_iface_cache", None)
    if cache is None:
        cache = agent._iface_cache = {}
    # One timestamp for the whole pass.
    now = time.time()
    # 4, not 16. Every permit is a `pvesh`/`qm` subprocess -- a Perl process
    # costing ~150MB RSS and 10-20% CPU for its short life -- and 16 of them at
    # once is 4x any other fan-out in this agent (pve_cmds and usb_provision both
    # use 4). Combined with the retry storm above it was spawning ~4.4
    # processes/sec forever.
    sem = asyncio.Semaphore(4)

    async def _one(v):
        """Fill one VM's interfaces and IPs, from the cache when it is fresh, otherwise by probing.
        """
        # Cache key, current power state, and any cached entry.
        key = str(v.get("vmid"))
        status = str(v.get("status") or "")
        ent = cache.get(key)
        # Cache hit: same power state AND still within TTL (longer once resolved,
        # short while a running guest hasn't yielded an IP). Reuse — no round-trip.
        if ent is not None and ent.get("status") == status:
            # Exponential backoff on consecutive misses, capped. A guest that is
            # simply slow to boot resolves on the next tick or two and never
            # reaches a long TTL; one that can never resolve stops being probed
            # every tick within a few minutes.
            if ent.get("ok"):
                ttl = _IFACE_TTL_OK
            else:
                _n = int(ent.get("misses", 1))
                ttl = min(_IFACE_TTL_MISS * (2 ** max(0, _n - 1)), _IFACE_TTL_MISS_MAX)
            # Fresh enough: copy the cached values and skip the probe.
            if (now - ent.get("ts", 0.0)) < ttl:
                v["interfaces"] = ent["interfaces"]
                v["ips"] = list(ent.get("ips") or [])
                return
        # Probe the VM while holding the concurrency limit.
        async with sem:
            ifaces = await vm_interfaces(
                agent, v.get("node", ""), v.get("vmid"), v.get("type"), status)
        # Flatten the IPs and store both on the VM record.
        ips = [ip for i in ifaces for ip in (i.get("ips") or [])]
        v["interfaces"] = ifaces
        v["ips"] = ips
        # "Resolved" = a running VM that yielded at least one IP, or a stopped VM
        # with config MACs (which never change). A running VM with MACs but no IP
        # yet (guest agent still booting / not answering) is a MISS → short TTL so
        # we keep trying until the IP appears, without paying 4s every tick.
        if status == "running":
            ok = bool(ips)
        else:
            ok = any(i.get("mac") for i in ifaces)
        # Consecutive-miss counter drives the backoff; any success resets it.
        # Count consecutive misses (reset to 0 on success) and store the result.
        _prev = int((ent or {}).get("misses", 0)) if ent is not None else 0
        cache[key] = {"interfaces": ifaces, "ips": ips, "ts": now,
                      "status": status, "ok": ok,
                      "misses": 0 if ok else _prev + 1}

    # Run all probes under one overall deadline.
    try:
        # 6s (was 12s) overall deadline: with per-VM timeout at 2s + the cache,
        # a full sweep finishes fast; capping at 6s bounds the worst-case tick on
        # a host with many unresponsive guests. VMs not annotated before the
        # deadline keep their cached value (or []), filled on a later tick.
        await asyncio.wait_for(
            asyncio.gather(*[_one(v) for v in targets], return_exceptions=True),
            timeout=6)
    # Deadline hit: keep whatever finished.
    except asyncio.TimeoutError:
        pass  # partial — VMs not yet annotated keep interfaces=[]/ips=[]
    # Prune cache entries for VMs no longer present so it can't grow unbounded
    # across clone/destroy churn.
    # Remove cache entries for VMs that no longer exist.
    live = {str(v.get("vmid")) for v in targets}
    for k in list(cache.keys()):
        if k not in live:
            cache.pop(k, None)


async def vm_pool_map(agent) -> dict:
    """Best-effort ``{vmid: poolid}`` from the Proxmox ``/pools`` endpoint.

    ``/cluster/resources`` (the VM list source) doesn't carry pool
    membership, so query ``/pools`` and reverse-map member vmid → poolid.
    Some PVE versions return ``members`` inline on the ``/pools`` listing;
    others require a per-pool ``/pools/{poolid}`` detail fetch. Both are
    handled. Returns ``{}`` on any failure (never raises) — callers then
    leave VM ``pool`` blank. A VM in no pool is simply absent from the map.
    """
    # Never raise: any failure returns an empty map.
    try:
        # List all pools, keeping only valid entries.
        pools = await agent._pvesh("/pools")
        out: dict = {}
        pools = [p for p in (pools if isinstance(pools, list) else [])
                 if isinstance(p, dict) and p.get("poolid")]

        # Pools whose listing already inlines members need no detail round-trip.
        # Map each inlined member VMID to its pool (first pool wins).
        for p in pools:
            if isinstance(p.get("members"), list):
                for m in p["members"]:
                    if isinstance(m, dict) and m.get("vmid") is not None:
                        out.setdefault(m.get("vmid"), p.get("poolid"))

        # The rest need a per-pool /pools/{pid} detail fetch. Run them
        # CONCURRENTLY under a small cap + overall deadline — NOT serially. A
        # serial loop here was N unbounded pvesh calls (each up to the 15s _pvesh
        # bound), so on a cluster with several pools whose /pools/{pid} proxies to
        # a slow/remote node this alone could exceed the 25s telemetry budget and
        # starve the whole vm_list phase every tick. Best-effort: a pool that
        # times out is simply absent from the map (its VMs get pool="").
        # Pools that still need a per-pool detail fetch.
        _need = [p.get("poolid") for p in pools if not isinstance(p.get("members"), list)]
        if _need:
            _sem = asyncio.Semaphore(4)

            async def _members(pid):
                """Fetch one pool's member list (returns ``(poolid, members)``; members is None on
                failure).
                """
                # Fetch the detail under the concurrency limit and a per-call timeout.
                try:
                    async with _sem:
                        detail = await asyncio.wait_for(
                            agent._pvesh(f"/pools/{pid}"), timeout=4)
                    return pid, (detail.get("members") if isinstance(detail, dict) else None)
                # A failed pool simply contributes no members.
                except Exception:  # noqa: BLE001
                    return pid, None

            # Fetch all pending pools concurrently under an overall deadline.
            try:
                _got = await asyncio.wait_for(
                    asyncio.gather(*[_members(pid) for pid in _need]), timeout=12)
            # Deadline hit: skip the slow pools.
            except asyncio.TimeoutError:
                _got = []
            # Add the fetched members to the map.
            for pid, members in _got:
                for m in (members if isinstance(members, list) else []):
                    if isinstance(m, dict) and m.get("vmid") is not None:
                        out.setdefault(m.get("vmid"), pid)
        return out
    # Pool lookup failed: callers leave the pool blank.
    except Exception as e:  # noqa: BLE001
        logger.debug(f"pool map unavailable: {e}")
        return {}


async def list_pools(agent) -> list:
    """Best-effort Proxmox resource pool list (``[{poolid, comment}, ...]``).

    Used by the clone/create-VM UI's pool dropdown. Reads ``/pools`` (which
    lists every pool id + comment); never raises — returns ``[]`` on failure.
    """
    # Never raise: an error returns an empty list.
    try:
        # List all pools.
        pools = await agent._pvesh("/pools")
        out = []
        # Keep only entries that have a pool id.
        for p in (pools if isinstance(pools, list) else []):
            if not isinstance(p, dict):
                continue
            pid = p.get("poolid")
            if not pid:
                continue
            # Return just the id and comment for the dropdown.
            out.append({"poolid": pid, "comment": p.get("comment", "") or ""})
        return out
    # Pool listing failed: return an empty list.
    except Exception as e:  # noqa: BLE001
        logger.debug(f"list_pools unavailable: {e}")
        return []


async def list_node_isos(agent, node: str) -> list:
    """ISO images available on ``node`` for the create-VM-from-ISO flow.

    Enumerates storages whose ``content`` includes ``iso`` and lists each
    storage's ISO content (Proxmox returns ``volid`` like
    ``local:iso/ubuntu-22.04.iso`` + ``size`` bytes). Returns a flat list of
    ``{volid, name, storage, size}``. ``[]`` on any failure (never raises).
    """
    # Collected ISO entries; never raise.
    out: list = []
    try:
        # Walk this node's storages.
        storages = await agent._pvesh(f"/nodes/{node}/storage")
        for s in (storages if isinstance(storages, list) else []):
            if not isinstance(s, dict):
                continue
            # Only storages that can hold ISOs.
            content = s.get("content") or ""
            if "iso" not in (content.split(",") if isinstance(content, str) else content):
                continue
            # A storage without a name can't be queried.
            storage = s.get("storage")
            if not storage:
                continue
            # List this storage's contents; skip the storage if that fails.
            try:
                items = await agent._pvesh(
                    f"/nodes/{node}/storage/{storage}/content",
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("iso content list failed for %s/%s: %s", node, storage, e)
                continue
            # Keep only .iso files.
            for it in (items if isinstance(items, list) else []):
                if not isinstance(it, dict):
                    continue
                volid = it.get("volid") or ""
                if not volid.endswith(".iso"):
                    continue
                # Record the volume id, file name, storage and size.
                out.append({
                    "volid":   volid,
                    "name":    it.get("volid", "").split("/")[-1],
                    "storage": storage,
                    "size":    it.get("size", 0) or 0,
                })
        # Return the flat ISO list.
        return out
    # Storage listing failed: return an empty list.
    except Exception as e:  # noqa: BLE001
        logger.debug(f"list_node_isos unavailable: {e}")
        return []


async def list_node_storages(agent, node: str, content_filter: str = "images") -> list:
    """Storages on ``node`` accepting the given content type (default
    ``images`` — where a new VM's boot disk can live). Returns
    ``[{storage, type, avail, total, shared}]``. ``[]`` on failure."""
    # Collected storages; never raise.
    out: list = []
    try:
        # Walk this node's storages.
        storages = await agent._pvesh(f"/nodes/{node}/storage")
        for s in (storages if isinstance(storages, list) else []):
            if not isinstance(s, dict):
                continue
            # Keep only storages whose content list includes the requested type.
            content = s.get("content") or ""
            parts = content.split(",") if isinstance(content, str) else content
            if content_filter not in parts:
                continue
            # Report capacity and whether the storage is shared.
            out.append({
                "storage": s.get("storage"),
                "type":    s.get("type", ""),
                "avail":   s.get("avail", 0) or 0,
                "total":   s.get("total", 0) or 0,
                "shared":  bool(s.get("shared", 0)),
            })
        return out
    # Storage listing failed: return an empty list.
    except Exception as e:  # noqa: BLE001
        logger.debug(f"list_node_storages unavailable: {e}")
        return []


async def get_vm_list(agent) -> Dict[str, Any]:
    """
    All VMs and containers via local pvesh — no API credentials required.

    Each entry includes:
      unique_id  — globally unique: "<cluster>/<node>/<vmid>"
      cluster    — Proxmox cluster name (or hostname for standalone)
      node       — Proxmox node name
      vmid       — integer VMID
      type       — "qemu" or "lxc"
      name, status, cpu, mem_bytes, uptime, tags, ips,
                 vcpus, disk_gb — provisioned capacity (maxcpu / maxdisk from
                   /cluster/resources) so the Hypervisor→NetBox VM sync can
                   populate NetBox vCPUs/disk without a per-VM qm config call
                 — ips: best-effort guest IPv4 list ([] for stopped VMs or
                   when qemu-guest-agent is absent; LXC needs no guest agent)

    Uses /cluster/resources as the primary source (up-to-date stats, single
    call, works for both standalone and clustered setups).  Falls back to
    per-node /qemu and /lxc queries if the cluster endpoint is unavailable.
    Guest IPs are annotated in parallel after the base list is built.
    """
    def _parse_tags(raw):
        """Split a Proxmox ";"-separated tag string into a clean list."""
        # Drop empty entries and surrounding whitespace.
        return [t.strip() for t in (raw or "").split(";") if t.strip()]

    def _vm_entry(r, node, rtype, vmid):
        """Build the normalised inventory record for one VM or container row."""
        # Fields come from the Proxmox row; interfaces/IPs are filled in later.
        return {
            "unique_id": f"{agent.cluster_name}/{node}/{vmid}",
            "cluster":   agent.cluster_name,
            "node":      node,
            "vmid":      vmid,
            "type":      rtype,
            "name":      r.get("name", f"{'vm' if rtype == 'qemu' else 'ct'}-{vmid}"),
            "status":    r.get("status", "unknown"),
            # Proxmox ``template: 1`` flag (set by ``qm template`` /
            # convert-to-template). /cluster/resources and the per-node
            # /qemu + /lxc endpoints all carry it. Captured here so the cs
            # telemetry ``_is_template`` heuristic can honor the real flag
            # instead of only tags/name (templates without a "template"
            # tag or a "template-" name were misfiled as 'Other').
            "template":  int(r.get("template", 0) or 0),
            # Proxmox guest OS type (qemu ``ostype`` from the VM config, e.g.
            # l26 / win11 / other). Best-effort: /cluster/resources and the
            # per-node /qemu+/lxc lists expose it for qemu guests; absent on
            # lxc and on older nodes → "" and the WebUI falls back to the
            # type label (Linux (CT) / —). Captured so the Hypervisor VM list
            # OS column mirrors the cs VM Server list (csVmOs).
            "ostype":    r.get("ostype", "") or "",
            "cpu":       round(r.get("cpu", 0) * 100, 1),
            "mem_bytes": r.get("mem") or r.get("maxmem", 0),
            "uptime":    r.get("uptime", 0),
            # Provisioned capacity for the Hypervisor→NetBox VM sync. Both
            # /cluster/resources and the per-node /qemu + /lxc fallback rows
            # carry maxcpu (vCPU count) and maxdisk (bytes), so no extra
            # qm config / pct config round-trip is needed here.
            "vcpus":     int(r.get("maxcpu", 0) or 0),
            "disk_gb":   round((r.get("maxdisk", 0) or 0) / 1e9, 1),
            # Proxmox resource pool membership (best-effort, from /pools).
            # /cluster/resources doesn't carry pool; vm_pool_map builds a
            # vmid→poolid map once before the entries are constructed.
            "pool":      pool_map.get(vmid, "") if pool_map else "",
            "tags":      _parse_tags(r.get("tags")),
            # Per-NIC records: [{name, mac, ips}] — filled by
            # annotate_vm_interfaces (running VMs get guest IPs + MACs via
            # QGA/LXC; stopped VMs get configured MACs via qm/pct config).
            # MACs land in NetBox on the VM's vminterfaces; ips is the flat
            # derivation kept for back-compat with consumers reading it.
            "interfaces": [],
            "ips":       [],   # derived flat IP list (back-compat)
        }

    # Best-effort vmid→poolid map. /cluster/resources doesn't expose pool
    # membership, so query /pools (which lists each pool's member VMs) and
    # reverse-map. A failure here is non-fatal: pool_map stays {} and every
    # VM gets pool="".
    pool_map = await vm_pool_map(agent)

    # Any unexpected failure returns an empty list with the error.
    try:
        # Primary: /cluster/resources — single call, Proxmox keeps this view
        # up-to-date for its own summary UI; works on standalone nodes too.
        try:
            # Build entries for every qemu VM and lxc container, then add their interfaces.
            resources = await agent._cluster_resources()
            all_vms = [
                _vm_entry(r, r.get("node", ""), r.get("type"), r.get("vmid"))
                for r in (resources if isinstance(resources, list) else [])
                if r.get("type") in ("qemu", "lxc")
            ]
            await annotate_vm_interfaces(agent, all_vms)
            return {"vms": all_vms, "cluster": agent.cluster_name}
        # Cluster view unavailable: log and fall back to per-node queries.
        except Exception as e:
            logger.warning(f"cluster/resources unavailable ({e}), falling back to per-node queries")

        # Fallback: per-node /qemu + /lxc
        # List the nodes, then each node's VMs and containers.
        raw_nodes = await agent._pvesh("/nodes")
        all_vms = []
        for n in (raw_nodes if isinstance(raw_nodes, list) else []):
            node_name = n.get("node", "")

            # qemu VMs on this node; a failure only loses this node's VMs.
            try:
                for vm in await agent._pvesh(f"/nodes/{node_name}/qemu"):
                    all_vms.append(_vm_entry(vm, node_name, "qemu", vm.get("vmid")))
            except Exception as e:
                logger.warning(f"QEMU list error for {node_name}: {e}")

            # lxc containers on this node; a failure only loses this node's containers.
            try:
                for ct in await agent._pvesh(f"/nodes/{node_name}/lxc"):
                    all_vms.append(_vm_entry(ct, node_name, "lxc", ct.get("vmid")))
            except Exception as e:
                logger.warning(f"LXC list error for {node_name}: {e}")

        # Add interfaces and return the combined list.
        await annotate_vm_interfaces(agent, all_vms)
        return {"vms": all_vms, "cluster": agent.cluster_name}
    # Nothing worked: return an empty list with the error.
    except Exception as e:
        logger.error(f"VM list error: {e}")
        return {"vms": [], "cluster": agent.cluster_name, "error": str(e)}
