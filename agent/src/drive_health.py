"""Drive health monitoring for Proxmox storage.

Provides SSD health monitoring via smartctl with cciss device interface and
standard fallback, extracting wear levels (0-100 scale) and reporting:
- Physical drive index
- Wear level percentage (0-100%, lower is better)
- Model/serial info
- Health status (healthy, warning, critical)
- Historical wear trend data
"""

# Standard-library helpers for subprocesses, JSON history, parsing, paths and timing.
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# Dedicated logger for drive health messages.
logger = logging.getLogger("DriveHealth")

# Path to HPE SSA CLI tools (if installed)
# Default install locations of the HPE array tool and smartctl.
SSACLI_PATH = "/usr/sbin/ssacli"
SMARTCTL_PATH = "/usr/bin/smartctl"

def find_nvme_path() -> Optional[str]:
    """Locate nvme CLI executable across system paths."""
    # Prefer whatever is on PATH.
    found = shutil.which("nvme")
    if found and os.access(found, os.X_OK):
        return found
    # Otherwise try the usual install locations.
    for p in ("/usr/sbin/nvme", "/usr/bin/nvme", "/sbin/nvme", "/usr/local/sbin/nvme"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None

def check_nvme_installed() -> bool:
    """Check if nvme-cli is installed."""
    # True when the nvme binary can be found.
    return find_nvme_path() is not None

def find_smartctl_path() -> Optional[str]:
    """Locate smartctl executable across system paths."""
    # Prefer whatever is on PATH.
    found = shutil.which("smartctl")
    if found and os.access(found, os.X_OK):
        return found
    # Otherwise try the usual install locations.
    for p in ("/usr/sbin/smartctl", "/usr/bin/smartctl", "/sbin/smartctl", "/usr/local/sbin/smartctl"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None

def find_ssacli_path() -> Optional[str]:
    """Locate ssacli or hpssacli executable across system paths."""
    # Try both tool names on PATH first.
    for name in ("ssacli", "hpssacli"):
        found = shutil.which(name)
        if found and os.access(found, os.X_OK):
            return found
    # Otherwise try the usual install locations.
    for p in ("/usr/sbin/ssacli", "/usr/sbin/hpssacli", "/usr/bin/ssacli", "/opt/hp/hpssacli/bld/hpssacli"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None

def check_ssacli_installed() -> bool:
    """Check if HPE SSA CLI tools are installed."""
    # True when ssacli/hpssacli can be found.
    return find_ssacli_path() is not None

def check_smartctl_installed() -> bool:
    """Check if smartctl is installed."""
    # True when smartctl can be found.
    return find_smartctl_path() is not None

# Wear level thresholds
# Wear percentages at which a drive becomes "warning" and "critical".
WEAR_WARNING_THRESHOLD = 60  # Start warning at 60%
WEAR_CRITICAL_THRESHOLD = 80  # Critical at 80%+

# Historical data storage
# Where wear readings are stored between runs for trend data.
HISTORY_FILE = "/var/lib/pxmx/drive_health_history.json"

# Command constants
# Command names this module handles.
PXMX_DRIVE_HEALTH = "PXMX_DRIVE_HEALTH"
PXMX_INSTALL_SSACLI = "PXMX_INSTALL_SSACLI"


class DriveHealthError(Exception):
    """Drive health monitoring error."""
    pass



# Vendor strings (lower-case) that identify HPE hardware.
_HPE_VENDOR_STRINGS = frozenset({
    "hpe",
    "hp",
    "hewlett-packard",
    "hewlett packard enterprise",
    "proliant",
})
# HPE PCI vendor ids, PCI class prefixes for storage controllers, HPE RAID driver names, and the apt
# package that provides the array CLI.
_HPE_PCI_VENDOR_IDS = frozenset({"103c", "1590"})
_STORAGE_CLASS_PREFIXES = ("0104", "0100")
_HPE_RAID_DRIVER_NAMES = ("smartpqi", "hpsa", "cciss")
_SSACLI_PACKAGE = "hpssacli"


def _read_sysfs_vendor() -> str:
    """Read system hardware vendor string from sysfs DMI table."""
    # DMI vendor file; empty string when missing or unreadable.
    path = "/sys/class/dmi/id/sys_vendor"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _read_sysfs_product_name() -> str:
    """Read system product name string from sysfs DMI table."""
    # DMI product-name file; empty string when missing or unreadable.
    path = "/sys/class/dmi/id/product_name"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _read_dmidecode_vendor() -> str:
    """Read system manufacturer string using dmidecode command."""
    # Ask dmidecode for the manufacturer; empty string on any failure.
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
    """Check if string matches known HPE vendor identifiers."""
    # Empty values never match.
    if not val:
        return False
    # Case-insensitive substring match against the known HPE names.
    val_lower = val.lower()
    return any(v in val_lower for v in _HPE_VENDOR_STRINGS)


def is_hpe_server() -> bool:
    """Determine if the host machine is an HPE server platform."""
    # Try the DMI vendor, then the product name, then dmidecode.
    if _matches_hpe_vendor(_read_sysfs_vendor()):
        return True
    if _matches_hpe_vendor(_read_sysfs_product_name()):
        return True
    if _matches_hpe_vendor(_read_dmidecode_vendor()):
        return True
    return False


def _has_active_raid_driver() -> bool:
    """Check if an HPE RAID driver is currently loaded in sysfs."""
    # A loaded driver has PCI-address entries (e.g. 0000:03:00.0) in its sysfs directory.
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
    """Check if Smart Array controllers are reported in /proc/scsi/scsi."""
    # Look for Smart Array controller names in the kernel's SCSI list.
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
    """Read attribute string for a given PCI device path."""
    # Read the attribute file; empty string when missing or unreadable.
    path = os.path.join(dev_path, attr)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _has_hpe_pci_storage_device() -> bool:
    """Check PCI bus for HPE storage controllers."""
    # No PCI sysfs means nothing to check.
    pci_dir = "/sys/bus/pci/devices/"
    if not os.path.isdir(pci_dir):
        return False
    # Look for an HPE-vendor PCI device whose class is a storage controller.
    try:
        for dev in os.listdir(pci_dir):
            dev_path = os.path.join(pci_dir, dev)
            # Vendor id without the 0x prefix.
            vendor = _read_pci_attr(dev_path, "vendor")
            if vendor.startswith("0x"):
                vendor = vendor[2:]
            if vendor.lower() in _HPE_PCI_VENDOR_IDS:
                # Class code without the 0x prefix; storage controllers start with 0104 or 0100.
                cls = _read_pci_attr(dev_path, "class")
                if cls.startswith("0x"):
                    cls = cls[2:]
                if any(cls.startswith(prefix) for prefix in _STORAGE_CLASS_PREFIXES):
                    return True
    # Any sysfs error means "not found".
    except Exception:
        pass
    return False


def has_hpe_raid_controller() -> bool:
    """Detect presence of HPE Smart Array / SmartRAID controller."""
    # Check the loaded drivers, then /proc/scsi, then the PCI bus.
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
    # Only HPE servers with an array controller need the tool.
    is_hpe = is_hpe_server()
    has_raid = has_hpe_raid_controller() if is_hpe else False

    # Not needed: report why it was skipped.
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

    # Already installed: nothing to do.
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

    # Result template for an install attempt.
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

    # Install smartmontools and the HPE array CLI with apt (2 minute limit).
    try:
        cmd = "apt-get update -y && apt-get install -y smartmontools && (apt-get install -y " + _SSACLI_PACKAGE + " || apt-get install -y ssacli || true)"

        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120
        )

        # Record success output or the error.
        if proc.returncode == 0:
            result["installed"] = True
            result["output"] = proc.stdout
        else:
            result["error"] = proc.stderr or f"Exit code: {proc.returncode}"

    # Timed out or failed to run.
    except subprocess.TimeoutExpired:
        result["error"] = "Installation timed out after 120s"
    except Exception as e:
        result["error"] = f"Installation failed: {str(e)}"

    return result



def discover_hpe_cciss_physical_drives(ctrl_dev: str = "/dev/sda", max_probes: int = 16) -> List[Dict[str, Any]]:
    """Discover physical drives behind an HPE Smart Array logical volume."""
    # Drives found, consecutive empty slots, and the smartctl binary to use.
    discovered = []
    miss_count = 0
    smartctl_bin = find_smartctl_path() or SMARTCTL_PATH
    
    # Probe each controller slot with smartctl's cciss passthrough.
    for i in range(max_probes):
        try:
            proc = subprocess.run(
                [smartctl_bin, "-d", f"cciss,{i}", "-i", ctrl_dev],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            # Slot answered: pull vendor, model and serial from the output.
            if proc.returncode == 0:
                vendor_m = re.search(r"Vendor:\s*(.+)", proc.stdout, re.IGNORECASE)
                model_m = re.search(r"(?:Device Model|Model Number|Product):\s*(.+)", proc.stdout, re.IGNORECASE)
                serial_m = re.search(r"Serial [Nn]umber:\s*(.+)", proc.stdout, re.IGNORECASE)
                
                # Use empty strings (or "unknown" serial) when a field is missing.
                vendor = vendor_m.group(1).strip() if vendor_m else ""
                model = model_m.group(1).strip() if model_m else ""
                serial = serial_m.group(1).strip() if serial_m else "unknown"
                
                # No identity at all counts as an empty slot.
                if not vendor and not model:
                    miss_count += 1
                # Otherwise record the drive (SAS or SATA) and reset the empty-slot count.
                else:
                    interface = "sas" if ("SAS" in proc.stdout or "sas" in model.lower() or "sas" in vendor.lower()) else "sata"
                    discovered.append({
                        "cciss_index": i,
                        "vendor": vendor,
                        "model": model,
                        "serial": serial,
                        "interface": interface
                    })
                    miss_count = 0
            else:
                # Non-zero returncode: check if this is a clean vacant slot
                out_text = f"{proc.stdout} {proc.stderr}".lower()
                is_vacant = (
                    "device open failed" in out_text
                    or "no such device" in out_text
                    or "inquiry failed" in out_text
                    or "device not found" in out_text
                )
                if is_vacant:
                    miss_count += 1
                else:
                    logger.debug("smartctl cciss probe index %d non-zero exit %d: %s", i, proc.returncode, proc.stderr)
                
            # Stop after 4 empty slots in a row once at least one drive was found.
            if len(discovered) > 0 and miss_count >= 4:
                break
                
        except subprocess.TimeoutExpired:
            logger.debug("smartctl cciss probe index %d timed out; continuing probe", i)
            # Do NOT increment miss_count on timeout so transient stalls do not trigger early break
            continue
        # Other errors: log and try the next slot.
        except Exception as e:
            logger.debug("smartctl cciss probe index %d error: %s", i, e)
            continue
            
    # Every physical drive found behind the controller.
    return discovered

async def get_scsi_devices() -> List[Dict[str, Any]]:

    """
    Get all storage devices (SATA, SAS, NVMe) connected to the host.
    """
    # Collected devices.
    devices = []
    
    # 1. Try lsscsi -g first
    lsscsi_map = {}
    # Run lsscsi and map each block device to its SCSI host, sg path, vendor and model.
    try:
        proc = subprocess.run(
            ["lsscsi", "-g"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # Parse each non-empty line.
            for line in proc.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    # SCSI host address without brackets; find the /dev/sd*|vd* and /dev/sg* paths.
                    host_part = parts[0].replace("[", "").replace("]", "")
                    blk_dev = ""
                    scsi_path = ""
                    for p in parts:
                        if p.startswith("/dev/sd") or p.startswith("/dev/vd"):
                            blk_dev = p
                        elif p.startswith("/dev/sg"):
                            scsi_path = p
                    # Fall back to the third column when it is a device path.
                    if not blk_dev and len(parts) >= 3 and parts[2].startswith("/dev/"):
                        blk_dev = parts[2]
                        
                    # Vendor and model columns (when they aren't paths).
                    vendor = parts[2] if len(parts) > 2 and not parts[2].startswith("/") else ""
                    model = parts[3] if len(parts) > 3 and not parts[3].startswith("/") else ""
                    
                    # Index by block device name (e.g. "sda").
                    if blk_dev:
                        lsscsi_map[os.path.basename(blk_dev)] = {
                            "host": host_part,
                            "scsi_path": scsi_path,
                            "vendor": vendor,
                            "model": model,
                        }
    # lsscsi missing or failed: carry on with sysfs only.
    except Exception as e:
        logger.debug("lsscsi check failed: %s", e)

    def _build_from_lsscsi() -> List[Dict[str, Any]]:
        """Build device entries from the lsscsi output alone (used when /sys/block isn't available).
        """
        # One entry per block device lsscsi reported.
        lsscsi_devices = []
        for blk_name, info in lsscsi_map.items():
            vendor = info["vendor"]
            model = info["model"]
            # HP/HPE "LOGICAL" devices are RAID volumes, not physical drives.
            if vendor.upper() in ("HP", "HPE") and "LOGICAL" in model.upper():
                interface = "raid_logical"
                is_raid_logical = True
            else:
                interface = "sas" if "sas" in vendor.lower() or "sas" in model.lower() else "sata"
                is_raid_logical = False
            # Record the device; serials aren't known from lsscsi.
            lsscsi_devices.append({
                "host": info["host"],
                "scsi_path": info["scsi_path"],
                "block_device": f"/dev/{blk_name}",
                "vendor": vendor,
                "model": model,
                "serial": "unknown",
                "interface": interface,
                "is_raid_logical": is_raid_logical,
            })
        # Sort sd* devices first, then by name, and number them.
        lsscsi_devices.sort(key=lambda x: (not x['block_device'].startswith('/dev/sd'), x['block_device']))
        for i, d in enumerate(lsscsi_devices):
            d["index"] = i
        return lsscsi_devices

    if not os.path.exists("/sys/block"):
        # /sys/block is unavailable (containerized/chrooted host, restricted
        # mount namespace) but lsscsi -g above may still have succeeded — that
        # was previously discarded outright, silently reporting zero drives on
        # any host where /sys/block isn't mounted even though lsscsi saw them
        # all. Build best-effort entries straight from the lsscsi output
        # instead; per-device sysfs details (removable/zero-size filtering,
        # cciss physical-drive expansion, NVMe) simply aren't available here.
        return _build_from_lsscsi()

    # Results, plus serials/slots already seen so a drive behind several logical volumes is listed
    # once.
    discovered = []
    seen_cciss_serials: Set[str] = set()
    seen_cciss_indices: Set[int] = set()
    sys_blocks = sorted(os.listdir("/sys/block"))
    # Walk the kernel's block devices.
    for dev in sys_blocks:
        # Only SATA/SAS (sd), NVMe and virtio (vd) disks.
        if not dev.startswith(("sd", "nvme", "vd")):
            continue
            
        # Skip partitions (sda1, nvme0n1p1, vda1).
        if re.match(r"^(?:sd[a-z]+\d+|nvme\d+n\d+p\d+|vd[a-z]+\d+)$", dev):
            continue

        # sysfs directory for this disk.
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

        # Device node path.
        dev_path = f"/dev/{dev}"
        
        # SATA/SAS/virtio disks.
        if dev.startswith(("sd", "vd")):
            # Use lsscsi details when available.
            if dev in lsscsi_map:
                info = lsscsi_map[dev]
                vendor = info["vendor"]
                model = info["model"]
                host = info["host"]
                scsi_path = info["scsi_path"]
            # Otherwise read vendor and model from sysfs.
            else:
                # Start with blanks; fill vendor and model from sysfs below.
                vendor = ""
                model = ""
                host = "0"
                scsi_path = ""
                
                # Vendor string from sysfs (if readable).
                vendor_path = os.path.join(sys_block_path, "device", "vendor")
                if os.path.exists(vendor_path):
                    try:
                        with open(vendor_path, "r") as f:
                            vendor = f.read().strip()
                    except Exception:
                        pass
                
                # Model string from sysfs (if readable).
                model_path = os.path.join(sys_block_path, "device", "model")
                if os.path.exists(model_path):
                    try:
                        with open(model_path, "r") as f:
                            model = f.read().strip()
                    except Exception:
                        pass
            
            # HPE logical volume: list the physical drives behind it instead.
            if vendor.upper() in ("HP", "HPE") and "LOGICAL" in model.upper():
                cciss_drives = discover_hpe_cciss_physical_drives(dev_path)
                if cciss_drives:
                    # Add each physical drive once (by serial, or by slot when the serial is
                    # unknown).
                    for c_drv in cciss_drives:
                        s = (c_drv.get("serial") or "").strip()
                        idx = c_drv.get("cciss_index")
                        if s and s != "unknown" and s in seen_cciss_serials:
                            continue
                        if idx is not None and idx in seen_cciss_indices and (not s or s == "unknown"):
                            continue
                        # Remember this drive so other logical volumes don't list it again.
                        if s and s != "unknown":
                            seen_cciss_serials.add(s)
                        if idx is not None:
                            seen_cciss_indices.add(idx)
                        # Record the physical drive with its cciss slot.
                        discovered.append({
                            "host": host,
                            "scsi_path": scsi_path,
                            "block_device": f"{dev_path} [cciss,{idx}]",
                            "device_path": dev_path,
                            "cciss_index": idx,
                            "vendor": c_drv["vendor"],
                            "model": c_drv["model"],
                            "serial": c_drv["serial"],
                            "interface": c_drv["interface"],
                            "is_raid_logical": False,
                        })
                    continue
                # If cciss probing yielded no member physical drives, preserve the container
                # but explicitly flag it as a logical container so downstream consumers and UI
                # know it is a RAID virtual volume rather than a bare physical drive.
                discovered.append({
                    "host": host,
                    "scsi_path": scsi_path,
                    "block_device": dev_path,
                    "device_path": dev_path,
                    "vendor": vendor,
                    "model": model,
                    "serial": "unknown",
                    "interface": "raid_logical",
                    "is_raid_logical": True,
                })
                continue

            # Plain disk: SAS or SATA based on the vendor/model text.
            interface = "sas" if "sas" in vendor.lower() or "sas" in model.lower() else "sata"
            
            # Record it.
            discovered.append({
                "host": host,
                "scsi_path": scsi_path,
                "block_device": dev_path,
                "vendor": vendor,
                "model": model,
                "serial": "unknown",
                "interface": interface,
                "is_raid_logical": False,
            })
            
        # NVMe disks: details come from sysfs.
        elif dev.startswith("nvme"):
            # Defaults for NVMe; filled from sysfs below.
            model = ""
            serial = ""
            vendor = "NVMe"
            host = ""
            
            # Find NVMe controller
            ctrl = re.match(r"^(nvme\d+)", dev)
            ctrl_name = ctrl.group(1) if ctrl else ""
            
            # Model from the block device or the NVMe controller, whichever exists first.
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
                        
            # Serial from the block device or the NVMe controller, whichever exists first.
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
            
            # Prefer the controller's own vendor string when there is one.
            vendor_path = f"/sys/class/nvme/{ctrl_name}/vendor" if ctrl_name else ""
            if vendor_path and os.path.exists(vendor_path):
                try:
                    with open(vendor_path, "r") as f:
                        vendor_val = f.read().strip()
                        if vendor_val:
                            vendor = vendor_val
                except Exception:
                    pass
            
            # Use the controller number (nvme0 → 0) as the host.
            if ctrl_name:
                host_match = re.search(r"nvme(\d+)", ctrl_name)
                if host_match:
                    host = host_match.group(1)
            
            # Record the NVMe drive.
            discovered.append({
                "host": host,
                "scsi_path": "",
                "block_device": dev_path,
                "vendor": vendor,
                "model": model,
                "serial": serial,
                "interface": "nvme"
            })
            
    # Drop duplicate drives with the same known serial.
    deduped = []
    seen_serials = set()
    for d in discovered:
        s = (d.get("serial") or "").strip()
        if s and s != "unknown":
            if s in seen_serials:
                continue
            seen_serials.add(s)
        deduped.append(d)
    discovered = deduped

    # Sort sd* devices first, then by name, number them, and add them to the result.
    discovered.sort(key=lambda x: (not x['block_device'].startswith('/dev/sd'), x['block_device']))
    for i, d in enumerate(discovered):
        d["index"] = i
        devices.append(d)
        
    # Every discovered device.
    return devices

def parse_smartctl_wear(stdout: str) -> Optional[int]:
    """
    Parse wear level percentage (0-100 scale, higher is more worn)
    across Samsung, Intel/WD, NVMe, and SAS smartctl formats.
    """
    # 1. Key-value formats:
    # Samsung wear count.
    m = re.search(r"Wear_Leveling_Count\s*[:=]\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # NVMe percentage used.
    m = re.search(r"PercentageUsed\s*[:=]\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Percentage still available: convert to percentage used.
    m = re.search(r"PercentageAvailable\s*[:=]\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return 100 - int(m.group(1))

    # 2. NVMe text format: "Percentage Used: 5%"
    # NVMe text output.
    m = re.search(r"Percentage\s+Used\s*:\s*(\d+)%?", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 3. SAS endurance indicator: "Percentage used endurance indicator: 1%"
    # SAS endurance indicator.
    m = re.search(r"Percentage\s+used\s+endurance\s+indicator\s*:\s*(\d+)%?", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 4. Available Spare: "Available Spare: 100%"
    # Spare capacity left: convert to percentage used.
    m = re.search(r"Available\s+Spare\s*:\s*(\d+)%?", stdout, re.IGNORECASE)
    if m:
        return 100 - int(m.group(1))

    # 5. Standard smartctl attribute table format:
    # ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
    # Attribute table: capture the normalised value and the raw value.
    table_m = re.search(
        r"(?:Wear_Leveling_Count|Used_RBV_Wear_Leveling|Media_Wearout_Indicator)\s+"
        r"0x[0-9a-fA-F]+\s+(\d+)\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        stdout, re.IGNORECASE
    )
    # Use the raw value when it already looks like a wear percentage; otherwise derive wear from the
    # normalised value.
    if table_m:
        val = int(table_m.group(1))  # normalized value (e.g. 85 = 85% life left)
        raw = int(table_m.group(2))  # raw value (e.g. 15 = 15% wear or cycle count)
        if raw <= 100 and abs((100 - val) - raw) <= 2:
            return raw
        return 100 - val if val <= 100 else raw

    # 6. Generic pattern for any attribute with "Wear" in name
    # Last resort: any attribute with "Wear" in its name.
    generic_m = re.search(r"(?i)([A-Za-z_]*Wear[A-Za-z_]*)\s*[:=\s]+(\d+)", stdout)
    if generic_m:
        return int(generic_m.group(2))

    # No wear information found.
    return None


async def get_smartctl_info(device_path: str, is_hpe_raid: Optional[bool] = None, cciss_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Get SMART information for a device using smartctl with cciss fallback.

    Args:
        device_path: Device path like /dev/sda
        is_hpe_raid: Optional pre-computed boolean for HPE RAID controller presence.

    Returns:
        Dict with SMART data including wear level if available.
    """
    # Default result, filled in as data is parsed.
    result = {
        "device": device_path,
        "success": False,
        "error": None,
        "wear_leveling_count": None,
        "model": None,
        "serial": None,
        "health_status": "unknown",
        "interface": None,
        "temperature": None,
        "data_units_written": None,
        "critical_warning": None,
        "raw_output": ""
    }

    # smartctl is required.
    if not check_smartctl_installed():
        result["error"] = "smartctl not installed"
        return result

    # Work out whether this is an HPE array host if the caller didn't say.
    if is_hpe_raid is None:
        is_hpe_raid = is_hpe_server() and has_hpe_raid_controller()

    # Any failure is reported in the result's error field.
    try:
        # smartctl binary, and whether the device is NVMe.
        smartctl_bin = find_smartctl_path() or SMARTCTL_PATH
        is_nvme = "nvme" in os.path.basename(device_path)
        
        # Will hold the smartctl result.
        proc = None
        
        # Intelligent routing
        # Physical drive behind an HPE controller: query it by slot.
        if cciss_index is not None:
            proc = subprocess.run(
                [smartctl_bin, "-d", f"cciss,{cciss_index}", "-a", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )
        # NVMe: always query directly.
        elif is_nvme:
            # NEVER execute -d cciss for nvme
            proc = subprocess.run(
                [smartctl_bin, "-x", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )
        # HPE array host: try the cciss passthrough, then a direct query.
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
        # Everything else: query directly.
        else:
            # Direct SATA/SAS on non-HPE
            proc = subprocess.run(
                [smartctl_bin, "-x", device_path],
                capture_output=True,
                text=True,
                timeout=30
            )

        # Keep the raw output for debugging.
        result["raw_output"] = proc.stdout

        # smartctl can exit non-zero yet still print useful data.
        has_smart_data = bool(
            proc.stdout
            and (
                "=== START OF" in proc.stdout
                or "SMART Attributes Data Structure" in proc.stdout
                or "Wear_Leveling_Count" in proc.stdout
                or "SMART overall-health" in proc.stdout
                or "SMART Health Status" in proc.stdout
                or "Device Model" in proc.stdout
                or "Model Number" in proc.stdout
            )
        )
        # Parse when the command worked or the output has SMART data.
        if proc.returncode == 0 or has_smart_data:
            result["success"] = True
            result["error"] = None

            # Wear level as a percentage used.
            wear_level = parse_smartctl_wear(proc.stdout)
            
            # Parse additional fields
            # NVMe critical warning bits (hex or decimal).
            crit_warn_m = re.search(r"Critical Warning:\s*(0x[0-9a-fA-F]+|\d+)", proc.stdout)
            if crit_warn_m:
                val_str = crit_warn_m.group(1)
                result["critical_warning"] = int(val_str, 16) if val_str.startswith("0x") else int(val_str)
                
            # Overall health verdict (ATA or SCSI wording).
            overall_health_m = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", proc.stdout)
            health_status_m = re.search(r"SMART Health Status:\s*(\w+)", proc.stdout)
            overall_health = None
            if overall_health_m:
                overall_health = overall_health_m.group(1).upper()
            elif health_status_m:
                overall_health = health_status_m.group(1).upper()
                
            # Drive temperature in Celsius.
            temp_m = re.search(r"(?:Current\s+Drive\s+Temperature:\s*|Temperature:\s*)(\d+)\s*(?:Celsius|C)?\b", proc.stdout, re.I)
            if temp_m:
                result["temperature"] = int(temp_m.group(1))
                
            # NVMe data units written, as printed.
            duw_m = re.search(r"Data Units Written:\s*([0-9,]+(?:\s*\[[^\]]+\])?)", proc.stdout)
            if duw_m:
                result["data_units_written"] = duw_m.group(1).strip()
                
            # Clamp wear to 0–100 and store it.
            if wear_level is not None:
                wear_level = max(0, min(100, wear_level))
                result["wear_leveling_count"] = wear_level

            # Health Status evaluation
            # Health: failed self-test or a critical warning is critical; then wear thresholds; then
            # the self-test verdict.
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
                
            # Parse vendor
            # Vendor from its own line, or guessed from the model family/model name.
            vendor_match = re.search(r"Vendor:\s*(.+)", proc.stdout, re.IGNORECASE)
            if vendor_match:
                result["vendor"] = vendor_match.group(1).strip()
            else:
                # Extract recognized manufacturers from Model Family or model string
                mf_match = re.search(r"Model Family:\s*(.+)", proc.stdout, re.IGNORECASE)
                mf_str = mf_match.group(1) if mf_match else ""
                search_corpus = f"{mf_str} {result.get('model') or ''}".lower()
                for v_name in ("Samsung", "Intel", "Crucial", "Micron", "Western Digital", "WD", "Seagate", "Toshiba", "Kioxia", "Kingston", "SanDisk", "SK hynix", "Solidigm"):
                    if v_name.lower() in search_corpus:
                        result["vendor"] = v_name
                        break

            # Interface
            # Interface type: NVMe, SAS if smartctl mentions it, otherwise SATA.
            if is_nvme:
                result["interface"] = "nvme"
            elif "SAS" in proc.stdout:
                result["interface"] = "sas"
            else:
                result["interface"] = "sata"

        # No usable output: record smartctl's error.
        else:
            result["error"] = proc.stderr or "smartctl failed"

    # smartctl hung or couldn't run.
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
    # Empty result with zeroed counters.
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

    # Find all drives; return the empty result if there are none.
    devices = await get_scsi_devices()

    if not devices:
        logger.warning("No storage devices found")
        return result

    # Count the drives and check once whether this is an HPE array host.
    result["summary"]["total_drives"] = len(devices)

    is_hpe_raid = is_hpe_server() and has_hpe_raid_controller()

    # Query each drive.
    for device in devices:
        # Device node to query (the controller node for drives behind an HPE array).
        raw_device_path = device.get("device_path") or device.get("block_device", "")
        if not raw_device_path:
            continue

        # Read SMART data, using the cciss slot when there is one.
        cciss_index = device.get("cciss_index")
        health_info = await get_smartctl_info(raw_device_path, is_hpe_raid=is_hpe_raid, cciss_index=cciss_index)

        # Prefer the discovery vendor unless it's a placeholder like "ATA".
        dev_vendor = (device.get("vendor") or "").strip()
        if dev_vendor.upper() in ("", "ATA", "UNKNOWN"):
            vendor = health_info.get("vendor") or dev_vendor
        else:
            vendor = dev_vendor or health_info.get("vendor") or ""

        # Interface: discovery value for RAID volumes, smartctl's otherwise, defaulting to SATA.
        raw_iface = device.get("interface") if device.get("is_raid_logical") else health_info.get("interface")
        if not raw_iface or raw_iface == "unknown":
            dev_iface = device.get("interface")
            raw_iface = dev_iface if dev_iface and dev_iface != "unknown" else "sata"

        # Merge discovery and SMART data into one record for this drive.
        drive_info = {
            "physical_index": device.get("index", 0),
            "scsi_path": device.get("scsi_path", ""),
            "block_device": device.get("block_device") or raw_device_path,
            "device_path": raw_device_path,
            "cciss_index": device.get("cciss_index"),
            "vendor": vendor,
            "model": health_info.get("model") or device.get("model", ""),
            "serial": health_info.get("serial") or device.get("serial", "unknown"),
            "wear_level": health_info.get("wear_leveling_count"),
            "health_status": health_info.get("health_status", "unknown"),
            "success": health_info.get("success", False),
            "error": health_info.get("error"),
            "interface": raw_iface,
            "temperature": health_info.get("temperature"),
            "critical_warning": health_info.get("critical_warning"),
            "is_raid_logical": device.get("is_raid_logical", False),
        }

        # Add it to the list.
        result["drives"].append(drive_info)

        # Count the drive under its health status.
        status = drive_info.get("health_status", "unknown")
        if status == "healthy":
            result["summary"]["healthy_drives"] += 1
        elif status == "warning":
            result["summary"]["warning_drives"] += 1
        elif status == "critical":
            result["summary"]["critical_drives"] += 1
        else:
            result["summary"]["unknown_drives"] += 1

    # Full results with the summary.
    return result


def save_history(drive_health: Dict[str, Any]) -> bool:
    """
    Save drive health snapshot to history file.

    Args:
        drive_health: Current drive health data

    Returns:
        True if saved successfully, False otherwise.
    """
    # Any failure is logged and returns False.
    try:
        # Make sure the directory exists and load the existing history.
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        history = load_history()

        # Snapshot of this check.
        snapshot = {
            "timestamp": time.time(),
            "drives": drive_health.get("drives", []),
            "summary": drive_health.get("summary", {})
        }

        # Append it to the snapshot list.
        if "snapshots" not in history:
            history["snapshots"] = []

        history["snapshots"].append(snapshot)

        # Keep only last 100 snapshots
        if len(history["snapshots"]) > 100:
            history["snapshots"] = history["snapshots"][-100:]

        # Write the history back.
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
    # Read the file if it exists; errors are logged.
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error("load_history failed: %s", e)

    # Missing or unreadable: start with empty history.
    return {"snapshots": []}


def get_historical_trends(drive_index: Optional[int] = None) -> Dict[str, Any]:
    """
    Get historical wear trends for a specific drive or all drives.

    Args:
        drive_index: Optional physical drive index to filter by

    Returns:
        Dict with historical trend data per drive.
    """
    # Load the history and prepare the empty result.
    history = load_history()

    trends = {
        "drives": {},
        "summary": {
            "total_snapshots": len(history.get("snapshots", []))
        }
    }

    # Nothing recorded yet: return the empty trends.
    snapshots = history.get("snapshots", [])

    if not snapshots:
        return trends

    # Group data points per drive index.
    drive_data: Dict[int, List[Dict]] = {}

    # Walk every drive in every snapshot.
    for snapshot in snapshots:
        for drive in snapshot.get("drives", []):
            # Skip drives without an index, or other drives when filtering to one.
            idx = drive.get("physical_index")
            if idx is None:
                continue

            if drive_index is not None and idx != drive_index:
                continue

            # Add this snapshot's wear and status to the drive's series.
            if idx not in drive_data:
                drive_data[idx] = []

            drive_data[idx].append({
                "timestamp": snapshot.get("timestamp"),
                "wear_level": drive.get("wear_level"),
                "health_status": drive.get("health_status")
            })

    # Summarise each drive's series.
    for idx, data_points in drive_data.items():
        if not data_points:
            continue

        # Wear readings that exist, oldest first.
        wear_levels = [p["wear_level"] for p in data_points if p["wear_level"] is not None]

        # Latest and earliest readings, and the change between them.
        current_wear = wear_levels[-1] if wear_levels else None
        first_wear = wear_levels[0] if wear_levels else None

        wear_change = None
        if current_wear is not None and first_wear is not None:
            wear_change = current_wear - first_wear

        # Store the summary with the full series.
        trends["drives"][str(idx)] = {
            "data_points": len(data_points),
            "current_wear": current_wear,
            "wear_change": wear_change,
            "trend": "increasing" if wear_change and wear_change > 0 else (
                "decreasing" if wear_change and wear_change < 0 else "stable"
            ),
            "history": data_points
        }

    # Trends per drive.
    return trends


async def get_drive_health_for_ui() -> Dict[str, Any]:
    """
    Get drive health data formatted for UI display.

    Returns:
        Dict ready for UI consumption with all drive health info.
    """
    # Run a full health check.
    health = await get_drive_health()

    # Build warning/critical alerts from each drive's wear level.
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

    # Wear trends from the stored history.
    trends = get_historical_trends()

    # Platform detection for the diagnostics block.
    is_hpe = is_hpe_server()
    has_raid = has_hpe_raid_controller() if is_hpe else False
    
    # Count drives by interface type.
    drives = health.get("drives", [])
    drive_counts = {"total": len(drives), "nvme": 0, "sata": 0, "sas": 0, "other": 0}
    # NVMe by device name, otherwise by the reported interface.
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

    # Overall controller layout: HPE array, mixed, direct-attached, or none.
    if has_raid and drive_counts["nvme"] > 0:
        controller_type = "mixed"
    elif has_raid:
        controller_type = "hpe_smartarray"
    elif len(drives) > 0:
        controller_type = "direct_attached"
    else:
        controller_type = "none"

    # Which tools are installed and where, plus the layout above.
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

    # Everything the UI needs in one response.
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
    # Run the check, save it to history, and return it.
    health = await get_drive_health()
    save_history(health)
    return health
