# Add a Simulation — exhaustive touch-point map

Workspace root: `/Users/lbockenstedt/vscode`. Repos used: `cs/` (client scripts,
config, spoke, UI copy), `lm/` (hub, UI copy, quota twin, docs).

Read the *current* files to find exact insertion points — the constructs below
are stable; line numbers are not. Placeholder `<sim>` = the sim's key (e.g.
`collab`). Keep the name consistent everywhere.

Legend: ☐ = a required edit. Skip a section only when the note says it's optional
for your sim kind.

---

## 1 — Client scripts (BOTH platforms) — REQUIRED

### ☐ `cs/clients/linux/<sim>.sh`  (new)
- First lines: shebang + `version=0.01`.
- It is launched by `simulation.sh` via `run_simulation` (a `nohup bash … &`), so
  it runs as its own process. Source what it needs: `ini-parser.sh` then
  `common.sh` (for `get_value` + the shared helpers). Follow an existing sim
  (e.g. `dns_fail.sh`, `collab.sh`) for the boilerplate + logging (`tee -a
  /usr/local/scripts/sim.log`).
- Read config with `get_value '<section>' '<key>'`.
- If it floods DNS: gate on the shared throttle —
  `rate=$(dns_ceiling_rate <configured>)` (rate 0 = skip this burst); before/at
  the burst check `dns_gw_confirmed_down "$gw"` → penalize via
  `dns_ceiling_penalize` + bail, and hold until `dns_gw_stable "$gw"`.
- Do NOT `pkill` your own sim (single-instance is the orchestrator's job).

### ☐ `cs/clients/windows/<sim>.ps1`  (new — INVARIANT: parity with the .sh)
- First lines: `$version = '0.01'`; then `. 'C:\Scripts\ini-parser.ps1'` and
  `. 'C:\Scripts\common.ps1'`; parse config
  (`$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'`);
  `Write-SimVersionBanner '<sim>.ps1' $version`.
- PowerShell 5.1 (Desktop): no ternary/`??`; use `Start-Sleep`,
  `[System.Diagnostics.Stopwatch]`, `System.Net.Sockets.UdpClient` for UDP.
- Read config via `get_value '<section>' '<key>'`; merge `Get-SimOverrides`.
- Flood sims: use `Get-DnsCeilingRate <configured>`, `Test-DnsGatewayConfirmedDown`
  / `Test-DnsGatewayStable`, `Invoke-DnsCeilingPenalize`.
- Cross-platform mapping: `dig`→`Resolve-DnsName`, raw sockets→`UdpClient`,
  `nmcli`/`wpa_supplicant`→`netsh wlan`.

## 2 — Shared lib (OPTIONAL — only if you add a helper)
- ☐ Edit CANONICAL `cs/clients/lib/common.sh`, then
  `cp cs/clients/lib/common.sh cs/clients/linux/common.sh` (verify `cmp`).
- ☐ Port the helper into `cs/clients/windows/common.ps1`.
- ☐ Add the sim's on/off key (+ any new address keys) to `CS_OVERRIDE_KEYS` in
  BOTH `common.sh` and `common.ps1` (the arrays must match).

## 3 — Config: `cs/configs/simulation.conf`  — REQUIRED
- ☐ Add `<sim>=off` to each `[s0]`…`[s9]` bucket that can run it (default off;
  set `on` in the buckets you want it live).
- ☐ Add tuning keys under `[simulation]` (rate/duration/threshold) and/or
  `[address]` (servers/targets), e.g. `<sim>_rate`, `<sim>_duration`.
- Note: this conf is the shared template; the spoke overlays per-tenant config.

## 4 — Orchestrators (BOTH) — REQUIRED

### ☐ `cs/clients/linux/simulation.sh`
- Read flag: `<sim>=$(get_value $simulation_id '<sim>')` (near the other bucket
  reads).
- Add `<sim>` to the `randomizable_sims` handling loops (the `for sim in …`
  lists — the random-pool roll AND the report list).
- Overrides: `apply_overrides` covers it automatically IF the key is in
  `CS_OVERRIDE_KEYS` (§2).
- Dispatch: add a gate in the sim-launch block, e.g.
  `[[ "$<sim>" == on ]] && run_simulation <sim>.sh` (mirrors the existing
  `run_simulation dns_fail.sh` pattern; the skip-if-running `pgrep` guard is in
  `run_simulation`).
- `report_status`: add `<sim>` to the active-sims `for sim in …` list AND the
  config JSON `printf` (add `"<sim>":"%s"` and a `"$(json_escape "${<sim>:-off}")"`
  arg).

### ☐ `cs/clients/windows/simulation.ps1`
- Read flag: `$script:<sim> = get_value $script:simulation_id '<sim>'`.
- Add `<sim>` to the randomizable `foreach ($sim in @(…))` list.
- Add `<sim>` (+ any tuning keys) to the `Apply-UserOverrides -Names @(…)` list.
- Dispatch (in the sim-launch block):
  `if ($script:<sim> -eq 'on' -and -not (Test-ScriptRunning -ScriptName '<sim>.ps1')) { Run-Simulation -ScriptName '<sim>.ps1' }`.
- `Send-Status`: add `<sim>` to the `active_simulations` `foreach ($name in @(…))`
  list AND the `config` hashtable (`<sim> = [string]$script:<sim>`).

## 5 — UI (BOTH `sim-views.js` copies) — REQUIRED
Files: `lm/WebUI/sim-views.js` AND `cs/lm-spoke/static/sim-views.js`. Make the
SAME edits in both.
- ☐ Add `<sim>` to the sim-name lists — the `names.push('normal','dns_fail',…)`
  default list AND the control-flag list (`CS_CONTROL_FLAGS` in the cs copy / the
  equivalent flag list in the lm copy). The per-bucket matrix editor picks it up
  from these.
- ☐ Add its config fields to the sim-conf editor field arrays: under the
  `simulation:` and/or `address:` sections add rows like
  `['<sim>_rate','<Sim> Rate (/min)']`, `['<sim>_duration','<Sim> Duration (s)']`,
  and for a multi-value address use the `'list'` type:
  `['<sim>_servers','<Sim> Servers (one per line)', 'list']`.
- If the field needs a new serializer behavior (like `'list'` did), that touched
  BOTH backends too — check whether your field type already exists.

## 6 — Quota / placement engine (OPTIONAL — alert/quota sims only)
- ☐ Placement matrix (see memory `sim-placement-unified-model`): add the sim so
  the engine can assign it (defs + weighted rules).
- ☐ `cs/lm-spoke/src/sim_quota.py` AND its twin
  `lm/core/src/simulations/sim_quota.py`: if the sim consumes a quota, add to
  `SIM_QUOTA_KEYS`; keep the two files' key lists matched (dual-copy-guard §5).
- ☐ Stacking/harvest: if the sim is shareable/stackable, add it to the relevant
  sets in `cs/lm-spoke/src/sim_quota_engine.py` (`randomizable_sims` ∩ the
  `_sim_multi` shareable set).
- ☐ Alert linkage (memory `alert-driven-sim-quota-feature`): map the sim to the
  Aruba Central alert it generates.

## 7 — Docs (recommended)
- ☐ `lm/docs/alert-generation.md`: add a section — how to generate this alert,
  the config knobs, and the quota levers. (Read-before-touching-alert-sims doc.)
- ☐ Any canonical feature doc under `lm/docs/` if the sim is a notable feature.

## 8 — Verify + ship
- ☐ Run the `dual-copy-guard` skill (both `sim-views.js`, `common.sh`↔`.ps1`
  keys, `sim_quota.py` twin, linux↔windows parity).
- ☐ Windows: if a Windows box is available, parse-check with
  `[System.Management.Automation.Language.Parser]::ParseFile(...)` (no `pwsh` on
  the dev host).
- ☐ Commit per the terse-commit convention (fast commit on main; the client
  content-hash manifest delivers the new files — no route change).

---

## Copyable pattern index (existing sims to imitate)
- Flood + DNS ceiling + gateway breaker: `dns_fail.sh` / `dns_fail.ps1`.
- Self-healing server selection + recheck: `dns_latency.sh` / `dns_latency.ps1`.
- Raw-packet UDP sim: `dhcp_fail.sh`(+`dhcp_fire.py`) / `dhcp_fail.ps1`.
- Media/UDP-to-sink sim: `collab.sh`(+`collab.py`) / `collab.ps1`.
- Steady traffic: `iperf` / `download` / `ping_test` / `www_traffic`.
- Inline connectivity-failure (no separate script): `ssidpw_fail` / `auth_fail`
  live in the orchestrator's connect loop (`Connect-WifiPskFail` on Windows).
