import asyncio
import logging
from typing import Any, Dict
from base_spoke import BaseSpoke

logger = logging.getLogger("ProxmoxSpoke")

class ProxmoxSpoke(BaseSpoke):
    """
    Proxmox integration spoke. Manages VMs and containers on a Proxmox cluster.
    Now acts as a bridge between the Lab Manager Hub and the Local Proxmox Agent.
    """
    def __init__(self, spoke_id: str, config: Dict[str, Any], control_plane=None):
        super().__init__(spoke_id, config)
        self.control_plane = control_plane
        self.telemetry_cache = {}

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if command_type == "UPDATE_CONFIG":
            logger.info(f"Updating Proxmox configuration: {data}")
            self.config = data
            if self.control_plane:
                # Forward config update to the local agent
                return await self.control_plane.send_to_agent("UPDATE_CONFIG", data)
            return {"status": "SUCCESS", "message": "Proxmox configuration updated (no agent connected)"}

        if not self.control_plane:
            return {"success": False, "error": "Control plane not initialized"}

        # Map Hub commands to Agent commands
        agent_commands = {
            "CREATE_VM": "AGENT_CREATE_VM",
            "DELETE_VM": "AGENT_DELETE_VM",
            "GET_VM_INFO": "AGENT_GET_VM_INFO",
            "GET_VM_LIST": "AGENT_GET_VM_LIST",
            "SHELLEXEC": "AGENT_SHELLEXEC"
        }

        target_cmd = agent_commands.get(command_type, command_type)

        logger.info(f"Bridging command {command_type} -> {target_cmd} to local agent")
        result = await self.control_plane.send_to_agent(target_cmd, data)

        return result

    async def get_status(self) -> Dict[str, Any]:
        """Reports status based on local agent telemetry."""
        if not self.telemetry_cache:
            return {"status": "AGENT_OFFLINE", "managed_nodes": 0}

        return {
            "status": "HEALTHY",
            "metrics": self.telemetry_cache,
            "managed_nodes": self.telemetry_cache.get("nodes", 1)
        }
