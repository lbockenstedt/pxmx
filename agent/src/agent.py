"""pxmx Proxmox host agent — ``ProxmoxAgent``.

Runs **on** a Proxmox node and is the only component with ``qm``/``pct``
clone/destroy access, so all VM-mutating work happens here. It connects (over
WS, via the pxmx spoke/control plane — ``wss://<spoke>:443/ws/agent`` standalone
default; ``ws://127.0.0.1:8443`` loopback via the hub ``/ws/agent`` byte-proxy;
``ws://:8766`` legacy no-cert fallback) and:

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

# ── Dependency self-heal (must run BEFORE the third-party imports below) ──────
# A skewed auto-update / partial install can leave the venv missing a declared
# dep (e.g. psutil — the generic-agent crash-loop root cause) → hard crash at
# `import psutil` below, crash-looping the agent under Restart=always. dep_guard
# is stdlib-only (vendored as a sibling so it imports with no third-party deps
# and no lm-core dependency — keep in sync with lm/core/src/dep_guard.py); it
# parses agent/requirements.txt, find_spec-checks each top-level package, and
# runs `pip install -r` in this venv if any are missing. LM_DEP_GUARD_DISABLE=1
# opts out.
from .dep_guard import ensure_requirements as _ensure_requirements
import os as _os
_req = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                     "requirements.txt")
_ensure_requirements(_req)
del _os, _ensure_requirements, _req

import asyncio
import base64
import collections
import hashlib
import json
import re
import ssl
import uuid
import time
import logging
import psutil
import argparse
import os
import socket
import sys
import tempfile
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit
from .security_utils import MessageSigner, encode_frame, split_frame
from . import cs_commands
from . import cs_sim
from . import watchdogs
from . import usb_provision
from . import pve_cmds
from . import managed_crontab
from . import console_relay
from . import template_ops
from . import vm_inventory

class _AuthError(Exception):
    """Raised when the spoke rejects our credentials (wrong secret)."""

class WebSocketLogHandler(logging.Handler):
    """Relays agent log records (INFO+, own loggers only) to the hub via
    AGENT_LOG — so Setup → Agent Logs shows them AND the BugFixer module can
    read agent errors without anyone touching the box's CLI. This is a hard
    requirement: once the agent is connected, the hub must see its logs.

    Installed ONCE for the agent's lifetime (not per-connection). While the
    socket is down (startup, between reconnects) records are buffered in a
    bounded ring and flushed on the next connect, so nothing logged during a
    gap is lost — the previous per-connection add/remove dropped every record
    outside an active connection (startup + disconnect windows)."""

    # Only relay records from loggers whose names start with these prefixes.
    # HubDiscovery is included so the agent's same-box-vs-remote / ws-vs-wss
    # decision (the key TLS troubleshooting fact) reaches the hub via AGENT_LOG,
    # not just the local pxmx-agent.log.
    _RELAY_PREFIXES = ("PxmxAgent", "ProxmoxAgent", "HubDiscovery")

    def __init__(self, agent, buffer_size: int = 1000):
        super().__init__(level=logging.INFO)
        self.agent = agent
        self._buffer: "collections.deque" = collections.deque(maxlen=buffer_size)

    def emit(self, record):
        if not record.name.startswith(self._RELAY_PREFIXES):
            return
        try:
            item = (self.format(record), record.levelname)
        except Exception:
            self.handleError(record)
            return
        # Connected → send now; disconnected (or no running loop yet) → buffer
        # for flush on the next connect so the hub still receives it.
        if self.agent.websocket is not None:
            if not self._dispatch(item):
                self._buffer.append(item)
        else:
            self._buffer.append(item)

    def _dispatch(self, item) -> bool:
        """Schedule a send on the running loop. Returns False (caller buffers)
        when there is no running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(self.agent.send_log(item[0], item[1]))
        return True

    def flush_buffered(self) -> None:
        """Drain buffered records after a (re)connect. Called from the async
        connect flow (running loop present). Best-effort: send_log itself never
        raises, so a still-flaky socket just re-drops rather than blocking."""
        while self._buffer:
            self._dispatch(self._buffer.popleft())

def get_log_path():
    """Resolve the agent log file path.

    Logs under ``/var/log/lm`` alongside the hub + spokes (the pxmx installer
    creates /var/log/lm and chowns it so the systemd service can write here).
    Falls back to a local ``logs/`` dir if that path isn't writable (e.g. run
    by hand as an unprivileged user without the install step).
    """
    primary = "/var/log/lm/pxmx-agent.log"
    try:
        with open(primary, "a") as f:
            pass
        return primary
    except Exception:
        local_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(local_dir, exist_ok=True)
        return os.path.join(local_dir, "pxmx-agent.log")

try:
    from logging_setup import configure_logging, set_log_level
except ImportError:
    try:
        from core.src.logging_setup import configure_logging, set_log_level
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
        def set_log_level(enabled):
            level = _logging.DEBUG if enabled else _logging.INFO
            _logging.getLogger().setLevel(level)
            for _n in list(_logging.root.manager.loggerDict):
                _logging.getLogger(_n).setLevel(level)
            return level
_log_path = get_log_path()
configure_logging(log_file=_log_path)
# When writing to the canonical /var/log/lm file, the systemd unit captures
# stderr into the SAME file (StandardError=append:/var/log/lm/pxmx-agent.log).
# configure_logging attaches a stderr StreamHandler alongside the FileHandler,
# which would write every record twice into that one file. Drop the stderr
# StreamHandler in that case so each record lands once (the FileHandler writes
# it; raw interpreter tracebacks still reach the file via systemd's stderr
# capture). Keep the StreamHandler for the local fallback path so a manual run
# still shows on the console.
if _log_path == "/var/log/lm/pxmx-agent.log":
    for _h in list(logging.getLogger().handlers):
        if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
            logging.getLogger().removeHandler(_h)
logger = logging.getLogger("PxmxAgent")


# Per-agent update-recovery state dir. Separate from the hub's /var/lib/lm/state
# and the spokes' /var/lib/lm/<spoke_id>/ so a co-located box never collides.
# The external health-gate watchdog (lm-component-update-restart) reads the
# pending manifest + healthy marker here and rolls back a failed self-update.
AGENT_STATE_DIR = os.environ.get("LM_PXMX_STATE_DIR", "/var/lib/pxmx/update-state")

# Last hub-pushed agent config, persisted so a self-update restart (or any
# restart) re-enters client-simulation mode from last-known config instead of
# sitting idle until the hub happens to re-push UPDATE_CONFIG — which, on a
# co-located/loaded box, can be lost in the spoke's request backlog and leave
# auto-provisioning silently off. Root-only (same dir as usb_state.json).
AGENT_CONFIG_FILE = os.environ.get(
    "LM_PXMX_CONFIG_FILE", "/var/lib/pxmx/agent_config.json")

_AGENT_WS_PATH = "/ws/agent"
_AGENT_DEFAULT_SCHEME = "wss"
_AGENT_DEFAULT_PORT = "443"


def _normalize_spoke_url(url: Optional[str]) -> Optional[str]:
    """Fill in a pinned ``spoke_url``'s scheme/port/path with sane defaults.

    ``websockets.connect()`` dials whatever URL it's given verbatim — no
    rewriting. Auto-discovery (``discovery.discover_hub_url``) already builds a
    fully-formed ``wss://host:443/ws/agent`` URL itself, but a manually-pinned
    ``--spoke-url``/``SPOKE_URL`` is often given as just a bare host or
    ``host:port`` (missing scheme and/or the ``/ws/agent`` path). Rather than
    connect to the wrong thing and fail with a cryptic
    ``1008 policy violation: unexpected path`` (or a bare-TCP scheme error),
    default each missing piece: scheme -> ``wss``, port -> ``443``,
    path -> ``/ws/agent``. Fixing it up front means it's simply never wrong to
    begin with, instead of retrying reactively after a connection failure.
    Idempotent: an already-fully-specified URL passes through unchanged;
    ``None``/``""``/``"auto"`` are left alone (the auto-discovery sentinel).
    """
    if not url or url == "auto":
        return url
    raw = url.strip()
    if "://" not in raw:
        raw = f"{_AGENT_DEFAULT_SCHEME}://{raw}"
    parts = urlsplit(raw)
    scheme = parts.scheme or _AGENT_DEFAULT_SCHEME
    netloc = parts.netloc
    # No port on the netloc (ignoring a bracketed IPv6 host's own colons) ->
    # default to 443. A bare "host" with no "://" at all lands here too, since
    # urlsplit puts everything after a missing scheme into .path, not .netloc
    # — handled above by prepending the default scheme first.
    host_part = netloc.rsplit("]", 1)[-1] if netloc else netloc
    if netloc and ":" not in host_part:
        netloc = f"{netloc}:{_AGENT_DEFAULT_PORT}"
    path = parts.path.rstrip("/")
    if not path.endswith(_AGENT_WS_PATH):
        path = _AGENT_WS_PATH
    return urlunsplit((scheme, netloc, path, "", ""))


def _deep_merge_config(base, incoming):
    """Recursively merge ``incoming`` into a copy of ``base``: nested dicts are
    merged key-by-key, every other value (scalars, lists) is replaced wholesale.

    UPDATE_CONFIG arrives from TWO sources with DIFFERENT partial payloads — the
    UI/agent-config save (``{client_simulation:{enabled,tenant_id}}``) and the
    hub CS bridge (``{client_simulation:{enabled,tenant_id,usb_config:{...}}}``).
    A blind ``self.config = data`` let each push clobber the other's keys — the
    enabled/tenant-only save wiped ``client_simulation.usb_config.vidpids`` and
    the provision loop then reported "no dongle_vidpids configured" until the
    next bridge push. Merging keeps sibling sub-trees intact; ``vidpids`` (a
    list) is still replaced whole, so removing a dongle type still takes effect.
    """
    if not isinstance(base, dict) or not isinstance(incoming, dict):
        return incoming
    out = dict(base)
    for k, v in incoming.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge_config(out[k], v)
        else:
            out[k] = v
    return out


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
    """Return the pxmx agent version from the VERSION file, or ``"unknown"``.

    Searches candidate paths in priority order. The self-update git checkout
    (``.pxmx_repo/VERSION``) is checked FIRST: it's a tracked file refreshed to
    the current ``.NN`` on every ``git pull``, so an agent originally installed
    BEFORE the .NN migration (whose ``install_dir/VERSION`` is a stale old-
    format copy that self-update never overwrote) still reports the current
    .NN instead of the stale value. Falls back to the install_dir copy, the
    source tree (dev), then CWD."""
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(here, "..", ".pxmx_repo", "VERSION"),  # self-update checkout — current .NN
        os.path.join(here, "..", "VERSION"),                 # install_dir/VERSION (install_agent.sh copy)
        os.path.join(here, "..", "..", "VERSION"),           # dev source tree: pxmx/VERSION
        "VERSION",                                           # CWD (last resort)
    ]
    for p in paths:
        try:
            if os.path.exists(p):
                with open(p) as f:
                    v = f.read().strip()
                    if v:
                        return v
        except Exception:
            pass
    return "unknown"

version = get_version()


class ProxmoxAgent:
    """The pxmx host agent — runs on a Proxmox node and owns all VM-mutating work.

    Connects to the pxmx spoke agent listener (``wss://<spoke>:443/ws/agent``
    standalone default, or ``ws://127.0.0.1:8443`` loopback reached via the
    hub ``/ws/agent`` byte-proxy; ``ws://:8766`` is the legacy no-cert
    fallback), authenticates, then runs the telemetry/USB-provision/cs-
    command/watchdog loops. See the module docstring.
    """

    def __init__(self, spoke_url: str, agent_id: str, secret: Optional[str] = None,
                 spoke_ip: Optional[str] = None):
        # Two ways to point the agent at its spoke:
        #   * spoke_ip  — the operator supplies ONLY an IP/host; the agent probes
        #     the known /ws/agent listener endpoints (wss:443, ws:8767, …) at
        #     startup and picks the live one, so scheme/port/path are auto-
        #     determined (see _resolve_spoke_url → discovery.resolve_agent_url).
        #   * spoke_url — a fully-pinned ws(s)://host:port/ws/agent (legacy /
        #     power-user). A concrete spoke_url always wins over spoke_ip.
        # When only spoke_ip is given we start on the resolve sentinel ("") so
        # run() → _resolve_spoke_url does the probing (and re-probes on failure).
        self.spoke_ip = (spoke_ip or "").strip() or None
        if spoke_url:
            self.spoke_url = _normalize_spoke_url(spoke_url)   # explicit pin wins
        elif self.spoke_ip:
            self.spoke_url = ""                                 # probe in _resolve_spoke_url
        else:
            self.spoke_url = _normalize_spoke_url(spoke_url)    # None/"" → hub auto-discovery
        self.agent_id = agent_id
        self.secret = secret or self._load_secret()
        # No secret is OK — we will connect without one and wait for approval.

        self.websocket = None
        # Seed from the last hub-pushed config so a restart (esp. a self-update)
        # can resume client-simulation mode immediately instead of idling until
        # the hub re-pushes UPDATE_CONFIG. Refreshed on every UPDATE_CONFIG.
        self.config: Dict[str, Any] = self._load_persisted_config()
        self.signer = MessageSigner(self.secret or "")
        self.hostname = socket.gethostname()
        self.agent_type = "pxmx-agent"
        # Stable install UUID (minted at first start, persisted to .env) + the
        # current OS hostname are sent on every connect so the hub can detect a
        # clone-and-rename of this Proxmox node and carry over its agent config
        # rather than treating it as a brand-new agent. prep-for-imaging strips
        # INSTALL_UUID from .env so a cloned node mints a fresh identity.
        self.install_uuid = self._ensure_install_uuid()

        # Proxmox cluster name — resolved on first telemetry push; defaults to hostname.
        self.cluster_name: str = self.hostname
        self._cluster_resolved: bool = False
        # 5s TTL memo for /cluster/resources: the telemetry loop calls
        # get_vm_list() and get_node_stats() back-to-back every ~60s, and BOTH
        # issued a separate pvesh /cluster/resources round-trip — doubling the
        # work each tick. The 5s TTL is short enough to expire before the next
        # 60s tick (no cross-tick staleness) but long enough to let the second
        # caller in a tick reuse the first's fetch.
        self._cluster_resources_cache: Optional[list] = None
        self._cluster_resources_ts: float = 0.0
        self._cluster_resources_ttl: float = 5.0

        # Prime psutil's non-blocking CPU sampler. cpu_percent(interval=None)
        # measures since the PREVIOUS call, so the first post-prime reading in
        # collect_metrics reflects usage since this prime (startup→first tick).
        # This lets collect_metrics use interval=None instead of interval=1,
        # which BLOCKED the whole agent event loop for 1s per telemetry tick.
        psutil.cpu_percent(interval=None)

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

        # ── VNC console (agent-terminates-WSS) ──────────────────────────────
        # Per-session state for browser→Proxmox VNC relays. session_id →
        # {"down_q": asyncio.Queue, "px_ws": websockets conn | None, "tasks": []}.
        # down_q buffers browser frames until the Proxmox WSS is open, then the
        # drain task forwards them. The console API token (root@pam!lm-vnc) is
        # provisioned once via local pvesh and cached in memory for the agent's
        # lifetime — its secret is NEVER logged (only its existence).
        self._vnc_sessions: Dict[str, Dict[str, Any]] = {}
        self._console_token: Optional[str] = None
        # Interactive host-shell (xterm terminal) sessions — {session_id: {master_fd,
        # proc, tasks}}. A PTY bash on THIS node, relayed to the browser like VNC.
        self._shell_sessions: Dict[str, Dict[str, Any]] = {}
        # Cached root@pam!cs-hub Proxmox API token (created locally via pvesh,
        # relayed up for the cs spoke's sim-tag sync). Provisioned once + cached
        # for the agent's lifetime, re-emitted on later triggers. See
        # _ensure_cs_hub_token.
        self._cs_hub_token: Optional[str] = None

        # Hub log relay — installed ONCE here (not per-connection) so records
        # from the very first startup line onward are captured; buffered while
        # the socket is down and flushed on each connect. Requirement: the hub
        # must have every agent's logs (Setup → Agent Logs + BugFixer) without
        # needing the box's CLI.
        self._ws_log_handler = WebSocketLogHandler(self)
        # Match the canonical format/datefmt used by configure_logging (see
        # logging_setup.DEFAULT_FORMAT/DEFAULT_DATEFMT) so log lines streamed to
        # the hub WebUI Agent Logs view are byte-identical in shape to the
        # journal/file lines. Without datefmt, asctime falls back to the logging
        # default which appends milliseconds (...,123) — inconsistent with every
        # other log surface in the fleet.
        self._ws_log_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                              datefmt='%Y-%m-%d %H:%M:%S'))
        logging.getLogger().addHandler(self._ws_log_handler)
        self._install_uncaught_exception_relay()

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

    def _env_path(self) -> str:
        """Absolute path to the agent .env (/opt/lm/pxmx/agent/.env in production)."""
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))

    def _load_persisted_config(self) -> Dict[str, Any]:
        """Load the last hub-pushed agent config from disk (empty dict if none
        or unreadable). Lets a restart resume client-simulation mode without
        waiting for the hub to re-push UPDATE_CONFIG."""
        try:
            if os.path.exists(AGENT_CONFIG_FILE) and os.path.getsize(AGENT_CONFIG_FILE) > 0:
                with open(AGENT_CONFIG_FILE) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load persisted config {AGENT_CONFIG_FILE}: {e}")
        return {}

    def _save_persisted_config(self, config: Dict[str, Any]) -> None:
        """Persist the latest hub-pushed agent config (best-effort, root-only)."""
        try:
            os.makedirs(os.path.dirname(AGENT_CONFIG_FILE), exist_ok=True)
            tmp = AGENT_CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f)
            os.replace(tmp, AGENT_CONFIG_FILE)
        except OSError as e:
            logger.warning(f"Could not persist config {AGENT_CONFIG_FILE}: {e}")

    def _install_uncaught_exception_relay(self) -> None:
        """Route uncaught *synchronous* exceptions through the PxmxAgent logger
        (which relays to the hub) before the interpreter's default handler runs,
        so a crash's traceback reaches Setup → Agent Logs + BugFixer, not only
        the local file via systemd stderr capture. The asyncio-task counterpart
        is installed in run() once the loop exists."""
        _prev = sys.excepthook

        def _hook(exc_type, exc, tb):
            try:
                if not issubclass(exc_type, KeyboardInterrupt):
                    logger.error("Uncaught exception", exc_info=(exc_type, exc, tb))
            finally:
                _prev(exc_type, exc, tb)

        sys.excepthook = _hook

    def _asyncio_exception_relay(self, loop, context) -> None:
        """asyncio loop exception handler — logs unhandled task exceptions via
        the PxmxAgent logger so they relay to the hub, then defers to the
        default handler for local reporting."""
        exc = context.get("exception")
        msg = context.get("message") or "unhandled asyncio exception"
        if exc is not None:
            logger.error("Uncaught asyncio exception: %s", msg, exc_info=exc)
        else:
            logger.error("asyncio error: %s", msg)
        loop.default_exception_handler(context)

    def _machine_fingerprint(self) -> str:
        """Stable per-MACHINE id, used to detect a cloned .env (identity copied
        onto a different physical box). /etc/machine-id first (regenerated per
        clone by most provisioning), then the SMBIOS/DMI product UUID. '' if
        neither is readable — binding is skipped (fail-open, never blind re-mint)."""
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id",
                     "/sys/class/dmi/id/product_uuid"):
            try:
                with open(path) as f:
                    v = f.read().strip()
                if v:
                    return v
            except Exception:
                continue
        return ""

    def _env_get(self, key: str) -> str:
        env_path = self._env_path()
        try:
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        if line.startswith(f"{key}="):
                            return line.split("=", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _env_upsert(self, updates: dict) -> None:
        """Write/replace each key in ``updates`` in .env (create the file/keys if absent)."""
        env_path = self._env_path()
        lines = []
        if os.path.exists(env_path):
            with open(env_path) as f:
                lines = [l for l in f if not any(l.startswith(f"{k}=") for k in updates)]
        for k, v in updates.items():
            lines.append(f"{k}={v}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)

    def _ensure_install_uuid(self) -> str:
        """Return this agent's stable install UUID, minting + persisting it on first start.

        MACHINE-BOUND (mirrors BaseControlPlane._ensure_install_uuid): the UUID is
        pinned to this box's machine fingerprint via ``INSTALL_UUID_MACHINE``. A VM
        clone copies the whole .env, so without this a cloned Proxmox host would
        present the ORIGIN's agent UUID and the hub would collapse both onto one
        hypervisor (the "N agents, one UUID" trap). If a cloned .env lands on a
        DIFFERENT machine (stored fingerprint != this box), we mint a FRESH UUID so
        the clone registers as its own agent. A missing stored fingerprint on an
        existing UUID = a pre-binding install: backfill the fingerprint, keep UUID.
        We trust only what lands on disk: a failed write returns '' (no correlation).
        """
        try:
            cur_fp = self._machine_fingerprint()
            existing = self._env_get("INSTALL_UUID")
            if existing:
                stored_fp = self._env_get("INSTALL_UUID_MACHINE")
                if cur_fp and stored_fp and stored_fp != cur_fp:
                    logger.warning(
                        "INSTALL_UUID %s… was minted on a different machine "
                        "(fingerprint %s… != this box %s…) — cloned .env detected; "
                        "minting a fresh agent identity so this clone does not step "
                        "on the origin hypervisor.",
                        existing[:8], stored_fp[:8], cur_fp[:8])
                    new_uuid = str(uuid.uuid4())
                    self._env_upsert({"INSTALL_UUID": new_uuid,
                                      "INSTALL_UUID_MACHINE": cur_fp})
                    return self._env_get("INSTALL_UUID")
                if cur_fp and not stored_fp:
                    self._env_upsert({"INSTALL_UUID_MACHINE": cur_fp})
                return existing
            new_uuid = str(uuid.uuid4())
            upd = {"INSTALL_UUID": new_uuid}
            if cur_fp:
                upd["INSTALL_UUID_MACHINE"] = cur_fp
            self._env_upsert(upd)
            logger.info(f"Install UUID minted and saved to {self._env_path()}")
            return self._env_get("INSTALL_UUID")
        except Exception as e:
            logger.warning(f"Could not ensure INSTALL_UUID in .env: {e}")
            return ""

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
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # MUST kill the child on timeout AND on cancellation. Callers wrap
            # this in an OUTER wait_for (vm_interfaces: 4s per VM;
            # annotate_vm_interfaces: 12s for the whole gather) — when that outer
            # deadline fires it cancels us here, and a bare wait_for leaves the
            # pvesh child running. A QGA query to an unresponsive/booting guest
            # blocks for a long time, so every telemetry tick would orphan pvesh
            # processes that pile up, saturate host CPU (100%), and slow ALL
            # pvesh — the runaway behind the fleet-wide 10-20 min telemetry lag.
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            raise
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
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()  # don't orphan a hung/slow pvesh on timeout/cancel
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            raise
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode().strip() or f"pvesh {verb} exited {proc.returncode}")
        if not json_out:
            return stdout.decode()
        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return stdout.decode()

    def _pvenode_bin(self) -> str:
        """Locate the pvenode binary (same pve-manager package as pvesh)."""
        for candidate in ["/usr/bin/pvenode", "/usr/sbin/pvenode", "pvenode"]:
            if candidate == "pvenode":
                return candidate  # fall back to PATH
            if os.path.isfile(candidate):
                return candidate
        return "pvenode"

    # pvenode writes the cert files FIRST, then restarts pveproxy. On a loaded
    # node the restart can take many minutes — we can't predict how fast the
    # cert will install/restart, so we (a) give pvenode a generous wait, and
    # (b) on timeout verify the deployed cert by fingerprint instead of trusting
    # the process exit. The cert is "deployed" once it's on disk; pveproxy
    # finishes reloading on its own. See _pveproxy_cert_matches.
    _PVEPROXY_CERT_PATH = "/etc/pve/local/pveproxy-ssl.pem"
    _PVENODE_WAIT_TIMEOUT = 600.0  # 10 min — generous upper bound for a slow pveproxy restart

    @staticmethod
    def _leaf_der_fingerprint(pem_text: str) -> Optional[str]:
        """SHA256 of the first (leaf) cert's DER in a PEM cert/chain, or None.
        Comparing fingerprints (not raw bytes) is robust to any whitespace /
        newline normalization pvenode does when it writes pveproxy-ssl.pem."""
        try:
            m = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                          pem_text, re.S)
            if not m:
                return None
            der = ssl.PEM_cert_to_DER_cert(m.group(0))
            return hashlib.sha256(der).hexdigest() if der else None
        except Exception:
            return None

    def _pveproxy_cert_matches(self, fullchain: str) -> bool:
        """True if the deployed pveproxy cert matches ``fullchain``'s leaf (by
        SHA256 of the DER). pvenode writes the cert file BEFORE restarting
        pveproxy, so this is the authoritative 'is the cert actually deployed'
        check — independent of whether the pvenode process finished or timed
        out during a slow restart. Returns False if the file is absent or
        unreadable (not a Proxmox node, or pvenode hasn't written it yet)."""
        want = self._leaf_der_fingerprint(fullchain)
        if not want:
            return False
        try:
            with open(self._PVEPROXY_CERT_PATH, "rb") as f:
                got = self._leaf_der_fingerprint(f.read().decode(errors="replace"))
        except OSError:
            return False
        return bool(got) and want == got

    async def install_cert(self, fullchain: str, privkey: str,
                           node: str = "") -> Dict[str, Any]:
        """Install a custom TLS cert on this node's pveproxy (the Proxmox web UI
        + API listener). The le spoke (via the hub) supplies the PEM fullchain +
        unencrypted privkey; we write them to root-only temp files and call
        ``pvenode cert set <cert> <key> --force --restart``, which writes
        ``/etc/pve/local/pveproxy-ssl.{pem,key}`` and restarts pveproxy — the
        same endpoint as the WebUI's Node→Certificates→Upload Custom Certificate.

        The agent runs ON a Proxmox node, so it installs on its local node only
        (``self.hostname``); the spoke routes INSTALL_CERT to the agent that
        owns the target node. ``node`` is accepted for a sanity check + log
        clarity but pvenode has no --node flag (it is inherently local).

        The private key is written to a 0600 temp file pvenode reads, then
        unlinked — it is never logged (mirrors the Proxmox-token-secret rule).

        Success detection: we can't predict how fast the cert will transfer or
        how long pveproxy's restart will take, so we don't fail a deploy solely
        on a timeout. pvenode writes the cert files BEFORE restarting pveproxy,
        so if the wait times out during a slow restart we verify the deployed
        cert by fingerprint — SUCCESS if it matches (pveproxy finishes reloading
        on its own), ERROR only if the cert genuinely isn't on disk."""
        fullchain = (fullchain or "").strip()
        privkey = (privkey or "").strip()
        if not fullchain or "BEGIN CERTIFICATE" not in fullchain:
            return {"status": "ERROR", "message": "missing or invalid fullchain PEM"}
        if not privkey or "PRIVATE KEY" not in privkey:
            return {"status": "ERROR", "message": "missing or invalid private key PEM"}
        if node and node.lower() != self.hostname.lower():
            logger.warning("INSTALL_CERT: requested node '%s' != local '%s'; "
                           "pvenode installs on the local node only",
                           node, self.hostname)

        # Idempotent: if pveproxy is ALREADY serving this exact cert (fingerprint
        # match), skip pvenode + the pveproxy restart entirely and report SUCCESS
        # now. A re-push — the hub retrying after a lost ack on a flaky link, or
        # an overlapping target hitting the same node — then costs nothing and,
        # crucially, does NOT restart pveproxy again. That removes the "always
        # deploying / keeps restarting" churn, and the fast SUCCESS is far more
        # likely to reach the hub before the connection blips (settling the le
        # ledger so the retry loop stops). Only a genuinely NEW cert runs
        # `pvenode cert set --restart` below.
        if self._pveproxy_cert_matches(fullchain):
            logger.info("INSTALL_CERT: pveproxy already serving this cert on %s — "
                        "no-op (idempotent, no restart)", self.hostname)
            return {"status": "SUCCESS",
                    "message": f"cert already deployed on {self.hostname} "
                               f"(unchanged — no restart)"}

        cert_fd, cert_path = tempfile.mkstemp(prefix="pve-cert-", suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(prefix="pve-key-", suffix=".pem")
        try:
            os.write(cert_fd, fullchain.encode())
            os.close(cert_fd)
            os.write(key_fd, privkey.encode())
            os.close(key_fd)
            os.chmod(cert_path, 0o600)
            os.chmod(key_path, 0o600)
            bin_ = self._pvenode_bin()
            proc = await asyncio.create_subprocess_exec(
                bin_, "cert", "set", cert_path, key_path, "--force", "--restart",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._PVENODE_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                # Slow pveproxy restart. The cert files are written before the
                # restart, so verify the deploy instead of trusting the wait:
                # if the cert is on disk, pveproxy will finish reloading on its
                # own — report SUCCESS. Kill the lingering pvenode so it can't
                # wedge a subsequent install.
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                if self._pveproxy_cert_matches(fullchain):
                    logger.info("INSTALL_CERT: pveproxy cert installed on %s "
                                "(fingerprint verified after slow restart)", self.hostname)
                    return {"status": "SUCCESS",
                            "message": f"cert installed on {self.hostname} "
                                       f"(pveproxy cert verified on disk; restart was slow)"}
                logger.warning("INSTALL_CERT: pvenode timed out on %s and cert not on disk",
                               self.hostname)
                return {"status": "ERROR",
                        "message": "pvenode cert set timed out and cert not on disk "
                                   "(pveproxy restart hung?)"}
            if proc.returncode != 0:
                err = (stderr.decode().strip() or stdout.decode().strip()
                       or f"pvenode exited {proc.returncode}")
                # pvenode writes the cert files BEFORE restarting pveproxy, so a
                # non-zero exit is usually a slow/among-warning pveproxy restart
                # (e.g. `command 'systemctl restart pveproxy' failed` while the
                # cert IS already on disk), not a failed cert write. Verify by
                # fingerprint before failing — same authoritative check as the
                # timeout path: if the cert is deployed, pveproxy finishes
                # reloading on its own, so report SUCCESS (the deploy succeeded).
                # This is the "cert landed on the node but the UI shows failed"
                # symptom. Only a cert that genuinely isn't on disk is an ERROR.
                if self._pveproxy_cert_matches(fullchain):
                    logger.info("INSTALL_CERT: pvenode exited %s on %s but cert "
                                "verified on disk — treating as installed (%s)",
                                proc.returncode, self.hostname, err[:200])
                    return {"status": "SUCCESS",
                            "message": f"cert installed on {self.hostname} "
                                       f"(verified on disk; pvenode reported: {err[:200]})"}
                logger.warning("INSTALL_CERT: pvenode failed on %s and cert not on "
                               "disk — %s", self.hostname, err[:200])
                return {"status": "ERROR",
                        "message": f"pvenode cert set failed: {err[:300]}"}
            logger.info("INSTALL_CERT: pveproxy cert installed on %s (pvenode restart)",
                        self.hostname)
            return {"status": "SUCCESS",
                    "message": f"cert installed on {self.hostname} (pveproxy restarted)"}
        except FileNotFoundError:
            return {"status": "ERROR", "message": "pvenode not found (not a Proxmox node?)"}
        except Exception as e:  # noqa: BLE001
            return {"status": "ERROR", "message": f"install_cert failed: {str(e)[:300]}"}
        finally:
            for p in (cert_path, key_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # ── Template backup + refresh ─────────────────────────────────────────────
    # Extracted to template_ops.py (free functions); these thin wrappers keep the
    # _connect_once dispatch chain (self.<method>) untouched.
    def _start_template_backup(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return template_ops.start_template_backup(data)

    def _start_template_refresh(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return template_ops.start_template_refresh(self, data)

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
        """Agent host OS metrics. cpu_percent uses interval=None (non-blocking,
        since-last-call) — primed once in __init__; interval=1 stalled the event
        loop 1s per telemetry tick."""
        return {
            "cpu_usage":    psutil.cpu_percent(interval=None),
            "memory_usage": psutil.virtual_memory().percent,
            "disk_usage":   psutil.disk_usage('/').percent,
            "timestamp":    time.time(),
        }

    async def _cluster_resources(self) -> list:
        """``/cluster/resources`` with a 5s TTL — get_vm_list + get_node_stats
        both consume this view and previously each issued its own pvesh round-
        trip per telemetry tick. On a hit the cached list is returned without a
        pvesh call; the 5s TTL expires before the next ~60s tick so cross-tick
        reads always re-fetch fresh data. Raises on failure so callers fall
        through to their per-node fallback paths (same as a bare _pvesh error)."""
        now = time.time()
        if (self._cluster_resources_cache is not None
                and (now - self._cluster_resources_ts) < self._cluster_resources_ttl):
            return self._cluster_resources_cache
        resources = await self._pvesh("/cluster/resources")
        self._cluster_resources_cache = resources
        self._cluster_resources_ts = now
        return resources

    async def get_node_stats(self) -> Dict[str, Any]:
        """Per-node stats via local pvesh — no API credentials required.

        Uses /cluster/resources (type=node) as primary source — same daemon that
        powers Proxmox's own UI, so cpu values are always current.  Falls back to
        per-node /status if the cluster endpoint is unavailable.
        """
        try:
            # Primary: /cluster/resources filtered for nodes — cpu from pvestatd (always live)
            try:
                resources = await self._cluster_resources()
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
                # str(e) is often EMPTY on a pvesh/quorum failure — !r keeps the
                # exception type visible so this never logs a blank reason.
                logger.warning(f"cluster/resources unavailable for nodes ({e!r}), "
                               f"falling back to per-node status")

            # Fallback: per-node /status (cpu resets to 0 on first call — less
            # accurate). For a REMOTE node, pvesh proxies /nodes/<n>/status over
            # SSH — so a node that is down (or the cluster is inquorate) makes
            # this block on a dead-host TCP connect EVERY tick, stalling the
            # telemetry loop (the "ssh: connect to <ip> port 22: No route to
            # host" / empty "Node stats error:" spam). Two guards: (1) SKIP nodes
            # /nodes already reports as not "online" — never SSH a known-dead
            # node; (2) run the survivors CONCURRENTLY under a short per-call
            # deadline so one slow node can't serialize-block the rest.
            raw_nodes = await self._pvesh("/nodes")
            candidates, offline = [], []
            for n in (raw_nodes if isinstance(raw_nodes, list) else []):
                (candidates if str(n.get("status", "")).lower() == "online"
                 else offline).append(n)
            # LOG the down member(s) by name — a dead node is a real operational
            # signal the operator needs to see; we skip the SSH poll but not the
            # visibility. WARNING, throttled by the ~60s telemetry tick.
            if offline:
                logger.warning(
                    "cluster node(s) not online — skipping status poll (no SSH): %s",
                    ", ".join(f"{n.get('node','?')}={n.get('status','?')}" for n in offline))

            async def _node_stat(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                node_name = n.get("node", "")
                try:
                    stat = await asyncio.wait_for(
                        self._pvesh(f"/nodes/{node_name}/status"), timeout=8)
                    mem      = stat.get("memory", {})
                    cpu_info = stat.get("cpuinfo", {})
                    return {
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
                    }
                except Exception as e:  # noqa: BLE001 — one node must not sink the rest
                    logger.warning(f"Node status error for {node_name}: {e}")
                    return None

            results = await asyncio.gather(*[_node_stat(n) for n in candidates],
                                           return_exceptions=True)
            nodes = [r for r in results if isinstance(r, dict)]
            return {"nodes": nodes, "cluster": self.cluster_name}
        except Exception as e:
            # !r so a pvesh/quorum failure with an EMPTY message still logs its
            # type (was the blank "Node stats error:" line).
            logger.error(f"Node stats error: {e!r}")
            return {"nodes": [], "error": str(e) or type(e).__name__}

    # ── VM/CT inventory + interface enrichment ────────────────────────────────
    # Extracted to vm_inventory.py (free functions taking ``agent``); these thin
    # wrappers keep the externally-called entry points (dispatch + cs_sim's
    # agent.get_vm_list()) untouched.
    async def list_pools(self) -> list:
        return await vm_inventory.list_pools(self)

    async def list_node_isos(self, node: str) -> list:
        return await vm_inventory.list_node_isos(self, node)

    async def list_node_storages(self, node: str, content_filter: str = "images") -> list:
        return await vm_inventory.list_node_storages(self, node, content_filter)

    async def get_vm_list(self) -> Dict[str, Any]:
        return await vm_inventory.get_vm_list(self)

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
            await self.websocket.send(encode_frame(self.signer, log_msg))
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
            await self.websocket.send(encode_frame(self.signer, msg))
        except Exception:
            pass

    # ── Console/shell relay (VNC + host-shell) ────────────────────────────────
    # Extracted to console_relay.py (free functions taking ``agent``); these thin
    # wrappers keep the _connect_once dispatch chain (self.<method>) untouched.
    async def send_vnc_event(self, event_type: str, data: Dict[str, Any]):
        await console_relay.send_vnc_event(self, event_type, data)

    async def _start_vnc_session(self, session_id: str, vmid: Any,
                                 node: str, kind: str) -> str:
        return await console_relay.start_vnc_session(self, session_id, vmid, node, kind)

    async def _vnc_teardown(self, session_id: str, send_disconnect: bool) -> None:
        await console_relay.vnc_teardown(self, session_id, send_disconnect)

    async def send_shell_event(self, event_type: str, data: Dict[str, Any]):
        await console_relay.send_shell_event(self, event_type, data)

    async def _start_shell_session(self, session_id: str) -> None:
        await console_relay.start_shell_session(self, session_id)

    def _shell_write(self, session_id: str, data: bytes) -> None:
        console_relay.shell_write(self, session_id, data)

    def _shell_resize(self, session_id: str, rows: int, cols: int) -> None:
        console_relay.shell_resize(self, session_id, rows, cols)

    async def _shell_teardown(self, session_id: str, send_disconnect: bool) -> None:
        await console_relay.shell_teardown(self, session_id, send_disconnect)

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
        """Pull latest code, sync to install dir, pip install, then restart.

        Snapshots the install-dir code + writes a pending-update manifest BEFORE
        the copytree swap, and schedules the external health-gate watchdog
        (``lm-component-update-restart``) before restarting. The watchdog checks
        the ``healthy`` marker + ``systemctl`` state and, if the new code crashes
        at boot, restores the pre-swap snapshot (the agent install dir is NOT a
        git repo, so rollback is a file-tree restore, not ``git reset``), marks
        the version bad, and restarts us. A version already marked bad is skipped
        here so we never crash-loop into the same broken release. Best-effort: if
        the watchdog script isn't installed yet (pre-reinstall), the Popen fails
        silently and we degrade to the pre-rollback behavior (restart, no rollback).
        """
        import subprocess, shutil, pathlib
        from . import update_recovery as ur
        current = get_version()
        subprocess.check_call(
            ["git", "-C", repo_dir, "pull", "--rebase", "--autostash"],
            timeout=60, stdout=subprocess.DEVNULL,
        )
        # The version lives at the repo root (VERSION), NOT agent/VERSION —
        # reading agent/VERSION always missed (no such file) and made the
        # "same version — no restart" short-circuit never fire.
        new_ver_path = pathlib.Path(repo_dir) / "VERSION"
        new_ver = new_ver_path.read_text().strip() if new_ver_path.exists() else "?"
        if new_ver == current:
            return  # same version — no restart needed
        # Skip a known-bad version (rolled back before) so we don't crash-loop
        # into the same broken release.
        try:
            if ur.is_version_bad(new_ver, state_dir=AGENT_STATE_DIR):
                logger.warning(
                    f"pxmx-agent update to {new_ver} skipped (marked bad — rolled "
                    f"back before); staying on {current}.")
                ur.clear_pending(state_dir=AGENT_STATE_DIR)
                return
        except Exception as e:  # pragma: no cover - update_recovery unavailable
            logger.debug(f"bad-version check skipped: {e}")
        logger.info(f"Updating pxmx-agent {current} → {new_ver}")
        # Snapshot the current install-dir code BEFORE the swap so the watchdog
        # can restore it (file-tree rollback — the install dir is non-git) if the
        # new code crashes at boot. belt-and-suspenders alongside the version record.
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            backup_dir = ur.snapshot_code(install_dir, ts, tree_list=["src"],
                                          state_dir=AGENT_STATE_DIR)
            ur.write_pending(backup_dir, from_version=current, to_version=new_ver,
                             ts=ts, state_dir=AGENT_STATE_DIR,
                             extra={"service_unit": "lm-pxmx-agent", "deadline": 90})
        except Exception as e:
            logger.warning(f"pre-update snapshot failed (rollback disabled): {e}")
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
        # Refresh install_dir/VERSION from the repo root so an agent installed
        # before the .NN migration doesn't keep a stale old-format copy that
        # get_version() would fall back to. Mirrors install_agent.sh.
        try:
            shutil.copy2(str(pathlib.Path(repo_dir) / "VERSION"), str(dst / "VERSION"))
        except Exception as e:
            logger.warning(f"self-update: could not refresh {dst}/VERSION: {e}")
        pip = dst / "venv" / "bin" / "pip"
        req = dst / "requirements.txt"
        if pip.exists() and req.exists():
            subprocess.check_call([str(pip), "install", "-r", str(req), "-q"], timeout=120)
        # Schedule the external health-gate watchdog. The agent runs as root, so
        # no sudo — the script re-execs via systemd-run to escape our cgroup so it
        # survives our exit. Best-effort: a missing script (pre-reinstall) fails
        # silently and we just restart with no rollback (the pre-rollback behavior).
        try:
            subprocess.Popen(
                ["/usr/local/bin/lm-component-update-restart",
                 "--unit", "lm-pxmx-agent", "--state-dir", AGENT_STATE_DIR,
                 "--install-dir", str(dst), "--deadline", "90",
                 "--recovery-py", os.path.abspath(ur.__file__)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:  # pragma: no cover - script missing / not executable
            logger.debug(f"could not schedule update watchdog: {e}")
        logger.info("Self-update applied — exiting so systemd relaunches on the new code")
        # Exit NON-ZERO (3) and let systemd relaunch us. We deliberately do NOT
        # `systemctl restart` ourselves: that runs inside our own cgroup, so
        # systemd's stop-phase SIGTERM kills the `systemctl` child mid-transaction
        # and can strand the agent offline (the anti-pattern lm core removed). The
        # unit's Restart=always relaunches on any exit; the nonzero code also keeps
        # us correct if the unit ever moves to Restart=on-failure, matching core's
        # BaseControlPlane restart contract (os._exit(3)).
        os._exit(3)

    async def trigger_update(self) -> None:
        """Force an immediate self-update check (Phase E ``update_agent`` long op).
        Runs the blocking git pull + sync + restart in an executor; returns if
        there is no repo or no new version (``_apply_update`` os._exit(3)s when
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

    async def _ensure_cs_hub_token(self, request_id: str = "auto", *, force: bool = False) -> None:
        """Provision (once, cached) the ``root@pam!cs-hub`` Proxmox API token and
        relay it up as ``CS_TOKEN_RESULT`` so the cs spoke (which has no pvesh)
        can call the Proxmox API for sim-tag sync — with NO manual key setup.

        Mirrors ``_ensure_console_token``: Proxmox never re-reveals a token secret
        after creation, so we delete+create ONCE (via local pvesh, as root on the
        Proxmox host) and cache the value for the agent's lifetime; later triggers
        (CS re-enable / cs-spoke reconnect / hub re-request) just RE-EMIT the
        cached value rather than rotating it — a rotation would invalidate the
        token the cs spoke is already using. ``force=True`` rotates. Best-effort;
        the secret is never logged. Mirrors bash ``handle_create_proxmox_token``."""
        TOKEN_ID = "cs-hub"
        USER = "root@pam"
        if self._cs_hub_token and not force:
            # Already have it — re-send so a freshly-(re)connected cs spoke picks
            # it up. No pvesh, no rotation.
            await self.send_cs_event("CS_TOKEN_RESULT",
                                     {"request_id": request_id, "status": "provisioned",
                                      "token": self._cs_hub_token})
            return
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
                logger.error(f"cs-hub token provision {request_id}: pvesh returned no value")
                await self.send_cs_event("CS_TOKEN_RESULT",
                                          {"request_id": request_id, "status": "error",
                                           "error": "pvesh returned no token value"})
                return
            self._cs_hub_token = f"{USER}!{TOKEN_ID}={secret}"
            await self.send_cs_event("CS_TOKEN_RESULT",
                                     {"request_id": request_id, "status": "provisioned",
                                      "token": self._cs_hub_token})
            del secret
            logger.info(f"Proxmox cs-hub token root@pam!cs-hub provisioned + relayed "
                        f"(request_id={request_id}, value not logged)")
        except Exception as e:  # noqa: BLE001
            logger.error(f"cs-hub token provision {request_id} failed: {e}")
            await self.send_cs_event("CS_TOKEN_RESULT",
                                      {"request_id": request_id, "status": "error",
                                       "error": str(e)[:300]})

    # Back-compat: the hub's CS_CREATE_PROXMOX_TOKEN path calls this name. Route
    # it through the cached provisioner so a re-request re-emits the SAME token
    # instead of rotating one the cs spoke is already using.
    async def _provision_proxmox_token(self, request_id: str) -> None:
        await self._ensure_cs_hub_token(request_id)

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

    async def _resolve_spoke_url(self) -> None:
        """Turn the resolve sentinel (``""``/``"auto"``/``None``) into a concrete
        ``ws(s)://host:port/ws/agent`` URL.

        Two sources, in order:
          1. ``self.spoke_ip`` — the operator gave ONLY an IP. Probe its known
             agent-listener endpoints (wss:443, ws:8767, wss:8443, ws:8766) and
             use the first that answers a WebSocket upgrade, so scheme/port/path
             are auto-determined. If nothing answers yet (spoke still booting) we
             leave the sentinel so the next reconnect re-probes — never fatal.
          2. Otherwise auto-discover the hub box via DNS (``lm-hub.<suffix>``)
             then mDNS and target its pxmx-agent listener.
        Best-effort throughout: on no result the sentinel is left in place so
        run()'s reconnect loop retries."""
        if self.spoke_url not in ("", "auto", None):
            return
        try:
            from .discovery import discover_hub_url, resolve_agent_url
        except ImportError:
            try:
                from discovery import discover_hub_url, resolve_agent_url
            except ImportError:
                logger.warning("discovery module unavailable — cannot resolve the "
                               "spoke; pass --spoke-ip/--spoke-url or set SPOKE_IP/SPOKE_URL.")
                return

        # 1. Operator supplied only an IP → probe its /ws/agent endpoints.
        if self.spoke_ip:
            url = await asyncio.to_thread(resolve_agent_url, self.spoke_ip, 5.0)
            if url:
                self.spoke_url = url
                logger.info(f"Resolved spoke agent listener at {self.spoke_url} "
                            f"(from --spoke-ip {self.spoke_ip})")
            else:
                logger.warning(
                    f"No agent listener answered at {self.spoke_ip} yet "
                    "(spoke still booting, or wrong IP) — will re-probe on reconnect.")
            return

        # 2. No IP given → fall back to hub auto-discovery.
        url = await asyncio.to_thread(discover_hub_url, 5.0, None, True)
        if url:
            self.spoke_url = _normalize_spoke_url(url)
            logger.info(f"Auto-discovered hub (agent listener) at {self.spoke_url}")
        else:
            logger.warning("Hub auto-discovery found no hub (no lm-hub DNS record / "
                           "mDNS broadcast); will retry on reconnect. Pass --spoke-ip to pin.")

    # ── Update-recovery healthy marker ──────────────────────────────────────
    def _clear_healthy_marker(self) -> None:
        """Drop a stale ``healthy`` marker on boot so a fresh start must re-prove
        health (the external update watchdog treats the marker as the positive
        "new code booted OK" signal)."""
        try:
            m = os.path.join(AGENT_STATE_DIR, "healthy")
            if os.path.exists(m):
                os.remove(m)
        except Exception:  # pragma: no cover - state dir missing / not writable
            pass

    def _touch_healthy_marker(self) -> None:
        """Mark the agent healthy after spoke auth completes — the watchdog's
        positive health signal (presence => new code booted + authed)."""
        try:
            os.makedirs(AGENT_STATE_DIR, exist_ok=True)
            open(os.path.join(AGENT_STATE_DIR, "healthy"), "w").close()
        except Exception as e:  # pragma: no cover - state dir not writable
            logger.debug(f"could not write healthy marker: {e}")

    async def run(self):
        """Main agent loop — connect, auth, run telemetry/provision/command/watchdog tasks, reconnect with backoff.

        Spawns the self-update check loop and the systemd watchdog loop, then
        reconnects forever (exponential-ish backoff) on socket loss; re-raises
        on repeated auth failure so a bad secret doesn't spin forever.
        """
        import websockets
        # Route uncaught asyncio-task exceptions through the PxmxAgent logger so
        # their tracebacks relay to the hub (Setup → Agent Logs + BugFixer),
        # not just the local file. Set here because the loop is now running.
        try:
            asyncio.get_running_loop().set_exception_handler(self._asyncio_exception_relay)
        except Exception:  # noqa: BLE001
            pass
        # Clear any stale healthy marker from a prior boot — a fresh start must
        # re-prove health (re-auth with the spoke) before the update watchdog
        # treats it as the "new code booted OK" signal. Without this a
        # crash-looping new version could inherit a stale marker and the watchdog
        # would never roll back.
        self._clear_healthy_marker()
        backoff = 5
        _consecutive_auth_fails = 0
        asyncio.create_task(self._update_check_loop())
        asyncio.create_task(self._sd_watchdog_loop())
        await self._resolve_spoke_url()
        while True:
            # Re-discover each pass while the URL is still the sentinel so a hub
            # that comes up after this agent (or moves) is found without a restart.
            if self.spoke_url in ("", "auto", None):
                await self._resolve_spoke_url()
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
                # If the operator pinned only an IP, a failure may mean the spoke
                # restarted on a different scheme/port (e.g. gained/lost its TLS
                # cert). Drop back to the resolve sentinel so the next pass
                # re-probes its endpoints instead of hammering a now-stale URL.
                if self.spoke_ip:
                    self.spoke_url = ""
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception as e:
                logger.error(f"Unexpected error: {e} — retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    def _wss_ssl_context(self, spoke_url: str):
        """Build the SSL context for a ``wss://`` connect to the spoke.

        Returns ``(ssl_ctx, mode_str)``. ``ssl_ctx`` is None only on a build
        failure or a misconfigured CA path — the caller then connects with
        ``ssl=None`` and ``websockets`` refuses a ``wss://`` URI ("ssl=None is
        incompatible with a wss:// URI"), so the agent retry-loops with the
        error logged rather than silently degrading security.

        Modes (mirror ``BaseControlPlane._client_ssl_ctx`` in lm core):
          * verify OFF (default) — ``ssl._create_unverified_context()``:
            encrypted but the spoke cert is NOT authenticated (lab default
            while cert deployment is in progress).
          * verify ON (``LM_HUB_TLS_VERIFY=1``):
            - ``LM_HUB_CA_CERT`` set + readable → ``create_default_context(
              cafile=…)`` pins the spoke CA (self-signed / private-CA case).
            - no CA path → ``create_default_context()`` trusts the SYSTEM
              store (public-CA / Let's Encrypt case) — so an LE-signed spoke
              cert verifies with ``LM_HUB_TLS_VERIFY=1`` alone, no CA file
              (previously the agent required a pinned CA even for public CAs,
              so the agent→spoke leg couldn't verify an LE cert without one).
            - CA path set but MISSING → log ERROR + return ``(None, …)``:
              never silently downgrade an operator who asked for verification
              to an unverified context (the footgun: they'd believe the spoke
              cert is authenticated when it isn't).
        """
        import ssl as _ssl
        if not (spoke_url or "").lower().startswith("wss://"):
            return None, "plaintext (loopback/legacy)"
        try:
            if os.environ.get("LM_HUB_TLS_VERIFY", "0").strip() in ("1", "true", "yes"):
                ca = os.environ.get("LM_HUB_CA_CERT", "").strip()
                if ca and not os.path.isfile(ca):
                    logger.error("wss: LM_HUB_TLS_VERIFY=1 but CA path %s does not "
                                 "exist — refusing to silently downgrade to "
                                 "unverified. Fix the path or unset LM_HUB_TLS_VERIFY.", ca)
                    return None, "TLS disabled (CA path missing)"
                if ca:
                    ctx = _ssl.create_default_context(cafile=ca)
                    return ctx, f"TLS verified (CA={ca})"
                # No pinned CA → system trust store (public-CA / Let's Encrypt).
                ctx = _ssl.create_default_context()
                return ctx, "TLS verified (system trust store)"
            # NOTE: the public name is ssl.create_default_context(); the
            # unverified builder is the PRIVATE ssl._create_unverified_context()
            # (leading underscore). Calling ssl.create_unverified_context()
            # raises AttributeError → ssl_ctx=None → websockets rejects
            # ssl=None on a wss:// URI and the agent retry-loops forever.
            ctx = _ssl._create_unverified_context()
            return ctx, "TLS unverified (self-signed cert)"
        except Exception as e:
            logger.warning(f"wss SSL context build failed: {e}; connecting without TLS")
            return None, "TLS disabled (context build failed)"

    async def _connect_once(self):
        import websockets
        logger.info(f"pxmx-agent {version} connecting to {self.spoke_url}...")

        # TLS: a wss:// spoke_url gets an SSL context (verify-off by default;
        # LM_HUB_TLS_VERIFY=1 verifies — pinned CA via LM_HUB_CA_CERT, else the
        # system trust store for a public-CA / Let's Encrypt spoke cert). ws://
        # stays plaintext. Mirrors BaseControlPlane._client_ssl_ctx in lm core.
        ssl_ctx, _tls_mode = self._wss_ssl_context(self.spoke_url)
        # INFO so the mode reaches the hub via the WebSocketLogHandler relay
        # (PxmxAgent prefix); pairs with the "Connection to ... failed" warning
        # in run() to form a TLS troubleshooting trail.
        logger.info("TLS mode: %s", _tls_mode)

        async with websockets.connect(self.spoke_url, ssl=ssl_ctx) as websocket:
            self.websocket = websocket

            # 1. Agent → Spoke handshake
            # Send without secret if we don't have one yet (zero-touch provisioning)
            handshake: Dict[str, Any] = {"agent_id": self.agent_id}
            if self.secret:
                handshake["secret"] = self.secret
            # install_uuid + hostname let the hub detect a clone-and-rename of this
            # node and carry over the agent's config/approval. Empty install_uuid =
            # .env unwritable → hub skips correlation (agent treated as before).
            if self.install_uuid:
                handshake["install_uuid"] = self.install_uuid
            if self.hostname:
                handshake["hostname"] = self.hostname
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
                    # Onboarding status frames ({"status":...}) are raw JSON;
                    # signed frames are <sig>.<body>. Accept either.
                    msg = json.loads(raw if raw[:1] == "{" else split_frame(raw)[1])
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
            # New code booted + authed with the spoke → mark healthy. The external
            # update watchdog treats this marker as the "new version is good"
            # signal; its absence past the deadline triggers a rollback.
            self._touch_healthy_marker()

            # Flush anything logged while the socket was down (startup,
            # reconnect gap) now that we're connected + authed, then continue
            # streaming live. The handler itself stays installed for the
            # process lifetime (added in __init__).
            self._ws_log_handler.flush_buffered()

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            telemetry_task = asyncio.create_task(self._telemetry_loop())

            # Resume client-simulation mode from persisted config if the hub
            # hasn't (re-)pushed UPDATE_CONFIG this session. Without this a
            # self-update restart drops CS mode — and auto-provisioning with
            # it — until the hub happens to re-push, which can be lost in a
            # loaded spoke's request backlog. The websocket is up here, so
            # telemetry/CS frames can flow. A later UPDATE_CONFIG just refreshes
            # self.config (same enabled state → no redundant restart).
            if not self.cs_enabled and bool(
                    (self.config.get("client_simulation") or {}).get("enabled")):
                logger.info("Resuming client_simulation mode from persisted config")
                await self._set_cs_enabled(True)

            try:
                async for message in websocket:
                    # Wire form <sig>.<body>: verify the RECEIVED body bytes
                    # directly, parse once (matches the hub's new frame format).
                    sig, body = split_frame(message)
                    msg_data = json.loads(body)
                    if sig and not self.signer.verify_bytes(body.encode(), sig):
                        logger.warning("Invalid signature — dropping")
                        continue

                    payload  = msg_data.get("payload", {})
                    cmd_type = payload.get("type")
                    data     = payload.get("data", {})
                    corr_id  = msg_data.get("header", {}).get("correlation_id")

                    # DEBUG, not INFO: routine per-command trace (polls arrive
                    # continuously); meaningful commands log their own INFO line
                    # (e.g. UPDATE_CONFIG → "client_simulation enabled=true").
                    # See logging-observability-contract.md (normalization).
                    logger.debug(f"Command: {cmd_type}")
                    result = {"status": "ERROR", "message": "Unknown command"}

                    if cmd_type == "UPDATE_CONFIG":
                        old_cs = bool((self.config.get("client_simulation") or {}).get("enabled"))
                        # Deep-merge, don't wholesale-replace: partial pushes from
                        # the UI save (enabled/tenant only) and the CS bridge
                        # (full usb_config) otherwise clobber each other, wiping
                        # client_simulation.usb_config.vidpids → the provision
                        # loop reports "no dongle_vidpids configured". See
                        # _deep_merge_config.
                        self.config = _deep_merge_config(self.config, data)
                        # Persist the MERGED config so a restart resumes from the
                        # full last-known config (not just the last partial push).
                        self._save_persisted_config(self.config)
                        new_cs = bool((self.config.get("client_simulation") or {}).get("enabled"))
                        if old_cs != new_cs:
                            await self._set_cs_enabled(new_cs)
                        # Managed crontab: apply immediately when the operator's
                        # pasted content is part of this push (idempotent; the
                        # telemetry loop also drift-corrects periodically).
                        if "managed_crontab" in data:
                            self._managed_crontab_status = await managed_crontab.apply_managed_crontab(
                                self.config.get("managed_crontab") or "")
                            self._last_cron_reconcile = time.time()
                        result = {"status": "SUCCESS", "message": "Config updated"}

                    elif cmd_type == "GET_VM_LIST":
                        result = await self.get_vm_list()

                    elif cmd_type == "GET_NODE_STATS":
                        result = await self.get_node_stats()

                    elif cmd_type == "GET_SYSTEM_STATS":
                        result = await self.collect_metrics()

                    elif cmd_type == "SET_LOG_LEVEL":
                        level = set_log_level(data.get("enabled"))
                        result = {"status": "SUCCESS",
                                  "message": f"Log level → {logging.getLevelName(level)}"}

                    elif cmd_type == "SHELLEXEC":
                        result = {"status": "ERROR", "message": "SHELLEXEC is disabled"}

                    elif cmd_type == "RUN_COMMAND":
                        # Remote Console: the hub relayed a signed RUN_COMMAND
                        # down through the owning spoke (Global-Admin gated +
                        # audit-logged at the hub). allow_shell mirrors the WebUI
                        # Debug knob; the runner enforces the allowlist otherwise,
                        # a timeout, and an output cap. Off the loop (subprocess).
                        try:
                            from command_runner import run_local_command
                            result = await asyncio.to_thread(
                                run_local_command,
                                data.get("command", ""),
                                bool(data.get("allow_shell", False)),
                                float(data.get("timeout", 30.0) or 30.0))
                        except Exception as _rce:
                            result = {"ok": False, "rc": None, "stdout": "", "stderr": "",
                                      "truncated": False, "error": f"runner error: {_rce}"}

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

                    elif cmd_type == "PXMX_VM_ACTION":
                        # Hypervisors view lifecycle: start/stop/reboot/snapshot
                        # ANY vmid (unguarded — real tenant VMs, not the sim
                        # 90000 floor). Fast qm/pct ops; the spoke allows a 30s
                        # window for stop/snapshot. `kind` (qemu/lxc) is passed
                        # by the hub to skip a detect_guest_type round-trip.
                        try:
                            res = await pve_cmds.vm_action_any(
                                data.get("vmid"), data.get("action"),
                                kind=data.get("type"),
                                snapshot_name=data.get("snapshot_name"),
                                backup_opts=data.get("backup"))
                            result = {"status": "SUCCESS", **res}
                        except pve_cmds.PveError as e:
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_VM_ACTION_BULK":
                        # ONE message → the SAME action on MANY VMs on this node
                        # (the Hypervisors bulk start/stop/reboot/snapshot/backup),
                        # instead of one PXMX_VM_ACTION per VM. Run bounded-
                        # concurrent; one VM's failure never sinks the rest. Each
                        # item: {vmid, type?, snapshot_name?, backup?}.
                        _items = data.get("items") or []
                        _action = data.get("action")
                        _sem = asyncio.Semaphore(6)

                        async def _bulk_one(it):
                            async with _sem:
                                try:
                                    r = await pve_cmds.vm_action_any(
                                        it.get("vmid"), _action, kind=it.get("type"),
                                        snapshot_name=it.get("snapshot_name"),
                                        backup_opts=it.get("backup"))
                                    return {"vmid": it.get("vmid"), "ok": True, **r}
                                except pve_cmds.PveError as e:
                                    return {"vmid": it.get("vmid"), "ok": False, "error": str(e)}
                                except Exception as e:  # noqa: BLE001
                                    return {"vmid": it.get("vmid"), "ok": False, "error": str(e)}

                        _res = await asyncio.gather(
                            *[_bulk_one(it) for it in _items if isinstance(it, dict)])
                        result = {"status": "SUCCESS", "results": list(_res)}

                    elif cmd_type == "PXMX_LIST_STORAGE":
                        # Backup-capable storages on this host for the Setup →
                        # Hypervisors dropdown (auto-list-from-host).
                        try:
                            result = {"status": "SUCCESS",
                                      **(await pve_cmds.list_backup_storages())}
                        except Exception as e:
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_RETAG_TENANT":
                        # Cross-tenant migration: swap a tenant's proxmox_tag on
                        # every VM/CT that carries it (old_tag -> new_tag).
                        try:
                            result = await pve_cmds.retag_tenant(
                                data.get("old_tag", ""), data.get("new_tag", ""))
                        except Exception as e:
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_APPLY_SIM_TAGS":
                        # sim-tag sync (moved off the cs spoke's Proxmox-API path).
                        # The cs spoke computes {vmid: [sim- tags]} from the client
                        # registry and sends it here; we apply via local qm/pct so
                        # tagging never PUTs to the API (was storming CS telemetry).
                        try:
                            result = await pve_cmds.apply_sim_tags(
                                data.get("tags") or {})
                        except Exception as e:
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_CLONE_VM":
                        # Clone-from-template: any tenant may clone a VM that
                        # lives in a configured template pool (the hub resolves
                        # the template unique_id and the cloning tenant's
                        # proxmox_tag, then routes here on the template's node).
                        # We auto-assign a free VMID, clone the template, and tag
                        # the new VM for the cloning tenant so the next VM sync
                        # attributes it to them. The agent runs on the
                        # template's node (the spoke routes by unique_id node),
                        # so qm/pct clone operates on the local template.
                        try:
                            # Resolve the template vmid + node + kind. The hub
                            # sends template_unique_id "<cluster>/<node>/<vmid>"
                            # (and the same value as unique_id for routing), or
                            # explicit vmid/node/type.
                            tuid = data.get("template_unique_id") or data.get("unique_id") or ""
                            if tuid and "/" in tuid:
                                parts = tuid.split("/")
                                node = parts[-2] if len(parts) >= 3 else ""
                                template_vmid = parts[-1]
                            else:
                                node = data.get("node") or ""
                                template_vmid = data.get("template_vmid")
                            if template_vmid is None:
                                raise pve_cmds.PveError(
                                    "template_vmid or template_unique_id required")
                            name = (data.get("name") or "").strip()
                            if not name:
                                raise pve_cmds.PveError("name is required")
                            kind = (data.get("type") or "").lower()
                            if kind not in ("qemu", "lxc"):
                                kind = await pve_cmds.detect_guest_type(
                                    int(template_vmid))
                            new_vmid = data.get("new_vmid")
                            if new_vmid is None:
                                new_vmid = await pve_cmds.next_free_vmid()
                            # Tenant labels to apply to the new VM: the hub sends
                            # tenant_tags (a list — typically the tenant display
                            # name as the visible label + the proxmox_tag which the
                            # Hypervisor→NetBox VM sync matches on). A single
                            # tenant_tag string is accepted for back-compat.
                            ttags_in = data.get("tenant_tags") or (
                                [data.get("tenant_tag")] if data.get("tenant_tag") else [])
                            tenant_tags = [str(t).strip() for t in ttags_in
                                           if str(t).strip()]
                            pool = (data.get("pool") or "").strip() or None
                            # Clone the template → new VMID. full clone so the
                            # new VM has its own disk (templates are shared).
                            # pool places the new VM in a Proxmox resource pool
                            # when the user selected one (both qm/pct clone take
                            # --pool).
                            await pve_cmds.clone_vm_any(
                                template_vmid, new_vmid, name, kind, pool=pool)
                            # Tag the new VM for the cloning tenant. Inherit the
                            # template's existing tags (clone copies config, but
                            # we set explicitly so the tenant labels are present
                            # even if the template had none) and append the tenant
                            # labels (dedup, case-insensitive). Best-effort: a tag
                            # failure does not undo a successful clone.
                            tags = []
                            try:
                                cfg = await self._pvesh(
                                    f"/nodes/{node}/{kind}/{int(template_vmid)}/config")
                                raw = (cfg or {}).get("tags", "")
                                tags = [t.strip() for t in str(raw).split(";")
                                        if t.strip()]
                            except Exception as e:
                                logger.warning("clone: could not read template "
                                                "tags for %s/%s: %s", node,
                                                template_vmid, e)
                            lower_tags = {t.lower() for t in tags}
                            for tt in tenant_tags:
                                if tt.lower() not in lower_tags:
                                    tags.append(tt)
                                    lower_tags.add(tt.lower())
                            if tags:
                                try:
                                    await pve_cmds.set_tags_any(
                                        new_vmid, kind, tags)
                                except pve_cmds.PveError as e:
                                    logger.warning("clone: tag set failed for new "
                                                   "VM %s: %s", new_vmid, e)
                            result = {
                                "status": "SUCCESS",
                                "unique_id": f"{self.cluster_name}/{node}/{new_vmid}",
                                "cluster": self.cluster_name,
                                "node": node,
                                "vmid": int(new_vmid),
                                "name": name,
                                "type": kind,
                                "pool": pool or "",
                                "template_vmid": int(template_vmid),
                                "tags": tags,
                            }
                        except pve_cmds.PveError as e:
                            result = {"status": "ERROR", "message": str(e)}
                        except Exception as e:
                            logger.exception("PXMX_CLONE_VM failed")
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_LIST_POOLS":
                        # Proxmox resource pool list for the clone/create-VM UI's
                        # pool dropdown. Best-effort; [] when /pools is unavailable.
                        try:
                            pools = await self.list_pools()
                            result = {"status": "SUCCESS", "pools": pools,
                                      "cluster": self.cluster_name}
                        except Exception as e:
                            logger.exception("PXMX_LIST_POOLS failed")
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_LIST_ISOS":
                        # ISO images on a node for the create-VM-from-ISO flow.
                        # pvesh /nodes/{node}/storage is cluster-wide, so this
                        # works for any node in the agent's cluster.
                        try:
                            node = data.get("node") or ""
                            isos = await self.list_node_isos(node)
                            result = {"status": "SUCCESS", "isos": isos,
                                      "node": node, "cluster": self.cluster_name}
                        except Exception as e:
                            logger.exception("PXMX_LIST_ISOS failed")
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_LIST_STORAGES":
                        # Storages on a node accepting 'images' (boot-disk targets)
                        # for the create-VM-from-ISO disk dropdown.
                        try:
                            node = data.get("node") or ""
                            content_filter = data.get("content") or "images"
                            storages = await self.list_node_storages(node, content_filter)
                            result = {"status": "SUCCESS", "storages": storages,
                                      "node": node, "cluster": self.cluster_name}
                        except Exception as e:
                            logger.exception("PXMX_LIST_STORAGES failed")
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "PXMX_CREATE_VM":
                        # Create a new qemu VM from an ISO (build-your-own-VM). The
                        # hub resolves the target node's agent (agent_id) and sends
                        # the ISO volid + disk/memory/cores config. We auto-assign
                        # a free VMID, create via pvesh (cluster-wide — works for
                        # any node in this cluster), tag the new VM with the acting
                        # tenant's labels (name + proxmox_tag), and optionally
                        # place it in a pool. The VM is created stopped; the user
                        # boots the ISO and installs via the VNC console.
                        try:
                            node = (data.get("node") or "").strip()
                            if not node:
                                raise pve_cmds.PveError("node is required")
                            name = (data.get("name") or "").strip()
                            if not name:
                                raise pve_cmds.PveError("name is required")
                            volid = (data.get("volid") or "").strip()
                            if not volid:
                                raise pve_cmds.PveError("volid (ISO) is required")
                            new_vmid = data.get("new_vmid")
                            if new_vmid is None:
                                new_vmid = await pve_cmds.next_free_vmid()
                            ttags_in = data.get("tenant_tags") or (
                                [data.get("tenant_tag")] if data.get("tenant_tag") else [])
                            tenant_tags = [str(t).strip() for t in ttags_in if str(t).strip()]
                            tags_joined = ";".join(tenant_tags)
                            pool = (data.get("pool") or "").strip() or None
                            memory_mb = int(data.get("memory_mb") or 2048)
                            cores = int(data.get("cores") or 2)
                            disk_storage = (data.get("disk_storage") or "").strip() or "local-lvm"
                            disk_gb = int(data.get("disk_gb") or 32)
                            bridge = (data.get("bridge") or "vmbr0").strip() or "vmbr0"
                            # pvesh create /nodes/{node}/qemu (cluster-wide API).
                            args = [
                                "--vmid", str(new_vmid),
                                "--name", name,
                                "--cdrom", volid,
                                "--memory", str(memory_mb),
                                "--cores", str(cores),
                                "--scsi0", f"{disk_storage}:{disk_gb}",
                                "--net0", f"virtio,bridge={bridge}",
                                "--ostype", "l26",
                            ]
                            if pool:
                                args += ["--pool", pool]
                            if tags_joined:
                                args += ["--tags", tags_joined]
                            await self._pvesh_action(
                                "create", f"/nodes/{node}/qemu", *args,
                                json_out=True, timeout=120)
                            result = {
                                "status": "SUCCESS",
                                "unique_id": f"{self.cluster_name}/{node}/{new_vmid}",
                                "cluster": self.cluster_name,
                                "node": node,
                                "vmid": int(new_vmid),
                                "name": name,
                                "type": "qemu",
                                "pool": pool or "",
                                "tags": tenant_tags,
                            }
                        except pve_cmds.PveError as e:
                            result = {"status": "ERROR", "message": str(e)}
                        except Exception as e:
                            logger.exception("PXMX_CREATE_VM failed")
                            result = {"status": "ERROR", "message": str(e)}

                    elif cmd_type == "VNC_PROXY":
                        # VNC console: ask Proxmox for a vncproxy ticket+port via
                        # local pvesh (root-authed, no API token needed). The hub
                        # opens the authenticated wss://<host>:8006/vncwebsocket
                        # itself using the cs-hub API token and relays bytes.
                        # Returns {ticket, port, node, host} the hub needs to
                        # build the WSS URL.
                        try:
                            node = data.get("node") or ""
                            vmtype = (data.get("type") or "qemu").lower()
                            vmid = data.get("vmid")
                            path = f"/nodes/{node}/{vmtype}/{vmid}/vncproxy"
                            vnc = await self._pvesh_action(
                                "create", path, "--websocket", "1", json_out=True)
                            result = {
                                "status": "SUCCESS",
                                "ticket": (vnc or {}).get("ticket") or "",
                                "port": (vnc or {}).get("port"),
                                "node": node,
                                "host": node or self.config.get("hostname") or "",
                            }
                        except Exception as e:
                            result = {"status": "ERROR", "message": f"vncproxy: {e}"}

                    elif cmd_type == "VNC_START":
                        # Hub→spoke→agent: open a Proxmox vncwebsocket HERE and
                        # relay frames over the existing agent↔spoke WS (agent-
                        # terminates-WSS model). Awaited synchronously so the
                        # Proxmox ticket (the RFB VNC password noVNC must
                        # present) is returned to the hub → browser; without it
                        # noVNC auths with an empty password and Proxmox drops
                        # the RFB session. down_q buffers browser frames until
                        # the WSS is open. vmid is unguarded — Hypervisors
                        # console targets real tenant VMs, not the sim floor.
                        session_id = data.get("session_id") or ""
                        if session_id and session_id not in self._vnc_sessions:
                            self._vnc_sessions[session_id] = {
                                "down_q": asyncio.Queue(), "px_ws": None, "tasks": [],
                            }
                            try:
                                ticket = await self._start_vnc_session(
                                    session_id, data.get("vmid"),
                                    data.get("node"), data.get("type"))
                            except Exception as e:
                                logger.warning(f"VNC start {session_id} failed: {e}")
                                await self.send_vnc_event("VNC_ERROR",
                                    {"session_id": session_id, "error": str(e)[:300]})
                                self._vnc_sessions.pop(session_id, None)
                                result = {"status": "ERROR",
                                          "message": str(e)[:300],
                                          "session_id": session_id}
                            else:
                                result = {"status": "SUCCESS",
                                          "session_id": session_id,
                                          "ticket": ticket}
                        else:
                            result = {"status": "SUCCESS",
                                      "session_id": session_id,
                                      "ticket": self._vnc_sessions[session_id].get("ticket", "")}

                    elif cmd_type == "VNC_FRAME_DOWN":
                        # Browser→Proxmox frame. Buffer onto the session's
                        # down_q (the drain task forwards to the WSS). No ack —
                        # high-volume; `continue` skips the AGENT_RESPONSE send
                        # so we don't ack every keystroke.
                        session_id = data.get("session_id") or ""
                        sess = self._vnc_sessions.get(session_id) if session_id else None
                        if sess is not None:
                            try:
                                raw = base64.b64decode(data.get("data") or "")
                                sess["down_q"].put_nowait(raw)
                            except Exception:
                                pass
                        continue

                    elif cmd_type == "VNC_DISCONNECT":
                        # Browser closed the console (or hub tore down). Close
                        # the Proxmox WSS + drop the session. Don't re-emit
                        # VNC_DISCONNECT up — the hub initiated this side.
                        session_id = data.get("session_id") or ""
                        if session_id:
                            asyncio.create_task(
                                self._vnc_teardown(session_id, send_disconnect=False))
                        result = {"status": "OK", "session_id": session_id}

                    elif cmd_type == "SHELL_START":
                        # Hub→spoke→agent: open a PTY bash on THIS node and relay
                        # it to the browser (agent-terminates-PTY, mirrors VNC).
                        # Gated hub-side (admin + opt-in toggle + audit).
                        session_id = data.get("session_id") or ""
                        if session_id and session_id not in self._shell_sessions:
                            self._shell_sessions[session_id] = {"master_fd": None, "tasks": []}
                            try:
                                await self._start_shell_session(session_id)
                                result = {"status": "SUCCESS", "session_id": session_id}
                            except Exception as e:
                                logger.warning("SHELL start %s failed: %s", session_id, e)
                                await self.send_shell_event("SHELL_ERROR",
                                    {"session_id": session_id, "error": str(e)[:300]})
                                self._shell_sessions.pop(session_id, None)
                                result = {"status": "ERROR", "message": str(e)[:300],
                                          "session_id": session_id}
                        else:
                            result = {"status": "SUCCESS", "session_id": session_id}

                    elif cmd_type == "SHELL_IN":
                        # Browser→PTY keystrokes. No ack (high-volume) — write to
                        # the PTY and `continue` past the AGENT_RESPONSE send.
                        session_id = data.get("session_id") or ""
                        if session_id:
                            try:
                                self._shell_write(session_id, base64.b64decode(data.get("data") or ""))
                            except Exception:
                                pass
                        continue

                    elif cmd_type == "SHELL_RESIZE":
                        session_id = data.get("session_id") or ""
                        if session_id:
                            self._shell_resize(session_id, data.get("rows", 24), data.get("cols", 80))
                        continue

                    elif cmd_type == "SHELL_DISCONNECT":
                        session_id = data.get("session_id") or ""
                        if session_id:
                            asyncio.create_task(
                                self._shell_teardown(session_id, send_disconnect=False))
                        result = {"status": "OK", "session_id": session_id}

                    elif cmd_type == "INSTALL_CERT":
                        # Hub-brokered cert distribution: the le spoke issued/
                        # renewed a Let's Encrypt cert and the hub pushes it
                        # here to install on this node's pveproxy. We install on
                        # the LOCAL node (pvenode is inherently local); the spoke
                        # routed this to us because we own `identifier`/node.
                        result = await self.install_cert(
                            data.get("fullchain", ""), data.get("privkey", ""),
                            node=data.get("identifier") or data.get("node") or "")

                    elif cmd_type == "START_BACKUP":
                        # Hub-triggered template backup: vzdump this VM and stream
                        # the archive to the hub's template repo. ACK immediately;
                        # the long vzdump+upload runs in the background and reports
                        # progress via the token'd /progress endpoint on the hub.
                        result = self._start_template_backup(data)

                    elif cmd_type == "REFRESH_TEMPLATE":
                        # Hub-triggered destructive refresh: pause auto-prov, wipe
                        # this host's sim VMs, restore the backup to the template
                        # VMID, resume auto-prov. ACK now; runs in the background.
                        result = self._start_template_refresh(data)

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
                    await websocket.send(encode_frame(self.signer, resp))

            finally:
                heartbeat_task.cancel()
                telemetry_task.cancel()
                # Handler stays installed (added in __init__) so records logged
                # during the disconnect gap are buffered and flushed on the next
                # connect, instead of being dropped.

    async def _heartbeat_loop(self):
        while True:
            try:
                msg = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.agent_id, "destination_id": "pxmx-spoke"},
                    "payload": {"type": "AGENT_HEARTBEAT", "data": {}},
                }
                await self.websocket.send(encode_frame(self.signer, msg))
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

                # Per-phase timing for the telemetry-freshness diagnostic (surfaced
                # on the VM Server detail page). These three pvesh-backed calls are
                # the usual stall points on a loaded host — recording how long each
                # took, per tick, is what pinpoints WHERE the lag is.
                _t_a = time.time()
                metrics = await self.collect_metrics()
                _t_b = time.time()
                vms     = await self.get_vm_list()
                _t_c = time.time()
                nodes   = await self.get_node_stats()
                _t_d = time.time()
                self._last_phase_ms = {
                    "metrics_ms":    int((_t_b - _t_a) * 1000),
                    "vm_list_ms":    int((_t_c - _t_b) * 1000),
                    "node_stats_ms": int((_t_d - _t_c) * 1000),
                }
                self._telemetry_iter = getattr(self, "_telemetry_iter", 0) + 1

                if vms.get("error"):
                    logger.error(f"get_vm_list error: {vms['error']}")
                if nodes.get("error"):
                    logger.error(f"get_node_stats error: {nodes['error']}")

                _vm_n = len(vms.get('vms', []))
                _node_n = len(nodes.get('nodes', []))
                # Per-tick detail stays DEBUG (chatty, every ~60s).
                logger.debug(f"Telemetry: {_vm_n} VMs, {_node_n} nodes")
                # Throttled INFO liveness+state line (~every 5 min) so the operator
                # can see CS mode + VM count + why provisioning is/isn't running
                # from the INFO log and the hub (Setup → Agent Logs) WITHOUT
                # enabling DEBUG — the normalization demoted the per-tick line, so
                # this restores a periodic heartbeat that also surfaces the #1
                # question ("is CS mode on?"). cs_mode=off → VM Server will be empty.
                _now = time.time()
                if _now - getattr(self, "_last_status_log", 0.0) >= 300:
                    self._last_status_log = _now
                    _reason = None
                    if self.cs_enabled:
                        try:
                            from . import usb_provision as _up
                            _reason = _up.current_provision_reason()
                        except Exception:
                            _reason = None
                    logger.info("status: cs_mode=%s vms=%d nodes=%d%s",
                                "on" if self.cs_enabled else "off", _vm_n, _node_n,
                                f" provision={_reason}" if _reason else "")

                # Managed-crontab drift-correct — re-reconcile every ~10 min so a
                # hand-edited block is restored to the pushed content (also runs on
                # the first tick → applies the persisted config after a restart).
                # config.get is None until the operator has ever set it, so we
                # NEVER touch root's crontab on a node that doesn't use this.
                _cron = self.config.get("managed_crontab")
                if _cron is not None and (_now - getattr(self, "_last_cron_reconcile", 0.0) >= 600):
                    self._last_cron_reconcile = _now
                    try:
                        self._managed_crontab_status = await managed_crontab.apply_managed_crontab(_cron or "")
                    except Exception:  # noqa: BLE001 — telemetry must not die on a cron error
                        pass

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
                            # Agent version (pxmx repo's .NN) so the spoke can
                            # expose it via GET_AGENTS and the Hub Diagnostics
                            # page shows a real version instead of "unknown".
                            # `version` is computed at module load (get_version).
                            "agent_version": version,
                        },
                    },
                }
                await self.websocket.send(encode_frame(self.signer, msg))

                # ── Client-Simulation telemetry (Phase D1) ───────────────────
                # When CS is enabled, also push a CS_TELEMETRY frame carrying the
                # per-host Proxmox snapshot the cs spoke ingests into its
                # proxmox_states and re-relays as CS_TELEMETRY to the hub, which
                # caches it for the Simulations/VM Server view. Piggybacks on the
                # 60s tick (the bash agent pushed every 3s; HEALTH_STALE_SECS=180
                # gives ample margin). send_cs_event injects hostname + agent_id.
                _usb_sig = None   # USB present-set for the fast-tick trigger below
                if self.cs_enabled:
                    try:
                        # Classify each VM's tier by passthrough (cached ~60s) in
                        # this async context, then stamp it into the sync body.
                        _t_e = time.time()
                        try:
                            tiers = await usb_provision.compute_vm_tiers(
                                self, (vms or {}).get("vms", []) or [])
                        except Exception as _te:
                            logger.debug(f"compute_vm_tiers failed: {_te}")
                            tiers = {}
                        (self._last_phase_ms or {})["tiers_ms"] = int((time.time() - _t_e) * 1000)
                        cs_body = self._cs_telemetry_body(vms, nodes, tiers)
                        await self.send_cs_event("CS_TELEMETRY", cs_body)
                        # Signature of the present USB set (dongles). A plug/unplug
                        # must propagate in seconds, but a USB change flips none of
                        # the VM/node/prov flags — so fold it into the fast-tick
                        # diff below (was: dongles waited out the full 60s tick).
                        try:
                            _all_usb = ((cs_body.get("present_usb") or [])
                                        + (cs_body.get("unknown_usb") or [])
                                        + (cs_body.get("usb_state") or []))
                            _usb_sig = frozenset(
                                f"{(u or {}).get('bus_path') or (u or {}).get('bus') or ''}"
                                f":{(u or {}).get('vidpid') or ''}"
                                for u in _all_usb)
                        except Exception:  # noqa: BLE001 — sig is a hint only
                            _usb_sig = None
                    except Exception as e:
                        logger.debug(f"CS_TELEMETRY emit failed: {e}")

                # Near-real-time on ANY VM/node-set OR USB change — not only
                # auto-prov ops. A manually created/removed VM, a node that just
                # joined, a dongle plugged/unplugged, or the settle right AFTER an
                # auto-prov burst does NOT flip the "active" flags below, so
                # without this they'd wait out the full 60s idle tick before the
                # VM Server / Overview / USB view reflects them. Diff this tick's
                # vmid set + node count + USB present-set vs the last; on a change,
                # open a short fast-tick window so it propagates in seconds.
                try:
                    _sig = (
                        frozenset(str(v.get("vmid")) for v in (vms or {}).get("vms", [])
                                  if v.get("vmid") is not None),
                        len((nodes or {}).get("nodes", []) or []),
                        _usb_sig,
                    )
                except Exception:  # noqa: BLE001
                    _sig = None
                if _sig is not None and _sig != getattr(self, "_last_vm_sig", None):
                    # Skip the very first tick (last is None → establishing baseline).
                    if getattr(self, "_last_vm_sig", None) is not None:
                        self._vm_change_fast_until = _now + 15   # settle window (s)
                    self._last_vm_sig = _sig

                # Adaptive cadence: the auto-prov / delete / reclone state rides
                # the CS_TELEMETRY frame, so a fixed 60s tick made the WebUI lag
                # (or miss) VMs coming up. While auto-prov is ACTIVELY provisioning,
                # deleting, or recloning — OR just after ANY VM/node change (above)
                # — tick fast (~3s, the old bash cadence) so the UI shows changes in
                # near real time; fall back to 60s when idle.
                interval = 60
                if self.cs_enabled:
                    try:
                        pr = usb_provision.current_prov_run()
                        active = (bool(pr.get("running"))
                                  or any(str(it.get("status") or "") == "provisioning"
                                         for it in (pr.get("items") or []))
                                  or bool(usb_provision.current_deleting_vmids())
                                  or usb_provision.current_reclone_state().get("status") == "running")
                        if active:
                            interval = 3
                    except Exception:  # noqa: BLE001 — cadence hint only
                        pass
                if _now < getattr(self, "_vm_change_fast_until", 0):
                    interval = min(interval, 3)
                # Record the cadence chosen this tick + when we finished, so the
                # detail page can show the effective interval and the true gap
                # between telemetry frames (not just the per-phase durations).
                self._last_interval_s = interval
                self._last_tick_done_ts = time.time()
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"Telemetry push failed: {e}")
                await asyncio.sleep(10)

    def _cs_telemetry_body(self, vms_resp: Dict[str, Any],
                           nodes_resp: Dict[str, Any],
                           tiers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
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
            # Honor the real Proxmox ``template: 1`` flag first — a VM converted
            # with ``qm template`` carries this even when it has no "template"
            # tag and a non-template name (the case that misfiled templates as
            # 'Other'). int flags from /cluster/resources: 1 = template.
            if int(v.get("template", 0) or 0):
                return True
            tags = [str(t).lower() for t in (v.get("tags") or [])]
            if any(t in ("template", "tmpl", "is-template") for t in tags):
                return True
            name = str(v.get("name", "")).lower()
            return name.startswith(("template-", "tmpl-"))

        tiers = tiers or {}
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
                # Authoritative tier (t1/t2/t3) by passthrough — the cs spoke maps
                # this vmid → client hostname and stamps it on the Clients row so
                # csClassifyClient renders the correct badge (T3 especially, which
                # has no USB dongle and would otherwise fall to the T1 default).
                "tier":            tiers.get(str(v.get("vmid"))),
                "_agent_hostname": self.hostname,
            })

        # Mirror the allocator's actual range resolution so the UI shows the
        # VMID block this host really uses. The cs speak sends flat
        # vmid_start/vmid_end (default 90000/99999); when those are at the
        # default the allocator derives a per-host block from this host's
        # hostname suffix (svr-02→90025-90048), so reporting the flat default
        # here would be wrong. Resolve the same way (incl. vm_set_override).
        _usb = cs_cfg.get("usb_config") or {}
        _max_slots = int(usb_provision._cfg_first(
            _usb, ("usb_max_slots", "max_slots"), 24) or 24)
        _vstart, _vend, _batch_id, _ = usb_provision._host_vmid_range(
            self.hostname, _max_slots,
            _usb.get("vmid_start"), _usb.get("vmid_end"),
            _usb.get("vm_set_override") or cs_cfg.get("vm_set_override") or 0,
        )
        vmid_range = {"start": _vstart, "end": _vend, "batch_id": _batch_id}

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
            "delete_gate":      usb_provision.current_delete_gate(),
            "gate_averages":    usb_provision.current_gate_averages(),
            "prov_run":         usb_provision.current_prov_run(),
            "deleting_vmids":   usb_provision.current_deleting_vmids(),
            "reclone_vmids":    usb_provision.current_reclone_vmids(),
            "reclone_state":    usb_provision.current_reclone_state(),
            "managed_crontab_status": getattr(self, "_managed_crontab_status", None),
            "usb_state":        usb.get("usb_state") or [],
            "present_usb":      usb.get("present_usb") or [],
            "unknown_usb":      usb.get("unknown_usb") or [],
            # Auto-provision diagnostic — WHY the last pass provisioned nothing
            # (or did). Surfaced through the cs spoke → hub cache → WebUI
            # Auto-Provisioning card so a silent gate (no dongle_vidpids / no
            # template ids / no eligible dongles) is visible without grepping the
            # agent log. ``loop_running`` is a heartbeat (3× the 60s cadence); it
            # is False before the first tick or after the loop task has died.
            "provision": {
                "cs_enabled":         bool(self.cs_enabled),
                "loop_running":       usb_provision.current_provision_loop_running(),
                "auto_provision_on": usb_provision.current_auto_provision_on(),
                "reason":            usb_provision.current_provision_reason(),
                "halt":              usb_provision.current_provision_halt(),
                "config":           usb_provision.current_provision_cfg_snapshot(),
            },
            # ── Telemetry-freshness diagnostic ──────────────────────────────
            # Stamps HOW and WHEN this frame was produced on the agent, so the
            # VM Server detail page can show where the delay is: agent gen age,
            # per-phase collect durations (the pvesh calls that stall on a
            # loaded host), the effective cadence, and the loop iteration.
            # The cs spoke adds ingested_at and the hub adds cached_at, giving a
            # per-hop age chain: agent → spoke → hub → WebUI.
            "telemetry": {
                "gen_ts":       time.time(),                       # frame built at (agent clock)
                "agent_id":     self.agent_id,
                "hostname":     self.hostname,
                "agent_version": get_version(),
                "cs_enabled":   bool(self.cs_enabled),
                "iter":         getattr(self, "_telemetry_iter", 0),
                "interval_s":   getattr(self, "_last_interval_s", None),  # cadence of the PREVIOUS tick
                "last_tick_done_ts": getattr(self, "_last_tick_done_ts", None),
                "phase_ms":     dict(getattr(self, "_last_phase_ms", {}) or {}),
                "vm_count":     len(vms),
                "node_count":   len(nodes_list),
            },
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
            # Auto-provision + relay the cs-hub Proxmox API token so the cs spoke
            # can sim-tag VMs with NO manual key setup. Background (pvesh create
            # takes a couple seconds) so it never delays CS startup; cached +
            # re-emitted on later enables (see _ensure_cs_hub_token).
            _tok_task = asyncio.create_task(self._ensure_cs_hub_token("auto"))
            self._cs_long_ops.add(_tok_task)
            _tok_task.add_done_callback(self._cs_long_ops.discard)
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
                # sample_resources is async (Proxmox node stats via pvesh) —
                # awaited here on the same cadence as the provision pass.
                await usb_provision.sample_resources(self)
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
    # --spoke-ip is the normal way to pin the agent: supply ONLY the spoke's IP
    # (or hostname) and the agent probes its known /ws/agent endpoints to work
    # out the scheme + port + path itself (see _resolve_spoke_url →
    # discovery.resolve_agent_url). No need to know wss-vs-ws or 443-vs-8767.
    parser.add_argument("--spoke-ip", default=os.getenv("SPOKE_IP") or None,
                        help="spoke IP/host to dial; scheme+port+/ws/agent are auto-determined")
    # --spoke-url is the OPTIONAL power-user / legacy form: a fully-pinned
    # ws(s)://host:port/ws/agent. It wins over --spoke-ip. When neither is
    # supplied (and no SPOKE_IP/SPOKE_URL env) the agent auto-discovers the hub
    # box via DNS (lm-hub.<dns-suffix>) then mDNS and targets its agent listener.
    parser.add_argument("--spoke-url", default=os.getenv("SPOKE_URL") or None,
                        help="fully-pinned ws(s)://host:port/ws/agent (advanced; "
                             "prefer --spoke-ip)")
    # --id is OPTIONAL: when not supplied the agent derives its id from the
    # current OS hostname at startup, so a cloned+renamed Proxmox node reconnects
    # under a new id (correlated to the old one via the install UUID by the hub)
    # instead of being frozen to the hostname captured at install. A pinned --id
    # (install_pxmx.sh printed cmd / explicit --id) wins.
    parser.add_argument("--id", default=None)
    parser.add_argument("--secret")
    args = parser.parse_args()
    if not args.id:
        # Derived id = the bare hostname (no "-agent" suffix). A pinned --id
        # still wins; only the unpinned/derived case is affected.
        args.id = socket.gethostname()

    try:
        agent = ProxmoxAgent(args.spoke_url, args.id, args.secret,
                             spoke_ip=args.spoke_ip)
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
