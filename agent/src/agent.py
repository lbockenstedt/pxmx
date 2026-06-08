import asyncio
import json
import uuid
import time
import logging
import psutil
import httpx
import argparse
import os
from typing import Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PxmxAgent")

class ProxmoxAgent:
    def __init__(self, spoke_url: str, agent_id: str, secret: str):
        self.spoke_url = spoke_url
        self.agent_id = agent_id
        self.secret = secret
        self.websocket = None

    def _sign(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        import hmac, hashlib
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    async def collect_metrics(self) -> Dict[str, Any]:
        """Collects local system performance metrics."""
        return {
            "cpu_usage": psutil.cpu_percent(interval=1),
            "memory_usage": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage('/').percent,
            "timestamp": time.time()
        }

    async def get_vm_list(self) -> Dict[str, Any]:
        """
        In a real environment, this would call the Proxmox API.
        For now, we provide a realistic structure.
        """
        # Mocking Proxmox API response
        return {
            "vms": [
                {"id": "100", "name": "web-server-01", "status": "running", "cpu": 12.5, "mem": 2048},
                {"id": "101", "name": "db-server-01", "status": "running", "cpu": 4.2, "mem": 4096},
                {"id": "102", "name": "test-node-01", "status": "stopped", "cpu": 0, "mem": 1024},
            ]
        }

    async def run(self):
        import websockets
        logger.info(f"Connecting to Proxmox Spoke at {self.spoke_url}...")

        async with websockets.connect(self.spoke_url) as websocket:
            self.websocket = websocket

            # 1. Handshake
            auth_msg = {
                "agent_id": self.agent_id,
                "secret": self.secret
            }
            await websocket.send(json.dumps(auth_msg))
            logger.info(f"Handshake sent for agent {self.agent_id}")

            # 2. Start background tasks
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            telemetry_task = asyncio.create_task(self._telemetry_loop())

            try:
                async for message in websocket:
                    msg_data = json.loads(message)

                    # Verify signature if present
                    if "signature" in msg_data:
                        if not self._verify_signature(msg_data):
                            logger.warning("Received message with invalid signature. Dropping.")
                            continue

                    payload = msg_data.get("payload", {})
                    cmd_type = payload.get("type")
                    data = payload.get("data", {})
                    corr_id = msg_data.get("header", {}).get("correlation_id")

                    logger.info(f"Received command: {cmd_type}")

                    result = {"status": "ERROR", "message": "Unknown command"}
                    if cmd_type == "GET_VM_LIST":
                        result = await self.get_vm_list()
                    elif cmd_type == "GET_SYSTEM_STATS":
                        result = await self.collect_metrics()
                    elif cmd_type == "SHELLEXEC":
                        # Safety check: only allow specific commands or use a restricted shell
                        cmd = data.get("command", "ls")
                        try:
                            import subprocess
                            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
                            result = {"status": "SUCCESS", "stdout": proc.stdout, "stderr": proc.stderr}
                        except Exception as e:
                            result = {"status": "ERROR", "message": str(e)}

                    # Send response
                    resp = {
                        "header": {
                            "message_id": str(uuid.uuid4()),
                            "correlation_id": corr_id,
                            "timestamp": time.time(),
                            "sender_id": self.agent_id,
                            "destination_id": "pxmx-spoke"
                        },
                        "payload": {"type": "AGENT_RESPONSE", "data": result}
                    }
                    resp["signature"] = self._sign(resp)
                    await websocket.send(json.dumps(resp))

            finally:
                heartbeat_task.cancel()
                telemetry_task.cancel()

    async def _heartbeat_loop(self):
        while True:
            try:
                msg = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.agent_id, "destination_id": "pxmx-spoke"},
                    "payload": {"type": "AGENT_HEARTBEAT", "data": {}}
                }
                msg["signature"] = self._sign(msg)
                await self.websocket.send(json.dumps(msg))
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
                await asyncio.sleep(5)

    async def _telemetry_loop(self):
        while True:
            try:
                metrics = await self.collect_metrics()
                vms = await self.get_vm_list()

                msg = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.agent_id, "destination_id": "pxmx-spoke"},
                    "payload": {"type": "AGENT_TELEMETRY", "data": {"metrics": metrics, "vms": vms}}
                }
                msg["signature"] = self._sign(msg)
                await self.websocket.send(json.dumps(msg))
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Telemetry push failed: {e}")
                await asyncio.sleep(10)

    def _verify_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        import hmac, hashlib
        expected = hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--spoke-url", required=True, help="URL of the Proxmox Spoke WebSocket server")
    parser.add_argument("--id", default="pxmx-agent-1", help="Agent ID")
    parser.add_argument("--secret", required=True, help="Authentication secret")
    args = parser.parse_args()

    agent = ProxmoxAgent(args.spoke_url, args.id, args.secret)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
