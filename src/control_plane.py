import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import hashlib
import argparse
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PxmxControlPlane")

class PxmxControlPlane:
    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        self.spoke_id = spoke_id
        self.secret = secret
        self.hub_secret = hub_secret
        self.hub_url = hub_url
        self.modules: Dict[str, Any] = {}
        self.agent_ws: Optional[websockets.WebSocketServerProtocol] = None
        self.agent_secret = "pxmx-agent-secret" # Default secret for agent auth
        self.pending_responses: Dict[str, asyncio.Future] = {}

    def register_module(self, name: str, module_instance: Any):
        self.modules[name] = module_instance
        logger.info(f"Registered module: {name}")

    async def run_agent_server(self):
        """Starts the WebSocket server that the Proxmox Local Agent connects to."""
        port = 8766
        async with websockets.serve(self._agent_handler, "0.0.0.0", port):
            logger.info(f"Proxmox Agent Server listening on port {port}")
            await asyncio.Future() # Keep server running

    async def _agent_handler(self, websocket, path):
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
                if "signature" in msg_data and not self._verify_agent_signature(msg_data):
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
        msg["signature"] = self._sign_agent_msg(msg)

        try:
            # Create a future to wait for the response
            fut = asyncio.get_event_loop().create_future()
            self.pending_responses[corr_id] = fut

            await self.agent_ws.send(json.dumps(msg))

            # Wait for response with timeout
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": "Agent response timeout"}
        except Exception as e:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": f"Failed to send to agent: {str(e)}"}

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting PXMX Module in HUB MODE -> {self.hub_url}")

        # Start the Agent Server in the background
        asyncio.create_task(self.run_agent_server())

        # Create and register the Proxmox module
        from .proxmox_spoke import ProxmoxSpoke
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {"proxmox_host": "localhost"}, control_plane=self)
        self.register_module("pxmx", pxmx_spoke)

        async with websockets.connect(self.hub_url) as websocket:
            # 1. Spoke Authentication Handshake
            await websocket.send(json.dumps({"spoke_id": self.spoke_id, "secret": self.secret}))
            logger.info(f"Connected to Lab Manager Hub as {self.spoke_id}. Performing mutual authentication...")

            # 2. Hub Mutual Authentication
            try:
                hub_proof_json = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                hub_proof = json.loads(hub_proof_json)

                if hub_proof.get("status") == "HUB_VERIFIED":
                    challenge = hub_proof.get("challenge")
                    signature = hub_proof.get("signature")

                    if self.hub_secret:
                        expected_sig = hmac.new(
                            self.hub_secret.encode(),
                            challenge.encode(),
                            hashlib.sha256
                        ).hexdigest()

                        if hmac.compare_digest(expected_sig, signature):
                            logger.info("Hub identity verified successfully.")
                            await websocket.send(json.dumps({"status": "HUB_OK"}))
                        else:
                            logger.error(f"Hub identity verification failed. Expected: {expected_sig}, Got: {signature}")
                            await websocket.close(1008, "Hub verification failed")
                            return
                    else:
                        logger.warning("Hub secret not configured. Skipping verification.")
                        await websocket.send(json.dumps({"status": "HUB_OK"}))
                else:
                    await websocket.close(1008, "Mutual authentication failed")
                    return
            except Exception as e:
                logger.error(f"Hub verification failed: {e}")
                await websocket.close(1008, "Mutual authentication timed out")
                return

            async def heartbeat():
                while True:
                    try:
                        msg = {
                            "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                                       "sender_id": self.spoke_id, "destination_id": "hub"},
                            "payload": {"type": "HEARTBEAT", "data": {}}
                        }
                        msg["signature"] = self._sign(msg)
                        await websocket.send(json.dumps(msg))
                    except Exception as e:
                        logger.error(f"Heartbeat failed: {e}")
                    await asyncio.sleep(10)

            asyncio.create_task(heartbeat())

            async for message in websocket:
                try:
                    msg = json.loads(message)
                    if not self._verify_signature(msg):
                        continue

                    payload = msg.get("payload", {})
                    cmd_type = payload.get("type")
                    data = payload.get("data", {})
                    corr_id = msg.get("header", {}).get("message_id")

                    # Multi-module routing
                    result = None
                    for module_name, module in self.modules.items():
                        if cmd_type.startswith(module_name) or True: # Simplify: let module try
                            try:
                                result = await module.handle_command(cmd_type, data)
                                if result is not None: break
                            except Exception as e:
                                logger.error(f"Error in module {module_name} handling {cmd_type}: {e}", exc_info=True)
                                result = {"status": "ERROR", "message": f"Module {module_name} crashed: {str(e)}"}
                                break

                    if result is None and self.modules:
                        try:
                            result = await list(self.modules.values())[0].handle_command(cmd_type, data)
                        except Exception as e:
                            logger.error(f"Error in fallback module handling {cmd_type}: {e}", exc_info=True)
                            result = {"status": "ERROR", "message": f"Fallback module crashed: {str(e)}"}

                    resp = {
                        "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                                   "sender_id": self.spoke_id, "destination_id": "hub",
                                   "correlation_id": corr_id},
                        "payload": {"type": "COMMAND_RESULT", "data": result}
                    }
                    resp["signature"] = self._sign(resp)
                    await websocket.send(json.dumps(resp))
                except Exception as e:
                    logger.error(f"Critical error in PxmxControlPlane message loop: {e}", exc_info=True)

    def _sign(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _verify_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        expected = hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def _sign_agent_msg(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.agent_secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _verify_agent_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        expected = hmac.new(self.agent_secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", required=True, help="Authentication secret")
    parser.add_argument("--hub-secret", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    args = parser.parse_args()

    cp = PxmxControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run_hub_mode())
