---
name: spoke-integration-engineer
description: Use when adding or extending a vendor/appliance spoke (truenas, opnsense, cppm/ClearPass, zabbix, ldap, le, kvm, nw, pxmx) together with its hub routes, cache, update wiring and WebUI, or when adding a hub<->spoke command. Covers the hub wiring points that were each missed at least once.
metadata:
  ab-match: "new spoke|spoke (module|type)|truenas|opnsense|cppm|clearpass|zabbix|\\bldap\\b|\\bkvm\\b|appliance|vendor api|hub.?spoke command"
---

# Spoke integration engineer

A spoke wraps one external system and does the heavy lifting; the hub relays,
caches and shows it. Spoke work produced about 320 commits, and truenas
alone had 15 fix-ups in two days. The recurring misses are wiring, not logic.

Recipes:
- **`add-spoke-module`** for a new spoke type;
- **`add-hub-spoke-command`** for a new command;
- **`add-hub-pushed-setting`** for a new hub-edited setting;
- **`add-vault-secret-field`** for each credential.

## Hub wiring points (each was missed once)

1. `main.py`: `_PUSH_CONFIG_MODULE_KEY`, `_PUSH_CONFIG_PREFIX_MAP`,
   `_SPOKE_MESSAGE_HANDLERS`, and the cache init/load.
2. `core/src/<mod>_cache.py`, built on `cache_core` (`JsonCacheFile` or
   `TenantShardCache`), plus its flush at shutdown in `api.py`
   (lm 6ae0a8f0).
3. Route `register()` in `api.py`, and `get_<mod>_spoke_for_tenant` in
   `hub_spoke_registry.py`.
4. Both module maps in `update_pipeline.py`, or the update fan-out skips the
   module (lm 3d9027d6).

## Spoke checklist

- Build on `BaseSpoke`. Start loops in `start_background_loops()`, because
  agent `control_plane.py` calls it when a role hosts the spoke
  (truenas b647b0d, nw 95b1177, netbox 7322af4).
- **Vendor responses are untrusted.** Check the HTTP status and the body
  (`result: failed` in a 200: opnsense e9fc04f, 76f463a). Reconnect when a
  session expires (truenas 732535e). Make pushed payloads JSON-safe
  (truenas b6eed26).
- Unknown commands return an ERROR envelope, not a traceback. Keep "no data"
  distinct from "couldn't ask" (nw c4c32e9).
- Secrets come from the vault from day one and are masked in logs,
  including UPDATE_CONFIG (truenas 0c60732).
- Keep the change generic: the whole device class, not one model. Nothing
  hard-coded.
- Pages render from the hub cache; the spoke refreshes in the background.

## Tests and docs

- Spoke: `python -m pytest -q tests`.
- Hub: `cd core && python -m pytest -q tests/test_cache_modules_shared.py tests/test_update_module_type_resolution.py`,
  plus the route test.
- Update `lm/docs/<module>.md` (canonical) and the spoke's `docs/`.

## Reference

`lm/MODULE_REQUIREMENTS.md`, `lm/docs/architecture-spoke-heavy-lifting.md`,
`lm/docs/hub-direct-ops.md`, `lm/docs/caching-contract.md`, and
`.github/fleet-rules.md`.
