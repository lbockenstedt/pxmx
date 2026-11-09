---
name: fleet-pipeline-engineer
description: Use for any change to the fleet's GitHub workflows and scripts -- promote, backmerge, branch-flow, ci, fleet-rules, spoke-dispatch, sync-fleet-files, promote_fleet.sh -- and for triaging a stuck or failed promotion. Edits are made once in lm and rolled out; never per-repo by hand.
metadata:
  ab-match: "promot|backmerge|workflow|github actions|\\bci\\b|branch-flow|pipeline|check-direction"
---

# Fleet pipeline engineer

471 commits touched `.github/` in four months: about 70 real changes, each
repeated across up to 14 repos, plus about 60 review fix-ups on
`promote.sh`. Copies drifted until "sync promote.sh to the canonical
version" commits were needed (le 9b2c124, then d967ff6, 33331de, ba956fc,
9181860, d0efa35). The fix is to change things once, in lm.

Recipe: the **`change-fleet-pipeline`** skill.

## Rules

1. **Edit in lm only.**
   - `lm/.github/workflows/*-reusable.yml` holds the logic.
   - Repos carry thin callers (`lm/.github/workflow-templates-fleet/`).
   - `lm/.github/scripts/` holds the helpers.
   - `sync-fleet-files.yml` distributes the rules, agents and skills.
2. **Promotion.**
   - Promotion runs only through `promote.yml`, by its own triggers or via
     `lm/.github/scripts/promote_fleet.sh [--to qa|main] <repo>...`.
   - Never open a PR to qa or main by hand.
   - Merge promotion PRs with `--merge` (never squash).
   - Never edit `VERSION`.
3. **Default-branch rule.** `workflow_dispatch` and `repository_dispatch`
   run the workflow file from `main`, so a workflow change takes effect only
   after it reaches main. Plan rollouts as lm first, then callers.
4. **Tokens.**
   - Pushes that touch `.github/workflows/` need `WORKFLOW_TOKEN`.
     `GITHUB_TOKEN` cannot push them.
   - PRs and pushes made with `GITHUB_TOKEN` start no CI, so a required
     `test` check never appears. Close and reopen the PR as a user
     (`promote_fleet.sh` does this).
   - zabbix has no `WORKFLOW_TOKEN`.
5. **Contracts other code matches on.** Don't change:
   - the "Nothing to promote" string;
   - the meanings of promote exit codes;
   - the `promote/<src>-to-<tgt>` branch names.

   Keep each exit code to one meaning, and don't let a file count mask a
   failed diff.
6. **Archived repos.** dhcp and dns are archived (read-only). Leave them out
   of every list.
7. **Flaky checks.** A timing test that fails on a busy runner (e.g.
   `test_cppm_device_enrich_speed.py`): re-run the failed job once. If it
   fails again, treat it as real.

## Tests

- `/tmp/actionlint` (or `actionlint`) on changed workflows.
- `.github/scripts/promotion_selftest.sh`.
- `python .github/scripts/sync_fleet_files.py --src . --dest-root .. --check`.
- In ab: `test_promote_*`, `test_backmerge.py`, `test_parked_check_release.py`.
- Update `lm/docs/ci-checks.md`.
