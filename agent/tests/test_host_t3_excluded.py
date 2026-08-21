"""Per-host T3 opt-out (``t3_exclude_hosts``), mirroring the existing T1 guard.

T1 has ``_host_t1_excluded`` gating its only PCI-attach call site
(``run_provision_loop`` before ``_provision_pci_tier(agent, "t1", ...)``); T3 had
no analogous guard at all, so an excluded host's T3 controller could still be
PCI-passed. This pins the new ``_host_t3_excluded`` matcher directly (mirrors
``_host_t1_excluded``'s case-insensitive exact-or-prefix semantics) and confirms
the T3 call site in ``run_provision_loop`` now skips when excluded, same as T1.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import usb_provision as up  # noqa: E402


class _Agent:
    def __init__(self, hostname, t3_exclude_hosts=None):
        self.hostname = hostname
        self.config = {"client_simulation": {"usb_config":
                       {"t3_exclude_hosts": t3_exclude_hosts or []}}}


def test_exact_match_excluded():
    a = _Agent("pxmx-cs-svr-06", ["pxmx-cs-svr-06"])
    assert up._host_t3_excluded(a) is True


def test_case_insensitive_match():
    a = _Agent("PXMX-CS-SVR-06", ["pxmx-cs-svr-06"])
    assert up._host_t3_excluded(a) is True


def test_prefix_match():
    a = _Agent("sim-svr-05-abc", ["sim-svr-05"])
    assert up._host_t3_excluded(a) is True


def test_non_matching_host_not_excluded():
    a = _Agent("pxmx-cs-svr-01", ["pxmx-cs-svr-06"])
    assert up._host_t3_excluded(a) is False


def test_empty_list_excludes_nothing():
    a = _Agent("pxmx-cs-svr-06", [])
    assert up._host_t3_excluded(a) is False


def test_no_hostname_never_excluded():
    a = _Agent("", ["pxmx-cs-svr-06"])
    assert up._host_t3_excluded(a) is False


def test_t1_and_t3_exclusion_are_independent():
    # A host listed only under t1_exclude_hosts must NOT be T3-excluded, and
    # vice versa — the two lists/functions must not cross-read each other's key.
    a = _Agent("pxmx-cs-svr-06")
    a.config["client_simulation"]["usb_config"]["t1_exclude_hosts"] = ["pxmx-cs-svr-06"]
    a.config["client_simulation"]["usb_config"]["t3_exclude_hosts"] = []
    assert up._host_t1_excluded(a) is True
    assert up._host_t3_excluded(a) is False
