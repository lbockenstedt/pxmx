---
name: add-vault-secret-field
description: >-
  Use when a hub-managed instance or device record (firewall, TrueNAS
  appliance, NetBox/IPAM, ClearPass/NAC, network device, console, a new
  module) gains a secret field — an API key, token, password, client secret
  or SNMP community — or when an existing inline secret must move into the
  Credential Vault. Covers the whole path: SECRET_FIELDS/VAULT_REQUIRED in
  core/src/instance_vault.py, validate_ref + strip_inline_secrets on the
  create/update routes, overlay/overlay_many at push time, the masked list
  response, the vaultPicker/vaultSecret/vaultRequired WebUI form flags, spoke
  log masking, tests and docs/credential-vault.md. Not for AB's own LLM
  provider keys (use ab-add-llm-provider) or for changing the vault backend
  itself.
---

# Add a vault-backed secret field

A secret on a hub-managed record is never stored inline once the Credential
Vault is on. The record keeps a non-secret `vault_credential: {bucket, name}`
reference; the hub resolves the secret onto a **copy** only when it pushes
config to the spoke. Five commits built this path one product at a time
(lm `9d40fdea` firewall, `8b0ab1c7` NetBox, `06f5847d` nw/NAC, `cefb9346`
enforcement, `7d436d03` TrueNAS), and each of them needed follow-up fixes.
The rule text is in `MODULE_REQUIREMENTS.md` → "Credentials must live in the
Credential Vault"; user-facing behaviour is in `docs/credential-vault.md`.

## When to use / when not to use

Use it when:
- a route, Pydantic model or WebUI form gains a field named like
  `api_key`, `api_secret`, `token`, `password`, `client_secret` or `community`;
- a product's existing inline secret must become vault-backed;
- AB's `check_vault_secrets` (`ab/fleet_checks.py`) flags "Secret stored
  without the Credential Vault" or "Secret field in the WebUI has no vault
  picker".

Do not use it for:
- AB's LLM provider keys (`ab-add-llm-provider`);
- the Credential Vault store itself (`core/src/routes/cred_vault.py`
  internals, encryption, Key Vault backends): that needs a human;
- a non-secret setting pushed to spokes (`add-hub-pushed-setting`).

## Steps (in order) — all in `lm` unless noted

1. **Declare the secret** in `core/src/instance_vault.py`:
   - add `"<storage_key>": {"<field>": (<accepted vault keys>…)}` to
     `SECRET_FIELDS`. `<storage_key>` is the `global_config` list key, e.g.
     `"truenas_appliances"`. The tuple lists the vault-secret keys that can
     supply the field, most specific first (lm `7d436d03`:
     `"api_key": ("api_key", "apikey", "token", "key", "value", "password")`);
   - add `<storage_key>` to `VAULT_REQUIRED` if the product cannot connect
     without the secret;
   - if the vault secret also carries a non-secret value the record needs
     (e.g. ClearPass `client_id`), use `NONSECRET_VAULT_FIELDS`. lm `e3e974ff`
     fixed `client_id` still being read inline.
2. **Create/update routes** (`core/src/routes/<product>.py`, where
   `_session_user = ctx._session_user` and `_is_admin = ctx._is_admin`).
   After the tenant check and before saving:
   ```python
   # Require a vault reference when the vault is on; never keep the inline copy.
   sess = _session_user(request)
   await instance_vault.validate_ref(hub, rec, sess, is_admin=_is_admin(sess),
                                     storage_key="<storage_key>")
   instance_vault.strip_inline_secrets(rec, "<storage_key>")
   ```
   On update, a blank secret means "keep the stored one". Copy the old value
   in before `validate_ref` (see the TrueNAS update route).
3. **List/read routes never return the secret.** Replace it with a presence
   flag:
   ```python
   aa["api_key_set"] = bool(aa.get("api_key")) or instance_vault.has_vault_ref(aa)
   aa.pop("api_key", None)
   ```
4. **Push paths resolve onto copies.** Every place that builds the spoke
   payload calls `await instance_vault.overlay_many(hub, [copy.deepcopy(r) …],
   "<storage_key>")` (or `overlay` for one record). There are usually two:
   - the route's own push helper, and
   - `LabManagerHub.push_config_to_spoke` in `core/src/main.py`, the
     reconnect path.

   lm `7d436d03` had to patch both.
5. **WebUI form** — `WebUI/js/module-management.js`:
   - on the product's entry in `INSTANCE_PRODUCTS` or `DEVICE_TYPES`, set
     `vaultPicker: true`, plus `vaultRequired: true` if step 1 used
     `VAULT_REQUIRED`;
   - on the secret field, set `vaultSecret: true` and keep
     `type: 'password'` with a "blank = keep" placeholder;
   - drive the row summary from the `*_set` flag, never the secret.
6. **Spoke logs** (in the spoke repo). The spoke's command logger must mask
   the field, including when it is nested in lists such as
   `UPDATE_CONFIG.appliances[]`. Most spokes have a static `_mask()`, e.g.
   `truenas/src/truenas_spoke.py` and `nw/src/nw_spoke.py`; add the key to its
   sensitive set. On the hub, `core/src/log_redaction.py` (`_is_secret_field`)
   covers common names; check that yours matches.
7. **Tests** — add to `core/tests/test_instance_vault.py`:
   - a missing reference is rejected when the vault is on;
   - `strip_inline_secrets` drops the field;
   - `overlay` resolves it.

   In the spoke repo, add a `_mask` test with a nested secret (truenas `0c60732`).
8. **Docs** — add the product's row/section to `docs/credential-vault.md`,
   and a line to the product's own page (`docs/netbox.md`, `docs/opnsense.md`,
   …).

## Tests

```bash
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_instance_vault.py tests/test_cred_vault_put_guard.py tests/test_secret_hygiene.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_cppm_reconnect_verify_ssl.py   # reconnect-push projection still intact
cd truenas && /tmp/nbvenv/bin/python -m pytest -q tests/test_truenas_spoke.py               # or the spoke you touched
node --check lm/WebUI/js/module-management.js
python ../ab/fleet_checks.py --base origin/dev --repo lbockenstedt/lm                      # run from lm/
```

## Common misses

- **Blank re-save wipes the secret.** The edit form never pre-fills a
  secret, so saving the form unchanged sends blanks. A DNS provider secret
  that also carries a non-secret `provider` marker passed the "all empty"
  check and was overwritten with empty fields. Fixed in lm `6b85cdb4`:
  `routes/cred_vault.py` and `main.js` now reject a save in which every
  non-marker field is blank.
- **A secondary value is still read inline.** ClearPass `client_id` kept
  coming from the record instead of the linked secret — lm `e3e974ff`
  (`NONSECRET_VAULT_FIELDS`).
- **The new module stores the secret inline.** TrueNAS shipped with
  `api_key` stored in `global_config` (lm `14f86224`). lm `7d436d03` moved it
  into the vault, backend and form picker together; the firewall needed a
  second pass to become vault-only (lm `54bf0e6c` after `9d40fdea`). AB now
  flags both halves: "Secret stored without the Credential Vault" and
  "Secret field in the WebUI has no vault picker". Picker bugs themselves:
  lm `2ea78ad4`.
- **Only one push path overlays.** The reconnect push in
  `push_config_to_spoke` skipped the overlay, so the spoke got no key after a
  restart — lm `7d436d03` patched both paths.
- **The spoke logs the secret.** `UPDATE_CONFIG` logged nested appliance API
  keys in clear text — truenas `0c60732` made `_mask` recurse into lists.
- **The reconnect projection drops fields.** `_INSTANCE_CONFIG_SOURCES` in
  `main.py` must mirror `_NAC_PAYLOAD`/`_IPAM_PAYLOAD` in
  `routes/tenant_devices.py`. lm `e72faa04` fixed `verify_ssl` being lost on
  every reconnect; any vault-related field you add to one belongs in both.

## Done when

- [ ] `SECRET_FIELDS` (and `VAULT_REQUIRED` / `NONSECRET_VAULT_FIELDS` if
      needed) updated
- [ ] create **and** update routes call `validate_ref` then
      `strip_inline_secrets`; a blank update keeps the stored value
- [ ] list routes return `<field>_set`, never the secret
- [ ] every push path (route helper + `push_config_to_spoke`) uses `overlay`/
      `overlay_many` on copies
- [ ] form has `vaultPicker` (+ `vaultRequired`) and `vaultSecret` on the field
- [ ] spoke `_mask` covers the field, nested too
- [ ] tests above pass; `fleet_checks.py` shows no vault finding
- [ ] `docs/credential-vault.md` and the product doc updated

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  This skill implements **Security and secrets › Credentials go in the vault**
  and **Never log or expose secrets**, and **Multitenancy › Everything is
  tenant-scoped** (call `validate_ref` after the tenant check).
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
