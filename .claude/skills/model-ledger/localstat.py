#!/usr/bin/env python3
"""Model statistics, local and cloud: record task outcomes and their cost, and
report which model is worth sending which kind of work to.

Two JSON Lines logs live in ~/.copilot/local-stats (LOCAL_STATS_DIR):
- calls-YYYY-MM.jsonl: written by ~/.copilot/ollama-proxy/proxy.py, one line per
  chat call to a local model (agent, prompt size, speed, tool calls, errors and
  corruption signals). Automatic.
- outcomes.jsonl: written by `localstat.py outcome` after the coordinator has
  reviewed an agent's result (any model, local or cloud). Only the reviewer
  knows if the work was right. Each record carries the model and the cost.

Cost, in USD (local models cost 0):
- --usd: given directly;
- --usage-file: a `copilot -p --usage-output-file` JSON (its totalNanoAiu);
- --since: a sub-agent of the session's own: the sum of the usage-log calls on
  --model made by sub-agents since that time (two sub-agents on the same model
  at once are counted together, so run one at a time when it matters).

Usage:
  localstat.py outcome --agent cloud-coder --task edit --result fail \
      --reason off_spec --since 18:20 --note "ignored the test command" [--redo cloud]
  localstat.py report [--days 30]        reports, plus the cost-per-action matrix
  localstat.py import-bench FILE         load cloudbench.jsonl benchmark results
Stdlib only.
"""
import argparse
import glob
import json
import os
import importlib.util
import re
import statistics
import time
from collections import Counter, defaultdict

DIR = os.path.expanduser(os.environ.get("LOCAL_STATS_DIR", "~/.copilot/local-stats"))
OUTCOMES = os.path.join(DIR, "outcomes.jsonl")
COPILOT = os.path.expanduser(os.environ.get("COPILOT_HOME", "~/.copilot"))
# The repo this script is in (<repo>/.claude/skills/model-ledger/localstat.py): its
# .github/agents and .github/hooks are found before personal copies in COPILOT.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# Known agents; any other name is accepted too (a new agent needs no code change).
AGENTS = ["local-coder", "local-scout", "local-summarizer", "local-docs", "local-big",
          "cloud-coder", "cloud-coder-plus", "coordinator", "direct"]
TASKS = ["search", "trace", "explain", "edit", "fix", "review", "design", "test", "docs",
         "summarize", "commit_msg", "json", "other"]
RESULTS = ["pass", "partial", "fail"]
REASONS = ["corrupt_code", "wrong_answer", "incomplete", "off_spec", "bad_citation", "tool_misuse",
           "loop_or_stall", "timeout", "crash", "too_slow", "other"]
READY, WEAK, MIN_N = 0.90, 0.60, 5  # pass-rate bars for the offload advice, and samples needed


def read_jsonl(paths, since):
    """Yield records from JSON Lines files whose ts is on or after `since` (ISO string)."""
    for p in paths:
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", "") >= since:
                    yield r


def agent_model(agent):
    """The model an agent file pins (``model:`` line): the repo's .github/agents first, then personal agents; or ""."""
    for path in (os.path.join(REPO, ".github", "agents", agent + ".agent.md"),
                 os.path.join(COPILOT, "agents", agent + ".agent.md")):
        try:
            with open(path) as f:
                m = re.search(r"(?m)^model:\s*(\S+)", f.read())
        except OSError:
            continue
        if m:
            return m.group(1)
    return ""


def since_utc(text):
    """A local HH:MM (today) or ISO time as UTC in the usage log's format."""
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        text = time.strftime("%Y-%m-%dT") + text.zfill(5)
    t = time.mktime(time.strptime(text[:16], "%Y-%m-%dT%H:%M"))
    # A future start would silently record $0; usually a typo or yesterday's time.
    if t > time.time():
        raise SystemExit("--since %s is in the future" % text)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))


def subagent_usd(model, since):
    """USD the usage log shows for sub-agent calls on ``model`` since ``since``.

    The usage log is read through the spend gate's ``usage_log()`` (the
    repo's copy, else the user-wide install), so only one script opens Copilot's database."""
    # Load the spend gate's script, which knows where Copilot's usage log is: the
    # repo's copy is tried first, then the user-wide install.
    gate = next((p for p in (os.path.join(REPO, ".github", "hooks", "ai_budget.py"),
                             os.path.join(COPILOT, "ai-budget", "hooks", "ai_budget.py"))
                 if os.path.isfile(p)), None)
    if gate is None:
        return None
    spec = importlib.util.spec_from_file_location("ai_budget", gate)
    ai_budget = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ai_budget)
    # Sum this model's sub-agent calls since the start time.
    con = ai_budget.usage_log()
    if con is None:
        return None
    try:
        row = con.execute("SELECT SUM(total_nano_aiu) FROM assistant_usage_events WHERE agent_id IS NOT NULL "
                          "AND (model = ? OR copilot_usage_model = ?) AND replace(created_at, ' ', 'T') >= ?",
                          (model.split("/")[-1], model.split("/")[-1], since_utc(since))).fetchone()
    finally:
        con.close()
    return (row[0] or 0) / 1e11


def outcome_cost(a, model):
    """The outcome's cost in USD from --usd, --usage-file or --since; None if unknown."""
    if a.usd is not None:
        return a.usd
    if model.startswith("ollama/") or model.startswith("llamacpp/"):
        return 0.0
    if a.usage_file:
        with open(a.usage_file) as f:
            return (json.load(f).get("totalNanoAiu") or 0) / 1e11
    if a.since:
        return subagent_usd(model, a.since)
    return None


def cmd_outcome(a):
    """Append one reviewed outcome, with its model and cost."""
    if a.result != "pass" and not a.reason:
        raise SystemExit("--reason is required unless --result pass")
    # The model: given, else the one the agent file pins.
    model = a.model or agent_model(a.agent)
    if not model:
        raise SystemExit("--model is required: no installed agent file names one for %s" % a.agent)
    usd = outcome_cost(a, model)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": a.agent, "model": model, "task": a.task,
           "result": a.result, "reasons": a.reason or [], "redo": a.redo, "minutes": a.minutes,
           "usd": None if usd is None else round(usd, 4), "repo": a.repo, "note": a.note,
           "source": a.source}
    os.makedirs(DIR, exist_ok=True)
    with open(OUTCOMES, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("recorded:", json.dumps(rec))


def cmd_import_bench(a):
    """Append cloudbench.jsonl fix and review results as outcomes (source=benchmark).

    A row already imported (same benchmark time, model and task) is skipped, so
    re-running the import is safe."""
    seen = set()
    if os.path.exists(OUTCOMES):
        for o in read_jsonl([OUTCOMES], ""):
            if o.get("source") == "benchmark":
                seen.add((o.get("bench_id"), o.get("model")))
    added = 0
    os.makedirs(DIR, exist_ok=True)
    with open(a.file) as f, open(OUTCOMES, "a") as out:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            # Only graded fix and review runs count; screens and smoke runs do not.
            if d.get("stage") == "fix" and "ok" in d:
                task, ok = "fix", d["ok"]
            elif d.get("stage") == "review" and "right" in d:
                task, ok = "review", d["right"]
            else:
                continue
            bench_id = "%s:%s:%s:%s" % (os.path.basename(os.path.dirname(os.path.abspath(a.file))),
                                       d.get("at"), d.get("task"), d.get("rep", 0))
            if (bench_id, d.get("model")) in seen:
                continue
            reason = [] if ok else (["wrong_answer"] if task == "review" else ["incomplete"])
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": "bench", "model": d.get("model"),
                   "task": task, "result": "pass" if ok else "fail", "reasons": reason, "redo": "none",
                   "minutes": round((d.get("secs") or 0) / 60, 1), "usd": d.get("usd"), "repo": "",
                   "note": "%s %s%s" % (d.get("task"), d.get("verdict") or "", " want " + d["want"] if "want" in d else ""),
                   "source": "benchmark", "bench_id": bench_id}
            out.write(json.dumps(rec) + "\n")
            added += 1
    print("imported %d benchmark outcomes" % added)


def pct(n, d):
    """Format n/d as a whole percentage, or '-' when d is 0."""
    return f"{100 * n / d:.0f}%" if d else "-"


def med(xs):
    """Median of the non-None values, or None."""
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def table(title, header, rows):
    """Print a simple aligned table."""
    print(f"\n## {title}")
    if not rows:
        print("(no data)")
        return
    w = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for r in [header] + rows:
        print("  ".join(str(x).ljust(n) for x, n in zip(r, w)))


def call_anomalies(c):
    """List the problems one proxy call shows (empty list means it looked healthy)."""
    out = []
    if c.get("error") or (c.get("status") or 200) >= 400:
        out.append("http_error")
    for k in ("empty", "think_leak", "truncated"):
        if c.get(k):
            out.append(k)
    if c.get("bad_tool_args"):
        out.append("bad_tool_args")
    if c.get("indent_drift"):
        out.append("indent_drift")
    if c.get("py_syntax_error"):
        out.append("py_syntax_error")
    if c.get("finish") == "length":
        out.append("hit_max_tokens")
    return out


def report_calls(calls):
    """Summarise proxy calls per agent/model, per prompt size, and per conversation."""
    # Step 1: per agent / model / host.
    groups = defaultdict(list)
    for c in calls:
        groups[(c.get("agent"), c.get("model"), c.get("host"))].append(c)
    rows = []
    for (agent, model, host), cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        bad = sum(1 for c in cs if call_anomalies(c))
        rows.append([agent, model, host, len(cs), len({c.get("conv") for c in cs}), pct(bad, len(cs)),
                     sum(c.get("prev_tool_errors", 0) for c in cs), sum(1 for c in cs if c.get("fallback_from")),
                     med(c.get("ttft_s") for c in cs), med(c.get("duration_s") for c in cs)])
    table("Calls by agent / model / host",
          ["agent", "model", "host", "calls", "convs", "anomaly", "tool_errs", "fallbacks", "ttft_s", "dur_s"], rows)

    kinds = Counter(k for c in calls for k in call_anomalies(c))
    table("Call anomalies (what went wrong at the API level)", ["anomaly", "calls"], kinds.most_common())

    # Step 2: does failure grow with prompt size? Tokens when Ollama reported them, else chars/4.
    buckets = defaultdict(list)
    for c in calls:
        tok = c.get("prompt_tokens") or (c.get("prompt_chars") or 0) // 4
        b = next((f"<{n // 1000}k" for n in (8000, 16000, 24000, 32000, 48000) if tok < n), ">=48k")
        buckets[b].append(c)
    order = ["<8k", "<16k", "<24k", "<32k", "<48k", ">=48k"]
    table("Prompt size vs problems", ["prompt", "calls", "anomaly", "tool_err_calls", "dur_s"],
          [[b, len(buckets[b]), pct(sum(1 for c in buckets[b] if call_anomalies(c)), len(buckets[b])),
            pct(sum(1 for c in buckets[b] if c.get("prev_tool_errors")), len(buckets[b])),
            med(c.get("duration_s") for c in buckets[b])] for b in order if buckets[b]])

    # Step 3: conversations: how many turns an agent needed, and whether its tools failed.
    convs = defaultdict(list)
    for c in calls:
        convs[c.get("conv")].append(c)
    per_agent = defaultdict(list)
    for cs in convs.values():
        per_agent[cs[0].get("agent")].append(cs)
    table("Agent runs (conversations)", ["agent", "runs", "median_turns", "max_turns", "runs_w_tool_errs", "median_min"],
          [[a, len(rs), med(len(r) for r in rs), max(len(r) for r in rs),
            pct(sum(1 for r in rs if any(c.get("prev_tool_errors") for c in r)), len(rs)),
            med(sum(c.get("duration_s") or 0 for c in r) / 60 for r in rs)] for a, rs in sorted(per_agent.items())])

    errs = Counter(t[:80] for c in calls for t in c.get("prev_tool_error_text", []))
    table("Most common tool errors seen by local agents", ["count", "error (first 80 chars)"],
          [[n, t.replace("\n", " ")] for t, n in errs.most_common(10)])


def report_outcomes(outs):
    """Summarise reviewed outcomes and say which task classes are ready to offload more."""
    # Group by agent and task, then score each group against the advice bars.
    groups = defaultdict(list)
    for o in outs:
        groups[(o["agent"], o["task"])].append(o)
    rows, advice = [], []
    for (agent, task), os_ in sorted(groups.items()):
        n = len(os_)
        r = Counter(o["result"] for o in os_)
        reasons = Counter(x for o in os_ for x in o.get("reasons", []))
        rate = r["pass"] / n
        rows.append([agent, task, n, pct(r["pass"], n), pct(r["partial"], n), pct(r["fail"], n),
                     sum(1 for o in os_ if o.get("redo") == "cloud"),
                     ", ".join(f"{k}:{v}" for k, v in reasons.most_common(3))])
        top = reasons.most_common(1)[0][0] if reasons else "unknown"
        if n < MIN_N:
            advice.append(f"{agent}/{task}: only {n} samples, keep collecting")
        elif rate >= READY:
            advice.append(f"{agent}/{task}: {pct(r['pass'], n)} pass, ready to offload more of this")
        elif rate < WEAK:
            advice.append(f"{agent}/{task}: {pct(r['pass'], n)} pass, keep in cloud or tune (top cause: {top})")
        else:
            advice.append(f"{agent}/{task}: {pct(r['pass'], n)} pass, usable with review; tune {top}")
    # Print the tables, the advice and the latest failure notes.
    table("Reviewed outcomes by agent / task",
          ["agent", "task", "n", "pass", "partial", "fail", "redone_in_cloud", "top reasons"], rows)
    table("Failure reasons overall", ["reason", "count"],
          Counter(x for o in outs for x in o.get("reasons", [])).most_common())
    print("\n## Offload advice")
    print("\n".join(f"- {a}" for a in advice) or "(no outcomes recorded yet)")
    fails = [o for o in outs if o["result"] != "pass" and o.get("note")][-10:]
    table("Latest failure notes", ["ts", "agent", "task", "reasons", "note"],
          [[o["ts"], o["agent"], o["task"], ",".join(o.get("reasons", [])), o["note"][:90]] for o in fails])


def report_matrix(outs):
    """Model x task: pass rate and cost per action, then the cheapest model that
    does each task well enough. Older outcomes with no model are left out."""
    groups = defaultdict(list)
    for o in outs:
        if o.get("model"):
            groups[(o["task"], o["model"])].append(o)
    rows, best = [], {}
    for (task, model), os_ in sorted(groups.items()):
        n, passes = len(os_), sum(1 for o in os_ if o["result"] == "pass")
        costs = [o["usd"] for o in os_ if o.get("usd") is not None]
        spent = sum(costs)
        # Cost per pass charges the failed attempts to the passes: what one
        # working result really costs when this model is tried first.
        per_try = spent / len(costs) if costs else None
        per_pass = (spent * n / len(costs)) / passes if costs and passes else None
        reasons = Counter(x for o in os_ for x in o.get("reasons", []))
        rows.append([task, model, n, pct(passes, n), "-" if per_try is None else "$%.3f" % per_try,
                     "-" if per_pass is None else "$%.3f" % per_pass,
                     ", ".join(f"{k}:{v}" for k, v in reasons.most_common(2))])
        if n >= MIN_N and passes / n >= WEAK and per_pass is not None:
            if task not in best or per_pass < best[task][1]:
                best[task] = (model, per_pass, pct(passes, n), n)
    table("Cost per action by task / model (usd per attempt, and per passing result)",
          ["task", "model", "n", "pass", "usd/try", "usd/pass", "top fail reasons"], rows)
    print("\n## Cheapest model that passes at least %d%% (min %d samples)" % (WEAK * 100, MIN_N))
    print("\n".join("- %s: %s at $%.3f per pass (%s of %d)" % (t, m, c, r, n)
                    for t, (m, c, r, n) in sorted(best.items())) or "(not enough data yet)")


def cmd_report(a):
    """Print the call and outcome reports for the last N days."""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - a.days * 86400))
    calls = list(read_jsonl(sorted(glob.glob(os.path.join(DIR, "calls-*.jsonl"))), since))
    outs = list(read_jsonl([OUTCOMES] if os.path.exists(OUTCOMES) else [], since))
    print(f"# Model report, last {a.days} days: {len(calls)} local calls, {len(outs)} reviewed outcomes")
    report_calls(calls)
    report_outcomes([o for o in outs if o.get("source") != "benchmark"])
    report_matrix(outs)


def main():
    """Parse arguments and run the sub-command."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    # outcome: one reviewed result, with its model and cost.
    o = sub.add_parser("outcome", help="record a reviewed agent result (local or cloud)")
    o.add_argument("--agent", required=True, help="agent name, e.g. " + ", ".join(AGENTS))
    o.add_argument("--model", default="", help="model id; default: the one the agent file pins")
    o.add_argument("--task", required=True, choices=TASKS)
    o.add_argument("--result", required=True, choices=RESULTS)
    o.add_argument("--reason", action="append", choices=REASONS, help="repeat for several")
    o.add_argument("--redo", default="none", help="who redid the work: cloud, local, an agent name, or none")
    o.add_argument("--minutes", type=float, help="wall time the agent took")
    o.add_argument("--repo", default="")
    o.add_argument("--note", default="", help="one line: what went wrong or what made it work")
    o.add_argument("--usd", type=float, help="cost in USD, when known")
    o.add_argument("--usage-file", help="copilot -p --usage-output-file JSON to read the cost from")
    o.add_argument("--since", help="sub-agent start (HH:MM today, or ISO): cost from the usage log")
    o.add_argument("--source", default="review", help=argparse.SUPPRESS)
    o.set_defaults(func=cmd_outcome)
    # import-bench and report.
    b = sub.add_parser("import-bench", help="load cloudbench.jsonl results as outcomes")
    b.add_argument("file")
    b.set_defaults(func=cmd_import_bench)
    r = sub.add_parser("report", help="summarise calls and outcomes")
    r.add_argument("--days", type=int, default=30)
    r.set_defaults(func=cmd_report)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
