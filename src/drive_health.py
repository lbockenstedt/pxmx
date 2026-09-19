"""Drive health monitoring for Proxmox storage.

Provides SSD health monitoring via smartctl with cciss device interface,
extracting Wear_Leveling_Count values (0-100 scale) and reporting:
- Physical drive index
- Wear level percentage (0-100%, lower is better)
- Model/serial info
- Health status (healthy, warning, critical)
- Historical wear trend data
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("DriveHealth")

# Path to HPE SSA CLI tools (if installed)
SSACLI_PATH = "/usr/sbin/ssacli"
SMARTCTL_PATH = "/usr/bin/smartctl"

# Wear level thresholds
WEAR_WARNING_THRESHOLD = 60  # Start warning at 60%
WEAR_CRITICAL_THRESHOLD = 80  # Critical at 80%+

# Historical data storage
HISTORY_FILE = "/var/lib/pxmx/drive_health_history.json"


class DriveHealthError(Exception):
    """Drive health monitoring error."""
    pass


def check_ssacli_installed() -> bool:
    """Check if HPE SSA CLI tools are installed."""
    return os.path.exists(SSACLI_PATH) and os.access(SSACLI_PATH, os.X_OK)


def check_smartctl_installed() -> bool:
    """Check if smartctl is installed."""
    return os.path.exists(SMARTCTL_PATH) and os.access(SMARTCTL_PATH, os.X_OK)


def install_ssacli_if_needed() -> Dict[str, Any]:
    """
    Check and install HPE SSA CLI tools if not present.

    Returns:
        Dict with installation status and any errors.
    """
    result = {
        "installed": False,
        "already_installed": False,
        "error": None,
        "output": ""
    }

    if check_ssacli_installed():
        result["already_installed"] = True
        result["installed"] = True
        return result

    # Try to install HPE SSA CLI (works on Proxmox/Debian)
    # This requires root privileges and network access
    try:
        # For Proxmox/Debian-based systems:
        # apt-get install smartmontools hpsa-cli or hpssacli
        import subprocess

        cmd = [
            "apt-get", "update", "-y",
            "&&", "apt-get", "install", "-y", "smartmontools", "hpssacli"
        ]

        proc = subprocess.run(
            " ".join(cmd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=120
        )

        if proc.returncode == 0:
            result["installed"] = True
            result["output"] = proc.stdout
        else:
            result["error"] = proc.stderr or f"Exit code: {proc.returncode}"

    except subprocess.TimeoutExpired:
        result["error"] = "Installation timed out after 120s"
    except Exception as e:
        result["error"] = f"Installation failed: {str(e)}"

    return result


async def get_scsi_devices() -> List[Dict[str, Any]]:
    """
    Get list of SCSI/SATA devices using lsscsi.

    Returns:
        List of device info dicts with index, device path, model, serial, etc.
    """
    devices = []

    try:
        import subprocess

        # Get SCSI device list
        proc = subprocess.run(
            ["lsscsi", "-g"],
            capture_output=True,
            text=True,
            timeout=10
        )

        if proc.returncode != 0:
            logger.warning(f"lsscsi failed: {proc.stderr}")
            return devices

        # Parse lsscsi output
        # Format: [h:h] scsi0:0:0:0  /dev/sg0  /dev/sda  Dell     SAFT2400SSD3   0050
        for line in proc.stdout.strip().splitlines():
            if not line.strip():
                continue

            # Parse: host:bus:target:lun  device_path  block_device  vendor  model  serial
            parts = line.split()
            if len(parts) >= 6:
                device_info = {
                    "host": parts[0],
                    "scsi_path": parts[1],
                    "block_device": parts[2],
                    "vendor": parts[3],
                    "model": parts[4],
                    "serial": parts[5] if len(parts) > 5 else "unknown",
                    "index": int(parts[0].split(":")[0].replace("[", "").replace("]", "")) if ":" in parts[0] else 0
                }
                devices.append(device_info)

    except Exception as e:
        logger.error(f"get_scsi_devices failed: {e}")

    return devices


async def get_smartctl_info(device_path: str) -> Dict[str, Any]:
    """
    Get SMART information for a device using smartctl with cciss interface.

    Args:
        device_path: Device path like /dev/sda

    Returns:
        Dict with SMART data including wear level if available.
    """
    result = {
        "device": device_path,
        "success": False,
        "error": None,
        "wear_leveling_count": None,
        "model": None,
        "serial": None,
        "health_status": "unknown",
        "raw_output": ""
    }

    if not check_smartctl_installed():
        result["error"] = "smartctl not installed"
        return result

    try:
        import subprocess

        # Try cciss interface first (for Proxmox/LSI RAID controllers)
        # Format: smartctl -d cciss,<index> -a /dev/sda
        # The index is the drive number from the cciss controller

        # First, get the controller info
        proc = subprocess.run(
            [SMARTCTL_PATH, "-d", "cciss", "-a", device_path],
            capture_output=True,
            text=True,
            timeout=30
        )

        result["raw_output"] = proc.stdout

        if proc.returncode == 0:
            result["success"] = True

              # Parse wear level - vendor-agnostic approach
              # Try multiple attribute types in order of preference:
              # 1. Samsung-style: Wear_Leveling_Count
              # 2. WD/Intel-style: PercentageUsed
              # 3. PercentageAvailable (convert: 100 - value)
              # 4. Generic pattern for any attribute with "Wear" in name
            wear_level = None

              # 1. Try Samsung-style Wear_Leveling_Count
            wear_match = re.search(
                r"Wear_Leveling_Count\s*=\s*(\d+)",
                proc.stdout,
                re.IGNORECASE
              )
            if wear_match:
                wear_level = int(wear_match.group(1))
                logger.debug(f"Found Samsung-style Wear_Leveling_Count: {wear_level}")

              # 2. Try WD/Intel-style PercentageUsed
            if wear_level is None:
                pct_used_match = re.search(
                    r"PercentageUsed\s*=\s*(\d+)",
                    proc.stdout,
                    re.IGNORECASE
                  )
                if pct_used_match:
                    wear_level = int(pct_used_match.group(1))
                    logger.debug(f"Found WD-style PercentageUsed: {wear_level}")

              # 3. Try PercentageAvailable (convert: 100 - value)
            if wear_level is None:
                pct_avail_match = re.search(
                    r"PercentageAvailable\s*=\s*(\d+)",
                    proc.stdout,
                    re.IGNORECASE
                  )
                if pct_avail_match:
                    wear_level = 100 - int(pct_avail_match.group(1))
                    logger.debug(f"Found PercentageAvailable: {100 - int(pct_avail_match.group(1))} (converted from {int(pct_avail_match.group(1))})")

              # 4. Generic pattern for any attribute with "Wear" in name
            if wear_level is None:
                generic_wear_match = re.search(
                    r"(?i)([A-Za-z]*Wear[A-Za-z]*)\s*=\s*(\d+)",
                    proc.stdout
                  )
                if generic_wear_match:
                    attr_name = generic_wear_match.group(1)
                    wear_level = int(generic_wear_match.group(2))
                    logger.debug(f"Found generic Wear attribute '{attr_name}': {wear_level}")

              # Only set health status if wear_level is not None
            if wear_level is not None:
                  # Clamp to valid range
                wear_level = max(0, min(100, wear_level))
                result["wear_leveling_count"] = wear_level

                if wear_level >= WEAR_CRITICAL_THRESHOLD:
                    result["health_status"] = "critical"
                elif wear_level >= WEAR_WARNING_THRESHOLD:
                    result["health_status"] = "warning"
                else:
                    result["health_status"] = "healthy"

            # Parse model and serial
            model_match = re.search(
                r"Product:\s*(.+)",
                proc.stdout,
                re.IGNORECASE
            )
            if model_match:
                result["model"] = model_match.group(1).strip()

            serial_match = re.search(
                r"Serial Number:\s*(.+)",
                proc.stdout,
                re.IGNORECASE
            )
            if serial_match:
                result["serial"] = serial_match.group(1).strip()

        else:
            result["error"] = proc.stderr or "smartctl failed"

    except subprocess.TimeoutExpired:
        result["error"] = "smartctl timed out after 30s"
    except Exception as e:
        result["error"] = f"smartctl error: {str(e)}"

    return result


async def get_drive_health() -> Dict[str, Any]:
    """
    Get comprehensive drive health information for all drives.

    Returns:
        Dict with drive health data including:
        - drives: List of drive health info
        - summary: Overall health summary
        - timestamp: When the check was performed
    """
    result = {
        "drives": [],
        "summary": {
            "total_drives": 0,
            "healthy_drives": 0,
            "warning_drives": 0,
            "critical_drives": 0,
            "unknown_drives": 0
        },
        "timestamp": time.time()
    }

    # Get SCSI device list
    devices = await get_scsi_devices()

    if not devices:
        logger.warning("No SCSI devices found")
        return result

    result["summary"]["total_drives"] = len(devices)

    # Query each device for SMART data
    for device in devices:
        device_path = device.get("block_device", "")
        if not device_path:
            continue

        health_info = await get_smartctl_info(device_path)

        # Combine device info with SMART data
        drive_info = {
            "physical_index": device.get("index", 0),
            "scsi_path": device.get("scsi_path", ""),
            "block_device": device_path,
            "vendor": device.get("vendor", ""),
            "model": health_info.get("model") or device.get("model", ""),
            "serial": health_info.get("serial") or device.get("serial", "unknown"),
            "wear_level": health_info.get("wear_leveling_count"),
            "health_status": health_info.get("health_status", "unknown"),
            "success": health_info.get("success", False),
            "error": health_info.get("error")
        }

        result["drives"].append(drive_info)

        # Update summary
        status = drive_info.get("health_status", "unknown")
        if status == "healthy":
            result["summary"]["healthy_drives"] += 1
        elif status == "warning":
            result["summary"]["warning_drives"] += 1
        elif status == "critical":
            result["summary"]["critical_drives"] += 1
        else:
            result["summary"]["unknown_drives"] += 1

    return result


def save_history(drive_health: Dict[str, Any]) -> bool:
    """
    Save drive health snapshot to history file.

    Args:
        drive_health: Current drive health data

    Returns:
        True if saved successfully, False otherwise.
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)

        # Load existing history
        history = load_history()

        # Add new snapshot
        snapshot = {
            "timestamp": time.time(),
            "drives": drive_health.get("drives", []),
            "summary": drive_health.get("summary", {})
        }

        if "snapshots" not in history:
            history["snapshots"] = []

        history["snapshots"].append(snapshot)

        # Keep only last 100 snapshots to prevent unbounded growth
        if len(history["snapshots"]) > 100:
            history["snapshots"] = history["snapshots"][-100:]

        # Save
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

        return True

    except Exception as e:
        logger.error(f"save_history failed: {e}")
        return False


def load_history() -> Dict[str, Any]:
    """
    Load drive health history from file.

    Returns:
        Dict with historical snapshots, or empty dict if file doesn't exist.
    """
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"load_history failed: {e}")

    return {"snapshots": []}


def get_historical_trends(drive_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Get historical wear trends for a specific drive or all drives.

    Args:
        drive_index: Optional physical drive index to filter by

    Returns:
        Dict with historical trend data per drive.
    """
    history = load_history()

    trends = {
        "drives": {},
        "summary": {
            "total_snapshots": len(history.get("snapshots", []))
        }
    }

    snapshots = history.get("snapshots", [])

    if not snapshots:
        return trends

    # Group by drive index
    drive_data: Dict[int, List[Dict]] = {}

    for snapshot in snapshots:
        for drive in snapshot.get("drives", []):
            idx = drive.get("physical_index")
            if idx is None:
                continue

            if drive_index is not None and idx != drive_index:
                continue

            if idx not in drive_data:
                drive_data[idx] = []

            drive_data[idx].append({
                "timestamp": snapshot.get("timestamp"),
                "wear_level": drive.get("wear_level"),
                "health_status": drive.get("health_status"),
                "model": drive.get("model"),
                "serial": drive.get("serial")
            })

    # Calculate trends for each drive
    for drive_idx, data_points in drive_data.items():
        if len(data_points) < 2:
            trends["drives"][drive_idx] = {
                "data_points": data_points,
                "trend": "insufficient_data",
                "wear_rate_per_hour": None
            }
            continue

        # Calculate wear rate
        first = data_points[0]
        last = data_points[-1]

        time_diff = last.get("timestamp", 0) - first.get("timestamp", 0)
        wear_diff = (last.get("wear_level", 0) or 0) - (first.get("wear_level", 0) or 0)

        wear_rate = None
        if time_diff > 0:
            wear_rate = wear_diff / (time_diff / 3600)  # wear per hour

        # Determine trend direction
        if wear_rate is not None:
            if wear_rate > 0.1:
                trend = "increasing_fast"
            elif wear_rate > 0:
                trend = "increasing"
            elif wear_rate < -0.1:
                trend = "decreasing"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        trends["drives"][drive_idx] = {
            "data_points": data_points,
            "trend": trend,
            "wear_rate_per_hour": wear_rate,
            "first_wear_level": first.get("wear_level"),
            "last_wear_level": last.get("wear_level"),
            "total_wear_change": wear_diff
        }

    return trends


async def get_drive_health_for_ui() -> Dict[str, Any]:
    """
    Get drive health data formatted for UI display.

    Returns:
        Dict ready for UI consumption with all drive health info.
    """
    health = await get_drive_health()

    # Add alerts for drives approaching end-of-life
    alerts = []
    for drive in health.get("drives", []):
        wear = drive.get("wear_level")
        if wear is not None:
            if wear >= WEAR_CRITICAL_THRESHOLD:
                alerts.append({
                    "drive_index": drive.get("physical_index"),
                    "alert_type": "critical",
                    "message": f"Drive {drive.get('physical_index')} is at {wear}% wear - critical",
                    "wear_level": wear
                })
            elif wear >= WEAR_WARNING_THRESHOLD:
                alerts.append({
                    "drive_index": drive.get("physical_index"),
                    "alert_type": "warning",
                    "message": f"Drive {drive.get('physical_index')} is at {wear}% wear - approaching end-of-life",
                    "wear_level": wear
                })

    # Get historical trends
    trends = get_historical_trends()

    return {
        "drives": health.get("drives", []),
        "summary": health.get("summary", {}),
        "alerts": alerts,
        "historical_trends": trends,
        "timestamp": time.time()
    }


async def run_periodic_drive_health_check(agent_instance=None) -> Dict[str, Any]:
    """
    Run a periodic drive health check and store the result.

    This function is designed to be called from the agent's telemetry loop.

    Args:
        agent_instance: Optional agent instance for logging/context

    Returns:
        Drive health check result.
    """
    logger.info("Running periodic drive health check")

    result = await get_drive_health_for_ui()

    # Save to history
    save_history(result)

    # Log summary
    summary = result.get("summary", {})
    logger.info(
        "Drive health: %d total, %d healthy, %d warning, %d critical",
        summary.get("total_drives", 0),
        summary.get("healthy_drives", 0),
        summary.get("warning_drives", 0),
        summary.get("critical_drives", 0)
    )

    # Log any alerts
    for alert in result.get("alerts", []):
        logger.warning(
            "Drive alert [%s]: %s",
            alert.get("alert_type"),
            alert.get("message")
        )

    return result
