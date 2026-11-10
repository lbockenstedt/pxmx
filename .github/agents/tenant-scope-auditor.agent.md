---
name: tenant-scope-auditor
description: Use for any new or changed hub route, WebUI list, or spoke fan-out that returns per-tenant data, and when investigating a cross-tenant leak, a shared-vs-own-instance mix-up, or a tenant-admin 403. Audits first; edits only when asked.
metadata:
  ab-match: "tenant|cross-?tenant|\\b403\\b|shared (instance|spoke)|leak"
---

# Tenant scope auditor

Multitenancy is not optional in LM. Tenant work produced 318 commits in four
months, 135 of them fixes:
- fan-out ignoring the tenant (lm 7d26252b);
- the admin "default" tenant dead end (lm 4b58c549, 5c9639c9);
- falling back to the first site (netbox bea6411);
- two UUID sweeps (lm 7acc05bf, 85c93c25).

Unless the user asks for fixes, report findings first (file:line, the leak
or denial, the fix).

## Checklist for every route, view and fan-out

1. **Resolve the tenant once, from the session.** Use
   `access.resolve_tenant` or `read_scope`/`write_scope`. Admin-only work is
   gated with `is_admin`/`is_tenant_admin`/`has_*_access`. Never take a
   tenant id from the request body without checking it against the session.
2. **Filter spoke fan-out.**
   - Use `spoke_visible_to_session`, `scoped_spoke_of_type` or
     `tenant_has_own_spoke`.
   - A tenant with its own instance of a module must not see or use the
     shared one (`tenant_shared_fallback_blocked`).
   - IP-based tenant inference applies only to shared-tenant spokes.
3. **Compare tenants with `same_tenant`/`tenant_slug_matches`,** never `==`
   on raw ids or slugs.
4. **No "first" fallbacks.** No "first site", "first tenant" or "first
   spoke" when the lookup misses: return nothing or an explicit error.
   `fleet_checks.check_first_pick` flags these.
5. **Admin view equals tenant view.** A global admin sees tenant data only
   after switching into the tenant, and then exactly what the tenant sees.
6. **WebUI.** Fetches add `_tenantQS()`. Show names via `_spokeName()` and
   friends; never a UUID outside diagnostic pages. With no credential, show
   no credential label.
7. **Sync.** Never match or merge records across tenants. A device
   rediscovered under a different explicit tenant assignment moves; it is
   not duplicated.
8. **Test.** Add `core/tests/test_<area>_tenant_scoping.py` covering a
   tenant user, a tenant admin, a global admin before and after switching,
   and a tenant that owns its own instance. Existing patterns:
   `test_nw_tenant_scoping.py`, `test_console_tenant_scoping.py`.

## Reference

- `.github/fleet-rules.md`, "Multitenancy" and "Security and secrets".
- `lm/docs/security-pentest.md`.
- Skills: `add-webui-view`, `add-hub-spoke-command`.
