# Architecture & Topology

This is the shared backbone page for the Lab Manager (LM) system. It describes the hub/spoke/agent mesh, the WebSocket + TLS scheme, discovery, message signing, onboarding, log relay, self-update, and state/tenancy. Every module page cross-references this one. The canonical copy lives in `lm/docs/`; each repo carries a verbatim copy in its own `docs/`.

## What LM is

LM is a zero-trust hub/spoke/agent management mesh for a lab/DC lab. One **hub** (the `lm` repo) is the control plane + WebUI + state store. It talks to many **spokes**, each wrapping one external system (a Proxmox cluster, an OPNsense firewall, NetBox IPAM, ClearPass NAC, an LDAP directory, Kea DHCP, Unbound DNS, a fleet of switches, a certbot ACME producer, a client-simulation engine). A few spokes **bridge** further out to **agents** that run on remote hosts (pxmx per-host agents on Proxmox nodes, GenericLeafAgent leaf agents, bugfixer as an agent-type client).

```
                        ┌──────────────┐
   browser (WebUI) ─────│   LM hub     │  lm repo: core/src + WebUI + generic_agent + agent
                        │  (uvicorn)   │  0.0.0.0:443 wss  (or 0.0.0.0:443 plain, no cert)
                        └──────┬───────┘
            ws/wss over /ws/spoke (one spoke = one module_type)
          ┌────────┬────────┼────────┬────────┬────────┬────────┐
         spokes   spokes   spokes   spokes   spokes   spokes   spokes
        (pxmx)   (cs)    (netbox)(opnsense)(cppm)  (ldap)  (dns/dhcp/nw/le …)
          │        │                                      
          │  relay-only for Proxmox                       
          ▼                                              
   pxmx host agents  (wss to pxmx spoke :443 standalone [DEFAULT, agent→spoke→hub]; or hub /ws/agent → spoke loopback :8443 [all-in-one, --loopback/install_all only])
   GenericLeafAgent leaf agents  (ws/wss to hub /ws/spoke or a SpokeGateway)
   bugfixer  (agent-type WS client of the hub, not a spoke)
```

## Topology in detail

- **Hub** — `lm/core/src/main.py` (`LabManagerHub`) + `lm/core/src/api.py` (FastAPI). Single uvicorn server on `0.0.0.0:LM_TLS_PORT` (443) serving the WebUI, the REST API, and the WebSocket routes. Background loops run sync, discovery sweeps, key rotation, cert distribution, etc.
- **Spokes** — subclasses of `core/src/messaging/control_plane.py::BaseControlPlane` (and the spoke logic class subclasses `core/src/base_spoke.py::BaseSpoke`). One spoke instance per module_type: `hypervisor` (pxmx), `simulation` (cs), `ipam` (netbox), `firewall` (opnsense), `nac` (cppm), `directory` (ldap), `dns`, `dhcp`, `nw`, `certificates` (le). Each spoke dials the hub over `/ws/spoke`.
- **Agents** — three flavors:
  - **pxmx per-host agents** run on Proxmox nodes (`pxmx/agent/src/agent.py`). They dial the **pxmx spoke's** agent listener (not the hub directly) over wss; the pxmx spoke relays their frames up to the hub wrapped in `AGENT_RELAY_UP`. This is the path for VM lifecycle, VNC, USB auto-provisioning, and all `CS_*` sim traffic.
  - **GenericLeafAgent** leaf agents (`lm/generic_agent/src/agent.py`) dial the hub `/ws/spoke` (or a SpokeGateway). They are the "call home, then morph into a role later" shape used by the agent-spoke role loader.
  - **bugfixer** is an **agent-type WS client** of the hub (`module_type="agent"`), not a spoke — it consumes hub logs and can trigger spoke self-updates; it does not register a spoke module.
- **Bridges:**
  - pxmx spoke bridges hub ↔ pxmx-agent (`pxmx/src/control_plane.py` `run_agent_server`).
  - `core/src/gateway/spoke_gateway.py::SpokeGateway` bridges hub ↔ leaf agents on `0.0.0.0:8767` (legacy path).
  - `core/src/gateway/cs_bridge.py::CSBridgePoller` polls the cs spoke inbox for CS-enabled pxmx agents and relays `CS_COMMAND` down, acks terminal results, syncs USB config.
- **Where the "brain" lives:** the Client-Sim VM auto-provisioning brain is the **pxmx agent** (`pxmx/agent/src/usb_provision.py:run_provision_loop`) — *not* the hub, *not* the cs spoke. The cs spoke is **relay-only** for Proxmox (it ingests telemetry and surfaces a `provision` diagnostic, but the gate/VMID-gap-audit/clone logic runs in the agent).

## WebSocket + TLS scheme

| Link | Same-box | Remote |
|---|---|---|
| spoke → hub | `wss://127.0.0.1:443/ws/spoke` (loopback, verify-off) | `wss://<hub>:443/ws/spoke` |
| pxmx agent → pxmx spoke (standalone, **default**) | n/a (spoke is on its own box) | `wss://<spoke>:443/ws/agent` (agent → spoke → hub; agent pinned via `--spoke-ip` — just the spoke's IP; the agent auto-determines the scheme/port/`/ws/agent` path by probing) |
| pxmx agent → hub `/ws/agent` (loopback, all-in-one `--loopback` only) | `wss://127.0.0.1:443/ws/agent` (loopback) | `wss://<hub>:443/ws/agent` (hub byte-proxies to pxmx spoke loopback :8443) |
| leaf agent → hub/gateway | `wss://127.0.0.1:443/ws/spoke` (loopback, verify-off) | `wss://<hub>:443/ws/spoke` |

- **Hub unified server:** one uvicorn process on `0.0.0.0:LM_TLS_PORT` (default **443**), `wss` when `LM_TLS_CERT`/`LM_TLS_KEY` are set, plaintext otherwise. WebSocket routes: `/ws/spoke` (spoke + agent control), `/ws/console/{session_id}` (browser ↔ Proxmox VNC byte relay), `/sim/ws` (cs telemetry). HTTP routes: `/api/*`, `/setup/*`, `/auth/*`, `/admin/*`, `/sim/api/*`, plus the mounted WebUI.
- **Co-located (loopback):** same-box spokes/agents dial `wss://127.0.0.1:443/ws/spoke` (or `/ws/agent`) with TLS verify OFF — the single :443 listener serves loopback too, so same-box traffic isn't a separate port. (The pxmx spoke's own agent listener is loopback `127.0.0.1:8443` **only on the co-located all-in-one path** — `install_all.sh` passes `--loopback`; reached via the hub `/ws/agent` byte-proxy, not advertised externally. A standalone pxmx spoke instead serves `wss://0.0.0.0:443` directly — see below.)
- **pxmx agent listener:** `LM_PXMX_AGENT_PORT` — **443 (standalone DEFAULT)** `wss://0.0.0.0:443` (the spoke lives on its own box; a Proxmox agent dials `wss://<spoke>:443/ws/agent` directly — agent → spoke → hub; the agent is **pinned** via `--spoke-ip` — auto-determined from just the spoke's IP by probing — since a standalone spoke does not broadcast `_lm-hub` mDNS); **8443 loopback** (`LM_PXMX_AGENT_LOOPBACK=1`, `--loopback`/`install_all` co-located only) bound `127.0.0.1:8443` plaintext, reached via the hub `/ws/agent` byte-proxy — agent → hub → spoke. Legacy no-cert fallback **8766**. mDNS TXT `agent_port` advertises `443` (the hub's external surface on the all-in-one path); the loopback 8443 is NOT advertised. `/api/pxmx/agent-install-cmd` returns a `--spoke-ip <host>` install command. See [pxmx.md "Agent listener modes"](pxmx.md).
- **No-cert fallback:** if `LM_TLS_CERT`/`LM_TLS_KEY` are unset, the hub serves a single `0.0.0.0:443` plaintext listener and discovery returns `ws://<host>:443/ws/spoke` — backward compatible.

## Discovery (mDNS + DNS)

`hub_discovery.py::discover_hub_url(timeout, agent_listener=False)` resolves where a spoke/agent should dial.

- **mDNS:** the hub broadcasts `_lm-hub._tcp.local.` (`lm-hub._lm-hub._tcp.local.`) via `zeroconf` (optional dep; skipped gracefully). TXT records: `version`, `agent_port` (always), and `tls_port` **only when** a cert is configured or `LM_HUB_ADVERTISE_TLS=1` (lets a reverse-proxy/TLS-terminator shape advertise TLS without owning the cert).
- **DNS:** tries `lm-hub.<search-domain>`, `lm-hub.local` (Avahi), bare `lm-hub`. DNS has no TXT → always returns `ws://` (pin `--hub wss://...` for a TLS remote hub reached only by DNS).
- **Scheme selection:** same-box → `wss://127.0.0.1:443/ws/spoke` (verify-off, loopback on the single :443 listener); remote + mDNS TXT `tls_port` → `wss://<ip>:443`; remote no TXT → `ws://<ip>:443` (no-cert hub). `agent_listener=True` reads TXT `agent_port` (advertised **443** on both deployments) → `wss://<hub>:443/ws/agent` — this is the **loopback/all-in-one** path (agent → hub → spoke). On the **standalone** path (agent → spoke → hub, the default) the pxmx spoke does NOT broadcast `_lm-hub` mDNS, so the agent cannot auto-discover it and **must be pinned** with `agent/install_agent.sh --spoke-ip <spoke>` (the installer prints this).
- **Same-box = IP-equality, NOT mDNS receipt.** mDNS crosses the LAN (L2-scoped), so hearing the hub over mDNS does not mean same-box. `is_hub_local(hub_ip)` compares the resolved/mDNS hub IP against this box's own interface IPv4s (UDP-connect-to-`223.255.255.1` + psutil, loopback included). When same-box, the caller dials `127.0.0.1:443` on the same unified listener instead of the LAN IP.
- **Four byte-identical vendored copies** of `hub_discovery.py` must move together: `lm/core/src/messaging/hub_discovery.py` (canonical), `pxmx/src/discovery.py`, `pxmx/agent/src/discovery.py`, `lm/generic_agent/src/hub_discovery.py`.

## TLS trust model

- **Verify-OFF by default** — `ssl._create_unverified_context()` (`CERT_NONE`): traffic is encrypted but the self-signed hub cert is **not** authenticated (on-path MITM-able; lab-acceptable). This is the default for every spoke/agent client (`BaseControlPlane._client_ssl_ctx`, mirrored in the pxmx agent and GenericLeafAgent).
- **Opt-in verification** is an **install flag**, not a hand-edited env: `--tls-verify` (+ optional `--tls-ca-cert <path>`) on `install_all.sh`, `install_pxmx.sh`, `install_cs.sh`, `generic_agent/install_github.sh` (+ an `install_menu.sh` prompt). It sets `LM_HUB_TLS_VERIFY=1` + `LM_HUB_CA_CERT=<path>` in the spoke/agent `.env`. Re-install toggling the flag `sed`-updates the `.env` (no stale setting).
  - `install_all.sh` defaults the CA to the hub's own generated cert (`$BASE_DIR/certs/hub.crt`) when co-located.
  - Standalone installers (pxmx/cs) **require** `--tls-ca-cert` (no local hub cert to default to).
  - generic-agent defaults to `/opt/lm/certs/hub.crt` if present, else requires it.
- **Hub cert:** self-signed via openssl (CN=lm-hub, SAN `IP:127.0.0.1` + `DNS:lm-hub` + `DNS:lm-hub.local`), 3650 days, generated by `install_all.sh`/`install_pxmx.sh`.
- **Non-root 443:** systemd `AmbientCapabilities=CAP_NET_BIND_SERVICE` (inherits to the nohup'd child) so `svc_lm` binds 443.
- **Fail-fast:** if the hub's cert load fails it aborts rather than silently serving plaintext on 0.0.0.0:443.

## Message signing & keys

- **Signing:** HMAC-SHA256 over canonical JSON (sorted keys, compact separators) in `core/src/security/signer.py::MessageSigner`. Every spoke/agent frame carries a `signature`; bugfixer reimplements the same scheme locally (`bugfixer/hub_agent.py`).
- **Per-spoke session secrets:** `core/src/security/key_manager.py::KeyManager` stores `keys.json`. `generate_first_secret` creates a 1-hour onboarding secret; `rotate_key` rotates every 30 days and keeps **1 previous** key in `history[spoke_id]` so a frame signed just before a rotation still verifies (`get_valid_key` accepts current + 1 previous; `verify_signature` falls back to history).
- **Hub challenge:** the hub signs its `HUB_VERIFIED` challenge with rotated hub secrets (`hub_secret.json`); the spoke verifies against its `hub_secrets` list. `run_key_rotation_loop` rotates keys due at 30 days.
- **Key-delivery ordering:** the delivery of a new session secret must be signed with the **pre-rotation** secret (the spoke holds the old key until it accepts the new one) — signing with the post-rotation key drops the push before dispatch and permanently desyncs the spoke.

## Onboarding & clone detection

- **PSK self-provisioning:** `LM_ONBOARDING_PSK` (spoke env) + `LM_TENANT_ID_HINT` let a spoke connect unauthenticated, present the PSK, and auto-provision into the hinted tenant (pending → approved) without an admin clicking Approve. Matching a hub PSK also auto-approves.
- **No secret = pending:** a spoke that connects with no secret enters pending-negotiation and shows up in the WebUI `/setup/approve_spoke` queue until approved.
- **Clone-and-rename detection:** `_ensure_install_uuid` (spoke) generates a stable install UUID; the hub's `_rebuild_install_uuid_index` + `_reconcile_spoke_identity`/`_reconcile_agent_identity` carry over approval/tenant/config when a cloned disk presents the same UUID with a new id, and report hostname changes. The `--clone` install flag additionally strips `.env` identity (INSTALL_UUID + HUB_SECRET + session key) so a cloned disk onboards as a **new** spoke, not a clone-and-rename.

## Log relay

- **Spokes:** `_SpokeLogRelayHandler` is attached to the **root** logger (no prefix filter), so every INFO+ record is forwarded to the hub. This is why logging at INFO+ at decision points (TLS mode, discovery, command dispatch) surfaces in the hub logs without extra wiring.
- **pxmx agent:** `WebSocketLogHandler` with a `_RELAY_PREFIXES` filter (`PxmxAgent`, `ProxmoxAgent`, `HubDiscovery`) → `send_log` → `AGENT_LOG` message → spoke `_relay_agent_msg_up` → hub.
- **Runtime log level:** the WebUI "Enable Debug" button broadcasts `SET_LOG_LEVEL`/`SPOKE_SET_LOG_LEVEL` to all spokes + agents (including bugfixer, which is in `active_connections` as `module_type="agent"`). `core/src/logging_setup.py::set_log_level` flips the root logger at runtime; `LOG_LEVEL` is honored at boot.

## Self-update & rollback

- **Hub:** `core/src/update_pipeline.py::UpdatePipelineMixin` orchestrates hub/spoke/agent git updates (`perform_update`, `update_spokes_only`, `update_agents_only`); commit-SHA decision logic in `_update_available`.
- **Rollback ledger:** `core/src/update_recovery.py` — snapshot/restore, pending-update state, bad-version/bad-commit ledgers. CLI subcommands: `snapshot`, `rollback`, `markbad`, `markbadcommit`, `clearpending`, `writefailed`, `prune`. Vendored into the pxmx agent for file-tree restore.
- **Spoke self-update:** `BaseControlPlane` helpers (`_run_git`, `_ensure_git_pull_strategy`, `perform_self_update_check`, `updater_worker`).
- **External watchdog:** `lm/scripts/lm-component-update-restart` (embedded verbatim as a here-doc in `install_pxmx.sh`, `agent/install_agent.sh`, and `cs/lm-spoke/install_cs.sh`) rolls back **only on crash-loop/failed-update**, not on active-no-marker (which is treated as connectivity, not failure). Runs via `systemd-run`.
- **Dep self-heal:** `core/src/dep_guard.py::ensure_requirements` runs at every entrypoint — parses `requirements.txt`, `importlib.util.find_spec`-checks each dep, `pip install -r` for any missing, never raises. Stdlib-only; vendored into the pxmx standalone agent. `LM_DEP_GUARD_DISABLE=1` skips.

## State & tenancy

- **State store:** `core/src/state/manager.py::StateManager` — JSON at `LM_STATE_DIR` (default `/var/lib/lm/state`): `system.json` (modules, global config) + `tenants.json`. Dirty-flag + 60s `persistence_loop`. Spoke/agent registry, tenant mapping, quotas, module metadata.
- **Tenant scoping:** `core/src/access.py` is the server-side isolation gate — `filter_config`/`filter_enabled` (subnet-filter + tenant-filter), `check_tenant_access`, `has_<module>_access`, `effective_tenant`. Every cross-tenant API path goes through it. Tested in `test_subnet_filter.py` / `test_tenant_filter.py`.
- **Encryption:** `core/src/security/encryption.py::HubEncryption` — Fernet at-rest encryption of state blobs, key from `LM_FERNET_KEY` (required, fail-closed); `rotate_fernet_key.py` re-encrypts state to a new key.

## Module-type → spoke → repo map

| module_type | spoke class | repo | agent? |
|---|---|---|---|
| `hypervisor` | `ProxmoxSpoke` | `pxmx` | pxmx per-host agent |
| `simulation` | `CSSpoke` | `cs` | (sim clients, relayed via pxmx agent) |
| `ipam` | `NetboxSpoke` | `netbox` | — |
| `firewall` | `OpnSpoke` | `opnsense` | — |
| `nac` | `CPPMSpoke` | `cppm` | — |
| `directory` | `LdapSpoke` | `ldap` | — |
| `dns` | `DNSSpoke` | `dns` | — |
| `dhcp` | `DHCPSpoke` | `dhcp` | — |
| `nw` | `NwSpoke` | `nw` | — |
| `certificates` | `LESpoke` | `le` | — |
| `agent` | — | `bugfixer` | bugfixer (WS client of hub) |

The agent-spoke role loader (`lm/agent/src/agent_spoke.py::_ROLE_MAP`) maps a `--role` to the spoke class + repo URL so a single generic agent box can morph into any of these on `LOAD_ROLE`.

## The unified agent + roles model (deep dive)

This is the current shape of a managed node end-to-end. It's also the answer to the most common question about the WebUI: *"why do I see so many spokes?"*

**One agent per node, hosting modules as ROLES.** Every managed node runs **one** generic-agent install — the systemd unit `lm-agent`, connecting as `agent-<hostname>` (e.g. `agent-fw01`). That single agent doesn't *become* a firewall or an IPAM; it **hosts** those modules as **roles**. Each loaded role opens its **own** WebSocket connection to the hub — a "sub-spoke" named `{agent}-{role}` (e.g. `agent-fw01-firewall`) that registers with **that module's** `module_type` (`firewall`, `ipam`, …). The base agent connection stays `module_type="agent"` and is the control channel for load/unload.

- The hub **parent-auto-approves** each role sub-spoke: because the parent agent (`agent-fw01`) is already approved, its children (`agent-fw01-firewall`, …) inherit approval via their `parent_spoke_id`. You approve the agent once; its roles come online without a second click.
- A single agent can host **many** roles at once (`agent-fw01-firewall` + `agent-fw01-ipam` + …). Each is an independent hub connection with its own module traffic.

**What a role maps to (`_ROLE_MAP`).** `lm/agent/src/agent_spoke.py::_ROLE_MAP` maps a role name → (spoke class, `module_type`, repo):

| role | module_type | code source |
|---|---|---|
| `dns` | `dns` | in-tree (ships inside the lm clone) |
| `dhcp` | `dhcp` | in-tree |
| `console` | `console` | in-tree |
| `network` | `nw` | `github.com/lbockenstedt/nw` |
| `netbox` | `ipam` | `github.com/lbockenstedt/netbox` |
| `opnsense` | `firewall` | `github.com/lbockenstedt/opnsense` |
| `ldap` | `directory` | `github.com/lbockenstedt/ldap` |
| `simulation` | `simulation` | `github.com/lbockenstedt/cs` |
| `cppm` | `nac` | `github.com/lbockenstedt/cppm` |
| `proxmox` | `hypervisor` | `github.com/lbockenstedt/pxmx` |
| `le` | `certificates` | `github.com/lbockenstedt/le` |

On a `LOAD_ROLE` the agent: clones the sibling repo (or uses the in-tree copy for `dns`/`dhcp`/`console`), `pip`-installs its `requirements.txt`, runs any host prep, then loads the real spoke class and spawns its sub-spoke connection. A role whose spoke class isn't a `BaseSpoke` subclass (today only `cppm`'s `CPPMSpoke`) is transparently wrapped by an internal `_RoleAdapter` so status/command handling stays uniform. Heavy roles (`simulation`, `proxmox`) additionally run their dedicated installer's `--infra-only` step (host prep only: certs, NICs, Kea — no unit, no `.env`) so a freshly-loaded role reaches parity with the standalone install path.

**`install_agent.sh` flags (verified against source).** The generic-agent installer (`lm/agent/install_agent.sh`) accepts:

- `--hub <url>` — the hub WebSocket URL (e.g. `wss://172.16.1.31:443/ws/spoke`). **Optional**: omit it (or pass `auto`) to auto-discover the hub via mDNS/DNS.
- `--id <id>` — the agent's spoke id. Defaults to `agent-<hostname>` when omitted (omitted entirely in `--clone` mode so each clone derives its id from its own hostname at runtime).
- `--secret <psk>` — a pre-shared onboarding secret. Omit it and the agent connects unauthenticated and waits in the WebUI approval queue.
- `--hub-secret <psk>` — the hub root secret (optional).
- `--role <name>` and `--roles <csv>` — **both exist**: `--roles` is the canonical comma-list (`--roles dns,dhcp`), `--role` is a backward-compat single-role alias. They are merged + de-duplicated.
- `--clone` — stage files + enable the unit but leave the service **stopped** for disk-cloning.
- `--loopback` — this agent is co-located with the hub (drives loopback listener modes for the pxmx/cs roles).
- `--tls-verify` (+ optional `--tls-ca-cert <path>`) — verify the hub's TLS cert instead of the default encrypt-without-auth.

> **Note on `--infra-only`:** this flag is **not** an `install_agent.sh` flag. It belongs to the *dedicated* heavy-role installers (`cs/lm-spoke/install_cs.sh`, `pxmx/install_pxmx.sh`), and the agent invokes it internally when loading the `simulation`/`proxmox` roles to do host prep only.

**Boot roles vs. hub-loaded roles.** Passing `--roles` at install time **pre-stages** each role's repo + Python deps + system packages (so a boot-time load has the code locally) and seeds `LOADED_ROLES` in the agent `.env`. The agent re-spawns everything in `LOADED_ROLES` on each boot (and after a self-update restart). A **bare** agent (no `--roles`) installs with zero roles and loads them later on demand from the hub.

**How a role's connection config is delivered.** A role's *connection* settings (NetBox URL + token, OPNsense host + API key, LDAP bind DN, …) are **pushed by the hub** (`UPDATE_CONFIG`, driven from the WebUI Setup page for that module) — **not** read from a per-module `.env`. Configure the module in the WebUI; the hub delivers that config down to the role sub-spoke. (The underlying *application* — e.g. the NetBox Postgres/nginx stack — is still its own separate install; the role only talks to it.)

**Adding / removing a role (WebUI).**
- **Add:** Setup → Agents → pick the agent → **Load Role**. The agent clones/installs and the new role sub-spoke appears within a few seconds, auto-approved.
- **Remove:** **Unload** the role. Its sub-spoke disconnects and is dropped from `LOADED_ROLES` so it is not re-spawned on the next boot.

**Why an agent and its roles are SEPARATE entries in the WebUI.** This is expected, not a bug. The **parent agent** shows under **Agents** (as `module_type="agent"`), and **each role** shows as its **own Spoke** of the matching module_type. So one agent hosting three roles produces **four** entries: the agent plus `…-firewall`, `…-ipam`, `…-dns`. If you're wondering "why do I have more spokes than machines," it's because each role is its own sub-spoke connection.

**Legacy contrast.** The older per-module path — a dedicated `lm-<module>.service` installed by a standalone `install_<module>.sh` (e.g. `install_opnsense.sh`) that connects as its own top-level spoke — still works and is what a fully **standalone** module box uses. The unified agent+roles model above is the current default and is what a hub install (`install_all.sh`) and a menu-installed generic node both use.

See [generic-agent.md](generic-agent.md) for the agent, and each module page for its role specifics.

## Auto-provisioning end-to-end (deep dive)

This explains why Client-Simulation VMs do — or don't — spin up. It is the single most-asked "nothing is happening" question.

**Where the brain lives.** The auto-provisioning logic runs in the **pxmx host agent** (`pxmx/agent/src/usb_provision.py::run_provision_loop`, called every ~60s). The **cs spoke is relay-only** — it forwards config down and telemetry up, but it does **not** decide to clone VMs. So a stuck auto-provision is almost always diagnosed on the pxmx agent, not the cs spoke.

**TWO toggles must BOTH be on.** This is the #1 trap:
1. The **tenant** toggle `usb_auto_provision` (Simulations UI, per tenant), **and**
2. The **per-agent** `client_simulation.enabled` for that Proxmox host.

If either is off, no VMs provision — and the two live in different places, so it's easy to have one on and the other off. The agent surfaces the effective toggle reading in its telemetry so the WebUI Auto-Provisioning card can show whether the tenant toggle actually reached the host.

**One provisioning pass, in order** (`run_provision_loop`):

1. **Reconcile** stale state — release any tracked dongle-bus whose VM no longer exists.
2. **Toggle gate** — `usb_auto_provision` off → telemetry-only pass (no clone/teardown/delete). Reason surfaced as `auto-provision disabled`.
3. **Dongle detection** — scan `/sys/bus/usb/devices` for USB devices whose `vid:pid` is in the hub-delivered **certified** set. No certified vid:pids configured → the pass halts with `no dongle_vidpids configured` (a formerly-silent gate, now loud in the log + card). Certify the dongle vid:pids in the Simulations UI.
4. **Resource thresholds (1h averages)** — CPU/memory 1-hour rolling averages are compared to the provision threshold (default 80%). **Source matters:** these come from **Proxmox node stats** (`/cluster/resources`), **not** `psutil`. `psutil` counts VM RAM + page cache as "used" and reads far higher than Proxmox, which used to fire a false "resource gate." **Cold start:** with less than an hour of samples the average is `None`, which the gate treats as *no data yet → don't block* — so a freshly-booted host provisions freely until a real hour of history accrues (matches the card's "applies only after a full hour" text).
5. **Delete gate** — over the **delete** threshold (default 90%) the agent sheds load by destroying the **newest** USB VM (highest VMID), then enters a **300-second cooldown** so it doesn't churn-delete every tick.
6. **Missing-dongle teardown** — a VM whose passthrough dongle has physically vanished past the `missing_timeout` is destroyed (the bus is freed for re-provisioning).
7. **Per-host VMID-gap audit** — every **300s**, the agent deletes the highest VMID above the lowest gap in its per-host block so the next pass refills the hole. Each Proxmox host owns its **own** VMID batch, derived from the host's trailing hostname number (e.g. `svr-02` → `90025-90048`, stride 24) so ranges never collide across hosts; an explicit `vmid_start`/`vmid_end` override wins.
8. **Clone** — only when resources are under the provision threshold, not in cooldown/ceiling, no clone batch already running, and under `usb_max_slots`: the agent clones a new sim VM from the configured template for each eligible dongle.

The agent reports the last pass's outcome/gate reason (`no dongle_vidpids configured`, `auto-provision disabled`, `no template ids configured`, `resource gate`, `slot cap reached`, `no eligible dongles`, or `provisioning: attempted N, provisioned M`) up through telemetry, so the WebUI Auto-Provisioning card tells you exactly which precondition stopped it.

See [pxmx.md](pxmx.md) for the agent/VM lifecycle and [cs.md](cs.md) for the Simulations UI and toggles.

## Tenants & scoping (deep dive)

**A tenant is an isolation boundary.** It groups a slice of the lab (its subnets, VMs, firewall objects, directory tree) and controls which users can see it. Objects aren't physically separated; the hub **filters** every read so a scoped user only sees their tenant's data.

**Enforcement is server-side, in `core/src/access.py`.** The filtering is applied by the hub on the API path — not just hidden in the UI — so a scoped user cannot reach another tenant's data with a hand-crafted request:

- **`check_tenant_access(sess, tenant_id)`** — may this user touch this tenant at all? Admins and users with no tenant restriction pass; otherwise the tenant must be in the user's allowed `tenants` list.
- **`effective_tenant(...)`** — which tenant a query scopes to, with **non-admin escape prevention**: an admin may pass `?tenant=` to scope to anything (or nothing = see all); a non-admin's `?tenant=` is honored **only** if it's in their allowed list, otherwise it falls back to their own session tenant. A crafted `?tenant=` can never cross the boundary.
- **`filter_config` / `filter_enabled`** — per-module **subnet-filter** toggles. Modules whose data carries tenant IPs (`nac`, `firewall`, `netbox`, `dhcp`, `hypervisor`, `nw`) default **ON**; Simulations (`cs`) is scoped by tenant **id** instead and defaults OFF. Admins can flip each in System → General.
- **Subnet-filter + tenant-filter** — for a scoped user the hub resolves the tenant's **NetBox prefixes** and drops records whose concrete IPs all fall outside them (`filter_session`, `filter_fw`, `filter_nw`, `filter_tenant`). Admins bypass unless they explicitly select a tenant in the switcher; a tenant with no prefixes means "can't filter" → no-op (fail-open on *visibility*, not on access).

**How objects get attributed to a tenant** (per module, since each system tags differently):

- **IPAM / NetBox** — by **prefix containment**: a record's IP is bucketed to the first tenant whose NetBox prefix contains it (`attribute_by_prefix`). The tenant's `netbox_tenant_slug` is the key.
- **Hypervisor / Proxmox** — by **`proxmox_tag`**: a VM tagged for the tenant is shown to it regardless of subnet; VMs in a configured **template pool** are shared to all tenants.
- **Firewall / OPNsense** — by subnet **and** by OPNsense **category** (matched against the tenant's display name, slug, netbox slug, or id).
- **Directory / LDAP** — by `ldap_base_dn`.

**Per-module source-of-truth.** Which side "wins" on a conflict is configurable per module (external-SoT overwrites vs. netbox-SoT add-only), so scoping and sync attribution agree. The scoping config for a tenant (`netbox_tenant_slug`, `proxmox_tag`, `ldap_base_dn`) is read via `get_tenant_scoping`.

## Install model (deep dive)

**Two shapes, one menu.** `install_menu.sh` is the single entry point and offers exactly two choices:

1. **Hub** — this box becomes the LM hub (+ WebUI, always), optionally co-locating spokes. It runs `install_all.sh`.
2. **Generic agent** — a role-capable node that calls home to an existing hub and morphs into roles later. It runs `agent/install_agent.sh`.

**The hub install (`install_all.sh`).** Brings up the hub + WebUI (always), then stands up the co-located modules as **one unified generic agent** (`agent-<hostname>`, installed with `--loopback` because the hub owns `:443` on the same box). The spoke checklist in the menu maps to `--exclude <csv>`: any module you *didn't* pick is excluded, and the remaining ones become that agent's roles. So even the all-in-one hub follows the agent+roles model — it is not ten separate spoke services. `--tls-verify` (+ optional `--tls-ca-cert`) opts into hub-cert verification; without it the co-located clients encrypt-without-auth against the self-signed hub cert.

**The mental model: one agent per node.** A managed node = **one** generic-agent install that hosts whatever roles that node needs. You don't install a separate service per module on a node; you install the agent once and load roles onto it (see the unified-agent deep dive above). The standalone per-module installers still exist for a dedicated single-purpose box, but the default is one agent per node.

See [lm-hub.md](lm-hub.md) and [install-flags.md](install-flags.md) for the full flag reference.

## Update & self-heal (deep dive)

LM is built to pull its own updates and recover from a bad one without leaving anything dark.

**Dependency self-heal (every boot).** `core/src/dep_guard.py::ensure_requirements` runs at the top of every entrypoint (hub, spoke, agent) *before* the heavy imports. It parses `requirements.txt`, checks each package is importable, and `pip install -r`s anything missing — then continues. It is stdlib-only, never raises (a pip failure is logged and the boot proceeds), and is a no-op (~milliseconds) when everything is already present. This is what heals a venv that a skewed update left short a dependency. `LM_DEP_GUARD_DISABLE=1` skips it for operators who manage the venv out-of-band.

**The hub update pipeline** (`core/src/update_pipeline.py`):
- **`perform_update`** — updates the hub itself (git pull for a git install, tarball merge for a non-git install) **and** fans `SPOKE_UPDATE` out to every approved spoke.
- **`update_spokes_only`** — pushes updates to spokes without touching the hub (used by BugFixer after it lands a fix).
- **`update_agents_only`** — same, filtered to `module_type="agent"` nodes.
- **Commit-SHA gate** — since the VERSION string was reset (both ends read the same string), "is there an update?" is decided by **commit SHA**, not version string: local `HEAD` vs. the remote branch tip (git install), or remote tip vs. the last-applied commit (tarball install). VERSION comparison is only a final fallback.

**The external rollback watchdog.** After a hub self-update, the hub schedules `/usr/local/bin/lm-update-restart` from a **transient systemd unit owned by PID 1** (outside the hub's own cgroup, so it survives the hub being stopped). That watchdog restarts the hub, polls `/status`, and — if the new version **fails to boot** — restores the pre-swap snapshot, marks the bad version, and restarts the rolled-back code. Crucially it rolls back **only on a crash-loop / failed-update**, *not* on an "active but no health marker" state (that's treated as connectivity, not failure), so a merely-slow-to-connect hub is never needlessly reverted.

**The recovery ledger** (`core/src/update_recovery.py`) is the single source of truth for the on-disk recovery state machine (under `/var/lib/lm/state`): pre-swap code `snapshot`, `pending_update.json`, and the bad-version / bad-commit ledgers. CLI subcommands include `snapshot`, `rollback`, `markbad`, `markbadcommit`, `clearpending`, `writefailed`, and `prune`. A version marked bad is skipped by the auto-update loop until a newer version ships (or an operator forces a retry). The same ledger is vendored into the pxmx agent for file-tree restore.

**How a user triggers an update.** From the WebUI Setup/Update controls: check-and-update the hub (which also fans out to spokes), or update spokes/agents only. The update is safe by construction — a failed hub boot is rolled back automatically, so triggering an update can't strand the hub.

## Related pages

See the canonical index at `lm/docs/README.md` for per-module pages, `environment-variables.md`, and `install-flags.md`.