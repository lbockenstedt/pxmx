---
name: ab-maintainer
description: Use for AppBuilder (ab repo) internals -- the LLM router/providers and model registry, PR review and remediation, fix engine, feature builds, log scan/health checks, fleet_checks rules, and AB's loading of fleet rules, agents and skills.
metadata:
  ab-match: "appbuilder|\\bab\\b|\\bllm\\b|model registry|provider|fleet_checks|review panel|fix engine|remediat"
---

# AB maintainer

AB is the fleet's automated fixer, reviewer and builder. It had 642 commits
in four months; the LLM area alone had 190, 62 of them fixes. Typical
breaks:
- an API key sent to the browser (ab 2bba55c);
- a provider parameter with the wrong name (`max_tokens` vs
  `max_completion_tokens`);
- blocking PyGithub calls that froze the app (ab 1d89642);
- reverts (ab c8a1800).

Recipes:
- **`ab-add-llm-provider`** for an LLM provider or model;
- **`ab-add-fleet-rule`** to turn a recurring rework class into a
  deterministic check.

## Rules specific to AB

1. **Models.** Use only frontier models from the curated list, picked
   automatically from the configured list. Ollama can be selected but is
   never picked automatically. Never lower the 0.90 confidence bar.
2. **Guardrails.**
   - No remediation loops without a change in the results.
   - Every feature gets a config knob.
   - Work submitted through chat passes the same guardrails as PRs.
3. **Keys.** Keys never reach the browser; routes return a `*_set` flag.
4. **Don't block.** PyGithub and other sync I/O run off the event loop
   (`asyncio.to_thread`), with timeouts.
5. **Fleet knowledge.**
   - `fleet_checks.rules_text()` reads `.github/fleet-rules.md`, the copy
     synced from lm.
   - `skills_loader` loads lm's `.claude/skills` and `.github/agents`.
   - `agents_context()` injects agents whose `metadata.ab-match` regex
     matches the issue or PR.
   - Don't fork these rules into AB-only files.
6. **New checks.** A new `fleet_checks` rule is calibrated against at least
   450 recent dev commits before it becomes error-level. A noisy rule at
   error level blocks every repo.
7. **Tests.** AB's UI tests click every button except the destructive
   denylist.

## Tests

- Full suite: `AB_DISABLE_WORKERS=1 python -m pytest -q -p no:warnings`
  (about 2 minutes).
- A single file: `AB_DISABLE_WORKERS=1 python -m pytest -q test_<x>.py`.
- Update `docs/ab.md`.
