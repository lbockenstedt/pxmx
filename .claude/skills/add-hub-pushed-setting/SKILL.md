---
name: add-hub-pushed-setting
description: >-
  Use when adding a setting or knob that is saved on the hub and must reach
  spokes/agents: a fleet-wide value fanned out to every spoke (like the
  hub-contact watchdog, SPOKE_SET_WATCHDOG), a Client-Sim knob delivered by
  CS_CONFIG_UPDATE (sim_weights, stack_cap, t1_exclude_hosts …), or a
  per-instance field carried in the push_config_to_spoke projection
  (verify_ssl on NAC/IPAM instances). Covers the _DEFAULTS/_cfg() read
  shape, the save route, the save-time fan-out AND the reconnect push, the
  spoke-side apply/persist, value normalisation, WebUI, docs and tests. Not
  for secrets (add-vault-secret-field) or a one-shot command
  (add-hub-spoke-command).
---

# Add a hub-pushed setting

A hub-pushed setting reaches a spoke by two paths. The **save-time fan-out**
delivers it to the spokes connected now. The **reconnect push** in
`LabManagerHub.push_config_to_spoke` delivers it to every spoke that
connects later. Most bugs in this class came from a setting that took only
one of the two paths, or from a value whose type changed on the way.
Reference builds: lm `31b2b121` (hub-contact watchdog, fleet-wide) and cs
`ae19c44` (sim-stacking knobs).

## When to use / when not to use

Use it when:
- a Setup/Settings value on the hub must change spoke behaviour;
- a new Client-Sim knob or quota input is added;
- a new non-secret field is added to an instance record (NAC, IPAM …) that
  the spoke reads.

Do not use it for:
- secrets (`add-vault-secret-field`);
- one-off actions (`add-hub-spoke-command`);
- settings that only the hub reads: use the `_DEFAULTS`/`_cfg()` shape
  from `add-webui-control` and stop there.

## Steps (in order)

### 1. Hub: store and read (lm)

- In the route module (`core/src/routes/<feature>.py`), declare a
  module-level `_DEFAULTS` and read through a `_cfg()` that merges the
  defaults with `hub.state.get_global_config().get("<key>")`. Copy the shape
  of `core/src/routes/hub_watchdog.py`. A fresh install then needs no
  migration.
- The save route validates and clamps every field, then calls
  `hub.state.update_global_config({"<key>": clean})`.
- Client-Sim knobs live in `core/src/simulations/routes.py`. Normalise list
  values with `normalize_hub_config_lists` / `_hub_config_list_value` in
  `core/src/simulations/helpers.py`.

### 2. Hub: deliver on save

- **Fleet-wide:** add a `push_<x>_to_all_spokes` method like
  `push_watchdog_to_all_spokes` in `core/src/main.py`. It loops over
  `approved_modules` and calls `push_or_queue_to_spoke(sid, "<CMD>", cfg)`,
  so offline spokes get it from the mailbox.
- **Client-Sim:** go through `_push_config(...)` in
  `core/src/simulations/routes.py` (CS_CONFIG_UPDATE to all of the tenant's
  cs spokes).

### 3. Hub: deliver on reconnect

- **Fleet-wide:** add a block in `push_config_to_spoke` next to the
  `SPOKE_SET_WATCHDOG` block. Put it **before** the module-key early
  return, so agents get it too.
- **Client-Sim:** add the key to the payload built in
  `LabManagerHub.push_cs_hub_config(spoke_id)`.
- **Per-instance fields:** add the field to the `_INSTANCE_CONFIG_SOURCES`
  projection in `main.py` **and** to the matching `_NAC_PAYLOAD` /
  `_IPAM_PAYLOAD` in `core/src/routes/tenant_devices.py`. The two must stay
  identical.

### 4. Spoke: apply and persist

- **lm-hosted control plane:** handle the command in
  `core/src/messaging/control_plane.py`, as for `SPOKE_SET_WATCHDOG`. Copy
  only known keys and persist them locally (`_hcw_save_config` in
  `core/src/messaging/hub_contact_watchdog.py`), so the setting survives a
  restart while the hub is unreachable. The handler must be idempotent.
- **cs:** in `cs/lm-spoke/src/command_handlers/handlers_config.py`, add
  `("<key>", <type or coercer>)` to `ConfigCommandsMixin._HUB_STORE_KEYS`.
  `_apply_hub_config` stores it with `local_store.set_<key>()`, so add that
  setter and its getter in `cs/lm-spoke/src/local_store.py`. If the key
  changes quota placement, also add it to `_RECONCILE_KEYS`.

### 5. WebUI, docs, tests

- **WebUI:** the form field goes on the existing settings page (see
  `add-webui-control`). Prefill it from the GET route; Save confirms the
  write and reports `pushed`/`queued`/`failed`.
- **Docs:** add the setting to the module page (`docs/cs.md`,
  `docs/generic-agent.md`, …) and, for env overrides,
  `docs/environment-variables.md`.
- **Tests:** a hub test that saves the setting and then asserts both
  delivery paths, plus a spoke test for apply/persist (step 4).

## Tests

```bash
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_hub_contact_watchdog.py tests/test_sim_quota_knob_push.py tests/test_cppm_reconnect_verify_ssl.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_push_config_multi_spoke.py tests/test_push_config_instance_no_cross_spoke_leak.py
cd cs && /tmp/nbvenv/bin/python -m pytest -q lm-spoke/tests      # if the cs side changed
```

## Common misses

- **Reconnect push drops the field.** `verify_ssl` was in the save-time
  payload (`_NAC_PAYLOAD`) but not in the `_INSTANCE_CONFIG_SOURCES`
  projection, so every spoke restart re-enabled TLS verification — lm
  `e72faa04` (+ `tests/test_cppm_reconnect_verify_ssl.py`).
- **A blank value changes type.** An empty `t1_exclude_hosts` was parsed as
  a vid:pid list and every hostname was dropped. It had to be fixed on both
  sides: lm `3579e815` (`_hub_config_list_value` in
  `simulations/helpers.py`) and cs `13b3e77` (`local_store.py`). Put every
  list key on the right branch of the normaliser on both hub and spoke.
- **Re-saving a config file drops keys.** A config re-save removed
  `dns_fail_rate`/`dns_fail_duration` from `simulation.conf` — cs `a603bf2`.
  Write by merge, never by replacing the whole dict.
- **Delivered only to the spokes that are online.** Use
  `push_or_queue_to_spoke` (mailbox), not a bare `request_response`, for
  the save-time fan-out.

## Done when

- [ ] `_DEFAULTS` + `_cfg()` read; save route validates and calls
      `update_global_config`
- [ ] save-time fan-out (queued for offline spokes)
- [ ] reconnect push in `push_config_to_spoke` / `push_cs_hub_config`;
      `_INSTANCE_CONFIG_SOURCES` mirrors `tenant_devices.py`
- [ ] spoke applies only known keys, persists locally, is idempotent; cs
      `_HUB_STORE_KEYS` + `local_store` setter
- [ ] blank / missing values keep their type on both sides
- [ ] WebUI field, docs, tests above pass

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **Multitenancy**: Hub settings are defaults; Everything is
    tenant-scoped.
  - **Architecture**: Spokes are autonomous; Wire changes stay
    backward-compatible.
  - **Data and storage**: Sync is idempotent.
  - **Code quality**: Twins stay in step, for hub/cs copies; see the
    `dual-copy-guard` skill.
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
