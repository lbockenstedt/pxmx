"""pxmx spoke — ``ProxmoxSpoke``, the multi-agent bridge.

Bridges the LM Hub control plane to one or more pxmx host agents. The spoke
itself never touches Proxmox directly — it forwards Hub commands to the right
agent (by canonical ``<cluster_name>/<node>/<vmid>`` VM key) and relays agent
telemetry/events back to the Hub. Inherits ``BaseSpoke`` for the common
connect/auth/dispatch loop. Audience: pxmx developers; see the repo
``ARCHITECTURE.md`` for topology and the ``ProxmoxSpoke`` class docstring for
the canonical VM identity key.
"""

import asyncio
import logging
from typing import Any, Dict, List

try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke

logger = logging.getLogger("ProxmoxSpoke")


class ProxmoxSpoke(BaseSpoke):
    """
    Proxmox integration spoke.

    Acts as a bridge between the LM hub and one or more Proxmox agents
    (pxmx-agent processes running on Proxmox hosts).  Multiple agents can
    connect simultaneously — each represents a separate Proxmox installation
    or cluster.

    VM identity — vmid alone is not globally unique (it resets per cluster).
    The canonical unique key for any VM/CT is:

        "<cluster_name>/<node>/<vmid>"

    where cluster_name is the Proxmox cluster name, or the Proxmox host's
    hostname when the node is not part of a cluster.
    """

    def __init__(self, spoke_id: str, config: Dict[str, Any], control_plane=None):
        super().__init__(spoke_id, config)
        self.control_plane = control_plane
        # Per-agent telemetry cache: agent_id → latest telemetry data blob
        self.telemetry_cache: Dict[str, Any] = {}
        # Per-agent Proxmox API config (host/user/password) — persists across reconnects
        self.agent_configs: Dict[str, Any] = {}
        # VNC console: session_id → agent_id, so inbound VNC_FRAME_DOWN /
        # VNC_DISCONNECT from the hub route to the agent that owns the session
        # (recorded when VNC_START passes through). Cleared on VNC_DISCONNECT.
        self.vnc_sessions: Dict[str, str] = {}

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Route a Hub command to the right agent or handle spoke-local (GET_VERSION/UPDATE_CONFIG)."""
        cmd = command_type.upper()

        if cmd == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        if cmd == "UPDATE_CONFIG":
            self.config = data
            if self.control_plane:
                agent_id = data.get("agent_id")
                if agent_id:
                    return await self.control_plane.send_to_agent("UPDATE_CONFIG", data, agent_id=agent_id)
                results = await self.control_plane.broadcast_to_agents("UPDATE_CONFIG", data)
                return {"status": "SUCCESS", "results": results}
            return {"status": "SUCCESS", "message": "Config updated (no agents connected)"}

        if cmd == "SET_AGENT_CONFIG":
            agent_id = data.get("agent_id")
            cfg = data.get("config", {})
            if not agent_id:
                return {"status": "ERROR", "message": "Missing agent_id"}
            # Persist so config is re-pushed on reconnect
            self.agent_configs[agent_id] = cfg
            if self.control_plane:
                return await self.control_plane.send_to_agent("UPDATE_CONFIG", cfg, agent_id=agent_id)
            return {"status": "ERROR", "message": "Agent not connected"}

        if cmd == "GET_AGENTS":
            return self._get_agents()

        if cmd == "SPOKE_RELAY":
            target = data.get("target_agent_id")
            command = data.get("command")
            if command == "APPROVAL_SUCCESS" and target and self.control_plane:
                await self.control_plane.approve_pending_agent(target)
                return {"status": "SUCCESS", "message": f"Agent {target} approved"}
            if command == "REVOKE_AGENT" and target and self.control_plane:
                await self.control_plane.revoke_agent(target)
                return {"status": "SUCCESS", "message": f"Agent {target} disconnected"}
            # Generic forward: relay an arbitrary command + payload to a specific
            # agent (e.g. CS_COMMAND). The agent's dispatch handles it and its
            # AGENT_RESPONSE data is returned to the hub. send_to_agent enforces
            # the 15s sync window — only fast commands belong here; long ops use
            # the accepted+progress pattern (later phase).
            if command and target and self.control_plane:
                inner = data.get("data") or {}
                return await self.control_plane.send_to_agent(command, inner, agent_id=target)
            return {"status": "ERROR", "error": "Unknown relay command"}

        if cmd == "GET_NODE_STATS":
            return await self._get_node_stats(data)

        # VM list — aggregated from all agents
        if cmd in ("PXMX_LIST_VMS", "GET_VM_LIST", "AGENT_GET_VM_LIST"):
            return await self._list_vms(data)

        if cmd == "SEARCH_VMS":
            return await self._search_vms(data)

        if cmd == "GET_VM_INFO":
            return await self._get_vm_info(data)

        if cmd in ("CREATE_VM", "AGENT_CREATE_VM"):
            return await self._route_vm_cmd("CREATE_VM", data)

        if cmd in ("DELETE_VM", "AGENT_DELETE_VM"):
            return await self._route_vm_cmd("DELETE_VM", data)

        # Hypervisors view VM lifecycle (unguarded — any vmid). stop/snapshot
        # can take a few seconds, so allow a 30s agent window.
        if cmd == "PXMX_VM_ACTION":
            return await self._route_vm_cmd("PXMX_VM_ACTION", data, timeout=30.0)

        # Clone-from-template: a tenant clones a template-pool VM. Routed to the
        # agent on the template's node via the template unique_id (cluster prefix)
        # — qm/pct clone operates on the local template. Full-disk clones can
        # take minutes, so allow a 600s agent window (matches clone_vm_any).
        if cmd == "PXMX_CLONE_VM":
            return await self._route_vm_cmd("PXMX_CLONE_VM", data, timeout=600.0)

        # Proxmox resource pool list for the clone/create-VM UI's pool dropdown.
        # Aggregated across every connected agent (each reports its cluster's
        # pools). 15s window — /pools is a fast local pvesh read.
        if cmd == "PXMX_LIST_POOLS":
            pools: list = []
            if self.control_plane:
                for aid, info in (self.control_plane.connected_agents or {}).items():
                    cluster = info.get("cluster_name", aid)
                    try:
                        r = await self.control_plane.send_to_agent(
                            "PXMX_LIST_POOLS", data, agent_id=aid, timeout=15.0)
                        r = r.get("payload", {}).get("data", r) if isinstance(r, dict) else r
                        for p in (r or {}).get("pools", []) if isinstance(r, dict) else []:
                            pools.append({"poolid": p.get("poolid"),
                                          "comment": p.get("comment", ""),
                                          "cluster": cluster})
                    except Exception as e:
                        logger.debug("list_pools agent %s failed: %s", aid, e)
            return {"status": "SUCCESS", "pools": pools}

        # VNC console: agent fetches a Proxmox vncproxy {ticket, port} via local
        # pvesh (fast); the hub opens the authenticated WSS itself.
        if cmd == "VNC_PROXY":
            return await self._route_vm_cmd("VNC_PROXY", data, timeout=15.0)

        # VNC console (agent-terminates-WSS): the hub tells the agent to open a
        # Proxmox vncwebsocket locally and relay frames over the existing
        # agent↔spoke↔hub WS. These are fire-and-forget (send_raw_to_agent) —
        # high-volume VNC_FRAME_DOWN must not block the hub-facing dispatch loop
        # awaiting an agent ack. VNC_START records session→agent so later
        # VNC_FRAME_DOWN/VNC_DISCONNECT resolve the agent without re-parsing
        # unique_id. Returns fast; the agent emits VNC_FRAME_UP/READY/ERROR/
        # DISCONNECT up via AGENT_RELAY_UP.
        if cmd == "VNC_START":
            session_id = data.get("session_id") or ""
            agent_id = data.get("agent_id")
            if not agent_id and session_id:
                agent_id = self.vnc_sessions.get(session_id)
            if not agent_id:
                # Resolve from the unique_id's cluster prefix
                unique_id = data.get("unique_id") or ""
                if "/" in unique_id and self.control_plane:
                    cluster = unique_id.split("/")[0]
                    for aid, info in self.control_plane.connected_agents.items():
                        if info.get("cluster_name") == cluster:
                            agent_id = aid
                            break
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for VNC_START"}
            if session_id:
                self.vnc_sessions[session_id] = agent_id
            await self.control_plane.send_raw_to_agent(agent_id, "VNC_START", data)
            return {"status": "OK", "session_id": session_id}

        if cmd == "VNC_FRAME_DOWN":
            session_id = data.get("session_id") or ""
            agent_id = self.vnc_sessions.get(session_id)
            if agent_id:
                await self.control_plane.send_raw_to_agent(agent_id, "VNC_FRAME_DOWN", data)
            return {"status": "OK"}

        if cmd == "VNC_DISCONNECT":
            session_id = data.get("session_id") or ""
            agent_id = self.vnc_sessions.pop(session_id, None)
            if agent_id:
                await self.control_plane.send_raw_to_agent(agent_id, "VNC_DISCONNECT", data)
            return {"status": "OK"}

        if not self.control_plane:
            return {"status": "ERROR", "error": "Control plane not initialised"}

        # Fallback: forward raw command to a specific agent (agent_id in data) or first
        agent_id = data.get("agent_id")
        return await self.control_plane.send_to_agent(command_type, data, agent_id=agent_id)

    # ── Agent registry ────────────────────────────────────────────────────────

    def _get_agents(self) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "SUCCESS", "agents": [], "pending_agents": []}
        agents = []
        for aid, info in self.control_plane.connected_agents.items():
            agents.append({
                "agent_id":      aid,
                "hostname":      info.get("hostname", aid),
                "cluster_name":  info.get("cluster_name", aid),
                "last_seen":     info.get("last_seen", 0),
                "nodes":         info.get("nodes", []),
                "vm_count":      len(info.get("vms", [])),
                "agent_metrics": info.get("agent_metrics", {}),
                "status":        "connected",
            })
        pending = [
            {"agent_id": aid, "status": "pending"}
            for aid in self.control_plane.pending_agents
        ]
        return {"status": "SUCCESS", "agents": agents, "pending_agents": pending}

    # ── Node stats ────────────────────────────────────────────────────────────

    async def _get_node_stats(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "ERROR", "error": "Control plane not initialised"}

        agent_id = data.get("agent_id")
        if agent_id:
            result = await self.control_plane.send_to_agent("GET_NODE_STATS", {}, agent_id=agent_id)
            return result

        # Aggregate from all agents via telemetry cache (avoid hammering PVE API)
        all_nodes: List[Dict] = []
        for aid, info in self.control_plane.connected_agents.items():
            cluster = info.get("cluster_name", aid)
            for node in info.get("nodes", []):
                all_nodes.append({**node, "agent_id": aid, "cluster": cluster})

        if not all_nodes:
            # Telemetry not yet received — ask agents directly
            results = await self.control_plane.broadcast_to_agents("GET_NODE_STATS", {})
            for res in results:
                aid = res.get("agent_id", "")
                for node in res.get("nodes", []):
                    all_nodes.append({**node, "agent_id": aid})

        if not all_nodes:
            # No agents connected — serve last-known data from disk cache
            disk_cache = getattr(self.control_plane, "disk_cache", {})
            for aid, info in disk_cache.items():
                cluster = info.get("cluster_name", aid)
                for node in info.get("nodes", []):
                    all_nodes.append({**node, "agent_id": aid, "cluster": cluster})
            if all_nodes:
                return {"status": "SUCCESS", "nodes": all_nodes, "stale": True}

        return {"status": "SUCCESS", "nodes": all_nodes}

    # ── VM list (aggregated) ──────────────────────────────────────────────────

    async def _list_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "ERROR", "error": "Control plane not initialised"}

        agent_id  = data.get("agent_id")
        tag_filter = data.get("tag_filter", "").lower() or None

        # Single agent request
        if agent_id:
            result = await self.control_plane.send_to_agent("GET_VM_LIST", {}, agent_id=agent_id)
            vms = result.get("vms", [])
            for vm in vms:
                vm["agent_id"] = agent_id
            return {"status": "SUCCESS", "vms": vms}

        # Aggregate from telemetry cache first (fast, no PVE API call)
        cached_vms: List[Dict] = []
        for aid, info in self.control_plane.connected_agents.items():
            cluster = info.get("cluster_name", aid)
            for vm in info.get("vms", []):
                # Ensure unique_id and agent_id are always present
                vmid = vm.get("vmid", "?")
                node = vm.get("node", "?")
                cached_vms.append({
                    **vm,
                    "agent_id":  aid,
                    "cluster":   vm.get("cluster", cluster),
                    "unique_id": vm.get("unique_id", f"{cluster}/{node}/{vmid}"),
                })

        if tag_filter:
            cached_vms = [v for v in cached_vms
                          if tag_filter in [t.lower() for t in (v.get("tags") or [])]]

        if cached_vms:
            return {"status": "SUCCESS", "vms": cached_vms,
                    "source": "telemetry_cache",
                    "agent_count": len(self.control_plane.connected_agents)}

        # No telemetry yet — live query all agents
        results = await self.control_plane.broadcast_to_agents("GET_VM_LIST", {})
        all_vms: List[Dict] = []
        for res in results:
            aid = res.get("agent_id", "")
            cluster = res.get("cluster", aid)
            for vm in res.get("vms", []):
                vmid = vm.get("vmid", "?")
                node = vm.get("node", "?")
                all_vms.append({
                    **vm,
                    "agent_id":  aid,
                    "cluster":   vm.get("cluster", cluster),
                    "unique_id": vm.get("unique_id", f"{cluster}/{node}/{vmid}"),
                })

        if all_vms:
            return {"status": "SUCCESS", "vms": all_vms, "source": "live_query",
                    "agent_count": len(self.control_plane.connected_agents)}

        # No agents connected — serve last-known data from disk cache
        disk_cache = getattr(self.control_plane, "disk_cache", {})
        if disk_cache:
            stale_vms: List[Dict] = []
            for aid, info in disk_cache.items():
                cluster = info.get("cluster_name", aid)
                for vm in info.get("vms", []):
                    vmid = vm.get("vmid", "?")
                    node = vm.get("node", "?")
                    stale_vms.append({
                        **vm,
                        "agent_id":  aid,
                        "cluster":   vm.get("cluster", cluster),
                        "unique_id": vm.get("unique_id", f"{cluster}/{node}/{vmid}"),
                    })
            if tag_filter:
                stale_vms = [v for v in stale_vms
                             if tag_filter in [t.lower() for t in (v.get("tags") or [])]]
            if stale_vms:
                return {"status": "SUCCESS", "vms": stale_vms, "source": "disk_cache",
                        "stale": True, "agent_count": 0}

        return {"status": "SUCCESS", "vms": all_vms, "source": "live_query",
                "agent_count": len(self.control_plane.connected_agents)}

    # ── VM search ─────────────────────────────────────────────────────────────

    async def _search_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Search VMs/CTs by name, VMID, or unique_id fragment."""
        q = data.get("q", "").strip().lower()
        all_r = await self._list_vms({})
        results = []
        for vm in all_r.get("vms", []):
            if (q in (vm.get("name") or "").lower() or
                    q == str(vm.get("vmid", "")) or
                    q in (vm.get("unique_id") or "").lower() or
                    q in (vm.get("cluster") or "").lower()):
                results.append({
                    "source":    "pxmx",
                    "type":      vm.get("type", "vm"),
                    "name":      vm.get("name", ""),
                    "id":        vm.get("unique_id", ""),
                    "unique_id": vm.get("unique_id", ""),
                    "vmid":      vm.get("vmid"),
                    "node":      vm.get("node", ""),
                    "cluster":   vm.get("cluster", ""),
                    "status":    vm.get("status", ""),
                    "agent_id":  vm.get("agent_id", ""),
                })
        return {"status": "SUCCESS", "results": results, "count": len(results)}

    # ── VM detail / actions ───────────────────────────────────────────────────

    async def _get_vm_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve a VM by unique_id ("<cluster>/<node>/<vmid>") or by agent_id+vmid.
        """
        unique_id = data.get("unique_id") or data.get("vm_id", "")
        agent_id  = data.get("agent_id")

        if not agent_id and "/" in unique_id:
            # Derive agent from cluster name
            cluster = unique_id.split("/")[0]
            for aid, info in (self.control_plane.connected_agents or {}).items():
                if info.get("cluster_name") == cluster:
                    agent_id = aid
                    break

        if not agent_id:
            return {"status": "ERROR", "message": f"Cannot resolve agent for '{unique_id}'"}

        return await self.control_plane.send_to_agent("GET_VM_INFO", data, agent_id=agent_id)

    async def _route_vm_cmd(self, cmd: str, data: Dict[str, Any],
                            timeout: float = 15.0) -> Dict[str, Any]:
        """Route a VM mutation command to the correct agent via unique_id."""
        unique_id = data.get("unique_id", "")
        agent_id  = data.get("agent_id")

        if not agent_id and "/" in unique_id:
            cluster = unique_id.split("/")[0]
            for aid, info in (self.control_plane.connected_agents or {}).items():
                if info.get("cluster_name") == cluster:
                    agent_id = aid
                    break

        if not agent_id:
            if not self.control_plane or not self.control_plane.connected_agents:
                return {"status": "ERROR", "message": "No agents connected"}
            agent_id = next(iter(self.control_plane.connected_agents))

        return await self.control_plane.send_to_agent(cmd, data, agent_id=agent_id,
                                                      timeout=timeout)

    # ── Status / version ──────────────────────────────────────────────────────

    async def get_status(self) -> Dict[str, Any]:
        """Return spoke health — agent count, total VMs across agents, and a HEALTHY/NO_AGENTS status."""
        agent_count = len(self.control_plane.connected_agents) if self.control_plane else 0
        total_vms   = sum(len(info.get("vms", [])) for info in
                          (self.control_plane.connected_agents.values() if self.control_plane else []))
        return {
            "spoke_id":    self.spoke_id,
            "module":      "proxmox",
            "agent_count": agent_count,
            "total_vms":   total_vms,
            "status":      "HEALTHY" if agent_count > 0 else "NO_AGENTS",
        }

    def get_version(self) -> str:
        """Return the pxmx spoke version from the VERSION file, or ``"unknown"``."""
        from pathlib import Path
        try:
            return (Path(__file__).parent.parent / "VERSION").read_text().strip()
        except Exception:
            return "unknown"
