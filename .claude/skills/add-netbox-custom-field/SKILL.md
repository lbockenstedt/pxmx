---
name: add-netbox-custom-field
description: >-
  Use when a NetBox custom field must be added or newly read/written by the
  fleet (e.g. dhcp_enabled / exclusion_ranges on ipam.prefix, nac_role on
  ipam.ipaddress, product/version on dcim.device): adding the row to
  netbox/src/custom_fields_spec.py CUSTOM_FIELDS_SPEC so install.sh and the
  spoke's _ensure_custom_fields self-heal provision it, and making the
  value round-trip through netbox_ipam.py list/create/update, the
  netbox_spoke.py dispatch, and the lm hub consumers (dns_dhcp_sync,
  endpoint_sync, WebUI/js/netbox.js). Not for NetBox sync/dedupe logic
  that uses no new field.
---

# Add a NetBox custom field (provision + round-trip)

NetBox drops a value whose custom field does not exist yet, and gives no
error. The netbox spoke also writes only whitelisted names. A new field
therefore needs two things: provisioning (the spec row) and a full
read/create/edit round trip. The DHCP-scope checkbox shipped with the spec
row only (netbox `2a7a75d`) and never worked until netbox `4eecff1`, which
fixed list, create and edit.

## When to use / when not to use

Use it when:
- the hub or a feeder (TrueNAS, nw, firewall discovery …) needs to store a
  new attribute on a NetBox object;
- a WebUI IPAM modal gains a field that is stored as a custom field;
- a hub sync (DHCP scopes, endpoint/NAC sync) starts reading a custom field.

Do not use it for:
- built-in NetBox fields (status, tenant, dns_name …);
- reference objects (roles, tags) — those are `REFERENCE_OBJECTS_SPEC` in
  the same file, with a different shape.

## Steps (in order)

### A. netbox repo (one commit)

1. `src/custom_fields_spec.py`: add `(name, type, label, content_type)` to
   `CUSTOM_FIELDS_SPEC`. Group it with its feature's comment block, and add
   one row per content type if the field goes on several (e.g.
   `mac_address`). The types in use are `text`, `integer` and `boolean`.
   - Fresh installs: `install.sh` imports this spec.
   - Existing installs: the self-heal
     `netbox_vmsync.py::_ensure_custom_fields(force=False)` provisions it on
     the next UPDATE_CONFIG / reconnect.
   - No other provisioning list exists; do not add one.
2. **Device discovery fields** (`dcim.device`): that is all — the provisioned
   name is then allowed by `netbox_sync.py::_device_cf_whitelist()` for the
   per-record `custom_fields` map on NETBOX_SYNC_DEVICES.
3. **IPAM fields** (prefix / IP), in `src/netbox_ipam.py`:
   - the read path (`get_prefixes` / `get_ip_addresses`) must return
     `custom_fields` on every row;
   - the create path (`allocate_prefix` / the IP create) must accept
     `custom_fields` and write it into the NetBox payload;
   - the update path (`update_prefix` / `update_ip_address`) must accept
     `custom_fields` and **merge** it onto the object's existing
     `custom_fields`. Never replace the dict: a partial edit must not blank
     unrelated fields.
4. `src/netbox_spoke.py`: each matching NETBOX_* dispatch must forward
   `custom_fields=data.get("custom_fields")` (as for NETBOX_ALLOCATE_PREFIX /
   NETBOX_UPDATE_PREFIX).
5. Tests:
   - extend `tests/test_custom_fields_spec.py` (row present, valid type and
     content type);
   - add round-trip tests in the style of
     `tests/test_prefix_dhcp_custom_fields.py`: read forwards, create
     writes, update merges.
6. Docs: `docs/netbox.md` (field list) and `API_SPEC.md` if a command
   payload changed.

### B. lm hub (separate commit, after A)

7. Consumers:
   - DHCP scope fields are read by `core/src/dns_dhcp_sync.py::build_dhcp_payload`
     off the prefix list;
   - endpoint / NAC fields go through `core/src/endpoint_sync.py` (lm
     `7c616e1f` added `nac_role`).

   Read with a default, because older netbox spokes do not send the key.
8. Route payloads: the hub route that proxies the IPAM create/edit must
   pass `custom_fields` through unchanged, including in the tenant-scoped
   variant.
9. WebUI: `WebUI/js/netbox.js`, the IPAM create/edit modals. Prefill from
   `editItem.custom_fields.<name>` and send only the changed keys in
   `custom_fields`. Use `add-webui-control` for the markup.
10. Docs: `docs/netbox.md`, plus the consuming module's page (e.g.
    `docs/cppm.md`).

## Tests

```bash
cd netbox && /tmp/nbvenv/bin/python -m pytest -q tests/test_custom_fields_spec.py tests/test_prefix_dhcp_custom_fields.py tests/test_ip_mac_custom_fields.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_endpoint_sync_flow.py      # if endpoint_sync changed
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_cppm_tenant_routing.py      # if a cppm/IPAM route changed
```
Before you finish, check every list/create/update function you touched by
hand: does `custom_fields` go in and come out?

## Common misses

- **Spec row only, no round trip.** `dhcp_enabled` was provisioned (netbox
  `2a7a75d`), but `get_prefixes` never returned `custom_fields`, and
  `allocate_prefix` / `update_prefix` never accepted it. The checkbox was a
  no-op and no prefix could become a Kea scope — netbox `4eecff1`
  (cherry-picked to every branch as `bc7a67f`, `51856c5`).
- **Update replaces instead of merging.** Writing the dict straight back
  blanks every other custom field on the object. `4eecff1` merges onto the
  existing value.
- **Spoke dispatch not forwarding.** `netbox_ipam` accepted the field but
  `netbox_spoke.py` never passed `data.get("custom_fields")` (fixed in the
  same `4eecff1`).
- **Blocking provisioning on the shared loop.** `_ensure_custom_fields`
  does synchronous pynetbox I/O; `netbox_spoke.py` runs it with `_run_sync`
  in the executor. Keep it there (fleet rule "Don't block the event loop").

## Done when

- [ ] spec row(s) in `CUSTOM_FIELDS_SPEC`; `test_custom_fields_spec.py`
      updated
- [ ] list returns, create writes, update merges `custom_fields`;
      `netbox_spoke.py` forwards it; round-trip test added
- [ ] lm consumer reads the value with a default; route passes it through;
      WebUI prefill + send
- [ ] docs updated in netbox and lm; one commit per repo, netbox first

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **Data and storage**: Sync is idempotent; Vendor responses are
    untrusted.
  - **Architecture**: Wire changes stay backward-compatible.
  - **Multitenancy**: Everything is tenant-scoped.
  - **Code quality**: Don't block the event loop.
  - **Git, branches and promotion**: One repo per change.
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
