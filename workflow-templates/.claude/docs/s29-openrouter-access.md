# CLAUDE.md §29: full reference

This is the full text of CLAUDE.md §29. CLAUDE.md keeps the
rules every session needs and points here for the rest. Both are binding;
where this file adds detail, follow it. It reaches consumer repos with the
rest of `.claude/` through the `@stable` sync.

## §29. OpenRouter Access (MANDATORY)

The session environment provides an `OR_MGMT_KEY` env var: an OpenRouter
**management** (provisioning) key for the account whose API keys the
pipelines use. This section applies in this repo and in every consumer repo
that receives this file via the `@stable` sync. It follows the same posture
split as §22–§24: **reads are self-serve**, **key and account mutations are
ask-first**.

`OR_MGMT_KEY` is not `OPENROUTER_API_KEY`. The Actions secret
`OPENROUTER_API_KEY` is an inference key the workflows send model requests
with (§6: name unchanged). The management key cannot run models; it reads and
manages the account and its keys.

### A) Read Operations — Act, Do Not Ask

Whenever a task needs OpenRouter data — spend, token usage, per-model cost,
which key is spending, remaining credit — pull it yourself with `OR_MGMT_KEY`
instead of asking the user for it, estimating it from list prices, or
reconstructing it from workflow logs. This is an explicit carve-out from §2
for **read-only** OpenRouter calls. Do it automatically, every time the data
is needed.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/activity` | One row per day × model × provider endpoint for the last 30 completed UTC days: `usage` (USD), `requests`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`. Optional `?date=YYYY-MM-DD` for a single day. |
| `GET /api/v1/keys` | Every API key on the account: `name`, `label`, `disabled`, `limit`, and USD `usage` / `usage_daily` / `usage_weekly` / `usage_monthly`. |
| `GET /api/v1/credits` | `total_credits` and `total_usage` (USD) for the account. |

Transport (no CLI exists; use the REST API):

```
curl -sS -H "Authorization: Bearer ${OR_MGMT_KEY}" \
  "https://openrouter.ai/api/v1/activity"
```

Limits that shape every analysis (verified 2026-10-06):

- **30 days only.** `/activity` rejects dates older than the last 30
  completed UTC days with HTTP 400, and today's partial day is not included.
  Anything older has to come from saved data (for example a report under
  `analysis/`), not from the API.
- **Split by model, not by key or repo.** `/activity` is account-wide. Cost
  can be attributed to a pipeline role only when that role is the sole user
  of the model slug. Check the slugs in `.github/workflows/*.yml` and
  `scripts/` before attributing, and say when a slug is shared (for example
  the reviewer-panel slot and the cross-reviewer summariser).
- **Per-key spend is totals only.** `/keys` gives each key's daily, weekly
  and calendar-month USD totals with no model breakdown. `usage_monthly` is
  the current calendar month, not a rolling 30 days.
- Some direct OpenRouter calls set attribution headers: `scripts/analyze_soft_errors.py`
  sets `HTTP-Referer` and `X-Title`, and `scripts/summarize_unselected_runs.py`
  sets `X-Title`. The management API's account-wide `/activity` and `/keys`
  totals do not provide a workflow, phase, or consumer-repo breakdown; do not
  infer one from these headers or from a model slug shared by multiple callers.

Write downloaded responses under the session scratchpad, not the repo
(§13).

### B) Key & Account Mutations — ALWAYS Ask First

**Never perform these without asking first** in the §2 Q/A format, even
under §12's proactive PR-review scope (this subsection is NOT superseded by
§12):

- creating, deleting, disabling, re-enabling, or renaming an API key
  (`POST` / `PATCH` / `DELETE /api/v1/keys/...`);
- setting or changing a key's spend `limit` or limit reset;
- buying credits or changing any billing, organization, or workspace
  setting.

The question must name the exact key (its `name` and `label`, never the key
value) and what the operation changes. After approval, perform it yourself
with the key; do not hand the user a command to run (§18).

### C) Token Hygiene and Degradation (hard rules)

- Never echo, log, or print `OR_MGMT_KEY`; reference it only via env
  expansion (`$OR_MGMT_KEY`).
- Never write it into committed files, PR bodies, issue comments, commit
  messages, or diagnostic output. Redact it if a tool response contains it.
  A newly created key's value, returned once by `POST /api/v1/keys`, gets the
  same treatment.
- If the var is missing or the API returns 401/403, **say so once** and
  continue with what can be done without it. Do not retry-loop, and do not
  ask the user to run the calls manually (§18).

### D) Interactive Sessions Only

- `OR_MGMT_KEY` is a session env var, like `DIGITALOCEAN_ACCESS_TOKEN` (§22)
  and the Cloudflare credentials (§24). No Actions workflow reads it, and
  none may be added that does: never commit a workflow, script, or hook that
  reads it from the session environment. Actions-side spend reporting would
  need its own repo secret and its own review; route that through §2.
- The unattended pipelines read `unattended_system_instructions.md` and
  never see this file, so §29 grants no new access to any codex-driven
  phase.
