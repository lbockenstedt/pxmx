"""``pve_cmds._run`` returns BYTES — callers must decode before text work.

Two live bugs, both this shape, both found from one host log line:

1. ``usb_diagnostics.kernel_usb_events`` did ``"usb" not in line.lower()`` on a
   bytes line ->  TypeError: a bytes-like object is required, not 'str'.
   collect() aborted every cycle, so USB_DIAGNOSTICS was EMPTY on every host
   while usb_presence.json kept updating (record_presence runs first).

       usb_diagnostics.collect failed: a bytes-like object is required, not 'str'

2. ``usb_quarantine.scan_dmesg_usb_errors`` matched a STR regex against bytes
   lines -> TypeError: cannot use a string pattern on a bytes-like object.
   This one had been there since the function was written and was swallowed by
   the caller's `except Exception` at DEBUG level, so the ONLY automatic
   quarantine path silently never fired.

These feed BYTES (what _run really returns) so a regression is caught, and also
str, so a future _run that decodes internally doesn't break the callers.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_bd")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_bd"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_bd.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_bd.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_state = _load("usb_state_store", "usb_state_store.py")
_uq = _load("usb_quarantine", "usb_quarantine.py")
_ud = _load("usb_diagnostics", "usb_diagnostics.py")

# Hermetic state: kernel_usb_events now ACCUMULATES into a persisted file, so
# without redirecting it these tests would read/write the real /var/lib/pxmx and
# leak counts between runs.
import tempfile as _tf
_ud.PXMLIB = _tf.mkdtemp()
_ud.USB_PRESENCE_FILE = f"{_ud.PXMLIB}/usb_presence.json"
_ud.USB_BOOT_FILE = f"{_ud.PXMLIB}/usb_boot_baseline.json"
_ud.USB_KERNEL_FILE = f"{_ud.PXMLIB}/usb_kernel_events.json"


def _reset_kernel_state():
    """Each kernel test must start from an empty accumulator."""
    import os as _os
    try:
        _os.remove(_ud.USB_KERNEL_FILE)
    except OSError:
        pass

KERNEL_LOG = (
    "usb 3-1.2: device descriptor read/64, error -71\n"
    "usb 3-1.2: unable to enumerate USB device\n"
    "usb 4-1: USB disconnect, device number 7\n"
    "hub 2-0:1.0: over-current condition on port 3\n"
    "kernel: something unrelated\n"
)


class _FakePve:
    """Stands in for pve_cmds, returning stdout in the requested form."""

    def __init__(self, payloads, as_bytes=True):
        self._payloads = payloads
        self._as_bytes = as_bytes

    async def _run(self, argv, check=False, timeout=None, env=None):
        key = argv[0]
        text = self._payloads.get(key, "")
        if self._as_bytes:
            return 0, text.encode(), b""
        return 0, text, ""


def _install(mod, pve):
    sys.modules["pxmx_agent_src_bd.pve_cmds"] = pve


# ── usb_diagnostics.kernel_usb_events ────────────────────────────────────────

def _kernel(as_bytes):
    _reset_kernel_state()
    _install(_ud, _FakePve({"journalctl": KERNEL_LOG}, as_bytes))
    return asyncio.run(_ud.kernel_usb_events(3600))


def test_kernel_scan_handles_bytes_stdout():
    """The reported failure: bytes stdout must not abort the collector."""
    out = _kernel(as_bytes=True)
    assert out["available"] is True
    assert out["totals"].get("link_error") == 2
    assert out["totals"].get("disconnect") == 1
    assert out["totals"].get("over_current") == 1


def test_kernel_scan_still_handles_str_stdout():
    assert _kernel(as_bytes=False)["totals"].get("link_error") == 2


def test_kernel_scan_attributes_errors_to_the_right_bus():
    out = _kernel(as_bytes=True)
    assert out["by_bus"]["3-1.2"]["link_error"] == 2
    assert out["by_bus"]["4-1"]["disconnect"] == 1


def test_kernel_scan_keeps_sample_lines_as_text():
    """Samples ride telemetry as JSON — bytes would not serialise."""
    for s in _kernel(as_bytes=True)["samples"]:
        assert isinstance(s, str)


def test_unreadable_journal_reports_unavailable_not_clean():
    class _Fail:
        async def _run(self, *a, **k):
            raise OSError("journalctl missing")
    _install(_ud, _Fail())
    out = asyncio.run(_ud.kernel_usb_events(3600))
    assert out["available"] is False and out["totals"] == {}


# ── usb_diagnostics.uhubctl_support ──────────────────────────────────────────

UHUBCTL = ("Current status for hub 1-1 [05e3:0608 GenesysLogic USB2.1 Hub, "
           "USB 2.10, 4 ports, ppps]\n"
           "  Port 1: 0503 power highspeed enable connect\n")


def test_uhubctl_parses_bytes_output():
    _install(_ud, _FakePve({"uhubctl": UHUBCTL}, as_bytes=True))
    out = asyncio.run(_ud.uhubctl_support())
    assert out["installed"] is True
    assert out["supported"] is True
    assert out["ppps_hubs"] and out["ppps_hubs"][0]["hub"] == "1-1"
    assert isinstance(out["version"], str)      # JSON-serialisable


def test_uhubctl_absent_is_reported_not_crashed():
    class _Missing:
        async def _run(self, *a, **k):
            raise FileNotFoundError("uhubctl")
    _install(_ud, _Missing())
    out = asyncio.run(_ud.uhubctl_support())
    assert out["installed"] is False and out["supported"] is False


def test_uhubctl_without_ppps_is_installed_but_unsupported():
    ganged = "Current status for hub 2-1 [1d6b:0002 Linux Foundation, ganged]\n"
    _install(_ud, _FakePve({"uhubctl": ganged}, as_bytes=True))
    out = asyncio.run(_ud.uhubctl_support())
    assert out["installed"] is True and out["supported"] is False


# ── usb_quarantine.scan_dmesg_usb_errors (the silent, long-standing one) ─────

def test_dmesg_quarantine_scan_handles_bytes():
    """Had ALWAYS raised TypeError here, so quarantine never fired."""
    _install(_uq, _FakePve({"journalctl": KERNEL_LOG}, as_bytes=True))
    errs = asyncio.run(_uq.scan_dmesg_usb_errors(180))
    assert errs == {"3-1.2": 2}


def test_dmesg_quarantine_scan_still_handles_str():
    _install(_uq, _FakePve({"journalctl": KERNEL_LOG}, as_bytes=False))
    assert asyncio.run(_uq.scan_dmesg_usb_errors(180)) == {"3-1.2": 2}


def test_dmesg_quarantine_scan_reaches_the_max_fails_threshold():
    """Three errors on one bus is what QUARANTINE_MAX_FAILS acts on — proving
    the scan can actually produce a quarantine-worthy count now."""
    log = "usb 5-2: error -110\n" * 3
    _install(_uq, _FakePve({"journalctl": log}, as_bytes=True))
    errs = asyncio.run(_uq.scan_dmesg_usb_errors(180))
    assert errs["5-2"] >= _uq._DMESG_USB_QUARANTINE_MIN


def test_dmesg_scan_empty_log_is_empty_not_an_error():
    _install(_uq, _FakePve({"journalctl": ""}, as_bytes=True))
    assert asyncio.run(_uq.scan_dmesg_usb_errors(180)) == {}


# ── the decode helper itself ─────────────────────────────────────────────────

def test_text_helper_round_trips():
    assert _ud._text(b"hello") == "hello"
    assert _ud._text("hello") == "hello"
    assert _ud._text(None) == ""
    assert _ud._text(b"\xff\xfe bad utf8") .endswith("bad utf8")   # never raises
