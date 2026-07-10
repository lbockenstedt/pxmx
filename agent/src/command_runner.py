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

import os
import shlex
import subprocess

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

_SHELL_METACHARS = set(";|&`$><\n\\!(){}")


def _check_allowlisted(command: str):
    bad = sorted({c for c in command if c in _SHELL_METACHARS})
    if bad:
        return False, (f"shell metacharacters {''.join(bad)!r} are not allowed in "
                       "diagnostic mode — enable Debug (shell) mode to run those")
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return False, f"unparseable command: {e}"
    if not parts:
        return False, "empty command"
    binary = os.path.basename(parts[0])
    if binary not in ALLOWED_BINARIES:
        return False, (f"'{binary}' is not in the diagnostic allowlist — enable "
                       "Debug (shell) mode to run arbitrary commands")
    return True, ""


def run_local_command(command: str, allow_shell: bool = False,
                      timeout: float = 30.0, max_bytes: int = 64 * 1024) -> dict:
    command = (command or "").strip()
    base = {"ok": False, "rc": None, "stdout": "", "stderr": "", "truncated": False}
    if not command:
        return {**base, "error": "empty command"}

    if allow_shell:
        argv = ["/bin/bash", "-lc", command]
        mode = "shell"
    else:
        ok, reason = _check_allowlisted(command)
        if not ok:
            return {**base, "error": reason}
        argv = shlex.split(command)
        mode = "allowlist"

    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd="/")
    except subprocess.TimeoutExpired:
        return {**base, "error": f"command timed out after {timeout:.0f}s", "mode": mode}
    except FileNotFoundError:
        return {**base, "error": f"binary not found: {argv[0]}", "mode": mode}
    except Exception as e:  # noqa: BLE001
        return {**base, "error": str(e), "mode": mode}

    out, err, truncated = proc.stdout or "", proc.stderr or "", False
    if len(out) > max_bytes:
        out, truncated = out[:max_bytes] + "\n…[truncated]", True
    if len(err) > max_bytes:
        err, truncated = err[:max_bytes] + "\n…[truncated]", True
    return {"ok": True, "rc": proc.returncode, "stdout": out, "stderr": err,
            "truncated": truncated, "error": "", "mode": mode}
