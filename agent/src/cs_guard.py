"""Client-Simulation safety guards for the unified pxmx agent.

Every mutating VM command funnels through :func:`assert_sim_vm` so the agent
can never touch a non-simulation VM or a protected system container, regardless
of what the hub sends down. This is an **execution-layer** guard.

The legacy cs bash agent (``cs/proxmox/proxmox-agent.sh``) enforced the 90000
floor and ``PROTECTED_VMIDS`` only at the *listing / allocation* layer — the
90000 base was allocation-only (``start_vmid=90000+...``) and
``PROTECTED_VMIDS = {1001}`` lived only in the embedded-Python ``reclone_info``
telemetry/listing helper. The command dispatch ``case "$action"`` never checked
either, so a ``delete_vm 1001`` or ``delete_vm 500`` would have been executed.
The Python port closes that hole at the execution layer.

Guards are consulted only on the CS command path (gated by
``client_simulation.enabled``); a non-CS host never reaches this code.
"""

# logging reports invalid protected-VMID configuration.
import logging
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, Iterable, Optional, Set

# Share the main agent logger so guard warnings appear alongside other agent output.
logger = logging.getLogger("PxmxAgent")

# Simulation VMIDs are allocated 90001+ (per-host block
#   90000 + (id_num - 1) * VMID_BLOCK_STRIDE + 1,  stride 24).
# Nothing below this floor may be mutated by a CS command — those are real
# customer / system VMs on a shared Proxmox host.
SIM_VMIN: int = 90000

# Hard-default protected set: the hub LXC container. The cs convention is
# "Hub always runs in LXC container ID 1001". The hub may override or extend
# this per host via client_simulation.protected_vmids; if it supplies that
# list it is used as-is (full per-host control, including removing 1001).
# If it is absent, this default protects the hub container so it is never
# accidentally left unprotected.
DEFAULT_PROTECTED_VMIDS: Set[int] = {1001}


class GuardError(Exception):
    """Raised when a command targets a VM the guard refuses to touch."""


def resolve_protected_vmids(cs_cfg: Optional[Dict[str, Any]]) -> Set[int]:
    """Return the protected-VMID set for a host given its client_simulation cfg.

    If ``cs_cfg["protected_vmids"]`` is present (a list of ints) it is used
    as-is — full per-host control, including removing 1001. Otherwise the
    default ``{1001}`` protects the hub container.
    """
    # No client_simulation config at all: fall back to protecting the hub container.
    if not cs_cfg:
        return set(DEFAULT_PROTECTED_VMIDS)
    # Look for a hub-supplied override of the protected list.
    cfg_list = cs_cfg.get("protected_vmids")
    # Key absent (as opposed to an empty list): use the default protection.
    if cfg_list is None:
        return set(DEFAULT_PROTECTED_VMIDS)
    # Coerce every entry to int so string VMIDs from JSON still match.
    try:
        return {int(v) for v in cfg_list}
    # A non-iterable or non-numeric entry means the config is broken; stay safe with the default.
    except (TypeError, ValueError):
        logger.warning(
            f"client_simulation.protected_vmids invalid ({cfg_list!r}); "
            f"using default {DEFAULT_PROTECTED_VMIDS}"
        )
        return set(DEFAULT_PROTECTED_VMIDS)


def assert_sim_vm(vmid: Any, protected: Iterable[int], *,
                  sim_min: int = SIM_VMIN) -> int:
    """Return ``int(vmid)`` if it is a mutable simulation VM, else raise GuardError.

    Checks (in order):
      1. ``vmid`` parses to an int and is ``>= sim_min`` (the 90000 floor).
      2. ``vmid`` is not in the protected set.
    """
    # Accept strings or ints; anything that is not a whole number is refused outright.
    try:
        vid = int(vmid)
    # Non-numeric VMID from the hub: refuse with a clear message.
    except (TypeError, ValueError):
        raise GuardError(f"invalid vmid {vmid!r} — expected an integer")
    # Check the protected set first so targeting the hub container (e.g. 1001,
    # which is also below the sim floor) logs an unambiguous "protected" refusal
    # rather than a generic range message.
    if vid in set(protected):
        raise GuardError(
            f"vmid {vid} is protected (hub/system container) — cannot be managed"
        )
    # Anything below the simulation floor is a real customer/system VM -- never touch it.
    if vid < sim_min:
        raise GuardError(
            f"vmid {vid} is outside the Client-Simulation range (>= {sim_min}) "
            f"— refusing to touch a non-simulation VM"
        )
    # Passed both checks: this VMID is a simulation VM we may mutate.
    return vid


def is_sim_vm(vmid: Any, protected: Iterable[int], *,
              sim_min: int = SIM_VMIN) -> bool:
    """Non-raising variant of :func:`assert_sim_vm` for batch filtering."""
    # Reuse the strict check and translate its outcome into True/False.
    try:
        assert_sim_vm(vmid, protected, sim_min=sim_min)
        return True
    # The guard refused this VMID, so it is not a manageable simulation VM.
    except GuardError:
        return False