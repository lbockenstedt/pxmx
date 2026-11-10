# Dual-Copy / Twin Pairs — authoritative reference

All paths relative to the workspace root (`/Users/lbockenstedt/vscode`, the nw.git
checkout that holds the sibling repos `lm/`, `cs/`, `pxmx/`, `netbox/`, …).

Sync-rule legend:
- **byte** — must be byte-identical; one side is canonical, the other is a `cp`.
- **parity** — functional parity across languages/paths; sync behavior + shared
  data (key lists, field arrays, thresholds, logic), not literal text.
- **twin** — two copies of the same module in different repos; keep logic +
  shared constants matched.

**CI enforces the mechanical part.** `lm/.github/twins.json` lists the byte
pairs and the key-list pairs (CS_OVERRIDE_KEYS, SIM_QUOTA_KEYS, QUOTA_TIERS,
CS_CONTROL_FLAGS); `lm/.github/scripts/check_twins.py` runs in every repo's
`Fleet rules` workflow on pushes/PRs to dev. Same-repo drift fails the run;
cross-repo drift (the other copy read from GitHub on the same branch) warns.
Add a pair to `twins.json` when you add one here. Field arrays and "judge
intent" parity still need a human.

---

## 1. sim-views.js  — the sim-config WebUI  (parity)
- `lm/WebUI/sim-views.js`  ⇄  `cs/lm-spoke/static/sim-views.js`
- **Both are hand-edited together.** Neither is canonical.
- Sync: the sim name lists, `CS_CONTROL_FLAGS`, the sim-conf editor field arrays
  (the `[simulation]`/`[address]` `['key','Label'[,'type']]` rows), the
  `csSimField` types (`'list'` etc.) and serializers.
- **Also both backends** when the change needs server support (e.g. the `'list'`
  textarea serializer needed backend handling on each side).
- Compare: diff the field arrays + flag lists by hand; they are not textually
  identical (different surrounding shell), so judge intent.

## 2. client common.sh  — shared bash lib  (byte)
- CANONICAL: `cs/clients/lib/common.sh`
- GENERATED: `cs/clients/linux/common.sh`  (the deploy path ships flat files, so
  the lib is `cp`-copied)
- Rule: **byte-identical**. Edit the canonical, then:
  `cp cs/clients/lib/common.sh cs/clients/linux/common.sh`
- Verify: `cmp cs/clients/lib/common.sh cs/clients/linux/common.sh`
- The generated copy carries a "GENERATED-COPY NOTICE" header pointing here.

## 3. common.sh ⇄ common.ps1  — client shared lib, cross-platform  (parity)
- `cs/clients/lib/common.sh`  ⇄  `cs/clients/windows/common.ps1`
- Windows is PowerShell 5.1; port behavior, not text. Specifically keep matched:
  - `CS_OVERRIDE_KEYS` (bash array) == `$script:CS_OVERRIDE_KEYS` (ps1 array)
  - the bucket algorithm = **crc32(hostname) % 10** on BOTH (must also match the
    spoke's `sim_config.bucket_for()`; Windows once used SHA256 — that was a bug)
  - the DNS ceiling / gateway circuit-breaker helpers and the dns_lat selection
    (threshold/recheck/probe/select) semantics
- Compare: read both, confirm the key lists + thresholds + algorithm match.

## 4. linux ⇄ windows client scripts  — full sim parity  (parity)
- `cs/clients/linux/*.sh`  ⇄  `cs/clients/windows/*.ps1`
- Every sim and orchestrator behavior exists on BOTH platforms. When a sim or a
  behavior lands on one, it must land on the other (see the `add-simulation`
  skill for the per-sim recipe).
- Cross-platform mapping (bash → PowerShell): `dig`→`Resolve-DnsName`,
  `nmcli`/`wpa_supplicant`→`netsh wlan`/WLAN-profile XML, `ip route`→`Get-NetRoute`,
  `ip link set`→`Enable/Disable-NetAdapter`, `apt`→`winget`, `pgrep -f`→
  `Get-CimInstance Win32_Process`, `nohup … &`→`Start-Process`, systemd/.desktop→
  Scheduled Task, `shutdown -r +N`(min)→`shutdown /r /t`(sec).
- Orchestrators to keep aligned: `simulation.sh`↔`simulation.ps1` (bucket read,
  `randomizable_sims`, override list, dispatch, `report_status`/`Send-Status`
  active_simulations+config), `update.sh`↔`update.ps1` (content-hash
  `/api/scripts/manifest?platform=<p>` sha256 sync + `/api/config/overrides`),
  `startup.sh`↔`startup.ps1`.
- Version headers: `version=` (`.sh`) / `$version =` (`.ps1`) — hand-bumped;
  keep the fleet baseline consistent (currently `0.01`).

## 5. sim_quota.py  — quota engine hub/spoke twin  (twin)
- `cs/lm-spoke/src/sim_quota.py`  ⇄  `lm/core/src/simulations/sim_quota.py`
- Keep matched: `SIM_QUOTA_KEYS`, `QUOTA_TIERS`, normalization defaults, and any
  shared quota logic. A key added on one side but not the other breaks the
  hub↔spoke quota contract.
- Note the separate but related engine `cs/lm-spoke/src/sim_quota_engine.py`
  (spoke-side placement/harvest/stacking) has no hub twin — don't confuse it.

## 6. dns / dhcp  — NOT twins (standalone repos archived)
- The `lbockenstedt/dns` and `lbockenstedt/dhcp` repos are **archived**. The
  only live code is `lm/dns/` and `lm/dhcp/` (each with its own `src/` and
  `tests/`). There is nothing to port: edit the lm copy and you are done.
- The agent loads these roles from lm: `_ROLE_MAP` in
  `lm/agent/src/agent_spoke.py` gives the `dns` and `dhcp` roles a `repo_url`
  of `None`, so they update with lm, not from a module repo.
- Lesson (lm `b6c82e0a`): a fix landed in a copy that is not run, and the
  resolvers kept answering "Unknown command". Always confirm you are editing
  the path that actually runs.
- Ignore any leftover `dns/` or `dhcp/` checkout at the workspace root; never
  copy changes into it.

## 7. hub ⇄ cs-spoke twin for SHARED client-sim modules  (twin)
- A fix to a client-simulation module that exists on BOTH the hub and the cs
  spoke must be ported to the spoke twin. Test paths and Alert paths can diverge
  if only one is fixed (lesson: `mist-cs-spoke-port-gap-lesson`).
- Example shape: an Aruba-Central/Mist client module living in `lm/core/...`
  with a mirror used by the cs spoke. When you fix the hub copy, grep the cs
  spoke (`cs/lm-spoke/src/…`) for the twin and apply the same fix.

## 8. dns_fail.txt  — bogus-domains data file  (byte, triple)
- `cs/configs/dns_fail.txt`  ⇄  `cs/clients/linux/dns_fail.txt`  ⇄
  `cs/clients/windows/dns_fail.txt`
- The DNS-failure sim's flood list. Keep the three copies identical when
  regenerated. Verify with `cmp` pairwise.

## 9. probe_signatures.py  — HTTPS-port scanner classifier  (byte, twin cross-repo)
- CANONICAL: `lm/core/src/security/probe_signatures.py`
- VENDORED : `ab/probe_signatures.py`  (the `ab` repo can't import from `lm/core`
  at runtime, so it ships its own byte-identical copy)
- Rule: **byte-identical**. The two `PROBE_SUFFIXES`/`PROBE_TOKENS` signature
  lists + `looks_like_probe()` MUST match so a new scanner signature added on the
  hub is enforced at the AppBuilder edge too. Edit the canonical, then:
  `cp lm/core/src/security/probe_signatures.py ab/probe_signatures.py`
- Verify: `cmp lm/core/src/security/probe_signatures.py ab/probe_signatures.py`
- The hub middleware (`lm/core/src/api.py`) and the edge proxy
  (`lm/proxy/src/proxy_app.py`) import the canonical directly — no copy — so only
  the cross-repo `ab` copy needs the `cp` sync.

---

## 10. CI/CD pipeline files  — copies across every active repo  (byte)
- CANONICAL: `.pipeline-templates/{bump_version.py,promote.sh,promote.yml,selftest.sh}`
  (in the workspace-root `nw` repo)
- COPIES: in **every active repo** (`lm cs pxmx ab cppm ldap le netbox
  nw opnsense qa kvm truenas tsa zabbix`; dns/dhcp are archived) as
  `<repo>/.github/scripts/bump_version.py`, `<repo>/.github/scripts/promote.sh`,
  `<repo>/.github/scripts/promotion_selftest.sh` (from `selftest.sh`) and
  `<repo>/.github/workflows/promote.yml`.
- Rule: **byte-identical**. Each repo runs its own GitHub Actions and cannot
  import from a sibling repo, so every repo ships its own copy. Fixing a
  promotion bug in one repo and not the rest means that repo promotes correctly
  and the others keep the old behaviour — silently, because promotion
  still "succeeds".
- These enforce that **VERSION is branch-owned**: dev's version never leaks into
  qa/main, and each promotion advances only the target's own sequence.
- Edit the canonical, then re-run the distribute loop:
  ```bash
  cd /Users/lbockenstedt/vscode
  for r in lm cs pxmx ab cppm ldap le netbox opnsense qa kvm truenas tsa zabbix .; do
    cp .pipeline-templates/bump_version.py .pipeline-templates/promote.sh $r/.github/scripts/
    cp .pipeline-templates/selftest.sh $r/.github/scripts/promotion_selftest.sh
    cp .pipeline-templates/promote.yml  $r/.github/workflows/promote.yml
    chmod +x $r/.github/scripts/promote.sh $r/.github/scripts/promotion_selftest.sh
  done
  ```
- Verify (drift sweep):
  ```bash
  for r in lm cs pxmx ab cppm ldap le netbox opnsense qa kvm truenas tsa zabbix .; do
    for f in scripts/bump_version.py scripts/promote.sh workflows/promote.yml; do
      c=.pipeline-templates/$(basename $f); [ "$(basename $f)" = promote.yml ] && c=.pipeline-templates/promote.yml
      cmp -s $c $r/.github/$f || echo "DRIFT: $r/.github/$f"
    done
  done
  ```
- `nw/tests/test_promotion_pipeline.py` pins the behaviour, but it only runs in
  the `nw` repo — it cannot catch drift in the other copies. Use the
  sweep above.

---

## Quick audit commands
```bash
cd /Users/lbockenstedt/vscode
# byte pairs
cmp cs/clients/lib/common.sh cs/clients/linux/common.sh && echo "common.sh in sync"
cmp cs/configs/dns_fail.txt cs/clients/linux/dns_fail.txt && echo "dns_fail.txt L in sync"
cmp cs/configs/dns_fail.txt cs/clients/windows/dns_fail.txt && echo "dns_fail.txt W in sync"
cmp lm/core/src/security/probe_signatures.py ab/probe_signatures.py && echo "probe_signatures in sync"
# parity/twin pairs — read + compare the shared data, don't cmp:
#   CS_OVERRIDE_KEYS   in cs/clients/lib/common.sh vs cs/clients/windows/common.ps1
#   SIM_QUOTA_KEYS     in cs/lm-spoke/src/sim_quota.py vs lm/core/src/simulations/sim_quota.py
#   sim lists+fields   in lm/WebUI/sim-views.js vs cs/lm-spoke/static/sim-views.js
#   linux *.sh sims    vs windows *.ps1 sims (filename set + dispatch)
```

When adding to this reference: a pair belongs here if editing one side without
the other produces a silent, hard-to-spot bug. Record the canonical direction
and the sync rule.
