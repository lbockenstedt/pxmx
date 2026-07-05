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
import json
import re
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
from .security_utils import MessageSigner
from . import cs_commands
from . import cs_sim
from . import watchdogs
from . import usb_provision
from . import pve_cmds

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


# Matches a MAC in either colon or dash form (case-insensitive): aa:bb:cc:dd:ee:ff
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)


def _looks_like_mac(s: str) -> bool:
    """True if ``s`` is a 6-octet MAC (colon or dash separators)."""
    return bool(_MAC_RE.match((s or "").strip()))


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

    Connects to the pxmx spoke (over WS on :8766), authenticates, then runs the
    telemetry/USB-provision/cs-command/watchdog loops. See the module docstring.
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

        # Hub log relay — installed ONCE here (not per-connection) so records
        # from the very first startup line onward are captured; buffered while
        # the socket is down and flushed on each connect. Requirement: the hub
        # must have every agent's logs (Setup → Agent Logs + BugFixer) without
        # needing the box's CLI.
        self._ws_log_handler = WebSocketLogHandler(self)
        self._ws_log_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
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

    def _ensure_install_uuid(self) -> str:
        """Return this agent's stable install UUID, minting + persisting it on first start.

        Mirrors the spoke-side BaseControlPlane._ensure_install_uuid: the UUID is
        created at FIRST START (not install) so cloning the agent install tree
        does not copy a UUID — a clone gets its own on first start. prep-for-imaging
        strips INSTALL_UUID so a cloned node mints a fresh one (clean new identity
        vs. a rename of the original). We trust only what lands on disk: a failed
        write returns '' (no UUID) rather than a volatile value that would differ
        every boot. The hub treats '' as "no correlation".
        """
        env_path = self._env_path()
        try:
            existing = ""
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        if line.startswith("INSTALL_UUID="):
                            existing = line.split("=", 1)[1].strip()
                            if existing:
                                return existing
            new_uuid = str(uuid.uuid4())
            lines = []
            if os.path.exists(env_path):
                with open(env_path) as f:
                    lines = [l for l in f if not l.startswith("INSTALL_UUID=")]
            lines.append(f"INSTALL_UUID={new_uuid}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            logger.info(f"Install UUID minted and saved to {env_path}")
            # Re-read so a silent write failure can't leave us reporting a UUID
            # that isn't actually on disk (which would mismatch on next start).
            with open(env_path) as f:
                for line in f:
                    if line.startswith("INSTALL_UUID="):
                        return line.split("=", 1)[1].strip()
            return ""
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

    def _pvenode_bin(self) -> str:
        """Locate the pvenode binary (same pve-manager package as pvesh)."""
        for candidate in ["/usr/bin/pvenode", "/usr/sbin/pvenode", "pvenode"]:
            if candidate == "pvenode":
                return candidate  # fall back to PATH
            if os.path.isfile(candidate):
                return candidate
        return "pvenode"

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
        """
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                err = (stderr.decode().strip() or stdout.decode().strip()
                       or f"pvenode exited {proc.returncode}")
                return {"status": "ERROR",
                        "message": f"pvenode cert set failed: {err[:300]}"}
            logger.info("INSTALL_CERT: pveproxy cert installed on %s (pvenode restart)",
                        self.hostname)
            return {"status": "SUCCESS",
                    "message": f"cert installed on {self.hostname} (pveproxy restarted)"}
        except asyncio.TimeoutError:
            return {"status": "ERROR",
                    "message": "pvenode cert set timed out (pveproxy restart?)"}
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

    async def _vm_interfaces(self, node: str, vmid: Any, rtype: str,
                             status: str) -> List[Dict[str, Any]]:
        """Best-effort per-network-interface record for one VM/CT:
        ``[{"name", "mac", "ips": [..]}]``.

        Running qemu uses the guest-agent ``network-get-interfaces`` endpoint
        (yields the guest-visible IPs AND the MAC); running lxc uses
        ``/interfaces`` (container netns — no guest agent needed). When the
        guest source is absent/unresponsive OR the VM is stopped, fall back to
        ``qm``/``pct config`` netN lines for the configured MACs (no guest IPs —
        MACs are config, available in any state). Stopped VMs therefore still
        get their MACs. Never raises; returns [] on any failure. Read-only
        pvesh GET, safe for any VMID (no execution guard).
        """
        if not node or vmid in (None, ""):
            return []
        kind = "qemu" if rtype == "qemu" else "lxc"
        interfaces: List[Dict[str, Any]] = []
        if status == "running":
            try:
                if kind == "qemu":
                    data = await asyncio.wait_for(
                        self._pvesh(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"),
                        timeout=4)
                else:
                    data = await asyncio.wait_for(
                        self._pvesh(f"/nodes/{node}/lxc/{vmid}/interfaces"),
                        timeout=4)
                interfaces = self._parse_guest_ifaces(data)
            except Exception:
                interfaces = []
        # Fall back to configured MACs when the guest source gave nothing (QGA
        # absent, stopped VM, or empty result) — MACs are config so always
        # available regardless of power state.
        if not interfaces:
            try:
                interfaces = await self._vm_net_macs(node, vmid, kind)
            except Exception:
                interfaces = []
        return interfaces

    @staticmethod
    def _parse_guest_ifaces(data: Any) -> List[Dict[str, Any]]:
        """Normalize QGA ``network-get-interfaces`` / lxc ``/interfaces`` into
        ``[{"name", "mac", "ips"}]``. QGA MAC is ``hardware-address``; lxc is
        ``hwaddr``. Loopback/link-local IPs are excluded; per-interface IPs are
        deduped. PVE wraps agent responses inconsistently (result/data/lists)
        — unwrapped here."""
        result = data
        if isinstance(data, dict):
            result = data.get("result", data.get("data", data))
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
        out: List[Dict[str, Any]] = []
        if not isinstance(result, list):
            return out
        seen_names: set = set()
        for iface in result:
            if not isinstance(iface, dict):
                continue
            name = str(iface.get("name") or iface.get("netdev") or "").strip()
            mac = str(iface.get("hardware-address") or iface.get("hwaddr") or "").strip().lower()
            # Skip the loopback / all-zeros-MAC pseudo-interfaces so they don't
            # become NetBox vminterfaces.
            if name.lower() == "lo" or mac == "00:00:00:00:00:00":
                continue
            ips: List[str] = []
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
            seen, uips = set(), []
            for ip in ips:
                if ip not in seen:
                    seen.add(ip)
                    uips.append(ip)
            key = name or mac or f"iface{len(out)}"
            if key in seen_names:
                continue
            seen_names.add(key)
            out.append({"name": name, "mac": mac, "ips": uips})
        return out

    async def _vm_net_macs(self, node: str, vmid: Any,
                           kind: str) -> List[Dict[str, Any]]:
        """Parse ``qm``/``pct config`` netN lines for the configured MACs — the
        fallback when the guest agent is absent or the VM is stopped (no guest
        IPs, but MACs are config so always available). Returns
        ``[{"name", "mac", "ips": []}]``."""
        try:
            data = await asyncio.wait_for(
                self._pvesh(f"/nodes/{node}/{kind}/{vmid}/config"), timeout=4)
        except Exception:
            return []
        cfg = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(cfg, dict):
            return []
        return self._parse_config_nets(cfg)

    @staticmethod
    def _parse_config_nets(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse a qm/pct config dict for ``netN`` entries →
        ``[{"name", "mac", "ips": []}]``.

        qemu: ``net0: "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0[,...]"`` — the MAC
              is the hex after the model (``virtio=``/``e1000=``/…).
        lxc:  ``net0: "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF[,...]"``.
        """
        out: List[Dict[str, Any]] = []
        for key, val in cfg.items():
            if not key.startswith("net") or not isinstance(val, str):
                continue
            mac, name = "", ""
            for token in val.split(","):
                token = token.strip()
                if not token or "=" not in token:
                    continue
                k, v = token.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k == "hwaddr" and _looks_like_mac(v):
                    mac = v.lower()
                elif k == "name":
                    name = v
                elif _looks_like_mac(v):
                    mac = v.lower()   # qemu: <model>=<MAC>
            if mac or name:
                out.append({"name": name or key, "mac": mac, "ips": []})
        return out

    async def _annotate_vm_interfaces(self, vms: List[Dict[str, Any]]) -> None:
        """Populate ``vm["interfaces"]`` (and the derived flat ``vm["ips"]``)
        in parallel — best-effort, bounded by a semaphore (16 concurrent pvesh
        calls) and a 12s deadline so a hung guest agent can't stall the 60s
        telemetry tick. Running VMs get guest IPs + MACs (QGA/LXC); stopped VMs
        get their configured MACs via qm/pct config. VMs not annotated before the
        deadline keep interfaces=[] (and ips=[]); they'll be filled next tick."""
        targets = [v for v in vms if v.get("node") and v.get("vmid") not in (None, "")]
        if not targets:
            return
        sem = asyncio.Semaphore(16)

        async def _one(v):
            async with sem:
                ifaces = await self._vm_interfaces(
                    v.get("node", ""), v.get("vmid"), v.get("type"), v.get("status"))
                v["interfaces"] = ifaces
                v["ips"] = [ip for i in ifaces for ip in (i.get("ips") or [])]

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_one(v) for v in targets], return_exceptions=True),
                timeout=12)
        except asyncio.TimeoutError:
            pass  # partial — VMs not yet annotated keep interfaces=[]/ips=[]

    async def _vm_pool_map(self) -> dict:
        """Best-effort ``{vmid: poolid}`` from the Proxmox ``/pools`` endpoint.

        ``/cluster/resources`` (the VM list source) doesn't carry pool
        membership, so query ``/pools`` and reverse-map member vmid → poolid.
        Some PVE versions return ``members`` inline on the ``/pools`` listing;
        others require a per-pool ``/pools/{poolid}`` detail fetch. Both are
        handled. Returns ``{}`` on any failure (never raises) — callers then
        leave VM ``pool`` blank. A VM in no pool is simply absent from the map.
        """
        try:
            pools = await self._pvesh("/pools")
            out: dict = {}
            for p in (pools if isinstance(pools, list) else []):
                if not isinstance(p, dict):
                    continue
                pid = p.get("poolid")
                if not pid:
                    continue
                members = p.get("members")
                if members is None:
                    detail = await self._pvesh(f"/pools/{pid}")
                    members = detail.get("members") if isinstance(detail, dict) else None
                for m in (members if isinstance(members, list) else []):
                    if isinstance(m, dict) and m.get("vmid") is not None:
                        # First pool seen wins; a VM shouldn't be in two pools.
                        out.setdefault(m.get("vmid"), pid)
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"pool map unavailable: {e}")
            return {}

    async def list_pools(self) -> list:
        """Best-effort Proxmox resource pool list (``[{poolid, comment}, ...]``).

        Used by the clone/create-VM UI's pool dropdown. Reads ``/pools`` (which
        lists every pool id + comment); never raises — returns ``[]`` on failure.
        """
        try:
            pools = await self._pvesh("/pools")
            out = []
            for p in (pools if isinstance(pools, list) else []):
                if not isinstance(p, dict):
                    continue
                pid = p.get("poolid")
                if not pid:
                    continue
                out.append({"poolid": pid, "comment": p.get("comment", "") or ""})
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"list_pools unavailable: {e}")
            return []

    async def list_node_isos(self, node: str) -> list:
        """ISO images available on ``node`` for the create-VM-from-ISO flow.

        Enumerates storages whose ``content`` includes ``iso`` and lists each
        storage's ISO content (Proxmox returns ``volid`` like
        ``local:iso/ubuntu-22.04.iso`` + ``size`` bytes). Returns a flat list of
        ``{volid, name, storage, size}``. ``[]`` on any failure (never raises).
        """
        out: list = []
        try:
            storages = await self._pvesh(f"/nodes/{node}/storage")
            for s in (storages if isinstance(storages, list) else []):
                if not isinstance(s, dict):
                    continue
                content = s.get("content") or ""
                if "iso" not in (content.split(",") if isinstance(content, str) else content):
                    continue
                storage = s.get("storage")
                if not storage:
                    continue
                try:
                    items = await self._pvesh(
                        f"/nodes/{node}/storage/{storage}/content",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("iso content list failed for %s/%s: %s", node, storage, e)
                    continue
                for it in (items if isinstance(items, list) else []):
                    if not isinstance(it, dict):
                        continue
                    volid = it.get("volid") or ""
                    if not volid.endswith(".iso"):
                        continue
                    out.append({
                        "volid":   volid,
                        "name":    it.get("volid", "").split("/")[-1],
                        "storage": storage,
                        "size":    it.get("size", 0) or 0,
                    })
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"list_node_isos unavailable: {e}")
            return []

    async def list_node_storages(self, node: str, content_filter: str = "images") -> list:
        """Storages on ``node`` accepting the given content type (default
        ``images`` — where a new VM's boot disk can live). Returns
        ``[{storage, type, avail, total, shared}]``. ``[]`` on failure."""
        out: list = []
        try:
            storages = await self._pvesh(f"/nodes/{node}/storage")
            for s in (storages if isinstance(storages, list) else []):
                if not isinstance(s, dict):
                    continue
                content = s.get("content") or ""
                parts = content.split(",") if isinstance(content, str) else content
                if content_filter not in parts:
                    continue
                out.append({
                    "storage": s.get("storage"),
                    "type":    s.get("type", ""),
                    "avail":   s.get("avail", 0) or 0,
                    "total":   s.get("total", 0) or 0,
                    "shared":  bool(s.get("shared", 0)),
                })
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"list_node_storages unavailable: {e}")
            return []

    async def get_vm_list(self) -> Dict[str, Any]:
        """
        All VMs and containers via local pvesh — no API credentials required.

        Each entry includes:
          unique_id  — globally unique: "<cluster>/<node>/<vmid>"
          cluster    — Proxmox cluster name (or hostname for standalone)
          node       — Proxmox node name
          vmid       — integer VMID
          type       — "qemu" or "lxc"
          name, status, cpu, mem_bytes, uptime, tags, ips,
                     vcpus, disk_gb — provisioned capacity (maxcpu / maxdisk from
                       /cluster/resources) so the Hypervisor→NetBox VM sync can
                       populate NetBox vCPUs/disk without a per-VM qm config call
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
                # Proxmox ``template: 1`` flag (set by ``qm template`` /
                # convert-to-template). /cluster/resources and the per-node
                # /qemu + /lxc endpoints all carry it. Captured here so the cs
                # telemetry ``_is_template`` heuristic can honor the real flag
                # instead of only tags/name (templates without a "template"
                # tag or a "template-" name were misfiled as 'Other').
                "template":  int(r.get("template", 0) or 0),
                "cpu":       round(r.get("cpu", 0) * 100, 1),
                "mem_bytes": r.get("mem") or r.get("maxmem", 0),
                "uptime":    r.get("uptime", 0),
                # Provisioned capacity for the Hypervisor→NetBox VM sync. Both
                # /cluster/resources and the per-node /qemu + /lxc fallback rows
                # carry maxcpu (vCPU count) and maxdisk (bytes), so no extra
                # qm config / pct config round-trip is needed here.
                "vcpus":     int(r.get("maxcpu", 0) or 0),
                "disk_gb":   round((r.get("maxdisk", 0) or 0) / 1e9, 1),
                # Proxmox resource pool membership (best-effort, from /pools).
                # /cluster/resources doesn't carry pool; _vm_pool_map builds a
                # vmid→poolid map once before the entries are constructed.
                "pool":      pool_map.get(vmid, "") if pool_map else "",
                "tags":      _parse_tags(r.get("tags")),
                # Per-NIC records: [{name, mac, ips}] — filled by
                # _annotate_vm_interfaces (running VMs get guest IPs + MACs via
                # QGA/LXC; stopped VMs get configured MACs via qm/pct config).
                # MACs land in NetBox on the VM's vminterfaces; ips is the flat
                # derivation kept for back-compat with consumers reading it.
                "interfaces": [],
                "ips":       [],   # derived flat IP list (back-compat)
            }

        # Best-effort vmid→poolid map. /cluster/resources doesn't expose pool
        # membership, so query /pools (which lists each pool's member VMs) and
        # reverse-map. A failure here is non-fatal: pool_map stays {} and every
        # VM gets pool="".
        pool_map = await self._vm_pool_map()

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
                await self._annotate_vm_interfaces(all_vms)
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

            await self._annotate_vm_interfaces(all_vms)
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

    async def send_vnc_event(self, event_type: str, data: Dict[str, Any]):
        """Emit a VNC_* frame up to the spoke for relay to the hub's browser WS.

        ``event_type`` is one of VNC_FRAME_UP / VNC_READY / VNC_ERROR /
        VNC_DISCONNECT. Best-effort and never raises — a dropped up-frame is
        tolerable (the browser RFB reconnects or times out); the Proxmox→hub
        relay task must not die on a transient socket blip. Mirrors
        ``send_cs_event`` but does not inject hostname/agent_id (the spoke
        already keys the relay by the connected agent_id)."""
        try:
            msg = {
                "header": {
                    "message_id":    str(uuid.uuid4()),
                    "timestamp":     time.time(),
                    "sender_id":     self.agent_id,
                    "destination_id": "pxmx-spoke",
                },
                "payload": {"type": event_type, "data": data},
            }
            msg["signature"] = self.signer.sign(msg)
            await self.websocket.send(json.dumps(msg))
        except Exception:
            pass

    # ── VNC console session orchestration ─────────────────────────────────────
    # The hub→spoke→agent VNC_START opens a Proxmox vncwebsocket HERE (local
    # root-authed API token) and relays frames both ways over the existing
    # agent↔spoke WS. See the plan in .claude/plans/purring-singing-breeze.md.

    async def _ensure_console_token(self) -> str:
        """Provision (once, cached) the root@pam!lm-vnc Proxmox API token used
        to create the vncproxy AND authenticate the vncwebsocket. Proxmox never
        reveals a token secret after creation, so we delete+create to get a
        fresh secret on first use and cache it in memory for the agent's
        lifetime. The secret is never logged (only its existence)."""
        if self._console_token:
            return self._console_token
        TOKEN_ID = "lm-vnc"
        USER = "root@pam"
        try:
            await self._pvesh_action(
                "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
                json_out=False, timeout=10)
        except Exception:
            pass  # token may not exist yet — expected
        data = await self._pvesh_action(
            "create", f"/access/users/{USER}/token/{TOKEN_ID}",
            "--privsep", "0", timeout=20)
        secret = str((data or {}).get("value") or "").strip() if isinstance(data, dict) else ""
        if not secret:
            raise RuntimeError("pvesh returned no token value for root@pam!lm-vnc")
        self._console_token = f"{USER}!{TOKEN_ID}={secret}"
        logger.info("Proxmox console token root@pam!lm-vnc provisioned (value not logged)")
        return self._console_token

    async def _start_vnc_session(self, session_id: str, vmid: Any,
                                 node: str, kind: str) -> str:
        """Open the Proxmox WSS for a session and spawn the relay tasks.

        Awaited synchronously by the VNC_START handler so the Proxmox ``ticket``
        (which doubles as the RFB VNC password the browser's noVNC must present
        during the security handshake) is returned to the hub in the VNC_START
        response — without it, noVNC authenticates with an empty password and
        Proxmox drops the RFB session ("Security failure" / blank console).
        The vncproxy POST + WSS open is ~1-2s (one-shot, user-initiated), an
        acceptable block of the dispatch loop — the high-volume frame relay
        stays non-blocking. Emits VNC_READY on success or VNC_ERROR on failure.
        Down-frames buffered in the session's down_q are drained to the WSS
        once it's open. Returns the ticket string; raises on failure."""
        sess = self._vnc_sessions.get(session_id)
        if not sess:
            raise RuntimeError(f"no VNC session record for {session_id}")
        k = (kind or "").lower()
        if k not in ("qemu", "lxc"):
            k = await pve_cmds.detect_guest_type(int(vmid))
        token = await self._ensure_console_token()
        px_ws, ticket, _port = await pve_cmds.open_vnc_ws(vmid, node, k, token)
        sess["px_ws"] = px_ws
        sess["ticket"] = ticket
        up_task = asyncio.create_task(self._vnc_proxmox_to_hub(session_id, px_ws))
        drain_task = asyncio.create_task(self._vnc_drain_down(session_id, px_ws, sess["down_q"]))
        sess["tasks"] = [up_task, drain_task]
        await self.send_vnc_event("VNC_READY", {"session_id": session_id})
        logger.info(f"VNC session {session_id} ready (vmid={vmid} node={node} kind={k})")
        return ticket

    async def _vnc_proxmox_to_hub(self, session_id: str, px_ws) -> None:
        """Relay Proxmox→browser frames. When the Proxmox WSS closes (VM
        stopped, ticket expired, admin disconnect), the loop exits and we tear
        the session down + tell the hub (VNC_DISCONNECT) so the browser WS closes."""
        try:
            async for raw in px_ws:
                if isinstance(raw, str):
                    raw = raw.encode()
                await self.send_vnc_event(
                    "VNC_FRAME_UP",
                    {"session_id": session_id,
                     "data": base64.b64encode(raw).decode()})
        except Exception:
            pass
        finally:
            await self._vnc_teardown(session_id, send_disconnect=True)

    async def _vnc_drain_down(self, session_id: str, px_ws, down_q: asyncio.Queue) -> None:
        """Forward buffered browser→Proxmox frames to the WSS. A ``None`` sentinel
        (put by teardown) breaks the loop so the task exits cleanly."""
        try:
            while True:
                raw = await down_q.get()
                if raw is None:
                    break
                await px_ws.send(raw)
        except Exception:
            pass

    async def _vnc_teardown(self, session_id: str, send_disconnect: bool) -> None:
        """Close the Proxmox WSS, cancel the relay tasks, drop the session.
        ``send_disconnect`` is False when the hub initiated the close (it
        already knows) and True when the Proxmox side closed (the hub needs the
        signal to close the browser WS)."""
        sess = self._vnc_sessions.pop(session_id, None)
        if not sess:
            return
        down_q = sess.get("down_q")
        if down_q is not None:
            try:
                down_q.put_nowait(None)
            except Exception:
                pass
        for task in sess.get("tasks", []):
            if not task.done():
                task.cancel()
        px_ws = sess.get("px_ws")
        if px_ws is not None:
            try:
                await px_ws.close()
            except Exception:
                pass
        if send_disconnect:
            await self.send_vnc_event("VNC_DISCONNECT", {"session_id": session_id})

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

    async def _connect_once(self):
        import websockets
        logger.info(f"pxmx-agent {version} connecting to {self.spoke_url}...")

        # TLS: a wss:// spoke_url gets an SSL context (verify-off by default for
        # the hub box's self-signed cert; LM_HUB_TLS_VERIFY=1 + LM_HUB_CA_CERT
        # verifies). ws:// stays plaintext. Mirrors BaseControlPlane._client_ssl_ctx.
        ssl_ctx = None
        _tls_mode = "plaintext (loopback/legacy)"
        if self.spoke_url.lower().startswith("wss://"):
            try:
                import ssl as _ssl
                if os.environ.get("LM_HUB_TLS_VERIFY", "0").strip() in ("1", "true", "yes") \
                        and os.environ.get("LM_HUB_CA_CERT", "").strip():
                    ssl_ctx = _ssl.create_default_context(cafile=os.environ["LM_HUB_CA_CERT"].strip())
                    _tls_mode = f"TLS verified (CA={os.environ['LM_HUB_CA_CERT'].strip()})"
                else:
                    # NOTE: the public name is ssl.create_default_context(); the
                    # unverified builder is the PRIVATE ssl._create_unverified_context()
                    # (leading underscore). Calling ssl.create_unverified_context()
                    # raises AttributeError → ssl_ctx=None → websockets rejects
                    # ssl=None on a wss:// URI ("ssl=None is incompatible with a
                    # wss:// URI") and the agent retry-loops forever. Mirrors
                    # BaseControlPlane._client_ssl_ctx in lm core.
                    ssl_ctx = _ssl._create_unverified_context()
                    _tls_mode = "TLS unverified (self-signed cert)"
            except Exception as e:
                logger.warning(f"wss SSL context build failed: {e}; connecting without TLS")
                ssl_ctx = None
                _tls_mode = "TLS disabled (context build failed)"
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
                    msg_data = json.loads(message)

                    if "signature" in msg_data and not self.signer.verify(msg_data):
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
                                snapshot_name=data.get("snapshot_name"))
                            result = {"status": "SUCCESS", **res}
                        except pve_cmds.PveError as e:
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

                    elif cmd_type == "INSTALL_CERT":
                        # Hub-brokered cert distribution: the le spoke issued/
                        # renewed a Let's Encrypt cert and the hub pushes it
                        # here to install on this node's pveproxy. We install on
                        # the LOCAL node (pvenode is inherently local); the spoke
                        # routed this to us because we own `identifier`/node.
                        result = await self.install_cert(
                            data.get("fullchain", ""), data.get("privkey", ""),
                            node=data.get("identifier") or data.get("node") or "")

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
            "prov_run":         usb_provision.current_prov_run(),
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
        args.id = f"{socket.gethostname()}-agent"

    try:
        agent = ProxmoxAgent(args.spoke_url, args.id, args.secret,
                             spoke_ip=args.spoke_ip)
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
