"""pxmx spoke control plane — ``PxmxControlPlane``.

The Hub-side of the pxmx spoke: accepts pxmx host agents on the agent listener
(``run_agent_server``, lifted into the shared ``AgentHostingControlPlane``
mixin so the cs spoke can host agents too), runs the spoke self-update check
from GitHub (``perform_self_update_check``), and routes signed messages
between the LM Hub and the connected agents. Overrides ``get_service_name`` →
``"lm-pxmx"`` and guarantees the agent port is released before a new instance
starts (the v2.0.3 agent-blackout fix). Audience: pxmx developers; see the repo
``ARCHITECTURE.md``.
"""

# ── Dependency self-heal (must run BEFORE the third-party imports below) ──────
# A skewed auto-update / partial install can leave the venv missing a declared
# dep (e.g. websockets) → hard crash at `import websockets` below, crash-looping
# the spoke under Restart=always. dep_guard is stdlib-only so it imports even
# when third-party deps are absent; it parses requirements.txt, find_spec-checks
# each top-level package, and runs `pip install -r` in this venv if any are
# missing. LM_DEP_GUARD_DISABLE=1 opts out. PYTHONPATH ($INSTALL_DIR +
# $INSTALL_DIR/core/src) resolves both `core.src.dep_guard` and the bare
# `dep_guard` fallback.
import os as _os
import sys as _sys
# Prefer the hub's packaged copy of dep_guard when lm core is importable as a package.
try:
    from core.src.dep_guard import ensure_requirements as _ensure_requirements
except ImportError:  # lm core not on path as a package — bare module on core/src
    from dep_guard import ensure_requirements as _ensure_requirements
# requirements.txt sits in the pxmx repo root, one directory above this src/ folder.
_req = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                     "requirements.txt")
# Install any missing dependencies now, before the third-party imports below run.
_ensure_requirements(_req)
# Remove the bootstrap helpers so they do not leak into this module's namespace.
del _os, _sys, _ensure_requirements, _req

# asyncio runs the spoke's event loop.
import asyncio
# json reads and writes the on-disk agent telemetry cache.
import json
# time stamps the cache and pauses briefly before a self-update exit.
import time
# pathlib locates the cache file relative to this source file.
import pathlib
# logging reports cache and startup events.
import logging
# argparse parses the command-line flags when run as a script.
import argparse
# os checks/replaces cache files and hard-exits after a self-update.
import os
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, Optional
# Shared base class with the agent-listener machinery; packaged path first, bare path as fallback.
try:
    from core.src.messaging.agent_hosting import AgentHostingControlPlane
except ImportError:
    from messaging.agent_hosting import AgentHostingControlPlane

# Use the fleet's shared logging configuration, trying the bare module and then the package.
try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    # Neither copy of logging_setup is available: define a minimal equivalent locally.
    except ImportError:
        import logging as _logging
        # Same log line and timestamp format the rest of the fleet uses.
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            """Fallback logging setup: same format as the fleet, optionally also writing to a file.
            """
            # Log to the file and the console when a log file is given, else use the default
            # handler.
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            # force=True replaces any handlers an earlier import may have installed.
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
# Configure logging once at import time so every later message is formatted consistently.
configure_logging()
# Logger for this control plane's own messages.
logger = logging.getLogger("PxmxControlPlane")


class PxmxControlPlane(AgentHostingControlPlane):
    """Hub-side control plane for pxmx agents (see module docstring).

    The generic agent-listener machinery (bind modes, ``_agent_handler`` auth
    + pending-approval flow, command routing, relay-up, approve/revoke) lives
    in the ``AgentHostingControlPlane`` mixin shared with the cs spoke. This
    subclass supplies the pxmx-specific knobs (env-var names, config path,
    always-on listener) and the pxmx telemetry caching + config re-push hooks.
    """

    # pxmx-specific tuning of the mixin's class attrs.
    # This spoke manages a hypervisor (used by the hub to classify it).
    MODULE_TYPE = "hypervisor"
    # Environment variable names the mixin reads for the agent port/loopback/listener settings.
    AGENT_PORT_ENV = "LM_PXMX_AGENT_PORT"
    AGENT_LOOPBACK_ENV = "LM_PXMX_AGENT_LOOPBACK"
    AGENT_LISTENER_ENV = "LM_PXMX_AGENT_LISTENER"
    # Where the agent-side config lives on a Proxmox host.
    AGENT_CONFIG_PATH = "/etc/lm-agent/config.json"
    # pxmx always serves the agent listener (backward compatible with existing
    # installs that never set LM_PXMX_AGENT_LISTENER).
    AGENT_LISTENER_OPT_IN = False
    # Agents connect on 8443 (loopback and wss); 8766 is the legacy fallback port.
    AGENT_LOOPBACK_PORT = 8443
    AGENT_WSS_PORT = 8443
    AGENT_FALLBACK_PORT = 8766

    def get_service_name(self) -> str:
        """Name of the systemd unit this spoke runs as (used for restarts and self-update)."""
        # The systemd unit is lm-pxmx.service.
        return "lm-pxmx"

    def perform_self_update_check(self) -> bool:
        """Override to guarantee the agent port is released before the new
        instance starts.

        lm core v0.27.98+ already calls os._exit(0) inside the base implementation and
        never returns, so this code is only reached on older lm core versions.
        """
        # Run the shared self-update; True means new code was pulled.
        changed = super().perform_self_update_check()
        # New code arrived: give logs a moment to flush, then hard-exit so systemd restarts us and
        # the agent port is freed for the new process.
        if changed:
            time.sleep(0.2)
            os._exit(0)
        # No update (or an older core that returns): report the result to the caller.
        return changed

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        """Create the control plane and load any cached agent telemetry from disk."""
        # Let the mixin set up identity, secrets and the hub connection details.
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        # Disk cache — survives service restarts; served as stale data until
        # agents reconnect. Stored next to this file's package root
        # (e.g. /opt/lm/pxmx/agent_cache.json).
        self._disk_cache_path = str(pathlib.Path(__file__).resolve().parent.parent / "agent_cache.json")
        # Start empty, then fill from the on-disk cache if one exists.
        self.disk_cache: Dict[str, Any] = {}
        self._load_disk_cache()

    # ── Disk cache ────────────────────────────────────────────────────────────

    def _load_disk_cache(self):
        """Load persisted agent telemetry from disk on startup."""
        # A missing or corrupt cache file must not prevent startup.
        try:
            # Only read the file if a previous run saved one.
            if os.path.exists(self._disk_cache_path):
                with open(self._disk_cache_path) as f:
                    data = json.load(f)
                # Keep the per-agent records and log how stale they are (in hours).
                self.disk_cache = data.get("agents", {})
                age_h = (time.time() - data.get("saved_at", 0)) / 3600
                logger.info(
                    f"Loaded agent disk cache: {len(self.disk_cache)} agent(s), {age_h:.1f}h old"
                )
        # Unreadable/corrupt cache: carry on with an empty cache.
        except Exception as e:
            logger.warning(f"Could not load agent disk cache: {e}")

    def _save_disk_cache(self):
        """Persist connected agent telemetry to disk (atomic write)."""
        # Write failures are logged, never raised (telemetry must keep flowing).
        try:
            # Snapshot only the fields the UI needs for each connected agent, plus a save timestamp.
            payload = {
                "saved_at": time.time(),
                "agents": {
                    aid: {
                        "hostname":      info.get("hostname", aid),
                        "cluster_name":  info.get("cluster_name", aid),
                        "last_seen":     info.get("last_seen", 0),
                        "nodes":         info.get("nodes", []),
                        "vms":           info.get("vms", []),
                        "agent_metrics": info.get("agent_metrics", {}),
                    }
                    for aid, info in self.connected_agents.items()
                },
            }
            # Write to a temp file then rename over the real one, so a crash never leaves half a
            # file.
            tmp = self._disk_cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._disk_cache_path)
            # Keep the in-memory copy in line with what was just saved.
            self.disk_cache = payload["agents"]
        # Disk full / permissions: log and continue.
        except Exception as e:
            logger.warning(f"Could not write agent disk cache: {e}")

    # ── Subclass hooks ────────────────────────────────────────────────────────

    async def _on_agent_registered(self, agent_id: str) -> None:
        """Re-push stored PVE credentials to a freshly-connected agent so a
        reconnect after a spoke restart picks up its saved config."""
        # Look up the config the hub previously pushed for this agent (if the pxmx module is
        # loaded).
        pxmx_mod = self.modules.get("pxmx")
        stored_cfg = pxmx_mod.agent_configs.get(agent_id) if pxmx_mod else None
        # Only re-push when there is something saved.
        if stored_cfg:
            try:
                # Send the saved config back down to the agent.
                await self.send_to_agent("UPDATE_CONFIG", stored_cfg, agent_id=agent_id)
                logger.info(f"Re-pushed stored config to agent '{agent_id}'")
            # Sending failed (e.g. the agent dropped again); it will be retried on its next
            # registration.
            except Exception as _e:
                logger.warning(f"Failed to re-push config to agent '{agent_id}': {_e}")

    async def _on_agent_telemetry(self, agent_id: str, rec: Optional[Dict[str, Any]],
                                  data: Dict[str, Any]) -> None:
        """Cache Proxmox nodes/vms/cluster + agent_metrics, persist the disk
        cache, and mirror the raw telemetry into the module telemetry_cache
        (served by ProxmoxSpoke for fast UI reads)."""
        # Update the agent's in-memory record when we are tracking it.
        if rec is not None:
            # Copy cluster name, node list, VM list and agent metrics out of the telemetry payload.
            rec["cluster_name"] = data.get("cluster_name", agent_id)
            rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
            rec["vms"]          = data.get("vms", {}).get("vms", [])
            rec["agent_metrics"] = data.get("metrics", {})
            # Distinct from "last_seen" (also bumped by plain AGENT_HEARTBEATs,
            # which carry no nodes/vms) — this marks exactly when THIS agent's
            # nodes/vms snapshot was taken, so the hub's cross-agent aggregator
            # (pxmx_node_vm_aggregation._freshest_by_key) can pick the freshest
            # cluster-mate's report instead of an arbitrary first-seen one.
            rec["telemetry_ts"] = time.time()
            # Persist the refreshed telemetry so it survives a spoke restart.
            self._save_disk_cache()
        # Also hand the raw telemetry to the ProxmoxSpoke module for fast UI reads.
        if "pxmx" in self.modules and hasattr(self.modules["pxmx"], "telemetry_cache"):
            self.modules["pxmx"].telemetry_cache[agent_id] = data

    # ── Spoke startup ─────────────────────────────────────────────────────────

    async def run(self):
        """Main spoke entrypoint — start the Hub connection and the agent
        listener (self-healing).

        ``_start_agent_server_task`` (mixin) restarts the listener if its task
        ever dies, so the agent port is never left dark until a unit restart
        (the v2.0.3 blackout fix). pxmx always serves the listener.
        """
        # Announce which hub we are about to connect to.
        logger.info(f"Starting pxmx spoke → {self.hub_url}")

        # Start (and keep restarting) the agent listener task.
        self._start_agent_server_task()

        # Imported here to avoid a circular import; the module needs this control plane object.
        from proxmox_spoke import ProxmoxSpoke
        # Register the Proxmox module so hub messages for "pxmx" are routed to it.
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {}, control_plane=self)
        self.register_module("pxmx", pxmx_spoke)

        # Hand over to the base class run loop (hub connection, message routing).
        await super().run()


# Script entry point: parse flags/environment, build the control plane, and run it.
if __name__ == "__main__":
    # os reads environment defaults; socket provides the hostname for a default spoke id.
    import os
    import socket
    # Command-line flags, each defaulting to its environment variable.
    parser = argparse.ArgumentParser()
    # --id is OPTIONAL: when not supplied the spoke derives its id from the
    # current OS hostname at startup, so a cloned+renamed container reconnects
    # under a new id (correlated to the old one via the install UUID by the hub)
    # instead of being frozen to the hostname captured at install. A pinned --id
    # (install_all.sh / explicit --id) wins.
    parser.add_argument("--id",         default=os.getenv("SPOKE_ID") or None)
    # Shared secret agents use to authenticate to this spoke.
    parser.add_argument("--secret",     default=os.getenv("SPOKE_SECRET", ""))
    # Secret for signing messages to the hub; a bare --hub-secret means empty.
    parser.add_argument("--hub-secret", nargs='?', default=os.getenv("HUB_SECRET", ""), const="")
    # --hub is OPTIONAL: when neither --hub nor HUB_URL is supplied the spoke
    # auto-discovers the hub via DNS (lm-hub.<dns-suffix>) then mDNS
    # (_lm-hub._tcp.local.) — see BaseControlPlane.run + src.discovery.
    parser.add_argument("--hub",        default=os.getenv("HUB_URL") or None)
    # Parse the flags.
    args = parser.parse_args()
    # No id given anywhere: derive one from the current hostname.
    if not args.id:
        args.id = f"{socket.gethostname()}-spoke"

    # Build the control plane (an empty secret becomes None) and run it until stopped.
    cp = PxmxControlPlane(args.id, args.secret or None, args.hub_secret, args.hub)
    asyncio.run(cp.run())