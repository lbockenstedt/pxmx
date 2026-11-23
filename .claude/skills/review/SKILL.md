---
name: review
description: >-
  Run AppBuilder's two-panel PR review (skeptical + state-logic) on a local
  change before pushing it, so a session catches what AB's panels would deny.
  For a local Copilot, Claude or other session: use it before every `git push`
  to dev in a fleet repo and before pushing a fix onto an open promotion
  branch. AB itself does not follow this skill; it runs the same panels on the
  PR. The prompts, fleet rules and specialist agents come from AB's code; one
  reviewer per panel climbs the review ladder in .github/model-policy.json.
---

# Review before push

AB reviews every PR with two panels of reviewers from different providers:

- **Panel 1, skeptical review:** correctness, regressions, intent vs. diff, and the fleet rules.
- **Panel 2, state-logic review:** checks that no status conflates distinct states, and that every new line is reachable.

This skill runs those same prompts locally, from any tool, with one
reviewer per panel on a cost ladder instead of AB's seats. Never write your
own version of them: `ab/scripts/local_review.py` builds the prompts by
importing AB's `pr_review` and `fix_engine`.

Paths below use `<fleet>` for the folder holding the fleet checkouts (`lm`,
`ab`, ...), and `<dir>` for a scratch folder for this repo's review (for
example `<session folder>/files/review/<repo>`).

## Steps

1. Commit the change locally (do not push). Write the commit message as the
   PR intent: the problem, the fix, and the evidence for any contract the
   change relies on in another repo (reviewers cannot see other repos).
   If the change will go up as a PR, also write its title and a body that
   fills in `.github/pull_request_template.md` (fleet rule 2.4) to a file
   now, so the panels review the PR's real description.
2. Build the prompts. The script needs a Python venv with ab's requirements
   (`python3 -m venv ~/.venvs/ab && ~/.venvs/ab/bin/pip install -r <fleet>/ab/requirements.txt`):

       rm -rf <dir> && ~/.venvs/ab/bin/python <fleet>/ab/scripts/local_review.py \
           <repo path> --base origin/<target> --out <dir> \
           [--title "<PR title>" --body-file <PR body file>]

   Pass `--title` and `--body-file` for a PR (step 1); without them the
   last commit subject and the commit messages stand in for the
   description.

   `<target>` is the branch the PR will merge into, so the diff is the one AB
   reviews: `dev` for a normal change, `qa` on `promote/dev-to-qa`, `main` on
   `promote/qa-to-main` (and the base of any other PR branch). Removing
   `<dir>` first means a failed run leaves nothing stale to review, and the
   command is not piped, so its exit status is the script's:
   - 0: the last line is JSON with `reviewers` (one entry per seat on each
     of AB's latest panels: its `panel`, `model`, the `prompt` file to give
     it and the `reply` file its answer goes in) and `agents_error`.
   - 3: no change since the base. Commit the change (step 1) or fix
     `--base`.
   - 4: it could not tell which models AB reviews with. Stop and fix that;
     never review with a guessed panel.
   - Anything else: a broken venv, checkout or arguments; read stderr.
   - `agents_error` not null: AB's specialist checklists did not load, so
     panel 1 would be missing them. Stop and fix that.
3. Run **one reviewer per panel**, climbing the review ladder in
   `.github/model-policy.json` (`review`): the owner's rule is one reviewer
   at a time, not a panel of seats. Use the `prompt` file of that panel's
   entries in `reviewers` (`<dir>/panel1.txt` or `<dir>/panel2.txt`; every
   seat of a panel shares it) and ignore the seats' models. Panel 2 runs only after panel 1 Approves (step 4), as in
   AB, so a change panel 1 rejects does not pay for a second pass (a
   live-outage fix is the one exception, step 4). The reviewer prompt is:

   > Read `<prompt>` and do exactly what it says. The change is in the
   > git repo at `<repo path>`; read any file there to check a claim before
   > rejecting on it. Reply with only the JSON object the prompt asks for.

   Pick the model:
   - First the `review.ladder` model (cheap), as a filter. Fix its real
     findings (step 5) and re-run it until it approves, or until its Reject
     is shown wrong from the files.
   - Then the `review.frontier` model (`anthropic_author` when an Anthropic
     model wrote the code, else `other_author`). Its verdict is the panel's
     final verdict. A frontier Reject that is right sends the change back to
     step 5; after the fix, re-run the frontier model (the cheap pass is
     optional then).

   Every tool, Copilot included, runs the reviewer with the Copilot CLI
   from `<repo path>` and saves its output in `<dir>/<panel>-<model>.out`:

       copilot -p "<reviewer prompt>" --model <model> -s --add-dir <dir> \
           --allow-tool view --allow-tool grep --allow-tool glob \
           --usage-output-file <dir>/<panel>-<model>-<round>.usage.json \
           > <dir>/<panel>-<model>.out

   The usage file holds the run's cost. Record every run in the model ledger
   (the `model-ledger` skill), retries and runs with no verdict included,
   passing that file so the cost of every review, and of every miss, is
   known:

       python3 <fleet>/lm/.claude/skills/model-ledger/localstat.py outcome \
           --agent reviewer --model <model> --task review --repo <repo> \
           --usage-file <dir>/<panel>-<model>-<round>.usage.json \
           --result pass|fail [--reason ...] --note "<panel>: <verdict>, <why>"

   The result is whether the verdict was right, not whether it found
   something:
   - `pass`: a Reject whose finding blocks under fleet rule 1.9, or an
     Approve that held up (the frontier reviewer did not overturn it on a
     blocking finding).
   - `fail --reason wrong_answer`: a Reject only on a follow-up (rule 1.9:
     not a reason to Reject), a Reject on a claim the code disproves, or an
     Approve the frontier reviewer then rejected on a blocking finding.
     Record a cheap reviewer's run once the frontier verdict is in.
   - `fail --reason crash` (or `timeout` when it hit the time limit): a run
     with no verdict. It still cost money, so it is still recorded.

   The reviewer gets read-only tools and no shell: it reads and greps the
   code but does not run it. The tests are smoke.sh's job (fleet rule 1.5),
   so run smoke.sh before the review. Never run a reviewer as a
   `code-review` agent or another sub-agent: those re-run the tests and
   re-read every file each round. In NAC rounds 6 to 16 that cost about
   ten times as much a round as read-only reviewers.
4. Read the JSON verdict at the end of the reply. A panel passes only on
   the frontier reviewer's `Approve` with confidence of at least
   `review.pass_confidence` (0.90, AB's bar). A cheap reviewer's verdict
   never passes a panel, however confident. A reply with no verdict is a
   failed run, not a finding: run it once more on the same model. If the
   cheap reviewer fails again, skip the filter and go to the frontier
   reviewer.
   - Panel 1 passes: now run panel 2 (step 3), cheap model first.
   - Panel 1 does not pass: do not run panel 2. The one exception is a fix
     for a live outage (fleet rule 1.7), which needs both panels' findings:
     run panel 2 anyway and read it yourself.
   - `local_review.py --gate` still applies AB's seat-by-seat rule
     (`fix_engine.panel_verdict`) and so expects every seat's vote; it is
     not used with the ladder.
5. Read every `Reject` critique, and the critique of every `Approve` (it
   can carry findings). Fix only real defects, whether the panel rejected
   or approved; record non-blocking notes as follow-ups for the user rather
   than editing for them (fleet rule 1.9 says which findings block). For
   each real defect, in a `Reject` or in an `Approve`'s critique:
   - Fix the whole class of defect in one pass, not just the line cited
     (fleet rule 1.9): grep every call site in the change and its twins,
     write the state matrix, and fix every cell. A fix that needs a new
     mechanism gets a design review first.
   - The coder ladder writes the fix. The coordinator writes it only when
     it is 5 lines or fewer, when it is in design, security, auth, tenant
     or credential code (as for any coding task), or when
     `cloud-coder-plus` has already failed on the same finding (the
     ladder's last rung). Give the coding agent
     the critique, the state matrix, the files and the test command. Start
     at `cloud-coder-plus` when it wrote the change, else at `cloud-coder`
     (a change written by the coordinator, a person, a local agent or
     `cloud-coder`). Climb one rung when the next review raises the same
     finding again.
   - Add a test that fails without the fix.
   - A fix that fails the tests or smoke is recorded at once as `--task
     fix --result fail` (`corrupt_code`) and goes one rung up; it never
     reaches the review.
   - Otherwise record the fix round in the model ledger once the next
     review is in:
     `--task fix`, the model that wrote the fix, `--result pass` if that
     review no longer raises the finding and finds no new defect in the
     fix, else `fail` (`incomplete` when the same finding returns,
     `wrong_answer` when the fix is wrong or brings in a new defect). This
     shows which models can fix from reviewer feedback, and at what cost.
   - Amend the commit, then go back to step 2.
   - If a reject is wrong, add the evidence to the commit message or a code
     comment rather than ignoring it. AB's reviewers will raise it too.
   - On a fix for a live outage (fleet rule 1.7), read every critique from
     both panels, `Approve` ones included, and sort each concrete finding.
     A regression the fix brings in, a security hole or a cross-tenant leak
     is still fixed first. A right finding about a corner case the live
     data does not hit (check the live data; don't assume it) may wait:
     record it under "Known follow-ups" in the commit message and as a
     GitHub issue, then go to step 7.
6. Stop rule (the same as AB's remediation; see `ab/docs/ab.md`). Score each
   round as (panels that approved, lowest panel confidence) and keep the best.
   Keep going while rounds beat the best, but stop, do not push (unless step
   7's outage exception applies), and report the remaining findings to the
   user when either:
   - 2 rounds in a row did not beat the best, or
   - 5 rounds have run.
   A backtest of 766 AB-reviewed PRs found none that first fully approved
   after pass 5; later passes swung back and forth.

   If the frontier reviewer still fails after a re-run, stop and report that the reviewers could not
   run (model, auth or tool error), not a finding.
7. Push only once both panels pass. The one exception is a fix for a live
   outage (fleet rule 1.7). It may be pushed while a panel still fails
   when smoke.sh passes, both panels' reviewers gave a verdict (a failed
   run or a missing reply never qualifies), and every open finding is
   one that may wait under step 5. Promote it once CI passes, then run the
   live check on the outage and on what worked before the fix. Keep the fix
   only if a completed live check shows it helped and broke nothing that
   worked before; then fix the
   follow-ups in a new commit and review that from step 1 in normal rounds,
   and whatever is still broken remains an outage. If it did not help,
   it broke something that worked before, or the live check failed, was
   incomplete or could not run, revert it at once (fleet rule
   3.9) unless the owner directs another mitigation. The revert undoes only
   that fix and needs only smoke.sh and CI: what it brings back was
   reviewed, or is an earlier outage fix kept after its live check.

   **Let AB reuse this review (dev PRs only).** Once both frontier replies
   Approve at 0.90 or more on a clean, committed head reviewed with the PR
   title and template body (steps 1 and 2), run

       ~/.venvs/ab/bin/python <fleet>/ab/scripts/local_review.py \
           --attest <dir> --frontier <frontier model>

   It writes `<dir>/pr-title.txt` and `<dir>/pr-body.md` (the reviewed
   title and body plus an `ab-local-review` marker; exit 1 writes none).
   Open the PR with exactly those, unedited:

       gh pr create --base dev --title "$(cat <dir>/pr-title.txt)" \
           --body-file <dir>/pr-body.md

   AB then skips its paid panels for that head. Any new push, rebase,
   retarget or title/body edit voids the marker and AB reviews as usual.
   A direct push to dev (no PR) needs none of this.

## Known differences from AB

AB's panel 1 also gets two hints that read the pushed head on GitHub, so
they cannot run before a push: where getattr'd names are defined
(`attr_definition_lookup`) and the names ruff verified as defined
(`check_undefined_names_verified`). Both only help a reviewer approve, so
the local panel can be stricter than AB, never more lenient.

## Keeping this in step with AB

- Change the review prompts only in `ab/pr_review.py`
  (`skeptical_issue_body`, `state_logic_issue_body`) and `ab/fix_engine.py`
  (`reviewer_prompt`). This skill and AB both pick the change up.
- Change the fleet rules only in `lm/.github/fleet-rules.md`.
- The reviewer models and the climb rule come from the `review` section of
  `lm/.github/model-policy.json`; change them there. AB's own panels still
  seat several reviewers (`fix_engine.panel_verdict`) until AB reads it too.
- This skill lives in lm with the other fleet skills and is synced to every
  repo's `.claude/skills/`, which both Copilot and Claude read. Edit it here
  only.
