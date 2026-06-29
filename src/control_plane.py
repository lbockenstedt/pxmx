"""pxmx spoke control plane — ``PxmxControlPlane``.

The Hub-side of the pxmx spoke: accepts pxmx host agents on port **:8766**
(``run_agent_server``), runs the spoke self-update check from GitHub
(``perform_self_update_check``), and routes signed messages between the LM Hub
and the connected agents. Overrides ``get_service_name`` → ``"lm-pxmx"`` and
guarantees :8766 is released before a new instance starts (the v2.0.3
agent-blackout fix). Audience: pxmx developers; see the repo ``ARCHITECTURE.md``.
"""

import asyncio
import json
import uuid
import time
import pathlib
import websockets
import logging
import hmac
import argparse
import os
from typing import Any, Dict, List, Optional
try:
    from core.src.messaging.control_plane import BaseControlPlane
    from core.src.security.signer import MessageSigner
except ImportError:
    from messaging.control_plane import BaseControlPlane
    from security.signer import MessageSigner

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("PxmxControlPlane")


class PxmxControlPlane(BaseControlPlane):
    """Hub-side control plane for pxmx agents (see module docstring)."""

    def get_service_name(self) -> str:
        return "lm-pxmx"

    async def handle_system_command(self, cmd_type: str, data: Dict[str, Any]) -> Any:
        """Handle a system command from the Hub; on log-level changes also broadcast to all agents."""
        result = await super().handle_system_command(cmd_type, data)
        if cmd_type in ("SET_LOG_LEVEL", "SPOKE_SET_LOG_LEVEL"):
            # Also propagate to all connected pxmx agents
            if self.connected_agents:
                await self.broadcast_to_agents("SET_LOG_LEVEL", data)
        return result

    def perform_self_update_check(self) -> bool:
        """Override to guarantee port 8766 is released before the new instance starts.

        lm core v0.27.98+ already calls os._exit(0) inside the base implementation and
        never returns, so this code is only reached on older lm core versions.
        """
        changed = super().perform_self_update_check()
        if changed:
            time.sleep(0.2)
            os._exit(0)
        return changed

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "hypervisor"

        config_path = "/etc/lm-agent/config.json"
        self.config: Dict[str, Any] = {}
        try:
            if os.path.exists(config_path):
                with open(config_path) as f:
                    self.config = json.load(f)
        except Exception as e:
            logger.error(f"Could not load agent config: {e}")

        self.agent_secret: Optional[str] = self.config.get("agent_secret")
        if not self.agent_secret:
            logger.warning("agent_secret not set — zero-touch provisioning only (agents will be approved before receiving a secret)")

        self.agent_signer = MessageSigner(self.agent_secret or "")
        self.pending_responses: Dict[str, asyncio.Future] = {}

        # agent_id → {ws, hostname, cluster_name, last_seen, nodes, vms, agent_metrics}
        self.connected_agents: Dict[str, Dict[str, Any]] = {}
        # agent_id → {ws, event} for agents awaiting admin approval
        self.pending_agents: Dict[str, Dict[str, Any]] = {}

        # Disk cache — survives service restarts; served as stale data until agents reconnect.
        # Stored next to this file's package root (e.g. /opt/lm/pxmx/agent_cache.json).
        self._disk_cache_path = str(pathlib.Path(__file__).resolve().parent.parent / "agent_cache.json")
        self.disk_cache: Dict[str, Any] = {}
        self._load_disk_cache()

    # ── Disk cache ────────────────────────────────────────────────────────────

    def _load_disk_cache(self):
        """Load persisted agent telemetry from disk on startup."""
        try:
            if os.path.exists(self._disk_cache_path):
                with open(self._disk_cache_path) as f:
                    data = json.load(f)
                self.disk_cache = data.get("agents", {})
                age_h = (time.time() - data.get("saved_at", 0)) / 3600
                logger.info(
                    f"Loaded agent disk cache: {len(self.disk_cache)} agent(s), {age_h:.1f}h old"
                )
        except Exception as e:
            logger.warning(f"Could not load agent disk cache: {e}")

    def _save_disk_cache(self):
        """Persist connected agent telemetry to disk (atomic write)."""
        try:
            payload = {
                "saved_at": time.time(),
                "agents": {
                    aid: {
                        "hostname":      info.get("hostname", aid),
                        "cluster_name":  info.get("cluster_name", aid),
                        "last_seen":     info.get("last_seen", 0),
                        "nodes":         info.get("nodes", []),
                        "vms":           info.get("vms", []),
                        "agent_metrics": info.get("agent_metrics", {}),
                    }
                    for aid, info in self.connected_agents.items()
                },
            }
            tmp = self._disk_cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._disk_cache_path)
            self.disk_cache = payload["agents"]
        except Exception as e:
            logger.warning(f"Could not write agent disk cache: {e}")

    # ── Agent WebSocket server ────────────────────────────────────────────────

    async def run_agent_server(self):
        """Serve the pxmx agent listener on :8766 (retries up to 10× on EADDRINUSE)."""
        port = 8766
        for attempt in range(10):
            try:
                async with websockets.serve(
                    self._agent_handler, "0.0.0.0", port,
                ):
                    logger.info(f"Agent listener on :{port}")
                    await asyncio.Future()
                return
            except OSError as e:
                # errno 98 = address in use (Linux), errno 48 = macOS equivalent
                if e.errno in (98, 48) and attempt < 9:
                    logger.warning(f"Port {port} in use, retrying in 3s (attempt {attempt + 1}/10)…")
                    await asyncio.sleep(3)
                else:
                    logger.error(f"Agent server failed to bind to port {port}: {e}")
                    raise
            except Exception as e:
                logger.error(f"Agent server unexpected error: {e}", exc_info=True)
                raise

    async def approve_pending_agent(self, agent_id: str):
        """Called when the LM hub approves a pending agent. Sends the provisioned secret."""
        pending = self.pending_agents.get(agent_id)
        if not pending:
            logger.warning(f"Approval for unknown/already-connected agent '{agent_id}'")
            return
        try:
            await pending["ws"].send(json.dumps({
                "status": "APPROVED",
                "secret": self.agent_secret,
            }))
            logger.info(f"Agent '{agent_id}' approved — secret provisioned")
            pending["event"].set()
        except Exception as e:
            logger.error(f"Failed to deliver approval to agent '{agent_id}': {e}")

    async def revoke_agent(self, agent_id: str):
        """Disconnect a connected or pending agent — it will auto-heal and re-enter pending."""
        agent = self.connected_agents.get(agent_id)
        if agent:
            try:
                await agent["ws"].close(1008, "Revoked by admin")
            except Exception:
                pass
            self.connected_agents.pop(agent_id, None)
            logger.info(f"Agent '{agent_id}' revoked (was connected)")
            return
        pending = self.pending_agents.get(agent_id)
        if pending:
            try:
                await pending["ws"].close(1008, "Revoked by admin")
            except Exception:
                pass
            pending["event"].set()
            self.pending_agents.pop(agent_id, None)
            logger.info(f"Agent '{agent_id}' revoked (was pending)")
            return
        logger.warning(f"Revoke requested for unknown agent '{agent_id}'")

    async def _agent_handler(self, websocket, path=None):
        agent_id = None
        try:
            # 1. Auth
            auth = json.loads(await websocket.recv())
            agent_id     = auth.get("agent_id")
            agent_secret = auth.get("secret")

            if not agent_id:
                await websocket.close(1008, "Missing agent_id"); return

            # ── Zero-touch / pending-approval path ───────────────────────────
            if not agent_secret:
                logger.info(f"Agent '{agent_id}' connected without credentials — pending approval")
                event = asyncio.Event()
                self.pending_agents[agent_id] = {"ws": websocket, "event": event}
                await websocket.send(json.dumps({"status": "APPROVAL_REQUIRED"}))
                try:
                    # Keep connection alive (heartbeats only) until approved or disconnected
                    while not event.is_set():
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                            msg = json.loads(raw)
                            # Only heartbeats are processed while pending
                        except asyncio.TimeoutError:
                            pass
                except Exception:
                    pass
                finally:
                    self.pending_agents.pop(agent_id, None)
                    if not event.is_set():
                        logger.info(f"Pending agent '{agent_id}' disconnected before approval")
                return

            # ── Authenticated path ────────────────────────────────────────────
            if not self.agent_secret or not hmac.compare_digest(str(agent_secret), str(self.agent_secret)):
                logger.warning(f"Agent '{agent_id}' auth failed — bad secret")
                await websocket.close(1008, "Auth failed"); return

            # 2. Mutual auth
            await websocket.send(json.dumps({"status": "HUB_VERIFIED"}))
            ack = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
            if ack.get("status") != "HUB_OK":
                await websocket.close(1008, "Agent failed mutual auth"); return

            logger.info(f"Agent '{agent_id}' connected")
            self.connected_agents[agent_id] = {
                "ws":           websocket,
                "hostname":     agent_id,
                "cluster_name": agent_id,   # overwritten by telemetry
                "last_seen":    time.time(),
                "nodes":        [],
                "vms":          [],
                "agent_metrics": {},
            }

            # Re-push stored PVE credentials if the spoke has them
            pxmx_mod = self.modules.get("pxmx")
            stored_cfg = pxmx_mod.agent_configs.get(agent_id) if pxmx_mod else None
            if stored_cfg:
                try:
                    await self.send_to_agent("UPDATE_CONFIG", stored_cfg, agent_id=agent_id)
                    logger.info(f"Re-pushed stored config to agent '{agent_id}'")
                except Exception as _e:
                    logger.warning(f"Failed to re-push config to agent '{agent_id}': {_e}")

            # 3. Message loop
            async for raw in websocket:
                msg = json.loads(raw)

                if "signature" not in msg or not self.agent_signer.verify(msg):
                    logger.warning("Invalid agent message signature — dropping")
                    continue

                payload  = msg.get("payload", {})
                msg_type = payload.get("type")
                data     = payload.get("data", {})
                corr_id  = msg.get("header", {}).get("correlation_id")

                if msg_type == "AGENT_HEARTBEAT":
                    if agent_id in self.connected_agents:
                        self.connected_agents[agent_id]["last_seen"] = time.time()
                    # Relay up so the hub's HeartbeatManager tracks per-agent
                    # liveness (keyed spoke_id:agent_id) and System → Diagnostics
                    # can render a GREEN/YELLOW/RED heartbeat for the agent like
                    # it does for spokes. Best-effort (see _relay_agent_msg_up).
                    await self._relay_agent_msg_up(agent_id, "AGENT_HEARTBEAT", data)

                elif msg_type == "AGENT_TELEMETRY":
                    if agent_id in self.connected_agents:
                        rec = self.connected_agents[agent_id]
                        rec["last_seen"]    = time.time()
                        rec["hostname"]     = data.get("hostname", agent_id)
                        rec["cluster_name"] = data.get("cluster_name", agent_id)
                        rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
                        rec["vms"]          = data.get("vms", {}).get("vms", [])
                        rec["agent_metrics"] = data.get("metrics", {})
                        self._save_disk_cache()
                    if "pxmx" in self.modules and hasattr(self.modules["pxmx"], "telemetry_cache"):
                        self.modules["pxmx"].telemetry_cache[agent_id] = data

                elif msg_type == "AGENT_RESPONSE":
                    if corr_id in self.pending_responses:
                        fut = self.pending_responses.pop(corr_id)
                        if not fut.done():
                            fut.set_result(data)

                elif msg_type == "AGENT_LOG":
                    # Relay to hub so it appears in Setup → Agent Logs.
                    await self._relay_agent_msg_up(agent_id, "AGENT_LOG", data)

                elif msg_type and msg_type.startswith("CS_"):
                    # Relay Client-Simulation events (CS_TELEMETRY / CS_LOG /
                    # CS_WATCHDOG_EVENT / CS_HW_RESET_EVENT / CS_PROGRESS /
                    # CS_COMMAND_RESULT / CS_TOKEN_RESULT) up to the hub, which
                    # dispatches them to the cs spoke via the AGENT_RELAY_UP CS_*
                    # dispatcher. The agent's send_cs_event already injected
                    # hostname + agent_id into ``data`` so the hub can resolve
                    # tenant/host.
                    await self._relay_agent_msg_up(agent_id, msg_type, data)

        except Exception as e:
            logger.error(f"Agent handler error: {e}", exc_info=True)
        finally:
            if agent_id:
                self.connected_agents.pop(agent_id, None)
                self.pending_agents.pop(agent_id, None)
            logger.info(f"Agent '{agent_id}' disconnected")

    async def _relay_agent_msg_up(self, agent_id: str, msg_type: str, data: Dict[str, Any]) -> None:
        """Wrap an agent message into an AGENT_RELAY_UP frame and forward it to
        the hub (best-effort). Shared by the AGENT_LOG and CS_* relay branches:
        the hub's AGENT_RELAY_UP handler logs AGENT_LOG and routes CS_* payloads
        to the cs spoke. Never raises — relay failures must not tear down the
        agent connection."""
        hub_ws = getattr(self, "_hub_ws", None)
        if not hub_ws:
            if msg_type == "AGENT_LOG":
                level = data.get("level", "INFO")
                msg_text = data.get("message", "")
                logger.warning("[agent:%s no-hub-relay] %s: %s", agent_id, level, msg_text)
            else:
                logger.debug("[agent:%s no-hub-relay] %s dropped", agent_id, msg_type)
            return
        if not self.signer:
            logger.warning(
                "Cannot relay %s from '%s': spoke has no session signer "
                "(hub connection not yet authenticated)", msg_type, agent_id)
            return
        try:
            relay = {
                "header": {
                    "message_id": str(uuid.uuid4()),
                    "timestamp": time.time(),
                    "sender_id": self.spoke_id,
                    "destination_id": "hub",
                },
                "payload": {
                    "type": "AGENT_RELAY_UP",
                    "data": {
                        "agent_id": agent_id,
                        "original_payload": {"payload": {"type": msg_type, "data": data}},
                    },
                },
            }
            relay["signature"] = self.signer.sign(relay)
            await hub_ws.send(json.dumps(relay, separators=(",", ":")))
        except Exception as _e:
            logger.warning("Failed to relay %s from '%s' to hub: %s", msg_type, agent_id, _e)

    # ── Agent command routing ─────────────────────────────────────────────────

    async def send_to_agent(self, cmd_type: str, data: Dict[str, Any],
                            agent_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Send a command to a specific agent (by agent_id) or the first available one.
        Returns the agent's response or an error dict.
        """
        if agent_id:
            rec = self.connected_agents.get(agent_id)
            if not rec:
                return {"status": "ERROR", "message": f"Agent '{agent_id}' not connected"}
            ws = rec["ws"]
        else:
            if not self.connected_agents:
                return {"status": "ERROR", "message": "No agents connected"}
            rec = next(iter(self.connected_agents.values()))
            ws = rec["ws"]

        corr_id = str(uuid.uuid4())
        msg = {
            "header": {
                "message_id": corr_id, "timestamp": time.time(),
                "sender_id": self.spoke_id, "destination_id": agent_id or "pxmx-agent",
            },
            "payload": {"type": cmd_type, "data": data},
        }
        msg["signature"] = self.agent_signer.sign(msg)

        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[corr_id] = fut
        try:
            await ws.send(json.dumps(msg, separators=(',', ':')))
            return await asyncio.wait_for(fut, timeout=15.0)
        except asyncio.TimeoutError:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": "Agent response timeout"}
        except Exception as e:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": str(e)}

    async def broadcast_to_agents(self, cmd_type: str,
                                  data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fan out a command to every connected agent; collect all results."""
        if not self.connected_agents:
            return []
        tasks = [
            self.send_to_agent(cmd_type, data, agent_id=aid)
            for aid in list(self.connected_agents)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = []
        for aid, res in zip(self.connected_agents, results):
            if isinstance(res, Exception):
                out.append({"agent_id": aid, "status": "ERROR", "message": str(res)})
            else:
                out.append({"agent_id": aid, **res})
        return out

    # ── Spoke startup ─────────────────────────────────────────────────────────

    async def run(self):
        """Main spoke entrypoint — start the Hub connection and the :8766 agent listener (self-healing).

        ``_run_agent_server_logged`` restarts the listener if its task ever dies,
        so :8766 is never left dark until a unit restart (the v2.0.3 blackout fix).
        """
        logger.info(f"Starting pxmx spoke → {self.hub_url}")

        async def _run_agent_server_logged():
            # Self-heal: if the agent listener ever exits (e.g. its serve task is
            # GC'd and raises "coroutine ignored GeneratorExit"), restart it after
            # a short backoff instead of leaving :8766 dark until a unit restart.
            while True:
                try:
                    await self.run_agent_server()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Agent server exited: {e} — restarting in 5s", exc_info=True)
                    await asyncio.sleep(5)

        # Keep a strong reference on the instance: if the only reference to a task
        # is asyncio's internal weak set, the loop may garbage-collect it mid-
        # flight — which raises "coroutine ignored GeneratorExit" and kills the
        # listener. Storing it prevents that GC; the restart loop above makes any
        # later exit self-recover instead of going dark for hours.
        self._agent_server_task = asyncio.create_task(_run_agent_server_logged())

        from proxmox_spoke import ProxmoxSpoke
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {}, control_plane=self)
        self.register_module("pxmx", pxmx_spoke)

        await super().run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id",         required=True)
    parser.add_argument("--secret",     default="")
    parser.add_argument("--hub-secret", nargs='?', default="", const="")
    parser.add_argument("--hub",        required=True)
    args = parser.parse_args()

    cp = PxmxControlPlane(args.id, args.secret or None, args.hub_secret, args.hub)
    asyncio.run(cp.run())
