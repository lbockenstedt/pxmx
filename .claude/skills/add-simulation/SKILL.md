---
name: add-simulation
description: >-
  Use when adding a new client traffic-simulation to the client-sim platform —
  a new failure/traffic type (like dns_fail, dns_latency, dhcp_fail, collab) that
  a client VM generates to trip an Aruba Central alert or produce load. Adding a
  sim touches ~15 spots across the cs and lm repos (both linux AND windows
  scripts, the shared config, both orchestrators, BOTH sim-views.js UI copies,
  and — for alert sims — the quota engine + its hub twin + the alert docs).
  Missing one silently half-ships the sim. This skill is the end-to-end recipe
  with the boundaries baked in. Invoke it whenever the user asks to add/create a
  new simulation, sim type, traffic generator, or alert generator for the
  clients.
---

# Add a Simulation

Adding a client sim is a coordinated change across the client scripts, config,
orchestrators, UI, and (for alert sims) the quota engine. Work through
`reference.md` (same folder) — it is the exhaustive, ordered touch-point map with
the exact patterns. This file is the shape + the rules.

## The shape (in order)
1. **Client scripts — BOTH platforms.** `cs/clients/linux/<sim>.sh` and
   `cs/clients/windows/<sim>.ps1`. This is an INVARIANT: a sim exists on linux
   AND windows, or it isn't done. (T3 is NOT a target — see Scope.)
2. **Shared lib** — only if the sim needs a new helper: edit the CANONICAL
   `cs/clients/lib/common.sh`, then `cp` to `cs/clients/linux/common.sh`; add the
   equivalent to `cs/clients/windows/common.ps1`. Add the sim's on/off key (and
   any new keys) to `CS_OVERRIDE_KEYS` in BOTH common files.
3. **Config** — `cs/configs/simulation.conf`: the sim's flag in the `[s0]`–`[s9]`
   buckets (default off) + any rate/duration/threshold/address keys.
4. **Orchestrators — BOTH.** `simulation.sh` and `simulation.ps1`: read the flag
   from the bucket, add to the `randomizable_sims` lists, add to the override
   apply list, add the dispatch gate (skip-if-already-running), and add the sim
   to `report_status`/`Send-Status` (active_simulations + config).
5. **UI — BOTH `sim-views.js` copies** (`lm/WebUI/` and `cs/lm-spoke/static/`):
   add the sim to the name/flag lists and add its config fields to the sim-conf
   editor arrays (right field `type` — `'list'` for multi-value).
6. **Quota engine** — ONLY if the sim generates an alert / consumes quota: the
   placement matrix, `sim_quota.py` + its hub twin `lm/core/src/simulations/
   sim_quota.py`, and the alert linkage.
7. **Docs** — `lm/docs/alert-generation.md` (how to fire the alert + levers).
8. **Verify** — run the `dual-copy-guard` skill to confirm nothing drifted, then
   the standard commit/push per the terse-commit convention.

## Boundaries — the rules a sim MUST obey
- **New file per sim — NEVER a function in the orchestrator.** A sim's traffic /
  failure logic lives in its OWN `cs/clients/linux/<sim>.sh` (+ the `.ps1` twin).
  `simulation.sh` / `simulation.ps1` invoke it ONLY as a flag-gated dispatch —
  `if [[ "$<flag>" == "on" ]]; then run_simulation "<sim>.sh" <pause>; fi` (the
  `.ps1` uses the same shape). Do NOT add the sim's logic as a function inside
  the orchestrator: it only reads flags, connects, dispatches, and reports. The
  new file delivers itself via the content-hash manifest and self-manages its
  own single-instance (the dispatch skip-if-running guard). The ONLY exception
  is a connectivity-failure sim (ssidpw_fail / auth_fail), which extends the
  connect loop instead of a script — see Kinds of sim.
- **One shared DNS ceiling.** Flood sims (anything that hammers DNS) share the
  single `dns_ceiling` self-throttle + gateway circuit-breaker in common.sh/ps1.
  Do NOT invent a second throttle. Use `dns_ceiling_rate`/`Get-DnsCeilingRate`
  and the `dns_gw_confirmed_down`/`_stable` gate.
- **Never self-kill.** A sim must NOT `pkill`/`Stop-Process` its own kind — the
  orchestrator's skip-if-running guard owns single-instance. Self-killing chops
  a mid-flight burst and drops the alert (regression lesson: a single-instance
  `pkill` once killed the 600s DNS burst).
- **Keep the linux network split.** Connect/adapter/reset logic stays in the
  sourced `network_common.sh`/`connect_*.sh` files — don't inline it.
- **Edit canonical, not generated.** `cs/clients/lib/common.sh` is canonical;
  `cs/clients/linux/common.sh` is a `cp`. Never edit the copy.
- **Both UI copies, both quota twins.** `sim-views.js` ×2; `sim_quota.py` hub +
  spoke.
- **Delivery is automatic.** The spoke serves `clients/<platform>` via the
  content-hash manifest; committing the files delivers them. No backend route
  change and no special VERSION bump needed (headers stay at the fleet baseline).

## Scope
This skill is for **client traffic sims → linux + windows only**. `cs/clients/t3`
is a different simulator (virtual-WiFi/IoT MAC emulation) with no per-sim
scripts — do NOT add a t3 port for a traffic sim.

## Kinds of sim (pick the path in reference.md)
- **Flood/alert sim** (dns_fail, dns_latency, dhcp_fail): rate/duration + shared
  DNS ceiling + gateway breaker + quota/alert wiring.
- **Steady traffic sim** (iperf, download, www_traffic, ping_test, collab): a
  target/server + a duration/bandwidth; usually no quota wiring.
- **Connectivity-failure sim** (ssidpw_fail, auth_fail): driven inline by the
  orchestrator's connect loop, not a separate script — extend the loop instead.
