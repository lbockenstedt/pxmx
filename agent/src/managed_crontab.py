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
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("PxmxAgent")

BEGIN_MARKER = "# BEGIN LM-MANAGED (do not edit — managed by Lab Manager)"
END_MARKER = "# END LM-MANAGED"


def _strip_block(text: str) -> str:
    """Return ``text`` with any existing LM-MANAGED block (and the surrounding
    blank line we add) removed. Tolerant of a hand-mangled block: removes from
    the first BEGIN marker to the next END marker; a BEGIN with no END removes
    to end-of-file (so a corrupted block can't strand un-managed lines)."""
    lines = (text or "").splitlines()
    out = []
    i = 0
    n = len(lines)
    while i < n:
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
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def reconcile_block(existing: str, desired: str) -> str:
    """Return root's crontab text with the LM-MANAGED block set to ``desired``.

    - ``desired`` blank/empty → the block is removed (managed jobs cleared).
    - Otherwise the block is (re)written with ``desired``'s lines, appended after
      the operator's own entries. Existing non-managed lines are preserved.
    Output always ends with exactly one trailing newline (cron wants a final
    newline) unless the whole result is empty.
    """
    base = _strip_block(existing).rstrip("\n")
    desired_body = "\n".join(
        ln.rstrip() for ln in (desired or "").splitlines() if ln.strip() != ""
    ).strip("\n")

    if not desired_body:
        # No managed jobs — just the preserved base (may be empty).
        return (base + "\n") if base else ""

    block = f"{BEGIN_MARKER}\n{desired_body}\n{END_MARKER}"
    if base:
        return f"{base}\n\n{block}\n"
    return f"{block}\n"


def _read_crontab_sync() -> str:
    """Read root's current crontab (empty string when none is installed)."""
    import subprocess
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise RuntimeError("crontab binary not found")
    if p.returncode != 0:
        # "no crontab for <user>" → returncode 1 with that message → treat as empty.
        err = (p.stderr or "").lower()
        if "no crontab" in err:
            return ""
        raise RuntimeError((p.stderr or p.stdout or f"crontab -l exited {p.returncode}").strip())
    return p.stdout or ""


def _write_crontab_sync(text: str) -> None:
    """Install ``text`` as root's crontab (empty text removes the crontab)."""
    import subprocess
    if not text.strip():
        subprocess.run(["crontab", "-r"], capture_output=True, text=True, timeout=15)
        return
    p = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"crontab - exited {p.returncode}").strip())


async def apply_managed_crontab(desired: str) -> Dict[str, Any]:
    """Reconcile root's crontab so its LM-MANAGED block matches ``desired``.

    Idempotent: only writes when the text actually changes. Runs the blocking
    ``crontab`` calls in a thread so the event loop isn't stalled. Returns a
    status dict for logging + telemetry."""
    def _do() -> Dict[str, Any]:
        current = _read_crontab_sync()
        target = reconcile_block(current, desired)
        if target == current or (not target.strip() and not current.strip()):
            return {"ok": True, "changed": False, "managed_lines": _count_managed(desired)}
        _write_crontab_sync(target)
        return {"ok": True, "changed": True, "managed_lines": _count_managed(desired)}
    try:
        res = await asyncio.to_thread(_do)
        if res.get("changed"):
            logger.info("managed crontab reconciled — %d managed line(s)", res["managed_lines"])
        return res
    except Exception as e:  # noqa: BLE001 — never let a crontab error crash the agent
        logger.warning("managed crontab apply failed: %s", e)
        return {"ok": False, "changed": False, "error": str(e)[:300],
                "managed_lines": _count_managed(desired)}


def _count_managed(desired: str) -> int:
    """Count non-blank, non-comment managed job lines (for status display)."""
    return sum(1 for ln in (desired or "").splitlines()
               if ln.strip() and not ln.strip().startswith("#"))


async def current_managed_crontab_status(desired: Optional[str]) -> Dict[str, Any]:
    """Telemetry status: how many managed jobs are configured and whether root's
    crontab currently matches (drift detection). Best-effort / read-only."""
    status: Dict[str, Any] = {"configured_lines": _count_managed(desired or "")}
    try:
        current = await asyncio.to_thread(_read_crontab_sync)
        status["in_sync"] = (reconcile_block(current, desired or "") == current)
    except Exception as e:  # noqa: BLE001
        status["error"] = str(e)[:200]
    return status
