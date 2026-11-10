---
name: change-fleet-pipeline
description: >-
  Use when changing the fleet CI/CD pipeline: promotion (promote.sh,
  promote-reusable.yml, promote.yml callers, promote_fleet.sh), back-merge,
  VERSION bumping (bump_version.py), branch-flow / check-direction, the
  fleet-rules workflow (fleet-rules-reusable.yml, ruff_new.py,
  check_twins.py, twins.json), spoke-dispatch, or any repo's ci.yml. Covers
  where to edit (lm only, for fleet-owned files), how the change reaches the
  other 14 repos, the strings other scripts match on, the selftest, and how
  to promote safely. Not for application code or AB fleet_checks.py rules
  (ab-add-fleet-rule).
---

# Change the fleet pipeline (CI, promotion, fleet checks)

Every repo runs the same promotion scripts, so a pipeline change has the
whole fleet as its blast radius. Pipeline commits were the most-reworked
class in the 120-day history. le alone needed five fixes in a month after
the one-unit-per-PR split (le `9b2c124`):
- `d967ff6`: nfiles masked a failed diff;
- `33331de`: four different "no intent" states were conflated;
- `c452cc6`: only the first parked run was released;
- `d0efa35`: le's copy had drifted from the canonical one;
- `85409c7`: a renamed log string broke every selftest.

## When to use / when not to use

Use it when you edit:
- anything under `.github/` in any fleet repo: `workflows/`,
  `scripts/promote*.sh`, `bump_version.py`, `check_twins.py`,
  `ruff_new.py` or `twins.json`;
- the PR template;
- `docs/ci-checks.md`.

Do not use it for:
- a new automated rule inside `ab/fleet_checks.py` or `ab/health_check.py`
  (`ab-add-fleet-rule`);
- keeping application twins in step (`dual-copy-guard`).

## Where the logic lives (lm `.github/`)

| File | Role |
| :--- | :--- |
| `workflows/promote-reusable.yml` | All promotion logic: VERSION pin + advance, `PROMOTE_SPLIT=1` unit split, PR intent body, parked-run release. Runs the **caller repo's** `.github/scripts/promote.sh`. |
| `workflows/backmerge-reusable.yml` | Reverse direction (main→qa→dev), same `promote.sh`. |
| `workflow-templates-fleet/{promote,backmerge}.yml` | Thin callers copied into every repo: triggers + permissions only. |
| `workflows/fleet-rules-reusable.yml` + `fleet-rules.yml` | Checks out ab, runs `fleet_checks.py`, `ruff_new.py`, `check_twins.py`. lm calls it by path; the other repos call it `@main`. |
| `workflows/branch-flow.yml` | `check-direction` (dev→qa→main only). |
| `workflows/ci.yml` | `node --check` on WebUI JS, then one pytest per component. |
| `workflows/spoke-dispatch.yml` | Asks every spoke to re-run CI against a new lm/core. Report only. |
| `scripts/promote.sh`, `promotion_selftest.sh`, `bump_version.py` | Byte-identical copies in every repo. |
| `scripts/promote_fleet.sh` | Operator tool: dispatches `promote.yml` and merges the `promote/<src>-to-<tgt>` PRs. Never creates a PR. |
| `scripts/check_twins.py` + `twins.json` | Twin-file check: in-repo pairs fail, cross-repo pairs warn. |

## Steps (in order)

1. **Edit in lm only.** Make the change in `lm/.github/`. Copies in other
   repos are overwritten by the fleet-file sync (Git, branches and promotion
   → "Fleet-owned files are edited only in lm"). Never fix a spoke's copy
   in place; that is how le drifted (`d0efa35`).
2. **Put logic in the reusable workflow, not the caller.** A caller keeps
   only `on:` triggers, `permissions:` and `uses:`. Never add `dev` or
   `main` to the promote caller's push trigger: promotion is deliberate.
3. **Keep the matched strings.** Other scripts grep the output:
   - `promotion_selftest.sh` greps `promote.sh`'s no-op line for the literal
     `Nothing to promote` (forward direction);
   - workflows read `changed=` from `$GITHUB_OUTPUT`.

   Before renaming any message or output, grep for it in both workflows and
   scripts.
4. **Fail closed.** For every shell pipeline:
   - check the exit status before using a count or list derived from it;
   - keep "unknown" separate from "none" (no PR body vs. a body with no
     intent vs. an unreadable PR vs. an API failure);
   - make every loop over runs or PRs handle all items, not just the first;
   - print `::warning::` with the manual remedy when the state is unknown.
5. **VERSION.** Only `bump_version.py` and the promotion job write
   `VERSION`. A promotion diff legitimately advances the *target's* own
   counter by one step.
6. **Docs.** Update the file's header comment (it is the spec) and
   `docs/ci-checks.md`.
7. **Commit to lm dev.** For reusable workflows, spokes call
   `fleet-rules-reusable.yml@main`, so the change reaches them only after
   lm is promoted to main. Copied scripts reach each repo through the sync;
   then promote each repo normally.
8. **Promote** only with `promote.yml` or
   `lm/.github/scripts/promote_fleet.sh [--to qa|main] <repo>...`.
   - If no promote PR appears, investigate.
   - Never open a dev→qa or qa→main PR by hand, and never push to qa or
     main: that bypasses VERSION pinning and intent carry-over.

## Tests

```bash
cd lm && bash .github/scripts/promotion_selftest.sh                       # promote.sh / bump_version.py changes; must print 0 failed
cd lm && python3 .github/scripts/check_twins.py --repo lm --branch dev    # twins.json / check_twins.py changes
cd lm && for r in ab cs le nw truenas netbox opnsense pxmx cppm ldap kvm qa tsa zabbix; do git -C ../$r fetch -q origin; diff -q <(git show origin/dev:.github/scripts/promote.sh) <(git -C ../$r show origin/dev:.github/scripts/promote.sh) >/dev/null || echo "DRIFT $r"; done
cd ab && AB_DISABLE_WORKERS=1 /tmp/nbvenv/bin/python -m pytest -q test_fleet_checks.py   # if the fleet-rules workflow's call into ab changed
```
For a workflow change, validate the YAML (for example with
`python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' <file>`).
Then watch the first real run with `gh run list -R lbockenstedt/<repo>
--workflow <name>` before rolling out to the next repo.

## Common misses

- **Renamed a string a selftest matches.** "Nothing to promote" became
  "Nothing to carry"; nw CI went red on three branches — le `85409c7`.
- **A failed command read as an empty result.** `nfiles` turned a failed
  diff into "0 files" — le `d967ff6`.
- **Different "unknown" states merged into one.** — le `33331de`.
- **Loop stops at the first item.** Only the first parked workflow run was
  approved, so the PR stayed BLOCKED — le `c452cc6`.
- **A copy edited in place drifts from the canonical file.** — le
  `d0efa35`. cs `0405c6f` fixed review findings in its own `promote.sh`
  copy.
- **Hand-made PRs to qa or main.** An earlier helper opened direct dev→qa
  PRs that skipped VERSION pinning; `promote_fleet.sh` now refuses to
  (see its header).

## Done when

- [ ] edited only in `lm/.github/` (or `ci.yml` of the one repo it belongs
      to)
- [ ] logic in the reusable workflow; callers are triggers/permissions only
- [ ] no matched string or output name changed (or every matcher updated)
- [ ] every shell step fails closed; unknown ≠ none; loops cover all items
- [ ] selftest / check_twins pass; copies show no drift
- [ ] header comment and `docs/ci-checks.md` updated
- [ ] rolled out through the sync + `promote.yml` / `promote_fleet.sh`
      only, with the first real run checked

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **Git, branches and promotion**: dev → qa → main; VERSION is
    bot-owned; Fleet-owned files are edited only in lm; Every PR states its
    intent.
  - **Code quality**: Twins stay in step.
  - **Tests and docs**: Docs ship with the change.
  - The **Enforcement map** lists which workflow enforces which rule.
- Every Python function (`check_twins.py`, `ruff_new.py`,
  `bump_version.py`) has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
