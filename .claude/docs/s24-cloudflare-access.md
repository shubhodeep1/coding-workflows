# CLAUDE.md §24: full reference

This is the full text of CLAUDE.md §24. CLAUDE.md keeps the
rules every session needs and points here for the rest. Both are binding;
where this file adds detail, follow it. It reaches consumer repos with the
rest of `.claude/` through the `@stable` sync.

## §24. Cloudflare Access (MANDATORY)

The session environment provides two Cloudflare credential env vars for
managing Cloudflare Workers. Each value is a single string in the format
`<account_id>:<api_token>` — the Cloudflare account ID, a literal colon,
then an API token authorized to create and edit Workers in that account.
This section applies in this repo and in every consumer repo that receives
this file via the `@stable` sync. It follows the same posture split as §22
and §23: **reads are self-serve**, **Worker deploys the task calls for are
self-serve**, **destructive and account-level writes are ask-first**.

### A) Credential Selection & Parsing

Pick the credential by the site the work targets — the two vars are
different Cloudflare accounts and are NOT interchangeable:

| Env var | Covers | Use for |
|---|---|---|
| `FUNTOKEN_IO_CF` | `funtoken.io` | Workers serving the funtoken.io website |
| `FT_GAMES_CF` | `ft.games`, `5m.fun` | Workers serving the ft.games and 5m.fun websites |

If the target site or zone is not one of the domains above, neither
credential covers it — STOP and ask (§2 Q/A format) rather than guessing
which account to use.

Split on the **first** colon only (API tokens can theoretically contain
further separators; account IDs are 32 hex chars and never contain `:`),
referencing the value only via env expansion:

```
CF_ACCOUNT_ID="${FUNTOKEN_IO_CF%%:*}"
CF_API_TOKEN="${FUNTOKEN_IO_CF#*:}"
```

Transport: prefer `wrangler` when installed — export the split halves as
`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, which wrangler honours
natively. Otherwise call the REST API directly:

```
curl -sS -H "Authorization: Bearer ${CF_API_TOKEN}" \
  "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/workers/scripts"
```

Health-check gotcha: these are **account-owned** tokens, so
`GET /client/v4/user/tokens/verify` returns 401 `Invalid API Token` for
them even when they work. Do not diagnose a credential as invalid from
that endpoint — probe with an account-scoped read instead (the
`workers/scripts` list above is the canonical probe).

### B) Read Operations — Act, Do Not Ask

When a task needs data from Cloudflare, pull it yourself with the matching
credential instead of asking the user to fetch it. This is an explicit
carve-out from §2 for **read-only** Cloudflare calls — needing
Cloudflare-hosted state is not, by itself, a reason to stop and ask.
Self-serve reads include (non-exhaustively):

- listing Workers and reading a Worker's script content, settings,
  bindings, routes, custom domains, and cron triggers;
- listing deployments/versions of a Worker and reading rollback state;
- reading zone and DNS records for the covered domains to verify routing;
- listing KV namespaces, R2 buckets, and D1 databases bound to a Worker
  (and reading individual values needed to diagnose a defect);
- tailing Worker logs (`wrangler tail`) and reading analytics/metrics to
  diagnose failures.

### C) Worker Deploys & Edits — Self-Serve When the Task Calls for Them

Creating and editing Workers is what these credentials exist for. When the
user's task is to build, fix, or update a Worker for a covered site,
perform the deploy yourself — do not hand the user a command to run (§18)
and do not add an extra approval round for the deploy the task already
implies. Self-serve writes include:

- creating a new Worker or uploading a new version of an existing Worker
  script for a covered site;
- editing a Worker's bindings, environment variables (non-secret), routes
  on covered zones, and cron triggers, when the task requires it;
- rolling back a deployment this session itself made that turns out to be
  broken.

Constraints that ride along:

- **Only for the covered domains.** A credential is scoped to its account;
  never use it to touch Workers or zones unrelated to the task.
- **Never execute unmerged code with credentials.** Project checks, package
  install lifecycle scripts, builds, and `wrangler deploy --dry-run` can
  execute repository code. Never run them from an unmerged PR or branch in
  a shell holding session credentials, even before deploy approval.
- **Deploy only a verified default-branch commit.** Require the target
  repository's default branch to be protected. Resolve its commit SHA via
  the GitHub API, fetch it, and deploy from a clean detached worktree pinned
  to that SHA, not a PR checkout or moving ref. Verify the worktree HEAD and
  cleanliness and recheck the API branch tip and protection immediately
  before deploying. If any check fails or cannot be verified, block the
  deploy; do not offer a manual Worker deploy as a workaround.
- **Validate without credentials.** Prefer GitHub check-runs for the pinned
  SHA; do not deploy while checks are failing or pending. Run local checks
  (typecheck, tests, `wrangler deploy --dry-run` where available) only in a
  credential-free, no-egress sandbox with an isolated home and no host
  credentials or Docker socket mounted. If isolation is unavailable, skip
  local checks and rely on check-runs; never run them in the credential-
  bearing session shell.
- **Limit deploy credential exposure.** Deploy only from the verified
  worktree, matching the target Worker against the repo's §24.F registry.
  Give the deploy process only the matching site's Cloudflare account ID and
  API token, with a clean home and unrelated session credentials stripped.
  These account-owned credentials are not Worker-scoped; provisioning a
  narrower token is an operator task, not a reason to expose additional
  credentials during deployment.
- **Preserve rollback.** Prefer versioned uploads/gradual rollouts where
  the account supports them; never delete the previous version as part of
  a deploy.

### D) Destructive & Account-Level Writes — ALWAYS Ask First

**Never perform these without asking first** in the §2 Q/A format — even
when the need seems obvious, and even under §12's proactive PR-review
scope (this subsection is NOT superseded by §12):

- deleting a Worker, route, custom domain, or cron trigger;
- creating, modifying, or deleting DNS records, or changing zone settings
  (SSL/TLS mode, caching rules, WAF, redirects);
- deleting or wiping KV namespaces, R2 buckets, or D1 databases, and any
  bulk data deletion inside them;
- setting or rotating Worker secrets (`wrangler secret put`), or rotating
  the API tokens themselves;
- account-level configuration: members, billing, subscriptions, zone
  additions/removals.

The question must name the exact account (which env var), zone, Worker,
and object, and what the operation changes, so the user approves a
concrete action rather than an intention. After approval, perform the
operation yourself with the credential — do not hand the user a command
to run (§18).

### E) Token Hygiene and Degradation (hard rules)

- Never echo, log, or print the credential or either of its halves;
  reference them only via env expansion (`$FUNTOKEN_IO_CF`,
  `$FT_GAMES_CF`, or split-out shell variables as in §24.A).
- Never write the credential into committed files, PR bodies, issue
  comments, commit messages, or diagnostic output. Redact it if a tool
  response contains it. The account ID half is less sensitive than the
  token half, but treat the combined value as a secret throughout.
- If a var is missing or the API returns 401/403, **say so once**, report
  which credential failed, and continue with what can be done without it —
  do not retry-loop, and do not ask the user to run the calls manually
  (§18).

### F) Worker & Zone Identifiers Live in the Agents File

Each repo's root agents file (`agents.md` in this repo; `AGENTS.md` in
consumer repos that use that casing) carries a `## Cloudflare resources`
section listing the Worker names, zone IDs, routes, and namespace IDs
relevant to that repo, as a table:

```md
## Cloudflare resources

| Resource | Type | ID / name | Credential | Notes |
|---|---|---|---|---|
| <name> | worker / zone / kv / r2 / d1 / route | <id or name> | FUNTOKEN_IO_CF / FT_GAMES_CF | <role, environment> |
```

Rules (same as §22.C):

- **Look there first.** Before asking for any Cloudflare identifier, read
  the repo's agents file. Use recorded entries without re-asking. Zone
  IDs are also discoverable self-serve via a §24.B read — prefer looking
  them up over asking.
- **Ask when missing.** If a needed identifier is neither recorded nor
  discoverable via a read, ask the user (§2 Q/A format, free-text answer
  allowed for the identifier itself).
- **Save once provided.** Verify a supplied identifier resolves with one
  read call (§24.B), then record it in the agents file **in the same
  PR/commit as the work that needed it**. Create the section if the file
  lacks it. Add it through an `agents.d/` fragment (§30).
- Entries are identifiers under §6 — never remove or rewrite an existing
  entry without the §2 ask flow.

### G) Interactive Sessions Only

- `FUNTOKEN_IO_CF` and `FT_GAMES_CF` are session env vars, like
  `DIGITALOCEAN_ACCESS_TOKEN` (§22) and the session reading of `GH_TOKEN`
  (§23). No Actions workflow reads them, and none may be added that does:
  never commit a workflow, script, or hook that reads either var from the
  session environment. Actions-side Cloudflare work would need its own
  repo secret and its own review — route that through §2.
- The unattended pipelines read `unattended_system_instructions.md` and
  never see this file, so §24 grants no new access to any codex-driven
  phase.
