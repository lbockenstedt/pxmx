---
name: credential-vault-wiring
description: Use whenever a record, form, module or spoke config gains a password, API key or token, or when moving an existing inline secret into the Credential Vault. Wires the field end to end (instance_vault, routes, push overlay, WebUI vault picker, log masking, docs, tests).
metadata:
  ab-match: "vault|\\b(api[ _-]?keys?|passwords?|secrets?|tokens?|credentials?)\\b"
---

# Credential vault wiring

Rule: when a vault is configured, every password, API key and token lives in
the Credential Vault, and the record holds only a
`vault_credential {bucket, name}` reference. With no vault, it is
Fernet-encrypted on disk. New modules and fields use the vault from day one.

About 180 commits went into this area, mostly secrets that were added inline
and moved to the vault later (lm 9d40fdea, 7d436d03).

Follow the **`add-vault-secret-field`** skill step by step. This agent adds
the judgement around it.

## Before you start

- List every secret the feature touches: the new field, plus any secret
  already in the same record or spoke config that still sits inline. Fix
  them together.
- Find where the secret is *read*, not just where it is saved. An inline
  read path left behind is the most common miss (lm e3e974ff).

## Must hold when you finish

1. The field is in `SECRET_FIELDS` (and in `VAULT_REQUIRED` when vault-only).
2. Saving calls `validate_ref` and `strip_inline_secrets`. A blank re-save
   keeps the stored credential instead of stripping it (lm 6b85cdb4).
3. List and GET routes return only `<field>_set`, never the secret or its
   reference details beyond the name.
4. The hub `overlay()`/`overlay_many()` runs before the config is pushed to
   the spoke.
5. The WebUI field has `vaultSecret: true` in a form with `vaultPicker`.
   Inline inputs are hidden while a vault is enabled. Check that the picker
   actually lists credentials (lm 2ea78ad4).
6. The spoke masks the field in every log line, including nested in
   UPDATE_CONFIG payloads (truenas 0c60732).
7. Tests: `core/tests/test_instance_vault.py`,
   `test_cred_vault_put_guard.py`, `test_secret_hygiene.py`, plus a
   round-trip test for this field.
8. `docs/credential-vault.md` and the module's doc list the field.

## Reference

- `.github/fleet-rules.md`, "Security and secrets".
- `lm/MODULE_REQUIREMENTS.md` ("Credentials must live in the Credential Vault").
- `lm/docs/credential-vault.md`.
