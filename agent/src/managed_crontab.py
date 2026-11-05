"""Managed-crontab reconciliation for the pxmx agent.

The operator pastes a crontab job (one or more standard 5-field crontab lines)
per Proxmox server in the WebUI; the hub pushes it to this node's agent, which
keeps root's crontab in sync with it. We NEVER touch the operator's own crontab
entries — the managed content lives inside a clearly-marked block:

    # BEGIN LM-MANAGED (do not edit — managed by Lab Manager)
    ...pasted lines...
    # END LM-MANAGED

Everything outside the block is preserved verbatim. An empty/blank desired
content removes the block entirely (so clearing the textarea removes the managed
jobs). Reconciliation is idempotent and drift-correcting: if someone edits the
managed block by hand, the next apply restores it to the pushed content.

``reconcile_block`` is pure (no subprocess / disk) so it is unit-testable; the
``apply_managed_crontab`` wrapper reads/writes root's crontab via the ``crontab``
binary and only writes when the text actually changes.
"""
# Postpone annotation evaluation so type hints never cost anything at import time.
from __future__ import annotations

# asyncio runs the blocking crontab subprocess calls in a worker thread.
import asyncio
# logging reports reconcile results and failures.
import logging
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, Optional

# Share the main agent logger so crontab messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# Exact marker lines that fence the hub-managed jobs inside root's crontab. Anything between them is
# owned by Lab Manager; everything outside is the operator's and left alone.
BEGIN_MARKER = "# BEGIN LM-MANAGED (do not edit — managed by Lab Manager)"
END_MARKER = "# END LM-MANAGED"


def _strip_block(text: str) -> str:
    """Return ``text`` with any existing LM-MANAGED block (and the surrounding
    blank line we add) removed. Tolerant of a hand-mangled block: removes from
    the first BEGIN marker to the next END marker; a BEGIN with no END removes
    to end-of-file (so a corrupted block can't strand un-managed lines)."""
    # Work line by line; None is treated as an empty crontab.
    lines = (text or "").splitlines()
    # out collects the lines we keep; i walks the input; n is the total line count.
    out = []
    i = 0
    n = len(lines)
    # Walk every line, copying it unless it belongs to a managed block.
    while i < n:
        # Found the start of a managed block: skip over it instead of copying it.
        if lines[i].strip() == BEGIN_MARKER:
            # Skip until (and including) the END marker, or to EOF.
            i += 1
            while i < n and lines[i].strip() != END_MARKER:
                i += 1
            i += 1  # consume the END marker (or step past EOF harmlessly)
            # Also drop a single trailing blank separator we may have added.
            if out and out[-1].strip() == "":
                out.pop()
            continue
        # Ordinary (operator-owned) line: keep it verbatim.
        out.append(lines[i])
        i += 1
    # Re-join the surviving lines.
    return "\n".join(out)


def reconcile_block(existing: str, desired: str) -> str:
    """Return root's crontab text with the LM-MANAGED block set to ``desired``.

    - ``desired`` blank/empty → the block is removed (managed jobs cleared).
    - Otherwise the block is (re)written with ``desired``'s lines, appended after
      the operator's own entries. Existing non-managed lines are preserved.
    Output always ends with exactly one trailing newline (cron wants a final
    newline) unless the whole result is empty.
    """
    # The operator's own crontab with any old managed block removed and trailing newlines trimmed.
    base = _strip_block(existing).rstrip("\n")
    # Clean the desired content: drop blank lines and trailing spaces so comparisons are stable.
    desired_body = "\n".join(
        ln.rstrip() for ln in (desired or "").splitlines() if ln.strip() != ""
    ).strip("\n")

    # Desired content is empty: the managed block should disappear entirely.
    if not desired_body:
        # No managed jobs — just the preserved base (may be empty).
        return (base + "\n") if base else ""

    # Build the fenced managed block from the cleaned desired lines.
    block = f"{BEGIN_MARKER}\n{desired_body}\n{END_MARKER}"
    # Operator content exists: append the block after it with one blank separator line.
    if base:
        return f"{base}\n\n{block}\n"
    # Only managed content: the crontab is just the block.
    return f"{block}\n"


def _read_crontab_sync() -> str:
    """Read root's current crontab (empty string when none is installed)."""
    # Imported locally because only the sync crontab helpers need subprocess.
    import subprocess
    # Run `crontab -l` to print root's current crontab.
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    # No crontab binary means cron is not installed; surface that as a clear error.
    except FileNotFoundError:
        raise RuntimeError("crontab binary not found")
    # Non-zero exit is either "no crontab yet" (normal) or a real failure.
    if p.returncode != 0:
        # "no crontab for <user>" → returncode 1 with that message → treat as empty.
        err = (p.stderr or "").lower()
        if "no crontab" in err:
            return ""
        raise RuntimeError((p.stderr or p.stdout or f"crontab -l exited {p.returncode}").strip())
    # Success: return the crontab text (may be empty).
    return p.stdout or ""


def _write_crontab_sync(text: str) -> None:
    """Install ``text`` as root's crontab (empty text removes the crontab)."""
    # Imported locally because only the sync crontab helpers need subprocess.
    import subprocess
    # Nothing to install: remove root's crontab entirely (errors ignored, e.g. none existed).
    if not text.strip():
        subprocess.run(["crontab", "-r"], capture_output=True, text=True, timeout=15)
        return
    # Feed the full text to `crontab -` on stdin, which replaces root's crontab atomically.
    p = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=15)
    # A rejected crontab (bad syntax etc.) is reported to the caller.
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"crontab - exited {p.returncode}").strip())


async def apply_managed_crontab(desired: str) -> Dict[str, Any]:
    """Reconcile root's crontab so its LM-MANAGED block matches ``desired``.

    Idempotent: only writes when the text actually changes. Runs the blocking
    ``crontab`` calls in a thread so the event loop isn't stalled. Returns a
    status dict for logging + telemetry."""
    def _do() -> Dict[str, Any]:
        """Blocking worker: read, reconcile, and write only when needed."""
        # Read the live crontab and compute what it should be.
        current = _read_crontab_sync()
        target = reconcile_block(current, desired)
        # Already correct (or both effectively empty): nothing to write.
        if target == current or (not target.strip() and not current.strip()):
            return {"ok": True, "changed": False, "managed_lines": _count_managed(desired)}
        # Drift or new content: install the reconciled crontab.
        _write_crontab_sync(target)
        return {"ok": True, "changed": True, "managed_lines": _count_managed(desired)}
    # Run the blocking work in a thread so the agent's event loop keeps serving other tasks.
    try:
        res = await asyncio.to_thread(_do)
        # Only log when something actually changed, to keep the journal quiet on every push.
        if res.get("changed"):
            logger.info("managed crontab reconciled — %d managed line(s)", res["managed_lines"])
        return res
    # Any failure is reported in the status dict rather than raised.
    except Exception as e:  # noqa: BLE001 — never let a crontab error crash the agent
        logger.warning("managed crontab apply failed: %s", e)
        return {"ok": False, "changed": False, "error": str(e)[:300],
                "managed_lines": _count_managed(desired)}


def _count_managed(desired: str) -> int:
    """Count non-blank, non-comment managed job lines (for status display)."""
    # Count lines that are real jobs: skip blanks and '#' comment lines.
    return sum(1 for ln in (desired or "").splitlines()
               if ln.strip() and not ln.strip().startswith("#"))


async def current_managed_crontab_status(desired: Optional[str]) -> Dict[str, Any]:
    """Telemetry status: how many managed jobs are configured and whether root's
    crontab currently matches (drift detection). Best-effort / read-only."""
    # Always report how many managed jobs the hub configured.
    status: Dict[str, Any] = {"configured_lines": _count_managed(desired or "")}
    # Read the live crontab off the event loop and check whether reconciling would change it.
    try:
        current = await asyncio.to_thread(_read_crontab_sync)
        status["in_sync"] = (reconcile_block(current, desired or "") == current)
    # Read failures are reported in the status instead of raising (telemetry must not crash).
    except Exception as e:  # noqa: BLE001
        status["error"] = str(e)[:200]
    # Return whatever we could determine.
    return status
