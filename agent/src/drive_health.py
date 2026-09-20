"""Drive health monitoring for Proxmox storage.

Provides SSD health monitoring via smartctl with cciss device interface and
standard fallback, extracting wear levels (0-100 scale) and reporting:
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
import subprocess
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

# Command constants
PXMX_DRIVE_HEALTH = "PXMX_DRIVE_HEALTH"
PXMX_INSTALL_SSACLI = "PXMX_INSTALL_SSACLI"


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

    try:
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
    Get list of SCSI/SATA/NVMe devices using lsscsi with /sys/block fallback.

    Returns:
        List of device info dicts with index, device path, model, serial, etc.
    """
    devices = []

    # 1. Try lsscsi -g first
    try:
        proc = subprocess.run(
            ["lsscsi", "-g"],
            capture_output=True,
            text=True,
            timeout=10
        )

        if proc.returncode == 0 and proc.stdout.strip():
            # Format: [h:b:t:l] type vendor model rev /dev/sgN /dev/sdX
            for line in proc.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    host_part = parts[0].replace("[", "").replace("]", "")
                    idx = int(host_part.split(":")[0]) if ":" in host_part and host_part.split(":")[0].isdigit() else len(devices)
                    blk_dev = ""
                    scsi_path = ""
                    for p in parts:
                        if p.startswith("/dev/sd") or p.startswith("/dev/nvme") or p.startswith("/dev/vd"):
                            blk_dev = p
                        elif p.startswith("/dev/sg"):
                            scsi_path = p
                    if not blk_dev and len(parts) >= 3 and parts[2].startswith("/dev/"):
                        blk_dev = parts[2]

                    vendor = parts[2] if len(parts) > 2 and not parts[2].startswith("/") else ""
                    model = parts[3] if len(parts) > 3 and not parts[3].startswith("/") else ""

                    device_info = {
                        "host": host_part,
                        "scsi_path": scsi_path,
                        "block_device": blk_dev,
                        "vendor": vendor,
                        "model": model,
                        "serial": "unknown",
                        "index": idx
                    }
                    if blk_dev:
                        devices.append(device_info)
    except Exception as e:
        logger.debug("lsscsi check failed: %s", e)

    # 2. Fallback to /sys/block if lsscsi returned nothing or failed
    if not devices and os.path.exists("/sys/block"):
        try:
            for dev in sorted(os.listdir("/sys/block")):
                if dev.startswith(("sd", "nvme", "vd")):
                    # Skip partition nodes (e.g. sda1, nvme0n1p1)
                    if re.search(r"\d+$", dev) and not dev.startswith("nvme"):
                        continue
                    if dev.startswith("nvme") and not re.search(r"nvme\d+n\d+$", dev):
                        continue
                    dev_path = f"/dev/{dev}"
                    model = ""
                    model_path = f"/sys/block/{dev}/device/model"
                    if os.path.exists(model_path):
                        try:
                            with open(model_path) as f:
                                model = f.read().strip()
                        except Exception:
                            pass
                    devices.append({
                        "host": "0",
                        "scsi_path": "",
                        "block_device": dev_path,
                        "vendor": "",
                        "model": model,
                        "serial": "unknown",
                        "index": len(devices)
                    })
        except Exception as e:
            logger.debug("/sys/block scan failed: %s", e)

    return devices


def parse_smartctl_wear(stdout: str) -> Optional[int]:
    """
    Parse wear level percentage (0-100 scale, higher is more worn)
    across Samsung, Intel/WD, NVMe, and SAS smartctl formats.
    """
    # 1. Key-value formats:
    m = re.search(r"Wear_Leveling_Count\s*[:=]\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"PercentageUsed\s*[:=]\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"PercentageAvailable\s*[:=]\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return 100 - int(m.group(1))

    # 2. NVMe text format: "Percentage Used: 5%"
    m = re.search(r"Percentage\s+Used\s*:\s*(\d+)%?", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 3. SAS endurance indicator: "Percentage used endurance indicator: 1%"
    m = re.search(r"Percentage\s+used\s+endurance\s+indicator\s*:\s*(\d+)%?", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 4. Available Spare: "Available Spare: 100%"
    m = re.search(r"Available\s+Spare\s*:\s*(\d+)%?", stdout, re.IGNORECASE)
    if m:
        return 100 - int(m.group(1))

    # 5. Standard smartctl attribute table format:
    # ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
    table_m = re.search(
        r"(?:Wear_Leveling_Count|Used_RBV_Wear_Leveling|Media_Wearout_Indicator)\s+"
        r"0x[0-9a-fA-F]+\s+(\d+)\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        stdout, re.IGNORECASE
    )
    if table_m:
        val = int(table_m.group(1))  # normalized value (e.g. 85 = 85% life left)
        raw = int(table_m.group(2))  # raw value (e.g. 15 = 15% wear or cycle count)
        if raw <= 100 and abs((100 - val) - raw) <= 2:
            return raw
        return 100 - val if val <= 100 else raw

    # 6. Generic pattern for any attribute with "Wear" in name
    generic_m = re.search(r"(?i)([A-Za-z_]*Wear[A-Za-z_]*)\s*[:=\s]+(\d+)", stdout)
    if generic_m:
        return int(generic_m.group(2))

    return None


async def get_smartctl_info(device_path: str) -> Dict[str, Any]:
    """
    Get SMART information for a device using smartctl with cciss fallback.

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
        # Try cciss interface first (for HPE/LSI controllers)
        proc = subprocess.run(
            [SMARTCTL_PATH, "-d", "cciss", "-a", device_path],
            capture_output=True,
            text=True,
            timeout=30
        )

        # If cciss mode fails, fall back to standard auto-probe (-a)
        if proc.returncode != 0:
            proc = subprocess.run(
                [SMARTCTL_PATH, "-a", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )

        result["raw_output"] = proc.stdout

        if proc.returncode == 0:
            result["success"] = True

            wear_level = parse_smartctl_wear(proc.stdout)

            if wear_level is not None:
                wear_level = max(0, min(100, wear_level))
                result["wear_leveling_count"] = wear_level

                if wear_level >= WEAR_CRITICAL_THRESHOLD:
                    result["health_status"] = "critical"
                elif wear_level >= WEAR_WARNING_THRESHOLD:
                    result["health_status"] = "warning"
                else:
                    result["health_status"] = "healthy"

            # Parse model
            model_match = re.search(
                r"(?:Device Model|Model Number|Product):\s*(.+)",
                proc.stdout,
                re.IGNORECASE
            )
            if model_match:
                result["model"] = model_match.group(1).strip()

            # Parse serial
            serial_match = re.search(
                r"Serial [Nn]umber:\s*(.+)",
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

    devices = await get_scsi_devices()

    if not devices:
        logger.warning("No storage devices found")
        return result

    result["summary"]["total_drives"] = len(devices)

    for device in devices:
        device_path = device.get("block_device", "")
        if not device_path:
            continue

        health_info = await get_smartctl_info(device_path)

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
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        history = load_history()

        snapshot = {
            "timestamp": time.time(),
            "drives": drive_health.get("drives", []),
            "summary": drive_health.get("summary", {})
        }

        if "snapshots" not in history:
            history["snapshots"] = []

        history["snapshots"].append(snapshot)

        # Keep only last 100 snapshots
        if len(history["snapshots"]) > 100:
            history["snapshots"] = history["snapshots"][-100:]

        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

        return True

    except Exception as e:
        logger.error("save_history failed: %s", e)
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
        logger.error("load_history failed: %s", e)

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
                "health_status": drive.get("health_status")
            })

    for idx, data_points in drive_data.items():
        if not data_points:
            continue

        wear_levels = [p["wear_level"] for p in data_points if p["wear_level"] is not None]

        current_wear = wear_levels[-1] if wear_levels else None
        first_wear = wear_levels[0] if wear_levels else None

        wear_change = None
        if current_wear is not None and first_wear is not None:
            wear_change = current_wear - first_wear

        trends["drives"][str(idx)] = {
            "data_points": len(data_points),
            "current_wear": current_wear,
            "wear_change": wear_change,
            "trend": "increasing" if wear_change and wear_change > 0 else (
                "decreasing" if wear_change and wear_change < 0 else "stable"
            ),
            "history": data_points
        }

    return trends


async def get_drive_health_for_ui() -> Dict[str, Any]:
    """
    Get drive health data formatted for UI display.

    Returns:
        Dict ready for UI consumption with all drive health info.
    """
    health = await get_drive_health()

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

    Args:
        agent_instance: Optional agent instance for reporting

    Returns:
        Dict with health check results.
    """
    health = await get_drive_health()
    save_history(health)
    return health
