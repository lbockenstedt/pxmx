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
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("DriveHealth")

# Path to HPE SSA CLI tools (if installed)
SSACLI_PATH = "/usr/sbin/ssacli"
SMARTCTL_PATH = "/usr/bin/smartctl"

def find_nvme_path() -> Optional[str]:
    """Locate nvme CLI executable across system paths."""
    found = shutil.which("nvme")
    if found and os.access(found, os.X_OK):
        return found
    for p in ("/usr/sbin/nvme", "/usr/bin/nvme", "/sbin/nvme", "/usr/local/sbin/nvme"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None

def check_nvme_installed() -> bool:
    """Check if nvme-cli is installed."""
    return find_nvme_path() is not None

def find_smartctl_path() -> Optional[str]:
    """Locate smartctl executable across system paths."""
    found = shutil.which("smartctl")
    if found and os.access(found, os.X_OK):
        return found
    for p in ("/usr/sbin/smartctl", "/usr/bin/smartctl", "/sbin/smartctl", "/usr/local/sbin/smartctl"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None

def find_ssacli_path() -> Optional[str]:
    """Locate ssacli or hpssacli executable across system paths."""
    for name in ("ssacli", "hpssacli"):
        found = shutil.which(name)
        if found and os.access(found, os.X_OK):
            return found
    for p in ("/usr/sbin/ssacli", "/usr/sbin/hpssacli", "/usr/bin/ssacli", "/opt/hp/hpssacli/bld/hpssacli"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None

def check_ssacli_installed() -> bool:
    """Check if HPE SSA CLI tools are installed."""
    return find_ssacli_path() is not None

def check_smartctl_installed() -> bool:
    """Check if smartctl is installed."""
    return find_smartctl_path() is not None

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



_HPE_VENDOR_STRINGS = frozenset({
    "hpe",
    "hp",
    "hewlett-packard",
    "hewlett packard enterprise",
    "proliant",
})
_HPE_PCI_VENDOR_IDS = frozenset({"103c", "1590"})
_STORAGE_CLASS_PREFIXES = ("0104", "0100")
_HPE_RAID_DRIVER_NAMES = ("smartpqi", "hpsa", "cciss")
_SSACLI_PACKAGE = "hpssacli"


def _read_sysfs_vendor() -> str:
    path = "/sys/class/dmi/id/sys_vendor"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _read_sysfs_product_name() -> str:
    path = "/sys/class/dmi/id/product_name"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _read_dmidecode_vendor() -> str:
    try:
        proc = subprocess.run(
            ["dmidecode", "-s", "system-manufacturer"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


def _matches_hpe_vendor(val: str) -> bool:
    if not val:
        return False
    val_lower = val.lower()
    return any(v in val_lower for v in _HPE_VENDOR_STRINGS)


def is_hpe_server() -> bool:
    if _matches_hpe_vendor(_read_sysfs_vendor()):
        return True
    if _matches_hpe_vendor(_read_sysfs_product_name()):
        return True
    if _matches_hpe_vendor(_read_dmidecode_vendor()):
        return True
    return False


def _has_active_raid_driver() -> bool:
    for driver in _HPE_RAID_DRIVER_NAMES:
        driver_path = f"/sys/bus/pci/drivers/{driver}"
        if os.path.isdir(driver_path):
            try:
                for entry in os.listdir(driver_path):
                    if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", entry):
                        return True
            except Exception:
                pass
    return False


def _has_raid_in_proc_scsi() -> bool:
    path = "/proc/scsi/scsi"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                content = f.read()
                if "Smart Array" in content or "SmartRAID" in content:
                    return True
        except Exception:
            pass
    return False


def _read_pci_attr(dev_path: str, attr: str) -> str:
    path = os.path.join(dev_path, attr)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _has_hpe_pci_storage_device() -> bool:
    pci_dir = "/sys/bus/pci/devices/"
    if not os.path.isdir(pci_dir):
        return False
    try:
        for dev in os.listdir(pci_dir):
            dev_path = os.path.join(pci_dir, dev)
            vendor = _read_pci_attr(dev_path, "vendor")
            if vendor.startswith("0x"):
                vendor = vendor[2:]
            if vendor.lower() in _HPE_PCI_VENDOR_IDS:
                cls = _read_pci_attr(dev_path, "class")
                if cls.startswith("0x"):
                    cls = cls[2:]
                if any(cls.startswith(prefix) for prefix in _STORAGE_CLASS_PREFIXES):
                    return True
    except Exception:
        pass
    return False


def has_hpe_raid_controller() -> bool:
    if _has_active_raid_driver():
        return True
    if _has_raid_in_proc_scsi():
        return True
    if _has_hpe_pci_storage_device():
        return True
    return False


def install_ssacli_if_needed() -> Dict[str, Any]:
    """
    Check and install HPE SSA CLI tools if not present.

    Returns:
        Dict with installation status and any errors.
    """
    is_hpe = is_hpe_server()
    has_raid = has_hpe_raid_controller() if is_hpe else False

    if not (is_hpe and has_raid):
        return {
            "installed": False,
            "already_installed": False,
            "skipped": True,
            "is_hpe": is_hpe,
            "has_raid": has_raid,
            "reason": "Not an HPE server" if not is_hpe else "No HPE Smart Array/RAID controller detected",
            "error": None,
            "output": "",
        }

    if check_ssacli_installed():
        return {
            "installed": True,
            "already_installed": True,
            "skipped": False,
            "is_hpe": True,
            "has_raid": True,
            "reason": None,
            "error": None,
            "output": ""
        }

    result = {
        "installed": False,
        "already_installed": False,
        "skipped": False,
        "is_hpe": True,
        "has_raid": True,
        "reason": None,
        "error": None,
        "output": ""
    }

    try:
        cmd = "apt-get update -y && apt-get install -y smartmontools && (apt-get install -y " + _SSACLI_PACKAGE + " || apt-get install -y ssacli || true)"

        proc = subprocess.run(
            cmd,
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
    Get all storage devices (SATA, SAS, NVMe) connected to the host.
    """
    devices = []
    
    # 1. Try lsscsi -g first
    lsscsi_map = {}
    try:
        proc = subprocess.run(
            ["lsscsi", "-g"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            for line in proc.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    host_part = parts[0].replace("[", "").replace("]", "")
                    blk_dev = ""
                    scsi_path = ""
                    for p in parts:
                        if p.startswith("/dev/sd") or p.startswith("/dev/vd"):
                            blk_dev = p
                        elif p.startswith("/dev/sg"):
                            scsi_path = p
                    if not blk_dev and len(parts) >= 3 and parts[2].startswith("/dev/"):
                        blk_dev = parts[2]
                        
                    vendor = parts[2] if len(parts) > 2 and not parts[2].startswith("/") else ""
                    model = parts[3] if len(parts) > 3 and not parts[3].startswith("/") else ""
                    
                    if blk_dev:
                        lsscsi_map[os.path.basename(blk_dev)] = {
                            "host": host_part,
                            "scsi_path": scsi_path,
                            "vendor": vendor,
                            "model": model,
                        }
    except Exception as e:
        logger.debug("lsscsi check failed: %s", e)

    if not os.path.exists("/sys/block"):
        return devices

    discovered = []
    for dev in sorted(os.listdir("/sys/block")):
        if not dev.startswith(("sd", "nvme", "vd")):
            continue
            
        if re.match(r"^(?:sd[a-z]+\d+|nvme\d+n\d+p\d+|vd[a-z]+\d+)$", dev):
            continue

        sys_block_path = f"/sys/block/{dev}"
        
        # Skip removable USB
        try:
            removable_path = os.path.join(sys_block_path, "removable")
            if os.path.exists(removable_path):
                with open(removable_path, "r") as f:
                    if f.read().strip() == "1":
                        real_path = os.path.realpath(sys_block_path)
                        device_real_path = os.path.realpath(os.path.join(sys_block_path, "device")) if os.path.exists(os.path.join(sys_block_path, "device")) else ""
                        if "usb" in real_path or "usb" in device_real_path:
                            continue
        except Exception:
            pass

        # Skip zero-size
        try:
            size_path = os.path.join(sys_block_path, "size")
            if os.path.exists(size_path):
                with open(size_path, "r") as f:
                    if f.read().strip() == "0":
                        continue
        except Exception:
            pass

        dev_path = f"/dev/{dev}"
        
        if dev.startswith(("sd", "vd")):
            if dev in lsscsi_map:
                info = lsscsi_map[dev]
                vendor = info["vendor"]
                model = info["model"]
                host = info["host"]
                scsi_path = info["scsi_path"]
            else:
                vendor = ""
                model = ""
                host = "0"
                scsi_path = ""
                
                vendor_path = os.path.join(sys_block_path, "device", "vendor")
                if os.path.exists(vendor_path):
                    try:
                        with open(vendor_path, "r") as f:
                            vendor = f.read().strip()
                    except Exception:
                        pass
                
                model_path = os.path.join(sys_block_path, "device", "model")
                if os.path.exists(model_path):
                    try:
                        with open(model_path, "r") as f:
                            model = f.read().strip()
                    except Exception:
                        pass
            
            interface = "sas" if "sas" in vendor.lower() or "sas" in model.lower() else "sata"
            
            discovered.append({
                "host": host,
                "scsi_path": scsi_path,
                "block_device": dev_path,
                "vendor": vendor,
                "model": model,
                "serial": "unknown",
                "interface": interface
            })
            
        elif dev.startswith("nvme"):
            model = ""
            serial = ""
            vendor = "NVMe"
            host = ""
            
            # Find NVMe controller
            ctrl = re.match(r"^(nvme\d+)", dev)
            ctrl_name = ctrl.group(1) if ctrl else ""
            
            model_paths = [
                os.path.join(sys_block_path, "device", "model"),
                f"/sys/class/nvme/{ctrl_name}/model" if ctrl_name else ""
            ]
            for p in model_paths:
                if p and os.path.exists(p):
                    try:
                        with open(p, "r") as f:
                            model = f.read().strip()
                            break
                    except Exception:
                        pass
                        
            serial_paths = [
                os.path.join(sys_block_path, "device", "serial"),
                f"/sys/class/nvme/{ctrl_name}/serial" if ctrl_name else ""
            ]
            for p in serial_paths:
                if p and os.path.exists(p):
                    try:
                        with open(p, "r") as f:
                            serial = f.read().strip()
                            break
                    except Exception:
                        pass
            
            vendor_path = f"/sys/class/nvme/{ctrl_name}/vendor" if ctrl_name else ""
            if vendor_path and os.path.exists(vendor_path):
                try:
                    with open(vendor_path, "r") as f:
                        vendor_val = f.read().strip()
                        if vendor_val:
                            vendor = vendor_val
                except Exception:
                    pass
            
            if ctrl_name:
                host_match = re.search(r"nvme(\d+)", ctrl_name)
                if host_match:
                    host = host_match.group(1)
            
            discovered.append({
                "host": host,
                "scsi_path": "",
                "block_device": dev_path,
                "vendor": vendor,
                "model": model,
                "serial": serial,
                "interface": "nvme"
            })
            
    discovered.sort(key=lambda x: (not x['block_device'].startswith('/dev/sd'), x['block_device']))
    for i, d in enumerate(discovered):
        d["index"] = i
        devices.append(d)
        
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


async def get_smartctl_info(device_path: str, is_hpe_raid: Optional[bool] = None) -> Dict[str, Any]:
    """
    Get SMART information for a device using smartctl with cciss fallback.

    Args:
        device_path: Device path like /dev/sda
        is_hpe_raid: Optional pre-computed boolean for HPE RAID controller presence.

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
        "interface": "unknown",
        "temperature": None,
        "data_units_written": None,
        "critical_warning": None,
        "raw_output": ""
    }

    if not check_smartctl_installed():
        result["error"] = "smartctl not installed"
        return result

    if is_hpe_raid is None:
        is_hpe_raid = is_hpe_server() and has_hpe_raid_controller()

    try:
        smartctl_bin = find_smartctl_path() or SMARTCTL_PATH
        is_nvme = "nvme" in os.path.basename(device_path)
        
        proc = None
        
        # Intelligent routing
        if is_nvme:
            # NEVER execute -d cciss for nvme
            proc = subprocess.run(
                [smartctl_bin, "-x", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )
        elif is_hpe_raid:
            # Try cciss interface first
            proc = subprocess.run(
                [smartctl_bin, "-d", "cciss", "-x", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            # If cciss fails, fallback
            if proc.returncode != 0:
                proc = subprocess.run(
                    [smartctl_bin, "-x", device_path],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
        else:
            # Direct SATA/SAS on non-HPE
            proc = subprocess.run(
                [smartctl_bin, "-x", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )

        result["raw_output"] = proc.stdout

        if proc.returncode == 0:
            result["success"] = True

            wear_level = parse_smartctl_wear(proc.stdout)
            
            # Parse additional fields
            crit_warn_m = re.search(r"Critical Warning:\s*(0x[0-9a-fA-F]+|\d+)", proc.stdout)
            if crit_warn_m:
                val_str = crit_warn_m.group(1)
                result["critical_warning"] = int(val_str, 16) if val_str.startswith("0x") else int(val_str)
                
            overall_health_m = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", proc.stdout)
            health_status_m = re.search(r"SMART Health Status:\s*(\w+)", proc.stdout)
            overall_health = None
            if overall_health_m:
                overall_health = overall_health_m.group(1).upper()
            elif health_status_m:
                overall_health = health_status_m.group(1).upper()
                
            temp_m = re.search(r"(?:Current\s+Drive\s+Temperature:\s*|Temperature:\s*)(\d+)\s*(?:Celsius|C)?\b", proc.stdout, re.I)
            if temp_m:
                result["temperature"] = int(temp_m.group(1))
                
            duw_m = re.search(r"Data Units Written:\s*([0-9,]+(?:\s*\[[^\]]+\])?)", proc.stdout)
            if duw_m:
                result["data_units_written"] = duw_m.group(1).strip()
                
            if wear_level is not None:
                wear_level = max(0, min(100, wear_level))
                result["wear_leveling_count"] = wear_level

            # Health Status evaluation
            if overall_health in ("FAILED", "BAD") or (result["critical_warning"] is not None and result["critical_warning"] > 0):
                result["health_status"] = "critical"
            elif wear_level is not None:
                if wear_level >= WEAR_CRITICAL_THRESHOLD:
                    result["health_status"] = "critical"
                elif wear_level >= WEAR_WARNING_THRESHOLD:
                    result["health_status"] = "warning"
                else:
                    result["health_status"] = "healthy"
            elif overall_health in ("PASSED", "OK"):
                result["health_status"] = "healthy"
            else:
                result["health_status"] = "unknown"

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
                
            # Interface
            if is_nvme:
                result["interface"] = "nvme"
            elif "SAS" in proc.stdout:
                result["interface"] = "sas"
            else:
                result["interface"] = "sata"

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

    is_hpe_raid = is_hpe_server() and has_hpe_raid_controller()

    for device in devices:
        device_path = device.get("block_device", "")
        if not device_path:
            continue

        health_info = await get_smartctl_info(device_path, is_hpe_raid=is_hpe_raid)

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
            "error": health_info.get("error"),
            "interface": health_info.get("interface") or device.get("interface", "sata"),
            "temperature": health_info.get("temperature"),
            "critical_warning": health_info.get("critical_warning")
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

    is_hpe = is_hpe_server()
    has_raid = has_hpe_raid_controller() if is_hpe else False
    
    drives = health.get("drives", [])
    drive_counts = {"total": len(drives), "nvme": 0, "sata": 0, "sas": 0, "other": 0}
    for d in drives:
        # try to get interface from block device
        dev = d.get("block_device", "")
        if "nvme" in dev:
            drive_counts["nvme"] += 1
        elif d.get("interface"):
            if d["interface"] == "sata":
                drive_counts["sata"] += 1
            elif d["interface"] == "sas":
                drive_counts["sas"] += 1
            else:
                drive_counts["other"] += 1
        else:
            drive_counts["other"] += 1

    if has_raid and drive_counts["nvme"] > 0:
        controller_type = "mixed"
    elif has_raid:
        controller_type = "hpe_smartarray"
    elif len(drives) > 0:
        controller_type = "direct_attached"
    else:
        controller_type = "none"

    diagnostics = {
        "smartctl_installed": check_smartctl_installed(),
        "smartctl_path": find_smartctl_path(),
        "ssacli_installed": check_ssacli_installed(),
        "ssacli_path": find_ssacli_path(),
        "nvme_tools_installed": check_nvme_installed(),
        "nvme_tools_path": find_nvme_path(),
        "is_hpe": is_hpe,
        "has_raid": has_raid,
        "controller_type": controller_type,
        "drive_counts": drive_counts,
    }

    return {
        "drives": drives,
        "summary": health.get("summary", {}),
        "alerts": alerts,
        "historical_trends": trends,
        "diagnostics": diagnostics,
        "timestamp": health.get("timestamp", time.time())
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
