import asyncio
import json
import uuid
import time
import logging
import psutil
import httpx
import argparse
import os
from typing import Dict, Any, Optional
from .security_utils import MessageSigner

def get_log_path():
    primary = "/var/log/pxmx-agent.log"
    try:
        with open(primary, "a") as f:
            pass
        return primary
    except Exception:
        local_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(local_dir, exist_ok=True)
        return os.path.join(local_dir, "pxmx-agent.log")

log_file = get_log_path()

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("PxmxAgent")

class ProxmoxAgent:
    def __init__(self, spoke_url: str, agent_id: str):
        self.spoke_url = spoke_url
        self.agent_id = agent_id

        # Load secret from protected local config
        self.secret = self._load_secret()
        if not self.secret:
            raise RuntimeError("Agent secret not found in /etc/lm-agent/config.json")

        self.websocket = None
        self.config = {} # Stores API credentials: host, user, password/token
        self.signer = MessageSigner(self.secret)

    def _load_secret(self) -> Optional[str]:
        config_path = "/etc/lm-agent/config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)
                    return config.get("secret")
        except Exception as e:
            logger.error(f"Failed to load agent secret from {config_path}: {e}")
        return None

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
        Fetches the real VM list from the Proxmox API.
        """
        if not self.config.get("host") or not self.config.get("user"):
            logger.warning("Proxmox API credentials missing. Returning empty VM list.")
            return {"vms": [], "error": "API credentials missing"}

        host = self.config["host"]
        user = self.config["user"]
        pwd = self.config.get("password")

        try:
            async with httpx.AsyncClient(verify=False) as client:
                # 1. Authentication (Get Ticket)
                auth_url = f"https://{host}:8006/api2/json/access/ticket"
                auth_resp = await client.post(auth_url, data={"username": user, "password": pwd})
                if auth_resp.status_code != 200:
                    return {"vms": [], "error": f"Auth failed: {auth_resp.text}"}

                ticket = auth_resp.json().get("data", {}).get("ticket")
                csrf_token = auth_resp.json().get("data", {}).get("CSRFPreventionToken")

                headers = {
                    "Cookie": f"BakeID={ticket}",
                    "CSRFPreventionToken": csrf_token
                }

                # 2. Get Nodes
                nodes_url = f"https://{host}:8006/api2/json/nodes"
                nodes_resp = await client.get(nodes_url, headers=headers)
                nodes = nodes_resp.json().get("data", {}).get("nodes", [])

                all_vms = []
                for node in nodes:
                    # Get QEMU VMs
                    qemu_url = f"https://{host}:8006/api2/json/nodes/{node}/qemu"
                    qemu_resp = await client.get(qemu_url, headers=headers)
                    qemu_data = qemu_resp.json().get("data", {})
                    for vmid, vm in qemu_data.items():
                        all_vms.append({
                            "id": vmid,
                            "name": vm.get("name"),
                            "status": vm.get("status"),
                            "cpu": vm.get("cpu", 0),
                            "mem": vm.get("maxmem", 0)
                        })

                    # Get LXC Containers
                    lxc_url = f"https://{host}:8006/api2/json/nodes/{node}/lxc"
                    lxc_resp = await client.get(lxc_url, headers=headers)
                    lxc_data = lxc_resp.json().get("data", {})
                    for vmid, vm in lxc_data.items():
                        all_vms.append({
                            "id": vmid,
                            "name": vm.get("name"),
                            "status": vm.get("status"),
                            "cpu": vm.get("cpu", 0),
                            "mem": vm.get("maxmem", 0)
                        })

                return {"vms": all_vms}
        except Exception as e:
            logger.error(f"Proxmox API error: {e}")
            return {"vms": [], "error": str(e)}

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
            logger.debug(f"Sending handshake: {auth_msg}")
            await websocket.send(json.dumps(auth_msg))
            logger.info(f"Handshake sent for agent {self.agent_id}")

            # 2. Start background tasks
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            telemetry_task = asyncio.create_task(self._telemetry_loop())

            try:
                async for message in websocket:
                    msg_data = json.loads(message)
                    logger.debug(f"Received message from spoke: {msg_data}")

                    # Verify signature if present
                    if "signature" in msg_data:
                        if not self.signer.verify(msg_data):
                            logger.warning("Received message with invalid signature. Dropping.")
                            continue

                    payload = msg_data.get("payload", {})
                    cmd_type = payload.get("type")
                    data = payload.get("data", {})
                    corr_id = msg_data.get("header", {}).get("correlation_id")

                    logger.info(f"Received command: {cmd_type}")

                    result = {"status": "ERROR", "message": "Unknown command"}
                    if cmd_type == "UPDATE_CONFIG":
                        logger.info(f"Updating Agent configuration: {data}")
                        self.config = data
                        result = {"status": "SUCCESS", "message": "Agent configuration updated"}
                    elif cmd_type == "GET_VM_LIST":
                        result = await self.get_vm_list()
                    elif cmd_type == "GET_SYSTEM_STATS":
                        result = await self.collect_metrics()
                    elif cmd_type == "SHELLEXEC":
                        # REMOVED: Generic shell execution is a critical security vulnerability (RCE)
                        result = {"status": "ERROR", "message": "SHELLEXEC command is disabled for security reasons"}

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
                    resp["signature"] = self.signer.sign(resp)
                    logger.debug(f"Sending response to spoke: {resp}")
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
                msg["signature"] = self.signer.sign(msg)
                logger.debug(f"Sending heartbeat: {msg}")
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
                msg["signature"] = self.signer.sign(msg)
                logger.debug(f"Sending telemetry: {msg}")
                await self.websocket.send(json.dumps(msg))
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Telemetry push failed: {e}")
                await asyncio.sleep(10)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--spoke-url", required=True, help="URL of the Proxmox Spoke WebSocket server")
    parser.add_argument("--id", default="pxmx-agent-1", help="Agent ID")
    args = parser.parse_args()


    agent = ProxmoxAgent(args.spoke_url, args.id)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"Critical failure during agent execution: {e}")
