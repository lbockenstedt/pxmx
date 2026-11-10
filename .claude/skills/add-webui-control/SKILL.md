---
name: add-webui-control
description: >-
  Use when adding a single new interactive control to LM's WebUI — a button,
  toggle, or small form that triggers a new backend action (e.g. "add a
  button to clear missing USB dongles", "add a toggle for X"). Touches a
  backend route module, a config default, the WebUI view that renders the
  control, its JS handler, and — only if the control lives in the
  Simulations (sim-views.js) module — a dual-copy twin in the cs repo. This
  skill is the small, self-contained recipe for a single bolt-on control; it
  is NOT for adding a new simulation (use add-simulation for that) or for
  architectural changes that touch auth, transport, encryption, or the
  self-update mechanism — those need a human, not this skill. Invoke it
  whenever asked to add a UI button/control/toggle to LM's WebUI.
---

# Add a WebUI Control

LM's WebUI (`WebUI/`) is a single-page, client-side app — one `index.html`
shell + `main.js` and `js/*.js` (older classic scripts that share globals) +
ES-module views in `js/views/*.js` (the current style, see `docs/webui.md`
"How to write markup") + `sim-views.js` (the Simulations module only). There
is no build step: files are served static, versioned by a `?v=` query string.
New markup uses `safeHtml` and events are wired with `data-action` +
`delegate()`; `core/tests/test_webui_ratchets.py` fails if the count of
inline `on*=` handlers, `escapeHtml(` calls or `window.x =` globals goes
up. Adding one control is a small, bounded change across a backend route, the view that renders it, and
its JS handler. Work through `reference.md` (same folder) for the exact
touch-points and code shapes; this file is the shape + the boundaries.

## The shape (in order)

1. **Backend route** — a new or extended file under `core/src/routes/`
   exporting `register(app, hub, ctx)`, wired into `core/src/api.py`'s
   existing route-registration block. Use `ctx`'s auth closures
   (`ctx._is_admin` / `ctx._is_tenant_admin` / `access.read_scope`/
   `write_scope`) for permission checks — never hand-roll a check. Any
   blocking call (subprocess, another service, a slow library) goes through
   `await asyncio.to_thread(...)` — LM's single FastAPI event loop stalls
   the ENTIRE app for the duration of a blocking call otherwise, the same
   class of bug documented in ab's own history ("TypeError: Load
   failed" from a stalled request).
2. **Config default** (only if the control needs a persisted setting) — a
   module-level `_DEFAULTS` dict + a `_cfg()` helper that merges it with
   `hub.state.get_global_config()` at READ time (see `hub_watchdog.py`'s
   shape). This means an existing install with no key at all just gets the
   default on its next read — no migration/backfill step needed. Never
   invent a second config-loading pattern.
3. **The view** — add the control's markup with the `safeHtml` tagged
   template (`WebUI/js/core/safe-html.js`) as
   `<button data-action="doThing" data-id="${row.id}">`, never an inline
   `onclick=`. If the feature area is already an ES-module view in
   `js/views/`, add it there. If it is a classic `js/*.js` page, put the
   control's render + actions in an ES module (extend the area's existing
   `js/views/` module or add one, registered on `lmViews` like
   `lmViews.fwoverview` in `js/views/firewall-overview.js`) and call it from
   the classic loader — classic scripts cannot import `safeHtml`/`delegate`.
   In `sim-views.js` (classic, with a cs twin — step 7) wire the button with
   `addEventListener` after rendering, as its Save buttons do. Style it from
   `docs/webui-style.md`'s token set (HPE-green primary:
   `bg-[#01A982]/10 hover:bg-[#01A982]/20 text-[#01A982] border
   border-[#01A982]`; destructive: `bg-red-100 hover:bg-red-200
   text-red-700`; neutral: `bg-slate-100 hover:bg-slate-200 text-slate-600
   border border-slate-200`). **A `title="…"` tooltip explaining exactly
   what the action does is mandatory** — every existing action button in
   this codebase carries one; a control without one is incomplete.
4. **JS action** — an entry in the `actions` object passed to
   `delegate(root, actions)` (`WebUI/js/core/view.js`); it is called as
   `actions.doThing(el, event)` and reads its arguments from `el.dataset`.
   Follow the established shape: a `//` comment above it, an optimistic
   `showToast(..., 'info')` (if the action takes a moment), `await` the fetch, branch on the response's `ok`, then a
   `success`/`error` toast, then (if the control changes something visible)
   re-render/refresh the view. Never a full page reload.
5. **State/status, if the control introduces one** — LM has no central
   status-enum registry (unlike ab's processed-issue counters). Add a
   local status→badge/class mapping function in the SAME view file, shaped
   like `statusBadge`/`spokeStatusMessage` — a small closure or function
   mapping the string/bool state to a Tailwind badge class, called inline
   per row. Store the state itself as a plain field on the relevant
   hub-state record (via the same config/state mechanism from step 2), not
   a new parallel data structure.
6. **Docs** — extend whichever `lm/docs/<module>.md` page already covers
   this feature area (not `webui.md`, unless the change alters the WebUI's
   overall architecture/routing, which a single control never does). The
   Help drawer renders these docs verbatim as in-app help / the Ask-AI
   knowledge base, so this is not a separate documentation-only step — it's
   how the feature becomes discoverable and explainable to users.
7. **Verify** — run the `dual-copy-guard` skill (mandatory if the control
   touched `sim-views.js`, harmless no-op otherwise), run
   `node --check` on each touched JS file and
   `cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_webui_ratchets.py tests/test_webui_safe_html.py tests/test_webui_view.py`, and report exactly which of the 6 steps above you did and which you
   intentionally skipped (e.g. no config default needed because the control
   is stateless) — a silent skip reads as "done" when it isn't.

## Boundaries — the rules a control MUST obey

- **No new top-level nav item, view, or route category without a human.**
  This skill is for bolting a control onto an EXISTING view/feature area —
  if the request implies a wholly new section of the app, that's a design
  decision, not a bolt-on; stop and say so rather than improvising one.
- **Never hand-roll an auth check.** Use `ctx`'s closures / `access.py`'s
  `read_scope`/`write_scope`/`is_admin`/`is_tenant_admin`/
  `has_module_access` — these encode the RBAC model (system-admin vs
  tenant-admin vs per-module permission flags); a bespoke check is exactly
  how a permission gap gets introduced.
- **Never touch auth/transport/encryption/self-update code to add a
  control.** mTLS, the hub-spoke signing scheme, Fernet state encryption,
  and the watchdog/self-update mechanism are boundaries this skill's caller
  (AppBuilder's feature-classifier) should already have kept you away from —
  if you find yourself needing to touch one of those files to make a
  "simple button" work, stop; that is no longer a bolt-on.
- **Config always defaults via the `_cfg()`-merge shape**, never a one-time
  migration script, and never a raw `get_global_config()[key]` that KeyErrors
  on a fresh install.
- **Blocking calls in routes are always offloaded** (`asyncio.to_thread`) —
  LM is a single-event-loop app; an inline blocking call stalls every other
  request, not just the one that made it.
- **A `title=` tooltip is mandatory on every new action control.**
- **No new inline `on*=` handler, `escapeHtml(` call or `window.x =`
  global.** Use `safeHtml` + `data-action` + `delegate()`; the ratchet test
  (`core/tests/test_webui_ratchets.py`) fails on any increase. Wrap trusted
  markup (e.g. `helpIcon()` output) in `rawHtml(...)`.
- **No UUIDs as visible text** — use `_spokeName(obj)` (`main.js`); keep the
  raw id in a `title=` only (fleet-rules.md → WebUI › No UUIDs in the UI).
- **`sim-views.js` changes need the dual-copy-guard skill** — `cs/lm-spoke/
  static/sim-views.js` is a hand-maintained twin, not generated, and drifts
  silently otherwise. `main.js`/`js/*.js`/`index.html`/`help.js`/`update_handler.js`
  have no twin — a change there is single-copy and complete on its own.

## Scope

This skill is for **one bolt-on control on an existing view**, backend route
included. It is explicitly NOT for: adding a new client simulation (use
`add-simulation`), adding a new top-level module/nav section, or any change
to authentication, the hub-spoke transport/signing scheme, encryption, or
the self-update/watchdog mechanism — those need a human to design. A whole
new page or tab is the `add-webui-view` skill.

## Fleet rules

- `lm/.github/fleet-rules.md` is the canonical rules file (it replaces
  `ab/fleet_rules.md`); this skill applies its WebUI, Security and secrets,
  Multitenancy and Code quality sections.
- Every Python function has a docstring plus step comments; every JS
  function a `//` comment.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
