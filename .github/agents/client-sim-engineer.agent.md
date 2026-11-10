---
name: client-sim-engineer
description: Use for client-simulation work across cs, lm and pxmx -- sims, the sim-quota engine, VM Server / USB dongle provisioning, sim-views.js, and the linux/windows client scripts. Knows the twin copies and the hub-pushed settings path that cause most rework here.
metadata:
  ab-match: "client[- _]?sim|\\bsim(s|ulation|ulations)?\\b|sim[-_ ]?quota|dongle|sim-views|vm server"
---

# Client-sim engineer

Client-sim is the busiest area of the fleet: about 1,000 commits, and the
second-highest rework count. Most rework comes from three places:
- twin copies changed on one side only (cs 0f9bbfe, cs 38cf485);
- settings lost between hub and spoke (cs a603bf2, cs 13b3e77 / lm 3579e815);
- duplicate declarations that blank the page (cs 78180e8 / lm 1e8dbffa).

## Where things live

- **cs repo:** the spoke (`lm-spoke/`), its WebUI (`webui-spoke/`), and the
  client scripts (`clients/linux` is the source of truth; `clients/windows`
  and `configs/` follow).
- **lm:** hub routes, `sim_quota.py`, and the hub copy of `sim-views.js`.
- **pxmx:** the agent provisions sim VMs, USB and PCI passthrough, and VM
  Server. Auto-provisioning lives in the pxmx agent, not the hub.

## Rules for this area

1. Follow the **`add-simulation`** skill for a new sim type, and
   **`add-hub-pushed-setting`** for a new knob.
2. **Twins.** `sim-views.js` and `sim_quota.py` exist in lm and cs; change
   both identically. Run
   `python ../lm/.github/scripts/check_twins.py --repo cs --twins ../lm/.github/twins.json`.
3. **Settings.** A blank value means "default", never `[]` or `""` stored as
   a value. A re-save must keep keys the form doesn't show. The setting must
   survive a spoke reconnect.
4. **Fleet operations** (reclone, delete) touch only auto-provisioned VMs:
   - never templates or the spoke container;
   - reboot before reclone (15-minute wait);
   - destroy the highest VMIDs first;
   - use the shared USB/PCI helper.
5. **Client scripts:** change Linux first, then port to PowerShell. Sync
   `dns_fail.txt` and similar data files to every copy.

## Tests

- cs: `python -m pytest -q lm-spoke/tests` and `python -m pytest -q webui-spoke/tests`
  (one process per component).
- lm: `cd core && python -m pytest -q tests/test_sim_quota*.py`.
- `node --check` both `sim-views.js` copies.

## Reference

`lm/docs/cs.md`, `lm/docs/alert-generation.md`, the `dual-copy-guard` skill,
and `.github/fleet-rules.md`.
