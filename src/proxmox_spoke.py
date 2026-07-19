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

        # Hypervisors view VM lifecycle (unguarded — any vmid; cs_guard does NOT
        # apply — these are real tenant VMs, not the sim 90000 floor).
        # #4 migration: the spoke builds the qm/pct/pvesm/vzdump command string
        # and sends it as RUN_COMMAND; the dumb Agent just runs it. start/stop/
        # snapshot are foreground (await + check rc); reboot/backup are fire-and-
        # forget (backgrounded). The Agent's old typed PXMX_VM_ACTION handler
        # stays as a rollback fallback.
        if cmd == "PXMX_VM_ACTION":
            agent_id = self._resolve_agent_for_vm(data)
            if not agent_id:
                return {"status": "ERROR", "message": "No agents connected"}
            return await self._vm_action_via_agent(agent_id, data)

        # Bulk VM lifecycle: ONE action over MANY VMs. The spoke groups items by
        # owning agent (unique_id → cluster → agent) and runs them CONCURRENTLY
        # per agent (6-semaphore, mirroring the Agent's _bulk_one) via RUN_COMMAND
        # — send_to_agent multiplexes in-flight requests per agent — then merges
        # the per-item results. One VM's failure never sinks the rest.
        if cmd == "PXMX_VM_ACTION_BULK":
            return await self._route_vm_bulk(data)

        # Clone-from-template: a tenant clones a template-pool VM. Routed to the
        # agent on the template's node via the template unique_id (cluster prefix)
        # — qm/pct clone operates on the local template.
        # #4 migration: the spoke orchestrates 3 RUN_COMMAND round-trips —
        # /cluster/nextid (atomic free VMID, no TOCTOU) → qm/pct clone → qm/pct
        # set --tags (best-effort) — + a template-config tag read. The Agent's
        # old typed PXMX_CLONE_VM handler stays as a rollback fallback.
        if cmd == "PXMX_CLONE_VM":
            agent_id = self._resolve_agent_for_vm(data)
            if not agent_id:
                return {"status": "ERROR", "message": "No agents connected"}
            return await self._clone_vm_via_agent(agent_id, data)

        # Proxmox resource pool list for the clone/create-VM UI's pool dropdown.
        # Aggregated across every connected agent (each reports its cluster's
        # pools). 15s window per agent — all agents queried in PARALLEL
        # (asyncio.gather) so total wall time is ~one agent's, not N×15s.
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
                agent_items = list((self.control_plane.connected_agents or {}).items())

                async def _pools_one(aid, info):
                    cluster = info.get("cluster_name", aid)
                    out: list = []
                    try:
                        r = await self.control_plane.send_to_agent(
                            "RUN_COMMAND",
                            {"command": pve_cmd_builder.list_pools_cmd(),
                             "allow_shell": True, "timeout": 12},
                            agent_id=aid, timeout=15.0)
                        for p in pve_cmd_builder.parse_pools(r):
                            out.append({"poolid": p.get("poolid"),
                                        "comment": p.get("comment", ""),
                                        "cluster": cluster})
                    except Exception as e:
                        logger.debug("list_pools agent %s failed: %s", aid, e)
                    return out

                for chunk in await asyncio.gather(
                        *[_pools_one(aid, info) for aid, info in agent_items]):
                    pools.extend(chunk)
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
        # cluster-wide so any agent in the cluster can create on any node.
        # #4 migration: the spoke orchestrates 2 RUN_COMMAND round-trips —
        # /cluster/nextid (atomic free VMID) → pvesh create /nodes/<node>/qemu
        # with the ISO/disk/memory/cores args + tenant tags. The Agent's old
        # typed PXMX_CREATE_VM handler stays as a rollback fallback.
        if cmd == "PXMX_CREATE_VM":
            agent_id = data.get("agent_id")
            if not agent_id:
                agent_id = self._agent_for_node(data.get("node", ""))
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for node"}
            return await self._create_vm_via_agent(agent_id, data)

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
            # All agents queried in PARALLEL (gather) — same bounded-fan-out
            # treatment as _list_vms' live query and PXMX_LIST_POOLS.
            aids = list(self.control_plane.connected_agents or {})
            results = await asyncio.gather(
                *[self._node_stats_from_agent(a) for a in aids],
                return_exceptions=True)
            for aid, res in zip(aids, results):
                if isinstance(res, Exception) or not isinstance(res, dict):
                    continue
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
        """Resolve a single VM (ips/tags/pool + detail) or the fleet list.

        The Agent never had a GET_VM_INFO handler (it returned "Unknown command"),
        so this is a spoke-side implementation via RUN_COMMAND (#4), reusing the
        LIST_VMS builders. Two variants:

        - ``vm_id == "all"`` (or absent) → fleet list across connected agents
          (``{status:SUCCESS, vms:[...], cluster}``); used by the admin aggregate.
        - ``vm_id == "<cluster>/<node>/<vmid>"`` (or ``{vmid, node}``) → the single
          VM record (``{status:SUCCESS, <vm fields>}`` with ips/tags/pool); used by
          the VM detail page + VNC/VM-action ownership (fail-closed: an
          unattributable VM returns ERROR so the hub 403s).

        Single-VM is a TARGETED fetch (``/cluster/resources`` for the one VM +
        short-circuit pool lookup + that VM's interface annotation), not a full
        LIST_VMS — the detail/VNC-ownership path is frequent and must not annotate
        the whole fleet."""
        unique_id = data.get("unique_id") or data.get("vm_id", "")
        agent_id = data.get("agent_id")
        node = data.get("node") or ""
        vmid = data.get("vmid")

        if unique_id == "all":
            return await self._all_vms_info()

        # Parse node/vmid from "<cluster>/<node>/<vmid>" when not supplied directly.
        if (not node or vmid in (None, "")) and "/" in unique_id:
            parts = unique_id.split("/")
            if len(parts) >= 3:
                cluster_part = parts[0]
                node = node or parts[1]
                vmid = parts[2]
                if not agent_id:
                    for aid, info in (self.control_plane.connected_agents or {}).items():
                        if info.get("cluster_name") == cluster_part:
                            agent_id = aid
                            break

        if not agent_id and node:
            agent_id = self._agent_for_node(node)
        if not agent_id and self.control_plane and self.control_plane.connected_agents:
            agent_id = next(iter(self.control_plane.connected_agents))
        if not agent_id:
            return {"status": "ERROR", "message": f"Cannot resolve agent for '{unique_id}'"}

        return await self._single_vm_info(agent_id, node, vmid, unique_id)

    async def _all_vms_info(self) -> Dict[str, Any]:
        """Fleet VM list across all connected agents (``vm_id:'all'``). Concurrent
        per agent; an unreachable agent is skipped (not fatal)."""
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "SUCCESS", "vms": [], "cluster": ""}
        aids = list(self.control_plane.connected_agents)
        results = await asyncio.gather(
            *[self._vms_from_agent(a) for a in aids], return_exceptions=True)
        all_vms: List[Dict[str, Any]] = []
        cluster = ""
        for aid, res in zip(aids, results):
            if isinstance(res, Exception) or not isinstance(res, dict):
                continue
            if res.get("status") == "ERROR":
                continue
            cluster = cluster or res.get("cluster", "")
            for vm in res.get("vms", []):
                all_vms.append({**vm, "agent_id": aid})
        return {"status": "SUCCESS", "vms": all_vms, "cluster": cluster}

    async def _single_vm_info(self, agent_id: str, node: str, vmid: Any,
                              unique_id: str) -> Dict[str, Any]:
        """Targeted single-VM fetch: /cluster/resources for the one VM (type,
        status, tags, cpu, mem) + a short-circuit pool lookup + that VM's
        interface annotation. Falls back to per-node /qemu+/lxc if the VM isn't
        in /cluster/resources."""
        info = (self.control_plane.connected_agents or {}).get(agent_id, {}) \
            if self.control_plane else {}
        cluster = info.get("cluster_name", agent_id)

        async def _send(cmd: str, timeout: float = 12.0):
            return await self.control_plane.send_to_agent(
                "RUN_COMMAND",
                {"command": cmd, "allow_shell": True, "timeout": timeout},
                agent_id=agent_id, timeout=15.0)

        probe = await _send(pve_cmd_builder.list_pools_cmd())
        if isinstance(probe, dict) and probe.get("status") == "ERROR":
            return {"status": "ERROR",
                    "message": probe.get("message", f"agent {agent_id} unreachable")}

        try:
            pool = await self._pool_for_vmid(agent_id, _send, probe, vmid)

            def _match(v):
                return str(v.get("vmid")) == str(vmid) and (not node or v.get("node") == node)

            # Primary: /cluster/resources.
            r = await _send(pve_cmd_builder.cluster_resources_cmd())
            vms = pve_cmd_builder.parse_cluster_resource_vms(r, cluster, {})
            vm = next((v for v in vms if _match(v)), None)

            # Fallback: per-node /qemu + /lxc (a VM missing from /cluster/resources).
            if vm is None and node:
                for kind, cmd in (("qemu", pve_cmd_builder.node_qemu_cmd(node)),
                                  ("lxc", pve_cmd_builder.node_lxc_cmd(node))):
                    rr = await _send(cmd)
                    vms2 = pve_cmd_builder.parse_node_vm_list(rr, node, kind, cluster, {})
                    vm = next((v for v in vms2 if _match(v)), None)
                    if vm is not None:
                        break

            if vm is None:
                return {"status": "ERROR",
                        "message": f"VM {unique_id} not found on agent {agent_id}"}

            if pool:
                vm["pool"] = pool
            await self._annotate_vm_interfaces(agent_id, [vm], _send)
            # The VM record carries its own ``status`` (running/stopped); rename it
            # to ``vm_status`` so it doesn't clobber the envelope ``status:SUCCESS``
            # the hub checks (pxmx.py:208) — ips/tags/pool stay top-level for the
            # fail-closed ownership probe (pxmx_vm.py:43).
            vm["vm_status"] = vm.pop("status")
            return {"status": "SUCCESS", **vm}
        except Exception as e:
            logger.warning("pxmx GET_VM_INFO %s failed: %s", unique_id, e)
            return {"status": "ERROR", "message": str(e)}

    async def _pool_for_vmid(self, agent_id: str, _send, probe, vmid: Any) -> str:
        """Short-circuit: the first poolid whose members contain ``vmid``. Reuses
        the ``/pools`` probe (no extra round-trip); fetches ``/pools/{pid}`` detail
        only for pools whose listing didn't include members inline, stopping at the
        first hit. ``""`` on any failure (best-effort — pool isn't a hard gate)."""
        try:
            listing = pve_cmd_builder.parse_pools_listing_for_members(probe)
            for p in listing:
                members = p.get("members")
                if members is None:
                    detail = await _send(pve_cmd_builder.pool_detail_cmd(p["poolid"]))
                    members = pve_cmd_builder.pool_detail_members(detail)
                for m in (members if isinstance(members, list) else []):
                    if isinstance(m, dict) and str(m.get("vmid")) == str(vmid):
                        return p["poolid"]
            return ""
        except Exception as e:  # best-effort
            logger.debug("pxmx pool-for-vmid %s unavailable: %s", agent_id, e)
            return ""

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

    def _resolve_agent_for_vm(self, data: Dict[str, Any]) -> Optional[str]:
        """Resolve the agent_id for a VM command: explicit ``agent_id`` → the
        ``unique_id`` cluster prefix → fall back to the first connected agent.
        Returns ``None`` when no agent is connected."""
        agent_id = data.get("agent_id")
        unique_id = data.get("unique_id", "")
        if not agent_id and "/" in unique_id and self.control_plane:
            cluster = unique_id.split("/")[0]
            for aid, info in (self.control_plane.connected_agents or {}).items():
                if info.get("cluster_name") == cluster:
                    agent_id = aid
                    break
        if not agent_id:
            if not self.control_plane or not self.control_plane.connected_agents:
                return None
            agent_id = next(iter(self.control_plane.connected_agents))
        return agent_id

    async def _route_vm_cmd(self, cmd: str, data: Dict[str, Any],
                            timeout: float = 15.0) -> Dict[str, Any]:
        """Route a still-typed VM command (VNC_PROXY) to the correct agent via
        unique_id. The mutating families now use the RUN_COMMAND paths below."""
        agent_id = self._resolve_agent_for_vm(data)
        if not agent_id:
            return {"status": "ERROR", "message": "No agents connected"}
        return await self.control_plane.send_to_agent(cmd, data, agent_id=agent_id,
                                                      timeout=timeout)

    # ── Mutating VM lifecycle via RUN_COMMAND (#4 family #5) ───────────────────
    # The spoke builds qm/pct/pvesm/vzdump/pvesh command strings; the dumb Agent
    # runs them and returns {ok, rc, stdout, stderr}. cs_guard does NOT apply
    # (unguarded tenant VMs). The Agent's typed handlers stay as a rollback
    # fallback (a rolled-back spoke uses the typed path; RUN_COMMAND is generic).

    async def _vm_action_one(self, agent_id: str, item: Dict[str, Any],
                             action: str) -> Dict[str, Any]:
        """One VM action via RUN_COMMAND. Returns a bulk-row-shaped dict:
        ``{vmid, ok: True, ...action-result}`` or ``{vmid, ok: False, error}``.
        Used by both the single PXMX_VM_ACTION (one item) and the bulk path."""
        vmid = item.get("vmid")
        kind = (item.get("type") or "").lower()
        snapshot_name = item.get("snapshot_name")
        backup_opts = item.get("backup") or {}
        try:
            if vmid is None:
                raise pve_cmd_builder.PveCmdError("vmid is required")
            vid = int(vmid)

            async def _send(cmd: str, timeout: float = 30.0):
                return await self.control_plane.send_to_agent(
                    "RUN_COMMAND",
                    {"command": cmd, "allow_shell": True, "timeout": timeout},
                    agent_id=agent_id, timeout=timeout + 3.0)

            # Resolve kind if the hub didn't pass it (one probe round-trip).
            if kind not in ("qemu", "lxc"):
                kind = pve_cmd_builder.kind_from_probe(
                    await _send(pve_cmd_builder.detect_kind_cmd(vid), timeout=10.0))

            act = (action or "").lower()
            if act in ("start", "stop", "snapshot"):
                cmd = pve_cmd_builder.vm_action_cmd(vid, act, kind, snapshot_name)
                r = await _send(cmd, timeout=30.0)
                if not pve_cmd_builder.runner_ok(r):
                    raise pve_cmd_builder.PveCmdError(pve_cmd_builder.runner_err(r))
                row: Dict[str, Any] = {"vmid": vid, "action": act, "kind": kind}
                if act == "snapshot":
                    row["snapshot"] = snapshot_name or pve_cmd_builder.default_snapshot_name()
                return {"ok": True, **row}
            if act in ("reboot", "restart"):
                # Foreground (qm reset is a fast hardware reset) + rc check, so a
                # failed reset (e.g. VM not running) surfaces as an error instead
                # of a silent false-success toast. Previously this was fire-and-
                # forget (backgrounded, output discarded) — RUN_COMMAND returned
                # rc=0 for launching the job and the hub toasted success even
                # when qm reset never ran.
                r = await _send(pve_cmd_builder.vm_reboot_cmd(vid, kind), timeout=30.0)
                if not pve_cmd_builder.runner_ok(r):
                    raise pve_cmd_builder.PveCmdError(pve_cmd_builder.runner_err(r))
                return {"ok": True, "vmid": vid, "action": "reboot", "kind": kind,
                        "method": "reset" if kind == "qemu" else "reboot",
                        "started": True}
            if act == "backup":
                storage = str((backup_opts or {}).get("storage") or "").strip()
                if not storage:
                    raise pve_cmd_builder.PveCmdError(
                        "backup: no storage configured — set one in Setup → Hypervisors")
                r = await _send(pve_cmd_builder.pvesm_status_cmd(storage), timeout=15.0)
                if not pve_cmd_builder.storage_present(r, storage):
                    raise pve_cmd_builder.PveCmdError(
                        f"backup: storage '{storage}' not found on this host")
                mode = pve_cmd_builder.normalize_backup_mode(
                    (backup_opts or {}).get("mode") or "snapshot")
                try:
                    keep = int((backup_opts or {}).get("keep") or 0)
                except (TypeError, ValueError):
                    keep = 0
                await _send(pve_cmd_builder.vzdump_cmd(vid, storage, mode, keep),
                            timeout=15.0)
                return {"ok": True, "vmid": vid, "action": "backup",
                        "storage": storage, "mode": mode, "keep": keep,
                        "kind": kind, "started": True}
            raise pve_cmd_builder.PveCmdError(f"unknown vm action: {action}")
        except pve_cmd_builder.PveCmdError as e:
            return {"vmid": vmid, "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - one VM must not sink the bulk
            logger.warning("pxmx VM_ACTION agent %s vmid=%s: %s", agent_id, vmid, e)
            return {"vmid": vmid, "ok": False, "error": str(e)}

    async def _vm_action_via_agent(self, agent_id: str,
                                   data: Dict[str, Any]) -> Dict[str, Any]:
        """Single PXMX_VM_ACTION via RUN_COMMAND. Wraps :meth:`_vm_action_one`
        and maps the row to the SUCCESS/ERROR envelope the typed handler sent."""
        row = await self._vm_action_one(agent_id, data, data.get("action"))
        if row.get("ok"):
            return {"status": "SUCCESS", **{k: v for k, v in row.items() if k != "ok"}}
        return {"status": "ERROR", "message": row.get("error", "vm action failed")}

    async def _route_vm_bulk(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Group a bulk VM action by owning agent (unique_id → cluster → agent)
        and run each item via RUN_COMMAND CONCURRENTLY per agent (6-semaphore,
        mirroring the Agent's ``_bulk_one``; send_to_agent multiplexes in-flight
        requests per agent). Merges the per-item rows; one VM's failure never
        sinks the rest."""
        items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        action = data.get("action")
        agents = (self.control_plane.connected_agents if self.control_plane else None) or {}
        if not agents:
            return {"status": "ERROR", "message": "No agents connected"}
        cluster_to_agent: Dict[str, str] = {}
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

        # Per-agent groups run in PARALLEL (asyncio.gather across agents — one
        # slow node no longer serializes the fleet's bulk action); within each
        # agent, items run CONCURRENTLY under that agent's own 6-semaphore
        # (mirroring the Agent's _bulk_one; send_to_agent multiplexes in-flight
        # requests per agent). _vm_action_one returns an error row instead of
        # raising, so one VM's failure never sinks the rest.
        async def _bulk_agent(aid, grp) -> list:
            sem = asyncio.Semaphore(6)

            async def _run_one(it):
                async with sem:
                    return await self._vm_action_one(aid, it, action)

            return list(await asyncio.gather(*[_run_one(it) for it in grp]))

        merged: list = []
        for rows in await asyncio.gather(
                *[_bulk_agent(aid, grp) for aid, grp in groups.items()]):
            merged.extend(rows)
        return {"status": "SUCCESS", "results": merged}

    async def _next_free_vmid_via_agent(self, _send) -> int:
        """Next free VMID via RUN_COMMAND: atomic ``/cluster/nextid`` first; on
        any failure fall back to ``max(qm list ∪ pct list)+1`` (two round-trips).
        Mirrors the Agent's ``next_free_vmid``."""
        vid = pve_cmd_builder.parse_next_free_vmid(
            await _send(pve_cmd_builder.next_free_vmid_cmd(), timeout=15.0))
        if vid is not None:
            return vid
        used: list = []
        for c in (pve_cmd_builder.qm_list_cmd(), pve_cmd_builder.pct_list_cmd()):
            try:
                used.extend(pve_cmd_builder.parse_vmids_from_list(
                    await _send(c, timeout=20.0)))
            except Exception as e:  # noqa: BLE001 - best-effort fallback source
                logger.debug("pxmx nextid fallback %s: %s", c, e)
        return pve_cmd_builder.next_free_vmid_fallback(used)

    async def _clone_vm_via_agent(self, agent_id: str,
                                  data: Dict[str, Any]) -> Dict[str, Any]:
        """PXMX_CLONE_VM via RUN_COMMAND. Resolves template vmid/node/kind (from
        ``template_unique_id`` ``<cluster>/<node>/<vmid>`` or explicit fields),
        auto-assigns a free VMID (atomic /cluster/nextid), clones, inherits the
        template's tags + appends the tenant labels (best-effort), and stamps the
        new VM's tags. Mirrors the Agent's PXMX_CLONE_VM handler."""
        cluster = ((self.control_plane.connected_agents or {}).get(agent_id, {})
                   .get("cluster_name", agent_id)) if self.control_plane else agent_id

        async def _send(cmd: str, timeout: float = 600.0):
            return await self.control_plane.send_to_agent(
                "RUN_COMMAND",
                {"command": cmd, "allow_shell": True, "timeout": timeout},
                agent_id=agent_id, timeout=timeout + 5.0)

        try:
            tuid = data.get("template_unique_id") or data.get("unique_id") or ""
            if tuid and "/" in tuid:
                parts = tuid.split("/")
                node = parts[-2] if len(parts) >= 3 else ""
                template_vmid = parts[-1]
            else:
                node = data.get("node") or ""
                template_vmid = data.get("template_vmid")
            if template_vmid is None:
                raise pve_cmd_builder.PveCmdError(
                    "template_vmid or template_unique_id required")
            name = (data.get("name") or "").strip()
            if not name:
                raise pve_cmd_builder.PveCmdError("name is required")
            kind = (data.get("type") or "").lower()
            if kind not in ("qemu", "lxc"):
                kind = pve_cmd_builder.kind_from_probe(
                    await _send(pve_cmd_builder.detect_kind_cmd(template_vmid),
                                timeout=10.0))
            new_vmid = data.get("new_vmid")
            if new_vmid is None:
                new_vmid = await self._next_free_vmid_via_agent(_send)
            pool = (data.get("pool") or "").strip() or None
            ttags_in = data.get("tenant_tags") or (
                [data.get("tenant_tag")] if data.get("tenant_tag") else [])
            tenant_tags = [str(t).strip() for t in ttags_in if str(t).strip()]

            # Clone the template → new VMID (full clone so the new VM has its own
            # disk). Can take minutes for a large template → 600s window.
            r = await _send(pve_cmd_builder.clone_cmd(
                template_vmid, new_vmid, name, kind, pool=pool), timeout=600.0)
            if not pve_cmd_builder.runner_ok(r):
                raise pve_cmd_builder.PveCmdError(pve_cmd_builder.runner_err(r))

            # Inherit the template's tags + append the tenant labels (dedup,
            # case-insensitive). Best-effort: a tag failure doesn't undo the clone.
            tags: list = []
            try:
                cfg = await _send(pve_cmd_builder.vm_config_cmd(
                    node, template_vmid, kind), timeout=15.0)
                tags = pve_cmd_builder.parse_config_tags(cfg)
            except Exception as e:  # noqa: BLE001
                logger.debug("clone: template tags %s/%s: %s", node, template_vmid, e)
            lower_tags = {t.lower() for t in tags}
            for tt in tenant_tags:
                if tt.lower() not in lower_tags:
                    tags.append(tt)
                    lower_tags.add(tt.lower())
            if tags:
                try:
                    r2 = await _send(pve_cmd_builder.set_tags_cmd(
                        new_vmid, kind, tags), timeout=30.0)
                    if not pve_cmd_builder.runner_ok(r2):
                        logger.warning("clone: tag set failed for new VM %s: %s",
                                       new_vmid, pve_cmd_builder.runner_err(r2))
                except Exception as e:  # noqa: BLE001
                    logger.warning("clone: tag set failed for new VM %s: %s", new_vmid, e)

            return {"status": "SUCCESS",
                    "unique_id": f"{cluster}/{node}/{new_vmid}",
                    "cluster": cluster, "node": node, "vmid": int(new_vmid),
                    "name": name, "type": kind, "pool": pool or "",
                    "template_vmid": int(template_vmid), "tags": tags}
        except pve_cmd_builder.PveCmdError as e:
            return {"status": "ERROR", "message": str(e)}
        except Exception as e:  # noqa: BLE001
            logger.exception("pxmx PXMX_CLONE_VM agent %s failed", agent_id)
            return {"status": "ERROR", "message": str(e)}

    async def _create_vm_via_agent(self, agent_id: str,
                                   data: Dict[str, Any]) -> Dict[str, Any]:
        """PXMX_CREATE_VM via RUN_COMMAND. Creates a stopped qemu VM from an ISO
        via ``pvesh create /nodes/<node>/qemu`` (cluster-wide), auto-assigns a
        free VMID (atomic /cluster/nextid), and tags it with the acting tenant's
        labels. Mirrors the Agent's PXMX_CREATE_VM handler."""
        cluster = ((self.control_plane.connected_agents or {}).get(agent_id, {})
                   .get("cluster_name", agent_id)) if self.control_plane else agent_id

        async def _send(cmd: str, timeout: float = 120.0):
            return await self.control_plane.send_to_agent(
                "RUN_COMMAND",
                {"command": cmd, "allow_shell": True, "timeout": timeout},
                agent_id=agent_id, timeout=timeout + 5.0)

        try:
            node = (data.get("node") or "").strip()
            if not node:
                raise pve_cmd_builder.PveCmdError("node is required")
            name = (data.get("name") or "").strip()
            if not name:
                raise pve_cmd_builder.PveCmdError("name is required")
            volid = (data.get("volid") or "").strip()
            if not volid:
                raise pve_cmd_builder.PveCmdError("volid (ISO) is required")
            new_vmid = data.get("new_vmid")
            if new_vmid is None:
                new_vmid = await self._next_free_vmid_via_agent(_send)
            ttags_in = data.get("tenant_tags") or (
                [data.get("tenant_tag")] if data.get("tenant_tag") else [])
            tenant_tags = [str(t).strip() for t in ttags_in if str(t).strip()]
            tags_joined = ";".join(tenant_tags)
            pool = (data.get("pool") or "").strip() or None
            memory_mb = int(data.get("memory_mb") or 2048)
            cores = int(data.get("cores") or 2)
            disk_storage = (data.get("disk_storage") or "").strip() or "local-lvm"
            disk_gb = int(data.get("disk_gb") or 32)
            bridge = (data.get("bridge") or "vmbr0").strip() or "vmbr0"

            args = ["--vmid", str(new_vmid), "--name", name, "--cdrom", volid,
                    "--memory", str(memory_mb), "--cores", str(cores),
                    "--scsi0", f"{disk_storage}:{disk_gb}",
                    "--net0", f"virtio,bridge={bridge}", "--ostype", "l26"]
            if pool:
                args += ["--pool", pool]
            if tags_joined:
                args += ["--tags", tags_joined]
            r = await _send(pve_cmd_builder.pvesh_create_cmd(
                f"/nodes/{node}/qemu", args), timeout=120.0)
            if not pve_cmd_builder.runner_ok(r):
                raise pve_cmd_builder.PveCmdError(pve_cmd_builder.runner_err(r))

            return {"status": "SUCCESS",
                    "unique_id": f"{cluster}/{node}/{new_vmid}",
                    "cluster": cluster, "node": node, "vmid": int(new_vmid),
                    "name": name, "type": "qemu", "pool": pool or "",
                    "tags": tenant_tags}
        except pve_cmd_builder.PveCmdError as e:
            return {"status": "ERROR", "message": str(e)}
        except Exception as e:  # noqa: BLE001
            logger.exception("pxmx PXMX_CREATE_VM agent %s failed", agent_id)
            return {"status": "ERROR", "message": str(e)}

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
