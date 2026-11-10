---
name: add-spoke-module
description: >-
  Use when adding a new spoke module / agent role to the fleet (the way
  TrueNAS "storage" and Zabbix "monitoring" were added), or when an existing
  spoke repo starts being hosted as a role by the generic agent. Covers the
  spoke repo (BaseSpoke subclass, handle_command, start_background_loops,
  _mask, control_plane.py, installer, API_SPEC), and every lm hub map that
  must learn the new module type: agent _ROLE_MAP, hub_spoke_registry,
  main.py push-config maps, update_pipeline maps, the hub cache mixin and
  its shutdown flush, routes registration, vault fields, WebUI device type,
  docs and the tests that pin those maps. Not for one new command on an
  existing module (add-hub-spoke-command) or a new WebUI tab alone
  (add-webui-view).
---

# Add a spoke module (new module type / agent role)

A module type has to be registered in roughly ten places in lm, and each
one that is missed fails silently: the spoke connects but never updates,
never gets its config, or loses cached data on shutdown. TrueNAS was built
in lm `14f86224` (hub routes, cache, registry) and `9dcf7b12` (agent role,
WebUI, NetBox sync), then needed eight follow-up fixes across three repos.
Use the `storage`/`truenas` entries as the reference in every map below.

## When to use / when not to use

Use it when:
- a new repo/product joins the fleet as a spoke (new `module_type`);
- an existing spoke gains role-hosted mode under `lm-agent`;
- a hub cache is added for a module's polled data.

Do not use it for:
- one more command on an existing module (`add-hub-spoke-command`);
- only a new secret on an existing module (`add-vault-secret-field`);
- only a new WebUI tab (`add-webui-view`).

## Steps (in order)

### A. Spoke repo (`<repo>/`)

1. `src/<x>_spoke.py`: `class <X>Spoke(BaseSpoke)`. Import `BaseSpoke` with
   both paths, as `truenas/src/truenas_spoke.py` does: `core.src.base_spoke`
   when hosted by the agent, `base_spoke` when standalone. Implement:
   - `handle_command(self, command_type, data)` and `get_status()`;
   - a static `_mask(data)` that hides secret keys, recursing into lists;
   - **`start_background_loops()`** for every poll, sweep or flush loop.
     `agent/src/control_plane.py` `_start_role_local_services` calls it
     (and `start_client_api_server`) when the agent hosts the role, and
     `control_plane.py` is NOT used in that mode.
2. `src/control_plane.py` (standalone mode). It calls the same
   `start_background_loops()`; do not copy the loop code into it.
3. Make every payload sent to the hub JSON-safe: convert datetimes and
   bytes first, and log the real result of the send.
4. Vendor client: check status and body; reconnect after an idle drop.
5. `install_<x>.sh`, `README.md`, `API_SPEC.md` / `docs/<x>.md`, `tests/`.
   CI must pass on its own; the pipeline files come from the fleet (see
   `change-fleet-pipeline`).

### B. lm hub — register the module type

6. `agent/src/agent_spoke.py` `_ROLE_MAP`: add
   `"<role>": ("<repo>/src/<x>_spoke.py", "<X>Spoke", "<module_type>",
   "https://github.com/lbockenstedt/<repo>.git")`. Pass `None` as
   `repo_url` only for code that lives in lm.
7. `core/src/update_pipeline.py`: add the module to
   `_UPDATE_SOURCE_MODULE_KEY` (`"storage": "truenas"`) and to
   `_MODULE_REPO_DIR`, with both the type and the repo-name aliases.
   Without this the spoke never receives SPOKE_UPDATE.
8. `install_all.sh`: add the repo to the `DEFAULTS` update-sources dict so a
   fresh install has an update URL (lm `411e3670` did this for `le`).
9. `core/src/hub_spoke_registry.py`:
   - add the entry to `_MODULE_TYPE_PREFIX` if spoke ids carry a prefix;
   - add a tenant-aware resolver modelled on
     `get_truenas_spoke_for_tenant` / `get_truenas_spoke_for_shared`.
10. `core/src/main.py`:
    - add the entry to `_PUSH_CONFIG_MODULE_KEY` and `_PUSH_CONFIG_PREFIX_MAP`;
    - add the per-module branch in `push_config_to_spoke`, plus
      `_INSTANCE_CONFIG_SOURCES` if instances are configured on the hub;
    - match records to their spoke with `self._primary_key(spoke_id)`, not
      the raw id;
    - add any spoke→hub messages to `_SPOKE_MESSAGE_HANDLERS`.
11. **Hub cache** (if the hub serves polled data):
    - add `core/src/<x>_cache.py` with a `<X>CacheMixin` built on
      `cache_core.JsonCacheFile` + `StalenessPolicy`, as in
      `truenas_cache.py`;
    - mix it into `LabManagerHub` and call `self.<x>_cache_init()` and
      `self.<x>_cache_load()` in `__init__`;
    - add `("<x>-cache", hub.<x>_cache_flush_now)` to `persist_on_shutdown`
      in `core/src/api.py`.

    Routes serve from this cache, never with a live call per page load
    (`docs/caching-contract.md`).
12. `core/src/routes/<x>.py` with `register(app, hub, ctx)`: import it and
    call `<x>.register(app, hub, ctx)` in the route block of `api.py`. Write
    each route with `add-hub-spoke-command`.
13. Secrets: run `add-vault-secret-field` (`SECRET_FIELDS`, `VAULT_REQUIRED`).
14. WebUI: a `DEVICE_TYPES` / `INSTANCE_PRODUCTS` entry in
    `WebUI/js/module-management.js`, then the page via `add-webui-view`.
15. Docs: the role list in `docs/generic-agent.md`, the module page, and
    `docs/caching-contract.md` if you added a cache.

## Tests

```bash
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_update_module_type_resolution.py tests/test_cache_modules_shared.py tests/test_push_config_multi_spoke.py tests/test_push_config_instance_no_cross_spoke_leak.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_<x>_cache.py          # new, modelled on tests/test_truenas_cache.py
cd lm/agent && /tmp/nbvenv/bin/python -m pytest -q tests                           # _ROLE_MAP change; CI runs one pytest per component
cd <repo> && /tmp/nbvenv/bin/python -m pytest -q tests
```
`test_update_module_type_resolution.py` checks that every `_ROLE_MAP` role
that has a repo resolves to that repo.

## Common misses

- **Update fan-out never mapped the type.** The zabbix spoke stayed on
  Nov 1 code ("unknown module type"). Fixed in lm `3d9027d6`:
  `storage→truenas`, `monitoring→zabbix` in `update_pipeline.py`, plus the
  test above.
- **Cache flush never called on shutdown**, so coalesced writes were lost —
  lm `6ae0a8f0` (`persist_on_shutdown`, `test_cache_modules_shared.py`).
- **Live call per page view** instead of serving from the poll cache —
  lm `afa737a9` moved Storage to the background-poll cache.
- **Raw spoke id instead of primary key.** A guid-bound appliance never
  reached its spoke — lm `6f5c7701`.
- **Loops only started in standalone mode.** Role-hosted spokes never polled
  — truenas `b647b0d`, nw `95b1177`, netbox `7322af4` (each moved the loops
  into the spoke class / `start_background_loops`).
- **Payload not JSON-safe.** Datetimes broke the poll push — truenas
  `b6eed26`.
- **No reconnect after idle drop** — truenas `732535e`.
- **Vendor errors read as success.** OPNsense 4xx/5xx and `result=failed`
  — opnsense `e9fc04f`, `76f463a`.
- **Secret stored inline at first** — lm `7d436d03` moved TrueNAS API keys
  into the vault.

## Done when

- [ ] spoke: `BaseSpoke` subclass, `_mask`, `start_background_loops`,
      standalone `control_plane.py` that reuses it, JSON-safe pushes,
      reconnect, tests
- [ ] lm: `_ROLE_MAP`, `update_pipeline` maps, `install_all.sh` `DEFAULTS`,
      registry resolver, `main.py` push maps / handlers, cache mixin +
      shutdown flush, routes registered
- [ ] vault fields, WebUI device type and view done (sibling skills)
- [ ] tests above pass in lm and the spoke repo; docs updated
- [ ] one commit per repo (spoke first)

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **Architecture**: The spoke does the heavy lifting; Spokes are
    autonomous; Generic, not vendor-specific; Nothing hard-coded.
  - **Data and storage**: JSON only, no database; Caching contract; Vendor
    responses are untrusted.
  - **Security and secrets**: Credentials go in the vault.
  - **Multitenancy**: Everything is tenant-scoped.
  - **Code quality**: Don't block the event loop.
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
