---
name: add-hub-spoke-command
description: >-
  Use when adding or changing a hub→spoke command: a new <MODULE>_<VERB>
  command type handled in a spoke's handle_command (pxmx, opnsense, ldap, nw,
  netbox, truenas, cppm, le, zabbix, cs, or the in-lm dns/dhcp/henet roles)
  plus the lm hub route in core/src/routes/<module>.py that sends it with
  hub.request_response and returns the result to the WebUI. Also for
  turning N per-item relays into one bulk command, or for a spoke→hub
  message added to _SPOKE_MESSAGE_HANDLERS. Covers tenant-aware spoke
  resolution, error envelopes (no false "success"), slow-command keepalives,
  backward compatibility with older spokes, API_SPEC/docs and tests on both
  sides. Not for a brand-new module (use add-spoke-module) or a config value
  pushed to every spoke (use add-hub-pushed-setting).
---

# Add a hub → spoke command

Every spoke feature is a command pair: the spoke does the work in
`handle_command`, and a thin hub route relays to it. This change happens
constantly (pxmx `433656a` + lm `d659f3ee` bulk VM actions; ldap `fee1f84`,
`492daa5` + lm `b55784ca` Directory contract; opnsense `3f833c5` overview).
Its misses are always the same few: the wrong spoke picked, an error that
reads as success, or a spoke that never received the new handler.

## When to use / when not to use

Use it when:
- a WebUI action or hub job needs a new thing done on a spoke;
- an existing command gains a field or changes its reply shape;
- N relays should become one bulk command;
- a spoke starts sending a new unsolicited message to the hub.

Do not use it for:
- a new spoke module or agent role (`add-spoke-module`);
- a setting pushed to every spoke on connect (`add-hub-pushed-setting`);
- NetBox custom fields (`add-netbox-custom-field`).

## Steps (in order)

Make one commit per repo, spoke first: the hub must cope with a spoke that
does not have the command yet (fleet-rules.md → Architecture › Wire changes
stay backward-compatible).

### Spoke repo

1. **Engine/manager method** that does the vendor call, e.g.
   `opnsense/src/opnsense_engine.py` `get_overview()` or
   `ldap/src/ldap_manager.py`.
   - Treat vendor replies as untrusted: check the HTTP status **and** the
     body (`result: failed`, `validations`). An empty answer and a failed
     answer are different things.
   - Return `{"status": "SUCCESS", "data": …}` or an `ERROR` envelope with a
     message.
2. **Dispatch branch** in the spoke's `handle_command(self, command_type,
   data)`. Follow the file's own style:
   - `elif normalized_cmd == "OPNSENSE_GET_OVERVIEW":` in
     `opnsense/src/opn_spoke.py`;
   - `if cmd == "PXMX_VM_ACTION_BULK":` in `pxmx/src/proxmox_spoke.py`;
   - `normalized_cmd == "LDAP_DELETE_GROUP"` in `ldap/src/ldap_spoke.py`.

   pxmx agent-side work also needs a branch in `pxmx/agent/src/agent.py`
   (`elif cmd_type == "PXMX_VM_ACTION_BULK":`).
3. **Contract doc** — add the command (purpose, payload, response) to the
   repo's `API_SPEC.md` or its README command table.
4. **Spoke test** in `<repo>/tests/` with a fake client: success, vendor
   error, and empty result (opnsense `tests/test_overview.py`, ldap
   `tests/test_ldap_manager_calls.py`).

### lm hub

5. **Slow command?** If it can run longer than the base timeout, add it to
   `_KEEPALIVE_CMDS` in `core/src/messaging/control_plane.py` so the spoke
   sends progress keepalives.
6. **Route** in `core/src/routes/<module>.py`, inside `register(app, hub, ctx)`:
   - authorise first (`access.read_scope` / `access.write_scope`, or the
     module's `_authz_*` helper such as `_authz_firewall` in
     `routes/firewall.py`);
   - **resolve the spoke tenant-aware.** Use the module's resolver
     (`hub.get_directory_spoke_for_tenant(tid)`,
     `hub.get_truenas_spoke_for_tenant(tid)`) or
     `access.scoped_spoke_of_type(hub, tid, module_type)`. Use
     `get_spoke_or_503` / `require_spoke` only for genuinely single-instance
     modules. AB's `check_first_pick` warns on a bare `get_spoke_by_type`;
   - call `await hub.request_response(spoke_id, "<CMD>", payload,
     timeout=…)`, then `access.unwrap_spoke(result)`;
   - turn a spoke `ERROR` envelope into an HTTP error (the `_relay` helper in
     `routes/ldap.py` raises 502), never a 200 that the WebUI reads as
     success;
   - an old spoke answers "Unknown command": return a clear "update the
     spoke" error, or fall back to the old command;
   - reads that the UI polls go through the hub cache: `hub.cache_read(ns,
     key, fetch, …)` with a `warm_cache.CacheJob`, then
     `hub.cache_annotate(...)` (see `_relay_list` in `routes/ldap.py`).
7. **Fire-and-forget** commands that need no reply use
   `hub.send_to_spoke_command(spoke_id, command_type, data)`.
8. **Spoke → hub messages** — add `"<MSG_TYPE>": "_spoke_msg_<name>"` to
   `_SPOKE_MESSAGE_HANDLERS` in `core/src/main.py` and implement
   `async def _spoke_msg_<name>(self, conn, msg_data, payload, dec_secret)`
   (see `_spoke_msg_truenas_poll_result`).
9. **WebUI** — call the route from the module's view. For a new button use
   `add-webui-control`; for a new tab use `add-webui-view`.
10. **Docs** — add the command to the module's `lm/docs/<module>.md` (for
    example the `handle_command` list in `docs/pxmx.md`).
11. **Hub test** in `core/tests/`. Assert that the route picks the
    tenant's spoke and maps an `ERROR` envelope to an HTTP error (pattern:
    `tests/test_pxmx_vm_action_spoke_routing.py`,
    `tests/test_ldap_routes_reply_frame.py`).

## Tests

```bash
cd opnsense && /tmp/nbvenv/bin/python -m pytest -q tests/<test_file>.py      # the spoke repo you touched
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_pxmx_vm_action_spoke_routing.py tests/test_ldap_routes_reply_frame.py tests/<your_new_test>.py
python ../ab/fleet_checks.py --base origin/dev --repo lbockenstedt/<repo>    # run from each touched repo
```

## Common misses

- **The spoke never got the handler.** LE_SET_DNS_CRED answered "Unknown
  command" because the le spoke was never sent SPOKE_UPDATE
  (`update_sources.le` was unset) — lm `411e3670`. Check that the module
  type is in `_UPDATE_SOURCE_MODULE_KEY` / `_MODULE_REPO_DIR`
  (`core/src/update_pipeline.py`) and handle "Unknown command" in the route.
- **The fix went into a copy that isn't run.** DNS forwarder edit/delete was
  missing from the resolver code that actually runs — lm `b6c82e0a`. dns
  and dhcp now live only in `lm/dns`/`lm/dhcp` (their repos are archived);
  confirm the path you edit is the live one.
- **A spoke error reads as success.** The LDAP routes returned the ERROR
  envelope with a 200, so the WebUI showed "created" — lm `f3bf4377` (now
  the 502 in `_relay`).
- **"Nothing there" vs "couldn't ask".** nw LLDP treated a failed query as
  zero neighbours — nw `c4c32e9`. OPNsense 4xx/5xx and `result=failed` were
  read as success — opnsense `e9fc04f`, `76f463a`.
- **Wrong library call shapes.** python-ldap adds and group membership were
  called with the wrong argument shapes — ldap `f6fe064` added
  `tests/test_ldap_manager_calls.py` to pin them.
- **First spoke wins.** A relay that used the first connected spoke sent one
  tenant's operation to another tenant's spoke; the tenant-aware resolver
  in `_relay`'s docstring (`routes/ldap.py`) exists for this reason.

## Done when

- [ ] spoke: engine method + `handle_command` branch + `API_SPEC.md` + test
      (success, error, empty)
- [ ] hub: authorised, tenant-aware route; ERROR → HTTP error;
      "Unknown command" handled
- [ ] slow command listed in `_KEEPALIVE_CMDS`; polled reads go through the
      cache
- [ ] module doc updated; hub test passes; `fleet_checks.py` clean in each
      repo
- [ ] one commit per repo, spoke first

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **Architecture**: The spoke does the heavy lifting; Wire changes stay
    backward-compatible.
  - **Data and storage**: Vendor responses are untrusted.
  - **Multitenancy**: Everything is tenant-scoped; Own instance beats shared.
  - **Git, branches and promotion**: One repo per change.
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
