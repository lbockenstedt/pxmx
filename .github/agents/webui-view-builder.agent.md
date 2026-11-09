---
name: webui-view-builder
description: Use when adding or reworking a WebUI page, tab, tile or form in lm/WebUI (or a spoke WebUI) -- ES-module views with safeHtml/createView/delegate, tenant-scoped data, HPE design tokens, tooltips, cache-first rendering. For a single button or field the add-webui-control skill is enough.
metadata:
  ab-match: "webui|\\bpage\\b|\\btab\\b|\\bview\\b|\\btile\\b|tooltip|\\bui\\b|dashboard"
---

# WebUI view builder

The WebUI is plain JavaScript with no build step, so one ReferenceError or
SyntaxError blanks the whole UI (lm 6958d5ee, b450e449, 743fe9df). About
1,900 commits touched it, including 166 "fix/stop/restore" fixes.

Recipes:
- **`add-webui-view`** for a page or tab;
- **`add-webui-control`** for a single control;
- **`add-vault-secret-field`** for a credential field.

## Rules

1. **Markup and events.** Build markup with `safeHtml`/`rawHtml` and wire
   events with `data-action` + `delegate()`. No new `escapeHtml(`, inline
   `on*=` or `window.x =` globals; `test_webui_ratchets.py` fails on any
   increase.
2. **Stale renders.** A view created with `createView` ignores async
   results that arrive after the user has navigated away. Leaving a tab and
   coming back resets it.
3. **Data.**
   - Data comes only from the hub API, tenant-scoped (`_tenantQS()`).
   - Render from the cache; show a "cached" badge only when the data is
     more than 5 minutes old.
   - Never show "update running, please wait".
4. **No UUIDs on screen.** Show names. `webui_walk.py` fails on visible
   UUIDs.
5. **HPE Design System.** Use the tokens in `docs/webui-style.md` and copy
   an existing page's layout.
   - Save goes on the right; disable it while a precondition is missing,
     and confirm the write succeeded.
   - Use toasts, not pop-ups, for routine actions.
   - Table text doesn't wrap.
   - Use dropdowns filled from the source system, not free text, for known
     values.
6. **Tooltips** (`title=`) explain controls, in place of banners. Keep them
   in step with the docs.
7. **Refresh.** Auto-refresh only on data pages, never on config or form
   pages.
8. **Twins.** If the view has a spoke-side copy (e.g. `sim-views.js`),
   change both.

## Tests

- `node --check` every changed JS file.
- `cd core && python -m pytest -q tests/test_webui_ratchets.py tests/test_webui_browser_smoke.py`.
- Update `docs/<module>.md` and `docs/webui.md` if you add a pattern.
