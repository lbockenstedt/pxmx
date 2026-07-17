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
from typing import Any, Dict, List, Optional

try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke

# Spoke-side Proxmox command builder (#4): the spoke constructs pvesh/qm/pct
# command strings and sends them to the dumb Agent as RUN_COMMAND; the Agent just
# executes them. Migrated families live in pve_cmd_builder (one family per
# commit). The Agent's old typed handlers stay as a rollback fallback.
import pve_cmd_builder  # noqa: E402

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
        # Host-shell (xterm terminal): session_id → agent_id (same pattern as
        # vnc_sessions), so SHELL_IN/RESIZE/DISCONNECT route to the owning agent.
        self.shell_sessions: Dict[str, str] = {}

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

        if cmd == "PXMX_RETAG_TENANT":
            # Cross-tenant migration: re-tag VMs carrying old_tag -> new_tag on
            # every managed node. Broadcast to all agents (returns a LIST of
            # {agent_id, **res}); unwrap each agent's result + aggregate counts.
            if not self.control_plane:
                return {"status": "ERROR", "message": "no control plane / agents connected"}
            results = await self.control_plane.broadcast_to_agents("PXMX_RETAG_TENANT", data)

            def _agent_result(item):
                if not isinstance(item, dict):
                    return {}
                p = item.get("payload")
                if isinstance(p, dict) and isinstance(p.get("data"), dict):
                    return p["data"]
                return item

            unwrapped = [_agent_result(r) for r in (results or [])]
            total = sum(int(u.get("count", 0) or 0) for u in unwrapped)
            any_err = any(u.get("status") not in ("SUCCESS", None) for u in unwrapped)
            return {"status": "PARTIAL" if any_err else "SUCCESS",
                    "retagged": total, "results": results,
                    "message": f"re-tagged {total} VM(s) across {len(unwrapped)} node(s)"}

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

        # Bulk VM lifecycle: ONE action over MANY VMs in a single hub message.
        # The spoke groups items by owning agent (unique_id → cluster) and sends
        # ONE PXMX_VM_ACTION_BULK per agent, then merges the per-item results —
        # so the agent inbox gets one message per node, not one per VM.
        if cmd == "PXMX_VM_ACTION_BULK":
            return await self._route_vm_bulk(data, timeout=120.0)

        # Clone-from-template: a tenant clones a template-pool VM. Routed to the
        # agent on the template's node via the template unique_id (cluster prefix)
        # — qm/pct clone operates on the local template. Full-disk clones can
        # take minutes, so allow a 600s agent window (matches clone_vm_any).
        if cmd == "PXMX_CLONE_VM":
            return await self._route_vm_cmd("PXMX_CLONE_VM", data, timeout=600.0)

        # Proxmox resource pool list for the clone/create-VM UI's pool dropdown.
        # Aggregated across every connected agent (each reports its cluster's
        # pools). 15s window — /pools is a fast local pvesh read.
        #
        # #4 migration: the spoke builds `pvesh get /pools` and sends it as
        # RUN_COMMAND; the dumb Agent just runs it + returns stdout. The spoke
        # parses the JSON (parse_pools) and adds the cluster field. The Agent's
        # old typed PXMX_LIST_POOLS handler stays as a rollback fallback (a
        # rolled-back spoke still uses the typed path; RUN_COMMAND is a generic
        # primitive present on every agent, so a new spoke works against any).
        if cmd == "PXMX_LIST_POOLS":
            pools: list = []
            if self.control_plane:
                for aid, info in (self.control_plane.connected_agents or {}).items():
                    cluster = info.get("cluster_name", aid)
                    try:
                        r = await self.control_plane.send_to_agent(
                            "RUN_COMMAND",
                            {"command": pve_cmd_builder.list_pools_cmd(),
                             "allow_shell": True, "timeout": 12},
                            agent_id=aid, timeout=15.0)
                        for p in pve_cmd_builder.parse_pools(r):
                            pools.append({"poolid": p.get("poolid"),
                                          "comment": p.get("comment", ""),
                                          "cluster": cluster})
                    except Exception as e:
                        logger.debug("list_pools agent %s failed: %s", aid, e)
            return {"status": "SUCCESS", "pools": pools}

        # ISO listing for the create-VM-from-ISO flow. Scoped to a node: route
        # to the agent that owns the node (agent_id passed by the hub, resolved
        # from /api/pxmx/nodes). Falls back to the first agent.
        #
        # #4 migration: multi-round-trip (the Agent's list_node_isos was a
        # multi-step pvesh sequence). The spoke: (1) RUN_COMMANDs the node's
        # storage list, picks iso-content storages; (2) RUN_COMMANDs each iso
        # storage's content; (3) flattens the .iso items. The Agent stays a dumb
        # executor; the spoke does the parse/flatten the Agent used to do. The
        # Agent's old typed PXMX_LIST_ISOS handler stays as a rollback fallback.
        if cmd == "PXMX_LIST_ISOS":
            agent_id = data.get("agent_id")
            if not agent_id:
                agent_id = self._agent_for_node(data.get("node", ""))
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for node"}
            node = data.get("node", "") or ""
            cluster = ((self.control_plane.connected_agents or {})
                        .get(agent_id, {}).get("cluster_name", agent_id))
            isos: list = []
            try:
                r = await self.control_plane.send_to_agent(
                    "RUN_COMMAND",
                    {"command": pve_cmd_builder.list_storages_cmd(node),
                     "allow_shell": True, "timeout": 12},
                    agent_id=agent_id, timeout=15.0)
                iso_storages = pve_cmd_builder.storage_names_for_content(r, "iso")
                # One content round-trip per iso storage (typically 1-2/node).
                for storage in iso_storages:
                    try:
                        r2 = await self.control_plane.send_to_agent(
                            "RUN_COMMAND",
                            {"command": pve_cmd_builder.list_iso_content_cmd(node, storage),
                             "allow_shell": True, "timeout": 12},
                            agent_id=agent_id, timeout=15.0)
                        isos.extend(pve_cmd_builder.parse_iso_items(r2, storage))
                    except Exception as e:  # noqa: BLE001 - one storage failing ≠ all
                        logger.debug("iso content %s/%s failed: %s",
                                      node, storage, e)
            except Exception as e:
                logger.debug("list_isos storage-list agent %s failed: %s",
                             agent_id, e)
            return {"status": "SUCCESS", "isos": isos,
                    "node": node, "cluster": cluster}

        if cmd == "PXMX_LIST_STORAGES":
            agent_id = data.get("agent_id")
            if not agent_id:
                agent_id = self._agent_for_node(data.get("node", ""))
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for node"}
            node = data.get("node", "") or ""
            content_filter = data.get("content") or "images"
            # #4 migration: spoke builds `pvesh get /nodes/<node>/storage` and
            # sends it as RUN_COMMAND; the dumb Agent just runs it + returns
            # stdout. The spoke parses + filters by content (parse_storages) and
            # adds node/cluster. The Agent's old typed PXMX_LIST_STORAGES handler
            # stays as a rollback fallback. The agent's list_node_storages
            # always returns [] on failure (never raises) → SUCCESS w/ empty here.
            cluster = ((self.control_plane.connected_agents or {})
                        .get(agent_id, {}).get("cluster_name", agent_id))
            try:
                r = await self.control_plane.send_to_agent(
                    "RUN_COMMAND",
                    {"command": pve_cmd_builder.list_storages_cmd(node),
                     "allow_shell": True, "timeout": 18},
                    agent_id=agent_id, timeout=20.0)
                storages = pve_cmd_builder.parse_storages(r, content_filter)
            except Exception as e:
                logger.debug("list_storages agent %s failed: %s", agent_id, e)
                storages = []
            return {"status": "SUCCESS", "storages": storages,
                    "node": node, "cluster": cluster}

        # Create a new qemu VM from an ISO. Routed to the target node's agent
        # (agent_id from the hub, or resolved from the node). pvesh create is
        # cluster-wide so any agent in the cluster can create on any node. The
        # create itself is fast (no install — just defines the VM); 120s window.
        if cmd == "PXMX_CREATE_VM":
            agent_id = data.get("agent_id")
            if not agent_id:
                agent_id = self._agent_for_node(data.get("node", ""))
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for node"}
            return await self.control_plane.send_to_agent(
                "PXMX_CREATE_VM", data, agent_id=agent_id, timeout=125.0)

        # Hub-brokered cert install: the le spoke issued/renewed a Let's Encrypt
        # cert and the hub pushes INSTALL_CERT here to apply it to a Proxmox
        # node's pveproxy. Routed to the agent that owns the target node
        # (agent_id from the hub, or resolved from `identifier`/`node`); the
        # agent runs `pvenode cert set` on its local node. We can't predict how
        # fast the cert will install or how long pveproxy's restart will take on
        # a loaded node, so give the agent a generous window — 620s > the
        # agent's 600s pvenode wait so the relay never times out first and masks
        # a successful deploy. The agent verifies the deployed cert by
        # fingerprint on its own timeout, so a slow restart still reports SUCCESS.
        # The spoke never touches Proxmox directly.
        if cmd == "INSTALL_CERT":
            agent_id = data.get("agent_id")
            if not agent_id:
                agent_id = self._agent_for_node(
                    data.get("identifier") or data.get("node") or "")
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for cert install"}
            r = await self.control_plane.send_to_agent(
                "INSTALL_CERT", data, agent_id=agent_id, timeout=620.0)
            r = r.get("payload", {}).get("data", r) if isinstance(r, dict) else r
            return r if isinstance(r, dict) else {"status": "ERROR", "message": "agent returned no result"}

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
            # Request/response (NOT send_raw_to_agent): the agent opens the
            # Proxmox vncwebsocket synchronously and returns the ticket, which
            # doubles as the RFB VNC password the browser's noVNC must present.
            # The hub relays it to the WebUI; without it noVNC auths with an
            # empty password and Proxmox drops the RFB session. 25s covers the
            # vncproxy POST + WSS open + first-use root@pam!lm-vnc token mint.
            r = await self.control_plane.send_to_agent(
                "VNC_START", data, agent_id=agent_id, timeout=25.0)
            return r

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

        # Host-shell (xterm terminal) — same routing as VNC. SHELL_START records
        # session→agent (resolved from agent_id/unique_id); SHELL_IN/RESIZE are
        # fire-and-forget (high-volume keystrokes must not block the dispatch
        # loop); the agent emits SHELL_OUT/READY/ERROR/DISCONNECT up via AGENT_RELAY_UP.
        if cmd == "SHELL_START":
            session_id = data.get("session_id") or ""
            agent_id = data.get("agent_id")
            if not agent_id and session_id:
                agent_id = self.shell_sessions.get(session_id)
            if not agent_id:
                unique_id = data.get("unique_id") or ""
                if "/" in unique_id and self.control_plane:
                    cluster = unique_id.split("/")[0]
                    for aid, info in self.control_plane.connected_agents.items():
                        if info.get("cluster_name") == cluster:
                            agent_id = aid
                            break
            if not agent_id and self.control_plane and self.control_plane.connected_agents:
                agent_id = next(iter(self.control_plane.connected_agents))
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for SHELL_START"}
            if session_id:
                self.shell_sessions[session_id] = agent_id
            return await self.control_plane.send_to_agent(
                "SHELL_START", data, agent_id=agent_id, timeout=20.0)

        if cmd in ("SHELL_IN", "SHELL_RESIZE"):
            session_id = data.get("session_id") or ""
            agent_id = self.shell_sessions.get(session_id)
            if agent_id:
                await self.control_plane.send_raw_to_agent(agent_id, cmd, data)
            return {"status": "OK"}

        if cmd == "SHELL_DISCONNECT":
            session_id = data.get("session_id") or ""
            agent_id = self.shell_sessions.pop(session_id, None)
            if agent_id:
                await self.control_plane.send_raw_to_agent(agent_id, "SHELL_DISCONNECT", data)
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
                "version":       info.get("version", "unknown"),
                "status":        "connected",
            })
        pending = [
            {"agent_id": aid, "status": "pending"}
            for aid in self.control_plane.pending_agents
        ]
        return {"status": "SUCCESS", "agents": agents, "pending_agents": pending}

    # ── Node stats ────────────────────────────────────────────────────────────

    async def _node_stats_from_agent(self, agent_id: str) -> Dict[str, Any]:
        """GET_NODE_STATS via multi-round-trip RUN_COMMAND (#4).

        Reproduces the Agent's ``get_node_stats`` orchestration on the spoke so
        the Agent is a dumb executor: primary ``pvesh get /cluster/resources``
        (type==node) → one first-node ``/status`` for the cluster-wide
        pveversion; fallback ``pvesh get /nodes`` → per-node ``/status``. The
        ``cluster`` field is stamped from ``connected_agents`` (the Agent used
        ``self.cluster_name``). On a total agent failure returns ``{nodes:[],
        error}`` (the Agent's outer-try shape); a pvesh error alone yields empty
        nodes (read-only, non-fatal), matching the Agent.
        """
        info = (self.control_plane.connected_agents or {}).get(agent_id, {}) \
            if self.control_plane else {}
        cluster = info.get("cluster_name", agent_id)

        async def _send(cmd: str, timeout: float = 12.0):
            return await self.control_plane.send_to_agent(
                "RUN_COMMAND",
                {"command": cmd, "allow_shell": True, "timeout": timeout},
                agent_id=agent_id, timeout=15.0)

        try:
            # Primary: /cluster/resources filtered to type==node.
            res = await _send(pve_cmd_builder.cluster_resources_cmd())
            nodes = pve_cmd_builder.parse_cluster_resource_nodes(res, cluster)
            if nodes:
                # Best-effort cluster-wide pveversion from the first node.
                try:
                    stat = await _send(pve_cmd_builder.node_status_cmd(nodes[0]["node"]))
                    pve_ver = pve_cmd_builder.parse_pveversion(stat)
                    if pve_ver:
                        for n in nodes:
                            n["proxmox_version"] = pve_ver
                except Exception as e:  # node-status trip failure is non-fatal
                    logger.debug("pxmx GET_NODE_STATS pveversion for %s: %s", agent_id, e)
                return {"nodes": nodes, "cluster": cluster}

            # Fallback: /nodes listing → per-node /status.
            res = await _send(pve_cmd_builder.nodes_list_cmd())
            entries = pve_cmd_builder.parse_nodes_list_entries(res)
            nodes = []
            for nrec in entries:
                try:
                    stat = await _send(pve_cmd_builder.node_status_cmd(nrec["node"]))
                    nodes.append(pve_cmd_builder.node_from_status(stat, nrec, cluster))
                except Exception as e:  # one node's /status failing is non-fatal
                    logger.debug("pxmx GET_NODE_STATS node %s: %s", nrec.get("node"), e)
            return {"nodes": nodes, "cluster": cluster}
        except Exception as e:
            # send_to_agent raised (agent unreachable) — Agent's total-failure shape.
            logger.warning("pxmx GET_NODE_STATS agent %s failed: %s", agent_id, e)
            return {"nodes": [], "error": str(e)}

    async def _get_node_stats(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "ERROR", "error": "Control plane not initialised"}

        agent_id = data.get("agent_id")
        if agent_id:
            # Multi-round-trip from the spoke (#4): build pvesh commands + send
            # RUN_COMMAND to the dumb Agent, orchestrating the parse/merge the
            # Agent's get_node_stats used to do. Returns the Agent's shape
            # ({nodes, cluster}) verbatim so the hub sees the same contract.
            return await self._node_stats_from_agent(agent_id)

        # Aggregate from all agents via telemetry cache (avoid hammering PVE API)
        all_nodes: List[Dict] = []
        for aid, info in self.control_plane.connected_agents.items():
            cluster = info.get("cluster_name", aid)
            for node in info.get("nodes", []):
                all_nodes.append({**node, "agent_id": aid, "cluster": cluster})

        if not all_nodes:
            # Telemetry not yet received — orchestrate per-agent RUN_COMMAND
            # round-trips (same helper as the pinned path) instead of the typed
            # GET_NODE_STATS the Agent used to answer. Best-effort per agent; an
            # agent that fails (unreachable/pvesh error) contributes no nodes.
            for aid in list(self.control_plane.connected_agents or {}):
                res = await self._node_stats_from_agent(aid)
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

    async def _vms_from_agent(self, agent_id: str) -> Dict[str, Any]:
        """PXMX_LIST_VMS via multi-round-trip RUN_COMMAND (#4).

        Reproduces the Agent's ``get_vm_list`` on the spoke so the Agent is a dumb
        executor: (1) best-effort vmid→poolid map from ``/pools`` (+ per-pool
        ``/pools/{pid}`` detail when members aren't inline); (2) base VM list —
        primary ``/cluster/resources`` filtered to qemu/lxc, fallback ``/nodes``
        → per-node ``/qemu`` + ``/lxc``; (3) per-VM interface annotation issued
        CONCURRENTLY (send_to_agent multiplexes in-flight requests per agent) with
        a 16-concurrent semaphore + 12s deadline, mirroring the Agent's
        ``_annotate_vm_interfaces``. ``cluster`` is stamped from
        ``connected_agents`` (the Agent used ``self.cluster_name``). On an
        unreachable agent returns ``{status:ERROR, message}`` (so the pinned path
        surfaces it honestly, not as "0 VMs synced, success"); on a reachable-but-
        failed query returns the Agent's ``{vms:[], cluster, error}`` shape."""
        info = (self.control_plane.connected_agents or {}).get(agent_id, {}) \
            if self.control_plane else {}
        cluster = info.get("cluster_name", agent_id)

        async def _send(cmd: str, timeout: float = 12.0):
            return await self.control_plane.send_to_agent(
                "RUN_COMMAND",
                {"command": cmd, "allow_shell": True, "timeout": timeout},
                agent_id=agent_id, timeout=15.0)

        # Reachability check: a RUN_COMMAND to an unreachable agent returns the
        # agent-level ERROR dict (not a runner dict). Surface it honestly so the
        # pinned sync records an 'error' status instead of an empty 'success'.
        probe = await _send(pve_cmd_builder.list_pools_cmd())
        if isinstance(probe, dict) and probe.get("status") == "ERROR":
            return {"status": "ERROR",
                    "message": probe.get("message", f"agent {agent_id} unreachable")}

        try:
            pool_map = await self._pool_map_from_agent(agent_id, _send, probe)

            # Primary: /cluster/resources filtered to qemu/lxc.
            r = await _send(pve_cmd_builder.cluster_resources_cmd())
            vms = pve_cmd_builder.parse_cluster_resource_vms(r, cluster, pool_map)
            if not vms:
                # Fallback: /nodes → per-node /qemu + /lxc.
                rn = await _send(pve_cmd_builder.nodes_list_cmd())
                for node in pve_cmd_builder.node_names(rn):
                    rq = await _send(pve_cmd_builder.node_qemu_cmd(node))
                    vms += pve_cmd_builder.parse_node_vm_list(rq, node, "qemu", cluster, pool_map)
                    rl = await _send(pve_cmd_builder.node_lxc_cmd(node))
                    vms += pve_cmd_builder.parse_node_vm_list(rl, node, "lxc", cluster, pool_map)

            await self._annotate_vm_interfaces(agent_id, vms, _send)
            return {"vms": vms, "cluster": cluster}
        except Exception as e:
            logger.warning("pxmx PXMX_LIST_VMS agent %s failed: %s", agent_id, e)
            return {"vms": [], "cluster": cluster, "error": str(e)}

    async def _pool_map_from_agent(self, agent_id: str, _send, probe) -> Dict[Any, str]:
        """Best-effort ``{vmid: poolid}`` from ``/pools`` (+ per-pool detail).
        ``probe`` is the already-fetched ``/pools`` response (the reachability
        check round-trip is reused rather than re-sent). ``{}`` on any failure."""
        try:
            listing = pve_cmd_builder.parse_pools_listing_for_members(probe)
            details: Dict[str, List[Dict[str, Any]]] = {}
            for p in listing:
                if p.get("members") is None:
                    details[p["poolid"]] = pve_cmd_builder.pool_detail_members(
                        await _send(pve_cmd_builder.pool_detail_cmd(p["poolid"])))
            return pve_cmd_builder.build_pool_map(listing, details)
        except Exception as e:  # pool map is best-effort — never sink the VM list
            logger.debug("pxmx pool map for %s unavailable: %s", agent_id, e)
            return {}

    async def _annotate_vm_interfaces(self, agent_id: str, vms: List[Dict[str, Any]],
                                      _send) -> None:
        """Per-VM interface annotation in parallel — bounded by a 16-concurrent
        semaphore and a 12s deadline so a hung guest agent can't stall the list.
        Mirrors the Agent's ``_annotate_vm_interfaces`` (which the telemetry loop
        still uses internally). Best-effort: VMs not annotated before the deadline
        keep ``interfaces=[]``/``ips=[]`` (filled next telemetry tick)."""
        targets = [v for v in vms if v.get("node") and v.get("vmid") not in (None, "")]
        if not targets:
            return
        sem = asyncio.Semaphore(16)

        async def _one(v):
            async with sem:
                try:
                    ifaces = await self._vm_interfaces(_send, v)
                except Exception:  # one VM's annotation failure is non-fatal
                    ifaces = []
                v["interfaces"] = ifaces
                v["ips"] = [ip for i in ifaces for ip in (i.get("ips") or [])]

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_one(v) for v in targets], return_exceptions=True),
                timeout=12)
        except asyncio.TimeoutError:
            pass  # partial — un-annotated VMs keep interfaces=[]/ips=[]

    async def _vm_interfaces(self, _send, v: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Best-effort ``[{name, mac, ips}]`` for one VM/CT. Running → guest
        interfaces (QGA / lxc netns); stopped or empty → ``qm/pct config`` netN
        MACs. Mirrors the Agent's ``_vm_interfaces`` (4s per-call timeout)."""
        node, vmid = v.get("node", ""), v.get("vmid")
        kind = "qemu" if v.get("type") == "qemu" else "lxc"
        status = v.get("status")
        ifaces: List[Dict[str, Any]] = []
        if status == "running":
            try:
                r = await _send(pve_cmd_builder.vm_guest_ifaces_cmd(node, vmid, kind),
                                timeout=4.0)
                ifaces = pve_cmd_builder.parse_guest_ifaces(r)
            except Exception:
                ifaces = []
        if not ifaces:  # QGA absent / stopped / empty → configured MACs
            try:
                r = await _send(pve_cmd_builder.vm_config_cmd(node, vmid, kind),
                                timeout=4.0)
                ifaces = pve_cmd_builder.parse_config_nets(r)
            except Exception:
                ifaces = []
        return ifaces

    async def _list_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "ERROR", "error": "Control plane not initialised"}

        agent_id  = data.get("agent_id")
        tag_filter = data.get("tag_filter", "").lower() or None

        # Single agent request (sync scoped to one pinned Proxmox server).
        # The tenant tag_filter still applies — pinning a server must NOT bypass
        # tenant scoping (otherwise every tenant's VMs on that server would sync).
        # If the pinned agent is unreachable, send_to_agent returns an ERROR dict;
        # surface it honestly so the hub records an 'error' sync status instead
        # of silently reading an empty vms list as "0 records synced, success".
        if agent_id:
            # Multi-round-trip from the spoke (#4): orchestrate pool map + base
            # list + per-VM interface annotation as RUN_COMMAND round-trips. The
            # pinned path MUST surface an unreachable agent honestly (an ERROR
            # dict) so the hub records an 'error' sync status instead of reading
            # an empty vms list as "0 records synced, success".
            result = await self._vms_from_agent(agent_id)
            if not isinstance(result, dict) or result.get("status") == "ERROR":
                logger.warning("PXMX_LIST_VMS pinned agent %r unreachable: %s",
                               agent_id, result if isinstance(result, dict) else "non-dict")
                return result if isinstance(result, dict) else {"status": "ERROR",
                                                                "message": "agent returned no data"}
            cluster = result.get("cluster", agent_id)
            vms = []
            for vm in result.get("vms", []):
                vm = dict(vm) if isinstance(vm, dict) else {}
                vm["agent_id"] = agent_id
                vm.setdefault("cluster", cluster)
                vm.setdefault("unique_id", f"{cluster}/{vm.get('node','?')}/{vm.get('vmid','?')}")
                vms.append(vm)
            if tag_filter:
                vms = [v for v in vms
                       if tag_filter in [t.lower() for t in (v.get("tags") or [])]]
            logger.info("PXMX_LIST_VMS pinned agent=%s tag_filter=%r -> %d VMs",
                        agent_id, tag_filter, len(vms))
            return {"status": "SUCCESS", "vms": vms, "source": "pinned_agent",
                    "agent_count": 1}

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
            logger.info("PXMX_LIST_VMS aggregate tag_filter=%r -> %d VMs (telemetry_cache, %d agents)",
                        tag_filter, len(cached_vms), len(self.control_plane.connected_agents))
            return {"status": "SUCCESS", "vms": cached_vms,
                    "source": "telemetry_cache",
                    "agent_count": len(self.control_plane.connected_agents)}

        # No telemetry yet — live query all agents via the same RUN_COMMAND
        # orchestration as the pinned path (concurrent across agents; each
        # agent's annotation round-trips are concurrent within). An unreachable
        # agent returns {status:ERROR} and contributes nothing (honest skip).
        aids = list(self.control_plane.connected_agents or {})
        results = await asyncio.gather(
            *[self._vms_from_agent(a) for a in aids], return_exceptions=True)
        all_vms: List[Dict] = []
        for aid, res in zip(aids, results):
            if isinstance(res, Exception) or not isinstance(res, dict):
                continue
            if res.get("status") == "ERROR":
                continue  # unreachable agent — skip, don't sink the aggregate
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

        if tag_filter:
            all_vms = [v for v in all_vms
                       if tag_filter in [t.lower() for t in (v.get("tags") or [])]]

        if all_vms:
            logger.info("PXMX_LIST_VMS aggregate tag_filter=%r -> %d VMs (live_query, %d agents)",
                        tag_filter, len(all_vms), len(self.control_plane.connected_agents))
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

    def _agent_for_node(self, node: str) -> Optional[str]:
        """Resolve the agent_id whose ``nodes`` list contains ``node``. Falls
        back to the first connected agent when none matches (single-node /
        standalone). Used by PXMX_LIST_ISOS / PXMX_LIST_STORAGES /
        PXMX_CREATE_VM which are node-scoped but routed via agent_id."""
        if not self.control_plane:
            return None
        agents = self.control_plane.connected_agents or {}
        if not agents:
            return None
        if node:
            node_l = node.lower()
            for aid, info in agents.items():
                nodes = [str(n).lower() for n in (info.get("nodes") or [])]
                if node_l in nodes:
                    return aid
        return next(iter(agents))

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

    async def _route_vm_bulk(self, data: Dict[str, Any],
                             timeout: float = 120.0) -> Dict[str, Any]:
        """Group a bulk VM action by owning agent (unique_id → cluster → agent)
        and send ONE PXMX_VM_ACTION_BULK per agent, then merge the per-item
        results. One message per node instead of one per VM."""
        items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        action = data.get("action")
        agents = (self.control_plane.connected_agents if self.control_plane else None) or {}
        if not agents:
            return {"status": "ERROR", "message": "No agents connected"}
        cluster_to_agent = {}
        for aid, info in agents.items():
            cn = info.get("cluster_name")
            if cn and cn not in cluster_to_agent:
                cluster_to_agent[cn] = aid
        default_agent = next(iter(agents))
        groups: Dict[str, list] = {}
        for it in items:
            uid = it.get("unique_id", "") or ""
            aid = it.get("agent_id")
            if not aid and "/" in uid:
                aid = cluster_to_agent.get(uid.split("/")[0])
            groups.setdefault(aid or default_agent, []).append(it)

        merged: list = []
        for aid, grp in groups.items():
            try:
                resp = await self.control_plane.send_to_agent(
                    "PXMX_VM_ACTION_BULK", {"action": action, "items": grp},
                    agent_id=aid, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                resp = {"status": "ERROR", "message": str(e)}
            inner = resp.get("payload", {}).get("data", resp) if isinstance(resp, dict) else resp
            rows = (inner or {}).get("results") if isinstance(inner, dict) else None
            if rows:
                merged.extend(rows)
            else:
                err = (inner or {}).get("message", "bulk relay failed") if isinstance(inner, dict) else "bulk relay failed"
                merged.extend({"vmid": it.get("vmid"), "ok": False, "error": err} for it in grp)
        return {"status": "SUCCESS", "results": merged}

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
