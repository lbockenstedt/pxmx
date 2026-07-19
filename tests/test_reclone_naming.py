"""Reclone VM naming — the deterministic-username regression.

The reclone path (``_reclone_vm_core`` / ``_reclone_all``) used to name the new
VM from the EXISTING Proxmox VM name (``v.get("name")``), so a VM named
``sim-<vmid>`` recloned to ``sim-<vmid>`` and the guest hostname came up as
``sim-9xxxx`` — losing the username programmed into ``vm_names.json`` (the same
map the initial clone ``_clone_and_provision`` uses). The fix resolves the name
from the map first, via ``_reclone_vm_name``. These tests pin the precedence.

Self-contained: puts ``pxmx/agent`` on sys.path and imports the ``src`` package
the agent uses itself. Runs on Python 3.9.
"""
import os
import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(_AGENT))

from src.cs_sim import _reclone_vm_name  # noqa: E402
from src.usb_provision import _vm_name  # noqa: E402


def test_reclone_name_uses_username_map_over_stale_sim_default():
    """The regression: a VM carrying the stale Proxmox default ``sim-<vmid>``
    reclones to the mapped username, not ``sim-<vmid>``."""
    assert _vm_name(90001) == "khenderson"          # the map is present
    assert _reclone_vm_name(90001, "sim-90001") == "khenderson"


def test_reclone_name_map_wins_over_custom_rename():
    """The map is the deterministic source-of-truth across re-clones (the
    documented intent), so it wins over an ad-hoc Proxmox rename too — a
    reclone destroys + re-clones from template, so a custom name is ephemeral
    anyway and the deterministic name is what the initial clone would produce."""
    assert _reclone_vm_name(90002, "some-custom-name") == "cdean"


def test_reclone_name_falls_back_to_existing_when_vmid_not_in_map():
    """A vmid outside the mapped range (e.g. the 90000 floor) keeps the existing
    Proxmox name rather than collapsing to sim-<vmid>."""
    assert _vm_name(90000) is None                   # 90000 is outside the map
    assert _reclone_vm_name(90000, "sim-90000") == "sim-90000"
    assert _reclone_vm_name(90000, "kept-custom") == "kept-custom"


def test_reclone_name_final_fallback_is_sim_vmid():
    """No map entry AND no existing name → sim-<vmid> (never an empty name)."""
    assert _reclone_vm_name(90000, "") == "sim-90000"


def test_reclone_name_map_covers_full_sim_range():
    """The map covers 90001-100000 (the sim VMID range), so every real sim VM
    resolves to a username — the regression is closed for the whole fleet."""
    assert _reclone_vm_name(100000, "sim-100000") == "dsloan"
    assert _vm_name(99999) is not None