"""Local command runner for the WebUI Remote Console (pxmx node agent).

Parity copy of ``lm/core/src/command_runner.py`` — the hub relays a signed
RUN_COMMAND down to this agent (hub → owning spoke → agent), and the agent runs
it here and returns the result up the same path. Two modes:

* **allowlist** (default): the command's binary must be in ``ALLOWED_BINARIES``
  and carry NO shell metacharacters — a curated diagnostic set. A fat-finger
  guard, not a hard boundary (the hub's Global-Admin can flip "Debug (shell)").
* **shell** (opt-in via the WebUI Debug knob): runs verbatim through ``bash -lc``.

Always bounded: a wall-clock timeout and an output byte cap.
"""

# os.path.basename strips any directory from the command's binary before the allowlist check.
import os
# shlex splits a command line into arguments the same way a POSIX shell would (quotes respected).
import shlex
# subprocess actually runs the command and captures its output.
import subprocess

# Binaries allowed in the default "allowlist" mode: read-only diagnostic tools (service status,
# logs, files, processes, network, Proxmox CLIs). Anything else needs the opt-in shell mode.
ALLOWED_BINARIES = {
    "systemctl", "journalctl", "service",
    "tail", "head", "cat", "grep", "egrep", "zgrep",
    "ls", "find", "stat", "readlink", "file", "wc",
    "ps", "pgrep", "df", "du", "free", "uptime", "uname", "hostname",
    "date", "whoami", "id", "env",
    "ip", "ss", "netstat", "ping", "dig", "nslookup", "host", "getent",
    "git", "cut", "sort", "uniq", "tr",
    # pxmx-node diagnostics
    "qm", "pct", "pvesh", "pvesm", "pveversion", "pvecm",
}

# Characters that would let a command chain, redirect, or substitute in a shell. Allowlist mode
# rejects them so a "safe" binary cannot be used to smuggle in another command.
_SHELL_METACHARS = set(";|&`$><\n\\!(){}")


def _check_allowlisted(command: str):
    """Decide whether a command is allowed in allowlist (non-shell) mode.

    Returns:
        A (allowed, reason) tuple; reason is a human-readable error when not allowed.
    """
    # Collect every shell metacharacter present in the command (sorted so the message is stable).
    bad = sorted({c for c in command if c in _SHELL_METACHARS})
    # Any metacharacter at all means the command is refused, with a hint about shell mode.
    if bad:
        return False, (f"shell metacharacters {''.join(bad)!r} are not allowed in "
                       "diagnostic mode — enable Debug (shell) mode to run those")
    # Split the command into argv; malformed quoting (e.g. an unclosed quote) raises ValueError.
    try:
        parts = shlex.split(command)
    # Unbalanced quotes etc. -- report that the command could not be parsed.
    except ValueError as e:
        return False, f"unparseable command: {e}"
    # Nothing left after splitting (e.g. only whitespace/quotes) is not a runnable command.
    if not parts:
        return False, "empty command"
    # Only the program name matters for the allowlist, so drop any path like /usr/bin/.
    binary = os.path.basename(parts[0])
    # Refuse binaries that are not in the curated diagnostic set.
    if binary not in ALLOWED_BINARIES:
        return False, (f"'{binary}' is not in the diagnostic allowlist — enable "
                       "Debug (shell) mode to run arbitrary commands")
    # All checks passed.
    return True, ""


def run_local_command(command: str, allow_shell: bool = False,
                      timeout: float = 30.0, max_bytes: int = 64 * 1024) -> dict:
    """Run a command locally with a timeout and output cap, returning a result dict.

    Args:
        command: The command line to run.
        allow_shell: True runs it verbatim through bash (Debug mode); False enforces the allowlist.
        timeout: Seconds before the command is killed.
        max_bytes: Maximum characters kept from stdout and from stderr.

    Returns:
        Dict with ok, rc, stdout, stderr, truncated, error and mode keys.
    """
    # Normalise the input: treat None as empty and trim surrounding whitespace.
    command = (command or "").strip()
    # Default "failed" result shape; error paths below copy it and add an error message.
    base = {"ok": False, "rc": None, "stdout": "", "stderr": "", "truncated": False}
    # An empty command is rejected up front.
    if not command:
        return {**base, "error": "empty command"}

    # Debug (shell) mode: hand the whole string to a login bash so pipes, globs etc. work.
    if allow_shell:
        argv = ["/bin/bash", "-lc", command]
        mode = "shell"
    # Default allowlist mode: validate first, then run the split argv directly (no shell).
    else:
        # Refuse the command if it fails the allowlist / metacharacter checks.
        ok, reason = _check_allowlisted(command)
        if not ok:
            return {**base, "error": reason}
        # Safe to split now; running argv without a shell means no shell interpretation happens.
        argv = shlex.split(command)
        mode = "allowlist"

    # Run the command from / with captured text output and a hard timeout.
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd="/")
    # The command ran too long and was killed by subprocess.
    except subprocess.TimeoutExpired:
        return {**base, "error": f"command timed out after {timeout:.0f}s", "mode": mode}
    # The program itself does not exist on this host.
    except FileNotFoundError:
        return {**base, "error": f"binary not found: {argv[0]}", "mode": mode}
    # Any other OS-level failure (permissions, etc.) is reported instead of crashing the agent.
    except Exception as e:  # noqa: BLE001
        return {**base, "error": str(e), "mode": mode}

    # Start with the raw output (None becomes "") and assume nothing was cut off.
    out, err, truncated = proc.stdout or "", proc.stderr or "", False
    # Cap stdout so a chatty command cannot flood the relay; mark the result as truncated.
    if len(out) > max_bytes:
        out, truncated = out[:max_bytes] + "\n…[truncated]", True
    # Same cap for stderr.
    if len(err) > max_bytes:
        err, truncated = err[:max_bytes] + "\n…[truncated]", True
    # Success means the command ran (even with a non-zero exit code); rc carries its exit status.
    return {"ok": True, "rc": proc.returncode, "stdout": out, "stderr": err,
            "truncated": truncated, "error": "", "mode": mode}
