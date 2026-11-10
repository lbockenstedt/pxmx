---
name: dual-copy-guard
description: >-
  Use whenever you edit a file that has a mirror/twin copy elsewhere in the
  workspace, or when asked to audit for drift between duplicated files. This
  codebase deliberately keeps several files in lockstep (the two sim-views.js
  copies, the canonical vs generated common.sh, the common.sh↔common.ps1
  parity, the sim_quota.py hub/spoke twin, linux↔windows client scripts).
  The dns/dhcp modules are NOT twins: their standalone repos are archived and
  `lm/dns/` + `lm/dhcp/` are the only live copies. Editing one copy and
  forgetting the other silently half-ships a change — this skill finds every
  pair, detects drift, and syncs it the correct direction. Invoke it after
  touching any client script, sim-views.js, common.sh/ps1 or sim_quota.py,
  and any time you want a full drift sweep.
---

# Dual-Copy Guard

This workspace has files that MUST stay in sync with a counterpart. Missing the
counterpart is a recurring source of "it works on one path but not the other"
bugs. Use this skill to catch that.

## Two modes

**Guard mode** (targeted) — you just changed one or more files. Question: *do any
of them have a twin I also need to update?*
1. Read `reference.md` (same folder) for the authoritative pair list.
2. For each file you changed, check whether it appears in a pair.
3. If it does, open the counterpart and apply the equivalent change, respecting
   the pair's **sync rule** (byte-identical / functional-parity / matched-keys)
   and **canonical direction** from the reference.
4. Verify with the pair's compare command, then report what you synced.

**Audit mode** (sweep) — asked to check the whole workspace for drift.
1. Read `reference.md`.
2. Run each pair's compare command. Collect every mismatch.
3. For each mismatch, determine whether it's real drift (a change that landed on
   one side only) or an expected difference (some pairs are functional-parity,
   not byte-identical — a diff is normal; judge the *intent*, not the bytes).
4. Report a table: pair, drift? (yes/expected), what's out of sync, fix.
5. Only auto-fix byte-identical pairs (safe: `cp` canonical→generated). For
   functional-parity/twin pairs, propose the specific edit and confirm before
   applying — the two sides legitimately differ in surrounding code.

## Non-negotiable rules
- **Never edit a generated copy directly.** For `cp`-generated files, edit the
  canonical source and re-generate. The reference marks which is canonical.
- **Functional-parity ≠ byte-identical.** `common.sh`↔`common.ps1`, the two
  `sim-views.js`, and the `sim_quota.py` twin differ in language/paths/imports.
  Sync the *behavior and the shared data* (key lists, field arrays, thresholds,
  logic), not the literal text.
- **Report, don't silently fix, the risky pairs.** Byte-identical `cp` sync is
  safe to apply. Everything else: show the proposed change, then confirm.
- **This is self-verifying.** Don't trust line numbers — read the current files
  to locate the equivalent construct. Code moves; the pairs don't.

## Scope note
T3 (`cs/clients/t3/`) is NOT a per-sim client — it's a virtual-WiFi/IoT emulator.
It shares only *delivery/config* plumbing (its `update_script.sh`, its
`ini-parser.sh` copy), never the traffic sims. Treat it as a config/delivery
node in the audit, not a sim-parity target.
