"""Shared BACK-PRESSURE signalling logic (VENDORED, keep in sync).

ONE implementation of the pressure score + throttle scaling, used identically by
every tier so a "slow down" signal means the same thing everywhere:

  * pxmx **agent**  — scores its Proxmox node load and reports it up.
  * cs **spoke**    — folds its own event-loop load with the agent's node load
    and turns the result into a client throttle interval.
  * lm **hub/core** — can fold spoke pressure into its own back-off decisions.

Principle (per design): if ANY part of the system is under load it raises a
pressure score, and downstream pollers (clients → spoke, spoke → hub) scale
their report/poll interval UP so the loaded tier gets fewer requests and can
recover. Pure functions, stdlib-only, so the file vendors byte-identically into
pxmx/agent/src, cs/lm-spoke/src, and lm/core/src — grep "resource_pressure" to
find all copies; edit the canonical (lm/core/src) and re-copy.

NOTE: this is ONLY the back-pressure signal. The CPU/mem 1h averaging stays
per-tier (each computes what it displays / gates on independently) — deliberately
not shared, so the display metric and the gate metric remain their own concerns.
"""
from __future__ import annotations

from typing import List, Optional


def pressure_score(cpu_pct: Optional[float] = None,
                   mem_pct: Optional[float] = None,
                   loop_lag_ms: Optional[float] = None,
                   *,
                   cpu_ceiling: float = 90.0,
                   mem_ceiling: float = 90.0,
                   lag_ceiling: float = 250.0) -> float:
    """Normalise each supplied load signal to its ceiling and return the MAX, so
    ANY single dimension crossing its ceiling drives the score to ≥ 1.0.

    0.0 = idle, ~1.0 = at the ceiling on some axis, > 1.0 = overloaded. Missing
    signals (``None``) are ignored, so a tier only scores on what it can measure."""
    signals: List[float] = []
    if cpu_pct is not None and cpu_ceiling > 0:
        signals.append(max(0.0, float(cpu_pct)) / cpu_ceiling)
    if mem_pct is not None and mem_ceiling > 0:
        signals.append(max(0.0, float(mem_pct)) / mem_ceiling)
    if loop_lag_ms is not None and lag_ceiling > 0:
        signals.append(max(0.0, float(loop_lag_ms)) / lag_ceiling)
    return max(signals) if signals else 0.0


def combine_pressure(*scores: Optional[float]) -> float:
    """Fold several tiers' pressure scores into one (the MAX) — so a spoke that
    is itself calm but whose Proxmox host is slammed still reports high pressure
    and throttles its clients."""
    vals = [float(s) for s in scores if s is not None]
    return max(vals) if vals else 0.0


def throttle_interval(base_s: float, pressure: Optional[float],
                      *, soft: float = 0.85, max_mult: float = 8.0) -> float:
    """Scale a poll/report interval UP as pressure rises.

    Below *soft* → no throttle (return *base_s*). From *soft* to 1.0 ramp the
    multiplier 1× → 3×; above 1.0 keep climbing toward *max_mult* (overloaded →
    heavy back-off). Monotonic and capped so a runaway signal can't push the
    interval to infinity. Returns a float ≥ base_s."""
    base = float(base_s)
    if pressure is None or pressure <= soft:
        return base
    if pressure <= 1.0:
        # soft..1.0  →  1x..3x
        frac = (pressure - soft) / max(1e-6, (1.0 - soft))
        mult = 1.0 + 2.0 * frac
    else:
        # >1.0  →  3x climbing to max_mult (each +1.0 pressure adds ~ the span)
        mult = min(max_mult, 3.0 + (pressure - 1.0) * (max_mult - 3.0))
    return base * min(max_mult, mult)
