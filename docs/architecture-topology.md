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
   pxmx host agents  (wss to hub /ws/agent → pxmx spoke loopback :8443, or :443 standalone)
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
| pxmx agent → hub `/ws/agent` | `wss://127.0.0.1:443/ws/agent` (loopback) | `wss://<hub>:443/ws/agent` (hub byte-proxies to pxmx spoke loopback :8443) |
| leaf agent → hub/gateway | `wss://127.0.0.1:443/ws/spoke` (loopback, verify-off) | `wss://<hub>:443/ws/spoke` |

- **Hub unified server:** one uvicorn process on `0.0.0.0:LM_TLS_PORT` (default **443**), `wss` when `LM_TLS_CERT`/`LM_TLS_KEY` are set, plaintext otherwise. WebSocket routes: `/ws/spoke` (spoke + agent control), `/ws/console/{session_id}` (browser ↔ Proxmox VNC byte relay), `/sim/ws` (cs telemetry). HTTP routes: `/api/*`, `/setup/*`, `/auth/*`, `/admin/*`, `/sim/api/*`, plus the mounted WebUI.
- **Co-located (loopback):** same-box spokes/agents dial `wss://127.0.0.1:443/ws/spoke` (or `/ws/agent`) with TLS verify OFF — the single :443 listener serves loopback too, so same-box traffic isn't a separate port. (The pxmx spoke's own agent listener stays loopback `127.0.0.1:8443`, reached via the hub `/ws/agent` byte-proxy — not advertised externally.)
- **pxmx agent listener:** `LM_PXMX_AGENT_PORT` — default **8443** loopback all-in-one (`LM_PXMX_AGENT_LOOPBACK=1`; the hub `/ws/agent` byte-proxy dials it); standalone pxmx spoke uses **443** wss. Legacy no-cert port **8766**. mDNS TXT `agent_port` advertises the **external** dial port (**443** on both deployments) so agents auto-discover `wss://<hub>:443/ws/agent`; the loopback 8443 is NOT advertised. `/api/pxmx/agent-install-cmd` returns the `wss://<host>:443/ws/agent` install string.
- **No-cert fallback:** if `LM_TLS_CERT`/`LM_TLS_KEY` are unset, the hub serves a single `0.0.0.0:443` plaintext listener and discovery returns `ws://<host>:443/ws/spoke` — backward compatible.

## Discovery (mDNS + DNS)

`hub_discovery.py::discover_hub_url(timeout, agent_listener=False)` resolves where a spoke/agent should dial.

- **mDNS:** the hub broadcasts `_lm-hub._tcp.local.` (`lm-hub._lm-hub._tcp.local.`) via `zeroconf` (optional dep; skipped gracefully). TXT records: `version`, `agent_port` (always), and `tls_port` **only when** a cert is configured or `LM_HUB_ADVERTISE_TLS=1` (lets a reverse-proxy/TLS-terminator shape advertise TLS without owning the cert).
- **DNS:** tries `lm-hub.<search-domain>`, `lm-hub.local` (Avahi), bare `lm-hub`. DNS has no TXT → always returns `ws://` (pin `--hub wss://...` for a TLS remote hub reached only by DNS).
- **Scheme selection:** same-box → `wss://127.0.0.1:443/ws/spoke` (verify-off, loopback on the single :443 listener); remote + mDNS TXT `tls_port` → `wss://<ip>:443`; remote no TXT → `ws://<ip>:443` (no-cert hub). `agent_listener=True` reads TXT `agent_port` (advertised **443** on both deployments) → `wss://<hub>:443/ws/agent`.
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

## Related pages

See the canonical index at `lm/docs/README.md` for per-module pages, `environment-variables.md`, and `install-flags.md`.