#!/usr/bin/env python3
"""Daily gate on cloud AI spend (fleet rule 1.10).

The gate stops runaway sessions; it is not a budget. Copilot CLI runs this
script from the hooks in .github/hooks/fleet-hooks.json:

    check     preToolUse: once today's cloud AI spend reaches the gate, refuse
              new work (cloud sub-agents, workflows, write_agent messages,
              copilot -p runs) so work already running can finish; once it
              reaches the gate plus overrun_usd, refuse every tool call except
              those an agent needs to stop, unless the session has used only
              local models today. In the last reserve_usd before the gate,
              refuse new cloud sub-agents. Always refuse an agent's attempt to
              reopen the gate.
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
import shlex
import sqlite3  # read-only: Copilot CLI's usage log; this script stores nothing in it
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
DEFAULTS = {"gate_usd": 100, "step_usd": 100, "reserve_usd": 25, "overrun_usd": 25}
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
SHELL_SEPARATORS = ";&|()"
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
    ``wait_s`` seconds. A session whose calls so far are all free is re-read
    for the same window too, so a paid call logged late is still seen; a paid
    row at any point means cloud. No session id, still no calls, or an
    unreadable log count as cloud, which keeps the gate closed."""
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
        # A paid row at any point means cloud. Free-only rows (or none) are
        # re-read for the whole wait window, because the current call's row
        # may not be logged yet; only then is the session called local.
        if nano:
            return False
        if time.monotonic() >= deadline:
            return calls > 0
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


def shell_text(args):
    """The shell text of a shell tool call: bash/powershell carry it in
    "command", write_bash in "input"."""
    if isinstance(args, dict):
        return str(args.get("command") or args.get("input") or "")
    return arg_text(args)


def status_call(tool, args):
    """True for a shell call that only runs ``ai_budget.py status``."""
    return tool in SHELL_TOOLS and isinstance(args, dict) and bool(STATUS_ONLY.match(shell_text(args)))


def cloud_copilot_run(text):
    """True when shell text starts a ``copilot -p`` run that is not on a local model.

    Each chained command is judged on its own, so a local run cannot cover a
    cloud one, and the text is split into words the way a shell does, so a
    prompt that merely mentions ``--model ollama/x`` does not count. A run is
    local only when it names models and every one is ollama/* or llamacpp/*.
    ``--agent local-*`` alone is not enough: a test run with only --agent
    used a cloud model. Text the shell parser cannot read counts as cloud.

    A quoted word that holds a command of its own (``bash -c '...'``,
    ``ssh host '...'``) is checked again by itself; a copilot prompt's text
    is not, since it is only words to the model. Any other quoted word with
    spaces is treated as a possible script, wherever it sits: listing the
    positions that run a script (bash -c, sh -lc, ssh with options, xargs,
    ...) would miss some and let new cloud work through. Quoted data that
    holds a cloud copilot command (``echo 'copilot -p ...'``) is
    therefore refused too. That only refuses new work, which a person can
    reopen; work already running is never stopped by it. A ``copilot`` word
    anywhere in a command counts, not only the first, because wrappers put it
    mid-command (``perl -e '...' copilot``, ``env X=1 copilot``); so
    ``echo copilot -p`` is refused too, which only errs toward refusing new
    work.

    It reads ordinary commands, quoting and escapes included; it does not try
    to catch deliberate evasion (``$(...)``, variables), which fleet rule
    1.10 already forbids."""
    # Nothing to parse unless "copilot" appears once quotes and escapes are gone.
    if "copilot" not in re.sub(r"[\\'\"`]", "", text):
        return False
    # Join continued lines (bash \, PowerShell `): they are one command. A
    # bare newline separates commands; inside quotes it only changes the prompt.
    text = re.sub(r"[\\`]\r?\n", " ", text).replace("\n", ";")
    # A PowerShell backtick escapes the next character (`-p is -p).
    text = re.sub(r"`(.)", r"\1", text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SHELL_SEPARATORS)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return True
    # Split the words into commands at the separators (; && || | and brackets).
    commands, current = [], []
    for word in words + [";"]:
        if word and set(word) <= set(SHELL_SEPARATORS):
            commands.append(current)
            current = []
        else:
            current.append(word)
    for command in commands:
        # A quoted script inside this command (each one is shorter, so this ends),
        # but not a prompt's text.
        scripts = [w for i, w in enumerate(command)
                   if " " in w and not (i and command[i - 1] in ("-p", "--prompt"))
                   and not w.startswith("--prompt=")]
        if any(w != text and cloud_copilot_run(w) for w in scripts):
            return True
        starts = [i for i, w in enumerate(command) if os.path.basename(w) == "copilot"]
        if not starts:
            continue
        prompt, models, rest = False, [], command[starts[0] + 1:]
        i = 0
        while i < len(rest):
            word = rest[i]
            if word in ("-p", "--prompt"):
                prompt, i = True, i + 2  # skip the prompt text itself
                continue
            if word.startswith("--prompt="):
                prompt = True
            elif word == "--model" and i + 1 < len(rest):
                models.append(rest[i + 1])
                i += 1
            elif word.startswith("--model="):
                models.append(word.split("=", 1)[1])
            i += 1
        if prompt and not (models and all(m.startswith(LOCAL_MODELS) for m in models)):
            return True
    return False


def cloud_fanout(tool, args):
    """True when a call starts more cloud model work: a sub-agent on a cloud
    model, a workflow, or a shell ``copilot -p`` run. An ollama/* or llamacpp/*
    model or a local-* agent is local."""
    if tool in SHELL_TOOLS:
        return cloud_copilot_run(shell_text(args))
    if tool not in FANOUT_TOOLS:
        return False
    args = args if isinstance(args, dict) else {}
    model = str(args.get("model") or "")
    if model:
        return not model.startswith(LOCAL_MODELS)
    return tool != "task" or not str(args.get("agent_type") or "").startswith("local-")


def decide(tool, args, spent, gate, reserve, local=False, overrun=0.0):
    """The reason to refuse one tool call, or None to let it run.

    Past the gate only new work is refused (cloud sub-agents, workflows,
    write_agent messages, copilot -p runs), so work already running can
    finish. Past gate + overrun every call of a cloud session is refused, the
    runaway backstop.

    ``local``: the session has used only local models today, so it costs
    nothing and keeps working past both limits, still without cloud sub-agents."""
    # Tool arguments may arrive as a JSON string.
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if reopen_attempt(tool, args):
        return REOPEN_REASON
    if spent >= gate + overrun and not local and tool not in STOP_TOOLS and not status_call(tool, args):
        return ("AI spend gate closed: $%.0f of cloud AI used today, gate $%.0f, and the $%.0f overrun for "
                "work already running is used up (fleet rule 1.10). Stop now and tell the user. Only a person "
                "reopens it, by running: python3 %s continue" % (spent, gate, overrun, SCRIPT))
    if spent >= gate and (cloud_fanout(tool, args) or tool == "write_agent"):
        return ("AI spend gate closed: $%.0f of cloud AI used today, gate $%.0f (fleet rule 1.10), so no new "
                "cloud sub-agents, workflows, agent messages or copilot -p runs. Let work already running finish, "
                "then stop and tell the user. Only a person reopens it, by running: python3 %s continue"
                % (spent, gate, SCRIPT))
    if cloud_fanout(tool, args) and spent + reserve >= gate:
        return ("Only $%.0f of cloud AI is left before today's gate of $%.0f (fleet rule 1.10), so no "
                "new cloud sub-agents. Use a local-* agent or an ollama/* or llamacpp/* model, or finish "
                "without one." % (max(gate - spent, 0), gate))
    return None


def context_text(spent, gate, overrun=0.0):
    """What an agent is told about the gate when a session or sub-agent starts.

    A session has no model calls logged when it starts, so a closed gate's
    text covers both cloud and local sessions."""
    if spent >= gate + overrun:
        return ("AI spend gate CLOSED and its $%.0f overrun used up: $%.0f of cloud AI used today, gate $%.0f "
                "(fleet rule 1.10). Every tool call of a session that used a cloud model today is refused, so "
                "stop now and tell the user. A session on a local model (ollama/*, llamacpp/*) keeps working "
                "but cannot start cloud work. Only a person reopens it, by running: python3 %s continue"
                % (overrun, spent, gate, SCRIPT))
    if spent >= gate:
        return ("AI spend gate CLOSED: $%.0f of cloud AI used today, gate $%.0f (fleet rule 1.10). Start no "
                "new work: no cloud sub-agents, workflows, write_agent messages or copilot -p runs. Let work "
                "already running finish, then stop and tell the user. At $%.0f (gate plus overrun) every tool "
                "call of a session that used a cloud model is refused. Only a person reopens it, by running: "
                "python3 %s continue" % (spent, gate, gate + overrun, SCRIPT))
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
            print(json.dumps({"additionalContext": context_text(spent, gate, pol["overrun_usd"])}))
            return 0
        # Only a closed gate needs to know whether the session is local.
        session = data.get("sessionId") or os.environ.get("COPILOT_AGENT_SESSION_ID")
        local = spent >= gate and local_session(session, since)
        reason = decide(data.get("toolName"), data.get("toolArgs"), spent, gate, pol["reserve_usd"], local,
                        pol["overrun_usd"])
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
    if spent >= gate + pol["overrun_usd"]:
        state = "CLOSED, overrun used up: cloud sessions are stopped"
    elif spent >= gate:
        state = "CLOSED to new work; running work may finish"
    else:
        state = "open, $%.2f left" % (gate - spent)
    print("AI spend today (%s): $%.2f; gate $%.2f (%s)." % (day, spent, gate, state))
    print("A person reopens it for $%.0f more with: python3 %s continue [USD]" % (pol["step_usd"], SCRIPT))
    print("Past the gate only new work is refused; at $%.2f (gate plus $%.0f overrun) every tool call of a "
          "cloud session is refused." % (gate + pol["overrun_usd"], pol["overrun_usd"]))
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
