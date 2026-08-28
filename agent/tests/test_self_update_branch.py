#!/usr/bin/env python3
"""The agent must self-update from the branch it is DEPLOYED on, not "main".

Run:  python3 -m pytest agent/tests/test_self_update_branch.py

The agent self-updates by pulling its own checkout. It previously hardcoded
``origin/main`` in the behind-count, the fetch and the remote-hash lookup, so a
host deployed on a dev or qa branch would be dragged back onto main on the next
update sweep -- silently replacing the code under test with main's code.

agent.py cannot be imported directly (heavy transitive imports for the full
agent runtime), so this extracts _repo_branch's SOURCE via ast, matching the
approach in test_apply_update_hash_gate.py.
"""
import ast

_SRC = "agent/src/agent.py"


def _load_repo_branch(rev_parse_result):
    """Exec _repo_branch with subprocess.check_output stubbed."""
    tree = ast.parse(open(_SRC).read())
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ProxmoxAgent":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_repo_branch":
                    fn = item
    assert fn is not None, "_repo_branch not found on ProxmoxAgent"

    class _FakeSub:
        DEVNULL = -3

        @staticmethod
        def check_output(*a, **k):
            if isinstance(rev_parse_result, Exception):
                raise rev_parse_result
            return rev_parse_result

    import sys
    import types
    fake = types.ModuleType("subprocess")
    fake.check_output = _FakeSub.check_output
    fake.DEVNULL = _FakeSub.DEVNULL
    saved = sys.modules.get("subprocess")
    sys.modules["subprocess"] = fake
    try:
        cls = ast.ClassDef(name="A", bases=[], keywords=[], body=[fn],
                           decorator_list=[])
        ns = {}
        mod = ast.Module(body=[cls], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), "<x>", "exec"), ns)
        return ns["A"]()._repo_branch("/opt/pxmx")
    finally:
        if saved is not None:
            sys.modules["subprocess"] = saved
        else:
            del sys.modules["subprocess"]


def test_dev_checkout_tracks_dev():
    assert _load_repo_branch(b"dev\n") == "dev"


def test_qa_checkout_tracks_qa():
    assert _load_repo_branch(b"qa\n") == "qa"


def test_main_checkout_still_tracks_main():
    assert _load_repo_branch(b"main\n") == "main"


def test_detached_head_falls_back_to_main():
    """rev-parse --abbrev-ref prints the literal "HEAD" when detached; that is
    not a branch and must not be used as a remote ref."""
    assert _load_repo_branch(b"HEAD\n") == "main"


def test_empty_output_falls_back_to_main():
    assert _load_repo_branch(b"\n") == "main"


def test_git_failure_falls_back_to_main():
    assert _load_repo_branch(OSError("git missing")) == "main"


def test_no_hardcoded_origin_main_remains_in_self_update():
    """The regression itself: any literal origin/main in the self-update path
    would re-pin a dev/qa host to main."""
    src = open(_SRC).read()
    assert "origin/main" not in src, (
        "self-update still references origin/main; a dev/qa deployment would be "
        "reset onto main")
