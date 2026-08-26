"""Tests for the "Back up to Hub" storage-target + guaranteed-cleanup behavior.

Covers two changes (see lm plan validated-launching-chipmunk.md):
1. ``pve_cmds.list_backup_storages`` now returns a ``storage_types`` name→type
   map (parallel to the existing ``storages`` name list) so the WebUI can filter
   non-file storages (PBS excluded — vzdump-to-PBS pushes dedup chunks, not a
   single streamable ``.vma.zst``). Existing name-only consumers are unaffected.
2. ``ProxmoxAgent._do_template_backup`` honors an optional ``data['storage']``:
   when set it runs ``vzdump --storage <X>`` (not ``--dumpdir``) and ALWAYS
   deletes the produced archive after streaming — on success AND on upload
   failure — so the dump never lingers on the admin-chosen storage. Without
   ``storage`` it keeps the legacy tempdir + ``rmtree`` path.

No network / no real pvesm / no real vzdump: ``subprocess.run``, ``httpx``,
``os.remove`` and ``pve_cmds._run`` are stubbed.
"""
import os
import sys
import types
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import subprocess as _subprocess
import shutil as _shutil
import tempfile as _tempfile
import glob as _glob
import os as _os

import pytest

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

# Synthetic package so agent.py / pve_cmds.py relative imports resolve
# (mirrors test_agent_tls_context.py). Load once at import time.
_pkg = types.ModuleType("pxmx_agent_src_tb")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_tb"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_tb.{modname}", SRC / fname,
        submodule_search_locations=[str(SRC)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_tb.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_pve_cmds = _load("pve_cmds", "pve_cmds.py")
_agent_mod = _load("agent", "agent.py")
_template_ops = _load("template_ops", "template_ops.py")
ProxmoxAgent = _agent_mod.ProxmoxAgent


# ── list_backup_storages: storage_types map ──────────────────────────────────

@pytest.mark.asyncio
async def test_list_backup_storages_returns_types_and_names(monkeypatch):
    sample = (
        "Name           Type Status      Total     Used   Avail\n"
        "local          dir  active   1048576   102400  946176\n"
        "nfs-backup     nfs  active   2097152   204800 1892352\n"
        "pbs            pbs  active   4194304   409600 3784704\n"
    )

    async def fake_run(argv, *, check=True, timeout=60):
        return 0, sample.encode(), b""

    monkeypatch.setattr(_pve_cmds, "_run", fake_run)

    out = await _pve_cmds.list_backup_storages()
    host = out["hosts"][0]
    assert host["storages"] == ["local", "nfs-backup", "pbs"]          # names preserved
    assert host["storage_types"] == {"local": "dir", "nfs-backup": "nfs", "pbs": "pbs"}
    # The WebUI filters PBS via storage_types[name] !== 'pbs'
    file_based = [s for s in host["storages"] if host["storage_types"].get(s) != "pbs"]
    assert file_based == ["local", "nfs-backup"]


@pytest.mark.asyncio
async def test_list_backup_storages_empty_when_pvesm_fails(monkeypatch):
    async def fake_run(argv, *, check=True, timeout=60):
        return 1, b"", b"oops"

    monkeypatch.setattr(_pve_cmds, "_run", fake_run)
    out = await _pve_cmds.list_backup_storages()
    assert out["hosts"][0]["storages"] == []
    assert out["hosts"][0]["storage_types"] == {}


# ── _do_template_backup: storage mode + always-delete ────────────────────────

VOLID = "local:dump/vzdump-qemu-90025-2026-07-13_00_00_00.vma.zst"
ARCHIVE = "/var/lib/vz/dump/vzdump-qemu-90025-2026-07-13_00_00_00.vma.zst"
PVESM_STATUS = (
    "Name Type Status Total Used Avail\n"
    "local dir active 100 10 90\n"
    "pbs pbs active 200 20 180\n")


class _FakeResp:
    def __init__(self, status, text="ok"):
        self.status_code = status
        self.text = text


class _FakeHttpxModule:
    """Stands in for httpx so the function's local ``import httpx`` is stubbed."""
    class Client:
        put_status = 200

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, url, content=None, headers=None):
            return _FakeResp(type(self).put_status)

        def post(self, url, headers=None, json=None):
            return _FakeResp(200)  # progress report — best-effort, ignored


def _install_fake_httpx(monkeypatch, put_status):
    fake = types.ModuleType("httpx")
    fake.Client = _FakeHttpxModule.Client
    # _do_template_backup does a local `import httpx`; make it resolve to ours.
    monkeypatch.setitem(sys.modules, "httpx", fake)
    _FakeHttpxModule.Client.put_status = put_status


def _make_subprocess(monkeypatch, removed, rmtree_calls, list_calls):
    """Fake subprocess.run dispatching pvesm/vzdump; records the vzdump argv."""
    vzdump_calls = []

    def fake_run(argv, **kwargs):
        vzdump_calls.append(list(argv))
        if argv[0] == "pvesm":
            sub = argv[1]
            if sub == "status":
                return SimpleNamespace(returncode=0, stdout=PVESM_STATUS, stderr="")
            if sub == "list":
                list_calls[0] += 1
                if list_calls[0] == 1:  # pre-snapshot
                    return SimpleNamespace(returncode=0, stdout="Volid Owner\n", stderr="")
                # post-snapshot: the new volid is present
                return SimpleNamespace(returncode=0, stdout=f"Volid Owner\n{VOLID} root@pam\n", stderr="")
            if sub == "path":
                return SimpleNamespace(returncode=0, stdout=ARCHIVE + "\n", stderr="")
            if sub == "free":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[0].endswith("vzdump"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", fake_run)
    return vzdump_calls


def _run_backup(data):
    _template_ops.do_template_backup(data)


def test_storage_mode_uses_storage_flag_and_deletes_on_success(monkeypatch):
    removed, rmtree_calls, list_calls = [], [], [0]
    _install_fake_httpx(monkeypatch, put_status=200)
    vzdump_calls = _make_subprocess(monkeypatch, removed, rmtree_calls, list_calls)
    monkeypatch.setattr(_shutil, "which", lambda b: "/usr/bin/vzdump")
    monkeypatch.setattr(_shutil, "rmtree", lambda d, **k: rmtree_calls.append(d))
    monkeypatch.setattr(_os.path, "isfile", lambda p: True)
    monkeypatch.setattr(_os.path, "getsize", lambda p: 1234)
    monkeypatch.setattr(_os, "remove", lambda p: removed.append(p))

    _run_backup({"vmid": 90025, "storage": "local",
                 "upload_url": "https://hub/api/templates/t1/upload",
                 "upload_token": "tok"})

    # vzdump uses --storage local, NOT --dumpdir
    assert any("--storage" in a and "local" in a for a in vzdump_calls), vzdump_calls
    assert not any("--dumpdir" in a for a in vzdump_calls), vzdump_calls
    # archive deleted after streaming
    assert removed == [ARCHIVE], removed
    # tempdir mode NOT used → no rmtree
    assert rmtree_calls == [], rmtree_calls


def test_storage_mode_deletes_on_upload_failure(monkeypatch):
    """The user's space concern: archive is removed even when the hub rejects it."""
    removed, rmtree_calls, list_calls = [], [], [0]
    _install_fake_httpx(monkeypatch, put_status=400)
    _make_subprocess(monkeypatch, removed, rmtree_calls, list_calls)
    monkeypatch.setattr(_shutil, "which", lambda b: "/usr/bin/vzdump")
    monkeypatch.setattr(_shutil, "rmtree", lambda d, **k: rmtree_calls.append(d))
    monkeypatch.setattr(_os.path, "isfile", lambda p: True)
    monkeypatch.setattr(_os.path, "getsize", lambda p: 1234)
    monkeypatch.setattr(_os, "remove", lambda p: removed.append(p))

    _run_backup({"vmid": 90025, "storage": "local",
                 "upload_url": "https://hub/api/templates/t1/upload",
                 "upload_token": "tok"})

    assert removed == [ARCHIVE], removed   # cleaned up despite the 400


def test_storage_mode_rejects_unknown_storage_before_vzdump(monkeypatch):
    removed, rmtree_calls, list_calls = [], [], [0]
    _install_fake_httpx(monkeypatch, put_status=200)
    vzdump_calls = _make_subprocess(monkeypatch, removed, rmtree_calls, list_calls)
    monkeypatch.setattr(_shutil, "which", lambda b: "/usr/bin/vzdump")
    monkeypatch.setattr(_shutil, "rmtree", lambda d, **k: rmtree_calls.append(d))
    monkeypatch.setattr(_os.path, "isfile", lambda p: True)
    monkeypatch.setattr(_os.path, "getsize", lambda p: 1234)
    monkeypatch.setattr(_os, "remove", lambda p: removed.append(p))

    _run_backup({"vmid": 90025, "storage": "nope-not-here",
                 "upload_url": "https://hub/api/templates/t1/upload",
                 "upload_token": "tok"})

    # vzdump never ran (only the pvesm status validation call), nothing to delete
    assert not any(any(x.endswith("vzdump") for x in a) for a in vzdump_calls), vzdump_calls
    assert removed == [], removed


def test_tempdir_mode_back_compat(monkeypatch):
    """Older hub (no storage in payload) → scratch-dir + rmtree path."""
    removed, rmtree_calls, list_calls = [], [], [0]
    _install_fake_httpx(monkeypatch, put_status=200)

    scratch_parent = "/var/lib/pxmx-template-scratch-test"
    tmpdir = os.path.join(scratch_parent, "lm-tmpl-backup-XXXX")
    archive_in_tmp = os.path.join(tmpdir, "vzdump-qemu-90025-...vma.zst")

    vzdump_calls = []

    def fake_run(argv, **kwargs):
        vzdump_calls.append(list(argv))
        if argv[0].endswith("vzdump"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(_shutil, "which", lambda b: "/usr/bin/vzdump")
    monkeypatch.setattr(_shutil, "rmtree", lambda d, **k: rmtree_calls.append(d))
    monkeypatch.setenv("LM_PXMX_TEMPLATE_MIN_FREE_BYTES", "0")
    monkeypatch.setattr(_os, "makedirs", lambda *a, **k: None)
    mkdtemp_calls = []
    monkeypatch.setattr(_tempfile, "mkdtemp",
                        lambda **k: mkdtemp_calls.append(k) or tmpdir)
    monkeypatch.setattr(_glob, "glob", lambda pat: [archive_in_tmp])
    monkeypatch.setattr(_os.path, "isfile", lambda p: True)
    monkeypatch.setattr(_os.path, "getsize", lambda p: 1234)
    monkeypatch.setattr(_os, "remove", lambda p: removed.append(p))

    _run_backup({"vmid": 90025,
                 "upload_url": "https://hub/api/templates/t1/upload",
                 "upload_token": "tok",
                 "scratch_dir": scratch_parent})

    assert any("--dumpdir" in a for a in vzdump_calls), vzdump_calls
    assert mkdtemp_calls == [{"prefix": "lm-tmpl-backup-", "dir": scratch_parent}]
    assert not any("--storage" in a for a in vzdump_calls), vzdump_calls
    assert rmtree_calls == [tmpdir], rmtree_calls      # tempdir cleaned
    assert removed == [], removed                       # no explicit os.remove (rmtree handles it)

# ── _proxmox_vzdump_name: qmrestore-parseable archive naming ─────────────────
# Regression: the refresh downloaded the archive under a neutral name
# (image/template.vma.zst) that does NOT match Proxmox's vzdump naming, so
# qmrestore died "couldn't determine archive info from '...'" — the archive was
# valid but never restored (and thus never re-marked a template). The agent now
# renames it to vzdump-qemu-<vmid>-<ts>.<ext> with the compression sniffed from
# the file's magic bytes.
import re as _re


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_vzdump_name_zstd_magic(tmp_path):
    path = _write(tmp_path, "download.tmp", b"\x28\xb5\x2f\xfd" + b"\x00" * 32)
    name = _template_ops._proxmox_vzdump_name(100, path, "image.vma.zst")
    assert _re.match(r"^vzdump-qemu-100-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}\.vma\.zst$", name), name


def test_vzdump_name_gzip_magic(tmp_path):
    path = _write(tmp_path, "download.tmp", b"\x1f\x8b" + b"\x00" * 32)
    name = _template_ops._proxmox_vzdump_name(200, path, "")
    assert name.endswith(".vma.gz")
    assert name.startswith("vzdump-qemu-200-")


def test_vzdump_name_uncompressed_vma(tmp_path):
    path = _write(tmp_path, "download.tmp", b"VMA\x00" + b"\x00" * 32)
    name = _template_ops._proxmox_vzdump_name(300, path, "image.vma")
    # Uncompressed VMA → no compression suffix.
    assert name.endswith("-300-" + name.split("-300-")[1]) and name.endswith(".vma")
    assert ".vma.zst" not in name


def test_vzdump_name_falls_back_to_hint_then_zstd(tmp_path):
    # Unrecognized magic → use the hub filename hint's extension.
    path = _write(tmp_path, "download.tmp", b"\x00\x01\x02\x03" + b"\x00" * 8)
    assert _template_ops._proxmox_vzdump_name(1, path, "image.vma.lzo").endswith(".vma.lzo")
    # No hint either → default zstd (the backup flow always uses --compress zstd).
    assert _template_ops._proxmox_vzdump_name(1, path, "").endswith(".vma.zst")
