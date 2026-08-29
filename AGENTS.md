# AGENTS.md — `pxmx`

**Proxmox VE module.** Two cooperating parts: a **host agent** on each Proxmox node that does the real VM work, and a **spoke** that bridges many agents into the hub.

- **Repo:** `github.com/lbockenstedt/pxmx`
- **Module type:** `module_type = "hypervisor"`
- **Canonical docs:** [`lm/docs/pxmx.md`](../lm/docs/pxmx.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Layout

| Path | Role |
| :--- | :--- |
| `src/proxmox_spoke.py`, `src/control_plane.py` | The spoke. Runs in its own container; bridges hub <-> agents (`run_agent_server`). |
| `agent/src/agent.py` | **Host agent** — runs on the Proxmox node. VM clone/destroy, VNC, USB provisioning, watchdogs. |
| `agent/src/usb_provision.py` | **The Client-Sim auto-provisioning brain lives here** (`run_provision_loop`) — not in the hub, not in the `cs` spoke. |
| `src/discovery.py`, `agent/src/discovery.py` | Vendored copies of `lm/core/src/messaging/hub_discovery.py`. |

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the agent/spoke split and where the brain lives.

## pxmx-specific gotchas

- **Two listener modes.** Standalone (**default**): agent dials `wss://<spoke>:443/ws/agent`, pinned with `--spoke-ip` because a standalone spoke does not broadcast mDNS. All-in-one (`--loopback`): agent dials the hub, which byte-proxies to the spoke on loopback `:8443`.
- **`discovery.py` is vendored twice here** and must stay byte-identical to the canonical copy in `lm/core/src/messaging/hub_discovery.py`.
- **There is both a `test/` and a `tests/` directory.** Check which one the runner picks up (`pytest.ini`) before adding tests.
- `pxmx.log` is a committed runtime artifact, not source.
- Two installers: `install_pxmx.sh` (spoke container) and `agent/install_agent.sh` (Proxmox host). They are not interchangeable.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
