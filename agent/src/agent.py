"""pxmx Proxmox host agent — ``ProxmoxAgent``.

Runs **on** a Proxmox node and is the only component with ``qm``/``pct``
clone/destroy access, so all VM-mutating work happens here. It connects (over
WS, via the pxmx spoke/control plane on :8766) and:

- Emits the cs telemetry body (``_cs_telemetry_body``) shaped to mirror the
  legacy cs bash agent's telemetry, consumed by the cs spoke's
  ``ProxmoxDeploy.ingest_telemetry`` — node summary, enriched VMs, versions,
  VMID range, vm-set/template-lock/provision-halt flags, USB state from
  ``usb_provision.cs_usb_telemetry``, plus ``provision_halt`` and ``prov_run``.
- Relays cs events upstream and runs the USB auto-provisioning brain
  (``_usb_provision_loop`` → ``usb_provision.run_provision_loop``) — this is
  the single brain in the LM topology (the cs spoke is relay-only).
- Dispatches cs commands (fast + long ops) and runs the watchdogs.

Provenance: Phase D1/G port of the legacy ``cs/proxmox/proxmox-agent.sh`` bash
agent (retired in Phase G). Audience: pxmx developers; see the repo
``ARCHITECTURE.md`` for topology and ``README.md`` for operators.
"""

import asyncio
import json
import uuid
import time
import logging
import psutil
import argparse
import os
import socket
from typing import Any, Dict, List, Optional
from .security_utils import MessageSigner
from . import cs_commands
from . import cs_sim
from . import watchdogs
from . import usb_provision

class _AuthError(Exception):
    """Raised when the spoke rejects our credentials (wrong secret)."""

class WebSocketLogHandler(logging.Handler):
    """Relays agent log records (INFO+, own loggers only) over the WebSocket connection."""

    # Only relay records from loggers whose names start with these prefixes.
    _RELAY_PREFIXES = ("PxmxAgent", "ProxmoxAgent")

    def __init__(self, agent):
        super().__init__(level=logging.INFO)
        self.agent = agent

    def emit(self, record):
        if not record.name.startswith(self._RELAY_PREFIXES):
            return
        try:
            msg = self.format(record)
            if self.agent.websocket:
                loop = asyncio.get_running_loop()
                loop.create_task(self.agent.send_log(msg, record.levelname))
        except RuntimeError:
            pass  # no running event loop — connection not yet up
        except Exception:
            self.handleError(record)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("/var/log/pxmx-agent.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("PxmxAgent")


def _sd_notify(state: str) -> None:
    """Best-effort systemd notification (READY=1 / WATCHDOG=1).

    Pairs with ``WatchdogSec=`` + ``NotifyAccess=main`` in the
    lm-pxmx-agent.service unit (Phase G): the heartbeat loop pings WATCHDOG=1
    every tick so systemd can detect a hung event loop. No-op when
    ``NOTIFY_SOCKET`` is unset (non-systemd / standalone runs), so it is safe
    to call unconditionally.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            # Abstract-socket addresses start with '@' (Linux convention).
            sock.connect("\0" + addr[1:] if addr.startswith("@") else addr)
            sock.sendall(state.encode())
        finally:
            sock.close()
    except Exception:
        pass


def get_version():
    """Return the pxmx agent version from the VERSION file (searches a few candidate paths), or ``"unknown"``."""
    paths = ["VERSION",
             os.path.join(os.path.dirname(__file__), "../../VERSION"),
             os.path.join(os.path.dirname(__file__), "../VERSION")]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip()
    return "unknown"

version = get_version()


class ProxmoxAgent:
    """The pxmx host agent — runs on a Proxmox node and owns all VM-mutating work.

    Connects to the pxmx spoke (over WS on :8766), authenticates, then runs the
    telemetry/USB-provision/cs-command/watchdog loops. See the module docstring.
    """

    def __init__(self, spoke_url: str, agent_id: str, secret: Optional[str] = None):
        self.spoke_url = spoke_url
        self.agent_id = agent_id
        self.secret = secret or self._load_secret()
        # No secret is OK — we will connect without one and wait for approval.

        self.websocket = None
        self.config: Dict[str, Any] = {}
        self.signer = MessageSigner(self.secret or "")
        self.hostname = socket.gethostname()
        self.agent_type = "pxmx-agent"

        # Proxmox cluster name — resolved on first telemetry push; defaults to hostname.
        self.cluster_name: str = self.hostname
        self._cluster_resolved: bool = False

        # ── Client Simulation mode (unified agent) ──────────────────────────────
        # When self.config["client_simulation"]["enabled"] is true, the agent
        # activates the cs feature set (USB provisioning, watchdogs, reseed, etc.)
        # as background asyncio tasks. This is the task-group seam every later
        # phase builds on; for now the group is empty and the toggle is logged.
        self.cs_enabled: bool = False
        self._cs_tasks: set = set()
        # Long-op (Phase E) + token-provision (Phase F) tasks. Cancelled on
        # CS-disable / disconnect so a toggled-off host stops mutating VMs.
        self._cs_long_ops: set = set()

    def _load_secret(self) -> Optional[str]:
        # 1. Prefer the hub-provisioned secret persisted to .env. This is the
        #    authoritative secret — it is the control plane's own agent_secret,
        #    handed to us on approval (see _save_secret), so it is guaranteed to
        #    match. Checking it first means we survive an agent restart (e.g. a
        #    self-update) without needing re-approval.
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.abspath(env_path)
        try:
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        if line.startswith("AGENT_SECRET="):
                            val = line.split("=", 1)[1].strip()
                            if val:
                                return val
        except Exception:
            pass
        # 2. No .env secret → go zero-touch (connect without a secret and wait for
        #    admin approval, which provisions the matching secret via _save_secret).
        #
        #    We deliberately do NOT fall back to /etc/lm-agent/config.json here. That
        #    file is written by install_pxmx.sh on the *hub*, not by install_agent.sh
        #    on the node, so on a node it is either absent or a stale copy from a
        #    manual deploy. A stale value there will never match the control plane's
        #    current agent_secret, so using it only guarantees a "bad secret" reject
        #    followed by a zero-touch re-approval — an infinite flap that keeps the
        #    agent out of connected_agents and invisible in the UI. Trusting only the
        #    provisioned .env secret (+ zero-touch when absent) breaks that loop.
        return None

    def _save_secret(self, secret: str):
        """Persist a provisioned secret to .env so it survives restarts."""
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.abspath(env_path)
        try:
            lines = []
            if os.path.exists(env_path):
                with open(env_path) as f:
                    lines = [l for l in f if not l.startswith("AGENT_SECRET=")]
            lines.append(f"AGENT_SECRET={secret}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            logger.info(f"Provisioned secret saved to {env_path}")
        except Exception as e:
            logger.error(f"Could not save provisioned secret: {e}")

    def _clear_secret(self):
        """Clear the provisioned (.env) secret and go zero-touch.

        Called when the spoke rejects our secret (the control plane's agent_secret
        rotated, or our .env value is stale). We wipe .env's AGENT_SECRET and drop to
        zero-touch so the admin can re-approve and re-provision a matching secret.

        We intentionally do NOT fall back to /etc/lm-agent/config.json (see _load_secret):
        that file is a hub-side artifact, stale-or-absent on the node, and falling back to
        it is what used to drive the "bad secret → bad secret → zero-touch" flap.
        """
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.abspath(env_path)
        try:
            if os.path.exists(env_path):
                with open(env_path) as f:
                    lines = [l for l in f if not l.startswith("AGENT_SECRET=")]
                with open(env_path, "w") as f:
                    f.writelines(lines)
        except Exception as e:
            logger.warning(f"Could not clear secret from .env: {e}")
        self.secret = None
        self.signer = MessageSigner("")

    # ── Local pvesh helpers ───────────────────────────────────────────────────

    def _pvesh_bin(self) -> str:
        """Locate the pvesh binary; checks common Proxmox install paths."""
        for candidate in ["/usr/bin/pvesh", "/usr/sbin/pvesh", "pvesh"]:
            if candidate == "pvesh":
                return candidate  # fall back to PATH
            if os.path.isfile(candidate):
                return candidate
        return "pvesh"

    async def _pvesh(self, path: str) -> Any:
        """Run pvesh get <path> locally and return parsed JSON. No auth needed."""
        bin_ = self._pvesh_bin()
        proc = await asyncio.create_subprocess_exec(
            bin_, "get", path, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode().strip() or f"pvesh exited {proc.returncode}")
        return json.loads(stdout.decode())

    async def _pvesh_action(self, verb: str, path: str, *args: str,
                             json_out: bool = True, timeout: int = 20) -> Any:
        """Run pvesh <verb> <path> [*args] locally. ``verb`` is create/delete/put/
        set/etc. Returns parsed JSON (when json_out) or stdout text. Raises
        RuntimeError on non-zero exit. Used by token provisioning (Phase F)."""
        bin_ = self._pvesh_bin()
        cmd = [bin_, verb, path, *args]
        if json_out:
            cmd += ["--output-format", "json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode().strip() or f"pvesh {verb} exited {proc.returncode}")
        if not json_out:
            return stdout.decode()
        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return stdout.decode()

    async def _fetch_cluster_name(self) -> str:
        """Returns the Proxmox cluster name, or this node's hostname for standalone nodes."""
        try:
            items = await self._pvesh("/cluster/status")
            for item in (items if isinstance(items, list) else []):
                if item.get("type") == "cluster":
                    return item.get("name", self.hostname)
        except Exception as e:
            logger.warning(f"Could not fetch cluster name via pvesh: {e}")
        return self.hostname

    async def collect_metrics(self) -> Dict[str, Any]:
        """Agent host OS metrics."""
        return {
            "cpu_usage":    psutil.cpu_percent(interval=1),
            "memory_usage": psutil.virtual_memory().percent,
            "disk_usage":   psutil.disk_usage('/').percent,
            "timestamp":    time.time(),
        }

    async def get_node_stats(self) -> Dict[str, Any]:
        """Per-node stats via local pvesh — no API credentials required.

        Uses /cluster/resources (type=node) as primary source — same daemon that
        powers Proxmox's own UI, so cpu values are always current.  Falls back to
        per-node /status if the cluster endpoint is unavailable.
        """
        try:
            # Primary: /cluster/resources filtered for nodes — cpu from pvestatd (always live)
            try:
                resources = await self._pvesh("/cluster/resources")
                nodes = []
                for r in (resources if isinstance(resources, list) else []):
                    if r.get("type") != "node":
                        continue
                    node_name = r.get("node", "")
                    mem_used  = r.get("mem", 0)
                    mem_total = r.get("maxmem", 1)
                    nodes.append({
                        "cluster":         self.cluster_name,
                        "node":            node_name,
                        "status":          r.get("status", "unknown"),
                        "cpu_usage":       round(r.get("cpu", 0) * 100, 1),
                        "cpu_cores":       r.get("maxcpu", 0),
                        "mem_used":        mem_used,
                        "mem_total":       mem_total,
                        "mem_pct":         round(mem_used / max(mem_total, 1) * 100, 1),
                        "uptime":          r.get("uptime", 0),
                        "proxmox_version": "",
                    })
                # /cluster/resources doesn't carry pveversion, so the Proxmox
                # version would always be blank here. Fetch it once from the
                # first node's /status (PVE version is cluster-wide) and fill it
                # into every node — otherwise the hub's Nodes "Version" column
                # always shows "—". Best-effort: stays "" if the lookup fails.
                if nodes:
                    try:
                        stat = await self._pvesh(f"/nodes/{nodes[0]['node']}/status")
                        pve_ver = (stat or {}).get("pveversion", "")
                    except Exception as e:
                        logger.warning(f"pveversion lookup failed: {e}")
                        pve_ver = ""
                    if pve_ver:
                        for n in nodes:
                            n["proxmox_version"] = pve_ver
                if nodes:
                    return {"nodes": nodes, "cluster": self.cluster_name}
            except Exception as e:
                logger.warning(f"cluster/resources unavailable for nodes ({e}), falling back to per-node status")

            # Fallback: per-node /status (cpu resets to 0 on first call — less accurate)
            raw_nodes = await self._pvesh("/nodes")
            nodes = []
            for n in (raw_nodes if isinstance(raw_nodes, list) else []):
                node_name = n.get("node", "")
                try:
                    stat = await self._pvesh(f"/nodes/{node_name}/status")
                    mem      = stat.get("memory", {})
                    cpu_info = stat.get("cpuinfo", {})
                    nodes.append({
                        "cluster":         self.cluster_name,
                        "node":            node_name,
                        "status":          n.get("status", "unknown"),
                        "cpu_usage":       round(stat.get("cpu", 0) * 100, 1),
                        "cpu_cores":       cpu_info.get("cpus", n.get("maxcpu", 0)),
                        "mem_used":        mem.get("used", n.get("mem", 0)),
                        "mem_total":       mem.get("total", n.get("maxmem", 0)),
                        "mem_pct":         round(mem.get("used", 0) / max(mem.get("total", 1), 1) * 100, 1),
                        "uptime":          stat.get("uptime", n.get("uptime", 0)),
                        "proxmox_version": stat.get("pveversion", ""),
                    })
                except Exception as e:
                    logger.warning(f"Node status error for {node_name}: {e}")
            return {"nodes": nodes, "cluster": self.cluster_name}
        except Exception as e:
            logger.error(f"Node stats error: {e}")
            return {"nodes": [], "error": str(e)}

    async def _vm_ips(self, node: str, vmid: Any, rtype: str, status: str) -> List[str]:
        """Best-effort guest IP lookup for one VM/CT. Returns IPv4 addresses
        (loopback/link-local excluded, deduped). qemu uses the guest-agent
        network-get-interfaces endpoint — only works when qemu-guest-agent is
        installed and running in the guest; lxc uses /interfaces, which reads
        the container network namespace (no guest agent needed). Stopped VMs
        and absent/unresponsive guest agents yield []. Read-only pvesh GET,
        safe for any VMID (no execution guard)."""
        if status != "running" or not node or vmid in (None, ""):
            return []
        kind = "qemu" if rtype == "qemu" else "lxc"
        try:
            if kind == "qemu":
                data = await asyncio.wait_for(
                    self._pvesh(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"),
                    timeout=4)
            else:
                data = await asyncio.wait_for(
                    self._pvesh(f"/nodes/{node}/lxc/{vmid}/interfaces"),
                    timeout=4)
        except Exception:
            return []
        # PVE wraps agent responses inconsistently; unwrap result/data/lists.
        result = data
        if isinstance(data, dict):
            result = data.get("result", data.get("data", data))
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
        ips: List[str] = []
        if isinstance(result, list):
            for iface in result:
                if not isinstance(iface, dict):
                    continue
                # qemu guest-agent: {"ip-addresses": [{"ip-address","ip-address-type"}]}
                for entry in (iface.get("ip-addresses") or []):
                    if str(entry.get("ip-address-type", "")).lower() == "ipv4":
                        ip = entry.get("ip-address")
                        if isinstance(ip, str) and ip and not ip.startswith(("127.", "169.254.")):
                            ips.append(ip)
                # lxc /interfaces: {"inet": "1.2.3.4/24" | ["1.2.3.4/24", ...]}
                inet = iface.get("inet")
                addrs = inet if isinstance(inet, list) else (
                    [inet] if isinstance(inet, str) and inet else [])
                for addr in addrs:
                    ip = str(addr).split("/")[0]
                    if ip and not ip.startswith(("127.", "169.254.")):
                        ips.append(ip)
        seen, out = set(), []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
        return out

    async def _annotate_vm_ips(self, vms: List[Dict[str, Any]]) -> None:
        """Populate vm["ips"] for running VMs in parallel — best-effort, bounded
        by a semaphore (16 concurrent pvesh calls) and an overall 10s deadline
        so a hung guest agent can't stall the 60s telemetry tick. Stopped/template
        VMs keep ips=[] (set in _vm_entry). VMs not annotated before the deadline
        also keep ips=[]."""
        running = [v for v in vms if v.get("status") == "running"]
        if not running:
            return
        sem = asyncio.Semaphore(16)

        async def _one(v):
            async with sem:
                v["ips"] = await self._vm_ips(
                    v.get("node", ""), v.get("vmid"), v.get("type"), v.get("status"))

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_one(v) for v in running], return_exceptions=True),
                timeout=10)
        except asyncio.TimeoutError:
            pass  # partial — VMs not yet annotated keep ips=[]

    async def get_vm_list(self) -> Dict[str, Any]:
        """
        All VMs and containers via local pvesh — no API credentials required.

        Each entry includes:
          unique_id  — globally unique: "<cluster>/<node>/<vmid>"
          cluster    — Proxmox cluster name (or hostname for standalone)
          node       — Proxmox node name
          vmid       — integer VMID
          type       — "qemu" or "lxc"
          name, status, cpu, mem_bytes, uptime, tags, ips
                     — ips: best-effort guest IPv4 list ([] for stopped VMs or
                       when qemu-guest-agent is absent; LXC needs no guest agent)

        Uses /cluster/resources as the primary source (up-to-date stats, single
        call, works for both standalone and clustered setups).  Falls back to
        per-node /qemu and /lxc queries if the cluster endpoint is unavailable.
        Guest IPs are annotated in parallel after the base list is built.
        """
        def _parse_tags(raw):
            return [t.strip() for t in (raw or "").split(";") if t.strip()]

        def _vm_entry(r, node, rtype, vmid):
            return {
                "unique_id": f"{self.cluster_name}/{node}/{vmid}",
                "cluster":   self.cluster_name,
                "node":      node,
                "vmid":      vmid,
                "type":      rtype,
                "name":      r.get("name", f"{'vm' if rtype == 'qemu' else 'ct'}-{vmid}"),
                "status":    r.get("status", "unknown"),
                "cpu":       round(r.get("cpu", 0) * 100, 1),
                "mem_bytes": r.get("mem") or r.get("maxmem", 0),
                "uptime":    r.get("uptime", 0),
                "tags":      _parse_tags(r.get("tags")),
                "ips":       [],   # filled by _annotate_vm_ips for running VMs
            }

        try:
            # Primary: /cluster/resources — single call, Proxmox keeps this view
            # up-to-date for its own summary UI; works on standalone nodes too.
            try:
                resources = await self._pvesh("/cluster/resources")
                all_vms = [
                    _vm_entry(r, r.get("node", ""), r.get("type"), r.get("vmid"))
                    for r in (resources if isinstance(resources, list) else [])
                    if r.get("type") in ("qemu", "lxc")
                ]
                await self._annotate_vm_ips(all_vms)
                return {"vms": all_vms, "cluster": self.cluster_name}
            except Exception as e:
                logger.warning(f"cluster/resources unavailable ({e}), falling back to per-node queries")

            # Fallback: per-node /qemu + /lxc
            raw_nodes = await self._pvesh("/nodes")
            all_vms = []
            for n in (raw_nodes if isinstance(raw_nodes, list) else []):
                node_name = n.get("node", "")

                try:
                    for vm in await self._pvesh(f"/nodes/{node_name}/qemu"):
                        all_vms.append(_vm_entry(vm, node_name, "qemu", vm.get("vmid")))
                except Exception as e:
                    logger.warning(f"QEMU list error for {node_name}: {e}")

                try:
                    for ct in await self._pvesh(f"/nodes/{node_name}/lxc"):
                        all_vms.append(_vm_entry(ct, node_name, "lxc", ct.get("vmid")))
                except Exception as e:
                    logger.warning(f"LXC list error for {node_name}: {e}")

            await self._annotate_vm_ips(all_vms)
            return {"vms": all_vms, "cluster": self.cluster_name}
        except Exception as e:
            logger.error(f"VM list error: {e}")
            return {"vms": [], "cluster": self.cluster_name, "error": str(e)}

    async def send_log(self, message: str, level: str):
        """Send an AGENT_LOG message upstream (relayed to BugFixer by the Hub)."""
        try:
            log_msg = {
                "header": {
                    "message_id":   str(uuid.uuid4()),
                    "timestamp":    time.time(),
                    "sender_id":    self.agent_id,
                    "destination_id": "pxmx-spoke",
                },
                "payload": {
                    "type": "AGENT_LOG",
                    "data": {"message": message, "level": level,
                             "hostname": self.hostname, "agent_type": self.agent_type},
                },
            }
            log_msg["signature"] = self.signer.sign(log_msg)
            await self.websocket.send(json.dumps(log_msg))
        except Exception:
            pass

    async def send_cs_event(self, event_type: str, data: Dict[str, Any]):
        """Emit a Client-Simulation event (CS_WATCHDOG_EVENT / CS_HW_RESET_EVENT /
        CS_PROGRESS / CS_COMMAND_RESULT / CS_TOKEN_RESULT / CS_TELEMETRY / CS_LOG)
        up to the spoke. Mirrors send_log; the payload ``type`` is the event_type
        so the hub's AGENT_RELAY_UP dispatcher can route CS_* payloads to the cs
        spoke. Best-effort: never raises (watchdogs must proceed even if the
        socket is gone)."""
        try:
            msg = {
                "header": {
                    "message_id":    str(uuid.uuid4()),
                    "timestamp":     time.time(),
                    "sender_id":     self.agent_id,
                    "destination_id": "pxmx-spoke",
                },
                "payload": {
                    "type": event_type,
                    "data": {**data, "hostname": self.hostname,
                             "agent_id": self.agent_id},
                },
            }
            msg["signature"] = self.signer.sign(msg)
            await self.websocket.send(json.dumps(msg))
        except Exception:
            pass

    # ── Self-update ───────────────────────────────────────────────────────────

    def _git_behind_count(self, repo_dir: str) -> int:
        """Return number of commits the local repo is behind origin/main."""
        import subprocess
        subprocess.check_call(
            ["git", "-C", repo_dir, "fetch", "--quiet"],
            timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        out = subprocess.check_output(
            ["git", "-C", repo_dir, "rev-list", "--count", "HEAD..origin/main"],
            timeout=10,
        )
        return int(out.decode().strip())

    def _apply_update(self, install_dir: str, repo_dir: str):
        """Pull latest code, sync to install dir, pip install, then restart."""
        import subprocess, shutil, pathlib
        current = get_version()
        subprocess.check_call(
            ["git", "-C", repo_dir, "pull", "--rebase", "--autostash"],
            timeout=60, stdout=subprocess.DEVNULL,
        )
        new_ver_path = pathlib.Path(repo_dir) / "agent" / "VERSION"
        new_ver = new_ver_path.read_text().strip() if new_ver_path.exists() else "?"
        if new_ver == current:
            return  # same version — no restart needed
        logger.info(f"Updating pxmx-agent {current} → {new_ver}")
        src = pathlib.Path(repo_dir) / "agent"
        dst = pathlib.Path(install_dir)
        for item in src.iterdir():
            if item.name in {".env", "venv"}:
                continue
            dest = dst / item.name
            if dest.is_dir():
                shutil.rmtree(dest)
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))
        pip = dst / "venv" / "bin" / "pip"
        req = dst / "requirements.txt"
        if pip.exists() and req.exists():
            subprocess.check_call([str(pip), "install", "-r", str(req), "-q"], timeout=120)
        logger.info("Self-update applied — restarting service")
        subprocess.Popen(["systemctl", "restart", "lm-pxmx-agent"])
        os._exit(0)

    async def trigger_update(self) -> None:
        """Force an immediate self-update check (Phase E ``update_agent`` long op).
        Runs the blocking git pull + sync + restart in an executor; returns if
        there is no repo or no new version (``_apply_update`` os._exit(0)s when
        it actually applies, so the caller's terminal result is only reached
        when the agent was already current)."""
        import pathlib
        install_dir = str(pathlib.Path(__file__).resolve().parent.parent)
        repo_dir = str(pathlib.Path(install_dir) / ".pxmx_repo")
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            logger.warning("trigger_update: no .pxmx_repo checkout — skipping")
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._apply_update, install_dir, repo_dir)

    # ── Proxmox API token provisioning (Phase F) ──────────────────────────────
    # The cs spoke has no pvesh, so the hub asks this agent to create the cs-hub
    # API token (root@pam!cs-hub) via the local pvesh. The token secret transits
    # the hub (unavoidable — the cs spoke must store it for sim-tag sync); it is
    # NEVER logged here and is zeroized after forwarding (the local `secret`
    # var is dropped when the task returns).

    async def _provision_proxmox_token(self, request_id: str) -> None:
        """Create (idempotent) the cs-hub token and emit CS_TOKEN_RESULT.

        Mirrors bash ``handle_create_proxmox_token`` (4466-4503): pvesh delete
        (ignore failure) → pvesh create --privsep 0 → parse ``value`` → emit
        ``root@pam!cs-hub=<secret>``. The token is forwarded best-effort; the
        secret is never logged."""
        TOKEN_ID = "cs-hub"
        USER = "root@pam"
        try:
            try:
                await self._pvesh_action("delete",
                                         f"/access/users/{USER}/token/{TOKEN_ID}",
                                         json_out=False, timeout=10)
            except Exception:
                pass  # token may not exist yet — expected
            data = await self._pvesh_action(
                "create", f"/access/users/{USER}/token/{TOKEN_ID}",
                "--privsep", "0", timeout=20)
            secret = ""
            if isinstance(data, dict):
                secret = str(data.get("value") or "").strip()
            if not secret:
                logger.error(f"token provision {request_id}: pvesh returned no value")
                await self.send_cs_event("CS_TOKEN_RESULT",
                                          {"request_id": request_id, "status": "error",
                                           "error": "pvesh returned no token value"})
                return
            token = f"{USER}!{TOKEN_ID}={secret}"
            await self.send_cs_event("CS_TOKEN_RESULT",
                                     {"request_id": request_id, "status": "provisioned",
                                      "token": token})
            # Drop the secret reference; nothing else holds it after the send.
            del token, secret
            logger.info(f"token provisioned for request_id={request_id} (value not logged)")
        except Exception as e:  # noqa: BLE001
            logger.error(f"token provision {request_id} failed: {e}")
            await self.send_cs_event("CS_TOKEN_RESULT",
                                      {"request_id": request_id, "status": "error",
                                       "error": str(e)[:300]})

    async def _update_check_loop(self):
        """Check for new versions every 10 minutes and self-update when found."""
        import pathlib
        await asyncio.sleep(120)  # wait before first check
        while True:
            try:
                install_dir = str(pathlib.Path(__file__).resolve().parent.parent)
                repo_dir = str(pathlib.Path(install_dir) / ".pxmx_repo")
                if not os.path.isdir(os.path.join(repo_dir, ".git")):
                    await asyncio.sleep(600)
                    continue
                loop = asyncio.get_running_loop()
                behind = await loop.run_in_executor(
                    None, self._git_behind_count, repo_dir
                )
                if behind > 0:
                    logger.info(f"Agent is {behind} commit(s) behind — applying update")
                    await loop.run_in_executor(
                        None, self._apply_update, install_dir, repo_dir
                    )
            except Exception as e:
                logger.debug(f"Update check: {e}")
            await asyncio.sleep(600)

    async def _sd_watchdog_loop(self):
        """Feed systemd's WatchdogSec on a fixed cadence, independent of the
        websocket state (so a long disconnect backoff doesn't falsely trip the
        watchdog while a genuine event-loop hang still does). Pairs with
        WatchdogSec=60 + NotifyAccess=main in lm-pxmx-agent.service (Phase G).
        No-op outside systemd (_sd_notify checks NOTIFY_SOCKET)."""
        interval = 20
        try:
            interval = max(5, int(os.environ.get("LM_SD_NOTIFY_INTERVAL_S", "20")))
        except Exception:
            pass
        _sd_notify("READY=1")  # harmless under Type=simple
        while True:
            try:
                _sd_notify("WATCHDOG=1")
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def run(self):
        """Main agent loop — connect, auth, run telemetry/provision/command/watchdog tasks, reconnect with backoff.

        Spawns the self-update check loop and the systemd watchdog loop, then
        reconnects forever (exponential-ish backoff) on socket loss; re-raises
        on repeated auth failure so a bad secret doesn't spin forever.
        """
        import websockets
        backoff = 5
        _consecutive_auth_fails = 0
        asyncio.create_task(self._update_check_loop())
        asyncio.create_task(self._sd_watchdog_loop())
        while True:
            try:
                await self._connect_once()
                backoff = 5  # reset on clean disconnect
                _consecutive_auth_fails = 0
            except _AuthError:
                _consecutive_auth_fails += 1
                logger.warning(
                    "Authentication rejected by spoke — clearing stale secret and "
                    "retrying zero-touch (will await re-approval). "
                    "Approve this Proxmox node agent from the LM WebUI: Setup → Spokes & Agents (Agents tile)."
                )
                self._clear_secret()
                # If the fallback static secret is also rejected, force zero-touch so
                # the admin can re-provision rather than looping on a bad secret.
                if _consecutive_auth_fails >= 2 and self.secret:
                    logger.warning("Pre-configured secret also rejected — entering zero-touch provisioning.")
                    self.secret = None
                await asyncio.sleep(5)
            except (OSError, websockets.exceptions.WebSocketException) as e:
                logger.warning(f"Connection to {self.spoke_url} failed: {e} — retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception as e:
                logger.error(f"Unexpected error: {e} — retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _connect_once(self):
        import websockets
        logger.info(f"pxmx-agent {version} connecting to {self.spoke_url}...")

        async with websockets.connect(self.spoke_url) as websocket:
            self.websocket = websocket

            # 1. Agent → Spoke handshake
            # Send without secret if we don't have one yet (zero-touch provisioning)
            handshake: Dict[str, Any] = {"agent_id": self.agent_id}
            if self.secret:
                handshake["secret"] = self.secret
            await websocket.send(json.dumps(handshake))

            # 2. Hub response — may be APPROVAL_REQUIRED, HUB_VERIFIED, or 1008 close
            try:
                hub_proof = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
            except Exception as exc:
                if "1008" in str(exc) or "policy violation" in str(exc).lower() or "Authentication" in str(exc):
                    raise _AuthError(str(exc)) from exc
                raise

            hub_status = hub_proof.get("status")

            # ── Zero-touch: pending admin approval ────────────────────────────
            if hub_status == "APPROVAL_REQUIRED":
                logger.info(
                    f"Agent '{self.agent_id}' is waiting for admin approval. "
                    "Approve this Proxmox node agent from the LM WebUI: Setup → Spokes & Agents (Agents tile)."
                )
                async for raw in websocket:
                    msg = json.loads(raw)
                    if msg.get("status") == "APPROVED":
                        provisioned_secret = msg.get("secret")
                        if provisioned_secret:
                            logger.info(f"Agent '{self.agent_id}' approved! Reconnecting with provisioned secret.")
                            self.secret = provisioned_secret
                            self.signer = MessageSigner(self.secret)
                            self._save_secret(provisioned_secret)
                        return  # retry loop reconnects with the new secret
                return  # connection closed before approval

            # ── Normal authenticated flow ─────────────────────────────────────
            if hub_status != "HUB_VERIFIED":
                logger.error(f"Spoke failed identity proof: {hub_proof}")
                await websocket.close(1008, "Spoke identity not verified")
                raise _AuthError(f"Spoke identity proof failed: {hub_proof}")

            await websocket.send(json.dumps({"status": "HUB_OK"}))
            logger.info("Spoke identity verified. Auth complete.")

            log_handler = WebSocketLogHandler(self)
            log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            logging.getLogger().addHandler(log_handler)

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            telemetry_task = asyncio.create_task(self._telemetry_loop())

            try:
                async for message in websocket:
                    msg_data = json.loads(message)

                    if "signature" in msg_data and not self.signer.verify(msg_data):
                        logger.warning("Invalid signature — dropping")
                        continue

                    payload  = msg_data.get("payload", {})
                    cmd_type = payload.get("type")
                    data     = payload.get("data", {})
                    corr_id  = msg_data.get("header", {}).get("correlation_id")

                    logger.info(f"Command: {cmd_type}")
                    result = {"status": "ERROR", "message": "Unknown command"}

                    if cmd_type == "UPDATE_CONFIG":
                        old_cs = bool((self.config.get("client_simulation") or {}).get("enabled"))
                        self.config = data
                        new_cs = bool((data.get("client_simulation") or {}).get("enabled"))
                        if old_cs != new_cs:
                            await self._set_cs_enabled(new_cs)
                        result = {"status": "SUCCESS", "message": "Config updated"}

                    elif cmd_type == "GET_VM_LIST":
                        result = await self.get_vm_list()

                    elif cmd_type == "GET_NODE_STATS":
                        result = await self.get_node_stats()

                    elif cmd_type == "GET_SYSTEM_STATS":
                        result = await self.collect_metrics()

                    elif cmd_type == "SET_LOG_LEVEL":
                        level = logging.DEBUG if data.get("enabled") else logging.INFO
                        logging.getLogger().setLevel(level)
                        result = {"status": "SUCCESS",
                                  "message": f"Log level → {logging.getLevelName(level)}"}

                    elif cmd_type == "SHELLEXEC":
                        result = {"status": "ERROR", "message": "SHELLEXEC is disabled"}

                    elif cmd_type == "CS_COMMAND":
                        # Client-Simulation command. Fast commands (start/stop/
                        # reboot/snapshot vm, batches, unlock_template,
                        # clear_provision_lock, clear_usb_quarantine) are sync
                        # (<15s). Long ops (delete_vm, reclone_vm, clone_lxc,
                        # provision_unassigned, backup, reseed, update_agent) use
                        # the accepted+progress+terminal CS_COMMAND_RESULT pattern
                        # (Phase E) — handle_cs_command spawns them and returns
                        # ACCEPTED. Guarded by cs_guard; ERROR if CS is off.
                        result = await cs_commands.handle_cs_command(
                            self, data.get("action"), data or {})

                    elif cmd_type == "CS_CREATE_PROXMOX_TOKEN":
                        # Phase F: hub asks us to create the cs-hub Proxmox API
                        # token (we have local pvesh; the cs spoke doesn't). Ack
                        # accepted immediately; the token task emits
                        # CS_TOKEN_RESULT up (hub → CS_STORE_PROXMOX_TOKEN).
                        request_id = data.get("request_id")
                        task = asyncio.create_task(
                            self._provision_proxmox_token(request_id))
                        self._cs_long_ops.add(task)
                        task.add_done_callback(self._cs_long_ops.discard)
                        result = {"status": "ACCEPTED", "request_id": request_id}

                    resp = {
                        "header": {
                            "message_id":   str(uuid.uuid4()),
                            "correlation_id": corr_id,
                            "timestamp":    time.time(),
                            "sender_id":    self.agent_id,
                            "destination_id": "pxmx-spoke",
                        },
                        "payload": {"type": "AGENT_RESPONSE", "data": result},
                    }
                    resp["signature"] = self.signer.sign(resp)
                    await websocket.send(json.dumps(resp))

            finally:
                heartbeat_task.cancel()
                telemetry_task.cancel()
                logging.getLogger().removeHandler(log_handler)

    async def _heartbeat_loop(self):
        while True:
            try:
                msg = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.agent_id, "destination_id": "pxmx-spoke"},
                    "payload": {"type": "AGENT_HEARTBEAT", "data": {}},
                }
                msg["signature"] = self.signer.sign(msg)
                await self.websocket.send(json.dumps(msg))
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
                await asyncio.sleep(5)

    async def _telemetry_loop(self):
        while True:
            try:
                # Resolve cluster name once after startup
                if not self._cluster_resolved:
                    self.cluster_name = await self._fetch_cluster_name()
                    self._cluster_resolved = True
                    logger.info(f"Cluster name resolved: {self.cluster_name}")

                metrics = await self.collect_metrics()
                vms     = await self.get_vm_list()
                nodes   = await self.get_node_stats()

                if vms.get("error"):
                    logger.error(f"get_vm_list error: {vms['error']}")
                if nodes.get("error"):
                    logger.error(f"get_node_stats error: {nodes['error']}")

                logger.info(f"Telemetry: {len(vms.get('vms', []))} VMs, {len(nodes.get('nodes', []))} nodes")

                msg = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.agent_id, "destination_id": "pxmx-spoke"},
                    "payload": {
                        "type": "AGENT_TELEMETRY",
                        "data": {
                            "metrics":      metrics,
                            "vms":          vms,
                            "nodes":        nodes,
                            "agent_id":     self.agent_id,
                            "hostname":     self.hostname,
                            "cluster_name": self.cluster_name,
                        },
                    },
                }
                msg["signature"] = self.signer.sign(msg)
                await self.websocket.send(json.dumps(msg))

                # ── Client-Simulation telemetry (Phase D1) ───────────────────
                # When CS is enabled, also push a CS_TELEMETRY frame carrying the
                # per-host Proxmox snapshot the cs spoke ingests into its
                # proxmox_states and re-relays as CS_TELEMETRY to the hub, which
                # caches it for the Simulations/VM Server view. Piggybacks on the
                # 60s tick (the bash agent pushed every 3s; HEALTH_STALE_SECS=180
                # gives ample margin). send_cs_event injects hostname + agent_id.
                if self.cs_enabled:
                    try:
                        cs_body = self._cs_telemetry_body(vms, nodes)
                        await self.send_cs_event("CS_TELEMETRY", cs_body)
                    except Exception as e:
                        logger.debug(f"CS_TELEMETRY emit failed: {e}")

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Telemetry push failed: {e}")
                await asyncio.sleep(10)

    def _cs_telemetry_body(self, vms_resp: Dict[str, Any],
                           nodes_resp: Dict[str, Any]) -> Dict[str, Any]:
        """Build the Client-Simulation telemetry body for this host.

        Shaped to mirror the legacy cs bash agent's telemetry body (consumed by
        the cs spoke's ``ProxmoxDeploy.ingest_telemetry``): a ``node`` summary,
        the enriched ``vms`` list, agent/pve versions, the assigned VMID range,
        vm-set/template-lock/provision-halt flags, and USB device state
        (``present_usb``/``unknown_usb``/``usb_state``) from
        ``usb_provision.cs_usb_telemetry`` — a live /sys/bus/usb/devices scan
        classified against the hub-delivered certified/ignored vidpid sets.
        Hostname/agent_id are added by ``send_cs_event``.
        """
        cs_cfg = self.config.get("client_simulation") or {}

        nodes_list = (nodes_resp or {}).get("nodes", []) or []
        first = nodes_list[0] if nodes_list else {}
        mem_used = first.get("mem_used", 0) or 0
        mem_total = first.get("mem_total", 0) or 0
        node = {
            "hostname":     self.hostname,
            "cluster":       self.cluster_name,
            "status":        first.get("status", "unknown"),
            "cpu_percent":   first.get("cpu_usage", 0),
            "mem_used_kb":   int(mem_used / 1024) if mem_used else 0,
            "mem_total_kb":  int(mem_total / 1024) if mem_total else 0,
            "proxmox_version": first.get("proxmox_version", ""),
        }

        def _is_template(v: Dict[str, Any]) -> bool:
            tags = [str(t).lower() for t in (v.get("tags") or [])]
            if any(t in ("template", "tmpl", "is-template") for t in tags):
                return True
            name = str(v.get("name", "")).lower()
            return name.startswith(("template-", "tmpl-"))

        vms = []
        for v in (vms_resp or {}).get("vms", []) or []:
            v = v or {}
            vms.append({
                "vmid":            v.get("vmid"),
                "name":            v.get("name"),
                "status":          v.get("status", "unknown"),
                "type":            v.get("type", "qemu"),
                "cpu":             v.get("cpu", 0),
                "mem":             v.get("mem_bytes", 0) or 0,
                "maxmem":          v.get("mem_bytes", 0) or 0,
                "is_template":     _is_template(v),
                "tags":            v.get("tags", []),
                "node":            v.get("node", ""),
                "_agent_hostname": self.hostname,
            })

        vr = cs_cfg.get("vmid_range") or {}
        vmid_range = {
            "start": int(vr.get("start", 90000)) if vr.get("start") is not None else 90000,
            "end":   int(vr.get("end", 99999)) if vr.get("end") is not None else 99999,
        }

        # USB passthrough detail: scan /sys/bus/usb/devices and classify against
        # the hub-delivered certified/ignored vidpid sets. Best-effort → empty
        # lists on any failure (the cs spoke tolerates empty).
        try:
            usb = usb_provision.cs_usb_telemetry(self)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cs telemetry: usb scan failed: %s", exc)
            usb = {"usb_state": [], "present_usb": [], "unknown_usb": []}

        return {
            "node":             node,
            "vms":              vms,
            "agent_version":    get_version(),
            "pve_version":      first.get("proxmox_version", ""),
            "vmid_range":       vmid_range,
            "vm_set_override":  int(cs_cfg.get("vm_set_override", 0) or 0),
            "effective_vm_set": max(1, int(cs_cfg.get("effective_vm_set", 1) or 1)),
            "template_lock":    str(cs_cfg.get("template_lock", "") or ""),
            "provision_halt":   usb_provision.current_provision_halt(),
            "prov_run":         usb_provision.current_prov_run(),
            "usb_state":        usb.get("usb_state") or [],
            "present_usb":      usb.get("present_usb") or [],
            "unknown_usb":      usb.get("unknown_usb") or [],
        }

    # ── Client Simulation mode actuation ──────────────────────────────────────
    # The hub pushes self.config["client_simulation"] down via UPDATE_CONFIG
    # (see the UPDATE_CONFIG handler above). When the enabled flag flips we start
    # or stop the CS background task group. Phase A only logs the transition;
    # later phases spawn the real tasks (_cs_telemetry_loop, _usb_provision_loop,
    # _hw_watchdog_loop, _vm_agent_watchdog_loop) inside _start_cs_tasks.

    async def _set_cs_enabled(self, enabled: bool) -> None:
        if enabled and not self.cs_enabled:
            self.cs_enabled = True
            cs_cfg = self.config.get("client_simulation") or {}
            logger.info(f"client_simulation enabled=true (tenant={cs_cfg.get('tenant_id')})")
            await self._start_cs_tasks()
        elif not enabled and self.cs_enabled:
            self.cs_enabled = False
            logger.info("client_simulation disabled")
            await self._stop_cs_tasks()

    async def _start_cs_tasks(self) -> None:
        """Spawn the Client Simulation background tasks (Phases C/E).

        - hw_watchdog_loop: kernel-journal Tier-1/Tier-2 hardware fault scan.
        - vm_agent_watchdog_loop: per sim-VM guest-agent ping → warn → soft reboot.
        - _usb_blacklist_loop: periodically (re)writes the dongle-driver modprobe
          blacklist so the host kernel never grabs passthrough dongles.
        - _usb_provision_loop: scan → reconcile → tear down missing-dongle VMs →
          clone+provision unassigned dongles (Phase E). No-ops cleanly when no
          dongle_vidpids / templates are configured.
        Each loop no-ops cleanly (logs + returns) when its feature is disabled or
        its precondition is absent, so spawning them unconditionally on CS-enable
        is safe.
        """
        self._cs_tasks.add(asyncio.create_task(watchdogs.hw_watchdog_loop(self)))
        self._cs_tasks.add(asyncio.create_task(watchdogs.vm_agent_watchdog_loop(self)))
        self._cs_tasks.add(asyncio.create_task(self._usb_blacklist_loop()))
        self._cs_tasks.add(asyncio.create_task(self._usb_provision_loop()))
        logger.info("CS task group started: hw_watchdog, vm_agent_watchdog, "
                    "usb_blacklist, usb_provision")

    async def _usb_blacklist_loop(self) -> None:
        """Re-apply the dongle-driver blacklist every 5 min. Idempotent
        (usb_provision.blacklist_dongle_drivers only writes on diff) and a no-op
        when no dongle_vidpids are configured (pre-Phase D)."""
        while True:
            try:
                await usb_provision.blacklist_dongle_drivers(self)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"usb_blacklist_loop: {e}")
            await asyncio.sleep(300)

    async def _usb_provision_loop(self) -> None:
        """Periodic USB-provision pass (Phase E). No-ops when no dongle_vidpids
        are configured. Interval defaults to 60s (env USB_PROVISION_INTERVAL_S)."""
        interval = 60
        try:
            interval = max(15, int(os.environ.get("USB_PROVISION_INTERVAL_S", "60")))
        except Exception:
            pass
        await asyncio.sleep(10)  # let the first telemetry/config settle
        while True:
            try:
                # Feed the rolling 1h cpu/mem window the auto-provision brain
                # gates on (cs _record_resource_samples). Sampled on the same
                # cadence as the provision loop.
                usb_provision.sample_resources(self)
                await usb_provision.run_provision_loop(self)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"usb_provision_loop: {e}")
            await asyncio.sleep(interval)

    async def _stop_cs_tasks(self) -> None:
        """Cancel every running CS background task + any in-flight long op /
        token-provision task and clear the groups. A disabled host stops
        mutating VMs immediately."""
        for task in list(self._cs_tasks):
            task.cancel()
        self._cs_tasks.clear()
        for task in list(self._cs_long_ops):
            task.cancel()
        self._cs_long_ops.clear()
        logger.info("CS task group stopped (background + long ops)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--spoke-url", required=True)
    parser.add_argument("--id", default="pxmx-agent-1")
    parser.add_argument("--secret")
    args = parser.parse_args()

    try:
        agent = ProxmoxAgent(args.spoke_url, args.id, args.secret)
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
