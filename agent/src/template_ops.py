"""Template backup + refresh for the unified pxmx agent.

Free-function extraction of ``ProxmoxAgent``'s hub-triggered template backup
(vzdump → stream to the hub template repo) and the destructive template refresh
(wipe this host's sim VMs + template → download the backup → qmrestore → resume
auto-prov). Functions take the ``agent`` instance as their first argument where
they need it (the cs_commands/usb_provision pattern). ``ProxmoxAgent`` keeps thin
wrapper methods for the two dispatch-facing kick-off calls so the dispatch chain
is untouched.
"""

import asyncio
import os
from typing import Any, Dict

import logging

logger = logging.getLogger("PxmxAgent")

DEFAULT_TEMPLATE_SCRATCH_MIN_FREE = 5 * 1024 * 1024 * 1024


def _template_scratch_parent(data: Dict[str, Any]) -> str:
    """Large, persistent filesystem for template archives when no Proxmox
    storage was explicitly selected. Never falls back to the OS temp dir."""
    configured = str(data.get("scratch_dir") or
                     os.environ.get("LM_PXMX_TEMPLATE_SCRATCH_DIR") or "").strip()
    if configured:
        return configured
    for candidate in ("/var/lib/vz/dump", "/var/lib/vz",
                      "/var/lib/pxmx/template-scratch"):
        if os.path.isdir(candidate):
            return candidate
    return "/var/lib/pxmx/template-scratch"


def _make_template_scratch_dir(prefix: str, data: Dict[str, Any]) -> str:
    import shutil
    import tempfile
    parent = _template_scratch_parent(data)
    os.makedirs(parent, exist_ok=True)
    min_free = int(os.environ.get("LM_PXMX_TEMPLATE_MIN_FREE_BYTES",
                                  str(DEFAULT_TEMPLATE_SCRATCH_MIN_FREE)) or 0)
    if min_free > 0:
        free = shutil.disk_usage(parent).free
        if free < min_free:
            raise RuntimeError(
                f"scratch dir {parent} has {free} bytes free; need at least {min_free}")
    return tempfile.mkdtemp(prefix=prefix, dir=parent)


def start_template_backup(data: Dict[str, Any]) -> Dict[str, Any]:
    """Kick off a hub-triggered template backup and ACK immediately.

    vzdump + the upload of a multi-GB archive take minutes, so we spawn the
    work in a background thread and return ACCEPTED right away (the hub's
    START_BACKUP request_response must not block that long). Progress + the
    terminal result flow back over the token'd ``/progress`` + ``/upload``
    endpoints on the hub (routes/templates.py)."""
    vmid = data.get("vmid")
    upload_url = str(data.get("upload_url") or "")
    token = str(data.get("upload_token") or "")
    if vmid is None or not upload_url or not token:
        return {"status": "ERROR",
                "message": "START_BACKUP requires vmid, upload_url and upload_token"}
    asyncio.create_task(asyncio.to_thread(do_template_backup, dict(data)))
    return {"status": "ACCEPTED",
            "message": f"vzdump of VM {vmid} started; streaming to the hub template repo"}


def do_template_backup(data: Dict[str, Any]) -> None:
    """Blocking backup worker (runs in a thread): vzdump the VM, then stream
    the archive to the hub. Reports progress/failure via the token'd progress
    endpoint; the hub finalizes size+sha256 on the upload.

    If ``data['storage']`` names a backup-capable Proxmox storage, vzdump
    targets it (``--storage``) so the dump lands on the admin-chosen storage
    (NFS/ZFS/dir/...) instead of the node's root-disk temp dir — and the
    produced archive is ALWAYS deleted after streaming (success OR failure)
    so it doesn't consume storage space. Without ``storage`` (back-compat with
    an older hub) it falls back to a local temp dir whose cleanup is rmtree.
    """
    import glob
    import shutil
    import subprocess
    import tempfile
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        logger.warning("START_BACKUP: httpx not available: %s", exc)
        return

    vmid = data.get("vmid")
    storage = str(data.get("storage") or "").strip()
    upload_url = str(data.get("upload_url") or "")
    token = str(data.get("upload_token") or "")
    progress_url = upload_url.rsplit("/upload", 1)[0] + "/progress"
    headers = {"x-upload-token": token}

    def _report(status: str, progress=None, error: str = "") -> None:
        body = {"status": status}
        if progress is not None:
            body["progress"] = progress
        if error:
            body["error"] = error
        try:
            with httpx.Client(verify=False, timeout=15) as c:
                c.post(progress_url, headers=headers, json=body)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def _pvesm(args):
        """Synchronous pvesm helper (we run in a thread, not an event loop).
        Returns (rc, stdout). Best-effort: (1, '') on failure."""
        try:
            proc = subprocess.run(["pvesm", *args], capture_output=True,
                                  text=True, timeout=90)
            return proc.returncode, (proc.stdout or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("START_BACKUP: pvesm %s failed: %s", args, exc)
            return 1, ""

    def _volids(txt: str):
        # pvesm list prints a header (no ':') + one volid per line
        # (``<storage>:dump/vzdump-...vma.zst``). The ':' filter drops header.
        return set(ln.split()[0] for ln in txt.splitlines() if ":" in ln)

    tmpdir = None
    archive_path = None       # filesystem path of the archive we stream
    archive_volid = None       # volid (for pvesm-free fallback) when --storage
    try:
        _report("dumping", 0)
        vzdump = shutil.which("vzdump") or "/usr/bin/vzdump"
        if storage:
            # Validate the storage exists + is backup-capable (mirror
            # pve_cmds.vm_action_any's fast-fail) so an obvious misconfig
            # fails before we touch vzdump. Then snapshot the current backup
            # volids for this VM so we can diff out the new one after vzdump.
            rc, sout = _pvesm(["status", "--content", "backup"])
            names = [ln.split()[0] for ln in sout.splitlines()[1:]
                     if ln.split()]
            if storage not in names:
                _report("failed", error=(
                    f"storage '{storage}' not found / not backup-capable "
                    f"on this host"))
                return
            _rc, pre = _pvesm(["list", storage, "--content", "backup",
                               "--vmid", str(vmid)])
            pre_set = _volids(pre)
            # --mode stop is safe for a non-running template; --storage lands
            # the dump on the admin-chosen storage. zstd = Proxmox default.
            proc = subprocess.run(
                [vzdump, str(vmid), "--compress", "zstd", "--mode", "stop",
                 "--storage", storage],
                capture_output=True, text=True)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or f"vzdump exited {proc.returncode}").strip()
                logger.warning("START_BACKUP: vzdump failed for %s: %s", vmid, err[:200])
                _report("failed", error=f"vzdump failed: {err[:300]}")
                return
            _rc, post = _pvesm(["list", storage, "--content", "backup",
                                "--vmid", str(vmid)])
            new_vols = list(_volids(post) - pre_set)
            if not new_vols:
                _report("failed", error="vzdump produced no archive on storage")
                return
            archive_volid = sorted(new_vols)[-1]
            _rc, pout = _pvesm(["path", archive_volid])
            archive_path = pout.strip().splitlines()[0].strip() if pout.strip() else ""
            if not archive_path or not os.path.isfile(archive_path):
                _report("failed", error=(
                    f"could not resolve archive path for {archive_volid}"))
                return
            path = archive_path
        else:
            # Back-compat (older hub, no storage in payload): use a large
            # Proxmox/template scratch path, never the OS temp dir on root.
            tmpdir = _make_template_scratch_dir("lm-tmpl-backup-", data)
            proc = subprocess.run(
                [vzdump, str(vmid), "--compress", "zstd", "--mode", "stop",
                 "--dumpdir", tmpdir],
                capture_output=True, text=True)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or f"vzdump exited {proc.returncode}").strip()
                logger.warning("START_BACKUP: vzdump failed for %s: %s", vmid, err[:200])
                _report("failed", error=f"vzdump failed: {err[:300]}")
                return
            archives = glob.glob(os.path.join(tmpdir, "vzdump-*.vma.zst")) \
                or [p for p in glob.glob(os.path.join(tmpdir, "vzdump-*")) if os.path.isfile(p)]
            if not archives:
                _report("failed", error="vzdump produced no archive")
                return
            path = max(archives, key=os.path.getsize)
        size = os.path.getsize(path)
        logger.info("START_BACKUP: vzdump of %s done (%d bytes) — uploading", vmid, size)
        _report("uploading", 0)

        def _chunks():
            with open(path, "rb") as fh:
                while True:
                    b = fh.read(4 * 1024 * 1024)
                    if not b:
                        break
                    yield b

        # Content-Length lets the hub enforce its size cap + disk-space guard
        # and compute upload progress. verify=False: the hub may still be on
        # a self-signed cert (pre-LE), same as the spoke/agent WS legs.
        with httpx.Client(verify=False, timeout=None) as c:
            resp = c.put(upload_url, content=_chunks(),
                         headers={**headers, "content-length": str(size),
                                  "content-type": "application/octet-stream"})
        if resp.status_code == 200:
            logger.info("START_BACKUP: upload complete for %s", vmid)
        else:
            msg = f"hub upload rejected: HTTP {resp.status_code} {resp.text[:200]}"
            logger.warning("START_BACKUP: %s", msg)
            _report("failed", error=msg)
    except Exception as e:  # noqa: BLE001
        logger.warning("START_BACKUP: backup of %s failed: %s", vmid, e)
        _report("failed", error=str(e)[:300])
    finally:
        # ALWAYS clean up the on-node archive (the user's space concern).
        # --storage mode writes outside any tempdir, so rmtree wouldn't reach
        # it — delete the resolved file best-effort, with a pvesm free
        # fallback. Tempdir mode is handled by rmtree below. A cleanup error
        # must never mask the real upload result, so it's logged + swallowed.
        if archive_path:
            try:
                if os.path.isfile(archive_path):
                    os.remove(archive_path)
                    logger.info("START_BACKUP: deleted archive %s", archive_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("START_BACKUP: os.remove failed for %s: %s — "
                               "trying pvesm free", archive_path, exc)
                if archive_volid:
                    _pvesm(["free", archive_volid])
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── template refresh (hub-triggered, destructive) ────────────────────────
def start_template_refresh(agent, data: Dict[str, Any]) -> Dict[str, Any]:
    """Kick off a REFRESH_TEMPLATE and ACK immediately. The destructive
    sequence (pause auto-prov → wipe the host's sim VMs + template → download
    the backup → qmrestore to the original VMID + re-mark template → resume
    auto-prov) runs as a background task and reports via /refresh-progress."""
    tid = data.get("template_id")
    vmid = data.get("template_vmid")
    url = str(data.get("download_url") or "")
    token = str(data.get("refresh_token") or "")
    if not tid or vmid is None or not url or not token:
        return {"status": "ERROR",
                "message": "REFRESH_TEMPLATE requires template_id, template_vmid, download_url, refresh_token"}
    logger.info("REFRESH_TEMPLATE accepted (template_id=%s template_vmid=%s) — "
                "wiping this host's sim VMs + restoring in the background", tid, vmid)
    asyncio.create_task(do_template_refresh(agent, dict(data)))
    return {"status": "ACCEPTED",
            "message": f"Refreshing template {vmid} — auto-provisioning paused"}


async def list_local_vmids() -> list:
    """VMIDs of qemu guests on this node (`qm list`), for range filtering."""
    import shutil
    try:
        qm = shutil.which("qm") or "/usr/sbin/qm"
        proc = await asyncio.create_subprocess_exec(
            qm, "list", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        ids = []
        for line in out.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].isdigit():
                ids.append(int(parts[0]))
        return ids
    except Exception:  # noqa: BLE001
        return []


def download_refresh_archive(url: str, token: str, dest: str,
                             on_progress=None) -> int:
    """Stream the backup archive from the hub to ``dest`` (blocking; called in
    a thread). verify=False — the hub may still be self-signed pre-LE."""
    import httpx
    total = 0
    with httpx.Client(verify=False,
                     timeout=httpx.Timeout(connect=15.0, read=300.0,
                                           write=None, pool=None)) as c:
        with c.stream("GET", url, headers={"x-refresh-token": token}) as r:
            r.raise_for_status()
            try:
                total = int(r.headers.get("content-length") or 0) or 0
            except (TypeError, ValueError):
                total = 0
            done = 0
            last_report = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(4 * 1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress and done - last_report >= 256 * 1024 * 1024:
                        last_report = done
                        try:
                            on_progress(done, total)
                        except Exception:  # noqa: BLE001
                            pass
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:  # noqa: BLE001
                    pass
    return total


def _proxmox_vzdump_name(vmid, archive_path: str, hint_name: str = "") -> str:
    """Build a Proxmox-valid ``vzdump`` archive filename for ``archive_path``.

    ``qmrestore`` parses the archive *basename* against the vzdump naming
    convention (``vzdump-<type>-<vmid>-<YYYY_MM_DD-HH_MM_SS>.<ext>``) to
    determine the guest type + compression. A name that doesn't match dies with
    ``couldn't determine archive info from '<file>'`` — which is exactly what a
    neutral hub name (``image.vma.zst`` / ``template.vma.zst``) produced, so a
    perfectly valid archive failed to restore. Rename to a conforming name first.

    Compression is sniffed from the file's magic bytes (zstd / gzip / lzop /
    uncompressed VMA), falling back to the hub-provided original filename's
    extension, then to zstd (the backup flow always uses ``--compress zstd``).
    The container is VMA (qemu): these are qemu VM templates and VMA is
    qemu-only.
    """
    import re
    import time as _time
    detected = None
    try:
        with open(archive_path, "rb") as fh:
            magic = fh.read(4)
        if magic[:4] == b"\x28\xb5\x2f\xfd":
            detected = "zst"
        elif magic[:2] == b"\x1f\x8b":
            detected = "gz"
        elif magic[:4] == b"\x89\x4c\x5a\x4f":
            detected = "lzo"
        elif magic[:4] == b"VMA\x00":
            detected = ""  # uncompressed VMA
    except OSError:
        detected = None
    if detected is None:
        m = re.search(r"\.(zst|gz|lzo)$", str(hint_name or ""))
        detected = m.group(1) if m else "zst"
    ext = "vma" + (("." + detected) if detected else "")
    stamp = _time.strftime("%Y_%m_%d-%H_%M_%S")
    return "vzdump-qemu-%d-%s.%s" % (int(vmid), stamp, ext)


async def do_template_refresh(agent, data: Dict[str, Any]) -> None:
    import os
    import re
    import shutil
    import tempfile
    from . import cs_sim, usb_provision

    tid = data.get("template_id")
    template_vmid = int(data.get("template_vmid"))
    url = str(data.get("download_url") or "")
    token = str(data.get("refresh_token") or "")
    progress_url = url.rsplit("/download", 1)[0] + "/refresh-progress"
    headers = {"x-refresh-token": token}

    def _report(status: str, step: str = "", error: str = "",
                bytes_done: int = None, total: int = None) -> None:
        body = {"status": status, "step": step, "error": error,
                "host": getattr(agent, "hostname", "") or "",
                "agent_id": getattr(agent, "agent_id", "") or "",
                "vmid": template_vmid}
        if bytes_done is not None:
            body["bytes"] = int(bytes_done)
        if total is not None:
            body["total"] = int(total)
        try:
            import httpx
            with httpx.Client(verify=False, timeout=15) as c:
                c.post(progress_url, headers=headers, json=body)
        except Exception:  # noqa: BLE001
            pass

    tmpdir = _make_template_scratch_dir("lm-tmpl-refresh-", data)
    usb_provision.set_refresh_paused(True)  # stop auto-prov fighting the wipe
    ok = False
    try:
        _report("pausing", "auto-provisioning paused")
        # 1. Wipe the host's sim VMs (only the sim VMID range, protected
        # excluded, guarded destroy). NOT the template — qmrestore --force
        # overwrites that in step 3, avoiding a raw destroy of an arbitrary vmid.
        prot = cs_sim._protected(agent)
        usb_cfg = (agent.config.get("client_simulation") or {}).get("usb_config") or {}
        max_slots = int(usb_cfg.get("usb_max_slots") or usb_cfg.get("max_slots") or 24)
        start, end, _bid, _d = usb_provision._host_vmid_range(
            getattr(agent, "hostname", "") or "", max_slots,
            usb_cfg.get("vmid_start"), usb_cfg.get("vmid_end"),
            usb_cfg.get("vm_set_override") or 0)
        vmids = await list_local_vmids()
        sim_vmids = [v for v in vmids if start <= v <= end and v not in prot and v != template_vmid]
        _report("killing", f"removing {len(sim_vmids)} sim VM(s)")
        for v in sim_vmids:
            try:
                await cs_sim.destroy_vm(agent, v, protected=prot, exclude_bus_after=True)
            except Exception as e:  # noqa: BLE001 — one failure shouldn't abort the refresh
                logger.warning("REFRESH_TEMPLATE: destroy VM %s failed: %s", v, e)

        # 2. Download the backup archive from the hub. Reports progress every
        # ~256 MB so the hub/UI can show bytes transferred (not a static
        # 'downloading'); a 300s read timeout in the downloader aborts a stalled
        # WAN stream instead of hanging forever.
        host = getattr(agent, "hostname", "") or ""
        _report("downloading", f"{host}: pulling backup from the hub")
        archive = os.path.join(tmpdir, "download.tmp")

        def _on_prog(done, total):
            mb = done // (1024 * 1024)
            if total:
                _report("downloading", f"{host}: {mb} / {total // (1024 * 1024)} MB",
                        bytes_done=done, total=total)
            else:
                _report("downloading", f"{host}: {mb} MB", bytes_done=done)

        total = await asyncio.to_thread(
            download_refresh_archive, url, token, archive, _on_prog)
        if not os.path.isfile(archive) or os.path.getsize(archive) == 0:
            raise RuntimeError("downloaded archive is empty")
        # Verify the full transfer when the hub reported a Content-Length — a
        # truncated archive would make qmrestore fail with a confusing error.
        if total:
            got = os.path.getsize(archive)
            if got != total:
                raise RuntimeError(f"download truncated: got {got} of {total} bytes")
        # qmrestore parses the archive *basename* against the vzdump naming
        # convention to determine the guest type + compression; the hub serves
        # the archive under a neutral name (image/template.vma.zst) which makes
        # qmrestore die "couldn't determine archive info from '<file>'" even for
        # a valid archive. Rename to a conforming vzdump-<type>-<vmid>-<ts>.<ext>
        # (compression sniffed from the file, hub filename as a hint) first.
        restore_name = _proxmox_vzdump_name(
            template_vmid, archive, str(data.get("archive_name") or ""))
        restore_path = os.path.join(tmpdir, restore_name)
        os.rename(archive, restore_path)
        archive = restore_path

        # 3. Restore to the target template VMID (--force overwrites the old
        # template) and re-mark it a template so clones/auto-prov are unchanged.
        _report("restoring", f"{host}: qmrestore → VM {template_vmid}")
        qmrestore = shutil.which("qmrestore") or "/usr/sbin/qmrestore"
        proc = await asyncio.create_subprocess_exec(
            qmrestore, archive, str(template_vmid), "--force",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"qmrestore failed: {(err.decode() or out.decode())[:300]}")
        qm = shutil.which("qm") or "/usr/sbin/qm"
        _report("templating", f"{host}: marking VM {template_vmid} as a template")
        tproc = await asyncio.create_subprocess_exec(
            qm, "template", str(template_vmid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _tout, terr = await tproc.communicate()
        # Verify the flag actually stuck (`qm config` reports `template: 1`) —
        # a restored-but-not-templated VM was the reported symptom, and the
        # conversion was previously swallowed as non-fatal so the refresh still
        # reported "complete". A VM that restored but did NOT become a template
        # is a FAILED refresh (clones/auto-prov depend on it), so surface it.
        cproc = await asyncio.create_subprocess_exec(
            qm, "config", str(template_vmid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        cout, _cerr = await cproc.communicate()
        is_template = bool(re.search(r"(?mi)^template:\s*1\b", cout.decode() or ""))
        if not is_template:
            detail = (terr.decode() or _cerr.decode() or "").strip()[:200]
            raise RuntimeError(
                f"restored VM {template_vmid} but 'qm template' did not take "
                f"(rc={tproc.returncode}): {detail or 'template flag not set'}")
        _report("resuming", "re-enabling auto-provisioning")
        ok = True
        logger.info("REFRESH_TEMPLATE: template %s restored + re-marked on %s",
                    template_vmid, getattr(agent, "hostname", ""))
    except Exception as e:  # noqa: BLE001
        logger.warning("REFRESH_TEMPLATE failed: %s", e)
        _report("failed", error=str(e)[:300])
    finally:
        usb_provision.set_refresh_paused(False)  # ALWAYS resume auto-prov
        shutil.rmtree(tmpdir, ignore_errors=True)
        if ok:
            _report("complete", "refresh complete — auto-provisioning resumed")
