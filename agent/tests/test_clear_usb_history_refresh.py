"""clear_usb_history must reflect the purge in telemetry IMMEDIATELY.

The WebUI missing-dongle panel renders the CACHED ``_last_usb_diag`` blob, which
``_slow_jobs_loop`` only recomputes every ``_USB_DIAG_INTERVAL_S`` (300s) — so
without an immediate scrub the operator's purge appears to do nothing for up to
five minutes, even on a page refresh (the "not deleting the dongles from the UI"
report). The handler therefore, after deleting the roster + boot baseline:

* zeroes the roster/baseline-derived loss fields on the cached diag blob (they
  are exactly what was just purged, so ``[]`` is accurate), and
* resets ``_last_usb_diag_ts`` so the next slow-jobs pass recomputes from what
  is actually attached within ~15s rather than up to 300s later.

Synthetic-package loader mirrors test_usb_diagnostics so the module's relative
imports resolve.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_cc")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_cc"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_cc.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_cc.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


cs_commands = _load("cs_commands", "cs_commands.py")
usb_diagnostics = _load("usb_diagnostics", "usb_diagnostics.py")


class _FakeAgent:
    def __init__(self, diag, ts):
        self.cs_enabled = True
        self.config = {"client_simulation": {}}
        self._last_usb_diag = diag
        self._last_usb_diag_ts = ts


def _populated_diag():
    return {
        "generated_at": 111.0,
        "present_count": 2,
        "known_count": 6,
        "missing": [{"bus_path": "3-1", "product": "wifi"}],
        "lost_since_boot": [{"bus_path": "3-1"}],
        "passed_through": [{"bus_path": "4-1"}],
        "boot_passthrough": [{"bus_path": "4-1"}],
        "lost_transient": [{"bus_path": "5-1"}],
        "missing_transient": [{"bus_path": "5-1"}],
        "causes": [{"bus_path": "3-1", "cause": "unplugged"}],
        "boot_baseline": {"boot_id": "abc", "count": 6},
        "controllers": [{"pci_address": "0000:01:00.0"}],
    }


def test_clear_usb_history_scrubs_cached_diag(monkeypatch):
    monkeypatch.setattr(usb_diagnostics, "purge_history",
                        lambda: {"purged": ["usb_presence.json", "usb_boot_baseline.json"]})
    agent = _FakeAgent(_populated_diag(), ts=99999.0)

    resp = asyncio.run(cs_commands.handle_cs_command(agent, "clear_usb_history", {}))

    assert resp["status"] == "SUCCESS"
    assert resp["action"] == "clear_usb_history"
    d = agent._last_usb_diag
    # Every roster/baseline-derived loss field is emptied straight away.
    for k in ("missing", "lost_since_boot", "passed_through", "boot_passthrough",
              "lost_transient", "missing_transient", "causes"):
        assert d[k] == [], f"{k} should be scrubbed to [] after purge"
    assert d["known_count"] == 0
    assert d["boot_baseline"] == {}
    # Untouched, non-loss fields survive (the frame stays otherwise valid).
    assert d["controllers"] == [{"pci_address": "0000:01:00.0"}]
    # Forces a fresh recompute on the next slow-jobs pass (was 99999.0).
    assert agent._last_usb_diag_ts == 0.0


def test_clear_usb_history_no_cached_diag_is_safe(monkeypatch):
    monkeypatch.setattr(usb_diagnostics, "purge_history", lambda: {"purged": []})
    agent = _FakeAgent(None, ts=42.0)

    resp = asyncio.run(cs_commands.handle_cs_command(agent, "clear_usb_history", {}))

    assert resp["status"] == "SUCCESS"
    # No diag cached yet -> nothing to scrub, but still force a recompute.
    assert agent._last_usb_diag is None
    assert agent._last_usb_diag_ts == 0.0
