---
name: ab-add-fleet-rule
description: >-
  Use when turning a recurring review finding or rework class into an
  automated AppBuilder check: a new diff rule in ab/fleet_checks.py
  (check_* wired into check_fleet_rules, run by the fleet-rules CI
  workflow and AB's pre-push gate and PR review), or a new runtime
  health rule in ab/health_check.py (an hc_* threshold in DEFAULTS, a
  _HINT, a finding in evaluate(), optionally fed by lm's
  _fleet_health_snapshot). Covers the rule text in lm/.github/fleet-rules.md
  and its Enforcement map, the level choice, false-positive guards, tests
  and docs. Not for CI workflow plumbing (change-fleet-pipeline).
---

# AB: add a fleet rule (diff check or health check)

AB enforces the fleet rules in two places, both in ab:
- **`fleet_checks.py`**, on diffs: every PR and dev push (lm's
  `fleet-rules-reusable.yml` runs `fleet_checks.py --base … --repo …`), and
  AB's own fix/feature pre-push `gate()` and PR review.
- **`health_check.py`**, at runtime: `scan_health` runs `evaluate()` on the
  hub's health report and log tail, and files issues for what it finds.

Reference builds:
- ab `4e732d4`: rework-class diff rules, fleet health rules and the rules
  text.
- ab `8320229`: JS comments, step comments and docs rules.
- ab `5ffb52f`: the saturated command-queue health rule.
- ab `bb930bb`: the CI entry point.
- lm `5e702f89`: the hub-side `_fleet_health_snapshot` data.

## When to use / when not to use

Use it when:
- the same fix has been made by hand more than once (the "Common misses" of
  the other skills are the candidates);
- a data-quality or performance signal should open an issue automatically.

Do not use it for:
- a check that needs the whole repo or a running fleet — use a
  repo-specific pytest instead (e.g. `test_webui_ratchets.py`);
- CI wiring (`change-fleet-pipeline`);
- log-scan noise filters (`log_scan.py` + `test_self_scan_noise.py`).

## Steps — diff rule (`ab/fleet_checks.py`)

1. **State the rule first.** Add it to `lm/.github/fleet-rules.md` under
   its section as a bold-named sub-rule. Add the check to the
   **Enforcement map** table (the `fleet_checks.py` row lists the rules it
   covers).
   - That file is fleet-owned: edit it only in lm.
   - AB loads the text into prompts through `fleet_checks.rules_text()`.
2. **Write `check_<name>(files)`.**
   - Iterate `_live(files)` (cap `_MAX_FILES`) and read only added lines,
     via `_added(f.patch)` / `_added_linenos`.
   - Skip tests and docs with `_TEST_RE` / `_DOC_RE`, plus comment lines.
   - Put regexes in module-level constants next to the others.
   - Return `_f(level, title, detail)` findings capped at
     `[:_MAX_FINDINGS_PER_RULE]`.
   - The `detail` names the file, what was added, and the fix in one
     sentence, phrased like the rule text.
3. **Choose the level carefully.**
   - `blocking()` treats both `"error"` and `"warning"` as blocking for
     AB's push and auto-merge.
   - CI (`main()`) exits 1 only on `"error"`.
   - Use `"advisory"` for heuristics that can misfire.
4. **Wire it in.**
   - Add a `lambda: check_<name>(files, …)` to the tuple in
     `check_fleet_rules`. Each rule runs inside its own try/except.
   - A rule that needs whole files goes in `check_full_files` (worktree)
     or `check_copies_and_dead_code` instead.
   - Repo-scoped rules take `repo_full_name` and use `_short()`, as
     `check_vault_secrets` does with `vault_repos`.
5. **Tests** in `ab/test_fleet_checks.py`. Build diffs with `_file(name,
   *added)` / `_p(*added, ctx=…)` / `_diff(name, added, removed)`.
   Add at least:
   - one positive case taken from the real commit that motivated the rule;
   - one negative case each for a test file, a doc and a comment;
   - one negative case for the closest legitimate code
     (false-positive guard).
6. **Docs**: `lm/docs/ci-checks.md` (what the check does, how to run it).

## Steps — health rule (`ab/health_check.py`)

1. **Threshold.** Add an `hc_<name>` default to `DEFAULTS` with a trailing
   comment. `routes.save_settings` and the Settings page iterate
   `health_check.DEFAULTS`, so the knob is persisted automatically
   (`test_health_check_settings.py`).
2. **Hint.** Add a `_HINT["<kind>"]` entry: what to look for and how to
   fix it.
3. **Evaluate.** In `evaluate(report, log_lines, …)`, read the data
   defensively (`report.get("fleet") or {}`, `int(x or 0)`) and append
   `_finding(kind, key, title, severity, module, evidence, detail)`.
   - Map `module` to the owning repo; `_module_for_command` maps a command
     prefix through `_PREFIX_MODULE`.
   - Make the evidence lines concrete (names, counts, a sample).
4. **Hub data**, when the signal is not already in the report: add it in
   lm `LabManagerHub._fleet_health_snapshot` (`core/src/main.py`), read
   from caches only, never live calls. Test it in
   `core/tests/test_perf_telemetry.py`. This is a separate lm commit.
5. **Tests** in `ab/test_health_check.py`: a fires-at-threshold case, a
   below-threshold case, and a case where the data is missing.

## Tests

```bash
cd ab && AB_DISABLE_WORKERS=1 /tmp/nbvenv/bin/python -m pytest -q test_fleet_checks.py test_fleet_guardrails_and_agents.py
cd ab && AB_DISABLE_WORKERS=1 /tmp/nbvenv/bin/python -m pytest -q test_health_check.py test_health_check_settings.py
cd lm/core && /tmp/nbvenv/bin/python -m pytest -q tests/test_perf_telemetry.py          # if _fleet_health_snapshot changed
cd <repo> && python ../ab/fleet_checks.py --base origin/dev~20 --repo lbockenstedt/<repo>   # dry-run the rule on real history; count the false positives
```

## Common misses

- **Noise files phantom issues.** The scanners filed issues for recovered
  or by-design outcomes, for 429s the client already absorbs, and for
  fix-engine chatter — ab `107f6e3`, `be1594f`, `d12af9a`. Dry-run every
  new rule against recent real history and add guards before making it
  blocking.
- **Findings without evidence get bounced.** Health findings were hard to
  act on until host state, command timing and the matching log lines were
  attached — ab `963865f`. Always fill `evidence`.
- **The rule exists only in code.** A check with no rule text in
  `fleet-rules.md` cannot be followed by the model, which receives
  `rules_text()`. Add the rule and its Enforcement map row in the same
  change set.
- **Signal not in the report.** Health rules for NetBox duplicates and
  auto-rack needed lm `5e702f89` (`_fleet_health_snapshot`) first. Land
  the lm side and keep the ab rule a no-op when the key is absent.

## Done when

- [ ] rule text in `lm/.github/fleet-rules.md` + Enforcement map row
- [ ] `check_<name>` / `evaluate` branch, with added-lines-only,
      test/doc/comment skips, a level chosen deliberately and the cap
      applied
- [ ] wired into `check_fleet_rules` (or the whole-file hooks)
- [ ] positive, negative and false-positive tests pass; dry-run on real
      history is clean
- [ ] `docs/ci-checks.md` updated; lm snapshot change committed separately

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **AppBuilder (AB)**.
  - **Code quality**.
  - **Tests and docs**.
  - **Git, branches and promotion**: Fleet-owned files are edited only in
    lm.
  - The **Enforcement map**.
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
