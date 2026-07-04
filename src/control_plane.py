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
try:
    from core.src.dep_guard import ensure_requirements as _ensure_requirements
except ImportError:  # lm core not on path as a package — bare module on core/src
    from dep_guard import ensure_requirements as _ensure_requirements
_req = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                     "requirements.txt")
_ensure_requirements(_req)
del _os, _sys, _ensure_requirements, _req

import asyncio
import json
import time
import pathlib
import logging
import argparse
import os
from typing import Any, Dict, Optional
try:
    from core.src.messaging.agent_hosting import AgentHostingControlPlane
except ImportError:
    from messaging.agent_hosting import AgentHostingControlPlane

try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
configure_logging()
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
    MODULE_TYPE = "hypervisor"
    AGENT_PORT_ENV = "LM_PXMX_AGENT_PORT"
    AGENT_LOOPBACK_ENV = "LM_PXMX_AGENT_LOOPBACK"
    AGENT_LISTENER_ENV = "LM_PXMX_AGENT_LISTENER"
    AGENT_CONFIG_PATH = "/etc/lm-agent/config.json"
    # pxmx always serves the agent listener (backward compatible with existing
    # installs that never set LM_PXMX_AGENT_LISTENER).
    AGENT_LISTENER_OPT_IN = False
    AGENT_LOOPBACK_PORT = 8443
    AGENT_WSS_PORT = 8443
    AGENT_FALLBACK_PORT = 8766

    def get_service_name(self) -> str:
        return "lm-pxmx"

    def perform_self_update_check(self) -> bool:
        """Override to guarantee the agent port is released before the new
        instance starts.

        lm core v0.27.98+ already calls os._exit(0) inside the base implementation and
        never returns, so this code is only reached on older lm core versions.
        """
        changed = super().perform_self_update_check()
        if changed:
            time.sleep(0.2)
            os._exit(0)
        return changed

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        # Disk cache — survives service restarts; served as stale data until
        # agents reconnect. Stored next to this file's package root
        # (e.g. /opt/lm/pxmx/agent_cache.json).
        self._disk_cache_path = str(pathlib.Path(__file__).resolve().parent.parent / "agent_cache.json")
        self.disk_cache: Dict[str, Any] = {}
        self._load_disk_cache()

    # ── Disk cache ────────────────────────────────────────────────────────────

    def _load_disk_cache(self):
        """Load persisted agent telemetry from disk on startup."""
        try:
            if os.path.exists(self._disk_cache_path):
                with open(self._disk_cache_path) as f:
                    data = json.load(f)
                self.disk_cache = data.get("agents", {})
                age_h = (time.time() - data.get("saved_at", 0)) / 3600
                logger.info(
                    f"Loaded agent disk cache: {len(self.disk_cache)} agent(s), {age_h:.1f}h old"
                )
        except Exception as e:
            logger.warning(f"Could not load agent disk cache: {e}")

    def _save_disk_cache(self):
        """Persist connected agent telemetry to disk (atomic write)."""
        try:
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
            tmp = self._disk_cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._disk_cache_path)
            self.disk_cache = payload["agents"]
        except Exception as e:
            logger.warning(f"Could not write agent disk cache: {e}")

    # ── Subclass hooks ────────────────────────────────────────────────────────

    async def _on_agent_registered(self, agent_id: str) -> None:
        """Re-push stored PVE credentials to a freshly-connected agent so a
        reconnect after a spoke restart picks up its saved config."""
        pxmx_mod = self.modules.get("pxmx")
        stored_cfg = pxmx_mod.agent_configs.get(agent_id) if pxmx_mod else None
        if stored_cfg:
            try:
                await self.send_to_agent("UPDATE_CONFIG", stored_cfg, agent_id=agent_id)
                logger.info(f"Re-pushed stored config to agent '{agent_id}'")
            except Exception as _e:
                logger.warning(f"Failed to re-push config to agent '{agent_id}': {_e}")

    async def _on_agent_telemetry(self, agent_id: str, rec: Optional[Dict[str, Any]],
                                  data: Dict[str, Any]) -> None:
        """Cache Proxmox nodes/vms/cluster + agent_metrics, persist the disk
        cache, and mirror the raw telemetry into the module telemetry_cache
        (served by ProxmoxSpoke for fast UI reads)."""
        if rec is not None:
            rec["cluster_name"] = data.get("cluster_name", agent_id)
            rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
            rec["vms"]          = data.get("vms", {}).get("vms", [])
            rec["agent_metrics"] = data.get("metrics", {})
            self._save_disk_cache()
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
        logger.info(f"Starting pxmx spoke → {self.hub_url}")

        self._start_agent_server_task()

        from proxmox_spoke import ProxmoxSpoke
        pxmx_spoke = ProxmoxSpoke(self.spoke_id, {}, control_plane=self)
        self.register_module("pxmx", pxmx_spoke)

        await super().run()


if __name__ == "__main__":
    import os
    import socket
    parser = argparse.ArgumentParser()
    # --id is OPTIONAL: when not supplied the spoke derives its id from the
    # current OS hostname at startup, so a cloned+renamed container reconnects
    # under a new id (correlated to the old one via the install UUID by the hub)
    # instead of being frozen to the hostname captured at install. A pinned --id
    # (install_all.sh / explicit --id) wins.
    parser.add_argument("--id",         default=os.getenv("SPOKE_ID") or None)
    parser.add_argument("--secret",     default=os.getenv("SPOKE_SECRET", ""))
    parser.add_argument("--hub-secret", nargs='?', default=os.getenv("HUB_SECRET", ""), const="")
    # --hub is OPTIONAL: when neither --hub nor HUB_URL is supplied the spoke
    # auto-discovers the hub via DNS (lm-hub.<dns-suffix>) then mDNS
    # (_lm-hub._tcp.local.) — see BaseControlPlane.run + src.discovery.
    parser.add_argument("--hub",        default=os.getenv("HUB_URL") or None)
    args = parser.parse_args()
    if not args.id:
        args.id = f"{socket.gethostname()}-spoke"

    cp = PxmxControlPlane(args.id, args.secret or None, args.hub_secret, args.hub)
    asyncio.run(cp.run())