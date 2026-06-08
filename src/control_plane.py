import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import hashlib
import argparse
import sys
import os
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, HTTPException
import uvicorn

from .proxmox_spoke import ProxmoxSpoke

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PxmxControlPlane")

class PxmxControlPlane:
    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        self.spoke_id = spoke_id
        self.secret = secret
        self.hub_secret = hub_secret
        self.hub_url = hub_url
        self.agent_connection = None
        self.agent_secret = "pxmx-agent-secret" # Default agent secret
        self.response_cache = {}
        self.modules: Dict[str, Any] = {}

    def register_module(self, name: str, module_instance: Any):
        self.modules[name] = module_instance
        logger.info(f"Registered module: {name}")

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting PXMX Module in HUB MODE -> {self.hub_url}")

        # Start the Agent Server in the background
        asyncio.create_task(self.run_agent_server())

        # Create and register the Proxmox module
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
                            logger.error("Hub identity verification failed.")
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
                corr_id = msg.get("header", {}).get("message_id")

                # Multi-module routing
                result = None
                for module_name, module in self.modules.items():
                    if cmd_type.startswith(module_name) or True: # Simplify: let module try
                        result = await module.handle_command(cmd_type, data)
                        if result is not None: break

                if result is None and self.modules:
                    result = await list(self.modules.values())[0].handle_command(cmd_type, data)

                resp = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.spoke_id, "destination_id": "hub",
                               "correlation_id": corr_id},
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
        try:
            auth_json = await websocket.recv()
            auth_data = json.loads(auth_json)
            agent_id = auth_data.get("agent_id")
            secret = auth_data.get("secret")

            if not agent_id or secret != self.agent_secret:
                await websocket.close(1008, "Authentication failed")
                return

            logger.info(f"Proxmox Agent {agent_id} connected.")
            self.agent_connection = websocket

            async for message in websocket:
                msg_data = json.loads(message)
                if "signature" in msg_data and not self._verify_agent_signature(msg_data):
                    continue

                payload = msg_data.get("payload", {})
                msg_type = payload.get("type")

                if msg_type == "AGENT_TELEMETRY":
                    logger.info(f"Received telemetry from agent {agent_id}")
                elif msg_type == "AGENT_RESPONSE":
                    corr_id = msg_data.get("header", {}).get("correlation_id")
                    if corr_id:
                        self.response_cache[corr_id] = msg_data.get("payload", {}).get("data")

        except Exception as e:
            logger.error(f"Agent connection error: {e}")
        finally:
            self.agent_connection = None

    async def send_to_agent(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
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

        start_time = time.time()
        while time.time() - start_time < 5.0:
            if corr_id in self.response_cache:
                return self.response_cache.pop(corr_id)
            await asyncio.sleep(0.1)

        return {"status": "ERROR", "message": "Timed out waiting for agent response"}

    def _sign_agent(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        return hmac.new(self.agent_secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _verify_agent_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        expected = hmac.new(self.agent_secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def run_standalone_mode(self):
        logger.info(f"Starting PXMX Module in STANDALONE MODE on port 8000")
        app = FastAPI()
        @app.get("/status")
        async def get_status():
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
    parser.add_argument("--hub-secret", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", help="Hub WebSocket URL (defaults to standalone mode if omitted)")
    args = parser.parse_args()

    cp = PxmxControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    if args.hub:
        asyncio.run(cp.run_hub_mode())
    else:
        cp.run_standalone_mode()
