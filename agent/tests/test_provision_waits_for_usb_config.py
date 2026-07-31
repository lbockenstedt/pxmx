"""Provisioning must not run before usb_config arrives from the spoke.

Regression (server 02): the hub delivers usb_config AFTER the agent connects, so
every agent restart opens a window where _usb_cfg is empty. In that window
_t1_pci_vidpids is empty too, making the T1/T3 PCI pass a silent no-op while the
T2 USB pass still ran. The T2 VMs it created took the dongles plugged into the
controller cards, and a controller whose dongle a T2 VM holds can no longer be
PCI-passed — so T1 stayed blocked. Observed as "the first VMs were T2", the exact
inverse of the mandatory T1 -> T3 -> T2 order. Six self-update restarts in one day
hit that window repeatedly.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import usb_provision as up  # noqa: E402


class _Agent:
    def __init__(self, cfg):
        self.config = cfg


def test_usb_cfg_empty_when_config_has_not_arrived():
    # The condition the provision gate keys on.
    assert up._usb_cfg(_Agent({})) == {}
    assert up._usb_cfg(_Agent({"client_simulation": {}})) == {}
    assert up._usb_cfg(_Agent({"client_simulation": {"usb_config": {}}})) == {}


def test_usb_cfg_present_once_delivered():
    cfg = {"client_simulation": {"usb_config": {"t1_pci_vidpids": ["1912:0015"]}}}
    assert up._usb_cfg(_Agent(cfg)) == {"t1_pci_vidpids": ["1912:0015"]}


def test_t1_vidpids_empty_before_delivery_is_why_the_gate_exists():
    # Without the gate this empty set is what silently disabled the T1 pass while
    # the T2 pass carried on.
    assert up._t1_pci_vidpids(_Agent({})) == set()
    assert up._t3_pci_vidpids(_Agent({})) == set()
    cfg = {"client_simulation": {"usb_config": {"t1_pci_vidpids": ["1912:0015"]}}}
    assert up._t1_pci_vidpids(_Agent(cfg)) == {"1912:0015"}
