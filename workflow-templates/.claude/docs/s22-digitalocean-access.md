# CLAUDE.md §22: full reference

This is the full text of CLAUDE.md §22. CLAUDE.md keeps the
rules every session needs and points here for the rest. Both are binding;
where this file adds detail, follow it. It reaches consumer repos with the
rest of `.claude/` through the `@stable` sync.

## §22. DigitalOcean Access (MANDATORY)

The session environment provides a `DIGITALOCEAN_ACCESS_TOKEN` env var — a
DigitalOcean API token. This section applies in this repo and in every
consumer repo that receives this file via the `@stable` sync. It splits
DigitalOcean operations into two postures: **reads are self-serve** (act,
do not ask), **mutations are ask-first** (always confirm before acting).

### A) Read Operations — Act, Do Not Ask

When a task needs data from DigitalOcean, pull it yourself with the token
instead of asking the user to fetch it or to run commands on your behalf.
This is an explicit carve-out from §2 for **read-only** DigitalOcean calls —
needing DO-hosted data is not, by itself, a reason to stop and ask.
Self-serve reads include (non-exhaustively):

- listing and inspecting Apps, Droplets, managed databases, volumes, and
  load balancers, and their specs/configuration;
- reading deployed app-level env vars to verify that a name or value
  matches what the code and docs expect;
- fetching build, deploy, and runtime logs to diagnose failures;
- checking deployment status, alert policies, and monitoring/metrics data.

Transport: prefer `doctl` when installed (it honours the
`DIGITALOCEAN_ACCESS_TOKEN` env var natively); otherwise call the REST API
directly:

```
curl -sS -H "Authorization: Bearer ${DIGITALOCEAN_ACCESS_TOKEN}" \
  "https://api.digitalocean.com/v2/<endpoint>"
```

Token hygiene (hard rules):
- Never echo, log, or print the token value; reference it only via env
  expansion (`$DIGITALOCEAN_ACCESS_TOKEN`).
- Never write the token into committed files, PR bodies, issue comments,
  or diagnostic output. Redact it if a tool response contains it.
- If the token is missing or the API returns 401/403, report that to the
  user and continue with what can be done without it — do not retry-loop
  and do not ask the user to run the API calls manually.

### B) Provisioning & Mutations — ALWAYS Ask First

**Never create, modify, resize, or destroy DigitalOcean resources without
asking first** in the §2 Q/A format — even when the need seems obvious,
and even under §12's proactive PR-review scope (this subsection is NOT
superseded by §12). Ask-first operations include (non-exhaustively):

- spinning up new Droplets, Apps, managed databases, volumes, load
  balancers, or any other billable resource;
- resizing, scaling, or migrating existing resources;
- destroying or powering off resources;
- changing a deployed app's spec or env vars, forcing rebuilds/redeploys,
  restoring from backups, or rotating credentials.

The question must name the exact resource type, size/plan, region, and
estimated billing impact where known, so the user approves a concrete
action rather than an intention. After approval, perform the operation
yourself with the token — do not hand the user a command to run (§18).

### C) Resource IDs Live in the Agents File

Each repo's root agents file (`agents.md` in this repo; `AGENTS.md` in
consumer repos that use that casing) carries a `## DigitalOcean resources`
section listing the App / database / Droplet IDs relevant to that repo,
as a table:

```md
## DigitalOcean resources

| Resource | Type | ID | Notes |
|---|---|---|---|
| <name> | app / db / droplet / ... | <id> | <role, environment> |
```

Rules:
- **Look there first.** Before asking for any DigitalOcean resource ID,
  read the repo's agents file. Use recorded IDs without re-asking.
- **Ask when missing.** If a needed ID is not recorded, ask the user for
  it (§2 Q/A format, free-text answer allowed for the ID itself).
- **Save once provided.** After the user supplies an ID, verify it
  resolves with one read API call (§22.A), then add it to the
  `## DigitalOcean resources` section of the agents file **in the same
  PR/commit as the work that needed it**, so it is never asked for again.
  Add the row through an `agents.d/` fragment (§30).
  Create the section if the file lacks it.
- IDs are identifiers under §6 — never remove or rewrite an existing
  entry without the §2 ask flow; correcting a stale ID requires telling
  the user what changed.
