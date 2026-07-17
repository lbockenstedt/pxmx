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
    """Build a ``pvesh get <path>`` command string for ``RUN_COMMAND``.

    ``pvesh`` prints JSON to stdout; the Agent runs it via the login shell so
    ``pvesh`` resolves on PATH (root on Proxmox has ``/usr/sbin``). The path is
    shell-quoted (node/storage names are safe but quoting is correct)."""
    return f"pvesh get {shlex.quote(path)}"


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