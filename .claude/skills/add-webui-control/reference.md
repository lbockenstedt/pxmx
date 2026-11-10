# Add a WebUI Control — exhaustive touch-point map

Workspace root: `/Users/lbockenstedt/vscode`. Repo used: `lm/` (hub backend +
WebUI). Only touches `cs/lm-spoke/static/sim-views.js` if the control lives in
the Simulations module (see §5).

Read the *current* files to find exact insertion points — the constructs
below are stable; line numbers are not. Placeholder `<feature>` = the route
module / feature area name (e.g. `pxmx`, `firewall`, `nw`).

Legend: ☐ = a required edit. Skip a section only when the note says it's
optional and explain why in your final report.

---

## 1 — Backend route — REQUIRED

### ☐ `core/src/routes/<feature>.py`  (new file, or an addition to an existing one)
- Export `register(app, hub, ctx)` — the only contract the route-registration
  block in `api.py` requires. Follow `core/src/routes/exec.py`'s shape for a
  simple admin-triggered action:
  - A small `_require_admin(request)` (or the tenant-scoped
    `_authz_<feature>()` shape from `firewall.py`/`ldap.py`/`nw.py`/
    `truenas.py`/`client_debug.py` if the resource is tenant-owned) that
    raises `HTTPException(403)` via `ctx._is_admin` / `access.write_scope`.
  - A config-gate check (`_cfg().get("enabled")`, see §2) before doing
    anything, if the feature has an on/off toggle.
  - Audit-log the action (`logger.warning(...)` or similar) before AND
    after, matching the surrounding module's convention.
  - Any blocking call — subprocess, another service, a slow library —
    wrapped in `await asyncio.to_thread(fn, *args)`. Never call it inline.
- ☐ Import the new module and add its `.register(app, hub, ctx)` call in the
  route-registration block in `core/src/api.py` (alongside the existing
  `security_routes.register(...)`, `exec_routes.register(...)`, etc. calls).

### ☐ Auth/permission check — pick ONE, matching the resource's ownership
- System-wide admin only: `ctx._is_admin(sess)`.
- Tenant-admin (tenant-confined): `ctx._is_tenant_admin(sess)`.
- A specific tenant-owned resource (device, firewall rule, LDAP config,
  etc.): resolve the resource's `tenant_id`, then
  `access.write_scope(sess, tenant_id)` (mutating) or
  `access.read_scope(sess, tenant_id)` (read-only) — `403` on `"deny"`.
  Copy `firewall.py`'s `_authz_firewall()` shape exactly.
- A whole module's visibility (nav-level, not per-action): `access.
  has_module_access(sess, "<module>")`.
- Never write a bespoke `if user.role == ...` check outside these helpers.

## 2 — Config default (OPTIONAL — only if the control needs a persisted setting)

### ☐ Module-level `_DEFAULTS` + `_cfg()` in the SAME route file
- Follow `hub_watchdog.py`'s shape:
  ```python
  _DEFAULTS = {"enabled": False, ...}
  def _cfg() -> dict:
      c = dict(_DEFAULTS)
      c.update(hub.state.get_global_config().get("<feature>") or {})
      return c
  ```
- Read via `_cfg()` everywhere; write via
  `hub.state.update_global_config({"<feature>": {...}})` (merges + persists
  encrypted + marks dirty — never write `system_state["global_config"]`
  directly).
- A single boolean/simple flag can skip the dict and just do
  `(hub.state.get_global_config() or {}).get("<feature>", <default>)`
  inline (see `exec.py`'s `remote_exec` key) — use the dict shape only once
  there's more than one field.

## 3 — The view — REQUIRED

### ☐ The control's markup, built with `safeHtml`
- Read `docs/webui.md` → "How to write markup" first; `js/views/os-updates.js`
  is the reference view.
- Where it goes:
  - Feature area already an ES module in `js/views/` → add it there.
  - Feature area is a classic `js/*.js` / `main.js` page → classic scripts
    cannot `import`, so put the control's render function and its actions in
    an ES module (extend the area's module in `js/views/`, or add one with a
    `<script type="module" src="js/views/<name>.js?v=__WEBUI_VERSION__">` tag
    at the end of `index.html`), register its entry point on `lmViews`
    (`lmViews.fwoverview = renderFirewallOverview;` in
    `js/views/firewall-overview.js`) and call `lmViews.<name>(container, …)`
    from the classic loader (`js/fleet-admin.js` does this for the firewall
    Overview tab).
  - `sim-views.js` (Simulations; classic, with a cs twin — §7) → render the
    button, then wire it with `addEventListener` (the shape its Save buttons
    use). No inline `onclick=`.
- Markup shape:
  ```js
  import { safeHtml } from '../core/safe-html.js';
  import { delegate } from '../core/view.js';

  // One row's "do the thing" button; the id rides in data-id.
  const thingButton = row => safeHtml`<button data-action="doThing"
      data-id="${row.id}" title="Explain exactly what the action does"
      class="px-2 py-1 rounded text-xs font-bold bg-[#01A982]/10
             hover:bg-[#01A982]/20 text-[#01A982] border border-[#01A982]">
      Do thing</button>`;
  ```
  - Every `${value}` is escaped by `safeHtml`; never call `escapeHtml()`
    inside it (double escape). Trusted markup (e.g. `helpIcon()`) goes in
    `rawHtml(...)`. Arrays of `safeHtml` results are inserted as-is (no
    `.join('')`).
  - Never print a UUID as text: `_spokeName(obj)` (`main.js`) for names, raw
    id only in a `title=`.
- Class tokens (from `docs/webui-style.md`, confirm current values there
  before copying — it's the canonical source, this list is illustrative):
  - Primary/HPE-green: `bg-[#01A982]/10 hover:bg-[#01A982]/20
    text-[#01A982] border border-[#01A982]`
  - Destructive: `bg-red-100 hover:bg-red-200 text-red-700` (or solid
    `bg-red-600 text-white` for a high-emphasis destructive action)
  - Neutral/secondary: `bg-slate-100 hover:bg-slate-200 text-slate-600
    border border-slate-200`
  - Size tiers: large CTA `px-6 py-2 rounded-md text-sm font-bold`; toolbar
    `px-4 py-2 rounded-md text-xs font-bold` (or `px-3 py-1.5`); inline row
    `px-2 py-1 rounded text-xs font-bold`.
- ☐ `title="…"` on the button, stating what the action DOES (not a
  restatement of the label) — every existing HPE-green action button in
  this codebase carries one; this is not optional.

## 4 — JS action — REQUIRED

### ☐ An entry in the `delegate()` actions object
```js
// Actions for every data-action control on this page (js/core/view.js delegate).
delegate(container, {
    // POST the thing for one row, then refresh the list.
    async doThing(el) {
        showToast('Doing the thing…', 'info');           // optimistic, if not instant
        try {
            const r = await fetch(`/api/<feature>/thing/${encodeURIComponent(el.dataset.id)}`,
                                  { method: 'POST' });
            const body = await r.json().catch(() => ({}));
            if (!r.ok) { showToast('Failed: ' + (body.detail || r.status), 'error'); return; }
            showToast('Done.', 'success');
            // re-render the affected part (view.refresh() / setState) — never a full page reload
        } catch (e) { showToast('Failed: ' + (e.message || e), 'error'); }
    },
});
```
- `delegate(root, actions)` puts one listener on `root`: `data-action` fires
  on click (or on change for inputs/selects), `data-input` on each keystroke,
  `data-submit` on form submit. Calling it again on the same root swaps the
  actions, so a re-render is safe.
- Use whichever fetch helper the surrounding module already uses
  (`_spokeFetch` in `js/dns-dhcp-cluster.js`, or plain `fetch` as in
  `js/views/firewall-overview.js`) — don't introduce another one, and only
  call helpers that exist (lm `6958d5ee`: an undefined `apiFetch` threw
  `ReferenceError` and blanked OS Updates).
- `showToast(message, type)` (`main.js`) — `type` is `'success'` / `'error'` /
  `'info'`. For a destructive action, use `showConfirmToast(message, opts)`
  first rather than acting immediately on click.
- No `window.<fn> =` global for the handler: the ratchet counts them.

## 5 — State/status wiring (OPTIONAL — only if the control introduces a new state)

- ☐ Store the state as a plain field on the relevant hub-state record
  (via §2's config/state mechanism, or wherever the owning resource's
  record already lives) — not a new parallel data structure.
- ☐ A local status→badge/class mapping function in the SAME view file,
  shaped like `statusBadge` (CPPM device list) or `spokeStatusMessage`
  (spoke/agent connectivity) — a small function/closure mapping the
  string/bool state to a Tailwind badge class, called inline per row.
  There is no central status registry to extend — LM's pattern is
  per-view, not global.

## 6 — Docs — REQUIRED

### ☐ Extend the existing `lm/docs/<module>.md` page for this feature area
- NOT `docs/webui.md` — that page documents the WebUI's overall
  architecture/routing, which a single control never changes.
- The Help drawer renders these docs verbatim (in-app help + the Ask-AI's
  knowledge base) — this step is how the control becomes discoverable, not
  a documentation-only formality.

## 7 — Dual-copy check (REQUIRED only if you touched `sim-views.js`)

- ☐ If the control's view code landed in `sim-views.js`: run the
  `dual-copy-guard` skill and port the equivalent change to
  `cs/lm-spoke/static/sim-views.js` — this file is a hand-maintained twin,
  NOT generated, and drifts silently otherwise.
- ☐ If the control also needed backend support inside the Simulations
  quota/config system: check the `sim_quota.py` twin
  (`lm/core/src/simulations/sim_quota.py` ⇄ `cs/lm-spoke/src/sim_quota.py`)
  too.
- If the control lives anywhere else (`main.js`, `js/*.js`, `index.html`, `help.js`,
  `update_handler.js`), there is no twin — this section is a no-op, report
  it as such rather than silently skipping it.

## 8 — Verify

- ☐ `python3 -m py_compile` on any touched `.py`; `node --check` on every
  touched `.js` (CI runs it on all WebUI JS — one stray brace blanks the UI,
  lm `743fe9df`).
- ☐ `cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_webui_ratchets.py tests/test_webui_safe_html.py tests/test_webui_view.py`
  (the ratchet fails on any new inline `on*=`, `escapeHtml(` or `window.x =`).
- ☐ Run the repo's own relevant self-tests if the feature area has them.
- ☐ Run `dual-copy-guard` (cheap even when §7 was a no-op — confirms it
  actually was).
- ☐ Report exactly which of sections 1-7 you touched and which you skipped,
  and WHY for each skip (e.g. "§2 skipped — control is stateless, no config
  needed" / "§5 skipped — no new state introduced" / "§7 no-op — control
  lives in main.js, no sim-views.js twin exists").
