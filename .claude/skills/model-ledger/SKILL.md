---
name: model-ledger
description: Use after reviewing any agent's or reviewer's result (local or cloud, pass or fail) to record it in the model ledger with its cost, and when choosing which model to send a kind of work to (the cost-per-action report). Covers localstat.py outcome / report / import-bench and how to capture the cost of a copilot -p run.
---

# Model ledger

Record every agent and reviewer outcome (pass, partial or fail) with its
model, task type and cost. Routing is then decided from data: which model
passes which kind of work, and what each pass really costs once the failed
tries are counted. Local models are included; they cost 0.

The script is `localstat.py` in this folder. Its data is in
`~/.copilot/local-stats/outcomes.jsonl` (set `LOCAL_STATS_DIR` to move it).
The local-model proxy also writes `calls-YYYY-MM.jsonl` there by itself.

## Record an outcome

Record it right after you review the result, passes included: the pass rate
is the point.

```bash
python3 <fleet>/lm/.claude/skills/model-ledger/localstat.py outcome --agent cloud-coder \
    --task edit --result fail --reason off_spec --since 14:05 \
    --redo cloud-coder-plus --repo lm --note "one line: what failed or worked"
```

- `--model` defaults to the model the agent file pins: the repo's
  `.github/agents/<agent>.agent.md` first, then `~/.copilot/agents/`. Give it
  for any other agent.
- The cost comes from one of:
  - `--since HH:MM`: when you launched the sub-agent (local time). The cost
    is read from Copilot's usage log for that model's sub-agent calls, so run
    one agent per model at a time when cost matters.
  - `--usage-file F`: the JSON from `copilot -p --usage-output-file F`. Use
    it for every `copilot -p` run, a reviewer for example: that run is its
    own session, so `--since` cannot see it.
  - `--usd N`, when you know the cost.
- `--task`: search, trace, explain, edit, fix, review, design, test, docs,
  summarize, commit_msg, json, other.
- `--result`: pass, partial, fail. `--reason` is needed unless the result is
  pass, and can be repeated: corrupt_code, wrong_answer, incomplete,
  off_spec, bad_citation, tool_misuse, loop_or_stall, timeout, crash,
  too_slow, other.
- `--redo`: who redid the work (cloud, local, an agent name, or none).
- Reviewers: record each reviewer run with `--agent reviewer --task review`
  and its usage file. A cheap reviewer's Approve that the frontier reviewer
  then rejects on a real defect is a `fail` with `--reason wrong_answer`.

## Choose a model

`localstat.py report --days 30` shows, per task type and model:
- the pass rate;
- USD per try;
- USD per passing result, failed tries included;
- the top failure reasons;
- the cheapest model that passes often enough.

Read it before changing `coding.ladder` in `.github/model-policy.json`.
`localstat.py import-bench FILE` adds benchmark runs (cloudbench.jsonl) to the
same matrix.

## Fleet rules

This skill is fleet-owned: edit it only in lm (fleet rule 2.7). The sync
copies it to every repo.
