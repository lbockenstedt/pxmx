"""Drift guard: discovery.py is the canonical copy of lm/core/src/messaging/hub_discovery.py."""
import hashlib
from pathlib import Path

# Same constant is pinned in lm/core/tests/test_hub_discovery_sync.py; bump both together.
EXPECTED_SHA = "5664e2693d483f25c37f643114c7dea39a1333bacf0d7fc67d73b322914c179e"


def test_pxmx_discovery_in_sync():
    # Hash this copy and compare with the pinned hash the lm copy must also match.
    p = Path(__file__).resolve().parents[1] / "src" / "discovery.py"
    got = hashlib.sha256(p.read_bytes()).hexdigest()
    assert got == EXPECTED_SHA, (
        f"discovery.py ({got}) changed. Copy it to lm/core/src/messaging/hub_discovery.py "
        "and bump EXPECTED_SHA in both repos."
    )
