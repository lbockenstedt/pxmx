"""Template backup + refresh for the unified pxmx agent.

Free-function extraction of ``ProxmoxAgent``'s hub-triggered template backup
(vzdump → stream to the hub template repo) and the destructive template refresh
(wipe this host's sim VMs + template → download the backup → qmrestore → resume
auto-prov). Functions take the ``agent`` instance as their first argument where
they need it (the cs_commands/usb_provision pattern). ``ProxmoxAgent`` keeps thin
wrapper methods for the two dispatch-facing kick-off calls so the dispatch chain
is untouched.
"""

# asyncio runs the long backup/refresh work as background tasks and threads.
import asyncio
# os handles paths, file sizes and environment overrides.
import os
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict

# logging reports backup and refresh progress and failures.
import logging

# Share the main agent logger so template messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# Minimum free space (5 GiB) required in the scratch directory before writing an archive.
DEFAULT_TEMPLATE_SCRATCH_MIN_FREE = 5 * 1024 * 1024 * 1024


def _template_scratch_parent(data: Dict[str, Any]) -> str:
    """Large, persistent filesystem for template archives when no Proxmox
    storage was explicitly selected. Never falls back to the OS temp dir."""
    # An explicit scratch dir from the hub or the environment always wins.
    configured = str(data.get("scratch_dir") or
                     os.environ.get("LM_PXMX_TEMPLATE_SCRATCH_DIR") or "").strip()
    if configured:
        return configured
    # Otherwise use the first existing large Proxmox storage directory.
    for candidate in ("/var/lib/vz/dump", "/var/lib/vz",
                      "/var/lib/pxmx/template-scratch"):
        if os.path.isdir(candidate):
            return candidate
    # Last resort: the agent's own state directory (created by the caller).
    return "/var/lib/pxmx/template-scratch"


def _make_template_scratch_dir(prefix: str, data: Dict[str, Any]) -> str:
    """Create a fresh scratch directory for a template archive, after checking free space."""
    # shutil measures free disk space; tempfile creates the uniquely named directory.
    import shutil
    import tempfile
    # Pick the parent directory and make sure it exists.
    parent = _template_scratch_parent(data)
    os.makedirs(parent, exist_ok=True)
    # Required free bytes (env override; 0 disables the check).
    min_free = int(os.environ.get("LM_PXMX_TEMPLATE_MIN_FREE_BYTES",
                                  str(DEFAULT_TEMPLATE_SCRATCH_MIN_FREE)) or 0)
    # Refuse to start when the filesystem is too full for a multi-GB archive.
    if min_free > 0:
        free = shutil.disk_usage(parent).free
        if free < min_free:
            raise RuntimeError(
                f"scratch dir {parent} has {free} bytes free; need at least {min_free}")
    # Create and return a unique directory under the parent.
    return tempfile.mkdtemp(prefix=prefix, dir=parent)


def start_template_backup(data: Dict[str, Any]) -> Dict[str, Any]:
    """Kick off a hub-triggered template backup and ACK immediately.

    vzdump + the upload of a multi-GB archive take minutes, so we spawn the
    work in a background thread and return ACCEPTED right away (the hub's
    START_BACKUP request_response must not block that long). Progress + the
    terminal result flow back over the token'd ``/progress`` + ``/upload``
    endpoints on the hub (routes/templates.py)."""
    # Pull the VM and the upload details out of the request.
    vmid = data.get("vmid")
    upload_url = str(data.get("upload_url") or "")
    token = str(data.get("upload_token") or "")
    # All three are required to do anything useful.
    if vmid is None or not upload_url or not token:
        return {"status": "ERROR",
                "message": "START_BACKUP requires vmid, upload_url and upload_token"}
    # Run the blocking worker in a thread so the agent's event loop stays responsive.
    asyncio.create_task(asyncio.to_thread(do_template_backup, dict(data)))
    # Acknowledge straight away; progress is reported separately to the hub.
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
    # glob finds the vzdump output; shutil/subprocess/tempfile run and manage the dump.
    import glob
    import shutil
    import subprocess
    import tempfile
    # httpx streams the archive to the hub; without it there is no way to upload.
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        logger.warning("START_BACKUP: httpx not available: %s", exc)
        return

    # Request details: VM, optional Proxmox storage, upload URL and token.
    vmid = data.get("vmid")
    storage = str(data.get("storage") or "").strip()
    upload_url = str(data.get("upload_url") or "")
    token = str(data.get("upload_token") or "")
    # The progress endpoint sits next to the upload endpoint on the hub.
    progress_url = upload_url.rsplit("/upload", 1)[0] + "/progress"
    headers = {"x-upload-token": token}

    def _report(status: str, progress=None, error: str = "") -> None:
        """Post a progress/status update to the hub (best-effort; errors are ignored)."""
        # Build the status body with optional progress percent and error text.
        body = {"status": status}
        if progress is not None:
            body["progress"] = progress
        if error:
            body["error"] = error
        # Send it; the hub may use a self-signed certificate, so verification is off.
        try:
            with httpx.Client(verify=False, timeout=15) as c:
                c.post(progress_url, headers=headers, json=body)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def _pvesm(args):
        """Synchronous pvesm helper (we run in a thread, not an event loop).
        Returns (rc, stdout). Best-effort: (1, '') on failure."""
        # Run pvesm with a timeout and return its exit code and output.
        try:
            proc = subprocess.run(["pvesm", *args], capture_output=True,
                                  text=True, timeout=90)
            return proc.returncode, (proc.stdout or "")
        # pvesm missing or timed out.
        except Exception as exc:  # noqa: BLE001
            logger.warning("START_BACKUP: pvesm %s failed: %s", args, exc)
            return 1, ""

    def _volids(txt: str):
        """Return the set of volume ids listed in ``pvesm list`` output."""
        # pvesm list prints a header (no ':') + one volid per line
        # (``<storage>:dump/vzdump-...vma.zst``). The ':' filter drops header.
        return set(ln.split()[0] for ln in txt.splitlines() if ":" in ln)

    # Track what we created so the finally block can clean it up.
    tmpdir = None
    archive_path = None       # filesystem path of the archive we stream
    archive_volid = None       # volid (for pvesm-free fallback) when --storage
    # Any failure is reported to the hub; cleanup always runs in finally.
    try:
        # Tell the hub the dump has started, and locate vzdump.
        _report("dumping", 0)
        vzdump = shutil.which("vzdump") or "/usr/bin/vzdump"
        # Proxmox storage mode: dump onto the storage chosen by the admin.
        if storage:
            # Validate the storage exists + is backup-capable (mirror
            # pve_cmds.vm_action_any's fast-fail) so an obvious misconfig
            # fails before we touch vzdump. Then snapshot the current backup
            # volids for this VM so we can diff out the new one after vzdump.
            # List backup-capable storages (skipping the header line).
            rc, sout = _pvesm(["status", "--content", "backup"])
            names = [ln.split()[0] for ln in sout.splitlines()[1:]
                     if ln.split()]
            # Unknown or non-backup storage: fail early with a clear message.
            if storage not in names:
                _report("failed", error=(
                    f"storage '{storage}' not found / not backup-capable "
                    f"on this host"))
                return
            # Remember which backups of this VM already exist, so the new one can be found
            # afterwards.
            _rc, pre = _pvesm(["list", storage, "--content", "backup",
                               "--vmid", str(vmid)])
            pre_set = _volids(pre)
            # --mode stop is safe for a non-running template; --storage lands
            # the dump on the admin-chosen storage. zstd = Proxmox default.
            proc = subprocess.run(
                [vzdump, str(vmid), "--compress", "zstd", "--mode", "stop",
                 "--storage", storage],
                capture_output=True, text=True)
            # vzdump failed: log and report the error.
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or f"vzdump exited {proc.returncode}").strip()
                logger.warning("START_BACKUP: vzdump failed for %s: %s", vmid, err[:200])
                _report("failed", error=f"vzdump failed: {err[:300]}")
                return
            # Compare the backup list before and after to find the new archive.
            _rc, post = _pvesm(["list", storage, "--content", "backup",
                                "--vmid", str(vmid)])
            new_vols = list(_volids(post) - pre_set)
            # No new archive appeared: report failure.
            if not new_vols:
                _report("failed", error="vzdump produced no archive on storage")
                return
            # Take the newest new volume and ask pvesm for its file path.
            archive_volid = sorted(new_vols)[-1]
            _rc, pout = _pvesm(["path", archive_volid])
            archive_path = pout.strip().splitlines()[0].strip() if pout.strip() else ""
            # Path could not be resolved or the file is missing: report failure.
            if not archive_path or not os.path.isfile(archive_path):
                _report("failed", error=(
                    f"could not resolve archive path for {archive_volid}"))
                return
            # This is the file we will upload.
            path = archive_path
        # No storage given (older hub): dump into a scratch directory.
        else:
            # Back-compat (older hub, no storage in payload): use a large
            # Proxmox/template scratch path, never the OS temp dir on root.
            tmpdir = _make_template_scratch_dir("lm-tmpl-backup-", data)
            proc = subprocess.run(
                [vzdump, str(vmid), "--compress", "zstd", "--mode", "stop",
                 "--dumpdir", tmpdir],
                capture_output=True, text=True)
            # vzdump failed: log and report the error.
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or f"vzdump exited {proc.returncode}").strip()
                logger.warning("START_BACKUP: vzdump failed for %s: %s", vmid, err[:200])
                _report("failed", error=f"vzdump failed: {err[:300]}")
                return
            # Find the archive (prefer .vma.zst, otherwise any vzdump-* file).
            archives = glob.glob(os.path.join(tmpdir, "vzdump-*.vma.zst")) \
                or [p for p in glob.glob(os.path.join(tmpdir, "vzdump-*")) if os.path.isfile(p)]
            # No archive at all: report failure.
            if not archives:
                _report("failed", error="vzdump produced no archive")
                return
            # Use the largest file in case several were produced.
            path = max(archives, key=os.path.getsize)
        # Archive size is sent as Content-Length; log and report the upload start.
        size = os.path.getsize(path)
        logger.info("START_BACKUP: vzdump of %s done (%d bytes) — uploading", vmid, size)
        _report("uploading", 0)

        def _chunks():
            """Yield the archive in 4 MiB chunks so the upload never loads it all into memory."""
            # Read until end of file.
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
        # HTTP 200 means the hub accepted the archive.
        if resp.status_code == 200:
            logger.info("START_BACKUP: upload complete for %s", vmid)
        # Any other status: log and report the rejection.
        else:
            msg = f"hub upload rejected: HTTP {resp.status_code} {resp.text[:200]}"
            logger.warning("START_BACKUP: %s", msg)
            _report("failed", error=msg)
    # Unexpected failure anywhere: log and report it.
    except Exception as e:  # noqa: BLE001
        logger.warning("START_BACKUP: backup of %s failed: %s", vmid, e)
        _report("failed", error=str(e)[:300])
    # Always clean up the on-node archive.
    finally:
        # ALWAYS clean up the on-node archive (the user's space concern).
        # --storage mode writes outside any tempdir, so rmtree wouldn't reach
        # it — delete the resolved file best-effort, with a pvesm free
        # fallback. Tempdir mode is handled by rmtree below. A cleanup error
        # must never mask the real upload result, so it's logged + swallowed.
        # Storage mode: delete the archive file directly.
        if archive_path:
            try:
                if os.path.isfile(archive_path):
                    os.remove(archive_path)
                    logger.info("START_BACKUP: deleted archive %s", archive_path)
            # Delete failed: try asking Proxmox to free the volume instead.
            except Exception as exc:  # noqa: BLE001
                logger.warning("START_BACKUP: os.remove failed for %s: %s — "
                               "trying pvesm free", archive_path, exc)
                if archive_volid:
                    _pvesm(["free", archive_volid])
        # Scratch-dir mode: remove the whole directory.
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── template refresh (hub-triggered, destructive) ────────────────────────
def start_template_refresh(agent, data: Dict[str, Any]) -> Dict[str, Any]:
    """Kick off a REFRESH_TEMPLATE and ACK immediately. The destructive
    sequence (pause auto-prov → wipe the host's sim VMs + template → download
    the backup → qmrestore to the original VMID + re-mark template → resume
    auto-prov) runs as a background task and reports via /refresh-progress."""
    # Pull the template id, its VMID, and the download details out of the request.
    tid = data.get("template_id")
    vmid = data.get("template_vmid")
    url = str(data.get("download_url") or "")
    token = str(data.get("refresh_token") or "")
    # All four are required.
    if not tid or vmid is None or not url or not token:
        return {"status": "ERROR",
                "message": "REFRESH_TEMPLATE requires template_id, template_vmid, download_url, refresh_token"}
    # Log that the destructive refresh is starting.
    logger.info("REFRESH_TEMPLATE accepted (template_id=%s template_vmid=%s) — "
                "wiping this host's sim VMs + restoring in the background", tid, vmid)
    # Run the refresh in the background and acknowledge immediately.
    asyncio.create_task(do_template_refresh(agent, dict(data)))
    return {"status": "ACCEPTED",
            "message": f"Refreshing template {vmid} — auto-provisioning paused"}


async def list_local_vmids() -> list:
    """VMIDs of qemu guests on this node (`qm list`), for range filtering."""
    # shutil.which locates the qm binary.
    import shutil
    # Any failure returns an empty list.
    try:
        # Run `qm list` and capture its output.
        qm = shutil.which("qm") or "/usr/sbin/qm"
        proc = await asyncio.create_subprocess_exec(
            qm, "list", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        # Skip the header line and collect the numeric VMID from the first column of each row.
        ids = []
        for line in out.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].isdigit():
                ids.append(int(parts[0]))
        # VMIDs found on this node.
        return ids
    # qm missing or failed.
    except Exception:  # noqa: BLE001
        return []


def download_refresh_archive(url: str, token: str, dest: str,
                             on_progress=None) -> int:
    """Stream the backup archive from the hub to ``dest`` (blocking; called in
    a thread). verify=False — the hub may still be self-signed pre-LE."""
    # httpx streams the download; total is the expected size (0 if unknown).
    import httpx
    total = 0
    # Long read timeout so slow links work, but a stalled stream still eventually fails.
    with httpx.Client(verify=False,
                     timeout=httpx.Timeout(connect=15.0, read=300.0,
                                           write=None, pool=None)) as c:
        # Download with the refresh token; HTTP errors raise immediately.
        with c.stream("GET", url, headers={"x-refresh-token": token}) as r:
            r.raise_for_status()
            # Expected size from Content-Length, or 0 when missing or invalid.
            try:
                total = int(r.headers.get("content-length") or 0) or 0
            except (TypeError, ValueError):
                total = 0
            # Bytes received so far, and when progress was last reported.
            done = 0
            last_report = 0
            # Write the stream to disk in 4 MiB chunks.
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(4 * 1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    # Report progress roughly every 256 MiB; callback errors are ignored.
                    if on_progress and done - last_report >= 256 * 1024 * 1024:
                        last_report = done
                        try:
                            on_progress(done, total)
                        except Exception:  # noqa: BLE001
                            pass
            # Final progress report once the download finishes.
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:  # noqa: BLE001
                    pass
    # Return the size the hub promised (0 if unknown).
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
    # re reads a compression hint from the filename; time stamps the new name.
    import re
    import time as _time
    # Compression type, or None until we know it.
    detected = None
    # Read the first four bytes of the file to identify the format.
    try:
        with open(archive_path, "rb") as fh:
            magic = fh.read(4)
        # zstd magic number.
        if magic[:4] == b"\x28\xb5\x2f\xfd":
            detected = "zst"
        # gzip magic number.
        elif magic[:2] == b"\x1f\x8b":
            detected = "gz"
        # lzop magic number.
        elif magic[:4] == b"\x89\x4c\x5a\x4f":
            detected = "lzo"
        # Plain uncompressed VMA.
        elif magic[:4] == b"VMA\x00":
            detected = ""  # uncompressed VMA
    # Unreadable file: fall back to the filename hint.
    except OSError:
        detected = None
    # Unknown magic: use the original filename's extension, else assume zstd.
    if detected is None:
        m = re.search(r"\.(zst|gz|lzo)$", str(hint_name or ""))
        detected = m.group(1) if m else "zst"
    # Build "vma" or "vma.<compression>" and a current timestamp.
    ext = "vma" + (("." + detected) if detected else "")
    stamp = _time.strftime("%Y_%m_%d-%H_%M_%S")
    # Proxmox-style name such as vzdump-qemu-9000-2026_01_01-12_00_00.vma.zst.
    return "vzdump-qemu-%d-%s.%s" % (int(vmid), stamp, ext)


async def do_template_refresh(agent, data: Dict[str, Any]) -> None:
    """Destructive template refresh: wipe sim VMs, download the backup, restore and re-template it.
    """
    # Local imports for file handling and lazy imports of sibling modules (avoids circular imports).
    import os
    import re
    import shutil
    import tempfile
    from . import cs_sim, usb_provision

    # Request details: template id/VMID and the download URL/token.
    tid = data.get("template_id")
    template_vmid = int(data.get("template_vmid"))
    url = str(data.get("download_url") or "")
    token = str(data.get("refresh_token") or "")
    # Optional destination storage (Proxmox storage id) for the restored disks.
    # Blank keeps qmrestore's default (each disk goes back to the storage recorded
    # in the backup) — but that storage may not exist on a DIFFERENT target host,
    # which made cross-host restores fail. When set, qmrestore --storage places
    # every restored disk on this storage instead.
    storage = str(data.get("storage") or "").strip()
    # The progress endpoint sits next to the download endpoint on the hub.
    progress_url = url.rsplit("/download", 1)[0] + "/refresh-progress"
    headers = {"x-refresh-token": token}

    def _report(status: str, step: str = "", error: str = "",
                bytes_done: int = None, total: int = None) -> None:
        """Post a refresh progress update (with host, VMID and optional byte counts) to the hub."""
        # Base status body identifying this host and the template VM.
        body = {"status": status, "step": step, "error": error,
                "host": getattr(agent, "hostname", "") or "",
                "agent_id": getattr(agent, "agent_id", "") or "",
                "vmid": template_vmid}
        # Add byte counts when given.
        if bytes_done is not None:
            body["bytes"] = int(bytes_done)
        if total is not None:
            body["total"] = int(total)
        # Send it; errors are ignored because progress is best-effort.
        try:
            import httpx
            with httpx.Client(verify=False, timeout=15) as c:
                c.post(progress_url, headers=headers, json=body)
        except Exception:  # noqa: BLE001
            pass

    # Make the scratch dir, pause auto-provisioning, and assume failure until the end.
    tmpdir = _make_template_scratch_dir("lm-tmpl-refresh-", data)
    usb_provision.set_refresh_paused(True)  # stop auto-prov fighting the wipe
    ok = False
    # Any failure is reported; finally always resumes auto-provisioning and cleans up.
    try:
        # Tell the hub provisioning is paused.
        _report("pausing", "auto-provisioning paused")
        # 1. Wipe the host's sim VMs (only the sim VMID range, protected
        # excluded, guarded destroy). NOT the template — qmrestore --force
        # overwrites that in step 3, avoiding a raw destroy of an arbitrary vmid.
        # Protected VMIDs and the USB config for this host.
        prot = cs_sim._protected(agent)
        usb_cfg = (agent.config.get("client_simulation") or {}).get("usb_config") or {}
        # This host's simulation VMID range (based on its hostname and slot count).
        max_slots = int(usb_cfg.get("usb_max_slots") or usb_cfg.get("max_slots") or 24)
        start, end, _bid, _d = usb_provision._host_vmid_range(
            getattr(agent, "hostname", "") or "", max_slots,
            usb_cfg.get("vmid_start"), usb_cfg.get("vmid_end"),
            usb_cfg.get("vm_set_override") or 0)
        # Simulation VMs on this host, excluding protected ones and the template itself.
        vmids = await list_local_vmids()
        sim_vmids = [v for v in vmids if start <= v <= end and v not in prot and v != template_vmid]
        # Report how many VMs will be removed, then destroy each one.
        _report("killing", f"removing {len(sim_vmids)} sim VM(s)")
        for v in sim_vmids:
            try:
                await cs_sim.destroy_vm(agent, v, protected=prot, exclude_bus_after=True)
            # Log the failure and continue with the next VM.
            except Exception as e:  # noqa: BLE001 — one failure shouldn't abort the refresh
                logger.warning("REFRESH_TEMPLATE: destroy VM %s failed: %s", v, e)

        # 2. Download the backup archive from the hub. Reports progress every
        # ~256 MB so the hub/UI can show bytes transferred (not a static
        # 'downloading'); a 300s read timeout in the downloader aborts a stalled
        # WAN stream instead of hanging forever.
        # Report the start of the download.
        host = getattr(agent, "hostname", "") or ""
        _report("downloading", f"{host}: pulling backup from the hub")
        # Temporary download path inside the scratch dir.
        archive = os.path.join(tmpdir, "download.tmp")

        def _on_prog(done, total):
            """Progress callback: report MB downloaded (and total when known) to the hub."""
            # Convert bytes to whole MB for display.
            mb = done // (1024 * 1024)
            if total:
                _report("downloading", f"{host}: {mb} / {total // (1024 * 1024)} MB",
                        bytes_done=done, total=total)
            else:
                _report("downloading", f"{host}: {mb} MB", bytes_done=done)

        # Download in a thread so the event loop stays free.
        total = await asyncio.to_thread(
            download_refresh_archive, url, token, archive, _on_prog)
        # An empty or missing file means the download failed.
        if not os.path.isfile(archive) or os.path.getsize(archive) == 0:
            raise RuntimeError("downloaded archive is empty")
        # Verify the full transfer when the hub reported a Content-Length — a
        # truncated archive would make qmrestore fail with a confusing error.
        if total:
            # Compare the actual file size with the advertised size.
            got = os.path.getsize(archive)
            if got != total:
                raise RuntimeError(f"download truncated: got {got} of {total} bytes")
        # qmrestore parses the archive *basename* against the vzdump naming
        # convention to determine the guest type + compression; the hub serves
        # the archive under a neutral name (image/template.vma.zst) which makes
        # qmrestore die "couldn't determine archive info from '<file>'" even for
        # a valid archive. Rename to a conforming vzdump-<type>-<vmid>-<ts>.<ext>
        # (compression sniffed from the file, hub filename as a hint) first.
        # Choose the Proxmox-style name and rename the file to it.
        restore_name = _proxmox_vzdump_name(
            template_vmid, archive, str(data.get("archive_name") or ""))
        restore_path = os.path.join(tmpdir, restore_name)
        os.rename(archive, restore_path)
        archive = restore_path

        # 3. Restore to the target template VMID (--force overwrites the old
        # template) and re-mark it a template so clones/auto-prov are unchanged.
        # --storage (when supplied) redirects every restored disk onto a storage
        # that exists on THIS host, so a seed backed up from a host whose storage
        # id differs still restores here.
        # Report the restore step (mentioning the target storage if one was chosen).
        dest = f" (storage {storage})" if storage else ""
        _report("restoring", f"{host}: qmrestore → VM {template_vmid}{dest}")
        # Build the qmrestore command; --force overwrites the existing template VM.
        qmrestore = shutil.which("qmrestore") or "/usr/sbin/qmrestore"
        qmrestore_args = [qmrestore, archive, str(template_vmid), "--force"]
        # Place restored disks on the chosen storage when one was given.
        if storage:
            qmrestore_args += ["--storage", storage]
        # Run qmrestore and wait for it.
        proc = await asyncio.create_subprocess_exec(
            *qmrestore_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        # Restore failed: abort with its error message.
        if proc.returncode != 0:
            raise RuntimeError(f"qmrestore failed: {(err.decode() or out.decode())[:300]}")
        # Convert the restored VM back into a template.
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
        # Read the VM config and check that "template: 1" is now set.
        cproc = await asyncio.create_subprocess_exec(
            qm, "config", str(template_vmid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        cout, _cerr = await cproc.communicate()
        is_template = bool(re.search(r"(?mi)^template:\s*1\b", cout.decode() or ""))
        # Not a template: treat the refresh as failed.
        if not is_template:
            detail = (terr.decode() or _cerr.decode() or "").strip()[:200]
            raise RuntimeError(
                f"restored VM {template_vmid} but 'qm template' did not take "
                f"(rc={tproc.returncode}): {detail or 'template flag not set'}")
        # Success: report, mark ok, and log it.
        _report("resuming", "re-enabling auto-provisioning")
        ok = True
        logger.info("REFRESH_TEMPLATE: template %s restored + re-marked on %s",
                    template_vmid, getattr(agent, "hostname", ""))
    # Any failure: log it and tell the hub.
    except Exception as e:  # noqa: BLE001
        logger.warning("REFRESH_TEMPLATE failed: %s", e)
        _report("failed", error=str(e)[:300])
    finally:
        usb_provision.set_refresh_paused(False)  # ALWAYS resume auto-prov
        # Remove the scratch directory and the downloaded archive.
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Tell the hub it finished only when everything worked.
        if ok:
            _report("complete", "refresh complete — auto-provisioning resumed")
