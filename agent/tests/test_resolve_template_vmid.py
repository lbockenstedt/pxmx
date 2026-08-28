"""``_resolve_template_vmid`` accepts EITHER a vmid (numeric) OR a template NAME
(text). A name resolves to the vmid whose ``qm list`` NAME matches exactly on
this host; multiple/no matches log an error and return None (no silent fallback
to a random template). The numeric path keeps its existing
fallback-to-lowest-template behavior when the vmid doesn't exist.

Name lookup is scoped to the LOCAL node: on a cluster every node carries its
own same-named template at a different vmid, and a clone runs locally, so only
this node's copy is a valid source.

No real ``qm``: a fake ``pve_cmds`` module (with ``qm_config`` /
``list_qemu_vms_cluster`` / ``list_qemu_vmids`` / ``local_node_name`` stubs)
is injected into the synthetic
package namespace so ``_resolve_template_vmid``'s ``from . import pve_cmds``
picks up the stubs directly. Mirrors the synthetic-package pattern in
``test_template_backup_storage.py``.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

# Synthetic package so usb_provision's ``from . import pve_cmds`` (and the
# usb_state_store / usb_quarantine / usb_resource_gate top-level relatives)
# resolve under one namespace.
_pkg = types.ModuleType("pxmx_agent_src_rt")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_rt"] = _pkg


def _load(modname, fname):
    # NOTE: no submodule_search_locations — passing it marks the module a
    # package (__package__ == its own name), which makes ``from . import
    # pve_cmds`` inside _resolve resolve to ``usb_provision.pve_cmds`` (a fresh
    # real import that shells out to `qm`) instead of the fake we registered.
    # Leaf modules get __package__ = the parent package, so relative imports
    # land in pxmx_agent_src_rt.* (whose __path__ is set on _pkg above).
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_rt.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_rt.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


# usb_provision imports these siblings at module load time (stdlib-only).
for _m, _f in (("usb_state_store", "usb_state_store.py"),
               ("usb_quarantine", "usb_quarantine.py"),
               ("usb_resource_gate", "usb_resource_gate.py")):
    _load(_m, _f)

# A FAKE pve_cmds — registered in sys.modules AND as a package attribute so
# ``from . import pve_cmds`` inside _resolve_template_vmid / _is_runnable_template
# resolves to THIS object (not a separately-imported real pve_cmds that would
# shell out to `qm`). Per-test code swaps the async stubs on this namespace.
_fake_pve = SimpleNamespace()
sys.modules["pxmx_agent_src_rt.pve_cmds"] = _fake_pve
_pkg.pve_cmds = _fake_pve

_usb_provision = _load("usb_provision", "usb_provision.py")
_resolve = _usb_provision._resolve_template_vmid


def _install(qm_config, list_qemu_vms_cluster, list_qemu_vmids, node="pve1"):
    """Point the fake pve_cmds at per-test async stubs."""
    _fake_pve.qm_config = qm_config
    _fake_pve.list_qemu_vms_cluster = list_qemu_vms_cluster
    _fake_pve.list_qemu_vmids = list_qemu_vmids

    async def _local_node_name():
        return node

    _fake_pve.local_node_name = _local_node_name


# ── stub builders ────────────────────────────────────────────────────────────

async def _qm_config_existing(_vid):
    return {"name": "foo"}  # non-empty → vmid "exists"


async def _qm_config_missing(_vid):
    return {}  # qm_config returns {} for a nonexistent vmid


async def _list_vms_dupname():
    # (vmid, name, node); 100 and 300 share the name "debian-12" on pve1.
    # 400 is a same-named template owned by ANOTHER node — never a valid source.
    return [(100, "debian-12", "pve1"), (200, "win11", "pve1"),
            (300, "debian-12", "pve1"), (400, "rocky-9", "pve2")]


async def _list_vmids_dupname():
    return [100, 200, 300]


# ── numeric path ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_numeric_existing_vmid_returned():
    _install(_qm_config_existing, _list_vms_dupname, _list_vmids_dupname)
    assert await _resolve(100) == 100
    assert await _resolve("100") == 100  # string-numeric works too


@pytest.mark.asyncio
async def test_numeric_missing_falls_back_to_lowest_template():
    # vmid 999 doesn't exist; _is_runnable_template treats qm_config["template"]
    # == "1" as a runnable template. Make 100 the lowest such template.
    async def qm_config(vmid):
        if int(vmid) == 100:
            return {"name": "t", "template": "1"}
        if int(vmid) == 200:
            return {"name": "other"}  # not a template
        return {}  # 999 (and anything else) → missing

    async def list_vmids():
        return [100, 200]

    _install(qm_config, _list_vms_dupname, list_vmids)
    assert await _resolve(999) == 100


# ── name path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_name_unique_resolves_to_vmid():
    _install(_qm_config_missing, _list_vms_dupname, _list_vmids_dupname)
    # "win11" matches only vmid 200.
    assert await _resolve("win11") == 200


@pytest.mark.asyncio
async def test_name_not_found_returns_none_no_fallback():
    # A name typo must NOT fall back to a random template (only the numeric path
    # falls back). Returns None; no clone.
    _install(_qm_config_missing, _list_vms_dupname, _list_vmids_dupname)
    assert await _resolve("nope-not-a-vm") is None


@pytest.mark.asyncio
async def test_name_multiple_matches_on_this_node_returns_none():
    # "debian-12" matches vmid 100 AND 300 on pve1 → ambiguous → refuse.
    _install(_qm_config_missing, _list_vms_dupname, _list_vmids_dupname)
    assert await _resolve("debian-12") is None


@pytest.mark.asyncio
async def test_name_only_on_another_node_returns_none():
    # "rocky-9" exists, but only on pve2. A clone runs locally, so cloning from
    # another node's vmid would clone the wrong (or no) image → refuse.
    _install(_qm_config_missing, _list_vms_dupname, _list_vmids_dupname)
    assert await _resolve("rocky-9") is None


@pytest.mark.asyncio
async def test_same_name_on_two_nodes_resolves_to_the_local_one():
    # Both nodes carry "debian-12" at different vmids; only the local copy wins.
    async def list_cluster():
        return [(100, "debian-12", "pve1"), (900, "debian-12", "pve2")]

    _install(_qm_config_missing, list_cluster, _list_vmids_dupname, node="pve2")
    assert await _resolve("debian-12") == 900


# ── empty / None ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_configured_returns_none():
    _install(_qm_config_missing, _list_vms_dupname, _list_vmids_dupname)
    assert await _resolve(None) is None
    assert await _resolve("") is None
    assert await _resolve("   ") is None