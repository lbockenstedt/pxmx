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

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting PXMX Module in HUB MODE -> {self.hub_url}")

        # Create the PxmxSpoke instance for command handling logic
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {"proxmox_host": "localhost"})

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
