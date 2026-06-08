import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import hashlib
import argparse
from typing import Dict, Any
from fastapi import FastAPI, HTTPException
import uvicorn

from .proxmox_spoke import ProxmoxSpoke

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PxmxControlPlane")

class PxmxControlPlane:
    def __init__(self, spoke_id: str, secret: str, hub_url: str = None):
        self.spoke_id = spoke_id
        self.secret = secret
        self.hub_url = hub_url
        self.agent_connection = None
        self.agent_secret = "pxmx-agent-secret" # Default agent secret

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting PXMX Module in HUB MODE -> {self.hub_url}")

        # Start the Agent Server in the background
        asyncio.create_task(self.run_agent_server())

        # Create the PxmxSpoke instance for command handling logic
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {"proxmox_host": "localhost"}, control_plane=self)

        async with websockets.connect(self.hub_url) as websocket:
            # Handshake
            await websocket.send(json.dumps({"spoke_id": self.spoke_id, "secret": self.secret}))
            logger.info(f"Connected to Lab Manager Hub as {self.spoke_id}")

            async def heartbeat():
                while True:
                    msg = {
                        "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                                   "sender_id": self.spoke_id, "destination_id": "hub"},
                        "payload": {"type": "HEARTBEAT", "data": {}}
                    }
                    msg["signature"] = self._sign(msg)
                    await websocket.send(json.dumps(msg))
                    await asyncio.sleep(30)

            asyncio.create_task(heartbeat())

            async for message in websocket:
                msg = json.loads(message)
                if not self._verify_signature(msg):
                    continue

                payload = msg.get("payload", {})
                cmd_type = payload.get("type")
                data = payload.get("data", {})

                result = await pxmx_spoke.handle_command(cmd_type, data)

                resp = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.spoke_id, "destination_id": "hub"},
                    "payload": {"type": "COMMAND_RESULT", "data": result}
                }
                resp["signature"] = self._sign(resp)
                await websocket.send(json.dumps(resp))

    async def run_agent_server(self):
        """WebSocket server for the local Proxmox Agent."""
        logger.info("Starting Proxmox Agent Server on port 8766...")
        async with websockets.serve(self.handle_agent_connection, "0.0.0.0", 8766):
            await asyncio.Future()

    async def handle_agent_connection(self, websocket):
        """Handles the lifecycle of the Local Proxmox Agent connection."""
        try:
            # 1. Authentication Handshake
            auth_json = await websocket.recv()
            auth_data = json.loads(auth_json)
            agent_id = auth_data.get("agent_id")
            secret = auth_data.get("secret")

            if not agent_id or secret != self.agent_secret:
                logger.warning(f"Agent authentication failed for {agent_id}")
                await websocket.close(1008, "Authentication failed")
                return

            logger.info(f"Proxmox Agent {agent_id} connected successfully.")
            self.agent_connection = websocket

            # 2. Message Loop
            async for message in websocket:
                msg_data = json.loads(message)

                # Signature Verification
                if "signature" in msg_data:
                    if not self._verify_agent_signature(msg_data):
                        logger.warning(f"Invalid agent signature from {agent_id}")
                        continue

                payload = msg_data.get("payload", {})
                msg_type = payload.get("type")

                if msg_type == "AGENT_HEARTBEAT":
                    logger.debug(f"Heartbeat from agent {agent_id}")
                elif msg_type == "AGENT_TELEMETRY":
                    # Update the spoke's cache with telemetry data
                    data = payload.get("data", {})
                    # The spoke instance is managed in run_hub_mode, so we might need
                    # a way to access the spoke's state from here.
                    # For now, we can use a shared state object or pass the spoke instance.
                    logger.info(f"Received telemetry from agent {agent_id}")
                elif msg_type == "AGENT_RESPONSE":
                    # This is a response to a command we sent.
                    # We should store it in a response cache mapped by correlation_id.
                    corr_id = msg_data.get("header", {}).get("correlation_id")
                    if corr_id:
                        self.response_cache[corr_id] = msg_data.get("payload", {}).get("data")

        except websockets.ConnectionClosed:
            logger.info("Proxmox Agent connection closed.")
        except Exception as e:
            logger.error(f"Error handling agent connection: {e}")
        finally:
            self.agent_connection = None

    async def send_to_agent(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Sends a command to the connected agent and waits for a response."""
        if not self.agent_connection:
            return {"status": "ERROR", "message": "Local agent not connected"}

        corr_id = str(uuid.uuid4())
        msg = {
            "header": {
                "message_id": corr_id,
                "correlation_id": corr_id,
                "timestamp": time.time(),
                "sender_id": "pxmx-spoke",
                "destination_id": "pxmx-agent"
            },
            "payload": {"type": command_type, "data": data}
        }
        msg["signature"] = self._sign_agent(msg)

        await self.agent_connection.send(json.dumps(msg))

        # Wait for response in the response cache (simplified poll)
        start_time = time.time()
        while time.time() - start_time < 5.0:
            if corr_id in getattr(self, "response_cache", {}):
                return self.response_cache.pop(corr_id)
            await asyncio.sleep(0.1)

        return {"status": "ERROR", "message": "Timed out waiting for agent response"}

    def _sign_agent(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        import hmac, hashlib
        return hmac.new(self.agent_secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _verify_agent_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        import hmac, hashlib
        expected = hmac.new(self.agent_secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def run_standalone_mode(self):
        """Standalone FastAPI server for local management."""
        logger.info(f"Starting PXMX Module in STANDALONE MODE on port 8000")
        app = FastAPI()

        @app.get("/status")
        async def get_status():
            # Create a temporary spoke to get status
            spoke = ProxmoxSpoke("temp", {})
            return await spoke.get_status()

        uvicorn.run(app, host="0.0.0.0", port=8000)

    def _sign(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _verify_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        expected = hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", required=True, help="Authentication secret")
    parser.add_argument("--hub", help="Hub WebSocket URL (defaults to standalone mode if omitted)")
    args = parser.parse_args()

    cp = PxmxControlPlane(args.id, args.secret, args.hub)
    if args.hub:
        asyncio.run(cp.run_hub_mode())
    else:
        cp.run_standalone_mode()
