# pxmx Architecture

## Topology

```
                       ┌──────────────────────────┐
                       │        LM Hub            │
                       │  (vscode/lm, port 443)   │
                       │  control plane + state   │
                       └─────────────┬────────────┘
                                     │ signed WS
                                     │ (HMAC-SHA256)
                          ┌──────────┴───────────┐
                          │   pxmx spoke         │
                          │  src/proxmox_spoke.py │   ← multi-agent bridge
                          │  src/control_plane.py │   ← accepts agents on :443 (standalone default)
                          └──────────┬───────────┘
                                     │ signed WS
                  ┌──────────────────┼──────────────────┐
                  │                  │                  │
            ┌─────┴─────┐      ┌─────┴─────┐      ┌─────┴─────┐
            │ pxmx agent│      │ pxmx agent│      │ pxmx agent│
            │ (Proxmox  │      │ (Proxmox  │      │ (Proxmox  │
            │  node A)  │      │  node B)  │      │  node C)  │
            └───────────┘      └───────────┘      └───────────┘
```

- The **pxmx spoke** (`src/proxmox_spoke.py`) is the bridge between the LM Hub
  and one or more **pxmx agents** running on Proxmox hosts. It owns the
  canonical identity key `<cluster_name>/<node>/<vmid>` for every VM.
- The **pxmx control plane** (`src/control_plane.py`) runs the agent listener
  (`run_agent_server`) and the spoke self-update. The listener default is
  **`wss://0.0.0.0:443`** (standalone — the spoke lives on its own box; a
  Proxmox agent dials `wss://<spoke>:443/ws/agent` directly, agent → spoke →
  hub). `--loopback` (co-located all-in-one, `install_all.sh` only) binds
  `127.0.0.1:8443` plaintext, reached via the hub `/ws/agent` byte-proxy. The
  legacy no-cert fallback is `ws://0.0.0.0:8766`. The spoke guarantees the
  listener port is released before a new instance starts (the agent-blackout
  fix — the agent-server task is kept alive and self-heals on exit).
- The **pxmx agent** (`agent/src/agent.py`, `ProxmoxAgent`) runs **on** the
  Proxmox host. It is the only component with `qm`/`pct` clone/destroy access,
  so all VM-mutating work happens here, not in the Hub or the spoke.

## Where the cs auto-provisioning brain lives

The Client-Sim (cs) auto-provisioning "brain" — toggle gate, 1h resource
thresholds, delete-gate + 300s cooldown, `provision_halt`, `prov_run`,
VMID-gap audit, slot cap — lives in the **agent**:

> `agent/src/usb_provision.py:run_provision_loop`

It does **not** run in the LM Hub and **not** in the LM cs spoke. The LM cs
spoke (`cs/lm-spoke`) is **relay-only** — `cs/lm-spoke/src/proxmox_deploy.py`
explicitly defers "the auto-provision gate, VMID-gap audit". (The legacy
`cs/webui-spoke` does contain a brain, but LM does not use it as the active
path; the pxmx port is the single brain.)

`run_provision_loop` is a 7-layer pipeline (reconcile → toggle gate →
thresholds → provision_halt → delete gate → missing-dongle teardown → VMID-gap
audit → clone). Resource samples are a rolling 1h deque fed by
`sample_resources` each `_usb_provision_loop` tick. `provision_halt` is
reported as a `{halted, reason}` dict; the lm-spoke coerces it with `bool()`.

### Toggle key-name duality
The toggle arrives at the agent under two key names depending on which spoke
sent it, so readers take the union:

| agent reads (union)        | webui-spoke 6-key blob | lm-spoke full 27-key payload |
|----------------------------|------------------------|------------------------------|
| `usb_auto_provision`       | ✓                      | —                            |
| `auto_provision`           | —                      | ✓                            |
| `usb_missing_timeout`      | ✓                      | —                            |
| `missing_timeout`          | —                      | ✓                            |
| `usb_max_slots`            | ✓                      | —                            |
| `max_slots`                | —                      | ✓                            |

Helpers `_toggle_on(usb_cfg)` and `_cfg_first(usb_cfg, keys, default)` read the
union.

## Execution-layer safeguards (`cs_guard.py`)

The legacy cs bash agent only enforced its `case "$action"` allowlist at the
**listing** layer, so a crafted command could still reach a sim VM. The pxmx
port adds an execution-layer guard on top:

- **90000 floor** — VMIDs below 90000 can never be treated as sim VMs.
- **`PROTECTED_VMIDS`** — a configurable set (default `{1001}`) of VMIDs that
  are never mutated regardless of any flag. See
  `~/.claude/projects/-Users-lbockenstedt-vscode/memory/unified-agent-safeguards.md`.

`assert_sim_vm` / `is_sim_vm` are the two central guards every sim-VM-mutating
path goes through.

## Telemetry shape

`agent._cs_telemetry_body` builds the telemetry body the cs spoke's
`ProxmoxDeploy.ingest_telemetry` consumes, mirroring the legacy cs bash agent's
telemetry: node summary, enriched VMs, versions, VMID range,
vm-set/template-lock/provision-halt flags, and USB state from
`usb_provision.cs_usb_telemetry`. The body now also carries
`provision_halt` (the dict) and `prov_run` (the live run state) so the Hub can
surface them.

Each per-VM `usb_state[]` entry carries `vmid, bus_path, missing_since,
missing_timeout_s, shed_at, name, vidpid, prov_status`, plus `reboot_at` /
`cloned_at` when a post-clone settle reboot is pending (stamped by
`usb_state_store.set_assignment` into `post_prov_reboot[vmid]`, swept by
`_run_post_prov_reboot_queue`). The Hub's WebUI renders `reboot_at` as a
blue "Reboots in Xm" countdown badge next to the VM's status. See
`lm/docs/pxmx.md` → *Post-clone settle reboot*.

## Ported-from-cs provenance

The agent code uses a **"Phase X port of legacy `cs/proxmox/proxmox-agent.sh`…"**
docstring convention with line-range cross-references. Phases:

| Phase | What |
|-------|------|
| A     | Initial port scaffolding. |
| C     | CS watchdogs + USB-blacklist. |
| D1    | CS telemetry relay. |
| E     | Long ops + USB provision loop + token provisioning. |
| F     | `CS_STORE_PROXMOX_TOKEN` (Proxmox token transits the Hub). |
| G     | Retire the bash agent, harden the unified agent. |

When porting more cs logic, cite the legacy file + line range and the Phase.