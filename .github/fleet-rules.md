# Lab Manager fleet rules

<!-- Single source of truth. Edit ONLY lm/.github/fleet-rules.md.
     sync-fleet-files.yml copies it into every fleet repo, both as
     .github/fleet-rules.md and between the fleet-rules markers in AGENTS.md.
     Edits made to a copy are overwritten. -->

These rules apply to every change in every Lab Manager (LM) repo, whoever
makes it: a person, Copilot, Claude, another agent, or AppBuilder (AB). They
come from the owner's working sessions, and each one exists because breaking
it has caused a real defect or rework. A change that breaks a rule is a
defect even when the code works.

How the rules are used:

- **Copilot, Codex, Cursor and similar tools** load them from `AGENTS.md`.
- **AB** puts them into every fix, build and review prompt.
- **The CI checks** in the [Enforcement map](#enforcement-map) enforce the
  mechanical ones.

Specialist agents live in `.github/agents/`; pick one in Copilot with
`/agent`. Step-by-step recipes (skills) live in `lm/.claude/skills/`.

## 1. How to work (people and agents)

1. **Discuss before building.** For a new feature or a design question,
   describe the approach and wait for agreement. "Don't build yet" means no
   code. "Wait" means stop: no deploy, approve or merge until told.
2. **Confirm destructive external calls.** Before any DELETE, or any PUT that
   replaces data, in an external system (NetBox, ClearPass, Proxmox, Zabbix,
   …), list exactly what will change and get confirmation.
3. **Stay in your lane.**
   - Several people and sessions share checkouts. Fetch and rebase before
     every push (`git pull --rebase --autostash`).
   - Commit only your own changes; never sweep up someone else's edits.
   - Don't change the working directory unless asked.
4. **Fix the class, not the instance.** After fixing a bug, search the whole
   fleet for the same pattern and fix every copy. When an issue comes back,
   read the git history of the earlier attempts before patching again.
5. **Verify, don't assume.**
   - Run the tests for what you changed.
   - After a deploy, check the live result: login works, the page renders,
     and the data shows up end to end in LM, NetBox, Zabbix and ClearPass.
   - An audit is not finished until the findings are fixed and verified.
   - When something can't be diagnosed, add logging or diagnostics-page data
     instead of guessing.
6. **Commit trailer.** Commits made with an AI assistant end with
   `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

## 2. Git, branches and promotion

1. **dev → qa → main.** Commit or push to `dev` (or open a PR to dev).
   Promotion to `qa` and `main` happens only through the `promote.yml`
   workflow, run by its own triggers or by
   `lm/.github/scripts/promote_fleet.sh`.
   - Never open a PR to `qa` or `main` by hand.
   - Never push to `qa` or `main` directly.
   - `ci.yml` (`test`) and `check-direction` are the required checks.
2. **VERSION is bot-owned.** Never edit a `VERSION` file. Promotion bumps the
   last segment; only the owner changes MAJOR.
3. **One repo per change.** Cross-repo work is a separate commit or PR per
   repo. The hub and the spokes deploy independently, so the wire contract
   between them must stay backward-compatible (see Architecture, rule 3).
4. **Every PR states its intent.** Fill in the `Intent & Problem Statement`
   in `.github/pull_request_template.md`: the root problem, the intended
   change, and the expected runtime outcome. The AB review panel checks the
   diff against it, catching scope creep and partial work. A PR with no
   stated intent gets low confidence.
5. **No hard-coded branch names.** Hub and spokes run the same repo and
   branch.
6. **Don't version runtime config.** `.conf`, `.env` and generated state
   files stay out of git.
7. **Fleet-owned files are edited only in lm:**
   - this file;
   - `.github/agents/`;
   - `.claude/skills/`;
   - the `*-reusable.yml` workflows.

   Copies in other repos are overwritten by the sync. A repo's own agent is
   named `<repo>-*.agent.md`.

## 3. Architecture

1. **The spoke does the heavy lifting.** Spokes do the work. The hub is
   transport, state, policy and UI: it relays and stores, and holds no
   module logic. See `lm/docs/architecture-spoke-heavy-lifting.md`.
2. **API first, UI for everything.**
   - Every operation has an API, and the WebUI calls only that API.
   - Nothing beyond the initial installer may need a CLI.
   - Config screens are built from the config schema, so a new key needs no
     UI code.
3. **Wire changes stay backward-compatible.**
   - Messages use push, ack and retry; never fire-and-forget.
   - A multi-step change rolls back fully on failure and reports a
     before/after diff.
   - Handle an older peer that answers "Unknown command".
   - Exception: a module still in dev mode (never released) needs no
     compatibility shims.
4. **Spokes are autonomous.** A spoke that loses its hub keeps working on its
   last config. The hub and spokes relay commands; they never host data in
   transit, such as backups.
5. **Centralised by default.** Features that can run centrally or per spoke
   (Central API, Teams, email) default to central. In hub mode a spoke does
   not poll the vendor itself.
6. **Generic, not vendor-specific.**
   - Code works for the whole device class (any BMC: iLO, iDRAC, …).
   - The user picks the backend product (Kea or ISC, bind9 or dnsmasq, …);
     never lock to one vendor.
   - Repeated parsing of hostnames, IPs, MACs and identities lives in one
     shared library. When something can't be identified, it goes to the LLM.
7. **Nothing hard-coded.** No lab hostnames, IPs, tenants, sites, vendor
   lists or credentials in code; read them from config or discover them. The
   only exception is a value the owner explicitly approved, with a comment
   saying so.
8. **Self-healing and appliance-like.**
   - Every service, including new ones, has a watchdog that restarts it when
     its API stops answering.
   - Keep the number of things to patch small.
   - Installers are idempotent: a re-run updates the code and keeps the
     credentials. Customisations survive a reinstall.
9. **Reversible and gentle.** Every change can be backed out. Prefer letting
   the existing processes pick a change up on their next run over forcing
   it.
10. **Updates and patches.**
    - An update is detected by commit hash; any mismatch triggers a sync,
      not just a higher version. The UI shows the version.
    - Installers pull the latest installer from GitHub and print the version
      they are installing.
    - Auto-update never runs while users are logged in, except the forced
      run at 2am.
    - Auto-patch and auto-reboot run only inside the configured off-hours
      window. A manual patch follows the same maintenance-mode flow.
11. **Linux first.** The Linux bash scripts are the source of truth; the
    PowerShell ports follow them.

## 4. Data and storage

1. **JSON only, no database.** State lives in plain JSON files: no SQLite,
   ORM, YAML, XML or CSV storage, locally or on the hub. A JSON variant such
   as JSON Lines is acceptable only for a real performance or reliability
   gain. Large state is split into one file per tenant, and written less
   often.
2. **The hub keeps no long-term data.** Local data is a cache; long-term
   reporting belongs to the downstream system.
3. **Caching contract** (`lm/docs/caching-contract.md`):
   - Pages render from the in-memory cache; never fetch live to render a
     page.
   - The cache is persisted to disk so it is warm after a restart.
   - Refresh happens in the background, through the shared `cache_core`
     module.
   - Show a "cached" badge only when the data is more than 5 minutes old.
     Mark a source "offline" only after about 24 hours.
   - Never show "update running, please wait"; show the cache.
4. **The network is the source of truth.** NetBox and Zabbix are sinks that
   can be rebuilt from LM.
5. **Sync is idempotent.**
   - Before creating a record, look it up by its natural key: serial, then
     MAC, then primary IP, then name in the same site.
   - Don't PATCH when nothing changed, and never delete because a poll came
     back empty.
   - Don't skip a repair pass just because the record looks unchanged.
   - Each physical device is its own record (both halves of a VSX pair), and
     duplicates are removed by serial.
6. **Naming.** Hostnames and device names written to NetBox, DHCP and DNS
   are UPPER CASE. Discovered devices must not stay "generic" or "unknown":
   profile them further, and always get the hostname over SNMP.
7. **Vendor responses are untrusted.**
   - Check the HTTP status and the body (`result: failed` inside a 200 is a
     failure).
   - Reconnect when a session expires.
   - Make anything pushed JSON-safe (datetimes, sets, bytes).
8. **Keep reads and writes to the downstream system minimal.** NetBox,
   Zabbix and other sinks are not a lookup database (rule 2); an unbounded
   loop of calls to them has pegged a VM before.
   - Read from the local cache; refresh lists in the background on a floor
     of minutes, never per page view, per item or per cycle of a faster loop.
     Don't pull a full list to answer a single lookup.
   - Compare before writing: send only the fields that changed, and write
     nothing when the data is the same. A second identical sync cycle must
     make zero writes.
   - Pick one value deterministically when sources disagree (for example,
     the port that learned the fewest MACs). Never "first answer wins": the
     field flips on every run and rewrites the record each time.
   - Stamp timestamps such as `last_seen` at most daily, not on every pass.
   - A feeder that adds or changes a sync path adds a test with a counting
     fake client: the second cycle makes no writes, and a cached list is not
     re-fetched inside its TTL.

## 5. Security and secrets

1. **Credentials go in the vault.**
   - When a Credential Vault is configured (any provider), every password,
     API key and token is stored there, and the record keeps only a
     `vault_credential {bucket, name}` reference. With no vault, secrets are
     Fernet-encrypted on disk.
   - A new module or field uses the vault from day one. Recipe: the
     `add-vault-secret-field` skill.
   - Wire the field through `instance_vault` (`SECRET_FIELDS`,
     `validate_ref`, `strip_inline_secrets`, `overlay`).
   - The WebUI field gets a vault picker (`vaultPicker`, `vaultSecret`), and
     inline secret inputs are hidden while a vault is enabled.
   - List routes return only an `*_set` flag, never the secret. A blank
     re-save must not strip the stored credential.
2. **Never log or expose secrets.**
   - Log whether a secret is set, or mask it, including secrets nested in
     command payloads.
   - No secret reaches the browser, not even the break-glass password in
     devtools.
   - Keep secrets out of chat, issues and commits.
3. **Safe outbound URLs.** Every outbound URL setting passes
   `safe_external_url` when it is saved.
4. **TLS.** Traffic is always encrypted. Hub↔spoke verification is currently
   opt-in at install time (`--tls-verify` / `--tls-ca-cert`), never by
   editing `.env`. The goal is a hub-issued internal CA with automatic
   renewal and mTLS, so add no new `verify=False` paths. An unapproved spoke
   can do nothing but wait for approval.
5. **Tenant boundaries are security boundaries.**
   - A superadmin can never remote-control a tenant's spoke.
   - Root or sudo on a tenant agent asks for a one-time password.
6. **Vendor-neutral wording.** The UI says "Key Vault" and "NSG", not
   "Azure Key Vault". Azure and OCI providers are mutually exclusive for
   each function.

## 6. Multitenancy

1. **Everything is tenant-scoped.**
   - Every page, API route and spoke fan-out is scoped to the caller's tenant
     (`access.resolve_tenant`, `read_scope`/`write_scope`) or explicitly
     guarded as admin-only.
   - Compare tenants with `same_tenant`/`tenant_slug_matches`, not `==`.
   - Recipe: the `tenant-scope-auditor` agent.
2. **Admin view equals tenant view.** A global admin sees a tenant's data
   only after switching into that tenant, and then sees exactly what the
   tenant sees. "Shared" is visible to everyone.
3. **Own instance beats shared.**
   - Once a tenant has its own instance of a module (ClearPass, Proxmox,
     NW, …), the shared one is hidden for that module.
   - A tenant's systems are never managed from outside the tenant, except
     shared-tenant spokes.
   - IP-based tenant inference applies only to shared-tenant spokes; an
     explicit tenant assignment always wins.
4. **No cross-tenant leaks.**
   - Sync never matches or merges records across tenants.
   - Labels and credentials never cross tenants. With no credential, show no
     credential label.
   - When a device is rediscovered (by serial, MAC, IP or name) under a
     different tenant assignment, it moves to that tenant; it is not
     duplicated.
5. **Onboarding.** A tenant onboards only its own devices. Approval is
   automatic only when tenant-id, tenant-name and PSK are all supplied;
   otherwise a tenant admin approves.
6. **LM is the only front end.** Tenants never see backend tools such as
   Zabbix.
7. **Hub settings are defaults.** A setting on the hub is the default for
   every spoke, and can be overridden per spoke.

## 7. WebUI

1. **Plain JavaScript, no build.** There is no npm or Node build anywhere.
   - Views are ES modules (`createView`), markup is built with `safeHtml`,
     and events are wired with `data-action` + `delegate()`.
   - No new `escapeHtml(`, inline `on*=` handlers or `window.x =` globals;
     the ratchet test fails on any increase.
   - Recipe: the `add-webui-view` skill.
2. **HPE Design System.** Use the tokens in `lm/docs/webui-style.md`, in AB
   too. Copy the pattern of an existing page rather than inventing a new
   one.
3. **No UUIDs in the UI.** Show human names. UUIDs belong only on diagnostic
   pages.
4. **Behaviour.**
   - Cancel or ignore stale async renders when the user navigates away.
   - Leaving a tab and coming back resets it to its first view.
   - Auto-refresh only data pages, never config or form pages.
   - Send no-cache headers on every page.
5. **Controls.**
   - Save buttons go on the right. Disable Save while a precondition is
     missing, and confirm the write actually succeeded.
   - Use dropdowns filled from the source system for known values (regions,
     NetBox sites), not free text.
   - Select-all applies only to the current view. Protected objects
     (templates, infrastructure VMs) can't be bulk-deleted.
6. **Feedback.**
   - Use toasts, not confirmation pop-ups, for routine actions. The loading
     toast sits bottom-right, above the footer.
   - Never show an error toast for a restart we caused.
   - The footer is always visible, with the versions and the Update button.
   - Prefer tooltips (`title=`) to banners, and keep them in step with the
     docs.
7. **Tables.** Table text doesn't wrap; columns size to their content.

## 8. Code quality

1. **Readable and small.** Use the fewest lines that stay clear, and plain
   Python. Code a human can follow beats clever code. Match the style of the
   surrounding code.
2. **Documented in the code.**
   - Every Python function has a docstring, and every JS function a `//`
     comment.
   - A function longer than about 15 lines has short step comments.
   - Explain *why* for any non-obvious or owner-directed choice, so it still
     makes sense weeks later.
   - Identity, JIT provisioning and other security-sensitive code gets heavy
     in-code docs.
3. **Few dependencies.** Add a package only when there is no reasonable
   alternative.
4. **Share, don't copy.**
   - Call or move an existing helper instead of pasting it.
   - When you replace code, delete the old path; leave no dead functions.
   - Check you are editing the live path, not a preserved legacy one.
5. **Twins stay in step.** When a file has a twin (hub↔spoke copies,
   Linux↔Windows scripts, pipeline files), change both copies the same way.
   `lm/.github/twins.json` lists the pairs. Recipe: the `dual-copy-guard`
   skill.
6. **Don't block the event loop.**
   - In `async def`, wrap sleeps, subprocesses, sync HTTP and file I/O in
     `asyncio.to_thread`.
   - Bound fan-out with a semaphore or queue.
   - Never start a poll while the last one is still running.
7. **Python 3.11**, FastAPI + `websockets` + `asyncio`. CI runs one pytest
   process per component, because components share module names.

## 9. Tests and docs

1. **Test what you change.** Bug fixes get a test that fails without the
   fix. Sensitive areas (identity, JIT provisioning, security) get
   exhaustive tests.
2. **Docs ship with the change.**
   - A new route, WebUI page, form field or setting updates `docs/*.md`
     (or the README / `MODULE_REQUIREMENTS.md`) in the same change.
   - `lm/docs/` is canonical for the fleet; update it first. Read the
     canonical page before changing a module: it is usually more current
     than the repo's README.
   - Document behaviour exactly as the owner described it, including
     operational caveats.
3. **Docs-only PRs** are reviewed but not gated on accuracy.
4. **Timing tests must not flake on a busy CI runner.** Do all setup
   (imports, parsing, building fakes) before starting the clock, and keep
   at least a 2x gap between the pass bound and the failure case (for
   example parallel 0.5s, serial 1.0s, bound 0.7s). A flaky required test
   blocks every promotion behind it.

## 10. AppBuilder (AB)

1. AB follows every rule above. It also loads this file, the agents in
   `lm/.github/agents/` (matched by `metadata.ab-match`) and the skills in
   `lm/.claude/skills/`.
2. AB uses only frontier models from its curated list, picked automatically
   from the configured list. Ollama can be selected but is never picked
   automatically.
3. AB never lowers its 0.90 confidence bar, and never loops on remediation
   when the results haven't changed.
4. Every AB feature has a config knob. Work submitted through chat goes
   through the same guardrails as a PR.
5. AB's tests click every button, except a denylist of destructive ones.

## 11. Fleet facts (reference)

- **Transport.** WebSocket to the hub on 443, mailbox pattern, push, ack and
  retry. Heartbeat every 30s; a spoke shows yellow at 120s or more and red at
  300s or more. The hub queues messages for an offline spoke for 24h.
- **Installers.** Common flags: `--hub` (a bare hostname is normalised to
  `wss://…:443`), `--id`/`--name`, `--secret`, `--hub-secret`,
  `--all-prereqs`, and `--tls-verify`/`--tls-ca-cert`.
- **Multitenancy.** Isolation rides on Proxmox labels and NetBox tenant IDs.
  New resources carry their tenant context.
- **Archived repos.** `dhcp` and `dns` are archived and read-only; their code
  lives in `lm`.
- **Where to read more.** `lm/AGENTS.md` (hub map),
  `lm/MODULE_REQUIREMENTS.md`, `lm/docs/architecture-topology.md`, and
  `lm/docs/agents-and-skills.md` (agents and skills).

## Enforcement map

These rules are checked automatically. Everything else is checked by
reviewers and the AB panel.

| Check | Where | Rules |
| :--- | :--- | :--- |
| `fleet_checks.py` (vault wiring, storage format, new deps, hard-coded hosts, VERSION, docstrings and comments, SSRF, tenant scope and first-site fallbacks, JS syntax, WebUI ratchet, UUIDs, secret logging, blocking async, copies and dead code, docs) | `checks` job in `ci.yml` (runs before `test`) on every PR and dev push; AB on every fix and review | 2.2, 3.7, 4.1, 5.1–5.3, 6.1, 6.4, 7.1, 7.3, 8.2–8.4, 8.6, 9.2 |
| `ruff_new.py` (new lint findings only) | `checks` job in `ci.yml` | 8.1, 8.4 |
| `check_twins.py` (twin pairs, fleet-owned copies) | `checks` job in `ci.yml` | 8.5, 2.7 |
| `pip-audit` (when requirements change; advisory) | `checks` job in `ci.yml` | 8.3 |
| WebUI ratchet, secret hygiene and JS syntax tests | lm `ci.yml` | 7.1, 5.2 |
| `check-direction` (branch flow), VERSION guard | `branch-flow` / `promote.yml` | 2.1, 2.2 |
| PR intent statement | AB review panel | 2.4 |
| Call-rate and NetBox churn health check (writes per hour, flapping fields, commands called far above their refresh interval) | AB hub health check | 4.8 |

See `lm/docs/ci-checks.md` for what each check does and how to run it
locally.
