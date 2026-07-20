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

import asyncio
import re
import time
from typing import Any, Dict, List, Optional  # noqa: F401 — kept for signature parity

import logging

logger = logging.getLogger("PxmxAgent")

# Per-VM interface cache TTLs (annotate_vm_interfaces). The QGA
# ``network-get-interfaces`` guest round-trip dominates get_vm_list on a busy
# host — an unresponsive/booting guest rides the 4s timeout EVERY telemetry tick
# (every 3s while provisioning). IPs/MACs barely change, so cache them:
_IFACE_TTL_OK = 300.0    # fully resolved (has an IP, or a stopped VM's config MAC): 5 min
_IFACE_TTL_MISS = 60.0   # running VM with no IP yet (guest agent booting / not answering):
                         # retry every 60s instead of riding the 4s timeout each tick

# Matches a MAC in either colon or dash form (case-insensitive): aa:bb:cc:dd:ee:ff
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)


def _looks_like_mac(s: str) -> bool:
    """True if ``s`` is a 6-octet MAC (colon or dash separators)."""
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
    if not node or vmid in (None, ""):
        return []
    kind = "qemu" if rtype == "qemu" else "lxc"
    interfaces: List[Dict[str, Any]] = []
    if status == "running":
        try:
            if kind == "qemu":
                data = await asyncio.wait_for(
                    agent._pvesh(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"),
                    timeout=4)
            else:
                data = await asyncio.wait_for(
                    agent._pvesh(f"/nodes/{node}/lxc/{vmid}/interfaces"),
                    timeout=4)
            interfaces = parse_guest_ifaces(data)
        except Exception:
            interfaces = []
    # Fall back to configured MACs when the guest source gave nothing (QGA
    # absent, stopped VM, or empty result) — MACs are config so always
    # available regardless of power state.
    if not interfaces:
        try:
            interfaces = await vm_net_macs(agent, node, vmid, kind)
        except Exception:
            interfaces = []
    return interfaces


def parse_guest_ifaces(data: Any) -> List[Dict[str, Any]]:
    """Normalize QGA ``network-get-interfaces`` / lxc ``/interfaces`` into
    ``[{"name", "mac", "ips"}]``. QGA MAC is ``hardware-address``; lxc is
    ``hwaddr``. Loopback/link-local IPs are excluded; per-interface IPs are
    deduped. PVE wraps agent responses inconsistently (result/data/lists)
    — unwrapped here."""
    result = data
    if isinstance(data, dict):
        result = data.get("result", data.get("data", data))
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    out: List[Dict[str, Any]] = []
    if not isinstance(result, list):
        return out
    seen_names: set = set()
    for iface in result:
        if not isinstance(iface, dict):
            continue
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
        inet = iface.get("inet")
        addrs = inet if isinstance(inet, list) else (
            [inet] if isinstance(inet, str) and inet else [])
        for addr in addrs:
            ip = str(addr).split("/")[0]
            if ip and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
        seen, uips = set(), []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                uips.append(ip)
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
    try:
        data = await asyncio.wait_for(
            agent._pvesh(f"/nodes/{node}/{kind}/{vmid}/config"), timeout=4)
    except Exception:
        return []
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
    out: List[Dict[str, Any]] = []
    for key, val in cfg.items():
        if not key.startswith("net") or not isinstance(val, str):
            continue
        mac, name = "", ""
        for token in val.split(","):
            token = token.strip()
            if not token or "=" not in token:
                continue
            k, v = token.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
            if k == "hwaddr" and _looks_like_mac(v):
                mac = v.lower()
            elif k == "name":
                name = v
            elif _looks_like_mac(v):
                mac = v.lower()   # qemu: <model>=<MAC>
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
    targets = [v for v in vms if v.get("node") and v.get("vmid") not in (None, "")]
    if not targets:
        return
    cache = getattr(agent, "_iface_cache", None)
    if cache is None:
        cache = agent._iface_cache = {}
    now = time.time()
    sem = asyncio.Semaphore(16)

    async def _one(v):
        key = str(v.get("vmid"))
        status = str(v.get("status") or "")
        ent = cache.get(key)
        # Cache hit: same power state AND still within TTL (longer once resolved,
        # short while a running guest hasn't yielded an IP). Reuse — no round-trip.
        if ent is not None and ent.get("status") == status:
            ttl = _IFACE_TTL_OK if ent.get("ok") else _IFACE_TTL_MISS
            if (now - ent.get("ts", 0.0)) < ttl:
                v["interfaces"] = ent["interfaces"]
                v["ips"] = list(ent.get("ips") or [])
                return
        async with sem:
            ifaces = await vm_interfaces(
                agent, v.get("node", ""), v.get("vmid"), v.get("type"), status)
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
        cache[key] = {"interfaces": ifaces, "ips": ips, "ts": now,
                      "status": status, "ok": ok}

    try:
        await asyncio.wait_for(
            asyncio.gather(*[_one(v) for v in targets], return_exceptions=True),
            timeout=12)
    except asyncio.TimeoutError:
        pass  # partial — VMs not yet annotated keep interfaces=[]/ips=[]
    # Prune cache entries for VMs no longer present so it can't grow unbounded
    # across clone/destroy churn.
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
    try:
        pools = await agent._pvesh("/pools")
        out: dict = {}
        for p in (pools if isinstance(pools, list) else []):
            if not isinstance(p, dict):
                continue
            pid = p.get("poolid")
            if not pid:
                continue
            members = p.get("members")
            if members is None:
                detail = await agent._pvesh(f"/pools/{pid}")
                members = detail.get("members") if isinstance(detail, dict) else None
            for m in (members if isinstance(members, list) else []):
                if isinstance(m, dict) and m.get("vmid") is not None:
                    # First pool seen wins; a VM shouldn't be in two pools.
                    out.setdefault(m.get("vmid"), pid)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"pool map unavailable: {e}")
        return {}


async def list_pools(agent) -> list:
    """Best-effort Proxmox resource pool list (``[{poolid, comment}, ...]``).

    Used by the clone/create-VM UI's pool dropdown. Reads ``/pools`` (which
    lists every pool id + comment); never raises — returns ``[]`` on failure.
    """
    try:
        pools = await agent._pvesh("/pools")
        out = []
        for p in (pools if isinstance(pools, list) else []):
            if not isinstance(p, dict):
                continue
            pid = p.get("poolid")
            if not pid:
                continue
            out.append({"poolid": pid, "comment": p.get("comment", "") or ""})
        return out
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
    out: list = []
    try:
        storages = await agent._pvesh(f"/nodes/{node}/storage")
        for s in (storages if isinstance(storages, list) else []):
            if not isinstance(s, dict):
                continue
            content = s.get("content") or ""
            if "iso" not in (content.split(",") if isinstance(content, str) else content):
                continue
            storage = s.get("storage")
            if not storage:
                continue
            try:
                items = await agent._pvesh(
                    f"/nodes/{node}/storage/{storage}/content",
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("iso content list failed for %s/%s: %s", node, storage, e)
                continue
            for it in (items if isinstance(items, list) else []):
                if not isinstance(it, dict):
                    continue
                volid = it.get("volid") or ""
                if not volid.endswith(".iso"):
                    continue
                out.append({
                    "volid":   volid,
                    "name":    it.get("volid", "").split("/")[-1],
                    "storage": storage,
                    "size":    it.get("size", 0) or 0,
                })
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"list_node_isos unavailable: {e}")
        return []


async def list_node_storages(agent, node: str, content_filter: str = "images") -> list:
    """Storages on ``node`` accepting the given content type (default
    ``images`` — where a new VM's boot disk can live). Returns
    ``[{storage, type, avail, total, shared}]``. ``[]`` on failure."""
    out: list = []
    try:
        storages = await agent._pvesh(f"/nodes/{node}/storage")
        for s in (storages if isinstance(storages, list) else []):
            if not isinstance(s, dict):
                continue
            content = s.get("content") or ""
            parts = content.split(",") if isinstance(content, str) else content
            if content_filter not in parts:
                continue
            out.append({
                "storage": s.get("storage"),
                "type":    s.get("type", ""),
                "avail":   s.get("avail", 0) or 0,
                "total":   s.get("total", 0) or 0,
                "shared":  bool(s.get("shared", 0)),
            })
        return out
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
        return [t.strip() for t in (raw or "").split(";") if t.strip()]

    def _vm_entry(r, node, rtype, vmid):
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

    try:
        # Primary: /cluster/resources — single call, Proxmox keeps this view
        # up-to-date for its own summary UI; works on standalone nodes too.
        try:
            resources = await agent._cluster_resources()
            all_vms = [
                _vm_entry(r, r.get("node", ""), r.get("type"), r.get("vmid"))
                for r in (resources if isinstance(resources, list) else [])
                if r.get("type") in ("qemu", "lxc")
            ]
            await annotate_vm_interfaces(agent, all_vms)
            return {"vms": all_vms, "cluster": agent.cluster_name}
        except Exception as e:
            logger.warning(f"cluster/resources unavailable ({e}), falling back to per-node queries")

        # Fallback: per-node /qemu + /lxc
        raw_nodes = await agent._pvesh("/nodes")
        all_vms = []
        for n in (raw_nodes if isinstance(raw_nodes, list) else []):
            node_name = n.get("node", "")

            try:
                for vm in await agent._pvesh(f"/nodes/{node_name}/qemu"):
                    all_vms.append(_vm_entry(vm, node_name, "qemu", vm.get("vmid")))
            except Exception as e:
                logger.warning(f"QEMU list error for {node_name}: {e}")

            try:
                for ct in await agent._pvesh(f"/nodes/{node_name}/lxc"):
                    all_vms.append(_vm_entry(ct, node_name, "lxc", ct.get("vmid")))
            except Exception as e:
                logger.warning(f"LXC list error for {node_name}: {e}")

        await annotate_vm_interfaces(agent, all_vms)
        return {"vms": all_vms, "cluster": agent.cluster_name}
    except Exception as e:
        logger.error(f"VM list error: {e}")
        return {"vms": [], "cluster": agent.cluster_name, "error": str(e)}
