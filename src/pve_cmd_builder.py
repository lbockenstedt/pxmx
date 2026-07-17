"""Spoke-side Proxmox command builder + result parser (agent-rework #4).

The pxmx spoke CONSTRUCTS ``pvesh``/``qm``/``pct``/``pvesm`` command strings and
sends them to the dumb Agent as ``RUN_COMMAND``; the Agent just executes them
(``/bin/bash -lc <cmd>`` when ``allow_shell=True``) and returns
``{ok, rc, stdout, stderr, truncated, error}``. The Agent no longer holds
Proxmox knowledge for the migrated families — it is a thin executor. This module
is the new home for that knowledge (moved from ``agent/src/pve_cmds.py`` +
``agent.py``'s ``_pvesh`` helpers).

Migration is incremental, one command family per commit, with the Agent's old
typed handler kept as a rollback fallback (a rolled-back spoke still uses the
typed path; a new spoke uses ``RUN_COMMAND`` against any agent, since
``RUN_COMMAND`` is a pre-existing generic primitive). Read-only families first;
the highest-risk mutating families (VM lifecycle) come last. ``cs_guard`` STAYS
at the Agent execution point for mutating families — the spoke builds the
command, the Agent's ``RUN_COMMAND`` dispatch runs it through the guard so the
``90000`` floor + ``PROTECTED_VMIDS`` still enforce. The spoke must NOT
pre-filter (it lacks local VMID state).

Single-shot pvesh reads are one ``RUN_COMMAND``. Multi-step families
(``LIST_ISOS``, ``GET_NODE_STATS``, ``LIST_VMS``) are orchestrated as several
``RUN_COMMAND`` round-trips from the spoke, with the spoke doing the parse/merge
the Agent used to do — keeping the Agent fully dumb.
"""
import json
import re
import shlex
from typing import Any, Dict, List

logger = __import__("logging").getLogger("PveCmdBuilder")


class PveCmdError(Exception):
    """Raised when a command can't be built or a result is unrecoverable."""


# ── RUN_COMMAND response handling ────────────────────────────────────────────

def _runner_dict(run_response: Any) -> Dict[str, Any]:
    """Normalize an Agent ``RUN_COMMAND`` response into the raw runner dict.

    ``send_to_agent`` returns the AGENT_RESPONSE ``data`` directly, which for
    ``RUN_COMMAND`` is ``run_local_command``'s return: ``{ok, rc, stdout, stderr,
    truncated, error, mode}``. Older/spurious wrapping (``payload.data``) is
    tolerated defensively (the typed-command envelope shape)."""
    r = run_response
    if isinstance(r, dict) and "payload" in r and isinstance(r["payload"], dict) \
            and "data" in r["payload"] and not {"ok", "rc", "stdout"} & set(r):
        r = r["payload"]["data"]  # typed-command envelope, not a runner dict
    return r if isinstance(r, dict) else {}


def runner_ok(run_response: Any) -> bool:
    """True if the Agent ran the command and it exited 0 (rc 0 + ok True)."""
    r = _runner_dict(run_response)
    return bool(r.get("ok")) and r.get("rc") == 0


def runner_stdout(run_response: Any) -> str:
    """The command's stdout (``""`` if the run failed)."""
    return (_runner_dict(run_response).get("stdout") or "").strip()


# ── single-shot pvesh reads ──────────────────────────────────────────────────

def pvesh_get(path: str) -> str:
    """Build a ``pvesh get <path> --output-format json`` command string for
    ``RUN_COMMAND``.

    ``pvesh`` defaults to ``text`` output (an ASCII table — NOT JSON; see
    pvesh(1) FORMAT_OPTIONS), so the ``--output-format json`` flag is MANDATORY:
    without it ``json.loads`` on the captured stdout fails and every read-only
    family silently returns empty. The Agent's own ``_pvesh`` always passed this
    flag; the builder must too. The Agent runs it via the login shell so ``pvesh``
    resolves on PATH (root on Proxmox has ``/usr/sbin``). The path is shell-quoted
    (node/storage names are safe but quoting is correct)."""
    return f"pvesh get {shlex.quote(path)} --output-format json"


def _parse_json_list(run_response: Any) -> List[Any]:
    """Parse a ``pvesh get`` JSON list from the run response. Returns ``[]`` on
    any failure — read-only pvesh errors are non-fatal (the spoke returns an
    empty list, same as the Agent's ``list_*`` helpers did)."""
    if not runner_ok(run_response):
        return []
    out = runner_stdout(run_response)
    if not out:
        return []
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


# ── PXMX_LIST_POOLS ───────────────────────────────────────────────────────────

def list_pools_cmd() -> str:
    """``pvesh get /pools`` — every pool id + comment (single-shot read)."""
    return pvesh_get("/pools")


def parse_pools(run_response: Any) -> List[Dict[str, Any]]:
    """``[{poolid, comment}, ...]`` — mirrors the Agent's ``list_pools`` shape so
    the spoke's aggregator only adds the ``cluster`` field."""
    out: List[Dict[str, Any]] = []
    for p in _parse_json_list(run_response):
        if not isinstance(p, dict):
            continue
        pid = p.get("poolid")
        if not pid:
            continue
        out.append({"poolid": pid, "comment": p.get("comment", "") or ""})
    return out


# ── PXMX_LIST_STORAGES ────────────────────────────────────────────────────────

def list_storages_cmd(node: str) -> str:
    """``pvesh get /nodes/<node>/storage`` — single-shot read of the node's
    storages. The spoke filters by content type (the Agent's
    ``list_node_storages`` did the same)."""
    return pvesh_get(f"/nodes/{node}/storage")


def parse_storages(run_response: Any, content_filter: str = "images") -> List[Dict[str, Any]]:
    """``[{storage, type, avail, total, shared}, ...]`` filtered to storages
    accepting ``content_filter`` (default ``images`` — boot-disk targets).
    Mirrors the Agent's ``list_node_storages`` shape + filter."""
    out: List[Dict[str, Any]] = []
    for s in _parse_json_list(run_response):
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


def storage_names_for_content(run_response: Any, content_filter: str = "iso") -> List[str]:
    """Storage NAMES whose ``content`` includes ``content_filter`` (e.g. ``iso``
    for the create-VM-from-ISO flow). The first round-trip of PXMX_LIST_ISOS;
    the spoke then fetches each storage's content. Mirrors the Agent's
    ``list_node_isos`` storage-filter step."""
    out: List[str] = []
    for s in _parse_json_list(run_response):
        if not isinstance(s, dict):
            continue
        content = s.get("content") or ""
        parts = content.split(",") if isinstance(content, str) else content
        if content_filter not in parts:
            continue
        storage = s.get("storage")
        if storage:
            out.append(storage)
    return out


# ── PXMX_LIST_ISOS (multi-round-trip) ─────────────────────────────────────────
# The Agent's ``list_node_isos`` was a multi-step pvesh sequence: list storages
# → for each iso-content storage, list its content → flatten the .iso items. The
# spoke now orchestrates the same sequence as RUN_COMMAND round-trips (keeping
# the Agent fully dumb) and does the parse/flatten the Agent used to do.

def list_iso_content_cmd(node: str, storage: str) -> str:
    """``pvesh get /nodes/<node>/storage/<storage>/content`` — the per-storage
    content listing (second round-trip of PXMX_LIST_ISOS)."""
    return pvesh_get(f"/nodes/{node}/storage/{storage}/content")


def parse_iso_items(run_response: Any, storage: str) -> List[Dict[str, Any]]:
    """``[{volid, name, storage, size}, ...]`` for items whose volid ends in
    ``.iso``. Mirrors the Agent's ``list_node_isos`` item flatten. The storage
    arg is stamped back so the caller knows where each ISO lives."""
    out: List[Dict[str, Any]] = []
    for it in _parse_json_list(run_response):
        if not isinstance(it, dict):
            continue
        volid = it.get("volid") or ""
        if not volid.endswith(".iso"):
            continue
        out.append({
            "volid":   volid,
            "name":    volid.split("/")[-1],
            "storage": storage,
            "size":    it.get("size", 0) or 0,
        })
    return out


# ── GET_NODE_STATS (multi-round-trip) ────────────────────────────────────────
# The Agent's ``get_node_stats`` had a primary path (``/cluster/resources``
# filtered to type=node → one first-node ``/status`` for the cluster-wide
# pveversion) and a fallback (``/nodes`` → per-node ``/status``). The spoke now
# orchestrates the same sequence as RUN_COMMAND round-trips. The ``cluster``
# field is stamped by the spoke (it knows the agent's cluster from
# connected_agents), matching the Agent's ``self.cluster_name``.

def cluster_resources_cmd() -> str:
    """``pvesh get /cluster/resources`` — primary node-stats source."""
    return pvesh_get("/cluster/resources")


def nodes_list_cmd() -> str:
    """``pvesh get /nodes`` — the fallback node listing."""
    return pvesh_get("/nodes")


def node_status_cmd(node: str) -> str:
    """``pvesh get /nodes/<node>/status`` — per-node detail (pveversion + the
    fallback's cpu/mem/uptime)."""
    return pvesh_get(f"/nodes/{node}/status")


def _parse_json_obj(run_response: Any) -> Dict[str, Any]:
    """Parse a ``pvesh get`` JSON OBJECT from the run response (``/nodes/{n}/status``).
    Returns ``{}`` on any failure (the Agent's per-node lookups are best-effort)."""
    if not runner_ok(run_response):
        return {}
    out = runner_stdout(run_response)
    if not out:
        return {}
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_cluster_resource_nodes(run_response: Any, cluster: str) -> List[Dict[str, Any]]:
    """Primary path: ``/cluster/resources`` filtered to ``type == node`` → the
    node-stats shape the Agent produced (``proxmox_version`` left blank; the
    spoke fills it from a first-node ``/status`` round-trip)."""
    out: List[Dict[str, Any]] = []
    for r in _parse_json_list(run_response):
        if not isinstance(r, dict) or r.get("type") != "node":
            continue
        node_name = r.get("node", "")
        mem_used = r.get("mem", 0)
        mem_total = r.get("maxmem", 1)
        out.append({
            "cluster":         cluster,
            "node":             node_name,
            "status":           r.get("status", "unknown"),
            "cpu_usage":        round(r.get("cpu", 0) * 100, 1),
            "cpu_cores":        r.get("maxcpu", 0),
            "mem_used":         mem_used,
            "mem_total":        mem_total,
            "mem_pct":          round(mem_used / max(mem_total, 1) * 100, 1),
            "uptime":           r.get("uptime", 0),
            "proxmox_version":  "",
        })
    return out


def parse_pveversion(run_response: Any) -> str:
    """The cluster-wide PVE version from a ``/nodes/{n}/status`` object (best-
    effort; ``""`` if unavailable — the Agent leaves proxmox_version blank then)."""
    return _parse_json_obj(run_response).get("pveversion", "") or ""


def parse_nodes_list_entries(run_response: Any) -> List[Dict[str, Any]]:
    """Fallback ``/nodes`` listing: minimal ``{node, status, maxcpu, mem, maxmem,
    uptime}`` per node — the rec the per-node ``/status`` merge falls back to."""
    out: List[Dict[str, Any]] = []
    for n in _parse_json_list(run_response):
        if not isinstance(n, dict) or not n.get("node"):
            continue
        out.append({
            "node":   n.get("node"),
            "status": n.get("status", "unknown"),
            "maxcpu": n.get("maxcpu", 0),
            "mem":    n.get("mem", 0),
            "maxmem": n.get("maxmem", 0),
            "uptime": n.get("uptime", 0),
        })
    return out


def node_from_status(run_response: Any, nrec: Dict[str, Any], cluster: str) -> Dict[str, Any]:
    """Build a node-stats dict from a ``/nodes/{n}/status`` object merged with
    the fallback ``/nodes`` rec (``nrec``). Mirrors the Agent's fallback branch
    (memory/cpuinfo from /status; status/maxcpu/mem/maxmem/uptime fall back to
    the /nodes rec when /status lacks them)."""
    stat = _parse_json_obj(run_response)
    mem = stat.get("memory", {}) if isinstance(stat.get("memory"), dict) else {}
    cpu_info = stat.get("cpuinfo", {}) if isinstance(stat.get("cpuinfo"), dict) else {}
    mem_used = mem.get("used", nrec.get("mem", 0))
    mem_total = mem.get("total", nrec.get("maxmem", 0))
    return {
        "cluster":         cluster,
        "node":             nrec.get("node", ""),
        "status":           nrec.get("status", "unknown"),
        "cpu_usage":        round(stat.get("cpu", 0) * 100, 1),
        "cpu_cores":        cpu_info.get("cpus", nrec.get("maxcpu", 0)),
        "mem_used":         mem_used,
        "mem_total":        mem_total,
        "mem_pct":          round(mem_used / max(mem_total, 1) * 100, 1),
        "uptime":           stat.get("uptime", nrec.get("uptime", 0)),
        "proxmox_version":  stat.get("pveversion", ""),
    }


# ── PXMX_LIST_VMS (multi-round-trip + pool map + interface annotation) ───────
# The Agent's ``get_vm_list`` was the richest family: a best-effort vmid→poolid
# map (``/pools`` + per-pool ``/pools/{pid}`` detail when members aren't inline),
# a base VM list (primary ``/cluster/resources`` type in {qemu,lxc}; fallback
# ``/nodes`` → per-node ``/qemu`` + ``/lxc``), and per-VM interface annotation
# (running: QGA ``network-get-interfaces`` / lxc ``/interfaces``; fallback
# ``qm/pct config`` netN lines for the configured MACs). The spoke now
# orchestrates all of it as RUN_COMMAND round-trips. Annotation round-trips are
# issued CONCURRENTLY from the spoke (send_to_agent multiplexes in-flight
# requests per agent via correlation-id futures), with a 16-concurrent
# semaphore + 12s deadline — mirroring the Agent's ``_annotate_vm_interfaces``.

_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)


def _looks_like_mac(s: str) -> bool:
    """True if ``s`` is a 6-octet MAC (colon or dash separators)."""
    return bool(_MAC_RE.match((s or "").strip()))


def _parse_tags(raw: Any) -> List[str]:
    """Proxmox tags are a ``;``-joined string → split + trimmed list."""
    return [t.strip() for t in (raw or "").split(";") if t.strip()]


# ── pool map ──────────────────────────────────────────────────────────────────

def parse_pools_listing_for_members(run_response: Any) -> List[Dict[str, Any]]:
    """``[{poolid, members}]`` from ``/pools``. ``members`` is the inline member
    list when PVE returns it on the listing, else ``None`` (the spoke then fetches
    ``/pools/{poolid}`` for the detail)."""
    out: List[Dict[str, Any]] = []
    for p in _parse_json_list(run_response):
        if not isinstance(p, dict) or not p.get("poolid"):
            continue
        members = p.get("members")
        out.append({"poolid": p.get("poolid"),
                    "members": members if isinstance(members, list) else None})
    return out


def pool_detail_cmd(poolid: str) -> str:
    """``pvesh get /pools/<poolid>`` — the per-pool detail fetch (members)."""
    return pvesh_get(f"/pools/{poolid}")


def pool_detail_members(run_response: Any) -> List[Dict[str, Any]]:
    """Member dicts from a ``/pools/{pid}`` detail object (``{members:[...]}``).
    ``[]`` on any failure (best-effort — a pool with no detail just maps nothing)."""
    d = _parse_json_obj(run_response)
    members = d.get("members")
    return members if isinstance(members, list) else []


def build_pool_map(pools_listing: List[Dict[str, Any]],
                   details: Dict[str, List[Dict[str, Any]]]) -> Dict[Any, str]:
    """``{vmid: poolid}`` reverse-map. ``pools_listing`` is
    ``parse_pools_listing_for_members`` output; ``details`` maps poolid → the
    member list fetched for pools whose listing ``members`` was ``None``. First
    pool seen wins (a VM shouldn't be in two pools)."""
    out: Dict[Any, str] = {}
    for p in pools_listing:
        pid = p.get("poolid")
        if not pid:
            continue
        members = p.get("members")
        if members is None:
            members = details.get(pid, [])
        for m in (members if isinstance(members, list) else []):
            if isinstance(m, dict) and m.get("vmid") is not None:
                out.setdefault(m.get("vmid"), pid)
    return out


# ── base VM list ──────────────────────────────────────────────────────────────

def _vm_entry(r: Dict[str, Any], node: str, rtype: str, vmid: Any,
              cluster: str, pool_map: Dict[Any, str]) -> Dict[str, Any]:
    """The VM record shape the Agent's ``get_vm_list`` produced. ``interfaces``
    + ``ips`` are left empty here; the spoke fills them via
    ``parse_guest_ifaces``/``parse_config_nets`` after the base list is built."""
    return {
        "unique_id": f"{cluster}/{node}/{vmid}",
        "cluster":   cluster,
        "node":      node,
        "vmid":      vmid,
        "type":      rtype,
        "name":      r.get("name", f"{'vm' if rtype == 'qemu' else 'ct'}-{vmid}"),
        "status":    r.get("status", "unknown"),
        "template":  int(r.get("template", 0) or 0),
        "cpu":       round(r.get("cpu", 0) * 100, 1),
        "mem_bytes": r.get("mem") or r.get("maxmem", 0),
        "uptime":    r.get("uptime", 0),
        "vcpus":     int(r.get("maxcpu", 0) or 0),
        "disk_gb":   round((r.get("maxdisk", 0) or 0) / 1e9, 1),
        "pool":      pool_map.get(vmid, "") if pool_map else "",
        "tags":      _parse_tags(r.get("tags")),
        "interfaces": [],
        "ips":       [],
    }


def parse_cluster_resource_vms(run_response: Any, cluster: str,
                               pool_map: Dict[Any, str]) -> List[Dict[str, Any]]:
    """Primary path: ``/cluster/resources`` filtered to ``type in {qemu, lxc}``."""
    out: List[Dict[str, Any]] = []
    for r in _parse_json_list(run_response):
        if not isinstance(r, dict):
            continue
        rtype = r.get("type")
        if rtype not in ("qemu", "lxc"):
            continue
        vmid = r.get("vmid")
        if vmid is None:
            continue
        out.append(_vm_entry(r, r.get("node", ""), rtype, vmid, cluster, pool_map))
    return out


def node_qemu_cmd(node: str) -> str:
    """``pvesh get /nodes/<node>/qemu`` — fallback per-node QEMU list."""
    return pvesh_get(f"/nodes/{node}/qemu")


def node_lxc_cmd(node: str) -> str:
    """``pvesh get /nodes/<node>/lxc`` — fallback per-node LXC list."""
    return pvesh_get(f"/nodes/{node}/lxc")


def parse_node_vm_list(run_response: Any, node: str, rtype: str, cluster: str,
                       pool_map: Dict[Any, str]) -> List[Dict[str, Any]]:
    """Fallback per-node ``/qemu`` or ``/lxc`` list → the same VM record shape."""
    out: List[Dict[str, Any]] = []
    for r in _parse_json_list(run_response):
        if not isinstance(r, dict):
            continue
        vmid = r.get("vmid")
        if vmid is None:
            continue
        out.append(_vm_entry(r, node, rtype, vmid, cluster, pool_map))
    return out


def node_names(run_response: Any) -> List[str]:
    """Node names from a ``/nodes`` listing — used by the LIST_VMS fallback
    (per-node /qemu + /lxc). Distinct from ``parse_nodes_list_entries`` (which
    carries the node-stats fields); this is just the names the VM loop needs."""
    return [n.get("node") for n in _parse_json_list(run_response)
            if isinstance(n, dict) and n.get("node")]


# ── interface annotation ──────────────────────────────────────────────────────

def vm_guest_ifaces_cmd(node: str, vmid: Any, kind: str) -> str:
    """Running-VM guest interfaces: QGA ``network-get-interfaces`` (qemu) or the
    container netns ``/interfaces`` (lxc). The first annotation round-trip."""
    if kind == "qemu":
        return pvesh_get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
    return pvesh_get(f"/nodes/{node}/lxc/{vmid}/interfaces")


def vm_config_cmd(node: str, vmid: Any, kind: str) -> str:
    """``qm/pct config`` — the configured-MAC fallback when the guest source is
    absent/unresponsive or the VM is stopped. MACs are config so always available."""
    return pvesh_get(f"/nodes/{node}/{kind}/{vmid}/config")


def parse_guest_ifaces(run_response: Any) -> List[Dict[str, Any]]:
    """Normalize QGA ``network-get-interfaces`` / lxc ``/interfaces`` into
    ``[{name, mac, ips}]``. PVE wraps agent responses inconsistently (result/data
    nesting) — unwrapped here. Loopback/zero-MAC pseudo-interfaces are excluded;
    per-interface IPv4s are deduped. ``[]`` on any failure."""
    if not runner_ok(run_response):
        return []
    out_str = runner_stdout(run_response)
    if not out_str:
        return []
    try:
        data = json.loads(out_str)
    except (ValueError, TypeError):
        return []
    result = data
    if isinstance(data, dict):
        result = data.get("result", data.get("data", data))
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    if not isinstance(result, list):
        return []
    out: List[Dict[str, Any]] = []
    seen_names: set = set()
    for iface in result:
        if not isinstance(iface, dict):
            continue
        name = str(iface.get("name") or iface.get("netdev") or "").strip()
        mac = str(iface.get("hardware-address") or iface.get("hwaddr") or "").strip().lower()
        if name.lower() == "lo" or mac == "00:00:00:00:00:00":
            continue
        ips: List[str] = []
        for entry in (iface.get("ip-addresses") or []):
            if str(entry.get("ip-address-type", "")).lower() == "ipv4":
                ip = entry.get("ip-address")
                if isinstance(ip, str) and ip and not ip.startswith(("127.", "169.254.")):
                    ips.append(ip)
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


def parse_config_nets(run_response: Any) -> List[Dict[str, Any]]:
    """Parse a ``qm``/``pct config`` object for ``netN`` entries →
    ``[{name, mac, ips: []}]`` (configured MACs only; no guest IPs). qemu:
    ``net0: "virtio=AA:..,bridge=vmbr0"``; lxc:
    ``net0: "name=eth0,bridge=vmbr0,hwaddr=AA:.."``. ``[]`` on any failure."""
    if not runner_ok(run_response):
        return []
    out_str = runner_stdout(run_response)
    if not out_str:
        return []
    try:
        data = json.loads(out_str)
    except (ValueError, TypeError):
        return []
    cfg = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(cfg, dict):
        return []
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