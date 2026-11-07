"""Best-effort hardware identity of the machine an agent runs on.

The chassis serial is what links a host OS to its management controller
(iLO/iDRAC) in the hub, so the agent reports it with its heartbeat. Read from
SMBIOS/DMI (``/sys/class/dmi/id``), falling back to ``dmidecode`` when running
as root. Placeholder, virtual-machine and implausible values are dropped.
"""
import os
import re
import subprocess
import time

_DMI = "/sys/class/dmi/id"
_JUNK = {
    "NOTSPECIFIED", "TOBEFILLEDBYOEM", "TOBEFILLEDBYO.E.M.", "NONE", "DEFAULTSTRING",
    "UNKNOWN", "NA", "N/A", "NOTAPPLICABLE", "NULL", "EMPTY", "SYSTEMSERIALNUMBER",
    "SERIALNUMBER", "CHASSISSERIALNUMBER", "FILLEDBYOEM", "OEM", "SERIAL",
    "1234567890", "0123456789", "123456789", "1234567",
}
_VM_PREFIXES = ("VMWARE", "VIRTUALBOX", "QEMU", "KVM", "XEN", "PARALLELS", "AMAZONEC2", "EC2")
_RETRY_S = 600.0
_cache = {"at": 0.0, "val": None}


def sanitize_serial(value):
    """Upper-cased serial, or "" for empty/placeholder/too-short/VM values."""
    s = str(value or "").strip().strip("\x00").strip()
    if not s:
        return ""
    up = s.upper()
    squashed = re.sub(r"[\s_\-]+", "", up)
    if squashed in _JUNK or up in _JUNK:
        return ""
    if len(squashed) < 5 or len(set(squashed)) == 1:
        return ""
    if up.startswith(_VM_PREFIXES):
        return ""
    return up


def _read(name):
    try:
        with open(os.path.join(_DMI, name)) as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001 - absent or root-only
        return ""


def _dmidecode(keyword):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return ""
    try:
        out = subprocess.run(["dmidecode", "-s", keyword], capture_output=True, text=True,
                             timeout=5).stdout
        return out.strip().splitlines()[0].strip() if out.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


def collect_hw_identity():
    """``{hw_serial, hw_manufacturer, hw_product}`` (only the non-empty keys).
    A found serial is cached for the process; an empty result is retried every
    10 minutes."""
    now = time.monotonic()
    if _cache["val"] is not None and (_cache["val"] or now - _cache["at"] < _RETRY_S):
        return dict(_cache["val"])
    serial = sanitize_serial(_read("product_serial"))
    if not serial:
        serial = sanitize_serial(_dmidecode("system-serial-number"))
    out = {}
    if serial:
        out["hw_serial"] = serial
        for key, dmi, dec in (("hw_manufacturer", "sys_vendor", "system-manufacturer"),
                              ("hw_product", "product_name", "system-product-name")):
            v = _read(dmi) or _dmidecode(dec)
            if v:
                out[key] = v[:80]
    _cache.update(at=now, val=out)
    return dict(out)
