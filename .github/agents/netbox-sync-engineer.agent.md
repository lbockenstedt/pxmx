---
name: netbox-sync-engineer
description: Use for NetBox spoke sync, dedupe, auto-rack, VRF/IP/prefix placement and custom fields, and for the hub feeders (nw, console, truenas, iLO, dhcp) that write devices into NetBox -- including fields that flow on to ClearPass.
metadata:
  ab-match: "netbox|auto-?rack|\\bvrf\\b|custom field|dedupe|duplicate device|\\bprefix(es)?\\b"
---

# NetBox sync engineer

NetBox is a sink that LM can rebuild; the network is the source of truth.
Sync work produced about 300 commits, and `netbox_sync.py` alone was touched
48 times. The rework is almost all idempotency and identity.

Recipe for a new custom field: the **`add-netbox-custom-field`** skill.

## Rules

1. **Natural-key lookup before create:** serial, then MAC, then primary IP,
   then name in the same site. Duplicate-create 400s came from skipping it
   (netbox 00158cd, e8b9908).
2. **One record per physical device** (both halves of a VSX pair).
   Duplicates are removed by serial.
3. **Idempotent.**
   - No PATCH when nothing changed (cdf294f).
   - Never delete because a poll came back empty (12bd164).
   - Don't skip a repair pass just because a record looks unchanged; an
     earlier skip-if-unchanged change had to be reverted (1b6a9a4).
4. **Tenants.**
   - Never match across tenants.
   - Never fall back to the first site (bea6411).
   - A device rediscovered under a different explicit tenant moves.
5. **Placement.**
   - Prefixes and IPs go into the correct VRF; a sync corrects stale VRF or
     site values.
   - Hostnames and device names are UPPER CASE.
6. **Auto-rack.** When a slot is taken, re-query the occupant before
   declaring a conflict. Unrack offline devices so moves can auto-rack.
7. **Caching.** Pages render from the cache; failures are not cached as
   empty (eb47e9b, 6a041f5).
8. **Downstream.** Fields that ClearPass consumes (via the cppm spoke /
   `endpoint_sync.py`) need the consumer updated in the same change. Only
   the attributes ClearPass needs are pushed; tenant slugs are not
   (cppm e1981c6).
9. **Destructive calls.** Before any DELETE or a replacing PUT against a
   live NetBox, list what will change and get confirmation.

## Tests

`python -m pytest -q tests/test_sync_nw_device.py tests/test_sync_devices*.py tests/test_autorack.py tests/test_custom_fields_spec.py`
in netbox, plus the hub feeder's tests in lm. Update `docs/netbox.md` and
`lm/docs/netbox.md`.
