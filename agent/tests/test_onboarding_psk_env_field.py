"""ProxmoxAgent._load_env_field — reads the installer-written .env for the
optional zero-touch onboarding PSK / tenant hint (--onboarding-psk /
--tenant-hint on install_agent.sh), the same file + parsing convention
_load_secret already uses for AGENT_SECRET.

agent.py can't be imported directly in a lightweight test env (package-
relative imports), so it's loaded as a real module via importlib with a
synthetic parent package — mirrors test_command_progress_keepalive.py. The
method is bound to a bare stub (it touches only the module's own __file__
global, no instance state), and __file__ is monkeypatched per test so the
resolved .env path lands in a pytest tmp_path instead of the real repo.
"""
import os
import sys
import types
import importlib.util
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("pxmx_agent_src_envfield")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_envfield"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "pxmx_agent_src_envfield.agent", SRC / "agent.py",
    submodule_search_locations=[str(SRC)])
_agent_mod = importlib.util.module_from_spec(_spec)
sys.modules["pxmx_agent_src_envfield.agent"] = _agent_mod
_spec.loader.exec_module(_agent_mod)

ProxmoxAgent = _agent_mod.ProxmoxAgent


class _Stub:
    def __init__(self):
        self._load_env_field = ProxmoxAgent._load_env_field.__get__(self)


def _point_at(tmp_path, monkeypatch):
    """_load_env_field resolves dirname(__file__)/../.env — pin the module's
    __file__ to <tmp_path>/src/agent.py so that collapses to <tmp_path>/.env."""
    monkeypatch.setattr(_agent_mod, "__file__", str(tmp_path / "src" / "agent.py"))


def test_reads_matching_key(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("ONBOARDING_PSK=abc123\nTENANT_HINT=lrb\n")
    stub = _Stub()
    assert stub._load_env_field("ONBOARDING_PSK") == "abc123"
    assert stub._load_env_field("TENANT_HINT") == "lrb"


def test_missing_key_returns_none(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("AGENT_SECRET=xyz\n")
    stub = _Stub()
    assert stub._load_env_field("ONBOARDING_PSK") is None


def test_missing_env_file_returns_none(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    stub = _Stub()
    assert stub._load_env_field("ONBOARDING_PSK") is None


def test_empty_value_returns_none(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("ONBOARDING_PSK=\n")
    stub = _Stub()
    assert stub._load_env_field("ONBOARDING_PSK") is None
