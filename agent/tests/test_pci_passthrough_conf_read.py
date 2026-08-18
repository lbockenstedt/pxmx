"""``pci_passthrough_vidpids`` reads the VM config from the pmxcfs FILE, not ``qm
config``.

Root cause of "Class column shows 0 T1 despite passed-through radios": the tier
sweep (``compute_vm_tiers``) probes every non-template VM, and each probe shelled
``qm config`` (spawns perl, ~0.5-2s). On a host with ~20+ VMs the sweep blew its
8s deadline every tick and fell back to USB-only classification, so raw-address
T1/T3 VMs (``hostpci0: 0000:03:00.0,pcie=1``) never classified — only the cheap
T2 (USB) VMs did. Reading ``/etc/pve/qemu-server/<vid>.conf`` directly is ~instant
so the sweep completes and T1/T3 classify.

These lock in:
  * the direct-file read + ``qm config`` fallback,
  * snapshot sections being ignored (a ``hostpciN`` in a ``[snap]`` block is not
    attached to the live VM),
  * hostpciN → lspci vidpid resolution still working through the new path.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_pt")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_pt"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_pt.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_pt.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("cs_guard", "cs_guard.py")
pve = _load("pve_cmds", "pve_cmds.py")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── _live_config_text: strip snapshot/pending sections ───────────────────────

def test_live_config_text_keeps_only_head_section():
    text = (
        "hostpci0: 0000:03:00.0,pcie=1\n"
        "name: t1-client\n"
        "[oldsnap]\n"
        "hostpci0: 0000:99:00.0,pcie=1\n"   # a snapshot's device — must be ignored
        "name: t1-client\n"
    )
    live = pve._live_config_text(text)
    assert "0000:03:00.0" in live
    assert "0000:99:00.0" not in live


def test_live_config_text_no_snapshots_is_identity():
    text = "hostpci0: 0000:04:00.0,pcie=1\nmemory: 4096\n"
    assert pve._live_config_text(text).strip() == text.strip()


# ── pci_passthrough_vidpids: raw address via the file path ───────────────────

def test_raw_hostpci_resolves_via_pci_path(tmp_path, monkeypatch):
    async def fake_conf(vid, kind):
        p = tmp_path / f"{vid}.conf"
        return pve._live_config_text(p.read_text()) if p.exists() else None
    (tmp_path / "90001.conf").write_text("hostpci0: 0000:03:00.0,pcie=1\nname: t1\n")
    monkeypatch.setattr(pve, "_vm_config_text", fake_conf)

    async def fake_lspci(addr):
        return {"0000:03:00.0": "1912:0015"}.get(addr)
    monkeypatch.setattr(pve, "_lspci_vidpid", fake_lspci)

    got = _run(pve.pci_passthrough_vidpids(90001, kind="qemu"))
    assert got == {"1912:0015"}


def test_vm_config_text_reads_file_then_strips_snapshots(tmp_path, monkeypatch):
    """The real _vm_config_text: read the qemu conf FILE (no qm subprocess) and
    return only the live section."""
    d = tmp_path / "qemu-server"
    d.mkdir()
    (d / "77.conf").write_text(
        "hostpci0: 0000:07:00.0,pcie=1\n[snap]\nhostpci0: 0000:aa:00.0\n")

    _orig_open = open

    def fake_open(path, *a, **k):
        if path == "/etc/pve/qemu-server/77.conf":
            return _orig_open(d / "77.conf", *a, **k)
        return _orig_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)

    async def boom(*a, **k):
        raise AssertionError("qm config must not be spawned when the file is readable")
    monkeypatch.setattr(pve, "_run", boom)

    text = _run(pve._vm_config_text(77, "qemu"))
    assert "0000:07:00.0" in text
    assert "0000:aa:00.0" not in text  # snapshot stripped


def test_vm_config_text_falls_back_to_cli_when_file_missing(monkeypatch):
    """No conf file (e.g. permission / non-clustered) → fall back to qm config."""
    async def fake_run(cmd, **k):
        assert cmd[:2] == ["qm", "config"]
        return 0, b"hostpci0: 0000:08:00.0,pcie=1\n", b""
    monkeypatch.setattr(pve, "_run", fake_run)
    # A vmid whose /etc/pve path won't exist in the test env.
    text = _run(pve._vm_config_text(999999, "qemu"))
    assert "0000:08:00.0" in text


def test_mapping_form_is_skipped(tmp_path, monkeypatch):
    async def fake_conf(vid, kind):
        return "hostpci0: mapping=t1radio,pcie=1\nname: t1\n"
    monkeypatch.setattr(pve, "_vm_config_text", fake_conf)

    async def fake_lspci(addr):
        raise AssertionError("mapping form must not hit lspci")
    monkeypatch.setattr(pve, "_lspci_vidpid", fake_lspci)

    got = _run(pve.pci_passthrough_vidpids(90002, kind="qemu"))
    assert got == set()


def test_lxc_short_circuits_empty(monkeypatch):
    async def fake_conf(vid, kind):
        raise AssertionError("LXC must not read a config")
    monkeypatch.setattr(pve, "_vm_config_text", fake_conf)
    got = _run(pve.pci_passthrough_vidpids(200, kind="lxc"))
    assert got == set()


def test_no_hostpci_returns_empty(monkeypatch):
    async def fake_conf(vid, kind):
        return "name: plain-vm\nmemory: 2048\n"
    monkeypatch.setattr(pve, "_vm_config_text", fake_conf)

    async def fake_lspci(addr):
        raise AssertionError("no hostpci → no lspci")
    monkeypatch.setattr(pve, "_lspci_vidpid", fake_lspci)

    got = _run(pve.pci_passthrough_vidpids(90003, kind="qemu"))
    assert got == set()


def test_snapshot_hostpci_ignored_end_to_end(monkeypatch):
    """A hostpci that exists ONLY in a snapshot section must not classify the VM."""
    async def fake_conf(vid, kind):
        # _vm_config_text strips snapshots; emulate the real reader.
        return pve._live_config_text(
            "name: t1\n[snap1]\nhostpci0: 0000:03:00.0,pcie=1\n")
    monkeypatch.setattr(pve, "_vm_config_text", fake_conf)

    async def fake_lspci(addr):
        return "1912:0015"
    monkeypatch.setattr(pve, "_lspci_vidpid", fake_lspci)

    got = _run(pve.pci_passthrough_vidpids(90004, kind="qemu"))
    assert got == set()


# ── _lspci_vidpid: resolve a full-domain addr from a single lspci -Dn scan ────

def test_addr_key_strips_domain():
    assert pve._addr_key("0000:03:00.0") == "03:00.0"
    assert pve._addr_key("03:00.0") == "03:00.0"
    assert pve._addr_key("0000:B0:00.0") == "b0:00.0"


def test_lspci_vidpid_resolves_full_domain_from_scan(monkeypatch):
    pve._lspci_vidpid_cache.clear()
    calls = {"n": 0}

    async def fake_run(cmd, **k):
        calls["n"] += 1
        assert cmd == ["lspci", "-Dn"]  # ONE full-scan, not per-address -s
        return 0, (b"0000:03:00.0 0280: 1912:0015 (rev 01)\n"
                   b"0000:b0:00.0 0280: 1912:0015\n"
                   b"0000:00:1f.0 0600: 8086:a082\n"), b""
    monkeypatch.setattr(pve, "_run", fake_run)

    # Full-domain address (as it appears on a hostpciN line) resolves.
    assert _run(pve._lspci_vidpid("0000:03:00.0")) == "1912:0015"
    # A second lookup hits the memoized map — no extra lspci scan.
    assert _run(pve._lspci_vidpid("0000:b0:00.0")) == "1912:0015"
    assert calls["n"] == 1
    # An unknown address resolves to None.
    assert _run(pve._lspci_vidpid("0000:99:00.0")) is None
