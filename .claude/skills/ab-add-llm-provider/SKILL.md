---
name: ab-add-llm-provider
description: >-
  Use when adding a new LLM provider (or a new API path for an existing
  one, such as Copilot's Responses API) to AppBuilder (ab): the
  llm_client.py dispatch in _call_provider / _request_openai, the
  provider-named circuit breaker and credit-exhaustion handling, model
  listing and online checks in workers.py, model_registry.py
  DEFAULT_MODEL_RULES plus the upgrade_* migration wired into
  llm_migrate.py and routes.py, the Settings template (provider list,
  dropdowns, _PROVIDER_RPM), key redaction, docs/ab.md and the tests. Not
  for tuning one model's rule or panel allowlist settings.
---

# AB: add an LLM provider

ab talks to every provider through `llm_client._call_provider`. Settings,
model discovery, cost routing and the reviewer panels all key off the
provider name, so a new provider has to be registered in each of those
places, and a provider that is only half registered fails silently.
Reference builds:
- ab `4c04a14`: OpenRouter. Touched `llm_client.py`, `workers.py`,
  `templates/index.html` and the docs.
- ab `6228734`: Copilot Responses API path, with
  `test_copilot_responses_api.py`.

Follow-up fixes were about leaked keys and wrong payload fields.

## When to use / when not to use

Use it when:
- a new hosted or local provider is added;
- an existing provider needs a different endpoint or payload shape for
  some models;
- an OpenAI-compatible service needs its own name (separate breaker,
  headers, model list).

Do not use it for:
- changing a model's cost tier or capability in the registry UI;
- panel allowlist or premium-tier settings (`docs/ab.md` config keys).

## Steps (in order) — all in `ab/`

1. **`llm_client.py`**
   - Add a `<NAME>_BASE_URL` constant (and `<NAME>_HEADERS` if needed) next
     to `OPENROUTER_BASE_URL`, with a comment on why the provider is
     distinct.
   - In `_call_provider`, add an `if p == "<name>":` branch.
     - For an OpenAI-compatible provider, reuse `_request_openai(...,
       provider_name="<name>", extra_headers=...)` so it gets **its own**
       circuit-breaker and credit key in `_llm_retry_post`. Never share
       `"openai"`'s.
     - Otherwise add a `_request_<name>` function.
   - In `_is_credit_exhaustion`, add the provider to the right family
     (HTTP 402 and body keywords).
   - Keyless providers: update `_provider_is_nokey` **and**
     `model_registry.is_nokey_provider`; they must agree.
   - Send the field names the endpoint accepts. Copilot chat needs
     `max_completion_tokens`, while `_request_openai` keeps `max_tokens`
     for ollama and LM Studio.
2. **`workers.py`**
   - `_check_provider_online`: an online probe for the provider.
   - `_fetch_models_for_provider`: a model-listing branch. Do not reuse the
     generic OpenAI branch: its gpt/o1/o3/o4 filter drops vendor-prefixed
     ids.
   - Route errors through `_model_fetch_reason`.
3. **`model_registry.py`**
   - Add rows to `DEFAULT_MODEL_RULES`: `cost_tier`, `max_complexity`,
     `context_window`, the `supports_*` flags and `speed_tier`. Put the
     specific match patterns before the `*` fallback.
   - Add an idempotent, append-only `upgrade_<name>_model_rules(rules)`
     (use `_append_missing_default_rules`, as
     `upgrade_copilot_model_rules` does), so existing installs get the
     rows.
4. **Wire the upgrade in three places:**
   - `llm_migrate.upgrade_registry`;
   - `routes.settings_page`, the curated view;
   - `routes.save_settings`.

   Each `upgrade_*` call is made in exactly these places.
5. **`templates/index.html`**
   - Add the provider to the `{% for pname in [...] %}` provider list and
     its colour class.
   - Add an `<option value="<name>">` in both the credential dropdown and
     the per-entry dropdown (`selected` when `entry.provider == '<name>'`).
   - Add a default to `_PROVIDER_RPM`.
6. **Keys stay server-side.** Settings and `GET /api/llm/config`
   (`get_llm_config`) send `has_key` only, never `api_key`. Save keeps
   the stored key when the field is untouched. Store real keys in the
   vault (`llm_credentials` / `llm_entries`).
7. **Docs.** Add the provider to the "Providers:" list in `docs/ab.md`,
   plus any provider-specific config.

## Tests

```bash
cd ab && AB_DISABLE_WORKERS=1 /tmp/nbvenv/bin/python -m pytest -q test_model_registry.py test_model_registry_ui.py test_model_selection.py test_llm_credential_redaction.py
cd ab && AB_DISABLE_WORKERS=1 /tmp/nbvenv/bin/python -m pytest -q test_llm_client_instrumentation.py test_llm_structured_output.py test_llm_migrate.py
cd ab && AB_DISABLE_WORKERS=1 /tmp/nbvenv/bin/python -m pytest -q test_copilot_max_completion_tokens.py test_copilot_responses_api.py   # Copilot paths
```
Add a test for the new branch: payload shape, headers, breaker key and
model-list parsing. Model it on `test_openrouter_free_router.py` /
`test_copilot_responses_api.py`, with `requests.post`/`get` mocked.

## Common misses

- **Plaintext keys sent to the browser.** `llm_credentials` / `llm_entries`
  reached the Settings page and `GET /api/llm/config` unredacted, and a
  blank field wiped stored keys on save — ab `2bba55c`
  (`test_llm_credential_redaction.py`).
- **Wrong token field → silent 400.** Copilot GPT models reject
  `max_tokens` with an empty 400. Every OpenAI-family reviewer looked
  broken and the cross-vendor panel rule relaxed silently — ab `0cb85da`.
- **An endpoint the model no longer serves.** Newer Copilot models dropped
  `/chat/completions` (`unsupported_api_for_model`) — ab `6228734` added
  the Responses path.
- **No registry rule → wrong cost tier.** A model with no rule falls to a
  generic `*` rule (Copilot's is `cheap`), so the cost-first picker prefers
  the most expensive models. See the premium-tier notes in `docs/ab.md`.
  Always add rules **and** the `upgrade_*` migration.
- **Shared breaker key.** Before `provider_name` was added, every
  OpenAI-compatible provider tripped `"openai"`'s breaker — see the
  `_request_openai` docstring (ab `4c04a14`).

## Done when

- [ ] `_call_provider` branch; own breaker key; credit-exhaustion family;
      nokey parity
- [ ] online check + model listing in `workers.py`
- [ ] `DEFAULT_MODEL_RULES` rows + `upgrade_<name>_model_rules`, wired in
      `llm_migrate.py`, `settings_page` and `save_settings`
- [ ] Settings template list, both dropdowns, `_PROVIDER_RPM`
- [ ] no `api_key` reaches the browser; untouched key preserved on save
- [ ] `docs/ab.md` provider list; tests above pass

## Fleet rules

- Canonical rules: `lm/.github/fleet-rules.md` (replaces `ab/fleet_rules.md`).
  Relevant sections:
  - **Security and secrets**: Credentials go in the vault; Never log or
    expose secrets; Safe outbound URLs.
  - **Architecture**: Nothing hard-coded.
  - **AppBuilder (AB)**.
  - **Tests and docs**.
- Every Python function has a docstring plus step comments.
- Promotion only via `promote.yml` / `lm/.github/scripts/promote_fleet.sh`;
  never open a PR to qa or main by hand.
