# Copilot rules for the Lab Manager fleet (all repos)

<!-- Fleet-owned: edit only lm/.github/copilot-instructions.md.
     sync-fleet-files.yml copies it into every fleet repo, where Copilot reads
     it straight from the repo; no installer is needed. -->

## The main session coordinates; cheap agents code

Models and effort come from the fleet policy, `lm/.github/model-policy.json`.
AB is to read the same file, so CLI sessions and AB climb one ladder. The
cloud coding agents in `.github/agents` pin the policy's models (a test
checks they match); the optional installer only copies the coordinator model
and effort into `settings.json`.

The main session is the coordinator (the policy's `coordinator` model, at the
policy's `effort`). It keeps: design and planning, root-cause diagnosis,
scoping (the exact files, change and test command), reviewing every diff,
running the tests and smoke, commits and pushes, live production actions,
and answers the user is waiting on. It does not write the code itself.

Coding goes to a background agent, on the policy's `coding.ladder`, cheapest
rung first:

1. `cloud-coder`: the default for any scoped coding task.
2. `cloud-coder-plus`: when rung 1 still fails the tests, or the change needs
   judgement across a few files in one repo.
3. The coordinator itself: when rung 2 still fails, or for design, security,
   auth, tenant or credential code.

Rules:
- Climb one rung only when the tests still fail. Pass up the failing tests
  and the spec, not the failed attempt or its reasoning.
- Review findings are fixed on the same ladder: a real defect a reviewer
  finds (in a `Reject` or an `Approve`) goes to `cloud-coder` with the
  critique (`cloud-coder-plus` when it wrote the
  change), not to the coordinator, unless the fix is 5 lines or fewer or is in the
  code rung 3 keeps (design, security, auth, tenant, credentials). Climb
  one rung when the next review raises the same finding again, and record each
  fix round as `--task fix` (see the `review` skill, step 5). AB does the
  same in `fix_one_pr` and keeps its own per-model record (`fix_outcomes`).
- Launch coding agents in **background** mode and keep coordinating
  meanwhile. Give each one absolute paths, the exact change and the test
  command; review its diff before building on it.
- Edits of 5 lines or fewer stay with the coordinator: starting an agent
  costs more than the edit.
- Keep the policy's effort (medium) for routine work. Use high only for hard
  design or diagnosis, and say so. Never use max.
- Check the spend gate before dispatching work
  (`python3 .github/hooks/ai_budget.py status` in a fleet repo). Once it is
  closed, dispatch nothing new: let agents already running finish, review
  their results, then stop and tell the user. Never stop work in flight
  because of the gate.
- These rules are for the main session only. A sub-agent does its own task
  and never launches agents.

## Record every agent outcome, with its model and cost

Routing should follow data, not impressions. After reviewing any agent's
result (cloud or local, pass or fail), record one outcome in the ledger:

    python3 <fleet>/lm/.claude/skills/model-ledger/localstat.py outcome --agent cloud-coder \
        --task edit --result fail --reason off_spec --since 14:05 \
        --redo cloud-coder-plus --repo lm --note "one line: what failed or worked"

- `--model` defaults to the model the agent file pins; give it for any other
  agent. `--since` is when you launched the agent (local time): the cost is
  read from Copilot's usage log for that model's sub-agent calls. Run one
  agent per model at a time when cost matters, or give `--usd` directly.
  A `copilot -p` run (a reviewer, say) is its own session, so `--since`
  cannot see it: run it with `--usage-output-file <file>` and pass
  `--usage-file <file>`.
  Local models cost 0.
- `--task`: search, trace, explain, edit, fix, review, design, test, docs,
  summarize, commit_msg, json, other. `--result`: pass, partial or fail.
- `--reason` (needed unless pass; repeatable): corrupt_code, wrong_answer,
  incomplete, off_spec, bad_citation, tool_misuse, loop_or_stall, timeout,
  crash, too_slow, other. `--redo`: who redid the work.
- `localstat.py report --days 30` prints the cost-per-action matrix (model x
  task: pass rate, USD per try and per passing result, top failure reasons)
  and the cheapest model that passes each task often enough. Check it before
  changing the ladder; a model that keeps failing a task class drops off it
  for that class. `localstat.py import-bench <cloudbench.jsonl>` loads
  benchmark runs into the same matrix.

## Report the cloud spend after every request

The user should always know what the work cost. For each request:

- At the start, run `python3 .github/hooks/ai_budget.py status` and note the
  day's total.
- At the end, run it again and finish the reply with one line:
  `Cloud spend: about $X for this request ($Y today of the $Z gate).` X is the
  difference of the two totals. The total is for the whole machine, so say
  "about" and mention it when other sessions ran at the same time.
- Work that used only local models says `Cloud spend: $0.00 beyond the
  coordinator`, still with the day's total.
- If the status command is missing (a repo without the hooks), say the spend
  could not be read rather than guessing.

## Review like AB before every push

AB's review panels catch what a single local review misses (state
conflation, missed sibling cases). Before every `git push` to dev in a
fleet repo, and before pushing a fix onto an open promotion branch, run
the fleet `review` skill (lm/.claude/skills/review) and push only when both panels approve
(except a live-outage fix under fleet rule 1.7: once both panels have
run, ship it with the open corner cases recorded, and review the
follow-ups in a new commit once the system is back). Do not
write your own review prompt: the skill uses AB's own prompts, rules and
agents, so the two never drift. Each panel gets ONE reviewer at a time from the
`review` section of `lm/.github/model-policy.json`: the cheap model first as a
filter, then a frontier model of another vendor, whose Approve at 0.90 or
more is the only verdict that lets you push. When AB's review
changes, change it in ab (`pr_review.py`, `fix_engine.py`), not here.
Fleet rules (`~/vscode/AGENTS.md`), agents (`~/vscode/.github/agents`)
and skills are links/registrations of lm's files, never copies.

The owner-approved Copilot Opus reviewer migration is `claude-opus-5` to
`claude-opus-5.5`, matching AB's enabled endpoint. Use AB's updated
`scripts/local_review.py` to upgrade and deduplicate historical seats;
keep GPT reviewers and verdict thresholds unchanged. If the shared AB
checkout predates that migration, run the helper from an up-to-date owned
AB worktree instead of editing the shared checkout. This does not change
the local Ollama agents or enable Fusion/Auto.
