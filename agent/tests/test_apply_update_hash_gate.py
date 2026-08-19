#!/usr/bin/env python3
"""Self-test for _apply_update's deploy-decision gate (commit hash, not VERSION).

Run:  python3 agent/tests/test_apply_update_hash_gate.py

agent.py cannot be imported directly (heavy transitive imports for the full
agent runtime), so this extracts the SOURCE of _apply_update via ast and execs
it with subprocess/update_recovery mocked out entirely.

SAFETY: _apply_update ends in os._exit(3) on its real success path — reaching
that in a test would silently kill the test process. Every case here is
designed to return EARLY, either via the hash-unchanged short-circuit or via
the pre-existing "snapshot failed -> abort" path (triggered deliberately by
making the mocked update_recovery.snapshot_code raise), so the destructive
file-swap/restart code is NEVER reached. os._exit itself is also stubbed to
raise instead of actually exiting, as a second line of defense.

Regression guard: the OLD gate compared VERSION file STRINGS (new_ver ==
current) instead of commit hashes. VERSION is bumped by a SEPARATE CI job on
push — if that job ever falls behind a real commit (or is retired), the old
gate read "unchanged" forever even though `git pull` kept succeeding, so the
agent silently stopped deploying real code changes with no error. The gate is
now based on `git rev-parse HEAD` before/after the pull, matching ab/lm.
"""
import ast


class _KilledSelfExit(Exception):
    """Raised by the stubbed os._exit so a test that WOULD have exited fails
    loudly and distinctly instead of silently terminating the process."""


def _load_apply_update(git_rev_parse_sequence, pull_should_raise=False):
    src = open("agent/src/agent.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ProxmoxAgent":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_apply_update":
                    seg = ast.get_source_segment(src, item)
    assert seg, "_apply_update not found on ProxmoxAgent"
    # Dedent the method body (it's indented as a class member) so exec sees a
    # bare top-level function definition.
    lines = seg.split("\n")
    indent = len(lines[0]) - len(lines[0].lstrip())
    seg = "\n".join(l[indent:] if l.strip() else l for l in lines)

    calls = {"rev_parse_n": 0, "pull_called": False, "snapshot_called": False,
             "swap_reached": False, "exit_reached": False}

    class _FakeSubprocess:
        DEVNULL = None

        @staticmethod
        def check_output(argv, timeout=None):
            if "rev-parse" in argv:
                idx = calls["rev_parse_n"]
                calls["rev_parse_n"] += 1
                val = git_rev_parse_sequence[min(idx, len(git_rev_parse_sequence) - 1)]
                return (val + "\n").encode()
            return b""

        @staticmethod
        def check_call(argv, timeout=None, stdout=None):
            if "pull" in argv:
                calls["pull_called"] = True
                if pull_should_raise:
                    raise RuntimeError("simulated pull failure")
            return 0

    class _FakePathlibPath:
        def __init__(self, p):
            self._p = str(p)
        def __truediv__(self, other):
            return _FakePathlibPath(self._p + "/" + str(other))
        def exists(self):
            # A real VERSION file IS present, reading the SAME string as
            # get_version() — this is the scenario the regression guards
            # against: VERSION unchanged (CI bump lagging/retired) while the
            # commit hash genuinely advanced.
            return True
        def read_text(self):
            return "0.98"
        def is_file(self):
            return False
        def __str__(self):
            return self._p

    class _FakePathlib:
        Path = _FakePathlibPath

    class _FakeUpdateRecovery:
        @staticmethod
        def is_version_bad(v, state_dir=None):
            return False
        @staticmethod
        def snapshot_code(*a, **k):
            calls["snapshot_called"] = True
            # Deliberately abort here — this is the EXISTING pre-swap safety
            # path (_apply_update already returns if snapshot fails), so the
            # test never reaches the destructive swap/restart code below it.
            raise RuntimeError("simulated snapshot failure (deliberate test abort point)")
        @staticmethod
        def clear_pending(state_dir=None):
            pass

    def _fake_exit(code):
        calls["exit_reached"] = True
        raise _KilledSelfExit(f"os._exit({code}) would have fired")

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    def _fake_get_version():
        return "0.98"

    ns = {
        "subprocess": _FakeSubprocess, "shutil": None, "pathlib": _FakePathlib,
        "os": type("FakeOs", (), {"_exit": staticmethod(_fake_exit),
                                   "getpid": staticmethod(lambda: 1)})(),
        "time": __import__("time"), "logger": _NoLog(),
        "get_version": _fake_get_version, "AGENT_STATE_DIR": "/tmp/fake-state",
        "update_recovery": _FakeUpdateRecovery,
    }
    # _apply_update does `from . import update_recovery as ur` — rewrite that
    # relative import to pull the fake module out of ns instead.
    seg = seg.replace("from . import update_recovery as ur",
                       "ur = update_recovery")
    # The function also does a LOCAL `import subprocess, shutil, pathlib` as
    # its first statement — a real import there would shadow the fakes placed
    # in ns above (module globals only apply until something re-imports the
    # same name locally). Strip it so the fakes actually get used.
    seg = seg.replace("import subprocess, shutil, pathlib", "pass")
    exec(seg, ns)
    return ns["_apply_update"], calls


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True

    # 1. Hash UNCHANGED after pull -> must return immediately, no snapshot
    #    attempt, no restart. (Old bug: this used to be gated on VERSION
    #    string equality instead — this case doesn't distinguish old-vs-new
    #    behavior by itself, but establishes the short-circuit still works.)
    fn, calls = _load_apply_update(["abc1234", "abc1234"])
    try:
        fn(None, "/fake/install", "/fake/repo")
    except Exception as e:
        ok &= _check("hash unchanged: no exception raised", False)
        print(f"    unexpected: {e}")
    else:
        ok &= _check("hash unchanged: pull was attempted", calls["pull_called"])
        ok &= _check("hash unchanged: returns before snapshot/swap",
                     not calls["snapshot_called"] and not calls["exit_reached"])

    # 2. Hash CHANGED after pull -> must proceed PAST the gate (reaches the
    #    snapshot step, which we've made deliberately fail to abort safely
    #    before the real swap/restart).
    fn2, calls2 = _load_apply_update(["abc1234", "def5678"])
    try:
        fn2(None, "/fake/install", "/fake/repo")
    except _KilledSelfExit:
        ok &= _check("hash changed: os._exit was reached (should NOT happen "
                     "in this test — snapshot abort should have fired first)", False)
    except Exception:
        pass  # expected: the deliberate simulated snapshot failure
    ok &= _check("hash changed: pull was attempted", calls2["pull_called"])
    ok &= _check("hash changed: proceeded PAST the gate to the snapshot step "
                 "(this is the actual regression guard — the OLD code would "
                 "have returned here if VERSION hadn't also changed)",
                 calls2["snapshot_called"])
    ok &= _check("hash changed: os._exit was never reached (test stayed safe)",
                 not calls2["exit_reached"])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running _apply_update hash-gate self-test...")
    import sys
    sys.exit(main())
