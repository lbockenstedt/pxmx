---
name: review
description: >-
  Run AppBuilder's two-panel PR review (skeptical + state-logic) on a local
  change before pushing it, so a session catches what AB's panels would deny.
  For a local Copilot, Claude or other session: use it before every `git push`
  to dev in a fleet repo and before pushing a fix onto an open promotion
  branch. AB itself does not follow this skill; it runs the same panels on the
  PR. The prompts, fleet rules, specialist agents, reviewer models and verdict
  rule all come from AB's code, so this review changes when AB's does.
---

# Review before push

AB reviews every PR with two panels of reviewers from different providers:

- **Panel 1, skeptical review:** correctness, regressions, intent vs. diff, and the fleet rules.
- **Panel 2, state-logic review:** checks that no status conflates distinct states, and that every new line is reachable.

This skill runs those same prompts locally, from any tool. Never write your
own version of them: `ab/scripts/local_review.py` builds the prompts by
importing AB's `pr_review` and `fix_engine`.

Paths below use `<fleet>` for the folder holding the fleet checkouts (`lm`,
`ab`, ...), and `<dir>` for a scratch folder for this repo's review (for
example `<session folder>/files/review/<repo>`).

## Steps

1. Commit the change locally (do not push). Write the commit message as the
   PR intent: the problem, the fix, and the evidence for any contract the
   change relies on in another repo (reviewers cannot see other repos).
2. Build the prompts. The script needs a Python venv with ab's requirements
   (`python3 -m venv ~/.venvs/ab && ~/.venvs/ab/bin/pip install -r <fleet>/ab/requirements.txt`):

       rm -rf <dir> && ~/.venvs/ab/bin/python <fleet>/ab/scripts/local_review.py \
           <repo path> --base origin/<target> --out <dir>

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
3. Run one reviewer per `panel1` entry in `reviewers`, all in parallel, each
   on exactly that entry's `model`. Never substitute a different model.
   Panel 2 runs only after panel 1 Approves (step 4), as in AB, so a change
   panel 1 rejects does not pay for a second pass. The reviewer prompt is:

   > Read `<prompt>` and do exactly what it says. The change is in the
   > git repo at `<repo path>`; read any file there to check a claim before
   > rejecting on it. Reply with only the JSON object the prompt asks for.

   - **Copilot:** a `code-review` agent with `model` set to the reviewer model.
   - **Claude or any other tool:** run each reviewer with the Copilot CLI
     from `<repo path>`, which reaches every provider AB uses, and save its
     output as the reply, in the entry's `reply` file:

         copilot -p "<reviewer prompt>" --model <model> -s --add-dir <dir> \
             --allow-tool view --allow-tool grep --allow-tool glob \
             > <reply>

     Your own sub-agents may run a seat only if they run that exact model.
4. Save each reply unedited in its entry's `reply` file (the script creates
   an empty `<dir>/votes/` each round). A reviewer that failed gets no file
   (delete an empty one); never write a vote for it. Then apply AB's own
   verdict rule (`fix_engine.panel_verdict`):

       ~/.venvs/ab/bin/python <fleet>/ab/scripts/local_review.py --gate <dir>

   Exit 0 means both panels Approve. As in AB, a seat in `reviewers.json`
   with no reply, or a reply with no verdict, is a failed reviewer: the
   panel then needs every other vote to Approve at an average of 0.80 or
   more, else it prints `Retry`. A panel with no replies fails.
   - Panel 1 `Approve` and panel 2 `pending` (no panel 2 reply yet): now run
     the `panel2` entries (step 3) and gate again.
   - Panel 1 not `Approve`: panel 2 stays `skipped`; do not run it.
   - `Retry` is not a finding about the change: re-run only the failed
     reviewers (step 3) and gate again. It does not start a new round, and
     the change is not edited for it.
5. Read every `Reject` critique, even when the gate passed (a panel can
   Approve on majority with a Reject in it). Once the gate passes, fix only
   real defects; record non-blocking notes as follow-ups for the user rather
   than editing for them. For each `Reject` that is right:
   - Fix the whole class of defect, not just the line cited: search the change
     (and its twins) for every sibling instance.
   - Add a test that fails without the fix.
   - Amend the commit, then go back to step 2.
   - If a reject is wrong, add the evidence to the commit message or a code
     comment rather than ignoring it. AB's reviewers will raise it too.
6. Stop rule (the same as AB's remediation; see `ab/docs/ab.md`). Score each
   round as (panels that approved, lowest panel confidence) and keep the best.
   Keep going while rounds beat the best, but stop, do not push, and report
   the remaining findings to the user when either:
   - 2 rounds in a row did not beat the best, or
   - 5 rounds have run.
   A backtest of 766 AB-reviewed PRs found none that first fully approved
   after pass 5; later passes swung back and forth.

   If reviewers still fail after re-running them (`Retry`), stop and report that the reviewers could not
   run (model, auth or tool error), not a finding.
7. Push only once the gate passes.

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
- The gate is `fix_engine.panel_verdict`, which AB's `review_fix` also uses,
  so a change to AB's verdict rule reaches this skill automatically.
- This skill lives in lm with the other fleet skills and is synced to every
  repo's `.claude/skills/`, which both Copilot and Claude read. Edit it here
  only.
