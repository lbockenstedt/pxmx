"""Runtime dependency self-heal — install missing venv deps at startup.

A component (hub / spoke / agent) can boot into a venv that's missing a declared
requirement: a skewed auto-update that pulled code but didn't finish ``pip
install`` (the hub forward path before lm ``1df455a``), a partial install, a
manually-wiped venv, or the bootstrap catch where a new dep lands via git pull
but the running process predates the dep-realign fix. The result is either a
warning (an optional dep like ``zeroconf`` guarded by ``try/except ImportError``)
or a hard crash at import (a required dep like ``websockets``).

``ensure_requirements`` is the safety net: at the very top of each entrypoint —
BEFORE the heavy third-party imports — it parses the component's
``requirements.txt``, checks each top-level package is importable via
``importlib.util.find_spec``, and if any are missing runs
``pip install -r requirements.txt`` in the current venv (``sys.executable``) so
the subsequent imports find the just-installed packages (site-packages is already
on ``sys.path``; no re-exec needed). When everything is present it does no I/O
and no network — the check is ~ms — so it's cheap to call on every boot.

This module is **stdlib-only** (``importlib.util, logging, os, re, subprocess,
sys``) so it loads even when every third-party dep is missing. It is vendored
verbatim into the standalone pxmx agent (``pxmx/agent/src/dep_guard.py``) which
has no lm-core dependency — keep the two copies in sync. Best-effort by design:
a pip failure (air-gapped box, PyPI unreachable, venv not writable) is logged
and the function returns False; it NEVER raises, so optional deps' own graceful
``try/except ImportError`` still applies and a hard dep surfaces as a normal
ImportError on the caller's subsequent import (which systemd ``Restart=always``
will retry — and the next boot's guard will retry the install).
"""

import importlib.util
import logging
import os
import re
import subprocess
import sys
from typing import List, Optional

logger = logging.getLogger("DepGuard")

# pip distribution name → import name, for the cases where they differ. Anything
# not listed uses the default heuristic (strip extras/version, replace "-" with
# "_"). A wrong guess is harmless: find_spec returns None → we trigger a
# `pip install -r` that is a no-op when the dep is actually satisfied, then the
# caller's real import (under the correct name) succeeds.
_IMPORT_NAME = {
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
    "pyyaml3": "yaml",
    "dnspython": "dns",
    "beautifulsoup4": "bs4",
    "pillow": "PIL",
    "python-dateutil": "dateutil",
    "protobuf": "google.protobuf",
}


def _import_name_for(pip_name: str) -> str:
    """Map a requirements.txt line's pip name to its top-level import name."""
    base = pip_name.split("[", 1)[0]            # drop [extras]
    base = re.split(r"[<>=!~;\s]", base)[0]     # drop version specs / whitespace
    base = base.strip().strip(";").strip()
    if not base:
        return ""
    return _IMPORT_NAME.get(base.lower(), base.replace("-", "_"))


def _parse_requirements(path: str) -> List[str]:
    """Return the non-comment, non-flag requirement lines from ``path``."""
    out: List[str] = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    # skip comments, blank lines, and pip flags (-e, -r, --foo)
                    continue
                line = line.split("#", 1)[0].strip()   # strip inline comments
                if not line:
                    continue
                out.append(line)
    except Exception as e:
        logger.debug("could not read requirements %s: %s", path, e)
    return out


def _missing_imports(path: str) -> List[str]:
    """Requirement lines whose top-level import name is NOT importable."""
    missing: List[str] = []
    for spec_line in _parse_requirements(path):
        imp = _import_name_for(spec_line)
        if not imp:
            continue
        try:
            if importlib.util.find_spec(imp) is None:
                missing.append(spec_line)
        except Exception:
            # find_spec can raise for malformed names; treat as missing.
            missing.append(spec_line)
    return missing


def ensure_requirements(requirements_path: str, timeout: int = 300) -> bool:
    """Ensure every declared requirement is importable; self-heal if not.

    Returns True if all deps are importable (after any install), False if a dep
    is still missing after a best-effort install attempt. Never raises.

    Set ``LM_DEP_GUARD_DISABLE=1`` to skip entirely (operators who manage the
    venv out-of-band, and the test suite — importing ``main`` triggers this at
    module load, and a dev box missing e.g. ``zeroconf`` must not attempt a
    real ``pip install`` into the test interpreter).
    """
    if os.environ.get("LM_DEP_GUARD_DISABLE") == "1":
        return True
    if not os.path.isfile(requirements_path):
        # Nothing to check (component has no requirements.txt) — not an error.
        return True

    missing = _missing_imports(requirements_path)
    if not missing:
        return True

    logger.warning(
        "Missing venv deps (%s); running pip install -r %s to self-heal…",
        ", ".join(missing), requirements_path,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", requirements_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(
                "dep self-heal pip install rc=%d: %s",
                proc.returncode, (proc.stderr or proc.stdout).strip()[:500],
            )
    except Exception as e:
        logger.warning("dep self-heal pip install failed (continuing): %s", e)
        return False

    # Clear any stale None/Partial entries so the caller's subsequent imports
    # retry against the just-installed packages.
    for spec_line in missing:
        imp = _import_name_for(spec_line)
        if imp:
            sys.modules.pop(imp, None)

    still = _missing_imports(requirements_path)
    if still:
        logger.warning("Still missing after self-heal: %s", ", ".join(still))
        return False
    logger.info("dep self-heal installed missing deps.")
    return True


def _main() -> int:
    """CLI: ``python -m dep_guard <requirements.txt> [--timeout N]`` → exit 0
    if all deps importable (after any install), 1 otherwise. For ad-hoc checks."""
    import argparse

    parser = argparse.ArgumentParser(description="Self-heal missing venv deps.")
    parser.add_argument("requirements", help="path to requirements.txt")
    parser.add_argument("--timeout", type=int, default=300,
                        help="pip install timeout in seconds (default 300)")
    args = parser.parse_args()
    return 0 if ensure_requirements(args.requirements, args.timeout) else 1


if __name__ == "__main__":
    sys.exit(_main())