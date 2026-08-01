"""A dongle whose radio never initialised must be quarantined, and a healthy one
must never be.

Context: an Archer T2U Nano (2357:011e) failed in a way the health ladder could
not name. Its driver bound cleanly, its netdev reached UP,LOWER_UP, and it still
refused every scan -- indistinguishable from "no APs in range" to the existing
probe set, which is exactly the ambiguity that forces the ladder to be slow and
sim-aware.

The two signals that break the tie are impossible transmit power (RF never came
up) and a scan the driver REFUSES. Neither is producible by a failure sim, which
is what makes them safe to act on immediately.

The risk in the other direction is what most of these cases guard: some Realtek
out-of-tree drivers report a bogus txpower on perfectly good hardware, so the
hint must never be able to quarantine a dongle on its own.

_classify_guest_health is extracted from source rather than imported, matching
the other agent tests -- importing the module pulls in the whole pve/QGA stack.
"""
import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "usb_provision.py"


def _load(fn_name):
    tree = ast.parse(_SRC.read_text())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    assert fn, f"could not locate {fn_name} in usb_provision.py — source shape changed?"
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<f>", "exec"), ns)
    return ns[fn_name]


classify = _load("_classify_guest_health")


def _probe(**kw):
    base = {"netdev_present": True, "associated": False,
            "sees_aps": False, "radio_dead_hint": False, "gateway_ok": True}
    base.update(kw)
    return base


def test_dead_radio_is_classified_radio_dead():
    """The observed failure: netdev up, sees nothing, impossible txpower."""
    assert classify(_probe(radio_dead_hint=True)) == "radio_dead"


def test_bogus_txpower_does_not_condemn_a_dongle_that_sees_aps():
    """THE false-positive guard. Several Realtek out-of-tree drivers report a
    nonsense txpower while working fine. A radio that can SEE access points is
    demonstrably alive, so the hint must be ignored — otherwise every client on
    such a driver would be quarantined."""
    assert classify(_probe(sees_aps=True, radio_dead_hint=True)) == "no_assoc"


def test_no_aps_without_the_hint_stays_on_the_normal_ladder():
    """No APs but sane txpower is still ambiguous (dead RF corner, downed SSID),
    so it must keep the slower no_scan handling rather than jumping to
    quarantine."""
    assert classify(_probe()) == "no_scan"


def test_assoc_fail_sim_shape_is_untouched():
    """A deliberate failure sim leaves the radio seeing APs but unassociated.
    That must remain no_assoc, which is the state the sim-aware hold protects."""
    assert classify(_probe(sees_aps=True)) == "no_assoc"


@pytest.mark.parametrize("probe,expected", [
    (None, "unknown"),
    ({"netdev_present": False}, "no_driver"),
    ({"netdev_present": True, "associated": True, "gateway_ok": False}, "no_gateway"),
    ({"netdev_present": True, "associated": True, "gateway_ok": True}, "healthy"),
])
def test_existing_states_unchanged(probe, expected):
    """The new branch must not perturb any pre-existing classification."""
    assert classify(probe) == expected


def test_radio_dead_never_reached_without_netdev():
    """No netdev means no radio to judge — must stay no_driver (a reboot fixes
    that case; quarantine would be wrong)."""
    assert classify({"netdev_present": False, "radio_dead_hint": True}) == "no_driver"


def test_hint_key_absent_behaves_as_false():
    """Older agents / cached probes predate radio_dead_hint. A missing key must
    degrade to the previous behaviour, not crash or condemn."""
    p = {"netdev_present": True, "associated": False, "sees_aps": False}
    assert classify(p) == "no_scan"


def test_probe_collects_the_hint_readonly():
    """radio_dead_hint must stay in the READ-ONLY probe set: it runs fleet-wide,
    and an active scan there would disrupt every working client. Assert it uses
    `iw dev ... info` and never triggers a scan."""
    tree = ast.parse(_SRC.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_guest_health_probe")
    # Read the literal COMMAND out of the checks dict. Scanning raw source would
    # also match the word "scan" in the surrounding comments.
    hint = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "radio_dead_hint":
                hint = ast.literal_eval(v)
    assert hint, "probe no longer collects radio_dead_hint"
    assert "iw dev" in hint and "info" in hint, \
        "hint should read `iw dev ... info` (txpower)"
    assert "scan" not in hint, \
        "hint must NOT trigger a scan — that would disrupt every working client"
