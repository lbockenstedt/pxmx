---
name: add-webui-view
description: >-
  Use when adding a new WebUI page, tab or sub-view (a new nav entry, a new
  sub-menu tab such as Firewalls → Overview, or moving a page into an ES
  module like OS Updates): the js/views/<name>.js ES module with safeHtml +
  createView/delegate, the lmViews registration, the <script type="module">
  tag in index.html, the router maps in main.js (VIEW_LOADERS,
  VIEW_SUBMENUS, VIEW_CHILDREN, VIEW_LABEL), the hub route it reads, the
  stale-render guard, names-not-UUIDs, docs (the in-app Help) and the
  WebUI tests. Not for a single button/field on an existing page
  (add-webui-control).
---

# Add a WebUI view / tab

New pages are ES modules in `WebUI/js/views/`. They build markup with
`safeHtml`, wire events with `delegate()` (or Preact), and are reached from
the classic router through the `lmViews` registry in `main.js`. Reference
builds:
- lm `ecb6f238`: Firewalls → Overview. Touched `index.html`,
  `js/fleet-admin.js`, `js/views/firewall-overview.js`, `main.js`,
  `routes/firewall.py` and `docs/opnsense.md`.
- lm `98f2fb12`: OS Updates moved to an ES module, which introduced
  `lmViews`.

Most follow-up fixes were runtime JS errors that broke the whole UI, and
UUIDs shown instead of names.

## When to use / when not to use

Use it when:
- you add a new nav view, sub-menu tab or child tab;
- you move an existing classic page into `js/views/`;
- a page needs its own polling or re-render (`createView`).

Do not use it for:
- a single button, field or modal on an existing page
  (`add-webui-control`);
- the backend command behind the data (`add-hub-spoke-command`).

## Steps (in order)

1. **Hub route.** The page reads a hub REST route, never a spoke directly.
   - Reuse a mapping table where one exists. Firewalls → Overview needed only
     `"overview": "OPNSENSE_GET_OVERVIEW"` in `FW_READ_CMDS`
     (`core/src/routes/firewall.py`).
   - Otherwise write the route with `add-hub-spoke-command`.
   - Serve cached data when the module has a poll cache
     (`docs/caching-contract.md`).
   - Scope the route to the tenant.
2. **View module** `WebUI/js/views/<name>.js`:
   - Start with a `//` header comment saying what the page shows, which
     route it reads and how it is opened.
   - Import: `import { safeHtml, rawHtml } from '../core/safe-html.js'`
     (and `import { createView, delegate } from '../core/view.js'` when the
     view re-renders or has actions).
   - Render with `safeHtml` only: no `escapeHtml(`, no `.join('')`, and no
     inline `on*=`.
   - Wire events with `delegate(root, { action(el, ev) {…} })` and
     `data-action` / `data-input` / `data-submit`.
   - For stateful widgets use Preact (`../../assets/preact-htm.module.js`),
     as `CveLookup` in `js/views/os-updates.js` does.
   - **Stale-render guard:** after every `await`, return early if the user
     has navigated away. `renderFirewallOverview` does
     `if (currentView !== 'opnsense' || currentSubView !== 'Overview') return;`.
   - **Names, not UUIDs:**
     - show `_spokeName(obj)` / `_spokeLabelFor(id)` (main.js) or a
       server-side `display_names.node_name()`;
     - put the raw id only in `title=`.
   - Every fetch checks `r.ok` and surfaces `body.detail` (see
     `fetchOverview`). Use only helpers that exist: plain `fetch`, or
     classic globals like `showToast` / `_spokeFetch`. There is no
     `apiFetch`.
   - End with `lmViews.<key> = render<Name>;`.
3. **Load it.** Add
   `<script type="module" src="js/views/<name>.js?v=__WEBUI_VERSION__"></script>`
   next to the other module tags at the end of `WebUI/index.html`.
4. **Route to it** in `WebUI/main.js`:
   - a new tab: add it to `VIEW_SUBMENUS[<view>]` (first position = default
     tab, as in `c6e19e65`);
   - a new child tab: add it to `VIEW_CHILDREN`;
   - a new top-level view: add `VIEW_LOADERS[<view>]`, `VIEW_LABEL` and the
     nav entry in `index.html`.
   - In the module's loader (e.g. `loadOpnsenseManagement` in
     `js/fleet-admin.js`), branch to the module:
     `if (subMenu === 'Overview') return lmViews.fwoverview(container, firewalls);`
5. **Style.** Use HPE Design System tokens and classes
   (`docs/webui-style.md`), plus the HPE button classes and feedback rules
   from `add-webui-control`.
6. **Docs = in-app Help.** Update the module page in `lm/docs/` (e.g.
   `docs/opnsense.md`). `help.js` renders these files verbatim and Ask AI
   answers from them. For a new pattern, update `docs/webui.md`.
7. **Tests.** Add a source-level test in `core/tests/test_webui_<name>.py`
   (see `test_webui_os_updates_view.py`) for the route, the registration and
   the guard. Make the view reachable through `setView`/`setSubView`, so
   the browser smoke walk (`core/tests/webui_walk.py`) opens it.

## Tests

```bash
cd lm && for f in WebUI/main.js WebUI/js/*.js WebUI/js/views/*.js WebUI/js/core/*.js; do node --check "$f" || echo "FAIL $f"; done
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_webui_ratchets.py tests/test_webui_safe_html.py tests/test_webui_view.py tests/test_webui_inline_handlers_defined.py tests/test_webui_stale_page_load.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_webui_<name>.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_webui_browser_smoke.py   # needs: pip install playwright && python -m playwright install chromium
```
CI runs the same `node --check` on every WebUI JS file. `node --check`
catches syntax errors only. The browser smoke test is the only check that
catches an undefined name at runtime.

## Common misses

- **Undefined helper → ReferenceError.**
  - OS Updates called `apiFetch`, which does not exist — lm `6958d5ee`.
  - Module Management threw "Can't find variable: btnSecCls" — lm
    `b450e449`.

  Grep that every helper you call is defined (or imported) before
  committing, and run the smoke test.
- **A syntax error breaks the whole UI.** A bad escape in a template
  string — lm `743fe9df`. Run `node --check` on every changed file.
- **UUIDs in the UI.** Spoke registration ids were shown instead of names —
  lm `7acc05bf` (fleet-admin, module-management, main.js) and `85c93c25`
  (`js/certificates.js`).
- **Stale data after navigation or tenant switch.** A slow fetch overwrote
  the page the user had moved to, and the dashboard kept the previous
  tenant's data. Add the guard; with no `VIEW_LOADERS` entry, a tenant
  switch does not re-render (see the comment on `VIEW_LOADERS.dashboard`).
- **Stale cached core module.** Unversioned `js/core/*` imports must be
  served `no-cache`. lm `98f2fb12` changed `api.py` and added
  `test_unversioned_static_asset_is_revalidated`. Do not import from
  outside `js/core/` or `assets/` without the same treatment.

## Done when

- [ ] hub route exists, is tenant-scoped and serves from the cache
- [ ] `js/views/<name>.js`: header comment, safeHtml, delegate/Preact,
      stale guard, names not UUIDs, `r.ok` checks, `lmViews.<key>`
- [ ] `index.html` module tag; `main.js` / loader routing wired
- [ ] ratchet counts not raised; `node --check` clean; tests above pass
      (smoke test run locally or in CI)
- [ ] module doc in `lm/docs/` updated (Help drawer copy)

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **WebUI**: Plain JavaScript, no build; HPE Design System; No UUIDs in
    the UI; Behaviour; Controls; Feedback.
  - **Multitenancy**: Admin view equals tenant view.
  - **Code quality**: Documented in the code — a `//` comment on JS
    functions.
  - **Tests and docs**: Docs ship with the change.
- Every Python function (the hub route) has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
