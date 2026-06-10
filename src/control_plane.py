import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import hashlib
import argparse
import os
from typing import Any, Dict, Optional
from core.src.messaging.control_plane import BaseControlPlane
from core.src.security.signer import MessageSigner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PxmxControlPlane")

class PxmxControlPlane(BaseControlPlane):
    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.agent_ws: Optional[websockets.WebSocketServerProtocol] = None

        # Load local configuration for agent management
        self.config = {}
        config_path = "/etc/lm-agent/config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load agent config from {config_path}: {e}")

        # Use agent secret from config, fallback to a generated one if not present
        self.agent_secret = self.config.get("agent_secret", "pxmx-agent-default-secret")
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.agent_signer = MessageSigner(self.agent_secret)

    def register_module(self, name: str, module_instance: Any):
        self.modules[name] = module_instance
        logger.info(f"Registered module: {name}")

    async def run_agent_server(self):
        """Starts the WebSocket server that the Proxmox Local Agent connects to."""
        port = 8766
        async with websockets.serve(self._agent_handler, "0.0.0.0", port):
            logger.info(f"Proxmox Agent Server listening on port {port}")
            await asyncio.Future() # Keep server running

    async def _agent_handler(self, websocket, path=None):
        """Handles the connection from the Proxmox Local Agent."""
        logger.info("Local Proxmox Agent attempting to connect...")
        try:
            # 1. Agent Authentication
            auth_msg = await websocket.recv()
            auth_data = json.loads(auth_msg)

            agent_id = auth_data.get("agent_id")
            agent_secret = auth_data.get("secret")

            if agent_secret != self.agent_secret:
                logger.error(f"Agent {agent_id} authentication failed. Invalid secret.")
                await websocket.close(1008, "Authentication failed")
                return

            logger.info(f"Agent {agent_id} authenticated successfully.")
            self.agent_ws = websocket

            # 2. Agent Message Loop
            async for message in websocket:
                msg_data = json.loads(message)

                # Verify signature
                if "signature" in msg_data and not self.agent_signer.verify(msg_data):
                    logger.warning("Received agent message with invalid signature. Dropping.")
                    continue

                payload = msg_data.get("payload", {})
                msg_type = payload.get("type")
                data = payload.get("data", {})
                corr_id = msg_data.get("header", {}).get("correlation_id")

                if msg_type == "AGENT_HEARTBEAT":
                    # Heartbeat is just for connectivity
                    pass
                elif msg_type == "AGENT_TELEMETRY":
                    # Push telemetry to the pxmx module
                    if "pxmx" in self.modules:
                        module = self.modules["pxmx"]
                        if hasattr(module, "telemetry_cache"):
                            module.telemetry_cache = data
                            logger.debug(f"Updated Proxmox telemetry cache from agent {agent_id}")
                elif msg_type == "AGENT_RESPONSE":
                    # Resolve pending command future
                    if corr_id in self.pending_responses:
                        fut = self.pending_responses.pop(corr_id)
                        if not fut.done():
                            fut.set_result(data)

        except Exception as e:
            logger.error(f"Error handling Proxmox Agent connection: {e}", exc_info=True)
        finally:
            self.agent_ws = None
            logger.info("Proxmox Agent disconnected.")

    async def send_to_agent(self, cmd_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Sends a command to the connected agent and waits for a response."""
        if not self.agent_ws:
            return {"status": "ERROR", "message": "No local agent connected"}

        corr_id = str(uuid.uuid4())
        msg = {
            "header": {
                "message_id": corr_id,
                "timestamp": time.time(),
                "sender_id": self.spoke_id,
                "destination_id": "pxmx-agent"
            },
            "payload": {"type": cmd_type, "data": data}
        }
        msg["signature"] = self.agent_signer.sign(msg)

        try:
            # Create a future to wait for the response
            fut = asyncio.get_event_loop().create_future()
            self.pending_responses[corr_id] = fut

            await self.agent_ws.send(json.dumps(msg, separators=(',', ':')))

            # Wait for response with timeout
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": "Agent response timeout"}
        except Exception as e:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": f"Failed to send to agent: {str(e)}"}

    async def run(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting PXMX Module in HUB MODE -> {self.hub_url}")

        # Start the Agent Server in the background
        asyncio.create_task(self.run_agent_server())

        # Create and register the Proxmox module
        from proxmox_spoke import ProxmoxSpoke
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {"proxmox_host": "localhost"}, control_plane=self)
        self.register_module("pxmx", pxmx_spoke)

        await super().run()
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", required=True, help="Authentication secret")
    parser.add_argument("--hub-secret", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    args = parser.parse_args()

    cp = PxmxControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run())
