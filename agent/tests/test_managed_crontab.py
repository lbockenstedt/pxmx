"""Tests for the pure crontab block reconciler (managed_crontab.reconcile_block).

The reconciler must (a) preserve the operator's own crontab entries, (b) keep the
LM-MANAGED block in sync with the pushed content, (c) be idempotent, and (d)
remove the block when the desired content is cleared — without ever dropping
non-managed lines.
"""
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("pxmx_agent_src")
_pkg.__path__ = [str(SRC)]
sys.modules.setdefault("pxmx_agent_src", _pkg)
_spec = importlib.util.spec_from_file_location(
    "pxmx_agent_src.managed_crontab", SRC / "managed_crontab.py")
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

B, E = mc.BEGIN_MARKER, mc.END_MARKER


def test_insert_block_into_empty_crontab():
    out = mc.reconcile_block("", "*/5 * * * * /usr/bin/backup.sh")
    assert B in out and E in out
    assert "*/5 * * * * /usr/bin/backup.sh" in out
    assert out.endswith("\n")


def test_preserves_operator_entries():
    existing = "# my own job\n0 3 * * * /root/nightly.sh\n"
    out = mc.reconcile_block(existing, "*/10 * * * * /usr/bin/check.sh")
    # Operator's line survives, managed block is appended after it.
    assert "0 3 * * * /root/nightly.sh" in out
    assert out.index("0 3 * * * /root/nightly.sh") < out.index(B)
    assert "*/10 * * * * /usr/bin/check.sh" in out


def test_replaces_existing_managed_block_not_operator_lines():
    first = mc.reconcile_block("0 3 * * * /root/nightly.sh\n", "1 1 * * * /a.sh")
    second = mc.reconcile_block(first, "2 2 * * * /b.sh")
    assert "0 3 * * * /root/nightly.sh" in second   # operator line preserved
    assert "2 2 * * * /b.sh" in second               # new managed content
    assert "1 1 * * * /a.sh" not in second           # old managed content replaced
    assert second.count(B) == 1 and second.count(E) == 1


def test_idempotent():
    existing = "0 3 * * * /root/nightly.sh\n"
    once = mc.reconcile_block(existing, "*/5 * * * * /x.sh")
    twice = mc.reconcile_block(once, "*/5 * * * * /x.sh")
    assert once == twice


def test_empty_desired_removes_block_keeps_operator_lines():
    with_block = mc.reconcile_block("0 3 * * * /root/nightly.sh\n", "*/5 * * * * /x.sh")
    cleared = mc.reconcile_block(with_block, "")
    assert B not in cleared and E not in cleared
    assert "0 3 * * * /root/nightly.sh" in cleared
    assert "*/5 * * * * /x.sh" not in cleared


def test_empty_desired_and_no_operator_lines_yields_empty():
    with_block = mc.reconcile_block("", "*/5 * * * * /x.sh")
    cleared = mc.reconcile_block(with_block, "")
    assert cleared == ""


def test_corrupted_block_missing_end_marker_does_not_strand_lines():
    # A hand-mangled block (BEGIN but no END) must be fully replaced, not leave
    # the old managed lines behind as un-managed crontab entries.
    corrupted = f"0 3 * * * /root/nightly.sh\n{B}\n9 9 * * * /old.sh\n"
    out = mc.reconcile_block(corrupted, "1 1 * * * /new.sh")
    assert "9 9 * * * /old.sh" not in out
    assert "1 1 * * * /new.sh" in out
    assert "0 3 * * * /root/nightly.sh" in out
    assert out.count(B) == 1 and out.count(E) == 1


def test_blank_and_comment_lines_in_desired_are_kept_but_not_counted():
    desired = "# a comment\n\n*/5 * * * * /x.sh\n"
    out = mc.reconcile_block("", desired)
    assert "# a comment" in out
    assert mc._count_managed(desired) == 1  # only the real job line counts
