"""Hub / spoke / agent update-recovery helpers.

Gives every update path a pre-swap code snapshot, a post-restart health gate,
and a bad-version / bad-commit registry so a broken update can be rolled back
instead of leaving the component dark:

- **Hub auto path** — ``Hub.perform_update`` (``main.py``) snapshots the code
  before a ``git pull``/tarball swap, writes a "pending update" manifest, then
  hands off to the root-run ``lm-update-restart`` helper (installed by
  ``install_all.sh``) which restarts the hub, polls ``/status``, and restores
  the snapshot if the new version won't boot.
- **Hub manual path** — ``install_all.sh`` snapshots before its destructive
  ``rm -rf core WebUI`` and, on the 60s ``/status`` poll failure, restores the
  snapshot inline (it already runs as root with the hub stopped).
- **Spoke / agent path** — ``BaseControlPlane._snapshot_and_prepare_restart``
  (cs + pxmx spokes) and ``ProxmoxAgent._apply_update`` (standalone pxmx agent)
  snapshot before their git-pull / file-copy swap, write a pending manifest,
  and hand off to the root-run ``lm-component-update-restart`` watchdog which
  checks a ``healthy`` marker + ``systemctl`` state and rolls back if the new
  code won't boot. Spokes track bad **commit SHAs** (their update message
  carries no version); the hub tracks bad **versions**.

All recovery state lives under a component-specific state dir (the hub's
``/var/lib/lm/state``, a spoke's ``/var/lib/lm/<spoke_id>/``, the agent's
``/var/lib/pxmx/update-state`` in prod) so it survives code swaps and is
writable by the component process and readable/writable by the root helper.
The bash helpers re-implement the trivial JSON read/write with ``jq`` against
these SAME paths and formats — keep them in sync if you change anything here.
Pass ``state_dir=`` to any function to target a non-hub component; the CLI
takes ``--state-dir``.

Python 3.9/3.11 note: prod runs 3.11, the dev machine runs 3.9, so this module
uses ``Optional[X]``/``Set``/``Dict``/``List`` (not ``X | None`` / ``set[X]``)
— PEP 604 container syntax is 3.10+ and would break the dev ``py_compile``
check. This module is **vendored verbatim** into the standalone pxmx agent
(``pxmx/agent/src/update_recovery.py``) which has no lm-core dependency — keep
the two copies in sync.
"""
import argparse
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("UpdateRecovery")

# ── Paths / tunables ───────────────────────────────────────────────────────
# Default to the hub prod state dir; spokes/agents pass state_dir= explicitly
# (and the CLI passes --state-dir). Allow an override for tests / non-standard
# installs via LM_STATE_DIR.
STATE_DIR = os.environ.get("LM_STATE_DIR", "/var/lib/lm/state")

# Default trees a HUB code update swaps and the only ones that can break its
# boot. Spokes pass tree_list=["src"]; the agent passes its code dirs. Each
# entry is a path relative to the component root; the backup preserves it under
# its basename (core/src→src, WebUI→WebUI, dns→dns) so the bash helpers that
# check ``$bdir/src`` keep working.
DEFAULT_TREE_LIST: List[str] = ["core/src", "WebUI"]

# How long the root helper waits for the new version to reach /status 200 (hub)
# or the healthy marker to appear (spoke/agent), and how long it then waits for
# the rolled-back version to come back. Mirrors the 60s readiness poll
# install_all.sh already uses.
HEALTH_TIMEOUT = 60
ROLLBACK_TIMEOUT = 30
# Pre-swap snapshots retained on disk (newer ones win; oldest pruned).
KEEP_BACKUPS = 3


def _state_dir(state_dir: Optional[str]) -> str:
    """Resolve the state dir, falling back to the module default."""
    return state_dir if state_dir is not None else STATE_DIR


def _backup_root(state_dir: Optional[str] = None) -> str:
    return os.path.join(_state_dir(state_dir), "update-backup")


def _pending_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(_state_dir(state_dir), "pending_update.json")


def _bad_versions_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(_state_dir(state_dir), "bad_versions.json")


def _bad_commits_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(_state_dir(state_dir), "bad_commits.json")


def _failed_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(_state_dir(state_dir), "update_failed.json")


# Module-level path constants for the default (hub) state dir — kept for
# backward compat with any external importer and for the CLI's default chown.
BACKUP_ROOT = _backup_root()
PENDING_PATH = _pending_path()
BAD_VERSIONS_PATH = _bad_versions_path()
BAD_COMMITS_PATH = _bad_commits_path()
FAILED_PATH = _failed_path()


def _tree_basename(tree: str) -> str:
    """Backup subdir name for a tree path = its final segment (core/src→src)."""
    return os.path.basename(tree.rstrip("/").replace("\\", "/"))


# ── Version comparison ─────────────────────────────────────────────────────
def _ver_tuple(v: str):
    """Parse a dotted version string into a tuple of ints for comparison.

    Matches perform_update's own ``_ver`` helper. Non-numeric versions fall
    back to (0, 0, 0) so a malformed VERSION file never crashes the registry.
    """
    try:
        return tuple(int(x) for x in (v or "").strip().split("."))
    except Exception:
        return (0, 0, 0)


# ── Pre-swap snapshot ─────────────────────────────────────────────────────
def _ignore_special(directory: str, names) -> list:
    """shutil.copytree ignore hook: skip non-regular, non-symlink, non-dir
    entries (FIFOs / sockets / device files) that would make ``copytree``
    raise. Broken symlinks are preserved (``symlinks=True`` copies the link,
    it does not follow it, so a dead target is not a failure).
    """
    skip: list = []
    for n in names:
        p = os.path.join(directory, n)
        if os.path.islink(p) or os.path.isdir(p) or os.path.isfile(p):
            continue
        skip.append(n)
    return skip


def snapshot_code(hub_root: str, ts: str,
                  tree_list: Optional[List[str]] = None,
                  state_dir: Optional[str] = None) -> str:
    """Copy each tree in ``tree_list`` into ``<state_dir>/update-backup/<ts>/``.

    ``tree_list`` entries are paths relative to ``hub_root`` (the component
    root); each is copied under its basename (``core/src``→``src``,
    ``WebUI``→``WebUI``, ``dns``→``dns``) so the bash helpers that check
    ``$bdir/src`` keep working. Defaults to the hub's ``core/src`` + ``WebUI``.
    ``state_dir`` defaults to the hub state dir; spokes/agents pass their own.

    Returns the backup directory (absolute). The caller stamps ``ts`` (the
    component process may use the wall clock; workflow scripts must pass one in).

    Best-effort per tree: a ``copytree`` failure on one tree (a broken symlink
    the old default followed, a socket/fifo in ``core/src``, a permissions
    oddity) is logged but does NOT raise — a partial snapshot still gives the
    rollback path whatever it could capture, and the install/update continues
    instead of aborting.
    """
    trees = tree_list if tree_list is not None else DEFAULT_TREE_LIST
    broot = _backup_root(state_dir)
    os.makedirs(broot, exist_ok=True)
    backup_dir = os.path.join(broot, str(ts))
    # If a same-timestamp dir already exists (fast double-update), reuse it.
    os.makedirs(backup_dir, exist_ok=True)
    for tree in trees:
        src = os.path.join(hub_root, tree)
        if not os.path.isdir(src):
            continue
        name = _tree_basename(tree)
        # symlinks=True preserves links as-is (no follow → a broken link in the
        # existing tree is not a fatal FileNotFoundError); _ignore_special
        # drops special files copytree can't copy.
        try:
            shutil.copytree(src, os.path.join(backup_dir, name),
                            dirs_exist_ok=True, symlinks=True,
                            ignore=_ignore_special)
        except Exception as e:  # pragma: no cover - disk/fs errors only
            logger.warning("snapshot: copytree %s failed: %s", tree, e)
    logger.info("update snapshot saved to %s", backup_dir)
    return backup_dir


def prune_backups(keep: int = KEEP_BACKUPS,
                  state_dir: Optional[str] = None) -> int:
    """Delete oldest backups beyond ``keep`` (by directory mtime). Returns the
    number removed. Best-effort: never raises."""
    try:
        broot = _backup_root(state_dir)
        if not os.path.isdir(broot):
            return 0
        entries = [
            os.path.join(broot, d)
            for d in os.listdir(broot)
            if os.path.isdir(os.path.join(broot, d))
        ]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        removed = 0
        for stale in entries[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
            removed += 1
        return removed
    except Exception as e:  # pragma: no cover - disk/fs errors only
        logger.warning("prune_backups failed: %s", e)
        return 0


# ── Pending-update manifest ────────────────────────────────────────────────
def write_pending(backup_dir: str, from_version: str, to_version: str, ts: str,
                  state_dir: Optional[str] = None,
                  extra: Optional[Dict[str, Any]] = None) -> None:
    """Record the in-flight update so the root helper knows what to roll back.

    Present only between "snapshot taken" and "health verified / rolled back".
    The helper reads ``backup_dir`` + ``to_version`` + ``from_version`` here.
    ``extra`` carries spoke/agent-specific fields (``from_commit``,
    ``to_commit``, ``service_unit``, ``deadline``) alongside the common ones.
    """
    sd = _state_dir(state_dir)
    os.makedirs(sd, exist_ok=True)
    payload: Dict[str, Any] = {
        "backup_dir": backup_dir,
        "from_version": from_version,
        "to_version": to_version,
        "ts": ts,
    }
    if extra:
        payload.update(extra)
    with open(_pending_path(state_dir), "w") as f:
        json.dump(payload, f)
    logger.info("pending update manifest written: %s -> %s", from_version, to_version)


def read_pending(state_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the pending-update manifest, or None if none/invalid."""
    try:
        with open(_pending_path(state_dir)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("read_pending failed: %s", e)
        return None


def clear_pending(state_dir: Optional[str] = None) -> None:
    """Remove the pending manifest (success or rollback complete). Best-effort."""
    try:
        p = _pending_path(state_dir)
        if os.path.exists(p):
            os.remove(p)
    except Exception as e:  # pragma: no cover
        logger.warning("clear_pending failed: %s", e)


# ── Bad-version registry ──────────────────────────────────────────────────
def read_bad_versions(state_dir: Optional[str] = None) -> Set[str]:
    """Versions that failed to boot and were rolled back. The auto loop skips
    re-pulling any version in this set (until a newer remote version clears it)."""
    try:
        with open(_bad_versions_path(state_dir)) as f:
            data = json.load(f)
        return set(data.get("versions", []))
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.warning("read_bad_versions failed: %s", e)
        return set()


def _write_bad_versions(versions: Set[str],
                        state_dir: Optional[str] = None) -> None:
    os.makedirs(_state_dir(state_dir), exist_ok=True)
    with open(_bad_versions_path(state_dir), "w") as f:
        json.dump({"versions": sorted(versions)}, f, indent=2)


def add_bad_version(version: str, state_dir: Optional[str] = None) -> None:
    """Mark a version bad (it was rolled back after failing to boot)."""
    versions = read_bad_versions(state_dir)
    if version and version not in versions:
        versions.add(version)
        _write_bad_versions(versions, state_dir)
        logger.warning("marked version %s bad (failed to boot, rolled back)", version)


def is_version_bad(version: str, state_dir: Optional[str] = None) -> bool:
    return version in read_bad_versions(state_dir)


def clear_bad_versions_older_than(threshold: str,
                                  state_dir: Optional[str] = None) -> int:
    """Drop bad-version entries older than ``threshold`` — called when a newer
    remote version appears, so stale "don't pull 1.0.9" entries clear once the
    hub is moving forward to 1.0.10. Returns the number removed."""
    thresh = _ver_tuple(threshold)
    versions = read_bad_versions(state_dir)
    keep = {v for v in versions if _ver_tuple(v) >= thresh}
    removed = len(versions) - len(keep)
    if removed:
        _write_bad_versions(keep, state_dir)
        logger.info("cleared %d stale bad-version entries older than %s", removed, threshold)
    return removed


# ── Bad-commit registry (spokes / agent track commit SHAs, not versions) ───
def read_bad_commits(state_dir: Optional[str] = None) -> Set[str]:
    """Commit SHAs that failed to boot and were rolled back. A spoke/agent
    update path skips re-pulling any commit in this set."""
    try:
        with open(_bad_commits_path(state_dir)) as f:
            data = json.load(f)
        return set(data.get("commits", []))
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.warning("read_bad_commits failed: %s", e)
        return set()


def _write_bad_commits(commits: Set[str],
                       state_dir: Optional[str] = None) -> None:
    os.makedirs(_state_dir(state_dir), exist_ok=True)
    with open(_bad_commits_path(state_dir), "w") as f:
        json.dump({"commits": sorted(commits)}, f, indent=2)


def add_bad_commit(commit: str, state_dir: Optional[str] = None) -> None:
    """Mark a commit SHA bad (it was rolled back after failing to boot)."""
    commits = read_bad_commits(state_dir)
    if commit and commit not in commits:
        commits.add(commit)
        _write_bad_commits(commits, state_dir)
        logger.warning("marked commit %s bad (failed to boot, rolled back)", commit)


def is_bad_commit(commit: str, state_dir: Optional[str] = None) -> bool:
    return commit in read_bad_commits(state_dir)


# ── Double-failure marker ─────────────────────────────────────────────────
def write_update_failed(to_version: str, backup_dir: str, reason: str,
                        state_dir: Optional[str] = None) -> None:
    """Last-resort marker: the new version failed AND the rollback also failed
    to boot. Carries the bad version + backup location so an operator can
    recover manually (the backup is preserved on disk)."""
    os.makedirs(_state_dir(state_dir), exist_ok=True)
    with open(_failed_path(state_dir), "w") as f:
        json.dump(
            {"to_version": to_version, "backup_dir": backup_dir, "reason": reason},
            f,
            indent=2,
        )
    logger.error("update FAILED and rollback also failed: %s (backup at %s)", to_version, backup_dir)


# ── Snapshot restore (rollback) ────────────────────────────────────────────
def restore_snapshot(backup_dir: str, hub_root: str,
                     tree_list: Optional[List[str]] = None,
                     chown_user: Optional[str] = None) -> bool:
    """Restore a pre-swap snapshot back into the component root.

    Each tree in ``tree_list`` (default ``core/src`` + ``WebUI``) is restored
    from its basename subdir in ``backup_dir`` (``src``, ``WebUI``, …) into
    ``hub_root/<tree>``. The FIRST tree is the required one — if its backup
    subdir is absent the function returns False (no usable snapshot), matching
    the hub's ``src``-required semantics. The remaining trees are best-effort
    (a partial restore of an optional tree is logged, not fatal). Returns True
    if the required tree restored. ``chown_user`` recursively re-owns the
    restored trees to ``user:user`` (None = skip)."""
    trees = tree_list if tree_list is not None else DEFAULT_TREE_LIST
    if not backup_dir:
        return False
    names = [_tree_basename(t) for t in trees]
    # The first tree is the required one (hub: src; spoke: src; agent: code dir).
    if not os.path.isdir(os.path.join(backup_dir, names[0])):
        return False
    ok = False
    for tree, name in zip(trees, names):
        src_backup = os.path.join(backup_dir, name)
        if not os.path.isdir(src_backup):
            continue
        dst = os.path.join(hub_root, tree)
        # Wipe the failed-code tree before restoring (matches ``rm -rf`` in bash).
        shutil.rmtree(dst, ignore_errors=True)
        try:
            shutil.copytree(src_backup, dst, dirs_exist_ok=True)
            ok = True
        except Exception as e:  # best-effort per tree
            logger.warning("restore %s failed: %s", tree, e)
        if chown_user:
            _chown_tree(dst, chown_user)
    logger.info("snapshot restored from %s", backup_dir)
    return ok


def _chown_tree(path: str, user: str) -> None:
    """Recursively ``chown -R user:user`` (best-effort, mirrors the bash
    ``2>/dev/null || true`` semantics)."""
    try:
        subprocess.run(
            ["chown", "-R", "{0}:{0}".format(user), path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except Exception as e:  # pragma: no cover - chown missing / denied
        logger.warning("chown %s to %s failed: %s", path, user, e)


# ── CLI entrypoint ─────────────────────────────────────────────────────────
# Single source of truth for the on-disk recovery state machine. The bash
# blocks that previously re-implemented these JSON writes / cp / prune in jq
# (install_all.sh recovery_* helpers and the /usr/local/bin/lm-update-restart
# heredoc) now shell out here. State paths/formats are defined ABOVE in this
# module; the CLI only wires arguments to those functions. All subcommands are
# best-effort and exit 0 on success; ``rollback`` always exits 0 and reports
# success/failure in its JSON payload so callers under ``set -e`` can parse it.
#
# ``--state-dir`` targets a non-hub component (spoke / agent); ``--tree``
# (repeatable) overrides the default snapshot/rollback tree list.
#
# Usage:
#   python3 update_recovery.py snapshot   --hub-root R --from-version F --to-version T [--ts TS] [--tree T]... [--state-dir D] [--chown-user U]
#   python3 update_recovery.py rollback   --hub-root R --backup-dir D [--tree T]... [--state-dir D] [--chown-user U]
#   python3 update_recovery.py markbad    VERSION [--state-dir D] [--chown-user U]
#   python3 update_recovery.py markbadcommit SHA [--state-dir D] [--chown-user U]
#   python3 update_recovery.py clearpending [--state-dir D]
#   python3 update_recovery.py writefailed --to-version V --backup-dir D --reason R [--state-dir D] [--chown-user U]
#   python3 update_recovery.py prune      [--keep N] [--state-dir D]
def _cli_snapshot(args) -> int:
    ts = args.ts or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        bdir = snapshot_code(args.hub_root, ts,
                             tree_list=args.tree or None,
                             state_dir=args.state_dir)
    except Exception as e:
        logger.warning("snapshot_code failed: %s", e)
        print("", end="")
        return 1
    write_pending(bdir, args.from_version, args.to_version, ts,
                  state_dir=args.state_dir)
    if args.chown_user:
        _chown_tree(_state_dir(args.state_dir), args.chown_user)
    print(bdir)
    return 0


def _cli_rollback(args) -> int:
    # The caller (bash) reads the pending manifest itself — that preserves the
    # original log ordering (the "Rolling back..." line is printed BEFORE the
    # restore). This subcommand does ONLY the restore cp given an explicit
    # backup_dir, so bash keeps its exact flow and the cp is delegated here.
    bdir = args.backup_dir or ""
    ok = restore_snapshot(bdir, args.hub_root,
                          tree_list=args.tree or None,
                          chown_user=args.chown_user)
    payload = {"ok": ok, "backup_dir": bdir,
               "reason": "" if ok else "no snapshot; new version failed to boot"}
    json.dump(payload, sys.stdout)
    print("")
    return 0


def _cli_markbad(args) -> int:
    add_bad_version(args.version, state_dir=args.state_dir)
    if args.chown_user:
        try:
            subprocess.run(
                ["chown", "{0}:{0}".format(args.chown_user), _bad_versions_path(args.state_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except Exception:
            pass
    return 0


def _cli_markbadcommit(args) -> int:
    add_bad_commit(args.commit, state_dir=args.state_dir)
    if args.chown_user:
        try:
            subprocess.run(
                ["chown", "{0}:{0}".format(args.chown_user), _bad_commits_path(args.state_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except Exception:
            pass
    return 0


def _cli_clearpending(args) -> int:
    clear_pending(state_dir=args.state_dir)
    return 0


def _cli_writefailed(args) -> int:
    write_update_failed(args.to_version, args.backup_dir, args.reason,
                        state_dir=args.state_dir)
    if args.chown_user:
        try:
            subprocess.run(
                ["chown", "{0}:{0}".format(args.chown_user), _failed_path(args.state_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except Exception:
            pass
    return 0


def _cli_prune(args) -> int:
    removed = prune_backups(args.keep, state_dir=args.state_dir)
    print(removed)
    return 0


def main(argv=None) -> int:
    # Stdlib-only by design (runs during update recovery, before/around venv
    # deps are guaranteed), so we can't import core.src.logging_setup here. A
    # standalone run (incident operator) should still get the canonical format
    # with timestamps; otherwise logger.info is dropped (root defaults to
    # WARNING) and warning/error print without asctime.
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    parser = argparse.ArgumentParser(prog="update_recovery", description="Update-recovery state machine CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_state_dir(p):
        p.add_argument("--state-dir", default=None,
                       help="component state dir (default: hub /var/lib/lm/state)")

    p = sub.add_parser("snapshot", help="snapshot code trees and write pending manifest")
    p.add_argument("--hub-root", required=True)
    p.add_argument("--from-version", required=True)
    p.add_argument("--to-version", required=True)
    p.add_argument("--ts", default=None)
    p.add_argument("--tree", action="append", default=None,
                   help="tree relative to hub-root to snapshot (repeatable; default core/src WebUI)")
    _add_state_dir(p)
    p.add_argument("--chown-user", default=None)
    p.set_defaults(func=_cli_snapshot)

    p = sub.add_parser("rollback", help="restore a snapshot back into hub-root")
    p.add_argument("--hub-root", required=True)
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--tree", action="append", default=None,
                   help="tree to restore (repeatable; default core/src WebUI)")
    _add_state_dir(p)
    p.add_argument("--chown-user", default=None)
    p.set_defaults(func=_cli_rollback)

    p = sub.add_parser("markbad", help="mark a version bad (skip re-pull)")
    p.add_argument("version")
    _add_state_dir(p)
    p.add_argument("--chown-user", default=None)
    p.set_defaults(func=_cli_markbad)

    p = sub.add_parser("markbadcommit", help="mark a commit SHA bad (skip re-pull)")
    p.add_argument("commit")
    _add_state_dir(p)
    p.add_argument("--chown-user", default=None)
    p.set_defaults(func=_cli_markbadcommit)

    p = sub.add_parser("clearpending", help="clear the pending-update manifest")
    _add_state_dir(p)
    p.set_defaults(func=_cli_clearpending)

    p = sub.add_parser("writefailed", help="write the double-failure marker")
    p.add_argument("--to-version", required=True)
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--reason", required=True)
    _add_state_dir(p)
    p.add_argument("--chown-user", default=None)
    p.set_defaults(func=_cli_writefailed)

    p = sub.add_parser("prune", help="prune old snapshots (keep newest N)")
    p.add_argument("--keep", type=int, default=KEEP_BACKUPS)
    _add_state_dir(p)
    p.set_defaults(func=_cli_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())