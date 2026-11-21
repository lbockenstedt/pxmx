#!/usr/bin/env python3
"""Daily gate on cloud AI spend (fleet rule 1.10).

The gate stops runaway sessions; it is not a budget. Copilot CLI runs this
script from the hooks in .github/hooks/fleet-hooks.json:

    check     preToolUse: once today's cloud AI spend reaches the gate, refuse
              every tool call except those an agent needs to stop, unless the
              session has used only local models today. In the last
              reserve_usd before the gate, refuse new cloud sub-agents.
              Always refuse an agent's attempt to reopen the gate.
    context   sessionStart and subagentStart: tell the agent today's spend
              and to prefer local models.

People run it from a terminal:

    python3 .github/hooks/ai_budget.py status          today's spend and gate
    python3 .github/hooks/ai_budget.py continue [USD]  reopen the gate for USD
                                                       more today (default:
                                                       step_usd)
    python3 .github/hooks/ai_budget.py prices          per-model prices seen in
                                                       the last 30 days, for
                                                       the table AB uses

Spend is read from Copilot CLI's own usage log ($COPILOT_HOME/session-store.db,
assistant_usage_events.total_nano_aiu; 1e9 nano AIU = 1 AI credit = $0.01).
It covers every CLI session and sub-agent on this machine, and local models
(Ollama, llama.cpp) cost nothing in it. The day is the local calendar day.
The numbers come from .github/ai-budget.json. The only state written is this
machine's reopened gate for today, in $COPILOT_HOME/ai-budget-state.json.

Fails open: with no usage log (another tool, or Copilot's cloud agent) or on
any error, the hook prints nothing and exits 0, because Copilot refuses the
tool call when a preToolUse command fails. The gate stops runaway sessions
of an agent that follows the rules; it is not a security boundary.

Plain Python 3.9+, no dependencies.
"""
import datetime
import json
import os
import re
import sqlite3  # read-only: Copilot CLI's usage log; this script stores nothing in it
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
DEFAULTS = {"gate_usd": 100, "step_usd": 100, "reserve_usd": 25}
STATE_NAME = "ai-budget-state.json"
# Tools an agent still needs once the gate is closed: to stop and to report.
STOP_TOOLS = {"task_complete", "ask_user", "stop_bash"}
SHELL_TOOLS = {"bash", "powershell", "write_bash"}
READ_TOOLS = {"view", "grep", "glob"}
# Tools that start more cloud model work.
FANOUT_TOOLS = {"task", "run_dynamic_workflow"}
# Copilot CLI model-name prefixes of the local model servers (Ollama and
# llama.cpp's llama-server), from the provider names in ~/.copilot/providers.json.
LOCAL_MODELS = ("ollama/", "llamacpp/")
REOPEN = re.compile(r"ai_budget\.py['\"]?\s+continue\b")
STATUS_ONLY = re.compile(r"^\s*python3?\s+\S*ai_budget\.py\s+status\s*$")
REOPEN_REASON = ("Only a person can reopen the AI spend gate (fleet rule 1.10). Do not run it, "
                 "change its state or work around it; stop and tell the user.")


def policy():
    """The fleet's gate numbers (USD) from .github/ai-budget.json; the defaults if unreadable."""
    try:
        with open(SCRIPT.parent.parent / "ai-budget.json", encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: float(data.get(k, v)) for k, v in DEFAULTS.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {k: float(v) for k, v in DEFAULTS.items()}


def copilot_home():
    """Copilot CLI's config directory: $COPILOT_HOME, else ~/.copilot."""
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def day_start():
    """(today's local date, local midnight as a UTC time in the usage log's format)."""
    now = datetime.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.date().isoformat(), midnight.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def usage_log():
    """A read-only connection to Copilot CLI's usage log, or None when there is none."""
    db = copilot_home() / "session-store.db"
    if not db.is_file():
        return None
    return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def spent_today(since):
    """Cloud AI spend (USD) logged since ``since``; None when there is no usage log."""
    con = usage_log()
    if con is None:
        return None
    try:
        row = con.execute("SELECT SUM(total_nano_aiu) FROM assistant_usage_events "
                          "WHERE replace(created_at, ' ', 'T') >= ?", (since,)).fetchone()
    finally:
        con.close()
    return (row[0] or 0) / 1e11


def local_session(session, since, wait_s=2.0):
    """True when ``session`` has logged model calls since ``since`` and none cost anything.

    Copilot CLI logs every model call under its session, sub-agents' calls
    included, so a session on a cloud model, or one that started a cloud
    sub-agent, shows spend. A model call is logged just before the tool calls
    it asked for are checked (5 ms before, in a test), but that order is not
    documented, so a session with no calls logged yet is re-read for up to
    ``wait_s`` seconds. No session id, still no calls, or an unreadable log
    count as cloud, which keeps the gate closed."""
    if not session:
        return False
    deadline = time.monotonic() + wait_s
    while True:
        # Count the session's calls today and what they cost.
        try:
            con = usage_log()
            if con is None:
                return False
            try:
                calls, nano = con.execute("SELECT COUNT(*), SUM(total_nano_aiu) FROM assistant_usage_events "
                                          "WHERE session_id = ? AND replace(created_at, ' ', 'T') >= ?",
                                          (session, since)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return False
        # Local models cost nothing: their calls log no cost, or 0.
        if calls or time.monotonic() >= deadline:
            return calls > 0 and not nano
        time.sleep(0.1)


def gate_today(pol, day):
    """Today's gate: the fleet's, or higher when a person reopened it today."""
    try:
        with open(copilot_home() / STATE_NAME, encoding="utf-8") as fh:
            state = json.load(fh)
        if state.get("day") == day:
            return max(pol["gate_usd"], float(state["gate_usd"]))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return pol["gate_usd"]


def arg_text(args):
    """A tool call's arguments as one string, for matching."""
    if isinstance(args, dict):
        return " ".join(str(v) for v in args.values())
    return str(args or "")


def reopen_attempt(tool, args):
    """True when a tool call would reopen the gate or change its state file.

    Shell commands are matched on their text. Other tools are matched only on
    the file they write, so this script and its docs stay editable."""
    if tool in SHELL_TOOLS:
        text = arg_text(args)
        return bool(REOPEN.search(text)) or STATE_NAME in text
    return (tool not in READ_TOOLS and isinstance(args, dict)
            and str(args.get("path") or "").endswith(STATE_NAME))


def status_call(tool, args):
    """True for a shell call that only runs ``ai_budget.py status``."""
    return tool in SHELL_TOOLS and isinstance(args, dict) and bool(STATUS_ONLY.match(str(args.get("command") or "")))


def cloud_fanout(tool, args):
    """True when a call starts more cloud model work: a sub-agent on a cloud
    model, or a workflow. An ollama/* or llamacpp/* model or a local-* agent
    is local."""
    if tool not in FANOUT_TOOLS:
        return False
    args = args if isinstance(args, dict) else {}
    model = str(args.get("model") or "")
    if model:
        return not model.startswith(LOCAL_MODELS)
    return tool != "task" or not str(args.get("agent_type") or "").startswith("local-")


def decide(tool, args, spent, gate, reserve, local=False):
    """The reason to refuse one tool call, or None to let it run.

    ``local``: the session has used only local models today, so it costs
    nothing and keeps working past the gate, still without cloud sub-agents."""
    # Tool arguments may arrive as a JSON string.
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if reopen_attempt(tool, args):
        return REOPEN_REASON
    if spent >= gate and not local and tool not in STOP_TOOLS and not status_call(tool, args):
        return ("AI spend gate closed: $%.0f of cloud AI used today, gate $%.0f (fleet rule 1.10). "
                "Stop now and tell the user. Only a person reopens it, by running: python3 %s continue"
                % (spent, gate, SCRIPT))
    if cloud_fanout(tool, args) and spent + reserve >= gate:
        return ("Only $%.0f of cloud AI is left before today's gate of $%.0f (fleet rule 1.10), so no "
                "new cloud sub-agents. Use a local-* agent or an ollama/* or llamacpp/* model, or finish "
                "without one." % (max(gate - spent, 0), gate))
    return None


def context_text(spent, gate):
    """What an agent is told about the gate when a session or sub-agent starts.

    A session has no model calls logged when it starts, so a closed gate's
    text covers both cloud and local sessions."""
    if spent >= gate:
        return ("AI spend gate CLOSED: $%.0f of cloud AI used today, gate $%.0f (fleet rule 1.10). A "
                "session on a local model (ollama/*, llamacpp/*) keeps working but cannot start cloud "
                "sub-agents. Other sessions' tool calls are refused until a person runs: python3 %s "
                "continue; when one is refused, stop and tell the user." % (spent, gate, SCRIPT))
    return ("AI spend today: $%.0f of the $%.0f daily gate, $%.0f left (fleet rule 1.10). Use local "
            "models (local-* agents, ollama/* and llamacpp/* models) for every task they can do, even when slower. "
            "Use a cloud model only when no local option fits; check first with: python3 %s status"
            % (spent, gate, gate - spent, SCRIPT))


def hook(cmd):
    """Answer one Copilot hook (``check`` or ``context``) read from stdin.

    Never fails: on any error it prints nothing, which lets the call run."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
        pol = policy()
        day, since = day_start()
        spent = spent_today(since)
        if spent is None:
            return 0
        gate = gate_today(pol, day)
        if cmd == "context":
            print(json.dumps({"additionalContext": context_text(spent, gate)}))
            return 0
        # Only a closed gate needs to know whether the session is local.
        session = data.get("sessionId") or os.environ.get("COPILOT_AGENT_SESSION_ID")
        local = spent >= gate and local_session(session, since)
        reason = decide(data.get("toolName"), data.get("toolArgs"), spent, gate, pol["reserve_usd"], local)
        if reason:
            print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
    except Exception:  # noqa: BLE001 -- fail open: a failing preToolUse hook refuses every call
        pass
    return 0


def status():
    """Print today's spend and gate."""
    pol = policy()
    day, since = day_start()
    spent = spent_today(since)
    if spent is None:
        print("No Copilot CLI usage log under %s, so no gate here." % copilot_home())
        return 0
    gate = gate_today(pol, day)
    state = "CLOSED" if spent >= gate else "open, $%.2f left" % (gate - spent)
    print("AI spend today (%s): $%.2f; gate $%.2f (%s)." % (day, spent, gate, state))
    print("A person reopens it for $%.0f more with: python3 %s continue [USD]" % (pol["step_usd"], SCRIPT))
    return 0


def reopen(amount):
    """A person's override: open the gate for ``amount`` USD more, today only."""
    pol = policy()
    day, since = day_start()
    amount = pol["step_usd"] if amount is None else amount
    spent = spent_today(since)
    if spent is None:
        print("No Copilot CLI usage log under %s, so no gate to reopen." % copilot_home())
        return 1
    gate = max(gate_today(pol, day), spent) + amount
    # Write a temp file and rename it, so a hook never reads half a file.
    path = copilot_home() / STATE_NAME
    tmp = path.with_name(STATE_NAME + ".tmp")
    tmp.write_text(json.dumps({"day": day, "gate_usd": round(gate, 2)}) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    print("AI spend gate open until $%.2f today ($%.2f used so far)." % (gate, spent))
    return 0


def prices():
    """Print the per-model prices seen in the last 30 days, in USD per 1M
    tokens, in the format of prices_usd_per_mtok in .github/ai-budget.json."""
    con = usage_log()
    if con is None:
        print("No Copilot CLI usage log under %s." % copilot_home())
        return 1
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        rows = con.execute("SELECT token_details_json FROM assistant_usage_events "
                           "WHERE replace(created_at, ' ', 'T') >= ? AND token_details_json IS NOT NULL",
                           (since,)).fetchall()
    finally:
        con.close()
    # Each entry prices one token type, in nano AIU per batch of tokens.
    seen = {}
    for (details,) in rows:
        for d in json.loads(details):
            usd = d["costPerBatch"] / d["batchSize"] * 1e6 / 1e11
            seen.setdefault(d["model"], {})[d["tokenType"]] = round(usd, 4)
    table = {m: {k: p.get(k, 0) for k in ("input", "cache_read", "output")} for m, p in sorted(seen.items())}
    print(json.dumps(table, indent=2))
    return 0


def main(argv):
    """Run the command in argv[1] (default: status); see the module docstring."""
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd in ("check", "context"):
        return hook(cmd)
    if cmd == "status":
        return status()
    if cmd == "continue":
        amount = float(argv[2]) if len(argv) > 2 else None
        if amount is not None and amount <= 0:
            print("The amount must be more than 0.")
            return 2
        return reopen(amount)
    if cmd == "prices":
        return prices()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
